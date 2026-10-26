#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  企业微信扫码登录 · 本机联调守护（launchd 版）
#
#  为什么需要守护：cloudflared 的临时域名（xxx.trycloudflare.com）每次
#  重启都会变，而企微后台「授权回调域」只能填一个、必须完全一致。
#  所以隧道必须【稳定常驻】，域名才能在测试期间保持不变。
#
#  用法：
#    bash dev_guard.sh install    安装守护（隧道+服务常驻，域名固定）
#    bash dev_guard.sh status     查看状态
#    bash dev_guard.sh url        打印公网地址
#    bash dev_guard.sh uninstall  卸载守护
#    bash dev_guard.sh logs       看服务日志
#
#  凭据来源：环境变量 > 同目录 wecom_config.json
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8888}"
LABEL="com.local.aitools.dev"
TUNNEL_BIN="$HOME/.local/bin/cloudflared"
TUNNEL_LOG="/tmp/cf_tunnel.log"
SERVER_LOG="/tmp/ai_server.log"
URL_FILE="/tmp/cf_url.txt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say()  { echo -e "\033[1;36m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
die()  { echo -e "\033[1;31m[x]\033[0m $*"; exit 1; }

# ── 读凭据（环境变量优先，其次 wecom_config.json）────────────────
CORP_ID="${WECOM_CORP_ID:-}"; AGENT_ID="${WECOM_AGENT_ID:-}"
SECRET="${WECOM_SECRET:-}";    BASE_URL="${WECOM_BASE_URL:-}"
REDIRECT="${WECOM_REDIRECT:-}"
if [ -f wecom_config.json ] && command -v python3 >/dev/null 2>&1; then
  eval "$(python3 - <<'PY'
import json, os, shlex
try:
    c = json.load(open('wecom_config.json', encoding='utf-8'))
except Exception:
    c = {}
for k, env in [('corp_id','CORP_ID'),('agent_id','AGENT_ID'),('secret','SECRET'),
               ('base_url','BASE_URL'),('redirect','REDIRECT')]:
    v = (c.get(k) or '').strip()
    if v:
        print('%s="$%s"' % (env, shlex.quote(v)))
PY
)"
fi

ensure_tunnel_bin() {
  [ -x "$TUNNEL_BIN" ] && return 0
  say "安装 cloudflared 到 $TUNNEL_BIN"
  local arch a; arch="$(uname -m)"; a="amd64"; [ "$arch" = "arm64" ] && a="arm64"
  mkdir -p "$(dirname "$TUNNEL_BIN")"
  curl -sL -o /tmp/cf.tgz "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$a.tgz" \
    || die "下载失败"
  tar xzf /tmp/cf.tgz -C /tmp && install -m 0755 /tmp/cloudflared "$TUNNEL_BIN" || die "安装失败"
}

