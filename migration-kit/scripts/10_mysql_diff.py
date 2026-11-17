# -*- coding: utf-8 -*-
"""阶段 4/5 验收 · 源库与目标库的结构差异比对

比什么（这几项是"看起来搬完了但实际不可用"的高发区）:
    1. 库：数量、字符集、排序规则
    2. 对象：表 / 视图 / 例程 / 触发器 数量（按库分组）
    3. 索引指纹：db.table.index.seq.column 全量比对 —— 漏建一个索引 = 业务慢 100 倍
    4. 账号与授权主机段（换网段后最容易漏）
    5. 指定表的真实行数（COUNT(*) 级，非估算）

用法:
    python scripts/10_mysql_diff.py
    python scripts/10_mysql_diff.py --tables ek_patient.T_PatVisit,ek_patient.T_NurseData
    python scripts/10_mysql_diff.py --count-tables            # 只比对行数
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

SYS_DBS = "('mysql','sys','information_schema','performance_schema')"

SQL = {
    "dbs": ("select schema_name, default_character_set_name, default_collation_name "
            "from information_schema.schemata where schema_name not in " + SYS_DBS),
    "tables": ("select table_schema, table_name from information_schema.tables "
               "where table_schema not in " + SYS_DBS),
    "views": ("select table_schema, table_name from information_schema.views "
              "where table_schema not in " + SYS_DBS),
    "routines": ("select routine_schema, routine_name, routine_type from information_schema.routines "
                 "where routine_schema not in " + SYS_DBS),
    "triggers": ("select trigger_schema, trigger_name from information_schema.triggers "
                 "where trigger_schema not in " + SYS_DBS),
    "indexes": ("select table_schema, table_name, index_name, seq_in_index, column_name, "
                "ifnull(non_unique,'') from information_schema.statistics "
                "where table_schema not in " + SYS_DBS + " order by 1,2,3,4"),
    "grants": ("select user, host from mysql.user where host not in ('localhost','127.0.0.1')"),
    "engines": ("select table_schema, table_name, engine from information_schema.tables "
                "where table_schema not in " + SYS_DBS + " and engine<>'InnoDB'"),
}


def query(conn, client, sql, timeout=900):
    rc, out, err = run(conn, '{} -e "{}" 2>/dev/null'.format(client, sql), timeout)
    return [x for x in out.splitlines() if x.strip()]


def diff_set(a, b):
    sa, sb = set(a), set(b)
    return sorted(sa - sb), sorted(sb - sa)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default="", help="逗号分隔 db.table，做 COUNT(*) 比对")
    ap.add_argument("--count-tables", action="store_true", help="只做行数比对")
    args = ap.parse_args()

    cfg = load_config()
    s_name = [n for n in cfg["src"] if cfg["hosts"][n]["role"] == "db"][0]
    d_name = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]
    s, d = cfg["hosts"][s_name], cfg["hosts"][d_name]
    cs = get_conn(s["ip"], s["user"], s["pwd"])
    cd = get_conn(d["ip"], d["user"], d["pwd"])
    sc, dc = cfg["mysql"]["src_client"], cfg["mysql"]["dst_client"]

    if not args.count_tables:
        for key in ["dbs", "tables", "views", "routines", "triggers", "indexes", "grants", "engines"]:
            a = query(cs, sc, SQL[key])
            b = query(cd, dc, SQL[key])
            only_src, only_dst = diff_set(a, b)
            flag = "OK" if not only_src and not only_dst else "DIFF"
            log("[{:<9}] 源 {} / 目 {}  {}  (仅在源 {} / 仅在目标 {})".format(
                key, len(a), len(b), flag, len(only_src), len(only_dst)))
            for x in only_src[:8]:
                log("    MISS  " + x)
            for x in only_dst[:8]:
                log("    EXTRA " + x)

    if args.tables:
        for item in [x.strip() for x in args.tables.split(",") if x.strip()]:
            db, tb = item.split(".", 1)
            a = query(cs, sc, "select count(*) from {}.{}".format(db, tb), 7200)
            b = query(cd, dc, "select count(*) from {}.{}".format(db, tb), 7200)
            same = a == b
            log("行数 {:<50} 源={} 目={} {}".format(item, a[0] if a else "?",
                                                   b[0] if b else "?", "✔" if same else "✘"))

    # 统计信息体检（只在目标端做）
    zero = query(cd, dc,
                 "select count(*) from mysql.innodb_table_stats s "
                 "join information_schema.tables t on t.table_schema=s.database_name "
                 "and t.table_name=s.table_name where s.n_rows=0 and t.data_length>20000 "
                 "and s.database_name not in ('mysql','sys')")
    log("目标端 非空表但统计为 0 的数量: {}（必须为 0）".format(zero[0] if zero else "?"))

    close_all()


if __name__ == "__main__":
    main()
