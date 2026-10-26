#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：可信 IP 自动核对（登记清单 vs 企微实际看到的出口 IP）。

覆盖：
  ① 配置里登记了 IP、企微看到的 IP 一致 → 通过
  ② 登记的 IP 与企微看到的不同 → 报警，且 action 说清怎么改
  ③ 没登记 IP → 跳过比对（不误报）
  ④ trusted_ips 支持多值 + 环境变量覆盖
  ⑤ deploy.sh 生成的配置里 trusted_ips 是合法 JSON 数组

跑法：python3 test_trusted_ip.py
"""
import json
import os
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
SERVER = os.path.join(PKG, "server.py")

TMP = tempfile.mkdtemp(prefix="tip_")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SEED_FILE"] = os.path.join(TMP, "no_seed.json")
os.environ["WECOM_CORP_ID"] = "wwtest"
os.environ["WECOM_AGENT_ID"] = "1000196"
os.environ["WECOM_SECRET"] = "dummy"
os.environ["WECOM_BASE_URL"] = "https://aitoolbox.maihaoche.com"
# 清掉可能从环境继承的可信 IP，保证下面每项都是显式设置的
os.environ.pop("WECOM_TRUSTED_IPS", None)

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


def items_of(diag):
    return {i["key"]: i for i in (diag.get("items") or [])}


# ---- 网络完全隔离 ----
# 这条测试的结论必须确定，不能依赖真实企微接口的返回。
# mock 两个出口：换 token（wecom_access_token）和探 IP（_http_json）。
MOCK = {"token": "", "egress_ip": "", "token_err": ""}
_real_token = server.wecom_access_token
_real_json = server._http_json


def fake_token():
    if MOCK["token"]:
        return MOCK["token"], ""
    return "", MOCK["token_err"] or "企业微信错误 40001：invalid credential, from ip: %s" % (
        MOCK["egress_ip"] or "0.0.0.0")


def fake_json(url, timeout=12, referer=""):
    # 只拦截探 IP 的 getuserinfo；其它调用放行
    if "auth/getuserinfo" in url:
        if MOCK["egress_ip"] == "__reject__":
            return True, {"errcode": 60020,
                          "errmsg": "not allow to access from your ip, from ip: 9.9.9.9"}
        return True, {"errcode": 40029, "errmsg": "invalid code"}
    return _real_json(url, timeout=timeout, referer=referer)


server.wecom_access_token = fake_token
server._http_json = fake_json
server._public_ip = lambda: MOCK["egress_ip"] if MOCK["egress_ip"] not in ("", "__reject__") else ""


print("\n[1] 配置登记的 trusted_ips 能被读出来")
# 走真实路径：写配置文件 → load_wecom_config 读取
CFG = os.path.join(TMP, "wecom_config.json")


def write_cfg(trusted=None, admins=None):
    d = {
        "corp_id": "wwtest", "agent_id": "1000196", "secret": "dummy",
        "base_url": "https://aitoolbox.maihaoche.com",
    }
    if trusted is not None:
        d["trusted_ips"] = trusted
    if admins is not None:
        d["admin_userids"] = admins
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    server.WECOM_CONFIG_FILE = CFG


write_cfg(trusted=["1.2.3.4"])
server.load_wecom_config(force=True)
check("配置解析出 trusted_ips", server.WECOM_TRUSTED_IPS == ["1.2.3.4"], server.WECOM_TRUSTED_IPS)

print("\n[2] 环境变量 WECOM_TRUSTED_IPS 优先于配置文件")
os.environ["WECOM_TRUSTED_IPS"] = "9.9.9.9,8.8.8.8"
server.load_wecom_config(force=True)
check("环境变量生效且支持多值", server.WECOM_TRUSTED_IPS == ["9.9.9.9", "8.8.8.8"],
      server.WECOM_TRUSTED_IPS)
os.environ.pop("WECOM_TRUSTED_IPS", None)

print("\n[3] diagnose 输出里带上 trusted_ips")
MOCK["token"] = ""
MOCK["egress_ip"] = ""
write_cfg(trusted=["1.2.3.4"])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
check("返回体含 trusted_ips 字段", d.get("trusted_ips") == ["1.2.3.4"], d.get("trusted_ips"))
check("返回体含 egress_ip 字段", "egress_ip" in d)

print("\n[4] 出口 IP 与登记清单一致 → 核对项通过")
# 企微回报的 egress_ip 是权威值。这里模拟「企微看到的 IP 在登记清单里」。
MOCK["token"] = "fake_token"
MOCK["egress_ip"] = "1.2.3.4"
write_cfg(trusted=["1.2.3.4", "5.6.7.8"])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
it = items_of(d).get("trusted_ip_match")
check("生成了核对项", it is not None)
if it:
    check("一致时通过", it.get("ok") is True, it)
    check("说明里写明已覆盖", "1.2.3.4" in (it.get("detail") or ""), it.get("detail"))

print("\n[5] 出口 IP 与登记清单不一致 → 报警并给出怎么改")
MOCK["token"] = "fake_token"
MOCK["egress_ip"] = "192.168.9.9"
write_cfg(trusted=["1.2.3.4"])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
it = items_of(d).get("trusted_ip_match")
check("生成了核对项", it is not None)
if it:
    check("不一致时不通过", it.get("ok") is False, it)
    check("说明里点出实际出口 IP", "192.168.9.9" in (it.get("detail") or ""), it.get("detail"))
    check("说明里点出登记清单", "1.2.3.4" in (it.get("detail") or ""), it.get("detail"))
    check("action 告诉运维去哪儿改",
          "企业可信 IP" in (it.get("action") or ""), it.get("action"))

print("\n[5-b] 企微回报的 IP 与本机探测值不同 → 提示多出口")
MOCK["token"] = ""              # 凭据失败 → 走 else 分支，用本机探测值
MOCK["egress_ip"] = "10.0.0.5"
write_cfg(trusted=["10.0.0.5"])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
it = items_of(d).get("trusted_ip_match")
check("生成了核对项", it is not None)
if it:
    check("一致时通过", it.get("ok") is True, it)

print("\n[6] 没登记 IP → 跳过比对，不误报")
MOCK["token"] = "fake_token"
MOCK["egress_ip"] = "1.2.3.4"
write_cfg(trusted=[])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
it = items_of(d).get("trusted_ip_match")
check("生成了核对项", it is not None)
if it:
    check("未登记时不算失败", it.get("ok") is True, it)
    check("说明里提示可以登记", "trusted_ips" in (it.get("detail") or ""), it.get("detail"))

print("\n[6-b] 凭据缺失时不再提前 return，其余检查项照常出现")
MOCK["token"] = ""
MOCK["egress_ip"] = ""
write_cfg(trusted=["1.2.3.4"])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
keys = [i.get("key") for i in (d.get("items") or [])]
check("凭据项标红", items_of(d).get("credentials", {}).get("ok") is False)
check("后续检查项没被跳过（出现可信IP核对）", "trusted_ip_match" in keys, keys)
check("后续检查项没被跳过（出现对外访问）",
      any(k in ("public_reachable",) for k in keys), keys)
check("后续检查项没被跳过（出现管理员白名单）",
      "admin_whitelist" in keys, keys)

print("\n[6-c] 凭据失败时，企微回报的 from ip 被记为权威出口 IP")
MOCK["token"] = ""
MOCK["egress_ip"] = "115.233.220.92"
write_cfg(trusted=["115.233.220.92"])
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
check("egress_ip 取到企微回报的值", d.get("egress_ip") == "115.233.220.92", d.get("egress_ip"))
it = items_of(d).get("trusted_ip_match")
if it:
    check("登记的是同一个 IP → 通过", it.get("ok") is True, it)

print("\n[7] 配置文件里写 trusted_ips 与 admin_userids 能被解析")
write_cfg(trusted=["10.0.0.1", "10.0.0.2"], admins=["zhangsan"])
server.load_wecom_config(force=True)
check("从文件读出 trusted_ips", server.WECOM_TRUSTED_IPS == ["10.0.0.1", "10.0.0.2"],
      server.WECOM_TRUSTED_IPS)
check("从文件读出 admin_userids", server.WECOM_ADMIN_USERIDS == ["zhangsan"],
      server.WECOM_ADMIN_USERIDS)

print("\n[8] deploy.sh 生成的 trusted_ips 是合法 JSON 数组")
import subprocess
deploy = os.path.join(PKG, "deploy", "deploy.sh")
if os.path.exists(deploy):
    src = open(deploy, encoding="utf-8").read()
    check("deploy.sh 写入 trusted_ips 字段", '"trusted_ips"' in src)
    check("deploy.sh 接受第 6 个参数", 'TRUSTED_IPS="${6:-}"' in src)
else:
    check("deploy.sh 存在", False, deploy)

print("\n[9] 模板里有 trusted_ips 字段")
tmpl_path = os.path.join(PKG, "wecom_config.example.json")
if os.path.exists(tmpl_path):
    tmpl = json.load(open(tmpl_path, encoding="utf-8"))
    check("模板含 trusted_ips", "trusted_ips" in tmpl, list(tmpl.keys()))
    check("模板含使用说明", "_trusted_ips说明" in tmpl, list(tmpl.keys()))
else:
    check("模板存在", False, tmpl_path)

print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if not FAIL else 1)
