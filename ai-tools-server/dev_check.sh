#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  企业微信扫码登录 · 后台配置自检
#
#  用途：填完企微后台两处设置后，一键验证是否全部就绪。
#  做法：直接调后端的 /api/login/wecom/diagnose（真实探测企微接口），
#        后端把「该改哪一项」直接算好输出，这里只做展示。
#
#  本机联调与服务器通用：自动识别后端端口
#    优先级：$PORT 环境变量 > systemd 单元文件 > nginx vhost > 8080 > 8888
#
#  用法：bash dev_check.sh            （在本包目录下跑，自动找后端）
#        bash /opt/ai-tools-server/deploy/check.sh   （服务器上跑）
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail
cd "$(dirname "$0")"

# ── 定位后端端口 ─────────────────────────────────────────────────
detect_port() {
  # 1) 环境变量
  [ -n "${PORT:-}" ] && { echo "$PORT"; return; }
  # 2) systemd 单元文件
  for f in /etc/systemd/system/ai-tools.service; do
    [ -f "$f" ] || continue
    p=$(grep -oE '^Environment=PORT=[0-9]+' "$f" | head -1 | cut -d= -f3)
    [ -n "$p" ] && { echo "$p"; return; }
  done
  # 3) nginx vhost 里本域名的 upstream 端口
  for f in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/*; do
    [ -f "$f" ] || continue
    p=$(awk '/proxy_pass/ { if (match($0,/:[0-9]+/)) { print substr($0,RSTART+1,RLENGTH-1); exit } }' "$f" 2>/dev/null)
    [ -n "$p" ] && { echo "$p"; return; }
  done
  # 4) 常见端口试探
  for p in 8080 8888; do
    curl -s -m 2 -o /dev/null "http://127.0.0.1:$p/api/health" 2>/dev/null && { echo "$p"; return; }
  done
  echo 8888
}

BACKEND_PORT=$(detect_port)
LOCAL="http://127.0.0.1:$BACKEND_PORT"

CORP_ID="${WECOM_CORP_ID:-}"; AGENT_ID="${WECOM_AGENT_ID:-}"; SECRET="${WECOM_SECRET:-}"
BASE_URL="${WECOM_BASE_URL:-}"
URL_FILE="/tmp/cf_url.txt"

if [ -f wecom_config.json ] && command -v python3 >/dev/null 2>&1; then
  eval "$(python3 - <<'PY'
import json, shlex
try: c=json.load(open('wecom_config.json',encoding='utf-8'))
except Exception: c={}
for k,env in [('corp_id','CORP_ID'),('agent_id','AGENT_ID'),('secret','SECRET'),('base_url','BASE_URL')]:
    v=(c.get(k) or '').strip()
    if v: print('%s=%s' % (env, shlex.quote(v)))
PY
)"
fi
[ -z "$BASE_URL" ] && [ -f "$URL_FILE" ] && BASE_URL=$(cat "$URL_FILE")

ok=0; fail=0
chk() { if [ "$2" = "1" ]; then echo -e "  ✅ $1"; ok=$((ok+1)); else echo -e "  ❌ $1"; fail=$((fail+1)); fi; }

echo "════════ 企业微信扫码登录 · 就绪自检 ════════"
echo "  后端地址：$LOCAL"
echo

# ① 凭据
echo "① 应用凭据"
chk "CorpID 已配置"  "$([ -n "$CORP_ID" ] && echo 1 || echo 0)"
chk "AgentId 已配置" "$([ -n "$AGENT_ID" ] && echo 1 || echo 0)"
chk "Secret 已配置"  "$([ -n "$SECRET" ] && echo 1 || echo 0)"
chk "base_url 已配置" "$([ -n "$BASE_URL" ] && echo 1 || echo 0)"
echo

# ② 后端服务
echo "② 后端服务"
ST=$(curl -s -m 8 "$LOCAL/api/login/wecom/status" 2>/dev/null)
chk "服务在线且企微已启用" "$(echo "$ST" | grep -q '"enabled": true' && echo 1 || echo 0)"
if [ -n "$BASE_URL" ]; then
  chk "公网可达" "$(curl -s -m 15 "$BASE_URL/api/health" >/dev/null 2>&1 && echo 1 || echo 0)"
fi
echo

# ③④ 后端体检接口：授权回调域 / 可信IP / 对外可达（真实探测企微）
echo "③ 企微后台配置体检"
DIAG=$(curl -s -m 40 "$LOCAL/api/login/wecom/diagnose" 2>/dev/null)
if [ -z "$DIAG" ]; then
  echo "  ⚠️  体检接口无响应（后端没起来？）"
  fail=$((fail+1))
else
  echo "$DIAG" | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin).get("data") or {}
except Exception as e:
    print("  ⚠️  体检返回无法解析：%s" % e); raise SystemExit(0)
print("     当前 redirect_uri：%s" % (d.get("redirect_uri") or "（未配置）"))
print("     需要登记的域名：  %s" % (d.get("redirect_host") or "（未配置）"))
if d.get("egress_ip"):
    print("     企微看到的出口IP：%s  ← 填到「应用管理→自建应用(1000196)→应用详情页→企业可信 IP→配置」" % d["egress_ip"])
print()
for it in d.get("items") or []:
    print(("  ✅ " if it.get("ok") else "  ❌ ") + (it.get("title") or ""))
    det = it.get("detail") or ""
    if det: print("     " + det)
    act = it.get("action") or ""
    if act and not it.get("ok"): print("     → " + act)
todo = d.get("todo") or []
if todo:
    print()
    print("  还要做的事（不用重新部署，改完即生效）：")
    for i, t in enumerate(todo, 1):
        print("   %d) %s" % (i, t))
'
  if echo "$DIAG" | python3 -c '
import json,sys
d=json.load(sys.stdin).get("data") or {}
keys=("credentials","callback_domain","trusted_ip")
rel=[i for i in (d.get("items") or []) if i.get("key") in keys]
sys.exit(0 if rel and all(i.get("ok") for i in rel) else 1)
' 2>/dev/null; then ok=$((ok+1)); else fail=$((fail+1)); fi
fi
echo

echo "════════ 结果：通过 $ok 项，失败 $fail 项 ════════"
if [ "$fail" = "0" ]; then
  echo "✅ 全部就绪，可以用手机企微扫码了"
else
  echo "⚠️  还有 $fail 项未就绪，按上面提示到企微后台补齐后重跑本脚本"
  echo "   （改后台设置或改 wecom_config.json 都即时生效，不需要重新部署）"
fi
