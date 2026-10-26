import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote, urlencode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data")))
WEB_DIR = os.path.abspath(os.environ.get("WEB_DIR", os.path.join(BASE_DIR, "web")))
SEED_FILE = os.path.abspath(
    os.environ.get("SEED_FILE", os.path.join(BASE_DIR, "seed", "seed_tools.json"))
)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
# 随包分发的真实工具原件（tools/xxx.zip）。与用户上传的附件分开存放，
# 目录只读、不会随运行数据变动，交付包里天然可复现。
TOOLS_DIR = os.path.abspath(os.environ.get("TOOLS_DIR", os.path.join(BASE_DIR, "tools")))
DB_PATH = os.path.join(DATA_DIR, "tools.db")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "").strip()

VALID_TYPES = ["智能体", "内容生成", "效率工具", "数据自动化", "其他"]
START_TIME = time.time()

def log(*a):
    print("[%s]" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)

_conn = None

def db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=15)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=8000")
    return _conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  dept       TEXT DEFAULT '',
  type       TEXT DEFAULT '其他',
  desc       TEXT DEFAULT '',
  detail     TEXT DEFAULT '{}',
  link       TEXT,
  files      TEXT DEFAULT '[]',
  added_at   TEXT DEFAULT '',
  is_new     INTEGER DEFAULT 0,
  downloads  INTEGER DEFAULT 0,
  likes      INTEGER DEFAULT 0,
  favorites  INTEGER DEFAULT 0,
  created_by INTEGER DEFAULT NULL,
  deleted    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tools_type ON tools(type);
CREATE INDEX IF NOT EXISTS idx_tools_added ON tools(added_at DESC);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL UNIQUE,
  name       TEXT DEFAULT '',
  dept       TEXT DEFAULT '',
  pwd_hash   TEXT NOT NULL,
  salt       TEXT NOT NULL,
  role       TEXT DEFAULT 'user',
  status     TEXT DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
  user_id    INTEGER NOT NULL,
  tool_id    INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, tool_id)
);
CREATE TABLE IF NOT EXISTS wecom_auth (
  code       TEXT PRIMARY KEY,
  userid     TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_user_id INTEGER,
  action        TEXT NOT NULL,
  target        TEXT DEFAULT '',
  before        TEXT DEFAULT '',
  after         TEXT DEFAULT '',
  created_at    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tool_id    INTEGER NOT NULL,
  user_id    INTEGER NOT NULL,
  content    TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  to_user_id  INTEGER NOT NULL,
  from_user_id INTEGER DEFAULT NULL,
  title       TEXT DEFAULT '',
  body        TEXT DEFAULT '',
  tool_id     INTEGER DEFAULT NULL,
  is_read     INTEGER DEFAULT 0,
  created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_comments_tool ON comments(tool_id);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_user_id, is_read);
