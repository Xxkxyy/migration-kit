#!/bin/bash
# =============================================================================
# oracle_driver.sh —— 给 Spring Boot fat jar 注入 Oracle 驱动（安全版）
#
# 背景（真实事故）:
#   现场 manager 用了动态数据源（Oracle），但 fat jar 里没有 ojdbc。
#   第一次用 `jar uf` 注入后，容器启动直接失败：
#     Unable to open nested entry 'BOOT-INF/lib/ojdbc8.jar'
#     org.springframework.boot.loader.jar.JarFile.createJarFileFromEntry
#   原因：Spring Boot 的嵌套 jar 必须 **STORED（不压缩）**，
#        `jar uf` 默认 deflate，Launcher 无法随机定位 → 启动即崩。
#
# 正确做法:
#   jar uf0  <jar> <entry>      # -0 = store only
#   注入后必须 `unzip -v | grep ojdbc` 确认第一列是 Stored
#
# 用法:
#   bash oracle_driver.sh \
#     --jar /data/app/xxx/apps/manager/sshapp-manager.jar \
#     --driver /path/to/ojdbc8.jar \
#     --container manager            # 可选：在容器内执行（jar 工具在镜像里）
#     [--apply]
#
# 安全保证:
#   * 默认 dry-run
#   * 自动备份 <jar>.bak_oracle（已存在则复用，保留最原始版本）
#   * 注入后做 unzip -t 完整性校验 + Stored 校验
#   * 失败自动回滚到备份
# =============================================================================
set -uo pipefail

JAR=""; DRIVER=""; CONTAINER=""; APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --jar) JAR=$2; shift 2 ;;
    --driver) DRIVER=$2; shift 2 ;;
    --container) CONTAINER=$2; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
done

say() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "!! $*" >&2; exit 1; }

[ -n "$JAR" ]    || die "需要 --jar"
[ -n "$DRIVER" ] || die "需要 --driver"
[ -f "$JAR" ]    || die "$JAR 不存在"
[ -f "$DRIVER" ] || die "$DRIVER 不存在"

BACKUP="${JAR}.bak_oracle"
ENTRY="BOOT-INF/lib/$(basename "$DRIVER")"

# 在容器内执行时，路径要换成容器内的视角
if [ -n "$CONTAINER" ]; then
  RUN() { docker exec "$CONTAINER" sh -c "$1"; }
else
  RUN() { sh -c "$1"; }
fi

say "目标 jar : $JAR"
say "驱动     : $DRIVER ($(du -h "$DRIVER" | cut -f1))"
say "将注入为 : $ENTRY"

# 布局检查：必须是 Spring Boot fat jar
if ! RUN "unzip -l '$JAR' 2>/dev/null | grep -q 'BOOT-INF/lib/'"; then
  say "警告: 未发现 BOOT-INF/lib，可能不是 Spring Boot fat jar（或路径不同），请确认"
fi
if RUN "unzip -l '$JAR' 2>/dev/null | grep -q '$ENTRY'"; then
  say "驱动已存在于 jar 中，无需重复注入"
  exit 0
fi

if [ "$APPLY" -eq 0 ]; then
  say "预演结束。加 --apply 执行（会修改 jar，自动备份到 $BACKUP）"
  exit 0
fi

# 备份（只保留最初的一份）
RUN "[ -f '$BACKUP' ] || cp -p '$JAR' '$BACKUP'"
say "备份: $BACKUP"

# 注入（关键：-0 必须 STORED）
WORK="/tmp/_drvinj_$$"
RUN "rm -rf $WORK; mkdir -p $WORK/BOOT-INF/lib; \
     cp '$JAR' $WORK/app.jar; cp '$DRIVER' $WORK/$ENTRY; \
     cd $WORK && jar uf0 app.jar $ENTRY" || { say "注入失败，jar 未改动"; rm -rf "$WORK" 2>/dev/null; exit 1; }

# 三重校验
OK=1
RUN "unzip -t $WORK/app.jar >/dev/null 2>&1" || OK=0
METHOD=$(RUN "unzip -v $WORK/app.jar | grep '$(basename "$DRIVER")' | awk '{print \$2}'" | tr -d '\r')
say "压缩方式: ${METHOD:-未知}（必须是 Stored）"
[ "$METHOD" = "Stored" ] || OK=0

if [ "$OK" -ne 1 ]; then
  say "校验未通过，放弃替换（现有 jar 保持原样）"
  RUN "rm -rf $WORK"
  exit 1
fi

# 替换（mv 原子替换，运行中的 JVM 不受影响，重启后生效）
RUN "cp '$WORK/app.jar' '$JAR.new' && mv -f '$JAR.new' '$JAR' && chmod 644 '$JAR'"
RUN "rm -rf $WORK"

say "注入完成。务必重启容器后验证："
say "  docker restart <container>"
say "  docker logs --since 3m <container> | grep -iE 'nested|ojdbc|driver'"
say ""
say "回滚: cp -p '$BACKUP' '$JAR' && docker restart <container>"
