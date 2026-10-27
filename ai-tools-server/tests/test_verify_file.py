#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：企微「可信域名」所有权校验文件（WW_verify_xxx.txt）。

覆盖：
  ① 校验文件能从站点**根目录**取到，路径不带二级目录（企微硬要求）
  ② 内容与文件原样一致（无 BOM / 无换行），Content-Type 是 text/plain
  ③ 放在 data/webroot/ 即刻生效 —— 不用重启服务
  ④ 只放行 WW_verify_*.txt；其他文件、目录穿越一律挡住（不能变成泄露口）
  ⑤ 体检项 verify_file 能分清「503 后端没起来」/「200 但是 HTML」/「正常」
  ⑥ nginx.conf.example 里必须有校验文件的 location，且 80 端口的 301
     写在 location 里（写在 server 层会早于 location 匹配，导致取不到）

跑法：python3 test_verify_file.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
SERVER = os.path.join(PKG, "server.py")

TMP = tempfile.mkdtemp(prefix="tvf_")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SEED_FILE"] = os.path.join(TMP, "no_seed.json")
os.environ["WECOM_CORP_ID"] = "wwtest"
os.environ["WECOM_AGENT_ID"] = "1000196"
os.environ["WECOM_SECRET"] = "dummy"

sys.path.insert(0, PKG)
import server  # noqa: E402

os.makedirs(server.DATA_DIR, exist_ok=True)
server.init_db()

PASS, FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  ✅ %s" % name)
    else:
        FAIL.append(name)
        print("  ❌ %s  %s" % (name, extra))
    return bool(cond)


VF_NAME = "WW_verify_VqlqcYAYJtkoHyot.txt"
VF_VALUE = "VqlqcYAYJtkoHyot"   # 企微给的就是 16 位随机串，无 BOM 无换行

print("① 校验文件放 data/webroot/ 后，根目录能取到")
webroot = server.WEBROOT_DIR
os.makedirs(webroot, exist_ok=True)
with open(os.path.join(webroot, VF_NAME), "wb") as f:
    f.write(VF_VALUE.encode("ascii"))

srv = server.HTTPServer(("127.0.0.1", 0), server.Handler)
port = srv.server_address[1]
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
BASE = "http://127.0.0.1:%d" % port


