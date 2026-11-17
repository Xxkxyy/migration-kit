# -*- coding: utf-8 -*-
"""主机清单模板 —— 复制为 config/hosts.py 后填写真实值。

约定:
  * 真实凭据只存在于 config/hosts.py（已在 .gitignore 中排除），不入库、不外发
  * role 用于脚本筛选: db / app / storage / all
  * src_ip / dst_ip 成对出现，表示"同一逻辑角色"在新旧环境的地址
"""

# ============ 源端（旧 CentOS 7） ============
SRC = [
    # name, ip,            user, password,           role,    note
    ("old-db",  "10.0.0.221", "root", "<PASSWORD>", "db",    "MySQL 5.7 独立数据库机"),
    ("old-002", "10.0.0.222", "root", "<PASSWORD>", "app",   "网关/认证/注册中心/中间件"),
    ("old-003", "10.0.0.223", "root", "<PASSWORD>", "app",   "护理/病历/管理/CA/Rule"),
    ("old-004", "10.0.0.224", "root", "<PASSWORD>", "storage", "SeaweedFS 集群 A"),
    ("old-005", "10.0.0.225", "root", "<PASSWORD>", "storage", "SeaweedFS 集群 B"),
]

# ============ 目标端（新 Anolis/RHEL 8） ============
DST = [
    ("new-db",  "10.1.0.196", "root", "<PASSWORD>", "db",    "新数据库机（LVM 合成 2T）"),
    ("new-002", "10.1.0.197", "root", "<PASSWORD>", "app",   "对应 old-002"),
    ("new-003", "10.1.0.198", "root", "<PASSWORD>", "app",   "对应 old-003"),
    ("new-004", "10.1.0.199", "root", "<PASSWORD>", "storage", "对应 old-004"),
    ("new-005", "10.1.0.201", "root", "<PASSWORD>", "storage", "对应 old-005"),
]

# ============ 角色映射（文件同步 / 容器重建按此对齐） ============
PAIRS = [
    ("old-002", "new-002"),
    ("old-003", "new-003"),
    ("old-004", "new-004"),
    ("old-005", "new-005"),
]

# ============ 路径约定（如与现场不同，改这里即可） ============
PATHS = {
    "app_root":   "/data/app/docker-scloud",     # 应用根目录（compose + conf + apps）
    "mysql_data": "/data/mysql/data",            # MySQL datadir
    "mysql_bin":  "/usr/local/mysql/bin/mysql",  # mysql 客户端绝对路径
    "work_dir":   "/data/_mig",                  # 迁移工作目录（日志/锁/临时脚本）
}

# ============ MySQL ============
MYSQL = {
    "src_client": "/usr/bin/mysql -uroot -p<PASSWORD> -N",
    "dst_client": "/usr/local/mysql/bin/mysql -uroot -N",
    "socket": "",
}

# ============ 新库参数 ============
NEW_DB = {
    "root_password": "<PASSWORD>",
    "innodb_buffer_pool": "20G",
}
