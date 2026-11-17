# -*- coding: utf-8 -*-
"""阶段 1 · 盘点

只读脚本。产出 inventory/01-survey.md，作为后续所有阶段的输入与切流后的排查依据。

用法:
    python scripts/01_survey.py                 # 全部主机
    python scripts/01_survey.py --only new-db   # 只盘某一台
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, run  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "inventory")


def blocks(host, mysql_client):
    """返回 [(标题, 命令, 超时)]，全部为只读命令。"""
    return [
        ("系统", "cat /etc/os-release | head -3; uname -r; hostname; "
                 "date '+%Z %z'; getenforce 2>/dev/null || true", 60),
        ("磁盘", "df -h | grep -vE 'tmpfs|overlay'; echo '---'; lsblk -o NAME,SIZE,TYPE,MOUNTPOINT", 60),
        ("Docker", "docker version --format '{{.Server.Version}}' 2>/dev/null || echo NO_DOCKER; "
                   "docker info --format '{{.Driver}}' 2>/dev/null || true", 60),
        ("镜像", "docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' 2>/dev/null | sort", 120),
        ("容器", "docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>/dev/null", 120),
        ("容器挂载", "for n in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do "
                     "echo \"## $n\"; docker inspect $n --format "
                     "'{{range .Mounts}}{{.Source}} -> {{.Destination}}{{\"\\n\"}}{{end}}' 2>/dev/null; "
                     "done", 180),
        ("容器启动参数", "for n in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do "
                         "echo \"## $n\"; docker inspect $n --format "
                         "'EP={{.Config.Entrypoint}} CMD={{.Config.Cmd}}' 2>/dev/null; done", 180),
        ("监听端口", "ss -lntp 2>/dev/null | awk 'NR>1{print $4, $6}' | sort -u | head -60", 60),
        ("MySQL", "{} -e \"select version()\" 2>/dev/null || echo NO_MYSQL".format(mysql_client), 60),
        ("库与表", "{} -e \"select table_schema, count(*) tables, "
                   "round(sum(data_length+index_length)/1024/1024/1024,2) gb "
                   "from information_schema.tables where table_schema not in "
                   "('mysql','sys','information_schema','performance_schema') "
                   "group by table_schema order by gb desc\" 2>/dev/null || true".format(mysql_client), 300),
        ("大表 Top20", "{} -e \"select table_schema, table_name, table_rows, "
                       "round((data_length+index_length)/1024/1024) mb "
                       "from information_schema.tables where table_schema not in "
                       "('mysql','sys','information_schema','performance_schema') "
                       "order by data_length+index_length desc limit 20\" 2>/dev/null || true"
         .format(mysql_client), 300),
        ("账号授权", "{} -e \"select user, host from mysql.user where host not in "
                     "('localhost','127.0.0.1')\" 2>/dev/null || true".format(mysql_client), 60),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只盘指定主机名")
    ap.add_argument("--old-ip-prefix", default="", help="用于扫描写死旧 IP 的配置文件，如 10.0.0.")
    args = ap.parse_args()

    cfg = load_config()
    mysql_client = cfg["mysql"]["dst_client"]
    os.makedirs(OUT_DIR, exist_ok=True)

    for name, h in cfg["hosts"].items():
        if args.only and name != args.only:
            continue
        print("=" * 70, flush=True)
        print("[{}] {}  role={}".format(name, h["ip"], h["role"]), flush=True)
        try:
            conn = get_conn(h["ip"], h["user"], h["pwd"])
        except Exception as exc:                       # noqa: BLE001
            print("  !! 连接失败: {}".format(exc), flush=True)
            continue

        lines = ["# 盘点 · {} ({})".format(name, h["ip"]), ""]
        client = cfg["mysql"]["src_client"] if h["role"] == "db" and "src" in name else mysql_client
        for title, cmd, tmo in blocks(name, client):
            rc, out, err = run(conn, cmd, timeout=tmo)
            body = (out or err).strip()
            print("  -- {} ({} 行)".format(title, len(body.splitlines())), flush=True)
            lines += ["## " + title, "```", body[:6000], "```", ""]

        if args.old_ip_prefix:
            rc, out, _ = run(
                conn,
                "grep -rlI '{}' /data/app /opt /etc 2>/dev/null "
                "--exclude-dir=mysql --exclude='*.log' | head -200".format(args.old_ip_prefix),
                timeout=300)
            n = len([x for x in out.splitlines() if x.strip()])
            print("  -- 含旧 IP 的配置文件: {} 个".format(n), flush=True)
            lines += ["## 含旧 IP 的配置文件 ({} 个)".format(n), "```", out[:6000], "```", ""]

        path = os.path.join(OUT_DIR, "01-survey-{}.md".format(name))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        print("  -> {}".format(path), flush=True)

    close_all()
    print("\n盘点完成，输出目录: {}".format(OUT_DIR), flush=True)


if __name__ == "__main__":
    main()