def fetch(path):
    """返回 (status, body, content_type)；不跟随重定向。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(BASE + path, timeout=8) as r:
            return int(r.status), r.read(), (r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            return int(e.code), e.read(), (e.headers.get("Content-Type") or "")
        except Exception:
            return int(e.code), b"", ""
    except Exception:
        return 0, b"", ""


st, body, ct = fetch("/" + VF_NAME)
check("根目录取校验文件返回 200", st == 200, "实际 %s" % st)
check("内容与文件原样一致（无 BOM/无换行）", body == VF_VALUE.encode("ascii"),
      "实际 %r" % body[:40])
check("Content-Type 是 text/plain（不能是 HTML）",
      ct.lower().startswith("text/plain"), "实际 %s" % ct)

print()
print("② 校验文件必须在根目录，二级目录取不到")
st2, _, _ = fetch("/sub/" + VF_NAME)
check("二级目录取不到（企微硬要求放根目录）", st2 == 404, "实际 %s" % st2)

print()
print("③ webroot 只放行 WW_verify_*.txt，其他文件不泄露")
with open(os.path.join(webroot, "secret.txt"), "w") as f:
    f.write("top secret")
with open(os.path.join(webroot, "notes.md"), "w") as f:
    f.write("internal")
st3, _, _ = fetch("/secret.txt")
check("webroot 里的普通 txt 不被提供", st3 == 404, "实际 %s" % st3)
st4, _, _ = fetch("/notes.md")
check("webroot 里的 md 不被提供", st4 == 404, "实际 %s" % st4)

print()
print("④ 目录穿越防护")
for bad in ("/../wecom_config.json", "/../../etc/passwd",
            "/..%2fwecom_config.json"):
    stb, bodyb, _ = fetch(bad)
    leaked = b"corp_id" in bodyb or b"root:" in bodyb
    check("穿越 %s 不泄露内容" % bad, not leaked, "status=%s" % stb)

print()
print("⑤ 体检项 verify_file 能区分各种情况")
# 体检函数开头会 load_wecom_config() 热重载，所以 base_url 必须用**环境变量**覆盖，
# 直接赋 server.WECOM_BASE_URL 会被配置文件覆盖掉（那是生产行为，不该改）。
os.environ["WECOM_BASE_URL"] = BASE
server.load_wecom_config(force=True)

# 5-a 未放文件
shutil.rmtree(webroot, ignore_errors=True)
os.makedirs(webroot, exist_ok=True)
items = {i["key"]: i for i in server.wecom_diagnose().get("items", [])}
vf = items.get("verify_file")
check("未放校验文件时仍有该体检项", vf is not None)
check("未放文件不算失败（不误报，只是提示）",
      vf and vf.get("ok") is True, "实际 %s" % (vf or {}).get("ok"))
check("提示里说清了放哪", vf and "webroot" in (vf.get("detail") or ""),
      (vf or {}).get("detail", "")[:80])

# 5-b 放了文件且能正常取到 → 通过
with open(os.path.join(webroot, VF_NAME), "wb") as f:
    f.write(VF_VALUE.encode("ascii"))
items = {i["key"]: i for i in server.wecom_diagnose().get("items", [])}
vf = items.get("verify_file")
check("文件可正常访问时判定通过", vf and vf.get("ok") is True,
      "实际 %s / %s" % ((vf or {}).get("ok"), (vf or {}).get("detail", "")[:90]))

# 5-c 取到的是 HTML → 报「必须是纯文本」
html_file = os.path.join(webroot, VF_NAME)
with open(html_file, "wb") as f:
    f.write(b"<!DOCTYPE html><html><body>404</body></html>")
items = {i["key"]: i for i in server.wecom_diagnose().get("items", [])}
vf = items.get("verify_file")
check("内容是 HTML 时判定不通过", vf and vf.get("ok") is False)
check("提示指明必须是纯文本",
      vf and "纯文本" in ((vf.get("action") or "") + (vf.get("detail") or "")),
      ((vf or {}).get("action") or "")[:90])

# 5-d 取不到（连不上）→ 判定不通过
with open(html_file, "wb") as f:
    f.write(VF_VALUE.encode("ascii"))
os.environ["WECOM_BASE_URL"] = "http://127.0.0.1:1"   # 必然连不上
server.load_wecom_config(force=True)
items = {i["key"]: i for i in server.wecom_diagnose().get("items", [])}
vf = items.get("verify_file")
check("连不上时判定不通过", vf and vf.get("ok") is False)

srv.shutdown()
srv.server_close()
shutil.rmtree(TMP, ignore_errors=True)

print()
print("⑥ nginx.conf.example 必须有校验文件的 location，且 301 写在 location 里")
ngx = open(os.path.join(PKG, "deploy", "nginx.conf.example"),
           encoding="utf-8").read()
check("有 WW_verify 的 location", "WW_verify_" in ngx)
check("nginx 直读目录已指定", "ai-tools-webroot" in ngx)
check("指定了 text/plain", "text/plain" in ngx)
# 80 端口 server 块里：return 301 必须出现在 location / 之内
s80 = ngx.split("listen 80;", 1)[1].split("listen 443", 1)[0]
ret_pos = s80.find("return 301")
loc_pos = s80.find("location /")
check("80 端口的 301 写在 location 内（不能放 server 层）",
      ret_pos > loc_pos > 0, "return@%s location@%s" % (ret_pos, loc_pos))

print()
print("⑦ deploy.sh 能放置校验文件并验证公网可达")
dep = open(os.path.join(PKG, "deploy", "deploy.sh"), encoding="utf-8").read()
check("有校验文件目录变量", "WEBROOT_NGINX" in dep)
check("会复制校验文件到 nginx 目录", "WEBROOT_NGINX" in dep and "cp -f" in dep)
check("会放到后端 data/webroot 兜底", "data/webroot" in dep)
check("有公网可达性验证", "校验文件" in dep and "http  取" in dep)
check("操作清单里包含可信域名这一步",
      "可信域名" in dep and "WW_verify" in dep)

print()
print("⑧ Dockerfile / compose 支持 webroot 挂载")
df = open(os.path.join(PKG, "deploy", "docker", "Dockerfile"),
          encoding="utf-8").read()
check("Dockerfile 创建了 webroot 目录", "/app/data/webroot" in df)
comp = open(os.path.join(PKG, "deploy", "docker", "docker-compose.yml"),
            encoding="utf-8").read()
check("compose 说明了 webroot 位置", "webroot" in comp)

print()
print("=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
print("全部通过 ✅")
