#!/bin/bash
# =============================================================================
# db_full_migrate.sh —— 数据库"骨架"全量迁移：库 → 表结构 → 例程 → 触发器 → 视图 → 账号
#
# 顺序很重要，错了会失败:
#   1. 建库（按源端字符集）
#   2. 建表结构（--no-data）
#   3. 建例程/触发器（需要 log_bin_trust_function_creators=1）
#   4. 建视图（依赖表与 DEFINER 账号）
#   5. 建账号并授权（换网段时同时改 host）
#   6. 数据由 bulk_table.sh / 04_mysql_bulkload.py 负责
#
# 用法（在能同时访问源库和目标库的机器上跑，或改 -h 参数）:
#   SRC="mysql -h10.0.0.221 -uroot -pXXX" DST="mysql -h10.1.0.196 -uroot -pYYY" \
#   SRC_DUMP="mysqldump -h10.0.0.221 -uroot -pXXX" \
#   bash db_full_migrate.sh
# =============================================================================
set -euo pipefail

SRC=${SRC:?需要源库 mysql 客户端命令}
DST=${DST:?需要目标库 mysql 客户端命令}
SRC_DUMP=${SRC_DUMP:?需要源库 mysqldump 命令}
WORK=${WORK:-/data/_mig/db}
OLD_HOST_PREFIX=${OLD_HOST_PREFIX:-}   # 如 10.0.0.  （用于改写授权 host）
NEW_HOST_PREFIX=${NEW_HOST_PREFIX:-}   # 如 10.1.0.
SYS="'mysql','sys','information_schema','performance_schema'"

mkdir -p "$WORK"
say() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---- 0. 目标端优化器准备 ----
$DST -e "set global log_bin_trust_function_creators=1" 2>/dev/null || true

# ---- 1. 建库（字符集/排序规则跟源端一致）----
say "采集并创建库"
$SRC -N -e "select schema_name, default_character_set_name, default_collation_name
            from information_schema.schemata where schema_name not in ($SYS)" > "$WORK/dbs.tsv"

while IFS=$'\t' read -r db cs coll; do
  [ -z "$db" ] && continue
  # SQL 写文件再执行：反引号在 shell 里会被命令替换（这是本次踩过的坑）
  echo "create database if not exists \`$db\` default character set $cs collate $coll;" > "$WORK/01_create.sql"
  $DST < "$WORK/01_create.sql"
  say "  库 $db ($cs/$coll)"
done < "$WORK/dbs.tsv"

# ---- 2. 表结构 ----
say "导出并导入表结构"
$SRC -N -e "select schema_name from information_schema.schemata
            where schema_name not in ($SYS)" | while read -r db; do
  [ -z "$db" ] && continue
  $SRC_DUMP --no-data --routines=false --triggers=false --single-transaction \
            --set-gtid-purged=OFF --skip-add-drop-table "$db" > "$WORK/schema_$db.sql"
  $DST < "$WORK/schema_$db.sql"
  say "  结构 $db"
done

# ---- 3. 例程 + 触发器 ----
say "导出并导入例程与触发器"
$SRC -N -e "select schema_name from information_schema.schemata
            where schema_name not in ($SYS)" | while read -r db; do
  [ -z "$db" ] && continue
  $SRC_DUMP --no-data --no-create-info --routines --triggers --single-transaction \
            --set-gtid-purged=OFF --skip-triggers=false "$db" > "$WORK/objects_$db.sql" 2>/dev/null || true
  [ -s "$WORK/objects_$db.sql" ] && $DST < "$WORK/objects_$db.sql" || true
  say "  对象 $db"
done

# ---- 4. 视图 ----
say "视图比对（视图通常已包含在 --no-data 的产物里；这里只做数量校验）"
$SRC -N -e "select count(*) from information_schema.views where table_schema not in ($SYS)" > "$WORK/v_src"
$DST -N -e "select count(*) from information_schema.views where table_schema not in ($SYS)" > "$WORK/v_dst"
say "  视图数 源=$(cat "$WORK/v_src") 目标=$(cat "$WORK/v_dst")"

# ---- 5. 账号与授权 ----
say "迁移账号"
$SRC -N -e "select concat('show grants for ''', user, '''@''', host, ''';')
            from mysql.user where host not in ('localhost','127.0.0.1')" > "$WORK/grants.sql"
> "$WORK/grants_fixed.sql"
while read -r line; do
  [ -z "$line" ] && continue
  if [ -n "$OLD_HOST_PREFIX" ] && [ -n "$NEW_HOST_PREFIX" ]; then
    line=$(echo "$line" | sed "s/${OLD_HOST_PREFIX//./\\.}/${NEW_HOST_PREFIX}/g")
  fi
  echo "$line" >> "$WORK/grants.sql2"
done < "$WORK/grants.sql"

# 逐条取回 GRANT 原文并重放（保留原口令哈希，不需要明文口令）
$SRC -N < "$WORK/grants.sql2" 2>/dev/null | sed 's/$/;/' > "$WORK/grants_replay.sql" || true
if [ -s "$WORK/grants_replay.sql" ]; then
  $DST < "$WORK/grants_replay.sql" || say "  部分 GRANT 重放失败（可能是账号已存在），请人工核对"
  $DST -e "flush privileges"
fi
say "  账号数 源=$($SRC -N -e "select count(*) from mysql.user where host not in ('localhost','127.0.0.1')") \
目标=$($DST -N -e "select count(*) from mysql.user where host not in ('localhost','127.0.0.1')")"

# ---- 6. 对象口径总校验 ----
say "对象数量比对"
$SRC -N -e "select
  (select count(*) from information_schema.tables  where table_schema not in ($SYS)) tables,
  (select count(*) from information_schema.views   where table_schema not in ($SYS)) views,
  (select count(*) from information_schema.routines where routine_schema not in ($SYS)) routines,
  (select count(*) from information_schema.triggers where trigger_schema not in ($SYS)) triggers" > "$WORK/obj_src"
$DST -N -e "select
  (select count(*) from information_schema.tables  where table_schema not in ($SYS)) tables,
  (select count(*) from information_schema.views   where table_schema not in ($SYS)) views,
  (select count(*) from information_schema.routines where routine_schema not in ($SYS)) routines,
  (select count(*) from information_schema.triggers where trigger_schema not in ($SYS)) triggers" > "$WORK/obj_dst"

echo "源  : $(cat "$WORK/obj_src")"
echo "目标: $(cat "$WORK/obj_dst")"
[ "$(cat "$WORK/obj_src")" = "$(cat "$WORK/obj_dst")" ] && say "骨架迁移一致 ✔" || say "骨架存在差异 ✘ 请检查 $WORK"
