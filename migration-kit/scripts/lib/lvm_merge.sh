#!/bin/bash
# =============================================================================
# lvm_merge.sh —— 把多块数据盘合成一个逻辑卷（本次把 1T+1T 合成 2T）
#
# 为什么要在迁移前做:
#   单盘 1T 装不下 726G 的库 + 313G 的另一库 + binlog + 临时空间；
#   用 LVM 合成后可以按需扩容，且不用改 datadir 路径。
#
# 安全约束（脚本会拒绝执行的情况）:
#   * 目标盘已挂载
#   * 目标盘上有文件系统签名或分区表（除非显式 --force）
#   * LV 已存在且已挂载
#
# 用法:
#   bash lvm_merge.sh /dev/vdb /dev/vdc            # 预演（只看计划）
#   bash lvm_merge.sh /dev/vdb /dev/vdc --yes      # 真正执行
#   VG=vg_data LV=lv_data MNT=/data bash lvm_merge.sh /dev/vdb /dev/vdc --yes
# =============================================================================
set -euo pipefail

DEVICES=()
YES=0
FORCE=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --force) FORCE=1 ;;
    *) DEVICES+=("$a") ;;
  esac
done

VG=${VG:-vg_data}
LV=${LV:-lv_data}
MNT=${MNT:-/data}
FS=${FS:-xfs}

say() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "!! $*" >&2; exit 1; }

[ ${#DEVICES[@]} -ge 1 ] || die "至少给一个块设备，例如 /dev/vdb /dev/vdc"

echo "==== 计划 ===="
echo "物理卷 : ${DEVICES[*]}"
echo "卷组   : $VG"
echo "逻辑卷 : /dev/$VG/$LV"
echo "挂载点 : $MNT  ($FS)"
echo

# ---- 安全预检 ----
for d in "${DEVICES[@]}"; do
  [ -b "$d" ] || die "$d 不是块设备"
  if findmnt -S "$d" >/dev/null 2>&1; then
    die "$d 已挂载，拒绝操作"
  fi
  if lsblk -no FSTYPE "$d" 2>/dev/null | grep -qE '.+'; then
    SIG=$(lsblk -no FSTYPE "$d" | head -1)
    echo "警告: $d 上已有文件系统签名 ($SIG)"
    [ "$FORCE" -eq 1 ] || die "如确认可清空，请加 --force"
  fi
done

if [ "$YES" -eq 0 ]; then
  say "预演结束。加 --yes 执行（会清空上述设备上的数据）"
  exit 0
fi

# ---- 建 PV / VG / LV ----
command -v pvcreate >/dev/null || dnf install -y lvm2
say "pvcreate"
pvcreate -y -ff "${DEVICES[@]}"

say "vgcreate $VG"
vgcreate -y "$VG" "${DEVICES[@]}" 2>/dev/null || vgextend -y "$VG" "${DEVICES[@]}"

say "lvcreate -l 100%FREE -n $LV"
lvcreate -y -l 100%FREE -n "$LV" "$VG"

# ---- 文件系统与挂载 ----
say "mkfs.$FS /dev/$VG/$LV"
mkfs."$FS" -f "/dev/$VG/$LV"

mkdir -p "$MNT"
if mountpoint -q "$MNT"; then
  die "$MNT 已挂载，请先 umount"
fi
mount "/dev/$VG/$LV" "$MNT"

UUID=$(blkid -s UUID -o value "/dev/$VG/$LV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $MNT $FS defaults 0 0" >> /etc/fstab
say "已写入 /etc/fstab: UUID=$UUID"

# ---- 结果 ----
echo
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT | grep -E 'vdb|vdc|vg_data|-lv_|NAME'
df -hT "$MNT"
say "完成。后续扩容: lvextend -l +100%FREE /dev/$VG/$LV && xfs_growfs $MNT"
