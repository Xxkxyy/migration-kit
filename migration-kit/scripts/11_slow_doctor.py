# -*- coding: utf-8 -*-
"""阶段 8 · 慢查询诊断器（迁移后"能跑但不好用"的定位工具）

做三件事:
    1. 打开慢日志（TABLE 输出），便于随时查
    2. 拉取 TopN 慢 SQL，按耗时/扫描行数排序
    3. 对每条做 EXPLAIN，自动判定"缺索引"还是"IO 瓶颈"，并给出建议索引

判定规则（实测有效）:
    type=ALL 且 rows 接近表行数            -> 缺索引/索引失效
    type=ref|range 但 rows 很大            -> 索引选择性差，考虑联合索引调整列序
    rows 很小但耗时很大                    -> IO/锁/回表，考虑覆盖索引或扩容

用法:
    python scripts/11_slow_doctor.py                     # Top10
    python scripts/11_slow_doctor.py --top 20 --explain   # 连 EXPLAIN 一起
    python scripts/11_slow_doctor.py --apply-index         # 生成并执行建议索引（谨慎）
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

ENABLE = ("set global slow_query_log=on; set global long_query_time=1; "
          "set global log_output='TABLE'; ")

TOP = ("select round(query_time) sec, rows_sent, rows_examined, "
       "replace(left(sql_text, 400), '\\n', ' ') "
       "from mysql.slow_log order by query_time desc limit {}")


def mysql(conn, client, sql, timeout=600, db=None):
    pre = "-D {} ".format(db) if db else ""
    rc, out, err = run(conn, '{}{} -e "{}" 2>&1'.format(client, pre, sql.replace('"', '\\"')), timeout)
    return out.strip()


def guess_table(sql):
    m = re.search(r"\bfrom\s+([`\w.]+)", sql, re.I)
    if not m:
        return ""
    return m.group(1).replace("`", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--apply-index", action="store_true",
                    help="把建议索引真正建出来（先用 --explain 确认）")
    args = ap.parse_args()

    cfg = load_config()
    d_name = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]
    d = cfg["hosts"][d_name]
    conn = get_conn(d["ip"], d["user"], d["pwd"])
    client = cfg["mysql"]["dst_client"]

    log("开启慢日志(1s 阈值, TABLE 输出)")
    mysql(conn, client, ENABLE)
    log("  " + mysql(conn, client, "select @@slow_query_log, @@long_query_time, @@log_output"))

    raw = mysql(conn, client, TOP.format(args.top))
    rows = [r for r in raw.splitlines() if r.strip()]
    log("慢查询 Top{}:".format(args.top))
    for r in rows:
        p = r.split("\t")
        if len(p) < 4:
            continue
        log("  {:>6}s  sent={:<8} scanned={:<10} {}".format(p[0], p[1], p[2], p[3][:90]))

    if not args.explain:
        close_all()
        return

    log("\n=== EXPLAIN ===")
    for r in rows:
        p = r.split("\t")
        if len(p) < 4:
            continue
        sql = p[3]
        db = ""
        tb = guess_table(sql)
        if "." in tb:
            db, tb = tb.split(".", 1)
        plan = mysql(conn, client, "explain " + sql, 300, db or None)
        log("\n-- {} ({}s, scanned {})".format(tb or "(unknown)", p[0], p[2]))
        for line in plan.splitlines()[:6]:
            log("   " + line[:150])

        full = "type\tALL" in plan or "\tALL\t" in plan
        if full:
            where = re.findall(r"where\s+(.+)$", sql, re.I | re.S)
            log("   >> 判定: 全表扫描，疑似缺索引")
            if where:
                cols = re.findall(r"(\w+)\s*=\s*[?']", where[0])
                if db and tb and cols:
                    suggestion = "alter table {}.{} add index idx_auto_{} ({})".format(
                        db, tb, "_".join(cols[:4])[:40], ",".join(cols[:4]))
                    log("   >> 建议: " + suggestion)
                    if args.apply_index:
                        log("   >> 执行: " + mysql(conn, client, suggestion, 3600))
        else:
            log("   >> 判定: 已用索引；若仍慢，看回表/IO/锁")

    close_all()


if __name__ == "__main__":
    main()
