#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  企业微信扫码登录 · 本机联调脚本
#
#  用途：在开发机上把服务暴露成公网 https 域名，验证企业微信扫码全链路
#        （扫码 → 回调 → 建号 → 换 token → 进系统）
#
#  原理：企微扫码授权后，是【浏览器】跳回 redirect_uri（企微 JS 里
#        window.location.href = redirect_uri），不是企微服务器推送。
#        所以只要本机浏览器能访问到回调地址即可 → 用隧道把 8888 暴露成 https。
#
#  用法：
#    bash dev_tunnel.sh start     启动隧道 + 服务，打印域名
#    bash dev_tunnel.sh status    查看状态
#    bash dev_tunnel.sh stop      停止隧道
#    bash dev_tunnel.sh logs      看服务日志
#
#  凭据：优先读环境变量，其次读同目录 wecom_config.json
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8888}"
TUNNEL_BIN="${TUNNEL_BIN:-/tmp/cloudflared}"
TUNNEL_LOG="/tmp/cf_tunnel.log"
SERVER_LOG="/tmp/ai_server.log"
URL_FILE="/tmp/cf_url.txt"

# ── 读企微凭据 ────────────────────────────────────────────────
CORP_ID="${WECOM_CORP_ID:-}"; AGENT_ID="${WECOM_AGENT_ID:-}"
SECRET="${WECOM_SECRET:-}";    BASE_URL="${WECOM_BASE_URL:-}"
if [ -f wecom_config.json ] && command -v python3 >/dev/null 2>&1; then
  eval "$(python3 - <<'PY'
import json, os, shlex
try:
    c = json.load(open('wecom_config.json', encoding='utf-8'))
except Exception:
    c = {}
for k, env in [('corp_id','CORP_ID'),('agent_id','AGENT_ID'),('secret','SECRET'),('base_url','BASE_URL')]:
    v = (c.get(k) or '').strip()
    if v:
        print(f'{env}="${{{env}:-{shlex.quote(v)}}}"')
PY
)"
fi

say()  { echo -e "\033[1;36m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
die()  { echo -e "\033[1;31m[x]\033[0m $*"; exit 1; }

cmd_stop() {
  say "停止服务与隧道"
  lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill 2>/dev/null
  pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null
  rm -f "$URL_FILE"
  echo "  已停止"
}

cmd_status() {
  local up="no"
  [ -f "$URL_FILE" ] && up=$(cat "$URL_FILE")
  say "服务端口 $PORT：$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && echo '运行中' || echo '未运行')"
  say "隧道：$(pgrep -f "cloudflared tunnel --url" >/dev/null 2>&1 && echo '运行中' || echo '未运行')"
  [ -n "$up" ] && echo "    公网地址: $up"
  echo
  curl -s -m 8 "http://127.0.0.1:$PORT/api/login/wecom/status" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "（服务未响应）"
}

cmd_start() {
  # 1. 隧道二进制
  if [ ! -x "$TUNNEL_BIN" ]; then
    say "下载 cloudflared"
    local arch; arch="$(uname -m)"
    local a="amd64"; [ "$arch" = "arm64" ] && a="arm64"
    curl -sL -o /tmp/cf.tgz "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$a.tgz" \
      || die "下载失败"
    tar xzf /tmp/cf.tgz -C /tmp && chmod +x /tmp/cloudflared || die "解压失败"
  fi

  # 2. 凭据检查
  if [ -z "$CORP_ID" ] || [ -z "$AGENT_ID" ] || [ -z "$SECRET" ]; then
    die "缺少企微凭据：请设 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET，或填 wecom_config.json"
  fi

  # 3. 旧进程清干净
  lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill 2>/dev/null
  pkill -f "cloudflared tunnel --url" 2>/dev/null
  sleep 1

  # 4. 起隧道
  say "启动隧道 → 127.0.0.1:$PORT"
  rm -f "$TUNNEL_LOG"
  nohup "$TUNNEL_BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > "$TUNNEL_LOG" 2>&1 &
  local url=""
  for i in $(seq 1 30); do
    sleep 1
    url=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$TUNNEL_LOG" 2>/dev/null | head -1)
    [ -n "$url" ] && break
  done
  [ -z "$url" ] && die "隧道启动失败，见 $TUNNEL_LOG"
  echo "$url" > "$URL_FILE"
  say "公网地址：$url"

  # 5. 起服务（注入企微配置）
  say "启动后端（企微登录已启用）"
  WECOM_CORP_ID="$CORP_ID" WECOM_AGENT_ID="$AGENT_ID" WECOM_SECRET="$SECRET" \
  WECOM_BASE_URL="$url" PORT="$PORT" \
    nohup python3 server.py > "$SERVER_LOG" 2>&1 &
  sleep 3

  # 6. 三重验证
  say "验证"
  local st
  st=$(curl -s -m 8 "http://127.0.0.1:$PORT/api/login/wecom/status")
  echo "$st" | grep -q '"enabled": true' \
    && echo "    ✅ 企微登录已启用" \
    || { echo "    ❌ 未启用：$st"; die "检查 $SERVER_LOG"; }

  curl -s -m 15 "$url/api/health" >/dev/null 2>&1 \
    && echo "    ✅ 公网可达" || warn "公网探测失败（可能是隧道刚建立，稍后再试）"

  # 7. 输出人话指引
  local host="${url#https://}"
  cat <<EOF

  ────────────────────────────────────────────────────────
  现在去【企业微信管理后台】填两处（需要管理员权限）：

  ① 应用管理 → 自建应用 → 你的应用(AgentId $AGENT_ID)
     → 企业微信授权登录 → 设置 → 【设置授权回调域】
     → 填：  $host

  ② 企业可信IP → 添加
     → 填：  $(curl -s -m 6 https://ifconfig.me 2>/dev/null)
     （本机出口 IP；不加也能登录，但姓名/部门会退化成 userid）

  然后在【手机企业微信】里：
     打开 $url
     点「使用企业微信扫码登录」→ 用手机企微扫 → 授权
  ────────────────────────────────────────────────────────

  本机直连：http://127.0.0.1:$PORT
  日志：$SERVER_LOG
EOF
}

case "${1:-start}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  logs)   tail -f "$SERVER_LOG" ;;
  *)      echo "用法: bash dev_tunnel.sh {start|stop|status|logs}"; exit 1 ;;
esac
