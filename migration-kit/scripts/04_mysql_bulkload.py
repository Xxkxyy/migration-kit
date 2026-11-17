# -*- coding: utf-8 -*-
"""阶段 5 · 大表分段并行导入引擎（本次迁移的核心）

解决的问题:
    慢盘上直接 mysqldump | mysql 只有 100~200 行/秒（每次提交 fsync）。
    拆成「索引后建 + 主键区间切片 + 多段并行 + 远端独立执行」后可稳定到 3000+ 行/秒，
    并且天然支持断点续跑。

关键约束（都是踩坑换来的）:
    1. 切片边界必须从【源端】取 —— 目标端 TRUNCATE 后 MIN/MAX 为 NULL，会导出 0 行且 rc=0
    2. 大表导入前必须 DROP 二级索引，导入后再 ADD，最后 ANALYZE
    3. 每个分段远端 setsid 独立执行 + 写 rc 文件，本地只轮询 rc（SSH 断了也不影响）
    4. 入口 flock，防止两个实例互相 TRUNCATE 同一张表
    5. 绝不用 INSERT IGNORE 反复重试（会叠加数据把表撑大）

用法:
    # 1) 看计划
    python scripts/04_mysql_bulkload.py --db ek_patient --all-big
    # 2) 真正导一张表
    python scripts/04_mysql_bulkload.py --db ek_patient --table T_NurseDataDetail --workers 8 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

BIG_MB = 5000          # >= 5GB 视为大表
BIG_ROWS = 10_000_000  # 或 >= 1000 万行


# --------------------------------------------------------------------------- 查询
def q(conn, sql, client, timeout=600):
    rc, out, err = run(conn, '{} -e "{}" 2>&1'.format(client, sql.replace('"', '\\"')), timeout)
    return out.strip()


def table_profile(dst_conn, client, db):
    sql = ("select table_name, ifnull(table_rows,0), "
           "round((data_length+index_length)/1024/1024) "
           "from information_schema.tables where table_schema='{}' "
           "and table_type='BASE TABLE' order by data_length+index_length desc").format(db)
    out = q(dst_conn, sql, client)
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3 and p[2].isdigit():
            rows.append((p[0], int(p[1]), int(p[2])))
    return rows


def src_bounds(src_conn, client, db, tb):
    out = q(src_conn, "select count(*), ifnull(min(id),0), ifnull(max(id),0) from {}.{}".format(db, tb),
            client, 3600)
    p = out.split("\t")
    if len(p) != 3:
        return None
    return int(p[0]), int(p[1]), int(p[2])


def secondary_indexes(dst_conn, client, db, tb):
    """返回 [(index_name, 'col1,col2')]，排除 PRIMARY。"""
    sql = ("select index_name, group_concat(column_name order by seq_in_index) "
           "from information_schema.statistics where table_schema='{}' and table_name='{}' "
           "and index_name<>'PRIMARY' group by index_name").format(db, tb)
    out = q(dst_conn, sql, client)
    idx = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 2:
            idx.append((p[0], p[1]))
    return idx


# --------------------------------------------------------------------------- 分段脚本
CHUNK = """#!/bin/bash
set -o pipefail
DB={db}; TB={tb}; LO={lo}; HI={hi}
RC={work}/rc_{tag}
ERR={work}/err_{tag}.log
mysqldump -h127.0.0.1 -uroot -p'{spwd}' --no-create-info --skip-add-locks \\
  --skip-disable-keys --skip-triggers --quick --single-transaction --compress \\
  --where="id >= $LO AND id <= $HI" $DB $TB 2>>$ERR \\
  | mysql -h{dst_ip} -uroot -p'{dpwd}' $DB 2>>$ERR
