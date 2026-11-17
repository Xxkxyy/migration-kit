# 老旧 CentOS 7 单体服务 → 新服务器 整体迁移工具包

> 实战沉淀：5 台旧机（1 库 + 4 应用/存储）整体搬迁到 5 台新机（Anolis 8.9），
> 全程**源端只读**，数据 1.97 TB、MySQL 726 GB + 313 GB 双大库、22+ 个容器、11 个微服务。
> 本工具包是那次迁移的可复现版本：流程、脚本、坑位、验收标准全部可照抄。

## 适用场景

- 一批 CentOS 7 上的 Docker 化单体应用 + 独立 MySQL，要整体搬到新机型/新系统
- 业务不能停机太久，允许"先全量搬、最后增量切"
- 源机必须保留（不允许删除任何数据）

## 目录结构

```
migration-kit/
├── README.md                      ← 你在这里
├── docs/
│   ├── 01-流程总览.md             ← 9 个阶段、依赖关系、时间估算、决策点
│   ├── 02-阶段详解.md             ← 每阶段的目标/操作/命令/验收/失败处理
│   ├── 03-排障与坑位.md           ← 17 个真实踩过的坑（现象→根因→处置）
│   ├── 04-验收清单.md             ← 可勾选的验收表
│   ├── 05-探测命令清单.md         ← 全部探测命令逐条说明「看哪个数字」
│   ├── 06-完整命令手册.md         ← ★ 全部命令（20 组、可直接复制执行）
│   └── 07-事故复盘.md             ← ★ 8 起事故：时间线/错误代码/修复代码/预防
├── config/
│   ├── hosts.example.py           ← 主机清单模板（复制为 hosts.py）
│   └── my.cnf.import              ← 导入期 MySQL 参数
└── scripts/
    ├── common.py                  ← SSH 通道：硬超时 + 断链自愈 + 统一日志
    ├── 00_conn_check.py           ← 连通/凭据体检（含全角口令自动纠正）
    ├── 01_survey.py               ← 盘点（只读，输出 md）
    ├── 02_docker_image.py         ← 镜像 save/scp/load + IMAGE ID 比对
    ├── 03_rsync_sync.py           ← rsync + 文件数/字节数双向校验
    ├── 04_mysql_bulkload.py       ← ★ 大表分段并行导入引擎（Python 版）
    ├── 05_rebuild_containers.py   ← ★ inspect → docker run 重建
    ├── 06_repoint_configs.py      ← 批量改指（.bak_ip 备份）
    ├── 07_firewall.py             ← 按容器真实端口开 firewalld
    ├── 08_acceptance.py           ← 四层验收 + 统计信息检查
    ├── 09_probe.py                ← ★ 探测执行器（铺 probe.sh 到所有机器）
    ├── 10_mysql_diff.py           ← ★ 库/对象/索引/账号 全量差异比对
    ├── 11_slow_doctor.py          ← ★ 慢查询诊断 + EXPLAIN + 索引建议
    ├── 12_perf_baseline.py        ← ★ 磁盘 IOPS / MySQL 写入基线
    ├── 13_cutover.py              ← 切流 / 回退 / 冒烟
    └── lib/
        ├── probe.sh               ← ★ 探测命令库（8 段 100+ 条，纯只读）
        ├── bulk_table.sh          ← ★ 大表导入的纯 shell 版（同 04，无 Python 依赖）
        ├── jobs.sh                ← ★ 通用作业框框架：setsid + rc 对账 + flock
        ├── mysql_init.sh          ← ★ 空机装 MySQL 5.7（二进制包 + systemd）
        ├── lvm_merge.sh           ← ★ 多盘合成一个逻辑卷（本次 1T+1T→2T）
        ├── db_full_migrate.sh     ← ★ 库/表/例程/触发器/视图/账号 骨架迁移
        ├── oracle_driver.sh       ← ★ 给 Spring Boot fat jar 安全注入 ojdbc（STORED）
        └── stat_repair.sh         ← ★ 全库 ANALYZE + 统计体检（事故 1 的修复脚本）
```

## 快速开始

```bash
# 0) 体检（先确认每台都能连、口令没写错）
cp config/hosts.example.py config/hosts.py && vim config/hosts.py
python scripts/00_conn_check.py

# 1) 全量探测（输出 inventory/probe/*.txt，留证 + diff）
python scripts/09_probe.py                       # 全部主机全部段
python scripts/09_probe.py --sections sys,disk,mysql

# 2) 存储基线（决定要不要用导入期参数）
python scripts/12_perf_baseline.py --apply --mysql-test

# 3) 搬镜像与文件
python scripts/02_docker_image.py --apply
python scripts/03_rsync_sync.py --apply

# 4) 灌库（先小库后大表；导入期 my.cnf 见 config/）
python scripts/04_mysql_bulkload.py --db ek_patient --table T_NurseDataDetail --workers 8 --apply
bash scripts/lib/bulk_table.sh                   # 纯 shell 等价版本（在源库机器上跑）

# 5) 起容器 + 改配置 + 开端口
python scripts/05_rebuild_containers.py --apply
python scripts/07_firewall.py --apply
python scripts/06_repoint_configs.py --old 10.0.0. --new 10.1.0. --apply

# 6) 验收（结构差异 + 慢查询 + 四层验收）
python scripts/10_mysql_diff.py --tables ek_patient.T_PatVisit
python scripts/11_slow_doctor.py --top 10 --explain
python scripts/08_acceptance.py

# 7) 切流
python scripts/13_cutover.py --precheck
python scripts/13_cutover.py --stop-old --confirm YES-STOP-OLD
python scripts/13_cutover.py --delta --smoke
```

## 三句话讲清核心做法

1. **盘点先行**：不要凭感觉迁移，先跑 `01_survey.py` 把"谁在跑什么、占多少、开哪些端口"变成一张表。
2. **数据搬迁用"分段并行 + 索引后建 + 统计信息重算"**：这是把 116 行/秒救到 3500 行/秒、并且不误判执行计划的关键。
3. **凡是长任务，一律"远端独立执行 + rc 文件对账 + flock 互斥"**：本地 SSH 断了、脚本被杀，远端照跑，重跑不重复。

## 最重要的三条经验

| # | 经验 | 代价 |
|---|---|---|
| 1 | 大表导入前先 `DROP INDEX`，导入后 `ADD INDEX`，再 `ANALYZE TABLE` | 不这么做：导入慢 15 倍，且优化器以为表是空的，业务查询 90 秒起步 |
| 2 | 切生产参数时必须把 `innodb_stats_auto_recalc` 从 OFF 改回 ON | 本次事故：`ek_patient` 215 张表统计全为 0，病人列表 93 秒、网关 504、一条详情查询 30 分钟不返回 |
| 3 | `ps | grep '[_x]'` 里的方括号是**字符类**，会命中全机进程 | 曾把 systemd(PID 1)、sshd、mysqld 全部 kill，机器需重启 |

## 复现性声明

- 所有脚本只依赖 `paramiko`（`pip install paramiko`），无其他第三方库
- 所有危险动作（改配置、停服务、建索引）都带 `--apply` 开关，默认 **dry-run**
- 所有脚本以"幂等"为前提设计：重复执行不会造成数据叠加或重复导入
- 时间基准：本次迁移实际耗时见 `docs/01-流程总览.md` 的甘特式表格
