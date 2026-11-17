# -*- coding: utf-8 -*-
"""阶段 5 前置 · 存储与写入性能基线

为什么必须测:
    同一套参数在 SSD 上 3 分钟导入完，在低 IOPS 虚拟盘上要 3 小时；
    "每次提交 fsync 要多久"直接决定要不要关 doublewrite / 改 flush_method。

测什么:
    A. 4K 同步写 IOPS（dd oflag=direct,dsync）—— 最关键，对应 InnoDB 每次提交
    B. 1M 顺序写带宽（dd oflag=direct）
    C. 1M 顺序读带宽
    D. 4K 随机读 IOPS（fio 存在时）
    E. MySQL 单条 INSERT 提交延迟（--apply 时执行，建临时表并删除）

判定阈值（实测经验）:
    A < 100 IOPS   → 必须用导入期参数（flush=0 / doublewrite=0 / O_DIRECT_NO_FSYNC）
    A < 30 IOPS    → 强烈建议先做磁盘/存储层优化，否则导入会按天算
    B < 100 MB/s   → 大文件 rsync 会很慢，考虑分批或错峰

用法:
    python scripts/12_perf_baseline.py                 # 只读测试
    python scripts/12_perf_baseline.py --apply         # 包含写测试（写 /tmp，200MB 上限，自动删除）
    python scripts/12_perf_baseline.py --host new-db --mysql-test
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402

READ_TEST = ("f=$(ls -S /data/app/*/apps/*/*.jar 2>/dev/null | head -1); "
             "[ -z \"$f\" ] && f=$(ls -S /usr/lib64/*.so 2>/dev/null | head -1); "
             "dd if=\"$f\" of=/dev/null bs=1M count=512 iflag=direct 2>&1 | tail -1")

WRITE_TESTS = [
    ("4K同步写(IOPS)",  "dd if=/dev/zero of=/tmp/_pb_sync bs=4k count=500 oflag=direct,dsync 2>&1 | tail -1"),
    ("1M顺序写",        "dd if=/dev/zero of=/tmp/_pb_seq bs=1M count=1024 oflag=direct 2>&1 | tail -1"),
]
CLEAN = "rm -f /tmp/_pb_sync /tmp/_pb_seq"

FIO = ("command -v fio >/dev/null && fio --name=rw --ioengine=libaio --direct=1 --bs=4k "
       "--size=128m --rw=randread --runtime=10 --time_based --filename=/tmp/_pb_fio "
       "--output-format=terse 2>/dev/null | cut -d';' -f8-9 || echo no-fio")

MYSQL_BENCH = """
drop table if exists _perf_probe;
create table _perf_probe(id bigint auto_increment primary key, v varchar(200)) engine=innodb;
set @t=now(6);
insert into _perf_probe(v) values {values};
select round(timestampdiff(microsecond,@t,now(6))/1000,1) as ms_for_{n}_rows,
       round({n}/(timestampdiff(microsecond,@t,now(6))/1000000),0) as rows_per_sec;
drop table _perf_probe;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="", help="只测某台（配置里的 name）")
    ap.add_argument("--apply", action="store_true", help="执行写测试")
    ap.add_argument("--mysql-test", action="store_true", help="额外测 MySQL 提交延迟（需 mysql 客户端可用）")
    args = ap.parse_args()

    cfg = load_config()
    targets = [args.host] if args.host else [n for n in cfg["dst"]]

    for name in targets:
        h = cfg["hosts"][name]
        log("=== {} ({}) ===".format(name, h["ip"]))
        try:
            conn = get_conn(h["ip"], h["user"], h["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        rc, out, err = run(conn, READ_TEST, 600)
        log("  顺序读: {}".format([x for x in (out + err).splitlines() if x.strip()][-1:]))

        if args.apply:
            for label, cmd in WRITE_TESTS:
                rc, out, err = run(conn, cmd, 900)
                log("  {}: {}".format(label, [x for x in (out + err).splitlines() if x.strip()][-1:]))
            rc, out, err = run(conn, FIO, 300)
            log("  4K随机读(fio): {}".format((out or err).strip()[:120]))
            run(conn, CLEAN, 60)
        else:
            log("  [dry-run] 写测试未执行（加 --apply）")

        if args.mysql_test:
            vals = ",".join(["('v{}')".format(i) for i in range(1000)])
            sql = MYSQL_BENCH.format(values=vals, n=1000).replace("\n", " ")
            rc, out, err = run(conn, '{} -e "{}" 2>&1'.format(cfg["mysql"]["dst_client"], sql), 1800)
            log("  MySQL 1000 行提交: {}".format((out or err).strip().splitlines()[-2:]))

    log("")
    log("阈值: 4K同步写 < 100 IOPS 必须用导入期参数；< 30 IOPS 需先优化存储层")
    close_all()


if __name__ == "__main__":
    main()
