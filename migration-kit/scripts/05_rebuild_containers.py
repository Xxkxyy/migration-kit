# -*- coding: utf-8 -*-
"""阶段 6 · 按 docker inspect 定义重建容器

为什么不用 compose:
    现场往往没有 docker-compose 插件，且 compose 文件可能早就和实际运行的容器不一致。
    以「实际运行的容器」为准重建，是最忠实的迁移。

三个必须处理的细节:
    1. Entrypoint 与 Cmd 只能拼一次（否则变成 java java -jar / weed weed server）
    2. Env 和 Cmd 参数里的旧 IP 都要替换
    3. 挂载路径用 inspect 里的绝对路径，不用 compose 的相对路径

用法:
    python scripts/05_rebuild_containers.py                 # 只看计划
    python scripts/05_rebuild_containers.py --apply         # 真正创建
    python scripts/05_rebuild_containers.py --apply --only new-002
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402


def ip_map(cfg):
    """旧 IP -> 新 IP，按 PAIRS 的两端对应。"""
    m = {}
    for s_name, d_name in cfg["pairs"]:
        m[cfg["hosts"][s_name]["ip"]] = cfg["hosts"][d_name]["ip"]
    sdb = [n for n in cfg["src"] if cfg["hosts"][n]["role"] == "db"][0]
    ddb = [n for n in cfg["dst"] if cfg["hosts"][n]["role"] == "db"][0]
    m[cfg["hosts"][sdb]["ip"]] = cfg["hosts"][ddb]["ip"]
    return m


def fix(text, mapping):
    for old, new in mapping.items():
        text = text.replace(old, new)
    return text


def inspect_all(conn):
    out = run(conn, "docker ps -a --format '{{.Names}}'", 120)[1].split()
    defs = {}
    for name in out:
        rc, js, _ = run(conn, "docker inspect {}".format(shlex.quote(name)), 120)
        if rc != 0 or not js.strip():
            continue
        try:
            defs[name] = json.loads(js)[0]
        except Exception:                              # noqa: BLE001
            continue
    return defs


def build_run(name, c, mapping, default_net):
    cfg_obj = c.get("Config") or {}
    hc = c.get("HostConfig") or {}
    image = cfg_obj.get("Image") or ""
    parts = ["docker run -d --name", shlex.quote(name)]

    entry = cfg_obj.get("Entrypoint") or []
    cmd = cfg_obj.get("Cmd") or []
    if entry:
        # 镜像自带 ENTRYPOINT，只传 Cmd（关键！）
        args = cmd
    else:
        args = entry + cmd

    net = hc.get("NetworkMode") or default_net
    if net and net not in ("default", "bridge"):
        parts += ["--network", shlex.quote(net)]

    # 环境变量：全量透传（去掉 docker 自动注入的 HOSTNAME），并替换旧 IP。
    # 注意：不能只白名单几个 key，否则会丢失 SPRING_PROFILES_ACTIVE 这类业务变量。
    for e in (cfg_obj.get("Env") or []):
        if e.startswith("HOSTNAME="):
            continue
        parts += ["-e", shlex.quote(fix(e, mapping))]

    for b in (hc.get("Binds") or []):
        parts += ["-v", shlex.quote(b)]

    for cport, binds in ((hc.get("PortBindings") or {}).items()):
        for b in (binds or []):
            hp = b.get("HostPort")
            if hp:
                parts += ["-p", "{0}:{1}".format(hp, cport.split("/")[0])]

    if hc.get("Memory"):
        parts += ["--memory", str(hc["Memory"])]
    if hc.get("Privileged"):
        parts += ["--privileged"]
    if cfg_obj.get("WorkingDir"):
        parts += ["-w", shlex.quote(cfg_obj["WorkingDir"])]

    rp = (hc.get("RestartPolicy") or {}).get("Name")
    if rp and rp != "no":
        parts += ["--restart", rp]

    parts.append(shlex.quote(image))
    parts += [shlex.quote(fix(a, mapping)) for a in args]
    return " ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="只处理某台目标机（配置里的 name）")
    ap.add_argument("--force", action="store_true", help="同名容器已存在时先删除再建")
    args = ap.parse_args()

    cfg = load_config()
    mapping = ip_map(cfg)
    log("IP 映射: {}".format(mapping))

    for s_name, d_name in cfg["pairs"]:
        if args.only and d_name != args.only:
            continue
        s, d = cfg["hosts"][s_name], cfg["hosts"][d_name]
        log("=== {} -> {} ===".format(s_name, d_name))
        try:
            cs = get_conn(s["ip"], s["user"], s["pwd"])
            cd = get_conn(d["ip"], d["user"], d["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        defs = inspect_all(cs)
        existing = set(run(cd, "docker ps -a --format '{{.Names}}'", 120)[1].split())
        log("  源端容器 {} 个，目标端已有 {} 个".format(len(defs), len(existing)))

        for name, c in defs.items():
            cmd = build_run(name, c, mapping, "bridge")
            if name in existing and not args.force:
                log("  SKIP {} (已存在)".format(name))
                continue
            if not args.apply:
                log("  [dry-run] {}".format(cmd[:240]))
                continue
            if name in existing and args.force:
                run(cd, "docker rm -f {}".format(shlex.quote(name)), 300)
            rc, out, err = run(cd, cmd, 600)
            log("  {} {} {}".format("OK  " if rc == 0 else "FAIL", name, (out or err).strip()[:160]))

    log("完成。按依赖顺序启动: nacos -> redis -> rabbitmq -> gateway -> 业务服务 -> nginx")
    close_all()


if __name__ == "__main__":
    main()