"""

SESSION_TTL_DAYS = 7
COOKIE_NAME = "ai_tools_session"

# ── 企业微信扫码登录配置（可全部留空 = 不启用，前端自动降级为占位态）────────
# 读取优先级：环境变量 > wecom_config.json（同目录）> 空
#   WECOM_CORP_ID    企业微信 CorpID
#   WECOM_AGENT_ID   自建应用 AgentId（扫码登录用）
#   WECOM_SECRET     自建应用 Secret（换取 access_token / 查通讯录用）
#   WECOM_BASE_URL   本服务的对外访问地址，如 https://aitoolbox.maihaoche.com
#   WECOM_REDIRECT   企微后台「授权回调域」对应的地址（扫码登录时上报给企微）。
#                    留空则自动取 {WECOM_BASE_URL}/api/login/wecom/callback。
#
# ★ 为什么 base_url 与 redirect 要分开：
#   企微校验的是「授权回调域」白名单里的**域名**，与本服务实际从哪个地址对外
#   无关。内嵌扫码模式下企微只校验域名、不校验连通性（code 由服务端长轮询
#   拿，不靠浏览器跳转），所以本机联调可以把 base_url 指向隧道、
#   redirect 指向正式域名（aitoolbox.maihaoche.com）——正式域名在企微后台
#   登记一次即可，本机/线上/换隧道都不用再改后台。
WECOM_CONFIG_FILE = os.path.join(BASE_DIR, "wecom_config.json")
WECOM_CORP_ID = ""
WECOM_AGENT_ID = ""
WECOM_SECRET = ""
WECOM_BASE_URL = ""
WECOM_REDIRECT = ""
# 管理员白名单：列在其中的企微 userid，扫码登录后自动拥有 admin 角色。
# 为什么需要它：管理后台的「用户管理」本身只有管理员能进，
# 而新扫码的人默认是普通用户 —— 没有这个白名单就出现「要进后台才能给自己升权」
# 的死循环。填自己的 userid 即可（userid 可从企微管理后台「成员详情」看到，
# 或用 diagnose 接口返回的出口信息配合 auth/getuserinfo 探测）。
WECOM_ADMIN_USERIDS = []
# 已登记到企微后台「企业可信 IP」的地址列表（可多个）。
# 体检时用它跟「企微实际看到的本机出口 IP」自动比对：不一致立刻报警，
# 免得部署完还得人工去两处 IP 之间来回对照。
WECOM_TRUSTED_IPS = []
# 配置热重载用的状态：记录 wecom_config.json 的 mtime 与上次载入的凭据指纹
_wecom_cfg_mtime = None
_wecom_cfg_fingerprint = ()
_wecom_cfg_env_fp = ()


def _parse_wecom_whitelists(cfg):
    """解析两个白名单字段：管理员 userid 清单、已登记的可信 IP 清单。

    环境变量优先，其次配置文件。两者都支持字符串（逗号/空格/分号分隔）或数组。

    ★ 这个函数必须在 load_wecom_config 的**早退分支之前**也调用一次：
      白名单常常只写在配置文件里，而环境变量只给凭据——
      早退时不解析，白名单就会一直是上一次的旧值（这坑踩过一次）。
    """
    global WECOM_ADMIN_USERIDS, WECOM_TRUSTED_IPS

    def pick(env_name, cfg_key):
        raw = os.environ.get(env_name)
        if raw is None or not str(raw).strip():
            raw = cfg.get(cfg_key) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,\s;]+", raw) if x]
        return [str(x).strip() for x in raw if str(x).strip()]

    WECOM_ADMIN_USERIDS = pick("WECOM_ADMIN_USERIDS", "admin_userids")
    WECOM_TRUSTED_IPS = pick("WECOM_TRUSTED_IPS", "trusted_ips")


def load_wecom_config(force=False):
    """载入企业微信配置：环境变量优先，其次同目录 wecom_config.json。

    ★ 热重载：wecom_config.json 的 mtime 变化时自动重新载入；凭据变了会顺手
      作废内存里的 access_token 缓存。这样运维在服务器上改完配置立即生效，
      **不用重启服务、更不用重新部署代码**（省掉「改一点就要重装一次」的往返）。
    """
    global WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET, WECOM_BASE_URL, WECOM_REDIRECT
    global WECOM_ADMIN_USERIDS
    global WECOM_TRUSTED_IPS
    global _wecom_cfg_mtime, _wecom_cfg_fingerprint, _wecom_cfg_env_fp
    try:
        mt = os.path.getmtime(WECOM_CONFIG_FILE)
    except OSError:
        mt = None
    cfg = {}
    if os.path.exists(WECOM_CONFIG_FILE):
        try:
            with open(WECOM_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception as e:
            log("wecom_config.json 读取失败：%s" % e)
            cfg = {}
    # 白名单类字段单独解析：它们跟凭据是不是走环境变量无关，
    # 必须在早退分支之前也解析一次（否则「凭据全用环境变量」时白名单读不到）。
    _parse_wecom_whitelists(cfg)
    # 环境变量的取值也进缓存键：容器里常用环境变量传凭据，
    # 改了环境变量（重启容器）必须能重新读进来
    env_fp = (os.environ.get("WECOM_CORP_ID"), os.environ.get("WECOM_AGENT_ID"),
              os.environ.get("WECOM_SECRET"), os.environ.get("WECOM_BASE_URL"),
              os.environ.get("WECOM_REDIRECT"), os.environ.get("WECOM_ADMIN_USERIDS"),
              os.environ.get("WECOM_TRUSTED_IPS"))
    # 环境变量已给全 → 文件改动无意义，直接跳过读盘
    # ⚠️ 但白名单类字段（admin_userids / trusted_ips）仍必须重新解析：
    #    它们可能只写在配置文件里，环境变量并没有给。早退前不解析的话，
    #    「凭据全用环境变量」时白名单会一直是上一次的旧值。
    env_full = all((os.environ.get(k) for k in
                    ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_BASE_URL")))
    if not force and env_full:
        _parse_wecom_whitelists(cfg)
        _wecom_cfg_mtime = mt
        _wecom_cfg_fingerprint = (
            WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET, WECOM_BASE_URL, WECOM_REDIRECT,
            tuple(WECOM_ADMIN_USERIDS), tuple(WECOM_TRUSTED_IPS))
        _wecom_cfg_env_fp = env_fp
        return wecom_enabled()
    if (not force and mt is not None and mt == _wecom_cfg_mtime
            and _wecom_cfg_fingerprint and env_fp == _wecom_cfg_env_fp):
        return wecom_enabled()
    WECOM_CORP_ID = (os.environ.get("WECOM_CORP_ID") or cfg.get("corp_id") or "").strip()
    WECOM_AGENT_ID = (os.environ.get("WECOM_AGENT_ID") or cfg.get("agent_id") or "").strip()
    WECOM_SECRET = (os.environ.get("WECOM_SECRET") or cfg.get("secret") or "").strip()
    WECOM_BASE_URL = (os.environ.get("WECOM_BASE_URL") or cfg.get("base_url") or "").strip().rstrip("/")
    WECOM_REDIRECT = (os.environ.get("WECOM_REDIRECT") or cfg.get("redirect") or "").strip().rstrip("/")
    _wecom_cfg_mtime = mt
    fp = (WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET, WECOM_BASE_URL, WECOM_REDIRECT,
          tuple(WECOM_ADMIN_USERIDS), tuple(WECOM_TRUSTED_IPS))
    # 凭据/地址变了 → 旧的 access_token 立即作废，避免拿旧凭据的 token 去换身份
    if _wecom_cfg_fingerprint and fp != _wecom_cfg_fingerprint:
        _wecom_token_cache["token"] = ""
        _wecom_token_cache["expire_at"] = 0.0
        log("企微配置已变更，access_token 缓存已作废")
    _wecom_cfg_fingerprint = fp
    _wecom_cfg_env_fp = env_fp
    return wecom_enabled()


def wecom_redirect_uri():
    """上报给企微的 redirect_uri（用于扫码会话与授权回调）。

    显式配置 WECOM_REDIRECT 优先；否则用 base_url 推导。
    """
    if WECOM_REDIRECT:
        return WECOM_REDIRECT
    return "%s/api/login/wecom/callback" % WECOM_BASE_URL


def wecom_redirect_host():
    """redirect_uri 的域名（企微白名单校验只看这一段）。"""
    try:
        return (urlparse(wecom_redirect_uri()).hostname or "").lower()
    except Exception:
        return ""


# 企微错误码 → 可照着做的中文指引。
# -31020 = 授权回调域白名单不匹配；60020 = 出口 IP 不在可信 IP 里。
WECOM_ERR_HINTS = {
    "-31020": "redirect_uri 的域名不在企微后台「授权回调域」白名单里",
    "60020": "本机出口 IP 不在企微后台「企业可信 IP」白名单里",
    "41048": "应用未开启「企业微信授权登录」能力（应用详情页 → 网页授权 → 开启）",
    "60011": "AgentId 无效或 Secret 不属于该应用",
    "60020.1": "",
}


def wecom_err_text(code, msg=""):
    """把企微原始错误码翻成能照着做的中文说明。

    只做翻译与提示，不臆测原因：企微没说的，我们也不编。
    这里保持简短（前端弹层里的错误框很小）；完整的后台点击路径见 wecom_diagnose()。
    """
    code = str(code).strip()
    hint = WECOM_ERR_HINTS.get(code, "")
    parts = ["企业微信错误 %s" % code]
    if msg:
        parts.append(msg)
    if hint:
        parts.append(hint)
    # -31020 顺带给出「该填哪个域名」——这是最常见的卡点
    if code == "-31020":
        host = wecom_redirect_host()
        if host:
            parts.append("请在企微后台「授权回调域」填入：%s" % host)
    return "：".join(parts[:2]) + ("　" + "　".join(parts[2:]) if len(parts) > 2 else "")


def wecom_enabled():
    """企微登录是否已具备可用配置（四个值缺一不可）。"""
    return bool(WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET and WECOM_BASE_URL)


def wecom_status():
    """给前端的可用性说明：缺什么就直说，便于运维补配置。"""
    missing = []
    if not WECOM_CORP_ID:
        missing.append("CorpID")
    if not WECOM_AGENT_ID:
        missing.append("AgentId")
    if not WECOM_SECRET:
        missing.append("Secret")
    if not WECOM_BASE_URL:
        missing.append("服务对外地址(WECOM_BASE_URL)")
    return {
        "enabled": wecom_enabled(),
        "missing": missing,
        "reason": ("企业微信扫码登录未配置，缺少：%s" % "、".join(missing)) if missing else "",
        "login_url": "/api/login/wecom/start",
    }


def _public_ip():
    """探测本机对公网的出口 IP（尽力而为，失败返回空串）。

    仅作交叉参考；**权威值以企微报错里的 from ip 为准**（那是企微看到的 IP）。
    """
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com"):
        ok, txt = _http_text(url, timeout=6)
        if not ok:
            continue
        m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", txt or "")
        if m:
            return m.group(0)
    return ""


def wecom_diagnose():
    """一次性体检企微后台三项配置，返回可直接照做的结论。

    ① 应用凭据能否换到 access_token
    ② 授权回调域是否与 redirect_uri 域名一致
    ③ 出口 IP 是否在「企业可信 IP」白名单里
    ④ 对外域名能否打开本服务
    ⑤ 管理员白名单是否配了（决定扫码后能不能进管理后台）
    ⑥ 已登记的可信 IP 与企微实际看到的出口 IP 是否一致

    只报告探测到的事实（企微返回什么就说什么），不臆测。
    """
    load_wecom_config()   # 拿最新的凭据与管理员白名单（支持热重载）
    out = {
        "corp_id": WECOM_CORP_ID,
        "agent_id": WECOM_AGENT_ID,
        "base_url": WECOM_BASE_URL,
        "redirect_uri": wecom_redirect_uri(),
        "redirect_host": wecom_redirect_host(),
        "egress_ip": "",
        "admin_userids": list(WECOM_ADMIN_USERIDS),
        "trusted_ips": list(WECOM_TRUSTED_IPS),
        "items": [],
        "todo": [],
    }

    def add(key, ok, title, detail="", action=""):
        out["items"].append({"key": key, "ok": ok, "title": title,
                              "detail": detail, "action": action})

    # ① 凭据
    tok = ""
    if not (WECOM_CORP_ID and WECOM_AGENT_ID and WECOM_SECRET):
        add("credentials", False, "应用凭据",
            "缺少 CorpID / AgentId / Secret",
            "填 wecom_config.json 或环境变量 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET")
        # ⚠️ 这里**不能提前 return**：凭据没配好时，运维更需要看到其余各项
        #    （授权回调域、可信 IP、对外可达…）—— 否则只看到「缺凭据」，
        #    补好凭据再跑一次才知道还差什么，来回好几轮。
        #    没 token 的检查项各自降级处理即可。
    else:
        tok, err = wecom_access_token()
        add("credentials", bool(tok), "应用凭据",
            "CorpID/AgentId/Secret 有效，已取到 access_token" if tok else (err or "换取失败"),
            "" if tok else "核对企微后台该应用的 AgentId 与 Secret 是否配对")
        # 企微在错误信息里会回报「它看到的本机出口 IP」（from ip: x.x.x.x）——
        # 这比本机自己探测更权威（能反映代理/NAT 后的真实出口）。
        # 凭据有问题时也要抓出来：白名单填错 IP 的人最爱漏看这条。
        if not tok and err:
            m = re.search(r"from ip:\s*([0-9.]+)", err)
            if m:
                out["egress_ip"] = m.group(1)

    # ② 授权回调域（真发一次申请，拿企微的判定）
    host = out["redirect_host"]
    if not host:
        add("callback_domain", False, "授权回调域",
            "缺少 redirect_uri 域名",
            "设置 WECOM_BASE_URL 或 WECOM_REDIRECT")
    else:
        if not tok:
            # 没 token 就验不了回调域，但**该填什么域名照样要告诉运维**
            add("callback_domain", False, "授权回调域",
                "未取到 access_token，暂时无法校验（凭据配好后会自动重测）",
                "需要在企微后台登记的域名是：%s" % host)
        else:
            data, qerr = wecom_qr_fetch()
            if data:
                add("callback_domain", True, "授权回调域",
                    "redirect_uri 域名「%s」已通过企微校验" % host)
            else:
                add("callback_domain", False, "授权回调域",
                    qerr or "校验失败",
                    "「企业微信管理后台 → 应用管理 → 自建应用 → AgentId %s → 企业微信授权登录 → "
                    "设置 → 设置授权回调域」填入：%s" % (WECOM_AGENT_ID, host))

    # ③ 可信 IP
    #   探测方式：用 auth/getuserinfo 带一个假 code。企微**先校验 IP 再校验 code**，
    #   所以 60020 = IP 未加白名单；换成别的错误码（如 40029 code 无效）= IP 已放行。
    #   这样探针不依赖通讯录权限，结论唯一。
    if tok:
        ip_note = ""
        probe_code = "diagnose_probe_invalid_code"
        ok_ip, data = _http_json("%s/auth/getuserinfo?%s" % (
            WECOM_API_BASE, urlencode({"access_token": tok, "code": probe_code})))
        errcode = (data.get("errcode") if isinstance(data, dict) else None)
        errmsg = (data.get("errmsg") or "") if isinstance(data, dict) else ""
        if str(errcode) == "60020":
            m = re.search(r"from ip:\s*([0-9.]+)", errmsg)
            if m:
                ip_note = ("　企微看到的本机出口 IP：%s" % m.group(1))
                out["egress_ip"] = m.group(1)
            add("trusted_ip", False, "企业可信 IP",
                "企微拒绝本机出口 IP（60020）" + ip_note +
                "　换身份的 auth/getuserinfo 同样受可信 IP 限制，不加白名单则扫码后也登不进来",
                "填写位置：企业微信管理后台 → 应用管理 → 自建（自建应用）→ 找到 AgentId %s 的应用"
                " → 应用详情页「开发者接口」区域 →「企业可信 IP」→「配置」→ 添加上面那个 IP 并保存。"
                "注意：填的是【跑后端那台机器】的公网出口 IP，不是域名的 A 记录。"
                "最多可加 120 个，不支持 IP 段/通配符" % WECOM_AGENT_ID)
        elif ok_ip and isinstance(data, dict) and not errcode:
            add("trusted_ip", True, "企业可信 IP",
                "企微未报 60020，本机出口 IP 已放行")
        else:
            # 非 60020：说明 IP 这一关已过（code 无效属于预期）
            out["egress_ip"] = _public_ip()
            add("trusted_ip", True, "企业可信 IP",
                "企微未报 60020，本机出口 IP 已放行（探测 code 无效属预期）"
                + ("　本机探测到的出口 IP：%s" % out["egress_ip"] if out["egress_ip"] else ""))

        # ③-b 已登记 IP 与实际出口 IP 是否一致
        #   企微放行了当前出口 IP，但它可能是**另一个** IP（多出口/NAT 场景）。
        #   拿「登记清单」跟「企微这次看到的 IP」比一比，不一致就是白名单填错了，
        #   或者服务器换出口了。提前发现，比扫码失败后排查省事得多。
        actual = out.get("egress_ip") or ""
        if WECOM_TRUSTED_IPS:
            listed = list(WECOM_TRUSTED_IPS)
            out["trusted_ips"] = listed
            if actual:
                if actual in listed:
                    add("trusted_ip_match", True, "可信 IP 核对",
                        "企微看到的出口 IP %s 已在登记清单里" % actual,
                        "")
                else:
                    add("trusted_ip_match", False, "可信 IP 核对",
                        "企微看到的出口 IP 是 %s，但登记清单里只有：%s。"
                        "说明白名单没覆盖当前出口（服务器换出口了 / 登记时填错了 IP）"
                        % (actual, "、".join(listed)),
                        "把 %s 加到企微后台「企业可信 IP」（应用详情页 → 开发者接口 → "
                        "企业可信 IP → 配置），或把 wecom_config.json 的 trusted_ips "
                        "更新成实际出口 IP" % actual)
            else:
                add("trusted_ip_match", True, "可信 IP 核对",
                    "登记清单：%s（本次没探到企微看到的出口 IP，无法自动比对）"
                    % "、".join(listed), "")
        else:
            out["trusted_ips"] = []
            add("trusted_ip_match", True, "可信 IP 核对",
                "未在 wecom_config.json 的 trusted_ips 里登记已放行的 IP，"
                "跳过自动比对（如需自动核对，把企微后台已登记的 IP 填进去）", "")
    else:
        # 没拿到 access_token → 探不了企微，但**企微在错误信息里回报的出口 IP
        # 仍然可用**（gettoken 失败时会带 from ip），那是权威值。
        # 拿它（退而求其次用本机探测值）跟登记清单比对。
        out["trusted_ips"] = list(WECOM_TRUSTED_IPS)
        actual = out.get("egress_ip") or _public_ip()
        src = "企微看到的" if out.get("egress_ip") else "本机探测到的"
        if WECOM_TRUSTED_IPS and actual:
            listed = list(WECOM_TRUSTED_IPS)
            if actual in listed:
                add("trusted_ip_match", True, "可信 IP 核对",
                    "%s出口 IP %s 已在登记清单里" % (src, actual),
                    "")
            else:
                # 本机探测值与企微看到的不一致 → 多出口/NAT 的信号，一并点出来
                local = _public_ip()
                extra = ""
                if local and local != actual:
                    extra = ("　注意：本机探测到的是 %s，与企微看到的 %s 不同，"
                             "说明这台机器有多个出口（代理/NAT），登记哪个要看企微那边。"
                             % (local, actual))
                add("trusted_ip_match", False, "可信 IP 核对",
                    "%s出口 IP 是 %s，但登记清单里只有：%s。"
                    "说明白名单没覆盖当前出口（服务器换出口了 / 登记时填错了 IP）"
                    % (src, actual, "、".join(listed)) + extra,
                    "把 %s 加到企微后台「企业可信 IP」（应用详情页 → 开发者接口 → "
                    "企业可信 IP → 配置），或把 wecom_config.json 的 trusted_ips "
                    "更新成实际出口 IP" % actual)
        else:
            add("trusted_ip_match", True, "可信 IP 核对",
                ("登记清单：%s（暂时无法探测到企微看到的出口 IP，无法自动比对）"
                 % "、".join(WECOM_TRUSTED_IPS)) if WECOM_TRUSTED_IPS
                else "未在 wecom_config.json 的 trusted_ips 里登记已放行的 IP，"
                     "跳过自动比对（如需自动核对，把企微后台已登记的 IP 填进去）",
                "")

    # ④ 对外可达性（域名能否打开本服务）
    if WECOM_BASE_URL:
        code = _http_status("%s/api/health" % WECOM_BASE_URL, timeout=8)
        if code == 200:
            add("public_reachable", True, "对外访问",
                "https 域名可正常访问本服务")
        elif code == 503:
            add("public_reachable", False, "对外访问",
                "域名返回 503 —— nginx 匹配到了 vhost，但连不上上游后端",
                "后端没起来，或 nginx 的 proxy_pass 端口与后端实际监听端口不一致。"
                "在服务器上执行：systemctl status ai-tools；"
                "ss -ltnp | grep <端口>；确认 nginx 的 proxy_pass 指向同一端口")
        elif code == 0:
            add("public_reachable", False, "对外访问",
                "从本机访问 %s/api/health 失败（连不上：DNS 未解析 / 证书问题 / 服务未监听）" % WECOM_BASE_URL,
                "先确认域名解析到本机、nginx vhost 存在、证书有效")
        else:
            add("public_reachable", False, "对外访问",
                "域名返回 HTTP %s" % code,
                "先确认 nginx 的 proxy_pass 指向本服务监听端口")
    else:
        add("public_reachable", False, "对外访问", "未配置 WECOM_BASE_URL",
            "填 wecom_config.json 的 base_url 为 https://aitoolbox.maihaoche.com")

    # ⑤ 管理员白名单（决定扫码登录后是不是管理员）
    if WECOM_ADMIN_USERIDS:
        add("admin_whitelist", True, "管理员白名单",
            "已配置 %d 个企微 userid，扫码后自动拥有管理员权限：%s"
            % (len(WECOM_ADMIN_USERIDS), "、".join(WECOM_ADMIN_USERIDS)))
    else:
        add("admin_whitelist", False, "管理员白名单",
            "未配置 admin_userids —— 企微扫码进来的人只能是普通用户，"
            "进不了「用户管理 / 审计日志」这些管理功能",
            "把你自己的企微 userid 填进 wecom_config.json 的 admin_userids 数组"
            "（可写多个），保存即生效，不用重启服务")

    # 汇总「还差哪几项」——运维照着做即可，不必回头找人。
    # 分两类：
    #   todo       = 要去**企微后台**点一下的（改完立即生效）
    #   todo_local = 改**本机配置文件 / 服务器**的（同样立即生效，不用碰企微后台）
    # 分开列，免得运维以为「管理员白名单」也要去企微后台里找。
    BACKEND_KEYS = ("credentials", "callback_domain", "trusted_ip", "trusted_ip_match")
    out["todo"] = [i["action"] for i in out["items"]
                    if not i.get("ok") and i.get("action")
                    and i.get("key") in BACKEND_KEYS]
    out["todo_local"] = [i["action"] for i in out["items"]
                         if not i.get("ok") and i.get("action")
                         and i.get("key") not in BACKEND_KEYS]
    out["ready"] = all(i.get("ok") for i in out["items"] if i.get("key") in
                        ("credentials", "callback_domain", "trusted_ip"))
    return out


# ── 企业微信 API 客户端（标准库 urllib，零第三方依赖）──────────────────────
WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
# 扫码授权登录（PC 浏览器扫二维码）用的端点，在应用后台「企业微信授权登录」里配置回调域
WECOM_SSO_BASE = "https://open.work.weixin.qq.com/wwopen/sso"
WECOM_QR_CONNECT_URL = WECOM_SSO_BASE + "/qrConnect"
# 企微扫码页内部用的两个端点（前端不直接调，由本服务代理）：
#   qrConnect  返回页面，window.settings 里带 qrUrl（二维码图片）与 key
#   l/qrConnect 以 JSONP 长轮询查询扫码状态
WECOM_QR_PAGE_URL = WECOM_SSO_BASE + "/qrConnect"
WECOM_QR_POLL_URL = WECOM_SSO_BASE + "/l/qrConnect"
# 网页授权登录（工作台内打开 H5）用的端点，URL 末尾必须带 #wechat_redirect
WECOM_OAUTH_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
_wecom_token_cache = {"token": "", "expire_at": 0.0}


def _http_json(url, timeout=10):
    """GET 一个 JSON 接口，返回 (ok, data)。不抛异常，失败时 ok=False。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AITools/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return True, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return True, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return False, {"errmsg": "HTTP %s" % e.code}
    except Exception as e:
        return False, {"errmsg": str(e)}


