#!/bin/bash
# =============================================================================
# mysql_init.sh —— 在空机器上装一套 MySQL 5.7（二进制包 + 独立 datadir + systemd）
#
# 本次现场用它重建了 196 的整套实例（原来那套被放弃，因为表膨胀/统计全错）。
#
# 前置:
#   * 已下载 mysql-5.7.44-linux-glibc2.12-x86_64.tar.gz 到 /tmp
#   * /data 已挂载（可用 lvm_merge.sh 合成单块大盘）
#
# 用法:
#   bash mysql_init.sh /tmp/mysql-5.7.44-linux-glibc2.12-x86_64.tar.gz '你的Root密码'
# =============================================================================
set -euo pipefail

TARBALL=${1:?用法: mysql_init.sh <mysql tar.gz> <root密码>}
ROOT_PWD=${2:?需要 root 密码}
BASE=/usr/local/mysql
DATA=/data/mysql
PKG=$(basename "$TARBALL" .tar.gz)
DIR=$(echo "$PKG" | sed 's/-linux-glibc.*//')

say() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---- 1. 依赖（libaio 是 5.7 在 EL8 上唯一常见缺包）----
say "安装依赖"
dnf install -y libaio ncurses-compat-libs 2>/dev/null || yum install -y libaio ncurses-libs

# ---- 2. 用户与目录 ----
id mysql >/dev/null 2>&1 || { say "创建 mysql 用户"; useradd -r -s /sbin/nologin mysql; }
mkdir -p "$DATA"/{data,binlog,log,tmp}
chown -R mysql:mysql "$DATA"

# ---- 3. 解包 ----
say "解包到 $BASE"
rm -rf "$BASE"
tar -xf "$TARBALL" -C /usr/local
ln -sfn "/usr/local/$DIR" "$BASE"

# ---- 4. 配置文件（生产基线，非导入期；导入期用 config/my.cnf.import 覆盖）----
say "写 /etc/my.cnf"
cat > /etc/my.cnf <<'EOF'
[mysqld]
user=mysql
basedir=/usr/local/mysql
datadir=/data/mysql/data
socket=/data/mysql/mysql.sock
pid-file=/data/mysql/mysqld.pid
port=3306

character-set-server=utf8mb4
collation-server=utf8mb4_general_ci
lower_case_table_names=1
max_connections=800
max_allowed_packet=1G
skip-name-resolve=1

innodb_buffer_pool_size=8G
innodb_log_file_size=1G
innodb_flush_log_at_trx_commit=2
innodb_doublewrite=1
innodb_io_capacity=2000
innodb_io_capacity_max=4000
innodb_stats_persistent=ON
innodb_stats_auto_recalc=ON

log-error=/data/mysql/log/mysqld.err
slow_query_log=ON
slow_query_log_file=/data/mysql/log/slow.log
long_query_time=2

[client]
socket=/data/mysql/mysql.sock
default-character-set=utf8mb4
EOF

# ---- 5. 初始化（5.7 用 --initialize-insecure，再自己设密码）----
if [ -d "$DATA/data/mysql" ]; then
  say "datadir 已有数据，跳过初始化（如需重来请先清空 $DATA/data）"
else
  say "初始化 datadir"
  "$BASE/bin/mysqld" --defaults-file=/etc/my.cnf --initialize-insecure --user=mysql
fi

# ---- 6. systemd ----
say "安装 systemd 单元"
cat > /usr/lib/systemd/system/mysqld.service <<'EOF'
[Unit]
Description=MySQL Server 5.7
After=network.target

[Service]
Type=simple
User=mysql
ExecStart=/usr/local/mysql/bin/mysqld --defaults-file=/etc/my.cnf
ExecStop=/usr/local/mysql/bin/mysqladmin -uroot -p'__PWD__' shutdown
Restart=on-failure
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

sed -i "s/__PWD__/${ROOT_PWD//\//\\/}/" /usr/lib/systemd/system/mysqld.service
systemctl daemon-reload
systemctl enable mysqld
systemctl start mysqld
sleep 5

# ---- 7. 设密码 + 常用健康检查 ----
say "设置 root 密码并自检"
"$BASE/bin/mysql" -uroot --skip-password -e "
  alter user 'root'@'localhost' identified by '${ROOT_PWD}';
  flush privileges;" 2>/dev/null || true

# 本机免密（脚本用；不要把这份文件带到别的机器）
cat > /root/.my.cnf <<EOF
[client]
user=root
password=${ROOT_PWD}
socket=/data/mysql/mysql.sock
EOF
chmod 600 /root/.my.cnf

"$BASE/bin/mysql" -uroot -p"$ROOT_PWD" -N -e "
  select version();
  select @@datadir, @@innodb_stats_auto_recalc, @@innodb_flush_log_at_trx_commit,
         @@innodb_doublewrite, @@log_bin;"

say "完成。导入期参数请: cp config/my.cnf.import /etc/my.cnf && systemctl restart mysqld"
say "导入完成后务必改回: flush=2 / doublewrite=1 / log_bin=1 / stats_auto_recalc=ON"
