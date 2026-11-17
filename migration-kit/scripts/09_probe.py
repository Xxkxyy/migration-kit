# -*- coding: utf-8 -*-
"""阶段 1 · 探测执行器（把 lib/probe.sh 铺到所有机器上跑一遍）

产物:
    inventory/probe/<host>-<section>.txt     原始输出（用于 diff、留证）
    inventory/probe/_summary.md              关键指标汇总表

用法:
    python scripts/09_probe.py                       # 全部主机 + 全部段
    python scripts/09_probe.py --sections sys,disk    # 只跑指定段
    python scripts/09_probe.py --only new-db          # 只跑一台
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "lib", "probe.sh")
OUT = os.path.join(os.path.dirname(HERE), "inventory", "probe")

ALL_SECTIONS = ["sys", "disk", "net", "docker", "mysql", "app", "mid", "verify"]


def metric(text, key):
    """从段输出里抓单行指标，抓不到返回 ''。"""
    for line in text.splitlines():
        if line.strip().startswith(key):
            return line.strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default=",".join(ALL_SECTIONS))
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    with open(PROBE, "r", encoding="utf-8") as fh:
        body = fh.read().replace("\r\n", "\n")

    os.makedirs(OUT, exist_ok=True)
    cfg = load_config()
    summary = []

    for name, h in cfg["hosts"].items():
        if args.only and name != args.only:
            continue
        log("=== {} ({}) role={} ===".format(name, h["ip"], h["role"]))
        try:
            conn = get_conn(h["ip"], h["user"], h["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        sftp = conn.open_sftp()
        with sftp.open("/tmp/_probe.sh", "w") as fh:
            fh.write(body)
        sftp.close()

        collected = {}
        for sec in sections:
            rc, out, err = run(conn, "bash /tmp/_probe.sh {}".format(sec), timeout=args.timeout)
            text = out if rc == 0 else (out + "\n[stderr]\n" + err)
            path = os.path.join(OUT, "{}-{}.txt".format(name, sec))
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            collected[sec] = text
            log("  {:<7} {:>6} 行 -> {}".format(sec, len(text.splitlines()), os.path.basename(path)))

        # 关键指标（只抓能判断"能不能迁/迁完对不对"的）
        row = {"name": name, "ip": h["ip"], "role": h["role"]}
        row["non_up"] = len([x for x in collected.get("docker", "").splitlines()
                             if "###" not in x and " Up " not in x and "|" in x])
        row["disk"] = metric(collected.get("disk", ""), "/data")
        summary.append(row)

    path = os.path.join(OUT, "_summary.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 探测汇总\n\n| 主机 | IP | 角色 | 非Up容器 | /data |\n|---|---|---|---|---|\n")
        for r in summary:
            fh.write("| {} | {} | {} | {} | {} |\n".format(
                r["name"], r["ip"], r["role"], r["non_up"], r.get("disk", "")[:40]))
    log("汇总: {}".format(path))
    close_all()


if __name__ == "__main__":
    main()