def wecom_access_token():
    """取企业微信 access_token，带内存缓存（有效期 7200s，提前 5 分钟失效）。"""
    now = time.time()
    if _wecom_token_cache["token"] and now < _wecom_token_cache["expire_at"]:
        return _wecom_token_cache["token"], ""
    if not (WECOM_CORP_ID and WECOM_SECRET):
        return "", "企业微信配置不完整（缺 CorpID 或 Secret）"
    url = "%s/gettoken?%s" % (WECOM_API_BASE, urlencode({
        "corpid": WECOM_CORP_ID, "corpsecret": WECOM_SECRET}))
    ok, data = _http_json(url)
    if not ok or not isinstance(data, dict):
        return "", "无法连接企业微信接口"
    if data.get("errcode"):
        return "", wecom_err_text(data.get("errcode"), data.get("errmsg"))
    tok = data.get("access_token") or ""
    _wecom_token_cache["token"] = tok
    _wecom_token_cache["expire_at"] = now + max(60, int(data.get("expires_in", 7200)) - 300)
    return tok, ""


def wecom_code_to_userid(code):
    """用授权 code 换取成员身份。返回 (userid, 附加信息dict, 错误信息)。

    两个接口都试一遍，兼容两种授权方式：
      - auth/getuserinfo  ：扫码授权登录（qrConnect）现行接口
      - user/getuserinfo  ：网页授权登录（oauth2 snsapi_base）旧接口
    """
    tok, err = wecom_access_token()
    if not tok:
        return "", {}, err
    last_err = ""
    for path in ("auth/getuserinfo", "user/getuserinfo"):
        url = "%s/%s?%s" % (WECOM_API_BASE, path, urlencode(
            {"access_token": tok, "code": code}))
        ok, data = _http_json(url)
        if not ok or not isinstance(data, dict):
            last_err = "无法连接企业微信接口"
            continue
        if data.get("errcode"):
            last_err = wecom_err_text(data.get("errcode"), data.get("errmsg"))
            continue
        uid = (data.get("userid") or data.get("UserId") or "").strip()
        if uid:
            extra = {"name": data.get("name") or "", "avatar": data.get("avatar") or ""}
            return uid, extra, ""
        last_err = "未能识别企业微信账号"
    return "", {}, last_err or "换取用户身份失败"


def wecom_get_user(userid):
    """按 userid 查成员详情：姓名、部门、头像、邮箱。返回 (dict, err)。"""
    tok, err = wecom_access_token()
    if not tok:
        return None, err
    url = "%s/user/get?%s" % (WECOM_API_BASE, urlencode(
        {"access_token": tok, "userid": userid}))
    ok, data = _http_json(url)
    if not ok or not isinstance(data, dict):
        return None, "无法连接企业微信接口"
    if data.get("errcode"):
        return None, wecom_err_text(data.get("errcode"), data.get("errmsg"))
    return data, ""


def wecom_dept_names():
    """部门 ID → 名称 映射。返回 (dict, err)。"""
    tok, err = wecom_access_token()
    if not tok:
        return None, err
    url = "%s/department/simplelist?access_token=%s" % (WECOM_API_BASE, quote(tok))
    ok, data = _http_json(url)
    if not ok or not isinstance(data, dict):
        return {}, ""
    if data.get("errcode"):
        return {}, ""
    out = {}
    for d in (data.get("department") or []):
        out[str(d.get("id"))] = d.get("name") or ""
    return out, ""


def wecom_user_dept_text(department_ids, dept_map):
    """把成员部门 ID 列表转成可读部门名（多个用 / 连接）。"""
    names = []
    for did in (department_ids or []):
        nm = dept_map.get(str(did)) or ""
        if nm and nm not in names:
            names.append(nm)
    return " / ".join(names)


def wecom_login_user(conn, code):
    """用企微授权码完成登录：换身份 → 查通讯录 → 建号/更新 → 签发本地 token。

    返回 (dict, err)。dict = {"token","user":{...}}
    整页回调（/api/login/wecom/callback）与内嵌扫码（/api/login/wecom/scan）
    共用这一套逻辑，保证两条路径行为完全一致。
    """
    # code → userid（现行接口优先，旧接口兜底）
    userid, ident, err = wecom_code_to_userid(code)
    if not userid:
        return None, err
    # 查通讯录详情（姓名 / 部门 / 头像）。失败不阻塞登录 —— 降级用 userid 当姓名。
    profile, perr = wecom_get_user(userid)
    dept_map, _ = wecom_dept_names()
    if profile:
        dept = wecom_user_dept_text(profile.get("department"), dept_map)
        name = (profile.get("name") or "").strip() or userid
        avatar = (profile.get("avatar") or "").strip()
    else:
        # 常见原因：服务器出口 IP 未加入企微「企业可信IP」，或未授予通讯录权限
        dept = ""
        name = (ident.get("name") or "").strip() or userid
        avatar = (ident.get("avatar") or "").strip()
        log("读取企微成员详情失败（降级为仅用户名）：%s" % (perr or "未知错误"))
    # 建号或更新
    u = conn.execute("SELECT * FROM users WHERE wecom_userid=?", (userid,)).fetchone()
    if u:
        # 通讯录可用才覆盖姓名/部门；不可用时保留库里已有的值，避免把"张三"降级成 userid
        if profile:
            conn.execute(
                "UPDATE users SET name=?, dept=?, avatar=COALESCE(NULLIF(?,''),avatar) WHERE id=?",
                (name, dept, avatar, u["id"]))
        else:
            conn.execute(
                "UPDATE users SET avatar=COALESCE(NULLIF(?,''),avatar) WHERE id=?",
                (avatar, u["id"]))
        conn.commit()
        user_id = u["id"]
        # 已在白名单里的 userid：补一次角色升级（先建号、后加白名单的情况）
        if userid in WECOM_ADMIN_USERIDS and u["role"] != "admin":
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (u["id"],))
            conn.commit()
            log("企微用户 %s（%s）已在管理员白名单，角色已升为 admin" % (userid, name))
            u = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    else:
        # 首次扫码：自动建号。用户名取 userid（企微内唯一），
        # 随机密码仅作为占位（该账号走企微登录，不需要用密码登录）
        username = "wecom_" + re.sub(r"[^A-Za-z0-9_.-]", "_", userid)
        if conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            username = "%s_%s" % (username, secrets.token_hex(3))
        salt = new_salt()
        # 白名单里的 userid 直接给管理员角色（否则没人能进管理后台给自己升权）
        role = "admin" if userid in WECOM_ADMIN_USERIDS else "user"
        conn.execute(
            "INSERT INTO users(username,name,dept,pwd_hash,salt,role,status,avatar,wecom_userid)"
            " VALUES(?,?,?,?,?,?,'active',?,?)",
            (username, name, dept, hash_password(secrets.token_urlsafe(12), salt), salt,
             role, avatar, userid))
        conn.commit()
        user_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        log("企业微信首次登录，自动建号：%s（%s）　角色：%s" % (username, name, role))
    # 签发本地会话 token
    local_token = secrets.token_urlsafe(32)
    exp = (datetime.now() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                 (local_token, user_id, exp))
    conn.commit()
    log("用户登录：%s（企微扫码，%s）" % (name, userid))
    return {
        "token": local_token,
        "user": {"id": user_id, "username": u["username"] if u else ("wecom_" + userid),
                 "name": name, "dept": dept,
                 # 角色取库里真实值，别写死 user —— 否则前端会一直显示普通用户，
                 # 管理员入口（用户管理/审计日志）点不进去
                 "role": (u["role"] if u else "user")},
    }, ""


