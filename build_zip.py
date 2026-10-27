#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包交付包（ai-tools-server.zip / ai-tools-docker.zip）。

为什么不用系统 zip：
    macOS 自带的 zip 会把**中文文件名**按 CP437（IBM437）存进包里，且不设
    UTF-8 标志位（general purpose bit 11）。在 Linux 服务器上解压后，
    文件名会变成「Φû¬Φ╡äΦ«íτ«ùµ£║σÖ¿Σ║║-Σ╜┐τö¿Φ»┤µÿÄ.md」这种乱码，
    工具附件就下载不到了。
    Python 的 zipfile 会正确写 UTF-8 标志位，Linux 的 unzip / python 都能正常还原。

★ 唯一真相原则：
    `ai-tools-server/` 是**唯一源码目录**。docker 包不再单独维护一份拷贝，
    而是在打包时用 `ai-tools-server/` + `ai-tools-server/deploy/docker/` 里
    的三个容器专属文件（Dockerfile / docker-compose.yml / README-Docker.md）
    现场合成。这样 git 里只有一份 server.py，不会出现两份代码漂移。

★ 前端只有一份：
    `ai-tools-server/web/index.html` 是前端的唯一真相。仓库根目录的
    `demo4-卡片风滚动分页.html` 是**自动生成的便捷副本**（给人直接双击打开看的），
    打包/提交前由本脚本同步，不参与版本控制。

用法：
    python3 build_zip.py
"""
import os
import shutil
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))

SRC_DIR = "ai-tools-server"                 # 唯一源码目录
SERVER_ZIP = "ai-tools-server.zip"
DOCKER_ZIP = "ai-tools-docker.zip"
FRONTEND_SRC = os.path.join(SRC_DIR, "web", "index.html")
FRONTEND_COPY = "demo4-卡片风滚动分页.html"  # 自动生成的便捷副本（不进版本库）

# 容器专属文件：从 deploy/docker/ 提到包根
DOCKER_OVERLAY = ["Dockerfile", "docker-compose.yml", "README-Docker.md"]

EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
EXCLUDE_FILES = {".DS_Store", "wecom_config.json", "accounts_preset.json"}
EXCLUDE_SUFFIX = (".pyc", ".pyo")


def collect(root, base):
    """列出 root 下所有要打包的文件（返回绝对路径 + 相对 base 的路径）。

    data/ 目录整体排除（运行时数据 + 上传附件，不该进交付包），
    但保留 data/uploads/.gitkeep 以维持目录结构。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        # data/ 下的运行时内容（tools.db / uploads 附件）不进交付包，
        # 只保留 .gitkeep 以维持目录结构
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == "data" or rel_dir.startswith("data" + os.sep):
            if rel_dir == "data":
                dirnames[:] = [d for d in dirnames if d == "uploads"]
                filenames = [f for f in filenames if f == ".gitkeep"]
            else:
                dirnames[:] = []
                filenames = [f for f in filenames if f == ".gitkeep"]
        for fn in sorted(filenames):
            if fn in EXCLUDE_FILES or fn.endswith(EXCLUDE_SUFFIX):
                continue
            full = os.path.join(dirpath, fn)
            out.append((full, os.path.relpath(full, base)))
    return out


def sync_frontend():
    """把 web/index.html 同步成根目录的便捷副本（并核对内容一致）。"""
    if not os.path.exists(FRONTEND_SRC):
        print("★ 找不到前端源文件：%s" % FRONTEND_SRC)
        return False
    shutil.copyfile(FRONTEND_SRC, os.path.join(BASE, FRONTEND_COPY))
    a = open(FRONTEND_SRC, "rb").read()
    b = open(os.path.join(BASE, FRONTEND_COPY), "rb").read()
    if a != b:
        print("★ 前端副本与源文件不一致")
        return False
    print("前端副本已同步：%s（%d 字节）" % (FRONTEND_COPY, len(a)))
    return True