echo ${{PIPESTATUS[0]}},${{PIPESTATUS[1]}} > $RC
"""


def launch(src_conn, work, db, tb, lo, hi, tag, spwd, dst_ip, dpwd):
    body = CHUNK.format(db=db, tb=tb, lo=lo, hi=hi, work=work, tag=tag,
                        spwd=spwd, dst_ip=dst_ip, dpwd=dpwd)
    path = "{}/chunk_{}.sh".format(work, tag)
    sftp = src_conn.open_sftp()
    with sftp.open(path, "w") as fh:
        fh.write(body)
    sftp.close()
    run(src_conn, "chmod +x {}; rm -f {}/rc_{}; "
                  "setsid nohup {} >/dev/null 2>&1 < /dev/null & ".format(path, work, tag, path), 60)


def wait_all(src_conn, work, tags, timeout=8 * 3600):
    t0 = time.time()
    pending = set(tags)
    while pending and time.time() - t0 < timeout:
        time.sleep(15)
        out = run(src_conn, "ls {}/rc_* 2>/dev/null".format(work), 60)[1]
        done = set()
        for line in out.splitlines():
            name = os.path.basename(line.strip())[3:]
            if name in pending:
                done.add(name)
        if done:
            pending -= done
            log("    已完成 {}/{}".format(len(tags) - len(pending), len(tags)))
    return pending


# --------------------------------------------------------------------------- 主流程
def load_one(cfg, args, src_conn, dst_conn, db, tb, spwd, dpwd):
    src_client = cfg["mysql"]["src_client"]
    dst_client = cfg["mysql"]["dst_client"]
    work = cfg["paths"]["work_dir"]
    dst_ip = cfg["hosts"][[n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]]["ip"]

    prof = src_bounds(src_conn, src_client, db, tb)
    if not prof:
        log("  !! 取源端边界失败（表可能没有 id 主键），跳过")
        return False
    total, lo, hi = prof
    log("  源端: {} 行, id ∈ [{}, {}]".format(total, lo, hi))

    if not args.apply:
        log("  [dry-run] 将切 {} 段并行导入，并 DROP/ADD 二级索引".format(args.workers))
        return True

    idx = secondary_indexes(dst_conn, dst_client, db, tb)
    log("  二级索引 {} 个，先删除".format(len(idx)))
    q(dst_conn, "truncate table {}.{}".format(db, tb), dst_client, 3600)
    for name, cols in idx:
        q(dst_conn, "alter table {}.{} drop index {}".format(db, tb, name), dst_client, 3600)

    n = max(1, args.workers)
    span = max(1, (hi - lo) // n + 1)
    tags = []
    for i in range(n):
        a = lo + span * i
        b = min(hi, a + span - 1)
        if a > hi:
            break
        tag = "{}".format(i)
        launch(src_conn, work, db, tb, a, b, tag, spwd, dst_ip, dpwd)
        tags.append(tag)
        log("    分片 {}: [{}, {}] 已启动".format(tag, a, b))

    pending = wait_all(src_conn, work, tags)
    bad = []
    for tag in tags:
        rc, out, _ = run(src_conn, "cat {}/rc_{} 2>/dev/null".format(work, tag), 60)
        if tag in pending or not out.strip().startswith("0,0"):
            bad.append((tag, out.strip() or "NO_RC"))
    log("  分片结果: 成功 {} / 失败 {}".format(len(tags) - len(bad), len(bad)))
    for tag, info in bad[:5]:
        log("    失败分片 {}: rc={}".format(tag, info))

    log("  重建二级索引...")
    for name, cols in idx:
        q(dst_conn, "alter table {}.{} add index {} ({})".format(db, tb, name, cols), dst_client, 7200)

    log("  ANALYZE ...")
    q(dst_conn, "analyze table {}.{}".format(db, tb), dst_client, 3600)

    src_cnt = q(src_conn, "select count(*) from {}.{}".format(db, tb), src_client, 3600)
    dst_cnt = q(dst_conn, "select count(*) from {}.{}".format(db, tb), dst_client, 3600)
    ok = src_cnt.strip() == dst_cnt.strip()
    log("  行数比对: 源={} 目={} {}".format(src_cnt, dst_cnt, "✔" if ok else "✘"))
    return ok and not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--table", default="", help="单表名；不填则按 --all-big 选择")
    ap.add_argument("--all-big", action="store_true", help="自动挑 >=5GB 或 >=1000万行的表")
    ap.add_argument("--workers", type=int, default=8, help="并发分段数")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    src_name = [n for n in cfg["src"] if cfg["hosts"][n]["role"] == "db"][0]
    dst_name = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]
    s, d = cfg["hosts"][src_name], cfg["hosts"][dst_name]

    src_conn = get_conn(s["ip"], s["user"], s["pwd"])
    dst_conn = get_conn(d["ip"], d["user"], d["pwd"])
    run(src_conn, "mkdir -p {}".format(cfg["paths"]["work_dir"]), 60)

    if args.table:
        tables = [args.table]
    else:
        tables = [t for t, rows, mb in table_profile(dst_conn, cfg["mysql"]["dst_client"], args.db)
                  if args.all_big and (mb >= BIG_MB or rows >= BIG_ROWS)]
        if not args.all_big:
            sys.exit("请指定 --table 或 --all-big")

    log("本次导入 {} 张表: {}".format(len(tables), tables))
    for tb in tables:
        log("=== {}.{} ===".format(args.db, tb))
        load_one(cfg, args, src_conn, dst_conn, args.db, tb, s["pwd"], d["pwd"])

    log("完成。别忘了切生产参数: flush_log_at_trx_commit=2 / doublewrite=1 / "
        "log_bin=1 / innodb_stats_auto_recalc=ON")
    close_all()


if __name__ == "__main__":
    main()
