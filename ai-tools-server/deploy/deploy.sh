#!/usr/bin/env bash
# ============================================================
# AI 成果库 · 一键部署脚本（在服务器上执行）
#   用法：  bash deploy.sh <CorpID> <AgentId> <Secret> [base_url] [redirect]
#   示例：  bash deploy.sh ww1234567890 1000002 abcdefsecret https://aitoolbox.maihaoche.com
#
# 特性：
#   - 自动从 nginx 现有 vhost 读出 upstream 端口，不用手猜
#   - 幂等：可重复执行；已有 data/ 不会覆盖
#   - 不动 nginx 配置（域名/证书已配好），只保证后端监听到 nginx 指的那个端口
#   - 部署完自动跑一次企微后台三项体检，把「还要做什么」直接打印出来
#
# ★ 部署后如果企微登录不通，**不需要重新部署**：
#   · 企微后台两项（授权回调域 / 企业可信 IP）→ 改后台即刻生效
#   · 本机配置（corp_id/secret/base_url…）→ 改 wecom_config.json 即刻生效（已做热重载）
#   改完直接重跑 `bash deploy/check.sh` 复核即可。
# ============================================================
set -uo pipefail

APP_DIR=/opt/ai-tools-server
SERVICE=ai-tools
SVC_FILE=/etc/systemd/system/ai-tools.service
DOMAIN=aitoolbox.maihaoche.com

