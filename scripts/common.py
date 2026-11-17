# -*- coding: utf-8 -*-
"""公共 SSH 通道 —— 迁移工具包的基础设施。

设计要点（都是踩过坑才加上的）:
  1) 连接按主机缓存，不放在 thread-local 里 —— 否则旁路线程会不断新建连接并泄漏
  2) 所有命令都有"硬超时"：旁路线程 + join 超时 + 丢弃连接
  3) transport 开 keepalive，避免 NAT/防火墙把空闲连接掐断
  4) 所有输出统一为 str，调用方不需要关心 bytes/解码
  5) 危险操作统一走 dry-run 开关（调用方实现）

用法:
    from common import get_conn, run, sh, load_config
    cfg = load_config()
    c = get_conn("10.1.0.197")
    print(run(c, "docker ps", timeout=60))
"""
from __future__ import annotations

import os
import sys
import threading
import time

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG_DIR = os.path.join(ROOT, "config")

_CONNS = {}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- 配置
def load_config():
    """加载 config/hosts.py（真实凭据文件，不入库）。"""
    if CONFIG_DIR not in sys.path:
        sys.path.insert(0, CONFIG_DIR)
    try:
        import hosts as hosts_module  # type: ignore
    except ImportError:
        sys.exit("缺少 config/hosts.py —— 请先 cp config/hosts.example.py config/hosts.py")

    hosts = {}
    for row in hosts_module.SRC + hosts_module.DST:
        name, ip, user, pwd, role = row[0], row[1], row[2], row[3], row[4]
        hosts[name] = {"name": name, "ip": ip, "user": user, "pwd": pwd, "role": role}
    return {"hosts": hosts, "pairs": hosts_module.PAIRS, "paths": hosts_module.PATHS,
            "mysql": hosts_module.MYSQL, "new_db": hosts_module.NEW_DB,
            "src": [r[0] for r in hosts_module.SRC],
            "dst": [r[0] for r in hosts_module.DST]}


def _rows(seq):
    for r in seq:
        yield {"name": r[0], "ip": r[1]}


# --------------------------------------------------------------------------- 连接
def get_conn(ip, user="root", pwd="", port=22, timeout=20):
    """取（或建立）到某台机器的连接。失败重试 2 次。"""
    key = (ip, user)
    with _LOCK:
        c = _CONNS.get(key)
        if c is not None:
            tr = c.get_transport()
            if tr is not None and tr.is_active():
                return c
            try:
                c.close()
            except Exception:
                pass
            _CONNS.pop(key, None)

    last = None
    for attempt in range(3):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(ip, port, user, pwd, timeout=timeout, banner_timeout=timeout,
                      auth_timeout=timeout, look_for_keys=False, allow_agent=False)
            c.get_transport().set_keepalive(20)
            with _LOCK:
                _CONNS[key] = c
            return c
        except Exception as exc:                      # noqa: BLE001
            last = exc
            time.sleep(2 + attempt * 3)
    raise RuntimeError("SSH {} 连接失败: {}".format(ip, last))


def drop(ip, user="root"):
    """丢弃连接（超时后必须丢弃，否则后续命令全部串行卡死）。"""
    key = (ip, user)
    with _LOCK:
        c = _CONNS.pop(key, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def close_all():
    for key in list(_CONNS):
        try:
            _CONNS[key].close()
        except Exception:
            pass
        _CONNS.pop(key, None)


# --------------------------------------------------------------------------- 执行
def _exec(conn, cmd, timeout):
    _, stdout, stderr = conn.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return stdout.channel.recv_exit_status(), out, err


def run(conn, cmd, timeout=120):
    """带硬超时的执行。返回 (rc, stdout, stderr)；超时 rc=-2。"""
    box = {}

    def _worker():
        try:
            box["r"] = _exec(conn, cmd, timeout)
        except Exception as exc:                       # noqa: BLE001
            box["r"] = (-1, "", repr(exc))

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout + 15)
    if th.is_alive():
        # 连接已不可信，必须丢弃（注意：不要在这里再取新连接）
        try:
            conn.close()
        except Exception:
            pass
        return -2, "", "HARD_TIMEOUT after {}s".format(timeout + 15)
    return box.get("r", (-1, "", "no result"))


def sh(ip, cmd, timeout=120, cfg=None, user="root"):
    """按 IP 直接执行（内部取连接）。"""
    pwd = ""
    if cfg:
        for v in cfg["hosts"].values():
            if v["ip"] == ip:
                pwd = v["pwd"]
                user = v["user"]
                break
    c = get_conn(ip, user, pwd)
    rc, out, err = run(c, cmd, timeout)
    if rc == -2:
        drop(ip, user)
    return rc, out, err


# --------------------------------------------------------------------------- 小工具
def log(msg, path=None):
    line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def human_mb(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "{:.1f}{}".format(n, unit)
        n /= 1024.0
    return "{:.1f}PB".format(n)


def ask_apply(flag):
    """统一 dry-run 提示。"""
    if not flag:
        print("  [dry-run] 加 --apply 才会真正执行", flush=True)
    return flag
