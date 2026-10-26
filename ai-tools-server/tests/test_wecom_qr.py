#!/usr/bin/env python3
"""企业微信「内嵌二维码」扫码登录链路端到端测试（不依赖真实企微）。

与 test_wecom_login.py 的区别：那条测的是「整页跳转 + 回调」模式，
这条测的是新版「登录框内嵌二维码」模式：

    /api/login/wecom/qr          → 申请二维码会话（拿 key + qrUrl）
    /api/login/wecom/qrcode?sid= → 代理二维码图片（二进制）
    /api/login/wecom/scan?sid=   → 长轮询扫码状态，成功后直接返回本地 token

做法同 test_wecom_login.py：起本地 mock 冒充企微，改写被测 server.py 里的
端点常量指向 mock，再跑真实业务流程。
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
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)          # ai-tools-server/ 包根
SRC = os.path.join(_PKG, "server.py")
MOCK_PORT = 18897
APP_PORT = 18896

PASS = [0]
FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1
        print("  ✅ %s" % name)
    else:
        FAIL[0] += 1
        print("  ❌ %s %s" % (name, extra))
    return bool(cond)


class MockHandler(BaseHTTPRequestHandler):
    """模拟 open.work.weixin.qq.com（扫码页/轮询）+ qyapi（身份接口）。"""

    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json"):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, txt, ctype="text/html; charset=utf-8"):
        raw = txt.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)

        # ── 企微扫码页：window.settings 里带 qrUrl 与 key ──
        if p.endswith("/mock/qrConnect"):
            state = (q.get("state") or [""])[0]
            # qrUrl 指向 mock 自己的图片接口
            qr = "http://127.0.0.1:%d/mock/qrcode.png?key=MOCK_KEY" % MOCK_PORT
            self._text(
                '<html><head><title>企业微信登录</title></head><body>'
                '<script>window.settings = {"qrUrl":"%s","key":"MOCK_KEY",'
                '"redirect_uri":"%s","appid":"ww_mock_corp","state":"%s",'
                '"code_type":2};</script></body></html>'
                % (qr, "http://127.0.0.1:%d/api/login/wecom/callback" % APP_PORT, state)
            )
            return

        # ── 二维码图片（一张 1x1 PNG）──
        if p.endswith("/mock/qrcode.png"):
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
                "00000049454e44ae426082")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
            return

        # ── 长轮询：第一次返回 SUCC（模拟已扫码并确认）──
        if p.endswith("/l/qrConnect"):
            last = (q.get("lastStatus") or [""])[0]
            if last == "QRCODE_SCAN_SUCC":
                self._text('jsonpCallback({"status":"QRCODE_SCAN_SUCC","auth_code":""})')
            else:
                self._text('jsonpCallback({"status":"QRCODE_SCAN_SUCC","auth_code":"MOCK_AUTH_CODE"})')
            return

        # ── qyapi 身份接口 ──
        if p.endswith("/gettoken"):
            self._send({"errcode": 0, "errmsg": "ok", "access_token": "MOCK_TOKEN", "expires_in": 7200})
        elif p.endswith("/auth/getuserinfo"):
            self._send({"errcode": 0, "errmsg": "ok", "userid": "zhangsan"})
        elif p.endswith("/user/get"):
            self._send({"errcode": 0, "errmsg": "ok", "userid": "zhangsan", "name": "张三",
                        "department": [1, 1002], "avatar": "", "email": "zhangsan@example.com"})
        elif p.endswith("/department/simplelist"):
            self._send({"errcode": 0, "errmsg": "ok",
                        "department": [{"id": 1, "name": "产品技术中心"},
                                       {"id": 1002, "name": "平台研发部"}]})
        else:
            self._send({"errcode": 404, "errmsg": "not mocked: %s" % p})


def free_port(p):
    os.system("lsof -ti tcp:%d | xargs kill -9 2>/dev/null" % p)
    time.sleep(0.6)


def api(base, path, timeout=30):
    """调用被测服务接口，返回 (http_code, json)。"""
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def main():
    work = tempfile.mkdtemp(prefix="wecom_qr_test_")
    app = os.path.join(work, "app")
    shutil.copytree(os.path.join(os.path.dirname(SRC)), app)
    dbpath = os.path.join(app, "data", "tools.db")
    if os.path.exists(dbpath):
        os.remove(dbpath)

    sp = os.path.join(app, "server.py")
    src = open(sp, encoding="utf-8").read()
    src = src.replace('WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"',
                      'WECOM_API_BASE = "http://127.0.0.1:%d/cgi-bin"' % MOCK_PORT)
    src = src.replace('WECOM_QR_PAGE_URL = WECOM_SSO_BASE + "/qrConnect"',
                      'WECOM_QR_PAGE_URL = "http://127.0.0.1:%d/mock/qrConnect"' % MOCK_PORT)
    src = src.replace('WECOM_QR_POLL_URL = WECOM_SSO_BASE + "/l/qrConnect"',
                      'WECOM_QR_POLL_URL = "http://127.0.0.1:%d/mock/l/qrConnect"' % MOCK_PORT)
    src = src.replace('WECOM_QR_CONNECT_URL = WECOM_SSO_BASE + "/qrConnect"',
                      'WECOM_QR_CONNECT_URL = "http://127.0.0.1:%d/mock/qrConnect"' % MOCK_PORT)
    open(sp, "w", encoding="utf-8").write(src)

    cfg = {"corp_id": "ww_mock_corp", "agent_id": "1000002", "secret": "mock_secret",
           "base_url": "http://127.0.0.1:%d" % APP_PORT}
    with open(os.path.join(app, "wecom_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)

    free_port(MOCK_PORT)
    free_port(APP_PORT)
    mock = HTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print("[1] mock 企微已启动 :%d" % MOCK_PORT)

    env = dict(os.environ, PORT=str(APP_PORT), HOST="127.0.0.1")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=app, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % APP_PORT
    for _ in range(60):
        try:
            c, _j = api(base, "/api/health", timeout=3)
            if c == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        print("❌ 被测服务启动失败")
        try:
            proc.terminate()
            mock.shutdown()
        except Exception:
            pass
        return 1
    print("[2] 被测服务已启动 :%d" % APP_PORT)

    try:
        print("\n[3] GET /api/login/wecom/qr → 申请二维码会话")
        c, j = api(base, "/api/login/wecom/qr")
        check("返回 200", c == 200, c)
        check("ok=true", j.get("ok") is True, j)
        d = j.get("data") or {}
        sid = d.get("sid") or ""
        img = d.get("img") or ""
        check("含 sid", bool(sid), d)
        check("含 img 地址", "/api/login/wecom/qrcode?sid=" in img, img)
        check("含 poll 地址", "/api/login/wecom/scan?sid=" in (d.get("poll") or ""), d.get("poll"))

        print("\n[4] GET 二维码图片代理")
        try:
            req = urllib.request.Request(base + img)
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
                ct = r.headers.get("Content-Type") or ""
            check("图片返回 200", True)
            check("Content-Type 是图片", ct.startswith("image/"), ct)
            check("是 PNG 二进制", body[:4] == b"\x89PNG", body[:8])
        except Exception as e:
            check("图片代理可访问", False, str(e))

        print("\n[5] GET /api/login/wecom/scan → 轮询到扫码成功")
        c, j = api(base, "/api/login/wecom/scan?sid=" + sid)
        check("返回 200", c == 200, c)
        d = j.get("data") or {}
        check("status=QRCODE_SCAN_SUCC", d.get("status") == "QRCODE_SCAN_SUCC", d)
        token = d.get("token") or ""
        check("返回本地 token", bool(token), d)
        user = d.get("user") or {}
        check("返回用户信息", bool(user.get("userid") or user.get("name")), user)
        check("姓名为通讯录姓名", user.get("name") == "张三", user)
        check("部门已同步", "产品技术中心" in (user.get("dept") or ""), user)

        print("\n[6] token 可用（调 /api/me）")
        try:
            req = urllib.request.Request(base + "/api/me", headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=15) as r:
                me = json.loads(r.read().decode("utf-8"))
            check("token 鉴权通过", me.get("ok") is True, me)
            me_user = (me.get("data") or {}).get("user") or {}
            check("me 是 zhangsan", me_user.get("name") == "张三", me_user)
            check("me 含部门", "产品技术中心" in (me_user.get("dept") or ""), me_user)
        except Exception as e:
            check("token 鉴权通过", False, str(e))

        print("\n[7] 自动建号落库")
        conn = sqlite3.connect(dbpath)
        row = conn.execute("SELECT username,name,dept,wecom_userid FROM users WHERE wecom_userid='zhangsan'").fetchone()
        check("已自动建号", row is not None, row)
        if row:
            check("username 以 wecom_ 开头", row[0].startswith("wecom_"), row[0])
            check("姓名为张三", row[1] == "张三", row[1])
            check("部门已写入", "产品技术中心" in (row[2] or ""), row[2])
        conn.close()

        print("\n[8] 会话一次性：成功后同 sid 再扫应失效")
        c, j = api(base, "/api/login/wecom/scan?sid=" + sid)
        check("返回非 200 或提示失效", c != 200 or (j.get("ok") is False), (c, j))

        print("\n[9] 非法 sid")
        c, j = api(base, "/api/login/wecom/scan?sid=NOT_EXIST")
        check("返回 404 或 ok=false", c == 404 or j.get("ok") is False, (c, j))
        c, j = api(base, "/api/login/wecom/qrcode?sid=NOT_EXIST")
        check("图片代理也返回 404", c == 404, (c, j))

        print("\n[10] 未配置企微时降级")
        with open(os.path.join(app, "wecom_config.json"), "w", encoding="utf-8") as f:
            json.dump({"corp_id": "", "agent_id": "", "secret": "", "base_url": ""}, f)
        # 配置只在启动时读，这里只验证 status 接口的判定逻辑（不重启）
        proc.terminate()
        proc.wait(timeout=10)
        env2 = dict(os.environ, PORT=str(APP_PORT), HOST="127.0.0.1")
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=app, env=env2,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(40):
            try:
                c, _j = api(base, "/api/health", timeout=3)
                if c == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        c, j = api(base, "/api/login/wecom/qr")
        check("返回 503", c == 503, (c, j))
        check("提示未配置", "未配置" in (j.get("msg") or ""), j)
        c, j = api(base, "/api/login/wecom/status")
        check("status enabled=false", (j.get("data") or {}).get("enabled") is False, j)

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            mock.shutdown()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)

    print("\n════════ 通过 %d 项，失败 %d 项 ════════" % (PASS[0], FAIL[0]))
    return 0 if FAIL[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