cmd_install() {
  ensure_tunnel_bin
  [ -n "$CORP_ID" ] && [ -n "$AGENT_ID" ] && [ -n "$SECRET" ] \
    || die "缺凭据：请设 WECOM_CORP_ID/WECOM_AGENT_ID/WECOM_SECRET，或填 wecom_config.json"

  cmd_uninstall >/dev/null 2>&1

  mkdir -p "$HOME/Library/LaunchAgents"
  # 两个独立任务：隧道（先起）与服务（后起，服务需要隧道域名 → 用 wrapper 等域名）
  cat > "$HOME/Library/LaunchAgents/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PWD/dev_guard.sh</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/aitools_guard.log</string>
  <key>StandardErrorPath</key><string>/tmp/aitools_guard_err.log</string>
  <key>WorkingDirectory</key><string>$PWD</string>
</dict>
</plist>
PLIST

  say "加载 launchd 任务"
  launchctl unload "$PLIST" 2>/dev/null
  launchctl load "$PLIST" 2>/dev/null || launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
    || die "launchctl 加载失败"

  # 等域名与就绪
  say "等待隧道与后端就绪…"
  local url="" ready=0
  for i in $(seq 1 60); do
    sleep 2
    url=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$TUNNEL_LOG" 2>/dev/null | head -1)
    if [ -n "$url" ] && curl -s -m 6 "http://127.0.0.1:$PORT/api/login/wecom/status" 2>/dev/null | grep -q '"enabled": true'; then
      ready=1; break
    fi
  done
  [ "$ready" = 1 ] || die "启动未就绪，见 /tmp/aitools_guard_err.log"

  echo "$url" > "$URL_FILE"
  say "就绪"

  local host="${url#https://}"
  # 授权回调域只填域名（不带协议/路径）。若配置了 redirect，则以它为准 ——
  # 正式域名在后台登记一次，本机联调/线上切换都不用再改后台。
  local cbhost="$host"
  [ -n "$REDIRECT" ] && cbhost=$(printf '%s' "$REDIRECT" | sed -E 's#^[a-zA-Z]+://##; s#/.*$##')
  local ip; ip=$(curl -s -m 6 https://ifconfig.me 2>/dev/null)
  cat <<EOF

  ════════════════════════════════════════════════════════════════
  公网地址（手机企微里打开这个）：
      $url

  【现在需要你去企微管理后台填两处】
  ① 应用管理 → 自建应用 → AgentId $AGENT_ID
     → 企业微信授权登录 → 设置 → 设置授权回调域
     → 填： $cbhost
        （只填域名，不带协议和路径）

  ② 企业可信IP → 添加
     → 填： $ip
        （不加则扫码后换身份也会被拒，登录不进来）

  【然后】
  手机企业微信 → 打开上面那个地址 → 点「登录」→ 扫码授权
  ════════════════════════════════════════════════════════════════

  本机直连：http://127.0.0.1:$PORT
  服务日志：$SERVER_LOG
  隧道日志：$TUNNEL_LOG
EOF
}

# launchd 内部调用：起隧道 → 等域名 → 起服务
cmd_run() {
  mkdir -p "$(dirname "$TUNNEL_LOG")"
  # 隧道
  "$TUNNEL_BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > "$TUNNEL_LOG" 2>&1 &
  local tp=$!
  # 等域名
  local url=""
  for i in $(seq 1 45); do
    sleep 1
    url=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$TUNNEL_LOG" 2>/dev/null | head -1)
    [ -n "$url" ] && break
  done
  [ -z "$url" ] && { echo "隧道启动失败" >&2; exit 1; }
  echo "$url" > "$URL_FILE"
  # 服务
  WECOM_CORP_ID="$CORP_ID" WECOM_AGENT_ID="$AGENT_ID" WECOM_SECRET="$SECRET" \
  WECOM_BASE_URL="$url" WECOM_REDIRECT="$REDIRECT" PORT="$PORT" \
    python3 server.py > "$SERVER_LOG" 2>&1 &
  # 守护：任一子进程退出则整体重启
  wait -n
  kill $tp 2>/dev/null
  lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill 2>/dev/null
  sleep 2
}

cmd_status() {
  echo "隧道进程：$(pgrep -f 'cloudflared tunnel --url' >/dev/null && echo 运行中 || echo 未运行)"
  echo "服务进程：$(lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && echo 运行中 || echo 未运行)"
  echo "launchd  ：$(launchctl list 2>/dev/null | grep -q "$LABEL" && echo 已加载 || echo 未加载)"
  [ -f "$URL_FILE" ] && echo "公网地址：$(cat "$URL_FILE")"
  echo
  curl -s -m 6 "http://127.0.0.1:$PORT/api/login/wecom/status" 2>/dev/null | python3 -m json.tool 2>/dev/null \
    || echo "（服务无响应）"
}

cmd_uninstall() {
  launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  pkill -f "cloudflared tunnel --url" 2>/dev/null
  lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill 2>/dev/null
  rm -f "$URL_FILE"
  echo "已卸载"
}

case "${1:-install}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  url)       cat "$URL_FILE" 2>/dev/null || echo "未启动" ;;
  logs)      tail -f "$SERVER_LOG" ;;
  run)       cmd_run ;;
  *) echo "用法: bash dev_guard.sh {install|uninstall|status|url|logs}"; exit 1 ;;
esac
