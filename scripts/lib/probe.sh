#!/bin/bash
# =============================================================================
# probe.sh —— 迁移探测命令库（全部只读，可在生产机直接跑）
#
# 用法:
#   bash probe.sh              # 跑全部
#   bash probe.sh sys disk     # 只跑指定段
#   bash probe.sh list         # 列出所有段
#
# 设计原则:
#   1. 每条命令都容错（2>/dev/null || true），缺工具不影响其他段
#   2. 每段输出以 "### 段名" 开头，便于机器切分与 diff
#   3. 绝不包含任何写操作（无 rm/docker rm/mysql DDL）
# =============================================================================

SECTIONS="sys disk net docker mysql app mid verify"
MYSQL="/usr/local/mysql/bin/mysql -uroot -N"
MYSQL_ALT="/usr/bin/mysql -uroot -N"

# 探测 mysql 客户端路径
pick_mysql() {
  for c in /usr/local/mysql/bin/mysql /usr/bin/mysql /usr/local/bin/mysql; do
    [ -x "$c" ] && { echo "$c -uroot -N"; return; }
  done
  echo "mysql -uroot -N"
}

sec_sys() {
  echo "### sys"
  echo "-- os --";        cat /etc/os-release 2>/dev/null | head -4
  echo "-- kernel --";    uname -a
  echo "-- cpu --";       nproc; grep -m1 'model name' /proc/cpuinfo 2>/dev/null
  echo "-- mem --";       free -g | head -2
  echo "-- tz --";        date '+%Z %z'; ls -l /etc/localtime 2>/dev/null | sed 's/.*-> //'
  echo "-- selinux --";   getenforce 2>/dev/null || echo none
  echo "-- ulimit --";    bash -c 'ulimit -n; ulimit -u'
  echo "-- swap --";      cat /proc/sys/vm/swappiness 2>/dev/null
  echo "-- timesync --";  (timedatectl 2>/dev/null | head -5) || echo no-timedatectl
  echo "-- uptime --";    uptime
  echo "-- boot --";      who -b 2>/dev/null
}

sec_disk() {
  echo "### disk"
  echo "-- df --";        df -hT | grep -vE 'tmpfs|overlay|devtmpfs'
  echo "-- lsblk --";     lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT 2>/dev/null
  echo "-- mount opts --"; findmnt -no SOURCE,TARGET,FSTYPE,OPTIONS /data 2>/dev/null || echo no-/data
  echo "-- inode --";     df -i | grep -vE 'tmpfs|overlay'
  echo "-- top dirs --";  du -sh /data/*/ 2>/dev/null | sort -h | tail -10
  echo "-- io sched --";  for d in /sys/block/*/queue/scheduler; do echo -n "$d: "; cat $d 2>/dev/null; done | head -6
  echo "-- fio/dd quick --"
  # 4K 同步写，测"每次提交要等多久"——迁移里最关键的容量指标
  dd if=/dev/zero of=/tmp/_probe_io bs=4k count=500 oflag=direct,dsync 2>&1 | tail -1
  rm -f /tmp/_probe_io
  echo "-- iostat --";    (iostat -dx 1 2 2>/dev/null | tail -8) || echo no-iostat
}

sec_net() {
  echo "### net"
  echo "-- addr --";      ip -o -4 addr show scope global | awk '{print $2,$4}'
  echo "-- route --";     ip route | head -10
  echo "-- dns --";       grep -E '^nameserver' /etc/resolv.conf 2>/dev/null || echo no-resolv
  echo "-- mtu --";       ip link show | grep -E 'mtu' | head -5
  echo "-- neigh --";     ip neigh | head -10
  echo "-- listen --";    ss -lntp 2>/dev/null | awk 'NR>1{print $4,$6}' | sort -u | head -40
  echo "-- conn count --";ss -s 2>/dev/null | head -3
  echo "-- firewall --";  (firewall-cmd --list-ports 2>/dev/null; firewall-cmd --state 2>/dev/null) || echo no-firewalld
  echo "-- ip_forward --";cat /proc/sys/net/ipv4/ip_forward 2>/dev/null
}

