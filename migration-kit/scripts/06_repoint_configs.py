# -*- coding: utf-8 -*-
"""阶段 7 · 配置改指（批量替换旧网段）

行为:
    * 只改文本文件，跳过二进制/日志/数据目录
    * 每个文件首次修改前备份为 <file>.bak_ip（已存在则保留最原始那份）
    * 只替换 IP 前缀（如 10.0.0. -> 10.1.0.），不做正则
    * 默认 dry-run，只列清单

用法:
    python scripts/06_repoint_configs.py --old 10.0.0. --new 10.1.0.            # 看清单
    python scripts/06_repoint_configs.py --old 10.0.0. --new 10.1.0. --apply    # 执行
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

EXCLUDE = ("--exclude-dir=mysql --exclude-dir=binlog --exclude-dir=logs --exclude-dir=log "
           "--exclude-dir=node_modules --exclude-dir=.git --exclude='*.log' --exclude='*.ibd' "
           "--exclude='*.bak_ip' --exclude='*.jar' --exclude='*.gz'")

FIND = "grep -rlI '{old}' {roots} " + EXCLUDE + " 2>/dev/null"

REPLACE = (
    "f='{f}'; "
    "[ -f \"$f.bak_ip\" ] || cp -p \"$f\" \"$f.bak_ip\"; "
    "sed -i 's/{old}/{new}/g' \"$f\""
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="旧 IP 前缀，如 10.0.0.")
    ap.add_argument("--new", required=True, help="新 IP 前缀，如 10.1.0.")
    ap.add_argument("--roots", default="", help="搜索根目录，默认 /data/app /opt /etc")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    roots = args.roots or " ".join([cfg["paths"]["app_root"], "/opt", "/etc"])

    for name in cfg["dst"]:
        h = cfg["hosts"][name]
        log("=== {} ({}) ===".format(name, h["ip"]))
        try:
            conn = get_conn(h["ip"], h["user"], h["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        rc, out, _ = run(conn, FIND.format(old=args.old, roots=roots), 600)
        files = [x.strip() for x in out.splitlines() if x.strip()]
        log("  命中文件 {} 个".format(len(files)))
        for f in files[:20]:
            log("    " + f)
        if len(files) > 20:
            log("    ... 其余 {} 个".format(len(files) - 20))

        if not args.apply or not files:
            continue

        changed = 0
        for f in files:
            cmd = REPLACE.format(f=f.replace("'", "'\\''"), old=args.old, new=args.new)
            rc2, _, err2 = run(conn, cmd, 120)
            if rc2 == 0:
                changed += 1
            else:
                log("    替换失败 {}: {}".format(f, err2.strip()[:120]))
        log("  已替换 {} 个文件（备份后缀 .bak_ip）".format(changed))

        rc3, out3, _ = run(conn, FIND.format(old=args.old, roots=roots), 600)
        left = [x for x in out3.splitlines() if x.strip()]
        log("  复核: 仍含旧网段的文件 {} 个 {}".format(len(left), left[:5]))

    log("提醒: 容器 Env / Cmd 里的地址需要重建容器才会生效")
    close_all()


if __name__ == "__main__":
    main()