# ── 扫码登录（内嵌二维码模式）────────────────────────────────────────────
# 企微扫码页 qrConnect 返回的 HTML 里，window.settings 带 qrUrl（二维码图片
# 直链）与 key（二维码会话标识）。前端据此显示二维码，再用 key 长轮询查扫码状态。
# 整个协议由本服务代理，前端不直接访问 open.work.weixin.qq.com。

_wecom_qr_sessions = {}      # sid -> {"key","redirect_uri","state","created"}
_wecom_qr_lock = threading.Lock()


def _http_text(url, timeout=12, referer=""):
    """GET 一个文本地址，返回 (ok, text)。

    ok=True 仅当 HTTP 状态码是 2xx。503 / 404 等一律算失败——
    否则「域名能打开」会被误判成通（nginx upstream 连不上时返回的正是 503，
    而 HTTPError 里带着响应体，看起来像成功）。
    """
    try:
        hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
        if referer:
            hdr["Referer"] = referer
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= int(resp.status) < 300):
                return False, "HTTP %s" % resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # 保留响应体：企微有时把错误信息（如 -31020）放在非 2xx 的 body 里
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return False, ("HTTP %s %s" % (e.code, body)).strip()
    except Exception as e:
        return False, str(e)
    try:
        return True, raw.decode("utf-8", "replace")
    except Exception:
        return False, "解码失败"


