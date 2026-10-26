#!/usr/bin/env python3
"""企业微信扫码登录链路端到端测试（不依赖真实企微）。

做法：
  1. 起一个本地 mock 服务，冒充 qyapi.weixin.qq.com 的三个接口
     （gettoken / user/getuserinfo / user/get / department/simplelist）
  2. 改写被测 server.py 里的 API 基址常量，指向 mock
  3. 跑真实的业务流程：start → callback → exchange → 校验 token 与用户数据
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)          # ai-tools-server/ 包根
SRC = os.path.join(_PKG, "server.py")
MOCK_PORT = 18899
APP_PORT = 18898

MOCK_USER = {
    "errcode": 0, "errmsg": "ok",
    "userid": "zhangsan", "name": "张三",
    "department": [1, 1002], "avatar": "", "email": "zhangsan@example.com",
}

# 开关：置 True 让 /user/get 返回 60020（模拟出口 IP 未加可信 IP 白名单）
MOCK_DEPT_FAIL = [False]


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if p.endswith("/gettoken"):
            self._send({"errcode": 0, "errmsg": "ok", "access_token": "MOCK_TOKEN", "expires_in": 7200})
        elif p.endswith("/auth/getuserinfo"):
            # 扫码授权登录（qrConnect）现行接口
            self._send({"errcode": 0, "errmsg": "ok", "userid": "zhangsan"})
        elif p.endswith("/user/getuserinfo"):
            # 网页授权登录（oauth2 snsapi_base）旧接口
            self._send({"errcode": 0, "errmsg": "ok", "userid": "zhangsan"})
        elif p.endswith("/user/get"):
            if MOCK_DEPT_FAIL[0]:
                # 模拟「出口 IP 未加可信 IP 白名单」
                self._send({"errcode": 60020, "errmsg": "not allow to access from your ip"})
            else:
                self._send(MOCK_USER)
        elif p.endswith("/department/simplelist"):
            self._send({"errcode": 0, "errmsg": "ok",
                        "department": [{"id": 1, "name": "产品技术中心"},
                                       {"id": 1002, "name": "平台研发部"}]})
        else:
            self._send({"errcode": 404, "errmsg": "not mocked: %s" % p})


def free_port(p):
    os.system("lsof -ti tcp:%d | xargs kill -9 2>/dev/null" % p)
    time.sleep(0.6)


def main():
    work = tempfile.mkdtemp(prefix="wecom_test_")
    app = os.path.join(work, "app")
    shutil.copytree(os.path.join(os.path.dirname(SRC)), app)
    dbpath = os.path.join(app, "data", "tools.db")
    if os.path.exists(dbpath):
        os.remove(dbpath)

    # 改写 API 基址 → 指向 mock；并写好企微配置
    sp = os.path.join(app, "server.py")
    src = open(sp, encoding="utf-8").read()
    src = src.replace('WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"',
                      'WECOM_API_BASE = "http://127.0.0.1:%d/cgi-bin"' % MOCK_PORT)
    src = src.replace('WECOM_OAUTH_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"',
                      'WECOM_OAUTH_URL = "http://127.0.0.1:%d/mock/authorize"' % MOCK_PORT)
    src = src.replace('WECOM_QR_CONNECT_URL = WECOM_SSO_BASE + "/qrConnect"',
                      'WECOM_QR_CONNECT_URL = "http://127.0.0.1:%d/mock/qrConnect"' % MOCK_PORT)
    open(sp, "w", encoding="utf-8").write(src)

    cfg = {"corp_id": "ww_mock_corp", "agent_id": "1000002", "secret": "mock_secret",
           "base_url": "http://127.0.0.1:%d" % APP_PORT}
    with open(os.path.join(app, "wecom_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    # 启动 mock
    free_port(MOCK_PORT)
    mock = HTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print("[1] mock 企微 API 已启动 :%d" % MOCK_PORT)

    # 启动被测服务
    free_port(APP_PORT)
    env = dict(os.environ, PORT=str(APP_PORT), DATA_DIR=os.path.join(app, "data"))
    proc = subprocess.Popen([sys.executable, sp], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.0)
    base = "http://127.0.0.1:%d" % APP_PORT
    fails = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + str(extra)) if extra else ""))
        if not cond:
            fails.append(name)

    try:
        print("\n[2] 状态接口")
        r = json.loads(urllib.request.urlopen(base + "/api/login/wecom/status").read())
        check("status.enabled = true", r.get("data", {}).get("enabled") is True, r.get("data"))

        print("\n[3] /api/login/wecom/start → 302 跳企微扫码登录")
        req = urllib.request.Request(base + "/api/login/wecom/start")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedirect)
        try:
            op.open(req)
            check("应返回 302", False, "未跳转")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            check("返回 302", e.code == 302, e.code)
            check("跳转到企微授权域", "appid=ww_mock_corp" in loc, loc[:110])
            check("默认走扫码端点 qrConnect", "/mock/qrConnect" in loc, loc[:110])
            check("不带 snsapi_base（那是工作台内 H5 用的）", "snsapi_base" not in loc)
            qs = parse_qs(urlparse(loc).query)
            state = qs.get("state", [""])[0]
            check("带 agentid", qs.get("agentid", [""])[0] == "1000002")
            check("带 redirect_uri 回调", "/api/login/wecom/callback" in unquote(loc), unquote(loc)[:130])

        print("\n[3b] /api/login/wecom/start?mode=oauth → 网页授权（工作台内 H5）")
        try:
            op.open(base + "/api/login/wecom/start?mode=oauth")
            check("应返回 302", False, "未跳转")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            check("返回 302", e.code == 302, e.code)
            check("走 oauth2 端点", "/mock/authorize" in loc, loc[:110])
            check("scope=snsapi_base", parse_qs(urlparse(loc).query).get("scope", [""])[0] == "snsapi_base")
            check("URL 末尾带 #wechat_redirect", loc.endswith("#wechat_redirect"), loc[-30:])

        print("\n[4] 回调：code 换 userid → 自动建号 → 签发 token")
        # 取出 state
        con = sqlite3.connect(os.path.join(app, "data", "tools.db"))
        con.row_factory = sqlite3.Row
        strow = con.execute("SELECT code FROM wecom_auth LIMIT 1").fetchone()
        check("state 已入库", strow is not None)
        state = strow["code"]
        try:
            op.open(base + "/api/login/wecom/callback?code=MOCKCODE&state=" + state)
            check("回调应 302", False, "未跳转")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            check("回调返回 302", e.code == 302, e.code)
            check("回跳带一次性 code", "wecom_code=" in loc, loc[:120])
            check("回跳地址不含 token", "token=" not in loc)
            handoff = parse_qs(urlparse(loc).query).get("wecom_code", [""])[0]

        print("\n[5] users 表：企微用户已自动建号")
        u = con.execute("SELECT * FROM users WHERE wecom_userid='zhangsan'").fetchone()
        check("已建号", u is not None)
        if u:
            check("姓名为企微姓名", u["name"] == "张三", u["name"])
            check("部门来自通讯录", u["dept"] == "产品技术中心 / 平台研发部", u["dept"])
            check("角色为普通用户", u["role"] == "user", u["role"])
            check("状态 active", u["status"] == "active")

        print("\n[6] exchange：一次性 code 换正式 token")
        body = json.dumps({"code": handoff}).encode()
        req = urllib.request.Request(base + "/api/login/wecom/exchange", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(urllib.request.urlopen(req).read())
        d = r.get("data") or {}
        check("exchange 成功", r.get("ok") is True, r)
        token = d.get("token")
        check("拿到 token", bool(token))
        check("返回用户信息", (d.get("user") or {}).get("name") == "张三", d.get("user"))

        print("\n[7] token 可用 + 一次性 code 已失效")
        req = urllib.request.Request(base + "/api/me", headers={"Authorization": "Bearer " + token})
        me = json.loads(urllib.request.urlopen(req).read())
        check("带 token 可访问 /api/me", (me.get("data") or {}).get("user", {}).get("name") == "张三", me.get("data"))
        body = json.dumps({"code": handoff}).encode()
        req = urllib.request.Request(base + "/api/login/wecom/exchange", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req)
            check("二次使用应失败", False)
        except urllib.error.HTTPError as e:
            rr = json.loads(e.read())
            check("二次使用已失效", rr.get("ok") is False, rr.get("msg"))

        print("\n[8] 二次扫码：同一 userid 不重复建号")
        try:
            op.open(base + "/api/login/wecom/start")
        except urllib.error.HTTPError:
            pass
        st2 = con.execute("SELECT code FROM wecom_auth LIMIT 1").fetchone()["code"]
        try:
            op.open(base + "/api/login/wecom/callback?code=MOCKCODE&state=" + st2)
        except urllib.error.HTTPError:
            pass
        n = con.execute("SELECT COUNT(*) c FROM users WHERE wecom_userid='zhangsan'").fetchone()["c"]
        check("仍只有 1 个企微账号", n == 1, n)
        # 种子自带 admin + guest，企微用户只应新增 1 个（不得每次扫码都建号）
        tot = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        seed = con.execute("SELECT COUNT(*) c FROM users WHERE wecom_userid='' OR wecom_userid IS NULL").fetchone()["c"]
        check("仅新增 1 个账号（未膨胀）", tot - seed == 1, "总=%s 非企微=%s" % (tot, seed))

        print("\n[9] 账密登录未受影响")
        body = json.dumps({"username": "admin", "password": "MhAi@2026"}).encode()
        req = urllib.request.Request(base + "/api/login", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            rr = json.loads(urllib.request.urlopen(req).read())
            check("账密登录接口可用", "data" in rr, rr.get("msg"))
        except urllib.error.HTTPError as e:
            check("账密登录接口可用", e.code in (401, 200), e.code)

        print("\n[10] 通讯录不可用时降级（模拟出口 IP 未加可信 IP 白名单）")
        MOCK_DEPT_FAIL[0] = True
        try:
            op.open(base + "/api/login/wecom/start")
        except urllib.error.HTTPError:
            pass
        st3 = con.execute("SELECT code FROM wecom_auth LIMIT 1").fetchone()["code"]
        try:
            op.open(base + "/api/login/wecom/callback?code=MOCKCODE&state=" + st3)
            check("回调应 302", False, "未跳转")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            check("通讯录失败时仍能登录（降级）", e.code == 302 and "wecom_code=" in loc, loc[:110])
            handoff2 = parse_qs(urlparse(loc).query).get("wecom_code", [""])[0]
        u3 = con.execute("SELECT * FROM users WHERE wecom_userid='zhangsan'").fetchone()
        check("账号未被清空", u3 is not None and u3["name"] == "张三", u3["name"] if u3 else None)
        MOCK_DEPT_FAIL[0] = False

        print("\n[11] 未配置凭据时降级为占位态")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        os.remove(os.path.join(app, "wecom_config.json"))
        env2 = dict(os.environ, PORT=str(APP_PORT), DATA_DIR=os.path.join(app, "data"))
        proc2 = subprocess.Popen([sys.executable, sp], env=env2,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2.0)
        r = json.loads(urllib.request.urlopen(base + "/api/login/wecom/status").read())
        dd = r.get("data") or {}
        check("status.enabled = false", dd.get("enabled") is False, dd)
        check("列出缺哪些配置", bool(dd.get("missing")), dd.get("missing"))
        try:
            op.open(base + "/api/login/wecom/start")
            check("未配置时 start 应拒绝", False)
        except urllib.error.HTTPError as e:
            rr = json.loads(e.read())
            check("未配置时 start 返回 503", e.code == 503, e.code)
            check("给出可读原因", bool(rr.get("msg")), rr.get("msg"))
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except Exception:
            proc2.kill()
        proc = None

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        mock.shutdown()   # mock 在本进程内监听，不能用 free_port（会把自己 kill 掉）
        free_port(APP_PORT)

    print("\n" + "=" * 56)
    if fails:
        print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
