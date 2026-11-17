# -*- coding: utf-8 -*-
"""阶段 9 · 切流与回退

纪律:
    * 停旧容器只 stop，绝不 rm / rmi
    * 任何一步失败，立即 --rollback
    * 回退目标 < 5 分钟

用法:
    python scripts/13_cutover.py --precheck            # 切流前检查（只读）
    python scripts/13_cutover.py --stop-old --confirm YES-STOP-OLD
    python scripts/13_cutover.py --delta               # 停写后最后一次增量
    python scripts/13_cutover.py --smoke               # 冒烟
    python scripts/13_cutover.py --rollback --confirm YES-ROLLBACK
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402


def http_probe(ip, port, path="/", timeout=6):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.sendall(("GET {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\n\r\n"
                   .format(path, ip, port)).encode())
        data = s.recv(1024).decode("utf-8", "replace").splitlines()
        s.close()
        return data[0] if data else "(空)"
    except Exception as exc:                          # noqa: BLE001
        return "ERR {}".format(exc)


def precheck(cfg):
    ok = True
    log("① 目标端容器")
    for name in cfg["dst"]:
        h = cfg["hosts"][name]
        if h["role"] == "db":
            continue
        conn = get_conn(h["ip"], h["user"], h["pwd"])
        up = run(conn, "docker ps -q | wc -l", 120)[1].strip()
        bad = run(conn, "docker ps -a --format '{{.Status}}' | grep -vc '^Up' || true", 120)[1].strip()
        log("   {:<10} 运行={} 非Up={}".format(name, up, bad))
        ok = ok and bad in ("0", "")

    log("② 端到端")
    apps = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "app"]
    if apps:
        ip = cfg["hosts"][apps[0]]["ip"]
        line = http_probe(ip, 4431, "/authserver/oauth/token")
        log("   {}:4431 -> {}".format(ip, line))
        ok = ok and "5" not in line.split(" ")[1:2][0][:1] if line.startswith("HTTP") else False

    log("③ 目标端慢查询 / 统计")
    dbn = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]
    h = cfg["hosts"][dbn]
    conn = get_conn(h["ip"], h["user"], h["pwd"])
    client = cfg["mysql"]["dst_client"]
    out = run(conn, '{} -N -e "select count(*) from mysql.slow_log" 2>/dev/null'.format(client), 120)[1]
    log("   慢日志条数: {}".format(out.strip() or "0"))
    zero = run(conn, '{} -N -e "select count(*) from mysql.innodb_table_stats s join '
                     'information_schema.tables t on t.table_schema=s.database_name and '
                     't.table_name=s.table_name where s.n_rows=0 and t.data_length>20000 and '
                     's.database_name not in (\'mysql\',\'sys\')" 2>/dev/null'.format(client), 300)[1]
    log("   非空表统计为 0: {}（必须为 0）".format(zero.strip()))
    ok = ok and zero.strip() == "0"

    log("")
    log("切流前检查: {}".format("✔ 可以切" if ok else "✘ 有未通过项，不要切"))
    return ok


def stop_old(cfg):
    for name in cfg["src"]:
        h = cfg["hosts"][name]
        if h["role"] == "db":
            continue
        conn = get_conn(h["ip"], h["user"], h["pwd"])
        rc, out, _ = run(conn, "docker ps -q | wc -l", 120)
        log("  {} 停止前运行容器: {}".format(name, out.strip()))
        run(conn, "docker stop -t 30 $(docker ps -q) >/dev/null 2>&1 || true", 900)
        rc, out, _ = run(conn, "docker ps -q | wc -l", 120)
        log("  {} 停止后: {}".format(name, out.strip()))


def rollback(cfg):
    for name in cfg["src"]:
        h = cfg["hosts"][name]
        if h["role"] == "db":
            continue
        conn = get_conn(h["ip"], h["user"], h["pwd"])
        run(conn, "docker start $(docker ps -aq) >/dev/null 2>&1 || true", 900)
        rc, out, _ = run(conn, "docker ps -q | wc -l", 120)
        log("  {} 已恢复运行容器: {}".format(name, out.strip()))


def delta(cfg):
    from importlib import import_module
    mod = import_module("03_rsync_sync")
    log("  执行最后一次增量（等同 03_rsync_sync --apply）")
    sys.argv = ["03_rsync_sync.py", "--apply"]
    mod.main()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--precheck", action="store_true")
    ap.add_argument("--stop-old", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--delta", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--confirm", default="", help="必须等于 YES-STOP-OLD 或 YES-ROLLBACK")
    args = ap.parse_args()

    cfg = load_config()

    if args.precheck:
        precheck(cfg)
    if args.stop_old:
        if args.confirm != "YES-STOP-OLD":
            log("拒绝执行：请加 --confirm YES-STOP-OLD")
        else:
            stop_old(cfg)
    if args.delta:
        delta(cfg)
    if args.smoke:
        apps = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "app"]
        if apps:
            ip = cfg["hosts"][apps[0]]["ip"]
            for port, path in [(4431, "/authserver/oauth/token"), (4431, "/notice/ws")]:
                log("  {}:{} {} -> {}".format(ip, port, path, http_probe(ip, port, path)))
    if args.rollback:
        if args.confirm != "YES-ROLLBACK":
            log("拒绝执行：请加 --confirm YES-ROLLBACK")
        else:
            rollback(cfg)

    close_all()


if __name__ == "__main__":
    main()
