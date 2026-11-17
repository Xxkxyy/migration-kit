# -*- coding: utf-8 -*-
"""阶段 6 配套 · 按容器真实端口开放 firewalld

为什么不能"猜端口":
    镜像 EXPOSE 的端口 ≠ 实际监听端口（有的服务在 9600，有的在 10620），
    漏开一个端口，外部访问就是 502/连接超时。

做法:
    读 docker ps 的 Ports 列 → 提取 HostPort → firewall-cmd --add-port，永久生效

用法:
    python scripts/07_firewall.py            # 只列端口
    python scripts/07_firewall.py --apply    # 真正放开
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

PORT_RE = re.compile(r":(\d+)->")


def host_ports(conn):
    rc, out, _ = run(conn, "docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null", 180)
    ports = {}
    for line in out.splitlines():
        name = line.split(" ")[0]
        for p in PORT_RE.findall(line):
            ports.setdefault(int(p), set()).add(name)
    return ports


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--extra", default="", help="额外要开的端口，逗号分隔，如 22,8848")
    args = ap.parse_args()

    cfg = load_config()
    extra = {int(x) for x in args.extra.split(",") if x.strip().isdigit()}

    for name in cfg["dst"]:
        h = cfg["hosts"][name]
        if h["role"] == "db":
            continue
        log("=== {} ({}) ===".format(name, h["ip"]))
        try:
            conn = get_conn(h["ip"], h["user"], h["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        rc, state, _ = run(conn, "systemctl is-active firewalld 2>/dev/null || echo inactive", 60)
        if state.strip() != "active":
            log("  firewalld 未启用，跳过")
            continue

        ports = host_ports(conn)
        for p in extra:
            ports.setdefault(p, set()).add("(extra)")

        rc, cur, _ = run(conn, "firewall-cmd --list-ports 2>/dev/null", 60)
        opened = {int(x.split("/")[0]) for x in cur.split() if x.split("/")[0].isdigit()}
        need = sorted(set(ports) - opened)
        log("  容器端口 {} 个，已开放 {} 个，待开 {} 个: {}".format(
            len(ports), len(opened), len(need), need[:20]))

        if not args.apply or not need:
            continue
        for p in need:
            run(conn, "firewall-cmd --permanent --add-port={}/tcp >/dev/null && "
                      "firewall-cmd --add-port={}/tcp >/dev/null".format(p, p), 60)
        run(conn, "firewall-cmd --reload >/dev/null 2>&1 || true", 120)

        rc, cur2, _ = run(conn, "firewall-cmd --list-ports 2>/dev/null", 60)
        still = [p for p in need if "{}".format(p) not in cur2]
        log("  复核: 未放开 {}".format(still or "无"))

    close_all()


if __name__ == "__main__":
    main()