def build_zip(files, zip_path):
    with zipfile.ZipFile(os.path.join(BASE, zip_path), "w",
                         zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, rel in files:
            # flag_bits 由 zipfile 自动置 UTF-8 位（文件名含非 ASCII 时）
            z.write(full, rel)


def verify(zip_path):
    """校验：① 中文名都带 UTF-8 标志位 ② 实际解压出来的名字与包内一致。"""
    zp = os.path.join(BASE, zip_path)
    bad_flag, tmp = [], tempfile.mkdtemp(prefix="zipchk_")
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            if not (info.flag_bits & 0x800) and any(ord(c) > 127 for c in info.filename):
                bad_flag.append(info.filename)
        z.extractall(tmp)
    mismatch = []
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            p = os.path.join(tmp, info.filename)
            if not os.path.isfile(p):
                mismatch.append("缺失: " + info.filename)
    shutil.rmtree(tmp, ignore_errors=True)
    return bad_flag, mismatch


def verify_dockerfile_copies(zip_path):
    """校验 Dockerfile 里每条 COPY 的源文件/目录是否真的在包里。

    ★ 为什么必须查：
        `wecom_config.json` / `accounts_preset.json` 含密钥和密码，被 EXCLUDE_FILES
        排除在包外；而 Dockerfile 若写了 `COPY wecom_config.json /app/`，
        `docker build` 会直接失败（COPY 源不存在）—— 运维拿到包才发现，一来一回
        就是一天。曾在 2026-10-27 真实踩到，已改为只 COPY .example 模板。

    规则：源是文件 → 包里必须有同名条目；源是目录（以 / 结尾）→ 包里至少有
    一个条目以它为前缀。
    """
    zp = os.path.join(BASE, zip_path)
    missing = []
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        try:
            raw = z.read("Dockerfile").decode("utf-8")
        except KeyError:
            return ["包里没有 Dockerfile，无法校验 COPY"]
    # 合并续行、去掉注释行
    lines, buf = [], ""
    for ln in raw.splitlines():
        s = ln.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        buf += s
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    for ln in lines:
        if not ln.upper().startswith("COPY"):
            continue
        parts = ln.split()[1:]
        # 去掉 --from= / --chown= 这类 flag
        parts = [p for p in parts if not p.startswith("--")]
        if len(parts) < 2:
            continue
        srcs = parts[:-1]          # 最后一个是目标
        for src in srcs:
            if src.startswith("--from="):
                continue
            s = src.rstrip("/")
            hit = any(n == s or n == s + "/" or n.startswith(s + "/") for n in names)
            if not hit:
                missing.append("Dockerfile COPY 的源不在包里：%s" % src)
    return missing


def build_docker_package():
    """用 server 包 + 容器专属文件现场合成 docker 包内容（不落盘中间目录）。"""
    items = collect(os.path.join(BASE, SRC_DIR), BASE)
    # 包内路径去掉 "ai-tools-server/" 前缀 → 直接铺在包根
    docker_items = []
    for full, rel in items:
        r = os.path.relpath(rel, SRC_DIR)
        # deploy/docker/ 里的三个文件会被提到包根，源目录不再重复带
        if r.startswith("deploy" + os.sep + "docker" + os.sep):
            continue
        docker_items.append((full, r))
    for fn in DOCKER_OVERLAY:
        src = os.path.join(BASE, SRC_DIR, "deploy", "docker", fn)
        if not os.path.exists(src):
            print("★ 缺少容器文件：%s" % src)
            return None
        docker_items.append((src, fn))
    return docker_items


def report(zip_path, count, check_dockerfile=False):
    size = os.path.getsize(os.path.join(BASE, zip_path))
    bad_flag, mismatch = verify(zip_path)
    print("%-24s %5d 个文件  %6.1f KB" % (zip_path, count, size / 1024.0))
    ok = True
    if bad_flag:
        ok = False
        print("   ★ 未标 UTF-8 的中文条目：", bad_flag)
    if mismatch:
        ok = False
        print("   ★ 解压后文件缺失：", mismatch)
    if check_dockerfile:
        miss = verify_dockerfile_copies(zip_path)
        if miss:
            ok = False
            for m in miss:
                print("   ★ " + m)
        else:
            print("   Dockerfile 的 COPY 源全部在包内")
    # 抽一个中文名做实际读取校验
    with zipfile.ZipFile(os.path.join(BASE, zip_path)) as z:
        cn = [n for n in z.namelist() if "/" in n and any(ord(c) > 127 for c in n)]
        if cn:
            with zipfile.ZipFile(os.path.join(BASE, zip_path)) as z2:
                data = z2.read(cn[0])
            print("   中文名条目校验通过：%s（%d 字节）" % (cn[0].split("/")[-1], len(data)))
    return ok


ok = True
if not sync_frontend():
    ok = False

# ---- server 包 ----
server_files = collect(os.path.join(BASE, SRC_DIR), BASE)
build_zip(server_files, SERVER_ZIP)
if not report(SERVER_ZIP, len(server_files)):
    ok = False

# ---- docker 包（从 server 包现场合成）----
docker_files = build_docker_package()
if docker_files is None:
    ok = False
else:
    build_zip(docker_files, DOCKER_ZIP)
    if not report(DOCKER_ZIP, len(docker_files), check_dockerfile=True):
        ok = False

print("\n%s" % ("全部通过" if ok else "★ 有问题"))
sys.exit(0 if ok else 1)
