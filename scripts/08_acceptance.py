# -*- coding: utf-8 -*-
"""阶段 8 · 端到端验收

四层验证 + 两个容易被忽略的检查（统计信息、慢查询）:

    ① 进程层  容器是否全部 Up
    ② 注册层  注册中心服务数与名单
    ③ 接口层  从外部经 nginx / 网关拿到"应用自己的业务响应"
    ④ 数据层  关键表行数、非空表统计是否为 0

用法:
    python scripts/08_acceptance.py
    python scripts/08_acceptance.py --probe 10.1.0.197:4431:/authserver/oauth/token
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402


def http_probe(ip, port, path, timeout=8):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.sendall(("GET {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\n\r\n"
                   .format(path, ip, port)).encode())
        data = s.recv(2048).decode("utf-8", "replace")
        s.close()
        head = data.split("\r\n\r\n")[0]
        body = data.split("\r\n\r\n", 1)[1][:120] if "\r\n\r\n" in data else ""
        return head, body
    except Exception as exc:                          # noqa: BLE001
        return "ERR {}".format(exc), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="append", default=[],
                    help="ip:port:path，可多次指定；默认探测认证接口")
    ap.add_argument("--nacos", default="127.0.0.1:8848", help="注册中心地址")
    ap.add_argument("--namespace", default="", help="命名空间 ID")
    args = ap.parse_args()

    cfg = load_config()
    ok = True

    # ① 进程层
    log("① 进程层")
    total = 0
    for name in cfg["dst"]:
        h = cfg["hosts"][name]
        if h["role"] == "db":
            continue
        conn = get_conn(h["ip"], h["user"], h["pwd"])
        up = run(conn, "docker ps -q | wc -l", 120)[1].strip()
        bad = run(conn, "docker ps -a --format '{{.Status}}' | grep -vc '^Up' || true", 120)[1].strip()
        total += int(up or 0)
        log("  {:<10} 运行={} 非Up={}".format(name, up, bad))
        if bad not in ("0", ""):
            ok = False
    log("  合计运行容器: {}".format(total))

    # ② 注册层
    log("② 注册层")
    app_hosts = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "app"]
    if app_hosts:
        h = cfg["hosts"][app_hosts[0]]
        conn = get_conn(h["ip"], h["user"], h["pwd"])
        url = ("http://{}/nacos/v1/ns/service/list?pageNo=1&pageSize=100"
               .format(args.nacos))
        if args.namespace:
            url += "&namespaceId=" + args.namespace
        rc, out, err = run(conn, "curl -s '{}'".format(url), 120)
        log("  " + (out or err).strip()[:400])
        if '"count"' not in out:
            ok = False

    # ③ 接口层
    log("③ 接口层")
    probes = args.probe or ["{}:4431:/authserver/oauth/token".format(
        cfg["hosts"][app_hosts[0]]["ip"])] if app_hosts else []
    for item in probes:
        ip, port, path = item.split(":", 2)
        head, body = http_probe(ip, int(port), path)
        log("  {}:{} {} -> {}".format(ip, port, path, head.splitlines()[0] if head else "?"))
        if body:
            log("    " + body.replace("\n", " ")[:150])
        if "HTTP/1.1 5" in head or head.startswith("ERR"):
            ok = False

    # ④ 数据层
    log("④ 数据层")
    db_name = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]
    h = cfg["hosts"][db_name]
    conn = get_conn(h["ip"], h["user"], h["pwd"])
    client = cfg["mysql"]["dst_client"]
    sql = ("select count(*) from mysql.innodb_table_stats s "
           "join information_schema.tables t on t.table_schema=s.database_name "
           "and t.table_name=s.table_name where s.n_rows=0 and t.data_length>20000 "
           "and s.database_name not in ('mysql','sys')")
    rc, out, err = run(conn, '{} -N -e "{}" 2>&1'.format(client, sql), 300)
    log("  非空表但统计为 0 的数量: {}（必须为 0）".format((out or err).strip()))
    if (out or "").strip() not in ("0",):
        ok = False

    rc, out, _ = run(conn, '{} -N -e "select count(*) from mysql.slow_log" 2>/dev/null'
                     .format(client), 120)
    log("  慢查询条数: {}".format(out.strip() or "0"))

    log("")
    log("验收结论: {}".format("✔ 通过" if ok else "✘ 存在未通过项，请查看上方输出"))
    close_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
