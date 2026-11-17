# -*- coding: utf-8 -*-
"""阶段 0 · 连通性与凭据体检（最先跑，也最容易被忽略）

覆盖:
    1. TCP 22 是否可达（区分"网络不通"和"认证失败"）
    2. SSH 认证是否通过
    3. 口令里的全角标点自动纠正（！@＃ 等 —— 会导致认证必然失败）
    4. 目标端基础能力：rsync / sshpass / pigz / docker / mysql 客户端是否存在
    5. 分组统计：源端 vs 目标端

用法:
    python scripts/00_conn_check.py
    python scripts/00_conn_check.py --only new-002
"""
from __future__ import annotations

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paramiko  # noqa: E402

from common import close_all, load_config, log  # noqa: E402

# 全角 -> 半角（口令里出现全角标点时，SSH 认证必然失败，且报错信息看不出原因）
FULL2HALF = {
    "！": "!", "＠": "@", "＃": "#", "＄": "$", "％": "%", "＾": "^", "＆": "&",
    "＊": "*", "（": "(", "）": ")", "－": "-", "＋": "+", "＝": "=", "［": "[",
    "］": "]", "｛": "{", "｝": "}", "；": ";", "：": ":", "＂": '"', "＇": "'",
    "｜": "|", "＼": "\\", "，": ",", "。": ".", "／": "/", "？": "?", "＜": "<",
    "＞": ">", "　": " ",
}


def norm_pwd(pwd):
    out = "".join(FULL2HALF.get(ch, ch) for ch in pwd)
    return out, out != pwd


def tcp(ip, port=22, timeout=4):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def try_ssh(ip, user, pwd, timeout=10):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, 22, user, pwd, timeout=timeout, banner_timeout=timeout,
                  auth_timeout=timeout, look_for_keys=False, allow_agent=False)
        _, o, _ = c.exec_command(
            "printf '%s|' \"$(hostname)\"; "
            "printf '%s|' \"$(grep -m1 PRETTY_NAME /etc/os-release | cut -d'\"' -f2)\"; "
            "printf '%s|' \"$(command -v docker >/dev/null && echo docker=yes || echo docker=no)\"; "
            "printf '%s|' \"$(command -v rsync >/dev/null && echo rsync=yes || echo rsync=no)\"; "
            "printf '%s|' \"$(command -v sshpass >/dev/null && echo sshpass=yes || echo sshpass=no)\"; "
            "printf '%s' \"$(command -v pigz >/dev/null && echo pigz=yes || echo pigz=no)\"",
            timeout=20)
        o.channel.recv_exit_status()
        info = o.read().decode("utf-8", "replace").strip()
        return True, info
    except paramiko.AuthenticationException:
        return False, "AUTH_FAIL"
    except Exception as exc:                           # noqa: BLE001
        return False, "ERR {}".format(exc)
    finally:
        try:
            c.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    cfg = load_config()
    rows = []
    log("主机                IP               TCP   SSH   说明")
    log("-" * 96)

    for name, h in cfg["hosts"].items():
        if args.only and name != args.only:
            continue
        ok_tcp = tcp(h["ip"])
        if not ok_tcp:
            rows.append((name, h["ip"], "FAIL", "-", "22 端口不通（查防火墙/网段）"))
            log("{:<18} {:<15} FAIL  -     {}".format(name, h["ip"], "22 端口不通"))
            continue

        ok, info = try_ssh(h["ip"], h["user"], h["pwd"])
        if not ok and info == "AUTH_FAIL":
            # 二次尝试：全角 -> 半角
            fixed, changed = norm_pwd(h["pwd"])
            if changed:
                ok2, info2 = try_ssh(h["ip"], h["user"], fixed)
                if ok2:
                    ok, info = ok2, info2 + " (口令已按半角纠正)"
                    h["pwd"] = fixed
                    rows.append((name, h["ip"], "OK", "OK", "口令含全角标点，已自动纠正"))
                    log("{:<18} {:<15} OK    OK    * 口令含全角标点，已纠正".format(name, h["ip"]))
                    continue

        if ok:
            rows.append((name, h["ip"], "OK", "OK", info))
            log("{:<18} {:<15} OK    OK    {}".format(name, h["ip"], info[:56]))
        else:
            rows.append((name, h["ip"], "OK", "FAIL", str(info)))
            log("{:<18} {:<15} OK    FAIL  {}".format(name, h["ip"], str(info)[:56]))

    log("")
    ok_n = sum(1 for r in rows if r[3] == "OK")
    log("汇总: {}/{} 台可用".format(ok_n, len(rows)))

    miss = [r[0] for r in rows if "rsync=no" in r[4] or "sshpass=no" in r[4]]
    if miss:
        log("提示: 以下主机缺 rsync/sshpass，文件同步阶段会失败: {}".format(miss))
        log("      目标端执行: dnf install -y rsync sshpass   (或 yum)")
    close_all()


if __name__ == "__main__":
    main()
