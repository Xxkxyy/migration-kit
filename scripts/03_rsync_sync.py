# -*- coding: utf-8 -*-
"""阶段 3 · 文件数据搬运与校验

设计:
  * 在目标端执行 rsync，从源端拉取（目标端需装 rsync + sshpass）
  * 绝不用 --delete（源端日志轮转会误删目标端已有文件）
  * 每批结束后立刻做「文件数 + 字节总和」双向校验
  * 校验不通过只报告差异，不自动处理

用法:
    python scripts/03_rsync_sync.py                      # dry-run，仅估算
    python scripts/03_rsync_sync.py --apply              # 实际同步
    python scripts/03_rsync_sync.py --apply --verify-only
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

STAT_CMD = ("cd {path} 2>/dev/null && "
            "echo -n 'files='; find . -type f | wc -l; "
            "echo -n 'bytes='; find . -type f -printf '%s\\n' | awk '{{s+=$1}} END {{print s+0}}'")

RSYNC = ("rsync -a --numeric-ids --partial --inplace --info=stats2 "
         "-e \"sshpass -p '{spwd}' ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null\" "
         "root@{sip}:{spath}/ {dpath}/")


def stat(conn, path):
    rc, out, err = run(conn, STAT_CMD.format(path=path), timeout=900)
    files = byts = -1
    for line in out.splitlines():
        if line.startswith("files="):
            files = int(line.split("=")[1] or 0)
        if line.startswith("bytes="):
            byts = int(line.split("=")[1] or 0)
    if files < 0:
        log("  统计失败: {}".format((err or out)[-160:]))
    return files, byts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify-only", action="store_true", help="只做校验，不传输")
    ap.add_argument("--path", default="", help="相对 app_root 的子路径，默认整个 app_root")
    args = ap.parse_args()

    cfg = load_config()
    root = cfg["paths"]["app_root"]
    rel = args.path.strip("/")
    sub = (rel + "/") if rel else ""

    for src_name, dst_name in cfg["pairs"]:
        s, d = cfg["hosts"][src_name], cfg["hosts"][dst_name]
        log("=== {} -> {} : {}{} ===".format(src_name, dst_name, root, "/" + rel if rel else ""))
        try:
            cs = get_conn(s["ip"], s["user"], s["pwd"])
            cd = get_conn(d["ip"], d["user"], d["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        spath = os.path.join(root, sub)
        dpath = os.path.join(root, sub)

        if not args.verify_only:
            run(cd, "mkdir -p {}".format(dpath), 120)
            cmd = RSYNC.format(spwd=s["pwd"], sip=s["ip"], spath=spath, dpath=dpath)
            if not args.apply:
                cmd += " --dry-run"
            rc, out, err = run(cd, cmd, timeout=4 * 3600)
            tail = [x for x in (out + err).splitlines() if x.strip()][-6:]
            log("  rsync rc={} {}".format(rc, " | ".join(tail)))
            if rc != 0:
                log("  !! rsync 非 0，停止该主机后续步骤")
                continue

        sf, sb = stat(cs, spath)
        df, db = stat(cd, dpath)
        same = (sf == df and sb == db)
        log("  源: {} 文件 / {} 字节".format(sf, sb))
        log("  目: {} 文件 / {} 字节".format(df, db))
        log("  {}".format("✔ 一致" if same else "✘ 不一致（差异文件见 --verify-only 后的 diff 步骤）"))

    close_all()


if __name__ == "__main__":
    main()