def _http_status(url, timeout=12, referer=""):
    """GET 一个地址，只返回 HTTP 状态码（int）。连不上返回 0。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AITools/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


def _http_bytes(url, timeout=12, referer=""):
    """GET 一个二进制地址，返回 (ok, bytes|错误串, content_type)。"""
    try:
        hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
        if referer:
            hdr["Referer"] = referer
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read(), (resp.headers.get("Content-Type") or "application/octet-stream")
    except urllib.error.HTTPError as e:
        try:
            return True, e.read(), (e.headers.get("Content-Type") or "application/octet-stream")
        except Exception:
            return False, "HTTP %s" % e.code, ""
    except Exception as e:
        return False, str(e), ""


def wecom_qr_fetch():
    """向企微申请一个二维码会话。返回 (dict, err)。

    dict: {"key","qr_url","redirect_uri","appid","state"}
    """
    if not wecom_enabled():
        return None, wecom_status()["reason"] or "企业微信登录未配置"
    cb = wecom_redirect_uri()
    state = secrets.token_urlsafe(24)
    url = "%s?%s" % (WECOM_QR_PAGE_URL, urlencode({
        "appid": WECOM_CORP_ID,
        "agentid": WECOM_AGENT_ID,
        "redirect_uri": cb,
        "state": state,
        "login_type": "jssdk",
        "version": "1.2.7",
    }))
    ok, html = _http_text(url, referer=WECOM_BASE_URL + "/")
    if not ok:
        return None, "无法连接企业微信：%s" % html[:120]
    m = re.search(r"window\.settings\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        return None, "企业微信未返回会话信息，请稍后重试"
    try:
        st = json.loads(m.group(1))
    except Exception:
        return None, "企业微信返回信息解析失败"
    if st.get("errCode"):
        return None, wecom_err_text(st.get("errCode"), st.get("errMsg") or "")
    key = st.get("key") or ""
    qr_url = st.get("qrUrl") or ""
    if not key or not qr_url:
        return None, "企业微信未返回二维码（请检查授权回调域配置）"
    sid = secrets.token_urlsafe(18)
    with _wecom_qr_lock:
        _wecom_qr_sessions[sid] = {
            "key": key,
            "qr_url": qr_url,          # 企微下发的二维码图片完整地址
            "redirect_uri": cb,
            "state": state,
            "created": time.time(),
        }
        # 只保留最近 30 个会话，避免长期运行内存增长
        if len(_wecom_qr_sessions) > 30:
            for k in sorted(_wecom_qr_sessions,
                            key=lambda x: _wecom_qr_sessions[x]["created"])[:-30]:
                _wecom_qr_sessions.pop(k, None)
    log("企微二维码会话：sid=%s… key=%s…" % (sid[:8], key[:8]))
    return {"sid": sid, "key": key, "qr_url": qr_url,
            "redirect_uri": cb, "state": state, "appid": WECOM_CORP_ID}, ""


def wecom_qr_get_session(sid):
    with _wecom_qr_lock:
        return dict(_wecom_qr_sessions.get(sid) or {})


def wecom_qr_poll(sid, last_status=""):
    """长轮询一次扫码状态。返回 (status_dict, err)。

    status_dict: {"status":"QRCODE_SCAN_NEVER/ING/SUCC/FAIL/ERR", "auth_code":...}
    成功时 auth_code 即授权码，交给 wecom_code_to_userid 换身份。
    """
    sess = wecom_qr_get_session(sid)
    if not sess:
        return None, "二维码会话已失效，请刷新二维码"
    url = "%s?%s" % (WECOM_QR_POLL_URL, urlencode({
        "key": sess["key"],
        "redirect_uri": sess["redirect_uri"],
        "appid": WECOM_CORP_ID,
        "lastStatus": last_status or "",
        "callback": "jsonpCallback",
        "_": int(time.time() * 1000),
    }))
    ok, raw = _http_text(url, timeout=65, referer=WECOM_BASE_URL + "/")
    if not ok:
        return None, "无法连接企业微信：%s" % raw[:120]
    m = re.search(r"jsonpCallback\((.*)\)\s*;?\s*$", raw.strip(), re.S)
    if not m:
        return None, "企业微信返回格式异常"
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None, "企业微信返回解析失败"
    if not isinstance(data, dict):
        return None, "企业微信返回异常"
    return data, ""


def hash_password(pwd, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 120000
    ).hex()


def new_salt():
    return secrets.token_hex(16)


def make_user(conn, username, name, dept, password, role="user"):
    salt = new_salt()
    conn.execute(
        "INSERT INTO users(username,name,dept,pwd_hash,salt,role,status)"
        " VALUES(?,?,?,?,?,?,'active')",
        (username.strip(), name.strip(), dept.strip(), hash_password(password, salt), salt, role),
    )
    return conn.execute("SELECT id FROM users WHERE username=?", (username.strip(),)).fetchone()["id"]


PRESET_ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts_preset.json")


def ensure_admin(conn):
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] > 0:
        return
    if os.path.exists(PRESET_ACCOUNTS_FILE):
        try:
            with open(PRESET_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
            if isinstance(accounts, list) and accounts:
                for a in accounts:
                    if not isinstance(a, dict) or not a.get("username") or not a.get("password"):
                        continue
                    make_user(
                        conn,
                        str(a["username"]),
                        str(a.get("name") or a["username"]),
                        str(a.get("dept") or ""),
                        str(a["password"]),
                        role=str(a.get("role") or "user"),
                    )
                conn.commit()
                log("已导入预置账号 %d 个（%s），来自 accounts_preset.json，上线后请尽快修改默认密码"
                    % (len(accounts), "、".join(str(a.get("username")) for a in accounts)))
                return
        except Exception as e:
            log("预置账号文件读取失败，改用随机管理员：%s" % e)
    pwd = secrets.token_urlsafe(9)
    make_user(conn, "admin", "超级管理员", "产品技术中心", pwd, role="admin")
    conn.commit()
    log("=" * 62)
    log("已创建内置管理员账号（首次启动，请立即登录修改密码）：")
    log("  用户名: admin")
    log("  密  码: %s" % pwd)
    log("=" * 62)


def migrate_db(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tools)").fetchall()}
    if "created_by" not in cols:
        conn.execute("ALTER TABLE tools ADD COLUMN created_by INTEGER DEFAULT NULL")
        log("已迁移：tools 增加 created_by 列")
    if "deleted" not in cols:
        conn.execute("ALTER TABLE tools ADD COLUMN deleted INTEGER DEFAULT 0")
        log("已迁移：tools 增加 deleted 列")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE tools ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE tools SET updated_at=added_at WHERE updated_at IS NULL OR updated_at=''")
        log("已迁移：tools 增加 updated_at 列")
    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT ''")
        log("已迁移：users 增加 avatar 列")
    if "wecom_userid" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN wecom_userid TEXT DEFAULT ''")
        log("已迁移：users 增加 wecom_userid 列（企业微信登录）")
    # 企微用户唯一索引：同一个企微 userid 只能绑一个本地账号
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wecom ON users(wecom_userid)"
            " WHERE wecom_userid IS NOT NULL AND wecom_userid<>''")
    except sqlite3.OperationalError as e:
        log("创建 users(wecom_userid) 唯一索引失败（可能存在重复值）：%s" % e)
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(favorites)").fetchall()}
    if "seen_at" not in fcols:
        conn.execute("ALTER TABLE favorites ADD COLUMN seen_at TEXT DEFAULT ''")
        conn.execute("UPDATE favorites SET seen_at=datetime('now','localtime') WHERE seen_at IS NULL OR seen_at=''")
        log("已迁移：favorites 增加 seen_at 列")
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "tool_id" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN tool_id INTEGER DEFAULT NULL")
        log("已迁移：messages 增加 tool_id 列")
    if "comment_text" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN comment_text TEXT DEFAULT ''")
        log("已迁移：messages 增加 comment_text 列")
    # 种子工具没有上传者（created_by 为空）→ 任何人评论都不会产生消息通知，
    # 演示时看不到「评论→红点」的完整链路。这里把无归属的种子工具归到管理员名下，
    # 使他人（游客体验号）评论这些工具时，管理员能真实收到消息通知。
    try:
        orphan = conn.execute(
            "SELECT COUNT(*) c FROM tools WHERE (created_by IS NULL OR created_by=0) AND deleted=0"
        ).fetchone()["c"]
        if orphan:
            adm = conn.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
            if adm:
                conn.execute(
                    "UPDATE tools SET created_by=? WHERE (created_by IS NULL OR created_by=0) AND deleted=0",
                    (adm["id"],))
                log("已迁移：%d 个无归属工具归入管理员名下（评论通知可正常触发）" % orphan)
    except Exception as e:
        log("工具归属回填失败：%s" % e)
    # 回填历史评论消息的 tool_id：从 body 里的《工具名》反查
    try:
        rows = conn.execute(
            "SELECT id, body FROM messages WHERE tool_id IS NULL AND body LIKE '%《%》%'"
        ).fetchall()
        for r in rows:
            body = r["body"] or ""
            i, j = body.find("《"), body.find("》")
            if i < 0 or j <= i:
                continue
            tname = body[i + 1:j].strip()
            if not tname:
                continue
            hit = conn.execute(
                "SELECT id FROM tools WHERE name=? ORDER BY id DESC LIMIT 1", (tname,)
            ).fetchone()
            if hit:
                conn.execute("UPDATE messages SET tool_id=? WHERE id=?", (hit["id"], r["id"]))
                log("已回填 messages.tool_id：message %s -> tool %s" % (r["id"], hit["id"]))
    except Exception as e:
        log("回填 messages.tool_id 失败：%s" % e)
    # 回填历史消息的评论正文：按「同发送人 + 时间最接近」匹配 comments
    try:
        rows = conn.execute(
            "SELECT id, from_user_id, tool_id, created_at FROM messages"
            " WHERE comment_text IS NULL OR comment_text=''"
        ).fetchall()
        for r in rows:
            hit = None
            if r["tool_id"]:
                hit = conn.execute(
                    "SELECT content FROM comments WHERE tool_id=? AND user_id=?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (r["tool_id"], r["from_user_id"])).fetchone()
            if not hit:
                hit = conn.execute(
                    "SELECT content FROM comments WHERE user_id=?"
                    " ORDER BY ABS(strftime('%s',created_at)-strftime('%s',?)) LIMIT 1",
                    (r["from_user_id"], r["created_at"])).fetchone()
            if hit and hit["content"]:
                conn.execute("UPDATE messages SET comment_text=? WHERE id=?",
                             (hit["content"], r["id"]))
    except Exception as e:
        log("回填 messages.comment_text 失败：%s" % e)
    # 规范化历史消息正文：去掉重复的「昵称 」前缀（头部已单独显示昵称）
    try:
        for r in conn.execute(
            "SELECT m.id, m.body, u.name FROM messages m LEFT JOIN users u ON u.id=m.from_user_id"
            " WHERE m.body LIKE '% 评论了你的工具《%'"
        ).fetchall():
            nm = (r["name"] or "").strip()
            body = r["body"] or ""
            if nm and body.startswith(nm + " "):
                conn.execute("UPDATE messages SET body=? WHERE id=?",
                             (body[len(nm) + 1:], r["id"]))
    except Exception as e:
        log("规范化 messages.body 失败：%s" % e)
    conn.commit()


def get_user_by_session(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.token=? AND s.expires_at > datetime('now','localtime')",
        (token,),
    ).fetchone()
    if not row or row["status"] != "active":
        return None
    return row


def audit(conn, admin_id, action, target, before="", after=""):
    conn.execute(
        "INSERT INTO audit_log(admin_user_id,action,target,before,after) VALUES(?,?,?,?,?)",
        (admin_id, action, target, json.dumps(before, ensure_ascii=False) if not isinstance(before, str) else before,
         json.dumps(after, ensure_ascii=False) if not isinstance(after, str) else after),
    )

def meta_get(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default

def meta_set(conn, key, val):
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (key, str(val)))

def init_db(reseed=False, clear=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = db()
    conn.executescript(SCHEMA)
    migrate_db(conn)
    ensure_admin(conn)
    if reseed or clear:
        conn.execute("DELETE FROM tools")
        conn.commit()
        log("已清空 tools 表")
    if reseed:
        conn.execute("DELETE FROM meta WHERE k='seed_loaded'")
        conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM tools").fetchone()["c"]

    seed_done = meta_get(conn, "seed_loaded") == "1"
    if n == 0 and not clear and not seed_done and os.path.exists(SEED_FILE):
        with open(SEED_FILE, "r", encoding="utf-8") as f:
            seed = json.load(f)
        for t in seed:
            insert_tool(conn, t, is_new=bool(t.get("is_new")))
        conn.commit()
        meta_set(conn, "seed_loaded", "1")
        conn.commit()
        log("已导入种子数据：%d 个工具" % len(seed))
    elif n == 0 and clear:
        meta_set(conn, "seed_loaded", "1")
        conn.commit()
        log("已标记为「不再自动导入种子数据」")
    return db().execute("SELECT COUNT(*) c FROM tools").fetchone()["c"]

def insert_tool(conn, t, is_new=False):
    detail = t.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    files = t.get("files") or []
    if not isinstance(files, list):
        files = []
    added = t.get("added_at") or t.get("addedAt") or datetime.now().strftime("%Y-%m-%d")
    cur = conn.execute(
        "INSERT INTO tools(name,dept,type,desc,detail,link,files,added_at,is_new,downloads,likes,favorites,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (t.get("name") or "未命名工具").strip(),
            t.get("dept") or "",
            t.get("type") if t.get("type") in VALID_TYPES else "其他",
            t.get("desc") or "",
            json.dumps(detail, ensure_ascii=False),
            t.get("link") or None,
            json.dumps(files, ensure_ascii=False),
            added,
            1 if is_new else 0,
            int(t.get("downloads") or 0),
            int(t.get("likes") or 0),
            int(t.get("favorites") or 0),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return cur.lastrowid

def row_to_item(r, user_id=None, conn=None):
    try:
        detail = json.loads(r["detail"] or "{}")
    except Exception:
        detail = {}
    try:
        files = json.loads(r["files"] or "[]")
    except Exception:
        files = []
    if not isinstance(detail, dict):
        detail = {}
    if not isinstance(files, list):
        files = []
    saved = False
    if user_id and conn:
        saved = bool(conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND tool_id=?",
            (user_id, r["id"]),
        ).fetchone())
    creator = ""
    if r["created_by"]:
        c = conn.execute("SELECT name FROM users WHERE id=?", (r["created_by"],)).fetchone() if conn else None
        creator = c["name"] if c else ""
    return {
        "id": r["id"],
        "name": r["name"],
        "dept": r["dept"] or "",
        "type": r["type"] or "其他",
        "desc": r["desc"] or "",
        "detail": {
            "intro": detail.get("intro", ""),
            "usage": detail.get("usage", []) or [],
            "highlights": detail.get("highlights", []) or [],
        },
        "link": r["link"],
        "files": files,
        "addedAt": r["added_at"] or "",
        "isNew": bool(r["is_new"]),
        "createdBy": creator,
        "downloads": int(r["downloads"] or 0),
        "likes": int(r["likes"] or 0),
        "favorites": int(r["favorites"] or 0),
        "liked": False,
        "saved": saved,
    }

def _get_param(line, key):
    m = re.search(r'%s="([^"]*)"' % re.escape(key), line)
    if m:
        return m.group(1)
    m = re.search(r"%s=([^;]+)" % re.escape(key), line)
    return m.group(1).strip() if m else None

def parse_multipart(body, boundary):
    delim = b"--" + boundary.encode("utf-8")
    out = []
    for seg in body.split(delim)[1:]:
        if seg[:2] == b"--":
            break
        seg = seg[2:] if seg[:2] == b"\r\n" else seg
        head, sep, data = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("utf-8", "replace")
        name = filename = None
        ctype = "application/octet-stream"
        for line in headers.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                name = _get_param(line, "name")
                filename = _get_param(line, "filename")
            elif low.startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        if filename is not None:
            try:
                filename = filename.encode("latin-1").decode("utf-8")
            except Exception:
                pass
        out.append((name, filename, ctype, data))
    return out

def safe_filename(name):
    name = os.path.basename(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f]", "", name).strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    if not name:
        name = "file"
    stem, ext = os.path.splitext(name)
    stem = stem[:60] or "file"
    ext = ext[:16]
    return stem + ext

class Handler(BaseHTTPRequestHandler):
    # 单线程模式必须用 HTTP/1.0（每请求处理完立即断开）：
    # 若用 HTTP/1.1 长连接，一个浏览器的 keep-alive 会一直占住服务，其他人全部卡死。
    protocol_version = "HTTP/1.0"
    server_version = "AITools/1.0"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _cors(self):
        if CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _ok(self, data=None):
        self._json(200, {"ok": True, "data": data if data is not None else {}})

    def _err(self, code, msg):
        self._json(code, {"ok": False, "msg": msg})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _path(self, raw):
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return unquote(raw)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        p = self._path(u.path)
        q = parse_qs(u.query)
        self._body()
        # 企微相关请求前热重载配置：运维改完 wecom_config.json 无需重启服务
        if p.startswith("/api/login/wecom"):
            load_wecom_config()
        try:
            if p == "/api/health":
                return self._ok(
                    {
                        "status": "ok",
                        "tools": db().execute("SELECT COUNT(*) c FROM tools WHERE deleted=0").fetchone()["c"],
                        "uptime_sec": int(time.time() - START_TIME),
                        "db": DB_PATH,
                        "data_dir": DATA_DIR,
                    }
                )
            if p == "/api/me":
                return self.api_me()
            if p == "/api/login/wecom/status":
                return self.api_wecom_status()
            if p == "/api/login/wecom/diagnose":
                return self.api_wecom_diagnose()
            if p == "/api/login/wecom/qr":
                return self.api_wecom_qr()
            if p == "/api/login/wecom/qrcode":
                return self.api_wecom_qrcode(q)
            if p == "/api/login/wecom/scan":
                return self.api_wecom_scan(q)
            if p == "/api/login/wecom/start":
                return self.api_wecom_start(q)
            if p == "/api/login/wecom/callback":
                return self.api_wecom_callback(q)
            if p == "/api/tools":
                return self.api_list(q)
            m = re.match(r"^/api/my/(tools|favorites)$", p)
            if m:
                return self.api_my(m.group(1))
            if p == "/api/admin/users":
                return self.api_admin_users_list()
            if p == "/api/admin/audit":
                return self.api_admin_audit()
            if p == "/api/admin/deleted":
                return self.api_admin_deleted()
            m = re.match(r"^/api/tools/(\d+)/comments$", p)
            if m:
                return self.api_comments(int(m.group(1)))
            if p == "/api/messages":
                return self.api_messages()
            if p == "/api/messages/unread":
                return self.api_messages_unread()
            if p.startswith("/uploads/"):
                return self.serve_upload(p[len("/uploads/"):])
            if p.startswith("/tools/"):
                return self.serve_tool_file(p[len("/tools/"):])
            return self.serve_static(p)
        except BrokenPipeError:
            pass
        except Exception as e:
            log("处理 GET %s 出错: %s" % (p, e))
            try:
                self._err(500, "服务器内部错误：%s" % e)
            except Exception:
                pass

    def do_POST(self):
        u = urlparse(self.path)
        p = self._path(u.path)
        body = self._body()
        # 企微相关请求前热重载配置：运维改完 wecom_config.json 无需重启服务
        if p.startswith("/api/login/wecom"):
            load_wecom_config()
        try:
            if p == "/api/upload":
                return self.api_upload(body)
            if p == "/api/tools":
                return self.api_create(body)
            if p == "/api/login":
                return self.api_login(body)
            if p == "/api/login/wecom/exchange":
                return self.api_wecom_exchange(body)
            if p == "/api/logout":
                return self.api_logout()
            if p == "/api/me/password":
                return self.api_change_pwd(body)
            if p == "/api/me":
                return self.api_me_update(body)
            m = re.match(r"^/api/favorite/(\d+)$", p)
            if m:
                return self.api_favorite_toggle(int(m.group(1)))
            m = re.match(r"^/api/favorite/(\d+)/seen$", p)
            if m:
                return self.api_favorite_seen(int(m.group(1)))
            m = re.match(r"^/api/tools/(\d+)/comments$", p)
            if m:
                return self.api_comment_post(int(m.group(1)), body)
            m = re.match(r"^/api/messages/(\d+)/read$", p)
            if m:
                return self.api_message_read(int(m.group(1)))
            m = re.match(r"^/api/tools/(\d+)/(like|favorite|download)$", p)
            if m:
                return self.api_bump(int(m.group(1)), m.group(2))
            if p == "/api/admin/users":
                return self.api_admin_user_create(body)
            m = re.match(r"^/api/admin/users/(\d+)/(reset-pwd|disable|enable)$", p)
            if m:
                return self.api_admin_user_action(int(m.group(1)), m.group(2), body)
            m = re.match(r"^/api/admin/tools/(\d+)/counters$", p)
            if m:
                return self.api_admin_counters(int(m.group(1)), body)
            m = re.match(r"^/api/admin/tools/(\d+)/restore$", p)
            if m:
                return self.api_tool_restore(int(m.group(1)))
            return self._err(404, "接口不存在：%s" % p)
        except BrokenPipeError:
            pass
        except Exception as e:
            log("处理 POST %s 出错: %s" % (p, e))
            try:
                self._err(500, "服务器内部错误：%s" % e)
            except Exception:
                pass

    def do_PUT(self):
        u = urlparse(self.path)
        p = self._path(u.path)
        body = self._body()
        try:
            m = re.match(r"^/api/tools/(\d+)$", p)
            if m:
                return self.api_tool_update(int(m.group(1)), body)
            return self._err(404, "接口不存在：%s" % p)
        except Exception as e:
            log("处理 PUT %s 出错: %s" % (p, e))
            try:
                self._err(500, "服务器内部错误：%s" % e)
            except Exception:
                pass

    def do_DELETE(self):
        u = urlparse(self.path)
        p = self._path(u.path)
        self._body()
        try:
            m = re.match(r"^/api/tools/(\d+)$", p)
            if m:
                return self.api_tool_delete(int(m.group(1)))
            return self._err(404, "接口不存在：%s" % p)
        except Exception as e:
            log("处理 DELETE %s 出错: %s" % (p, e))
            try:
                self._err(500, "服务器内部错误：%s" % e)
            except Exception:
                pass

    def api_list(self, q):
        conn = db()
        user = get_user_by_session(conn, self._token())
        uid = user["id"] if user else None
        sql = "SELECT * FROM tools WHERE deleted=0"
        args = []
        where = []
        kw = (q.get("q") or [""])[0].strip()
        if kw:
            like = "%" + kw + "%"
            where.append("(name LIKE ? OR dept LIKE ? OR type LIKE ? OR desc LIKE ?)")
            args += [like, like, like, like]
        tag = (q.get("tag") or [""])[0].strip()
        if tag and tag != "全部":
            where.append("type = ?")
            args.append(tag)
        mine = (q.get("mine") or [""])[0].strip()
        if mine == "1":
            if not uid:
                return self._err(401, "请先登录")
            where.append("created_by = ?")
            args.append(uid)
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY is_new DESC, added_at DESC, id DESC"
        rows = conn.execute(sql, args).fetchall()
        items = [row_to_item(r, uid, conn) for r in rows]
        total = len(items)
        page_s = (q.get("page") or [""])[0].strip()
        size_s = (q.get("pageSize") or [""])[0].strip()
        if page_s or size_s:
            page = max(1, int(page_s or 1))
            size = max(1, min(200, int(size_s or 12)))
            items = items[(page - 1) * size: page * size]
        return self._ok({"items": items, "total": total})

    def api_create(self, raw):
        conn = db()
        user = get_user_by_session(conn, self._token())
        if not user:
            return self._err(401, "上传工具请先登录")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            return self._err(400, "请求体格式错误")
        name = (data.get("name") or "").strip()
        if not name:
            return self._err(400, "工具名称不能为空")
        t = {
            "name": name,
            "dept": data.get("dept") or "",
            "type": data.get("type") if data.get("type") in VALID_TYPES else "其他",
            "desc": data.get("desc") or "",
            "detail": data.get("detail") or {"intro": "", "usage": [], "highlights": []},
            "link": data.get("link") or None,
            "files": data.get("files") or [],
            "added_at": datetime.now().strftime("%Y-%m-%d"),
        }
        tid = insert_tool(conn, t, is_new=True)
        conn.execute("UPDATE tools SET created_by=? WHERE id=?", (user["id"], tid))
        conn.commit()
        row = conn.execute("SELECT * FROM tools WHERE id=?", (tid,)).fetchone()
        log("新增工具 #%d %s（by %s）" % (tid, name, user["name"]))
        return self._ok(row_to_item(row, user["id"], conn))

    def api_upload(self, body):
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r"boundary=([^;]+)", ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._err(400, "请用 multipart/form-data 上传")
        limit = MAX_UPLOAD_MB * 1024 * 1024
        if len(body) > limit * 20:
            return self._err(413, "上传内容过大")
        saved = []
        for name, filename, fct, data in parse_multipart(body, m.group(1).strip('"')):
            if filename is None:
                continue
            if len(data) > limit:
                return self._err(413, "文件「%s」超过 %d MB 上限" % (filename, MAX_UPLOAD_MB))
            if not data:
                continue
            clean = safe_filename(filename)
            stored = "%s_%s" % (uuid.uuid4().hex[:12], clean)
            with open(os.path.join(UPLOAD_DIR, stored), "wb") as f:
                f.write(data)
            saved.append(
                {
                    "name": clean,
                    "size": len(data),
                    "type": fct or mimetypes.guess_type(clean)[0] or "application/octet-stream",
                    "url": "/uploads/" + stored,
                }
            )
        log("上传 %d 个附件" % len(saved))
        return self._ok(saved)

    def api_bump(self, tid, kind):
        col = {"like": "likes", "favorite": "favorites", "download": "downloads"}[kind]
        conn = db()
        cur = conn.execute(
            "UPDATE tools SET %s = %s + 1 WHERE id = ?" % (col, col), (tid,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return self._err(404, "工具不存在：%d" % tid)
        val = conn.execute("SELECT %s v FROM tools WHERE id=?" % col, (tid,)).fetchone()["v"]
        return self._ok({col: val, "id": tid})

    def _token(self):
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h.startswith("Bearer ") else None

    def _me(self):
        return get_user_by_session(db(), self._token())

    def _require_admin(self):
        u = self._me()
        if not u:
            return None, self._err(401, "请先登录")
        if u["role"] != "admin":
            return None, self._err(403, "需要管理员权限")
        return u, None

    def api_login(self, body):
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return self._err(400, "请输入用户名和密码")
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u or hash_password(password, u["salt"]) != u["pwd_hash"]:
            return self._err(401, "用户名或密码错误")
        if u["status"] != "active":
            return self._err(403, "账号已被禁用，请联系管理员")
        token = secrets.token_urlsafe(32)
        exp = (datetime.now() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token, u["id"], exp))
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now','localtime')")
        conn.commit()
        log("用户登录：%s（%s）" % (username, u["name"]))
        return self._ok({
            "token": token,
            "user": {"id": u["id"], "username": u["username"], "name": u["name"],
                     "dept": u["dept"], "role": u["role"]},
        })

    # ── 企业微信扫码登录 ──────────────────────────────────────────────
    def api_wecom_status(self):
        """前端探测企微登录是否可用（未配置时返回 enabled=false + 缺什么）。"""
        return self._ok(wecom_status())

    def api_wecom_diagnose(self):
        """企微后台三项配置的一键体检（凭据 / 授权回调域 / 可信IP）。"""
        try:
            return self._ok(wecom_diagnose())
        except Exception as e:
            return self._err(500, "体检失败：%s" % e)

    def api_wecom_start(self, query):
        """生成企微扫码登录地址并 302 跳转。state 做防 CSRF，同时作为回调凭据。

        mode=qr （默认）：企微「扫码授权登录」qrConnect —— PC 浏览器显示二维码，
                            手机企微扫后回调。应用后台需开「企业微信授权登录」并设授权回调域。
        mode=oauth：企微「网页授权登录」snsapi_base —— 必须在企业微信客户端内打开
                            （工作台应用/聊天链接），URL 末尾要带 #wechat_redirect。
        """
        if not wecom_enabled():
            return self._err(503, wecom_status()["reason"] or "企业微信扫码登录未配置")
        try:
            state = secrets.token_urlsafe(24)
        except Exception:
            return self._err(500, "生成 state 失败")
        conn = db()
        try:
            conn.execute("INSERT INTO wecom_auth(code,userid) VALUES(?,?)", (state, ""))
            conn.commit()
        except Exception as e:
            return self._err(500, "写入授权记录失败：%s" % e)
        cb = wecom_redirect_uri()
        mode = ((query.get("mode") or ["qr"])[0] or "qr").lower()
        if mode == "oauth":
            # 网页授权（工作台内 H5）：snsapi_base 静默授权，拿 code 换 userid
            url = "%s?%s#wechat_redirect" % (WECOM_OAUTH_URL, urlencode({
                "appid": WECOM_CORP_ID,
                "redirect_uri": cb,
                "response_type": "code",
                "scope": "snsapi_base",
                "state": state,
                "agentid": WECOM_AGENT_ID,
            }))
        else:
            # 扫码授权登录（PC 浏览器）：二维码在 PC 上，扫码后回调带 code
            url = "%s?%s" % (WECOM_QR_CONNECT_URL, urlencode({
                "appid": WECOM_CORP_ID,
                "agentid": WECOM_AGENT_ID,
                "redirect_uri": cb,
                "state": state,
            }))
        log("企业微信登录跳转：mode=%s state=%s…" % (mode, state[:8]))
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def api_wecom_callback(self, query):
        """企微回调：code 换 userid → 查通讯录 → 建号/更新 → 签发本地 token。
        成功后 302 回首页并带上一次性 code（前端再用它换正式 token）。"""
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            # 用户点了"拒绝授权"时企微只回 state、不回 code
            return self._redirect_with_error("未获得授权（可能已在手机上取消授权）")
        conn = db()
        row = conn.execute("SELECT * FROM wecom_auth WHERE code=?", (state,)).fetchone()
        if not row:
            return self._redirect_with_error("授权状态已失效，请重新登录")
        # state 一次性：用完即删
        conn.execute("DELETE FROM wecom_auth WHERE code=?", (state,))
        conn.commit()
        # 建号 + 签发 token（与内嵌扫码共用同一套逻辑）
        res, err = wecom_login_user(conn, code)
        if not res:
            return self._redirect_with_error(err)
        local_token = res["token"]
        # 把本地 token 包成一次性 code 回首页，避免 token 出现在 URL 里
        handoff = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO wecom_auth(code,userid) VALUES(?,?)", (handoff, local_token))
        conn.commit()
        back = "%s/?wecom_code=%s" % (WECOM_BASE_URL, handoff)
        self.send_response(302)
        self.send_header("Location", back)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── 内嵌二维码模式（登录框内直接显示二维码，不整页跳转）────────────────
    def api_wecom_qr(self):
        """申请一个企微二维码会话，返回二维码图片代理地址 + 会话 id。"""
        data, err = wecom_qr_fetch()
        if not data:
            return self._err(503, err)
        return self._ok({
            "sid": data["sid"],
            "img": "/api/login/wecom/qrcode?sid=%s" % quote(data["sid"]),
            "poll": "/api/login/wecom/scan?sid=%s" % quote(data["sid"]),
        })

    def api_wecom_qrcode(self, query):
        """代理企微二维码图片。前端直接 <img src> 指向这里，避免跨域与防盗链问题。"""
        sid = (query.get("sid") or [""])[0]
        sess = wecom_qr_get_session(sid)
        if not sess:
            return self._err(404, "二维码会话已失效")
        img_url = sess.get("qr_url") or ""
        if not img_url:
            return self._err(404, "二维码地址已失效")
        ok, data, ctype = _http_bytes(img_url, referer=WECOM_BASE_URL + "/")
        if not ok:
            return self._err(502, "获取二维码图片失败")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def api_wecom_scan(self, query):
        """轮询扫码状态。扫成功后直接建号并把 token 返回给前端（不整页跳转）。"""
        sid = (query.get("sid") or [""])[0]
        last = (query.get("last") or [""])[0]
        data, err = wecom_qr_poll(sid, last)
        if not data:
            return self._err(404, err)
        status = data.get("status") or ""
        if status == "QRCODE_SCAN_SUCC":
            code = data.get("auth_code") or data.get("code") or ""
            if not code:
                return self._err(502, "扫码成功但未获得授权码")
            conn = db()
            res, lerr = wecom_login_user(conn, code)
            if not res:
                return self._err(502, lerr)
            # 一次性会话，用完即删
            with _wecom_qr_lock:
                _wecom_qr_sessions.pop(sid, None)
            return self._ok({"status": status, "token": res["token"], "user": res["user"]})
        if status == "QRCODE_SCAN_FAIL":
            # 用户在手机上点了"取消"
            with _wecom_qr_lock:
                _wecom_qr_sessions.pop(sid, None)
            return self._ok({"status": status})
        if status == "QRCODE_SCAN_ERR":
            # 二维码过期
            with _wecom_qr_lock:
                _wecom_qr_sessions.pop(sid, None)
            return self._ok({"status": status})
        return self._ok({"status": status or "QRCODE_SCAN_NEVER"})

    def api_wecom_exchange(self, body):
        """前端拿回跳里的一次性 code 换正式 token（code 用完即失效）。"""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        handoff = (data.get("code") or "").strip()
        if not handoff:
            return self._err(400, "缺少授权凭据")
        conn = db()
        row = conn.execute("SELECT * FROM wecom_auth WHERE code=?", (handoff,)).fetchone()
        if not row:
            return self._err(400, "授权凭据已失效，请重新登录")
        conn.execute("DELETE FROM wecom_auth WHERE code=?", (handoff,))
        conn.commit()
        token = row["userid"] or ""   # 这里存的是本地会话 token
        u = get_user_by_session(conn, token)
        if not u:
            return self._err(401, "登录状态已失效，请重新登录")
        return self._ok({
            "token": token,
            "user": {"id": u["id"], "username": u["username"], "name": u["name"],
                     "dept": u["dept"], "role": u["role"], "avatar": u["avatar"]},
        })

    def _redirect_with_error(self, msg):
        """出错时回首页并带上错误说明（前端弹提示）。"""
        back = "%s/?wecom_error=%s" % (WECOM_BASE_URL, quote(msg))
        self.send_response(302)
        self.send_header("Location", back)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def api_logout(self):
        tok = self._token()
        if tok:
            conn = db()
            conn.execute("DELETE FROM sessions WHERE token=?", (tok,))
            conn.commit()
        return self._ok({"bye": True})

    def api_me(self):
        u = self._me()
        if not u:
            return self._ok({"user": None})
        return self._ok({"user": {"id": u["id"], "username": u["username"], "name": u["name"],
                                  "dept": u["dept"], "role": u["role"], "avatar": u["avatar"]}})

    def api_change_pwd(self, body):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        old, new = data.get("old") or "", data.get("new") or ""
        if len(new) < 6:
            return self._err(400, "新密码至少 6 位")
        if hash_password(old, u["salt"]) != u["pwd_hash"]:
            return self._err(400, "旧密码不正确")
        conn = db()
        salt = new_salt()
        conn.execute("UPDATE users SET pwd_hash=?, salt=? WHERE id=?",
                     (hash_password(new, salt), salt, u["id"]))
        conn.commit()
        return self._ok({"changed": True})

    def api_me_update(self, body):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        name = data.get("name")
        dept = data.get("dept")
        avatar = data.get("avatar")
        sets, vals = [], []
        if name is not None:
            name = (name or "").strip()
            if not name:
                return self._err(400, "账号名称不能为空")
            sets.append("name=?"); vals.append(name)
        # 所属部门由企业微信通讯录同步，用户端不允许修改：即使传入 dept 也直接忽略
        if avatar is not None:
            if avatar and not avatar.startswith("data:image/"):
                return self._err(400, "头像格式不正确")
            if len(avatar or "") > 1_500_000:
                return self._err(400, "头像图片过大（请小于 1.5MB）")
            sets.append("avatar=?"); vals.append(avatar or "")
        if not sets:
            return self._err(400, "没有要修改的内容")
        conn = db()
        conn.execute("UPDATE users SET %s WHERE id=?" % ",".join(sets), vals + [u["id"]])
        conn.commit()
        new_name = name if name is not None else u["name"]
        new_dept = u["dept"]
        new_avatar = avatar if avatar is not None else u["avatar"]
        return self._ok({"user": {"id": u["id"], "username": u["username"], "name": new_name,
                                  "dept": new_dept, "role": u["role"], "avatar": new_avatar}})

    def api_favorite_toggle(self, tid):
        u = self._me()
        if not u:
            return self._err(401, "收藏需要先登录")
        conn = db()
        tool = conn.execute("SELECT id FROM tools WHERE id=? AND deleted=0", (tid,)).fetchone()
        if not tool:
            return self._err(404, "工具不存在")
        exists = conn.execute("SELECT 1 FROM favorites WHERE user_id=? AND tool_id=?",
                              (u["id"], tid)).fetchone()
        if exists:
            conn.execute("DELETE FROM favorites WHERE user_id=? AND tool_id=?", (u["id"], tid))
            conn.execute("UPDATE tools SET favorites = MAX(favorites - 1, 0) WHERE id=?", (tid,))
            saved = False
        else:
            conn.execute("INSERT INTO favorites(user_id,tool_id,seen_at) VALUES(?,?,datetime('now','localtime'))",
                         (u["id"], tid))
            conn.execute("UPDATE tools SET favorites = favorites + 1 WHERE id=?", (tid,))
            saved = True
        conn.commit()
        n = conn.execute("SELECT favorites v FROM tools WHERE id=?", (tid,)).fetchone()["v"]
        return self._ok({"saved": saved, "favorites": n})

    def api_favorite_seen(self, tid):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        conn = db()
        conn.execute("UPDATE favorites SET seen_at=datetime('now','localtime') WHERE user_id=? AND tool_id=?",
                     (u["id"], tid))
        conn.commit()
        return self._ok({"ok": True})

    def api_my(self, kind):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        conn = db()
        if kind == "tools":
            rows = conn.execute(
                "SELECT * FROM tools WHERE deleted=0 AND created_by=? ORDER BY id DESC",
                (u["id"],)).fetchall()
            items = [row_to_item(r, u["id"], conn) for r in rows]
            return self._ok({"items": items, "total": len(items), "updatedCount": 0})
        else:
            rows = conn.execute(
                "SELECT t.*, f.seen_at fav_seen, f.created_at fav_at FROM tools t JOIN favorites f ON f.tool_id=t.id"
                " WHERE f.user_id=? AND t.deleted=0 ORDER BY f.created_at DESC",
                (u["id"],)).fetchall()
            items, updated_count = [], 0
            for r in rows:
                it = row_to_item(r, u["id"], conn)
                seen = r["fav_seen"] or ""
                upd = r["updated_at"] or ""
                it["updated"] = bool(seen and upd and upd > seen)
                it["favAt"] = r["fav_at"] or ""
                it["updatedAt"] = upd
                if it["updated"]:
                    updated_count += 1
                items.append(it)
            return self._ok({"items": items, "total": len(items), "updatedCount": updated_count})

    # ── 评论 ──
    def api_comments(self, tid):
        conn = db()
        rows = conn.execute(
            "SELECT c.id,c.user_id,c.content,c.created_at,u.name author"
            " FROM comments c LEFT JOIN users u ON u.id=c.user_id"
            " WHERE c.tool_id=? ORDER BY c.created_at ASC, c.id ASC", (tid,)).fetchall()
        return self._ok({"items": [dict(r) for r in rows]})

    def api_comment_post(self, tid, body):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        content = (data.get("content") or "").strip()
        if not content:
            return self._err(400, "评论内容不能为空")
        if len(content) > 500:
            return self._err(400, "评论过长（最多 500 字）")
        conn = db()
        tool = conn.execute("SELECT id,created_by,name FROM tools WHERE id=? AND deleted=0", (tid,)).fetchone()
        if not tool:
            return self._err(404, "工具不存在")
        conn.execute("INSERT INTO comments(tool_id,user_id,content) VALUES(?,?,?)", (tid, u["id"], content))
        # 给工具上传者发一条消息（不能自己评自己）
        notified, notify_to = False, ""
        if tool["created_by"] and tool["created_by"] != u["id"]:
            conn.execute(
                "INSERT INTO messages(to_user_id,from_user_id,title,body,tool_id,comment_text) VALUES(?,?,?,?,?,?)",
                (tool["created_by"], u["id"], "新评论",
                 "评论了你的工具《%s》" % (tool["name"],), tid, content))
            notified = True
            row = conn.execute("SELECT name,username FROM users WHERE id=?",
                               (tool["created_by"],)).fetchone()
            notify_to = (row["name"] or row["username"]) if row else ""
        conn.commit()
        ts = conn.execute("SELECT datetime('now','localtime') t").fetchone()["t"]
        return self._ok({"id": conn.execute("SELECT last_insert_rowid() id").fetchone()["id"],
                         "author": u["name"] or u["username"], "content": content, "created_at": ts,
                         "notified": notified, "notify_to": notify_to})

    # ── 我的消息 ──
    def api_messages(self):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        conn = db()
        rows = conn.execute(
            "SELECT m.id,m.from_user_id,m.title,m.body,m.is_read,m.created_at,m.tool_id,m.comment_text,"
            " u.name from_name, u.avatar from_avatar, t.name tool_name, t.deleted tool_deleted"
            " FROM messages m LEFT JOIN users u ON u.id=m.from_user_id"
            " LEFT JOIN tools t ON t.id=m.tool_id"
            " WHERE m.to_user_id=? ORDER BY m.is_read ASC, m.created_at DESC", (u["id"],)).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            # 工具被删除时不允许跳转
            d["can_jump"] = bool(d.get("tool_id")) and not d.get("tool_deleted")
            if not d.get("tool_name") and d.get("tool_id"):
                d["tool_name"] = "（工具已删除）"
            items.append(d)
        return self._ok({"items": items})

    def api_messages_unread(self):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        n = db().execute("SELECT COUNT(*) c FROM messages WHERE to_user_id=? AND is_read=0", (u["id"],)).fetchone()["c"]
        return self._ok({"count": n})

    def api_message_read(self, mid):
        u = self._me()
        if not u:
            return self._err(401, "请先登录")
        conn = db()
        conn.execute("UPDATE messages SET is_read=1 WHERE id=? AND to_user_id=?", (mid, u["id"]))
        conn.commit()
        return self._ok({"ok": True})

    def api_admin_users_list(self):
        admin, err = self._require_admin()
        if err:
            return err
        conn = db()
        rows = conn.execute(
            "SELECT id,username,name,dept,role,status,created_at FROM users ORDER BY id").fetchall()
        return self._ok({"items": [dict(r) for r in rows], "total": len(rows)})

    def api_admin_user_create(self, body):
        admin, err = self._require_admin()
        if err:
            return err
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        conn = db()
        results = []
        users = data.get("users") if isinstance(data.get("users"), list) else [data]
        for it in users:
            username = (it.get("username") or "").strip()
            password = it.get("password") or ""
            if not username or not password:
                results.append({"username": username, "ok": False, "msg": "缺少用户名或密码"})
                continue
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                results.append({"username": username, "ok": False, "msg": "用户名已存在"})
                continue
            uid = make_user(conn, username, it.get("name") or username,
                            it.get("dept") or "", password, it.get("role") or "user")
            audit(conn, admin["id"], "create_user", "user:%d" % uid, "",
                  {"username": username, "name": it.get("name") or username})
            results.append({"username": username, "ok": True, "id": uid})
        conn.commit()
        return self._ok({"results": results})

    def api_admin_user_action(self, uid, action, body):
        admin, err = self._require_admin()
        if err:
            return err
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return self._err(404, "用户不存在")
        if action == "reset-pwd":
            try:
                new = (json.loads(body.decode("utf-8")) or {}).get("password") or ""
            except Exception:
                new = ""
            if len(new) < 6:
                return self._err(400, "新密码至少 6 位")
            salt = new_salt()
            conn.execute("UPDATE users SET pwd_hash=?, salt=? WHERE id=?",
                         (hash_password(new, salt), salt, uid))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            audit(conn, admin["id"], "reset_pwd", "user:%d" % uid, "", {"username": u["username"]})
            conn.commit()
            return self._ok({"reset": True})
        if action in ("disable", "enable"):
            status = "disabled" if action == "disable" else "active"
            conn.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
            if status == "disabled":
                conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            audit(conn, admin["id"], action, "user:%d" % uid, u["status"], status)
            conn.commit()
            return self._ok({"status": status})
        return self._err(404, "未知操作")

    def api_admin_counters(self, tid, body):
        admin, err = self._require_admin()
        if err:
            return err
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        conn = db()
        row = conn.execute("SELECT * FROM tools WHERE id=?", (tid,)).fetchone()
        if not row:
            return self._err(404, "工具不存在")
        before = {"downloads": row["downloads"], "likes": row["likes"], "favorites": row["favorites"]}
        after = dict(before)
        for k in ("downloads", "likes", "favorites"):
            if k in data:
                try:
                    after[k] = max(0, int(data[k]))
                except (TypeError, ValueError):
                    return self._err(400, "%s 必须是整数" % k)
        conn.execute(
            "UPDATE tools SET downloads=?, likes=?, favorites=? WHERE id=?",
            (after["downloads"], after["likes"], after["favorites"], tid))
        audit(conn, admin["id"], "set_counters", "tool:%d" % tid, before, after)
        conn.commit()
        return self._ok(after)

    def api_tool_update(self, tid, body):
        user = self._me()
        if not user:
            return self._err(401, "请先登录")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return self._err(400, "请求体不是合法 JSON")
        conn = db()
        row = conn.execute("SELECT * FROM tools WHERE id=? AND deleted=0", (tid,)).fetchone()
        if not row:
            return self._err(404, "工具不存在")
        # 管理员可改任何工具；普通用户只能更新自己上传的（created_by 为本人）
        if user["role"] != "admin" and row["created_by"] != user["id"]:
            return self._err(403, "只能更新自己上传的工具")
        before = row_to_item(row, user["id"], conn)
        cur = row_to_item(row, user["id"], conn)
        for k_src, k_dst in (("name", "name"), ("dept", "dept"), ("type", "type"),
                             ("desc", "desc"), ("link", "link")):
            if k_src in data:
                cur[k_dst] = data[k_src]
        if "detail" in data and isinstance(data["detail"], dict):
            cur["detail"].update({k: v for k, v in data["detail"].items()
                                  if k in ("intro", "usage", "highlights")})
        # 附件全量替换（编辑时移除旧附件 / 新增附件后整体回传）
        if "files" in data and isinstance(data["files"], list):
            cur["files"] = [
                {"name": f.get("name"), "size": f.get("size"), "type": f.get("type"), "url": f.get("url")}
                for f in data["files"] if isinstance(f, dict) and f.get("name")
            ]
        if cur["type"] not in VALID_TYPES:
            cur["type"] = "其他"
        conn.execute(
            "UPDATE tools SET name=?, dept=?, type=?, desc=?, detail=?, link=?, files=?, updated_at=datetime('now','localtime') WHERE id=?",
            (cur["name"], cur["dept"], cur["type"], cur["desc"],
             json.dumps(cur["detail"], ensure_ascii=False), cur["link"],
             json.dumps(cur["files"], ensure_ascii=False), tid))
        audit(conn, user["id"], "edit_tool", "tool:%d" % tid, before, cur)
        conn.commit()
        new_row = conn.execute("SELECT * FROM tools WHERE id=?", (tid,)).fetchone()
        return self._ok(row_to_item(new_row, user["id"], conn))

    def api_tool_delete(self, tid):
        user = self._me()
        if not user:
            return self._err(401, "请先登录")
        conn = db()
        row = conn.execute("SELECT name, created_by FROM tools WHERE id=? AND deleted=0", (tid,)).fetchone()
        if not row:
            return self._err(404, "工具不存在")
        # 管理员可删任意工具；普通用户只能删自己上传的
        if user["role"] != "admin" and row["created_by"] != user["id"]:
            return self._err(403, "只能删除自己上传的工具")
        conn.execute("UPDATE tools SET deleted=1 WHERE id=?", (tid,))
        audit(conn, user["id"], "delete_tool", "tool:%d" % tid, row["name"], "")
        conn.commit()
        log("工具 #%d「%s」已移入回收站（by %s）" % (tid, row["name"], user["name"]))
        return self._ok({"deleted": True})

    def api_admin_deleted(self):
        admin, err = self._require_admin()
        if err:
            return err
        conn = db()
        rows = conn.execute("SELECT * FROM tools WHERE deleted=1 ORDER BY id DESC").fetchall()
        return self._ok({"items": [row_to_item(r, admin["id"], conn) for r in rows], "total": len(rows)})

    def api_tool_restore(self, tid):
        admin, err = self._require_admin()
        if err:
            return err
        conn = db()
        row = conn.execute("SELECT name FROM tools WHERE id=? AND deleted=1", (tid,)).fetchone()
        if not row:
            return self._err(404, "回收站中没有该工具")
        conn.execute("UPDATE tools SET deleted=0 WHERE id=?", (tid,))
        audit(conn, admin["id"], "restore_tool", "tool:%d" % tid, "", row["name"])
        conn.commit()
        return self._ok({"restored": True})

    def api_admin_audit(self):
        admin, err = self._require_admin()
        if err:
            return err
        conn = db()
        rows = conn.execute(
            "SELECT a.*, u.name admin_name FROM audit_log a LEFT JOIN users u ON u.id=a.admin_user_id"
            " ORDER BY a.id DESC LIMIT 100").fetchall()
        return self._ok({"items": [dict(r) for r in rows], "total": len(rows)})

    def serve_upload(self, name):
        name = os.path.basename(name)
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path):
            return self._err(404, "文件不存在")
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
            return self._err(403, "禁止访问")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(real, "rb") as f:
            data = f.read()
        fallback = name.encode("ascii", "replace").decode("ascii").replace('"', "")
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quote(name))
        return self._send(200, data, ctype, {"Content-Disposition": disp})

    def serve_tool_file(self, name):
        """提供随包分发的真实工具原件（tools/ 目录，只读）。

        与 serve_upload 分开：这里放的是交付包自带的真实工具文件，
        不依赖运行数据目录，重装/换机器都还在。
        """
        name = os.path.basename(unquote(name))
        path = os.path.join(TOOLS_DIR, name)
        if not os.path.isfile(path):
            return self._err(404, "文件不存在")
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(TOOLS_DIR) + os.sep):
            return self._err(403, "禁止访问")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(real, "rb") as f:
            data = f.read()
        fallback = name.encode("ascii", "replace").decode("ascii").replace('"', "")
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quote(name))
        return self._send(200, data, ctype, {"Content-Disposition": disp})

    def serve_static(self, p):
        if p in ("/", ""):
            p = "/index.html"
        rel = p.lstrip("/")
        path = os.path.join(WEB_DIR, rel)
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(WEB_DIR) + os.sep):
            return self._err(403, "禁止访问")
        if not os.path.isfile(real):
            return self._err(404, "页面不存在：%s" % p)
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(real, "rb") as f:
            data = f.read()
        cache = "no-cache" if real.endswith(".html") else "public, max-age=3600"
        return self._send(200, data, ctype, {"Cache-Control": cache})

def main():
    reseed = "--reseed" in sys.argv
    clear = "--clear" in sys.argv
    if "--help" in sys.argv or "-h" in sys.argv:
        print("命令行参数：")
        print("  --reseed   清空工具表并按 seed/seed_tools.json 重新导入种子数据")
        print("  --clear    清空工具表且【不】导入种子数据（上线前清掉演示占位数据用）")
        return
    n = init_db(reseed=reseed, clear=clear)
    wecom_on = load_wecom_config()
    log("=" * 62)
    log("AI 成果库 · 轻量后端已启动（单线程模式）")
    log("  监听地址   : http://%s:%d" % (HOST, PORT))
    log("  前端页面   : %s" % WEB_DIR)
    log("  数据目录   : %s" % DATA_DIR)
    log("  数据库     : %s（%d 个工具）" % (DB_PATH, n))
    log("  附件上限   : %d MB/文件" % MAX_UPLOAD_MB)
    if wecom_on:
        log("  企微登录   : 已启用（CorpID=%s…，对外 %s）"
            % (WECOM_CORP_ID[:6], WECOM_BASE_URL))
        log("  上报回调域 : %s（企微后台「授权回调域」填 %s）"
            % (wecom_redirect_uri(), wecom_redirect_host() or "（未配置）"))
    else:
        log("  企微登录   : 未启用（%s）——登录页只显示账号密码登录"
            % (wecom_status()["reason"] or "缺少配置"))
    log("  健康检查   : http://%s:%d/api/health" % (HOST, PORT))
    log("=" * 62)

    httpd = HTTPServer((HOST, PORT), Handler)
    httpd.allow_reuse_address = True

    def shutdown(signum, frame):
        log("收到退出信号，正在关闭…")
        # 直接终止：所有写操作都是即时 commit + WAL 模式，不存在半途数据损坏，
        # 硬退出是单线程模式下最确定的做法（systemd / docker stop 均可正常停止）
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever()
    except SystemExit:
        pass
    finally:
        httpd.server_close()
        log("服务已停止")

if __name__ == "__main__":
    main()