sec_docker() {
  echo "### docker"
  echo "-- version --";   docker version --format '{{.Server.Version}}' 2>/dev/null || echo NO_DOCKER
  echo "-- info --";      docker info --format 'driver={{.Driver}} root={{.DockerRootDir}} cgroup={{.CgroupDriver}}' 2>/dev/null
  echo "-- images --";    docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}' 2>/dev/null | sort
  echo "-- ps --";        docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>/dev/null
  echo "-- port map --";  docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null
  echo "-- networks --";  docker network ls --format '{{.Name}}|{{.Driver}}|{{.Scope}}' 2>/dev/null
  echo "-- volumes --";   docker volume ls -q 2>/dev/null | head -20
  echo "-- restart policy --"
  for n in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    echo -n "$n="; docker inspect $n --format '{{.HostConfig.RestartPolicy.Name}}' 2>/dev/null
  done
  echo "-- entrypoint/cmd --"
  for n in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    echo -n "$n|"; docker inspect $n --format 'EP={{.Config.Entrypoint}} CMD={{.Config.Cmd}}' 2>/dev/null
  done
  echo "-- mounts --"
  for n in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    echo "## $n"
    docker inspect $n --format '{{range .Mounts}}{{.Source}} => {{.Destination}}{{"\n"}}{{end}}' 2>/dev/null
  done
  echo "-- env --"
  for n in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    echo "## $n"
    docker inspect $n --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -vE '^(PATH|HOME|HOSTNAME|LANG|TERM)='
  done
  echo "-- size --";      docker system df 2>/dev/null
  echo "-- non-up --";    docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -v ' Up ' | head -20
}

