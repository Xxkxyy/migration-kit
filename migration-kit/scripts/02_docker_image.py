# -*- coding: utf-8 -*-
"""阶段 2 · 镜像搬运（比对 IMAGE ID，而不是 tag）

流程: 源端 docker save -> 下载到本地 -> 上传到目标端 -> docker load -> 复核 ID

用法:
    python scripts/02_docker_image.py            # 只比对，不传
    python scripts/02_docker_image.py --apply    # 补齐缺失镜像
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import close_all, get_conn, load_config, log, run  # noqa: E402


def image_map(conn):
    """返回 {'repo:tag': image_id}">
    """
    rc, out, _ = run(conn, "docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null", 120)
    m = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] != "<none>:<none>":
            m[p[0]] = p[1]
    return m


def save_and_transfer(src, src_pwd, dst, dst_pwd, image, tag):
    """在源端 save，流式经本地落到目标端 load。返回 (ok, 说明)"""
    tmpdir = tempfile.mkdtemp(prefix="img_")
    local = os.path.join(tmpdir, image.replace("/", "_").replace(":", "_") + ".tar.gz")

    rc, out, err = run(src, "docker save '{}' | gzip -1 > /tmp/_mig_img.tar.gz && "
                            "ls -l /tmp/_mig_img.tar.gz | awk '{{print $5}}'".format(image), 3600)
    if rc != 0:
        return False, "save 失败: " + (err or out)[-200:]

    sftp = src.open_sftp()
    sftp.get("/tmp/_mig_img.tar.gz", local)
    sftp.close()
    run(src, "rm -f /tmp/_mig_img.tar.gz", 60)

    sftp = dst.open_sftp()
    sftp.put(local, "/tmp/_mig_img.tar.gz")
    sftp.close()
    os.unlink(local)
    os.rmdir(tmpdir)

    rc, out, err = run(dst, "docker load -i /tmp/_mig_img.tar.gz 2>&1 | tail -3; "
                            "rm -f /tmp/_mig_img.tar.gz", 3600)
    return rc == 0, (out or err).strip()[-200:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正传输缺失镜像")
    args = ap.parse_args()

    cfg = load_config()
    pairs = cfg["pairs"]

    for src_name, dst_name in pairs:
        s, d = cfg["hosts"][src_name], cfg["hosts"][dst_name]
        log("=== {} -> {} ===".format(src_name, dst_name))
        try:
            cs = get_conn(s["ip"], s["user"], s["pwd"])
            cd = get_conn(d["ip"], d["user"], d["pwd"])
        except Exception as exc:                       # noqa: BLE001
            log("  连接失败: {}".format(exc))
            continue

        si, di = image_map(cs), image_map(cd)
        missing = {k: v for k, v in si.items() if di.get(k) != v}
        log("  源端镜像 {} 个，目标端 {} 个，需补齐 {} 个".format(len(si), len(di), len(missing)))

        for name, iid in sorted(missing.items()):
            if not args.apply:
                log("  [dry-run] {} ({})".format(name, iid))
                continue
            ok, msg = save_and_transfer(cs, s["pwd"], cd, d["pwd"], name, iid)
            log("  {} {} -> {}".format("OK  " if ok else "FAIL", name, msg))

        if args.apply:
            si2, di2 = image_map(cs), image_map(cd)
            bad = [k for k, v in si2.items() if di2.get(k) != v]
            log("  复核: 仍不一致 {} 个 {}".format(len(bad), bad[:5]))

    close_all()


if __name__ == "__main__":
    main()
