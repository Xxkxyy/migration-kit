#!/bin/bash
# =============================================================================
# bulk_table.sh —— 单张大表"分段并行 + 索引后建"导入（纯 shell 版，不依赖 Python）
#
# 与 04_mysql_bulkload.py 等价，适合在源库机器上直接跑、或塞进 crontab/运维平台。
#
# 用法（在【源库】机器上执行）:
#   DB=ek_patient TB=T_NurseDataDetail \
#   DST_HOST=10.1.0.196 DST_PASS='***' SRC_PASS='***' \
#   WORKERS=8 WORK=/data/_mig \
#   bash bulk_table.sh
#
# 前置:
#   * 目标端已建好表结构、且该表所有二级索引已存在（脚本会先 DROP 再重建）
#   * 源端能直连目标端 3306（或改 mysql 命令里的 -h）
#   * 目标端账号允许源端 IP 连接
# =============================================================================
set -o pipefail

DB=${DB:?需要 DB}
TB=${TB:?需要 TB}
DST_HOST=${DST_HOST:?需要 DST_HOST}
DST_PASS=${DST_PASS:?需要 DST_PASS}
SRC_PASS=${SRC_PASS:-}
WORKERS=${WORKERS:-8}
WORK=${WORK:-/data/_mig}
MYSQL_BIN=${MYSQL_BIN:-$(command -v mysql)}
MYSQLDUMP_BIN=${MYSQLDUMP_BIN:-$(command -v mysqldump)}
LOCK=$WORK/bulk_${DB}_${TB}.lock
LOG=$WORK/bulk_${DB}_${TB}.log

mkdir -p "$WORK"

# ---- 互斥：绝不允许两个实例同时处理同一张表（否则会互相 TRUNCATE）----
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "另一实例正在处理 $DB.$TB，退出" | tee -a "$LOG"
  exit 0
fi

say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# ---- 1. 从【源端】取边界（关键：不能从目标端取，TRUNCATE 后是 NULL）----
BOUND=$($MYSQL_BIN -uroot ${SRC_PASS:+-p$SRC_PASS} -N -e \
        "select count(*), ifnull(min(id),0), ifnull(max(id),0) from $DB.$TB" 2>/dev/null)
TOTAL=$(echo "$BOUND" | cut -f1)
LO=$(echo "$BOUND"     | cut -f2)
HI=$(echo "$BOUND"     | cut -f3)
say "源端 $DB.$TB : rows=$TOTAL id=[$LO,$HI]"
[ -z "$TOTAL" ] && { say "取边界失败（表可能没有 id 主键），退出"; exit 1; }

# ---- 2. 目标端：清空 + 记录并删除二级索引 ----
$MYSQL_BIN -h "$DST_HOST" -uroot -p"$DST_PASS" -e "truncate table $DB.$TB" || { say "TRUNCATE 失败"; exit 1; }

IDX_FILE=$WORK/idx_${DB}_${TB}.txt
: > "$IDX_FILE"
$MYSQL_BIN -N -e "select index_name, group_concat(column_name order by seq_in_index)
                  from information_schema.statistics
                  where table_schema='$DB' and table_name='$TB' and index_name<>'PRIMARY'
                  group by index_name" 2>/dev/null > "$IDX_FILE"
say "待重建索引 $(wc -l < "$IDX_FILE") 个"
while IFS=$'\t' read -r name cols; do
  [ -z "$name" ] && continue
  $MYSQL_BIN -h "$DST_HOST" -uroot -p"$DST_PASS" -e "alter table $DB.$TB drop index $name" || say "DROP $name 失败"
done < "$IDX_FILE"

# ---- 3. 分段并行（远端独立执行 + rc 对账）----
SPAN=$(( (HI - LO) / WORKERS + 1 ))
say "切 $WORKERS 段，每段约 $SPAN 个 id"
for i in $(seq 0 $((WORKERS - 1))); do
  A=$(( LO + SPAN * i ))
  B=$(( A + SPAN - 1 ))
  [ $B -gt $HI ] && B=$HI
  [ $A -gt $HI ] && break
  RC=$WORK/rc_${DB}_${TB}_$i
  ERR=$WORK/err_${DB}_${TB}_$i.log
  rm -f "$RC"
  cat > "$WORK/seg_${DB}_${TB}_$i.sh" <<EOF
#!/bin/bash
set -o pipefail
$MYSQLDUMP_BIN -h127.0.0.1 -uroot ${SRC_PASS:+-p$SRC_PASS} \
  --no-create-info --skip-add-locks --skip-disable-keys --skip-triggers \
  --quick --single-transaction --compress \
  --where="id >= $A AND id <= $B" $DB $TB 2>>$ERR \\
  | $MYSQL_BIN -h $DST_HOST -uroot -p'$DST_PASS' $DB 2>>$ERR
echo \${PIPESTATUS[0]},\${PIPESTATUS[1]} > $RC
EOF
  chmod +x "$WORK/seg_${DB}_${TB}_$i.sh"
  setsid nohup "$WORK/seg_${DB}_${TB}_$i.sh" >/dev/null 2>&1 < /dev/null &
  say "  段 $i: [$A, $B] 已启动"
done

# ---- 4. 轮询 rc（本地断了也不影响远端）----
TAGS=$(seq 0 $((WORKERS - 1)))
while true; do
  left=0
  for i in $TAGS; do [ -f "$WORK/rc_${DB}_${TB}_$i" ] || left=$((left + 1)); done
  [ $left -eq 0 ] && break
  say "  等待中，未完成 $left 段"
  sleep 30
done

BAD=0
for i in $TAGS; do
  rc=$(cat "$WORK/rc_${DB}_${TB}_$i" 2>/dev/null)
  [ "$rc" = "0,0" ] || { BAD=$((BAD + 1)); say "  段 $i 失败 rc=$rc"; }
done
say "分段结果: 成功 $((WORKERS - BAD)) / 失败 $BAD"
[ $BAD -gt 0 ] && { say "存在失败分片，未重建索引，请处理后再跑"; exit 1; }

# ---- 5. 重建索引 + ANALYZE ----
while IFS=$'\t' read -r name cols; do
  [ -z "$name" ] && continue
  say "重建索引 $name ($cols)"
  $MYSQL_BIN -h "$DST_HOST" -uroot -p"$DST_PASS" -e "alter table $DB.$TB add index $name ($cols)" || say "ADD $name 失败"
done < "$IDX_FILE"

$MYSQL_BIN -h "$DST_HOST" -uroot -p"$DST_PASS" -e "analyze table $DB.$TB" >/dev/null 2>&1

# ---- 6. 行数核对 ----
SRC_CNT=$($MYSQL_BIN -uroot ${SRC_PASS:+-p$SRC_PASS} -N -e "select count(*) from $DB.$TB" 2>/dev/null)
DST_CNT=$($MYSQL_BIN -h "$DST_HOST" -uroot -p"$DST_PASS" -N -e "select count(*) from $DB.$TB" 2>/dev/null)
say "行数核对: 源=$SRC_CNT 目标=$DST_CNT"
[ "$SRC_CNT" = "$DST_CNT" ] && say "BULK_DONE OK" || { say "BULK_DONE MISMATCH"; exit 1; }
