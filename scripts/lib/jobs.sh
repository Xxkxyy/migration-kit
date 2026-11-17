#!/bin/bash
# =============================================================================
# jobs.sh —— 通用"远端独立执行 + rc 对账"作业框架（可 source 到任何脚本里）
#
# 解决三个真实问题:
#   1. SSH 断链后远端命令被 SIGHUP 杀掉 —— 用 setsid 脱离会话
#   2. 本地超时退出但远端还在跑，重跑就双跑 —— 用 rc 文件对账 + flock
#   3. 并发任务互相干扰 —— 每个作业独立锁文件
#
# 用法:
#   source lib/jobs.sh
#   job_start "dump_seg_0" "/data/_mig/scripts/seg_0.sh"
#   job_start "dump_seg_1" "/data/_mig/scripts/seg_1.sh"
#   job_wait  dump_seg_0 dump_seg_1        # 阻塞直到 rc 齐全
#   job_status dump_seg_0                  # 回显 rc 内容
#   job_ok   dump_seg_0 && echo 成功
# =============================================================================

JOBS_DIR=${JOBS_DIR:-/data/_mig/jobs}
LOCK_WAIT=${LOCK_WAIT:-0}     # flock 等待秒数，0=不等待直接失败

job_init() {
  mkdir -p "$JOBS_DIR"
}

# job_start <名字> <脚本路径> [超时秒数]
job_start() {
  local name=$1 script=$2 tmo=${3:-0}
  job_init
  local rc="$JOBS_DIR/$name.rc"
  local log="$JOBS_DIR/$name.log"
  local pidf="$JOBS_DIR/$name.pid"

  if [ -f "$rc" ]; then
    echo "[jobs] $name 已有 rc=$(cat "$rc")，跳过（如需重跑先删 $rc）"
    return 0
  fi

  if [ ! -x "$script" ]; then
    chmod +x "$script" 2>/dev/null || { echo "[jobs] $name 脚本不可执行: $script"; return 1; }
  fi

  # 关键：setsid 脱离当前会话；nohup 忽略 HUP；stdin 断开
  if [ "$tmo" -gt 0 ]; then
    setsid nohup timeout "$tmo" "$script" >"$log" 2>&1 < /dev/null &
  else
    setsid nohup "$script" >"$log" 2>&1 < /dev/null &
  fi
  echo $! > "$pidf"
  echo "[jobs] $name 已启动 pid=$(cat "$pidf") log=$log"
}

# job_wait <名字...>   全部出现 rc 才返回；返回 0=全成功 1=有失败
job_wait() {
  local names="$*"
  local total=0 n
  for n in $names; do total=$((total + 1)); done

  while true; do
    local left=0
    for n in $names; do [ -f "$JOBS_DIR/$n.rc" ] || left=$((left + 1)); done
    [ "$left" -eq 0 ] && break
    echo "[jobs] 等待中：未完成 $left/$total"
    sleep 15
  done

  local bad=0
  for n in $names; do
    local rc; rc=$(cat "$JOBS_DIR/$n.rc" 2>/dev/null)
    [ "$rc" = "0" ] || { echo "[jobs] $n 失败 rc=$rc"; bad=$((bad + 1)); }
  done
  echo "[jobs] 完成：成功 $((total - bad)) / 失败 $bad"
  [ "$bad" -eq 0 ]
}

job_status() {
  local n=$1
  [ -f "$JOBS_DIR/$n.rc" ] && cat "$JOBS_DIR/$n.rc" || echo "RUNNING_OR_MISSING"
}

job_ok() {
  [ "$(cat "$JOBS_DIR/$1.rc" 2>/dev/null)" = "0" ]
}

job_clean() {
  rm -f "$JOBS_DIR"/*.rc "$JOBS_DIR"/*.pid "$JOBS_DIR"/*.log 2>/dev/null
  echo "[jobs] 已清理作业状态"
}

# 单实例保护：脚本入口调用，防止被重复启动
job_lock() {
  local name=${1:-main}
  job_init
  exec 9>"$JOBS_DIR/$name.lock"
  if ! flock -w "$LOCK_WAIT" 9; then
    echo "[jobs] 另一实例正在运行（$name），退出"
    exit 0
  fi
}
