#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：企微扫码后按 admin_userids 自动授予/升级管理员角色。

覆盖：
  ① 白名单内的 userid 首次扫码 → role=admin
  ② 白名单外的 userid 首次扫码 → role=user
  ③ 先以普通用户建号、后被加进白名单 → 再扫码自动升级为 admin
  ④ 登录返回体里的 role 取库里真实值（不是写死的 user）
  ⑤ diagnose 能报出管理员白名单状态

跑法：python3 test_admin_whitelist.py
"""
import os
import sys
import json
import sqlite3
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)            # ai-tools-server/ 包根
SERVER = os.path.join(PKG, "server.py")

TMP = tempfile.mkdtemp(prefix="adm_wl_")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["SEED_FILE"] = os.path.join(TMP, "no_seed.json")   # 不导入种子
os.environ["WECOM_ADMIN_USERIDS"] = "zhangsan,lisi"
os.environ["WECOM_CORP_ID"] = "wwtest"
os.environ["WECOM_AGENT_ID"] = "1000196"
os.environ["WECOM_SECRET"] = "dummy"
os.environ["WECOM_BASE_URL"] = "https://aitoolbox.maihaoche.com"

sys.path.insert(0, PKG)
import server  # noqa: E402

os.makedirs(server.DATA_DIR, exist_ok=True)
server.init_db()   # 建表（正常启动时由 main 调用）
server.load_wecom_config(force=True)   # 读环境变量里的凭据与管理员白名单

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [NG] ") + name + (("  " + str(extra)) if extra else ""))


def reset_users():
    conn = server.db()
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM sessions")
    conn.commit()
    return conn


def fake_login(conn, userid, name="测试人", dept="产品技术中心"):
    """打桩企微接口，只保留建号/发 token 的核心逻辑。"""
    server.wecom_code_to_userid = lambda code: (userid, {"name": name}, "")
    server.wecom_get_user = lambda uid: ({"name": name, "department": [1]}, "")
    server.wecom_dept_names = lambda: ({}, "")
    return server.wecom_login_user(conn, "fake_code_" + userid)


print("\n[1] 白名单外的 userid → 普通用户")
conn = reset_users()
res, err = fake_login(conn, "wangwu")
check("登录成功", not err, err)
check("role 为 user", res and res["user"]["role"] == "user", res and res["user"])

print("\n[2] 白名单内的 userid 首次扫码 → 直接是管理员")
conn = reset_users()
res, err = fake_login(conn, "zhangsan")
check("登录成功", not err, err)
check("role 为 admin", res and res["user"]["role"] == "admin", res and res["user"])
row = conn.execute("SELECT role FROM users WHERE wecom_userid='zhangsan'").fetchone()
check("数据库里的 role 也是 admin", row and row["role"] == "admin", row and row["role"])

print("\n[3] 先普通建号、后来加进白名单 → 再扫码自动升级")
conn = reset_users()
_saved_admins = list(server.WECOM_ADMIN_USERIDS)
server.WECOM_ADMIN_USERIDS = []          # 模拟「当时还没配白名单」
res, err = fake_login(conn, "zhaoliu")
check("首次扫码是普通用户", res and res["user"]["role"] == "user", res and res["user"])
server.WECOM_ADMIN_USERIDS = ["zhaoliu"]  # 之后配上了白名单
res, err = fake_login(conn, "zhaoliu")
check("再次扫码自动升级为 admin", res and res["user"]["role"] == "admin", res and res["user"])
row = conn.execute("SELECT role FROM users WHERE wecom_userid='zhaoliu'").fetchone()
check("数据库 role 已更新", row and row["role"] == "admin", row and row["role"])
server.WECOM_ADMIN_USERIDS = _saved_admins  # 还原，别影响后面的用例

print("\n[4] 登录返回体的 role 取真实值")
conn = reset_users()
res, _ = fake_login(conn, "lisi")
check("返回 role=admin（不是写死的 user）", res["user"]["role"] == "admin", res["user"])

print("\n[5] diagnose 报出管理员白名单")
d = server.wecom_diagnose()
items = {i["key"]: i for i in d.get("items", [])}
wl = items.get("admin_whitelist")
check("diagnose 含管理员白名单项", wl is not None)
check("白名单项为通过", wl and wl["ok"] is True, wl and wl.get("detail"))
check("列出了白名单里的 userid", wl and "lisi" in wl.get("detail", ""), wl and wl.get("detail"))

print("\n[6] 未配白名单时给出可操作提示")
os.environ["WECOM_ADMIN_USERIDS"] = ""      # 模拟「还没配」
server.load_wecom_config(force=True)
d = server.wecom_diagnose()
items = {i["key"]: i for i in d.get("items", [])}
wl = items.get("admin_whitelist")
check("白名单项为未通过", wl and wl["ok"] is False, wl and wl.get("detail"))
check("提示里指明配置文件字段", wl and "admin_userids" in (wl.get("action") or ""), wl and wl.get("action"))

print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