sec_mysql() {
  M=$(pick_mysql)
  echo "### mysql"
  echo "-- client --";    echo "$M"
  echo "-- version --";   $M -e "select version(), @@version_comment" 2>/dev/null || echo NO_MYSQL
  echo "-- key vars --"
  $M -e "select @@datadir, @@character_set_server, @@collation_server, @@lower_case_table_names,
                @@max_connections, @@innodb_buffer_pool_size/1024/1024/1024 as bp_gb,
                @@innodb_flush_log_at_trx_commit as flush, @@innodb_doublewrite as dw,
                @@innodb_flush_method, @@sync_binlog, @@log_bin,
                @@innodb_stats_auto_recalc as stats_recalc, @@innodb_io_capacity" 2>/dev/null
  echo "-- status --"
  $M -e "show global status where Variable_name in
         ('Threads_connected','Max_used_connections','Questions','Com_insert','Com_select',
          'Aborted_connects','Aborted_clients','Innodb_buffer_pool_reads',
          'Innodb_buffer_pool_read_requests','Uptime')" 2>/dev/null
  echo "-- databases --"
  $M -e "select schema_name, default_character_set_name, default_collation_name
         from information_schema.schemata
         where schema_name not in ('mysql','sys','information_schema','performance_schema')" 2>/dev/null
  echo "-- db size --"
  $M -e "select table_schema, count(*) tables,
                round(sum(data_length+index_length)/1024/1024/1024,2) gb
         from information_schema.tables
         where table_schema not in ('mysql','sys','information_schema','performance_schema')
         group by table_schema order by gb desc" 2>/dev/null
  echo "-- top tables --"
  $M -e "select table_schema, table_name, table_rows,
                round((data_length+index_length)/1024/1024) mb
         from information_schema.tables
         where table_schema not in ('mysql','sys','information_schema','performance_schema')
         order by data_length+index_length desc limit 20" 2>/dev/null
  echo "-- object count --"
  $M -e "select
      (select count(*) from information_schema.tables where table_schema not in ('mysql','sys','information_schema','performance_schema')) tables_all,
      (select count(*) from information_schema.views where table_schema not in ('mysql','sys','information_schema','performance_schema')) views_all,
      (select count(*) from information_schema.routines where routine_schema not in ('mysql','sys','information_schema','performance_schema')) routines_all,
      (select count(*) from information_schema.triggers where trigger_schema not in ('mysql','sys','information_schema','performance_schema')) triggers_all" 2>/dev/null
  echo "-- users --"
  $M -e "select user, host from mysql.user where host not in ('localhost','127.0.0.1')" 2>/dev/null
  echo "-- processlist --"
  $M -e "select id, user, host, db, command, time, state, left(info,60)
         from information_schema.processlist where command<>'Sleep' order by time desc limit 10" 2>/dev/null
  echo "-- long query --"
  $M -e "select id, db, time, state, left(info,80) from information_schema.processlist
         where command='Query' and time>60 and id<>connection_id()" 2>/dev/null
  echo "-- stats zero --"
  $M -e "select count(*) non_empty_zero_stats from mysql.innodb_table_stats s
         join information_schema.tables t on t.table_schema=s.database_name and t.table_name=s.table_name
         where s.n_rows=0 and t.data_length>20000 and s.database_name not in ('mysql','sys')" 2>/dev/null
  echo "-- slow log --"
  $M -e "select @@slow_query_log, @@long_query_time, @@log_output" 2>/dev/null
  echo "-- slow top --"
  $M -e "select round(query_time) sec, rows_sent, rows_examined, left(replace(sql_text,'\n',' '),100)
         from mysql.slow_log order by query_time desc limit 10" 2>/dev/null || true
  echo "-- error log tail --"
  tail -n 30 $(ls -t /data/mysql/*.err /var/log/mysqld.log 2>/dev/null | head -1) 2>/dev/null | grep -aE 'ERROR|Warning|Aborted|denied' | tail -10
}

sec_app() {
  echo "### app"
  echo "-- app root --";  ls -la /data/app 2>/dev/null | head -10
  echo "-- dir size --";  du -sh /data/app/*/*/ 2>/dev/null | sort -h | tail -10
  echo "-- compose --";   ls -l /data/app/*/docker-compose.y*ml 2>/dev/null
  echo "-- jar list --";  find /data/app -maxdepth 5 -name '*.jar' -printf '%s %p\n' 2>/dev/null | sort -rn | head -15
  echo "-- java proc --"; ps -ef | grep '[j]ava' | awk '{print $2, $8, $9, $10}' | head -10
  echo "-- ip in config --"
  for p in $(ls -d /data/app/* 2>/dev/null); do
    echo "## $p"
    grep -rlIE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$p" 2>/dev/null \
      --exclude='*.jar' --exclude='*.log' --exclude-dir=logs --exclude-dir=data \
      --exclude='*.bak*' | head -15
  done
  echo "-- log error scan --"
  for d in $(ls -d /data/app/*/*/logs 2>/dev/null | head -6); do
    echo "## $d"
    grep -ahoE '^[0-9-]{10} [0-9:]{8}.*(ERROR|Exception)' "$d"/*.log 2>/dev/null | tail -3 | cut -c1-160
  done
}

sec_mid() {
  echo "### mid"
  echo "-- redis --"
  docker exec $(docker ps --format '{{.Names}}' | grep -m1 redis) redis-cli info server 2>/dev/null | head -8 || echo no-redis
  echo "-- redis keys --"
  docker exec $(docker ps --format '{{.Names}}' | grep -m1 redis) redis-cli dbsize 2>/dev/null || true
  echo "-- rabbitmq vhost --"
  docker exec $(docker ps --format '{{.Names}}' | grep -m1 rabbitmq) rabbitmqctl list_vhosts 2>/dev/null | tail -6 || echo no-rabbitmq
  echo "-- rabbitmq users --"
  docker exec $(docker ps --format '{{.Names}}' | grep -m1 rabbitmq) rabbitmqctl list_users 2>/dev/null | tail -6 || true
  echo "-- nacos services --"
  curl -s 'http://127.0.0.1:8848/nacos/v1/ns/service/list?pageNo=1&pageSize=100' 2>/dev/null | head -c 600
  echo
  echo "-- seaweed master --"
  curl -s 'http://127.0.0.1:9333/cluster/status' 2>/dev/null | head -c 400 || echo no-weed
  echo
  echo "-- weed volumes --"
  curl -s 'http://127.0.0.1:9333/dir/status?pretty=y' 2>/dev/null | head -c 400 || true
}

sec_verify() {
  echo "### verify"
  M=$(pick_mysql)
  echo "-- file count --"
  for d in /data/app/*/; do
    [ -d "$d" ] || continue
    echo -n "$d files="; find "$d" -type f 2>/dev/null | wc -l
    echo -n "$d bytes="; find "$d" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'
  done
  echo "-- mysql counts --"
  for t in $($M -e "select concat(table_schema,'.',table_name) from information_schema.tables
                    where table_schema not in ('mysql','sys','information_schema','performance_schema')
                    and table_rows>1000000 limit 10" 2>/dev/null); do
    echo -n "$t "; $M -e "select count(*) from $t" 2>/dev/null
  done
  echo "-- http probes --"
  for p in 4431 9600 8848; do
    echo -n "127.0.0.1:$p -> "
    timeout 3 bash -c "echo > /dev/tcp/127.0.0.1/$p" 2>/dev/null && echo open || echo closed
  done
}

run_section() {
  case "$1" in
    sys) sec_sys ;;
    disk) sec_disk ;;
    net) sec_net ;;
    docker) sec_docker ;;
    mysql) sec_mysql ;;
    app) sec_app ;;
    mid) sec_mid ;;
    verify) sec_verify ;;
    list) echo "$SECTIONS" ;;
    *) echo "unknown section: $1" ;;
  esac
}

if [ $# -eq 0 ]; then
  for s in $SECTIONS; do run_section "$s"; echo; done
else
  for s in "$@"; do run_section "$s"; echo; done
fi