say()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[警告] $*\033[0m"; }
die()  { echo -e "\033[1;31m[错误] $*\033[0m"; exit 1; }

CORP_ID="${1:-}"
AGENT_ID="${2:-}"
SECRET="${3:-}"
BASE_URL="${4:-https://$DOMAIN}"
# redirect 留空 = 自动取 {base_url}/api/login/wecom/callback，域名即 $DOMAIN。
# 正式部署不需要单独指定，企微后台「授权回调域」就填 $DOMAIN。
REDIRECT="${5:-}"

# ---------- 0. 参数检查 ----------
[ -n "$CORP_ID" ] && [ -n "$AGENT_ID" ] && [ -n "$SECRET" ] \
  || die "用法: bash deploy.sh <CorpID> <AgentId> <Secret> [base_url]"

say "参数"
echo "  CorpID  : $CORP_ID"
echo "  AgentId : $AGENT_ID"
echo "  Secret  : ${SECRET:0:6}********"
echo "  base_url: $BASE_URL"
echo "  redirect: ${REDIRECT:-（自动 = ${BASE_URL}/api/login/wecom/callback）}"

# ---------- 1. 环境检查 ----------
say "检查运行环境"
command -v python3 >/dev/null || die "没有 python3，请先安装"
PYV=$(python3 -V 2>&1 | awk '{print $2}')
echo "  python3 : $PYV"
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' \
  || die "python3 版本需 ≥ 3.8（当前 $PYV）"
python3 -c 'import sqlite3' || die "缺 sqlite3 模块"
command -v systemctl >/dev/null || warn "没有 systemctl，将改用 nohup 方式启动"
command -v nginx >/dev/null && echo "  nginx   : $(nginx -v 2>&1)" || warn "没找到 nginx 命令"

# ---------- 2. 出网连通性（企微接口必须能通，否则扫码必然失败） ----------
say "检查到企业微信的网络连通性"
WECOM_REACH=0
for u in "https://qyapi.weixin.qq.com/cgi-bin/gettoken" \
         "https://open.work.weixin.qq.com/wwopen/sso/qrConnect"; do
  if curl -s -m 10 -o /dev/null "$u"; then
    echo "  ✅ 可达 $(echo "$u" | cut -d/ -f3)"
    WECOM_REACH=$((WECOM_REACH+1))
  else
    echo "  ❌ 不可达 $(echo "$u" | cut -d/ -f3)"
  fi
done
[ "$WECOM_REACH" -lt 2 ] && warn "部分企微接口不可达，服务器可能需要配代理/白名单，先解决再扫码"

# ---------- 3. 出口 IP（填企微「企业可信IP」用） ----------
say "本机公网出口 IP（这就是要填进企微后台「企业可信IP」的地址）"
OUT_IP=$(curl -s -m 8 https://ifconfig.me 2>/dev/null || true)
[ -z "$OUT_IP" ] && OUT_IP=$(curl -s -m 8 https://api.ipify.org 2>/dev/null || true)
if [ -n "$OUT_IP" ]; then
  echo "  $OUT_IP"
  echo -e "  \033[1;33m请把这个 IP 加到企微后台「企业可信IP」\033[0m"
else
  warn "自动获取失败，手动执行: curl -s https://ifconfig.me"
fi

# ---------- 4. 从 nginx 读出 upstream 端口 ----------
say "读取 nginx 里已配置的 upstream 端口"
UPSTREAM_PORT=""
for f in /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/* /etc/nginx/nginx.conf; do
  [ -f "$f" ] || continue
  hit=$(grep -l "$DOMAIN" "$f" 2>/dev/null) && [ -n "$hit" ] || continue
  echo "  命中配置文件: $f"
  # 只取属于本域名的 server 块里的 proxy_pass
  p=$(awk -v d="$DOMAIN" '
    $0 ~ "server_name" && index($0, d) { ind=1 }
    ind && /proxy_pass/ {
      if (match($0, /:[0-9]+/)) { print substr($0, RSTART+1, RLENGTH-1); exit }
    }
  ' "$f")
  [ -n "$p" ] && { UPSTREAM_PORT="$p"; break; }
done

if [ -z "$UPSTREAM_PORT" ]; then
  UPSTREAM_PORT=8080
  warn "nginx 里没读到本域名的 proxy_pass，默认用 8080。"
  warn "若 nginx 实际指向别的端口，请改 APP_DIR 下的启动端口并同步 nginx。"
else
  echo "  upstream 端口 = $UPSTREAM_PORT  ✅（自动读出，与 nginx 一致）"
fi

# 检查端口占用
if command -v ss >/dev/null && ss -ltnp 2>/dev/null | grep -q ":$UPSTREAM_PORT "; then
  echo "  端口 $UPSTREAM_PORT 当前已被占用："
  ss -ltnp 2>/dev/null | grep ":$UPSTREAM_PORT " | sed 's/^/    /'
  echo "  → 若占用者就是本服务（旧进程），继续重启即可；"
  echo "    若占用者是别的服务，需要换端口并同步 nginx 的 proxy_pass。"
fi

# ---------- 5. 上传代码（若已上传则跳过） ----------
say "准备代码目录 $APP_DIR"
mkdir -p "$APP_DIR"
if [ -f "$APP_DIR/server.py" ]; then
  echo "  已有 server.py，跳过代码上传（若要更新请手动 scp 覆盖后重跑本脚本）"
else
  echo -e "\033[1;33m  代码还没上传到 $APP_DIR。请先执行：\033[0m"
  echo -e "    scp 'ai-tools-server.zip' user@<服务器IP>:/tmp/"
  echo -e "    cd /tmp && unzip -q ai-tools-server.zip && cp -r 'ai-tools-server 2'/* $APP_DIR/"
  echo -e "    bash deploy.sh $CORP_ID $AGENT_ID <Secret> $BASE_URL"
  exit 2
fi

# ---------- 6. 写企微配置 ----------
say "写入企微配置"
cat > "$APP_DIR/wecom_config.json" <<EOF
{
  "_说明": "企业微信扫码登录配置。修改后无需重启服务，下次请求会自动重新读取。",
  "corp_id": "$CORP_ID",
  "agent_id": "$AGENT_ID",
  "secret": "$SECRET",
  "base_url": "$BASE_URL",
  "redirect": "$REDIRECT",
  "_admin_userids说明": "管理员白名单：把你的企微 userid 填进来（数组，可多个）。这些 userid 扫码后自动是本系统管理员，保存即生效。",
  "admin_userids": []
}
EOF
chmod 600 "$APP_DIR/wecom_config.json"
echo "  已写入 $APP_DIR/wecom_config.json（权限 600）"

# ---------- 7. systemd 服务 ----------
say "配置 systemd 服务"
RUN_USER="aitools"
id "$RUN_USER" >/dev/null 2>&1 || { useradd -r -s /sbin/nologin "$RUN_USER" 2>/dev/null; echo "  已创建运行用户 $RUN_USER"; }
mkdir -p "$APP_DIR/data"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

cat > "$SVC_FILE" <<EOF
[Unit]
Description=AI 成果库 · 内部 AI 工具平台
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
User=$RUN_USER
Group=$RUN_USER
Environment=HOST=127.0.0.1
Environment=PORT=$UPSTREAM_PORT
Environment=DATA_DIR=$APP_DIR/data
Environment=MAX_UPLOAD_MB=50
ExecStart=/usr/bin/python3 $APP_DIR/server.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF
echo "  已写入 $SVC_FILE（监听 127.0.0.1:$UPSTREAM_PORT，与 nginx upstream 对齐）"

# ---------- 8. 启动 ----------
say "启动服务"
systemctl daemon-reload
systemctl enable --now "$SERVICE" 2>/dev/null || {
  warn "systemd 启动失败，改用 nohup"
  cd "$APP_DIR" && nohup python3 server.py >> "$APP_DIR/server.log" 2>&1 &
}
sleep 3
systemctl status "$SERVICE" --no-pager -l 2>/dev/null | head -12

# ---------- 9. 验证 ----------
say "验证"
echo -n "  本机健康检查: "
curl -s -m 8 "http://127.0.0.1:$UPSTREAM_PORT/api/health" && echo || echo "失败"
echo -n "  企微登录状态: "
curl -s -m 8 "http://127.0.0.1:$UPSTREAM_PORT/api/login/wecom/status" && echo || echo "失败"

echo -n "  域名访问   : "
curl -sk -m 10 "https://$DOMAIN/api/health" && echo || echo "失败"

# ---------- 10. 企微后台三项体检 ----------
say "企微后台配置体检（决定扫码能不能真的登进去）"
if [ -f "$APP_DIR/dev_check.sh" ]; then
  bash "$APP_DIR/dev_check.sh" 2>/dev/null || echo "  （体检脚本执行异常，可手动跑：bash $APP_DIR/dev_check.sh）"
else
  echo "  未找到 dev_check.sh，跳过"
fi

say "完成"
cat <<EOF
下一步（只有前两项需要人去后台点，其余脚本都做完了）：
1. 【企微后台】应用 →「企业微信授权登录」→「设置」→「设置授权回调域」
   填：$DOMAIN      ← 缺这个会报 -31020 授权完成回调域名不一致
2. 【企微后台】应用管理 → 自建应用(1000196) → 应用详情页底部「企业可信 IP」→ 配置 → 加服务器出口 IP
   （上面已打印；${OUT_IP:-请在服务器上执行 curl -s https://ifconfig.me}）
   ← 缺这个能扫出码、能扫，但换不到身份，登不进来
3. 打开 https://$DOMAIN → 登录页应出现可点击的「使用企业微信扫码登录」
4. 手机企业微信扫描二维码完成登录
5. 首次登录会自动建号：用户名 wecom_<userid>，姓名/部门从企微通讯录同步
6. 如需清掉 28 条演示占位数据：systemctl stop $SERVICE && cd $APP_DIR && \\
     python3 server.py --clear && systemctl start $SERVICE
7. 实时日志：journalctl -u $SERVICE -f

★ 如果扫码仍不通：改上面两项后台设置即可生效，**不用重新部署**；
  要改本机配置（corp_id/secret/base_url）就编辑 $APP_DIR/wecom_config.json，
  保存后即刻生效（配置热重载），再跑 `bash $APP_DIR/dev_check.sh` 复核。
EOF
