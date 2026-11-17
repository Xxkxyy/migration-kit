#!/bin/bash
# =============================================================================
# stat_repair.sh —— 全库 ANALYZE + 统计体检（本次最贵的事故就是它没做）
#
# 事故背景:
#   导入期设了 innodb_stats_auto_recalc=OFF，切生产参数时漏改回 ON。
#   结果 ek_patient 的 215 张表 n_rows 全为 0（实际 726GB），优化器以为表空，
#   护士站病人列表 93 秒、网关 504、护理记录详情 30 分钟不返回。
#
# 这个脚本踩过两个坑，都已在代码里修掉:
#   坑1: M=/usr/local/mysql/bin/mysql -uroot -N   ← 少了引号，变量没被赋值，脚本空转
#   坑2: "analyze table db.\`$t\`"                 ← 反斜杠被当成字面量，SQL 语法错误
#
# 用法:
#   bash stat_repair.sh                     # 全库全表
#   DB=ek_patient bash stat_repair.sh       # 只修一个库
#   VERIFY_ONLY=1 bash stat_repair.sh       # 只体检不执行
# =============================================================================
set -uo pipefail

MYSQL=${MYSQL:-/usr/local/mysql/bin/mysql}
C="$MYSQL -uroot -N"
LOG=${LOG:-/data/_mig/stat_repair.log}
DB=${DB:-}
LOCK=${LOCK:-/data/_mig/stat_repair.lock}

mkdir -p "$(dirname "$LOG")"

# ---- 单实例保护 ----
exec 9>"$LOCK"
if ! flock -n 9; then echo "另一实例正在运行，退出"; exit 0; fi

say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

safe() { echo "$1" | sed 's/\\/\\\\/g'; }

# ---- 0. 参数检查 ----
say "当前: auto_recalc=$($C -e "select @@innodb_stats_auto_recalc" 2>/dev/null) \
flush=$($C -e "select @@innodb_flush_log_at_trx_commit" 2>/dev/null) \
dw=$($C -e "select @@innodb_doublewrite" 2>/dev/null)"

# ---- 1. 体检 ----
health() {
  $C -e "select count(*) non_empty_zero_stats from mysql.innodb_table_stats s
         join information_schema.tables t
           on t.table_schema=s.database_name and t.table_name=s.table_name
         where s.n_rows=0 and t.data_length>20000
           and s.database_name not in ('mysql','sys')" 2>/dev/null
}

BEFORE=$(health)
say "修复前: 非空表但统计为 0 的数量 = $BEFORE"

if [ "${VERIFY_ONLY:-0}" = "1" ]; then
  exit 0
fi

# ---- 2. 打开自动重算（内存 + 配置文件双写，否则重启又丢）----
if [ -f /etc/my.cnf ]; then
  if grep -q '^innodb_stats_auto_recalc=OFF' /etc/my.cnf; then
    sed -i 's/^innodb_stats_auto_recalc=OFF/innodb_stats_auto_recalc=ON/' /etc/my.cnf
    say "已改 /etc/my.cnf: innodb_stats_auto_recalc=ON"
  elif ! grep -q '^innodb_stats_auto_recalc' /etc/my.cnf; then
    printf '\ninnodb_stats_auto_recalc=ON\n' >> /etc/my.cnf
    say "已追加 /etc/my.cnf: innodb_stats_auto_recalc=ON"
  fi
fi
$C -e "set global innodb_stats_auto_recalc=ON" 2>/dev/null
say "运行时: auto_recalc=$($C -e "select @@global.innodb_stats_auto_recalc" 2>/dev/null)"

# ---- 3. 逐表 ANALYZE（只处理统计为 0 的，最多 3 轮）----
if [ -n "$DB" ]; then
  WHERE="and s.database_name='$DB'"
else
  WHERE=""
fi

for pass in 1 2 3; do
  LEFT=$($C -e "select concat(s.database_name,'.',s.table_name)
                from mysql.innodb_table_stats s
                join information_schema.tables t
                  on t.table_schema=s.database_name and t.table_name=s.table_name
                where s.n_rows=0 and t.data_length>20000
                  and s.database_name not in ('mysql','sys') $WHERE" 2>/dev/null)
  CNT=$(printf '%s\n' "$LEFT" | grep -c . || true)
  say "第 $pass 轮: 待修 $CNT 张"
  [ "$CNT" -eq 0 ] && break

  for ft in $LEFT; do
    # timeout 防止单表卡死（曾遇到 Waiting for table flush 被长查询阻塞）
    if timeout 300 $MYSQL -uroot -e "analyze table $ft" >/dev/null 2>>"$LOG"; then
      :
    else
      say "  SKIP $ft（超时或被阻塞）"
    fi
  done
done

# ---- 4. 复核 ----
AFTER=$(health)
say "修复后: 非空表但统计为 0 的数量 = $AFTER"

say "各库统计概览:"
$C -e "select database_name, count(*) total, sum(n_rows=0) zero_rows
       from mysql.innodb_table_stats
       where database_name not in ('mysql','sys')
       group by database_name order by zero_rows desc" 2>/dev/null | tee -a "$LOG"

if [ "$AFTER" = "0" ]; then
  say "STAT_REPAIR OK —— 统计已恢复，请再跑一次业务慢查询确认已转快"
  exit 0
else
  say "STAT_REPAIR INCOMPLETE —— 仍有 $AFTER 张表统计为 0，检查是否被长查询阻塞（见下方 processlist）"
  $C -e "select id, db, time, state, left(info,60) from information_schema.processlist
         where command='Query' and time>60 and id<>connection_id()" 2>/dev/null | tee -a "$LOG"
  exit 1
fi
