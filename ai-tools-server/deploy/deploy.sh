#!/usr/bin/env bash
# ============================================================
# AI 成果库 · 一键部署脚本（在服务器上执行）
#   用法：  bash deploy.sh <CorpID> <AgentId> <Secret> [base_url] [redirect]
#   示例：  bash deploy.sh ww1234567890 1000002 abcdefsecret https://aitoolbox.maihaoche.com
#
#   域名所有权校验文件（可选，第 7 个参数）：
#     bash deploy.sh <CorpID> <AgentId> <Secret> <base_url> <redirect> <trusted_ips> <WW_verify_xxx.txt路径>
#     文件会被复制到 nginx 直接可读的位置，企微保存「可信域名」时能直接校验通过。
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
# 校验文件放这里：nginx 直接读磁盘（不经后端）→ 后端没起来也能通过企微校验
WEBROOT_NGINX=/var/www/ai-tools-webroot

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
# 本脚本所在位置：<源码根>/deploy/deploy.sh → 源码根就是上一级
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SRC_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
mkdir -p "$APP_DIR"
if [ -f "$APP_DIR/server.py" ]; then
  echo "  已有 server.py，跳过代码上传（若要更新请手动覆盖后重跑本脚本）"
elif [ -f "$SRC_ROOT/server.py" ]; then
  echo "  从当前源码树复制代码 → $APP_DIR"
  echo "    来源: $SRC_ROOT"
  # 只同步代码，不带运行时数据（data/ 里是数据库和上传附件，不能覆盖）
  for item in server.py web seed tools deploy dev_check.sh dev_guard.sh dev_tunnel.sh \
              requirements.txt wecom_config.example.json accounts_preset.example.json tests; do
    [ -e "$SRC_ROOT/$item" ] || continue
    rm -rf "$APP_DIR/$item"
    cp -r "$SRC_ROOT/$item" "$APP_DIR/$item"
  done
  echo "  代码已就位"
else
  echo -e "\033[1;33m  代码还没上传到 $APP_DIR。请先执行：\033[0m"
  echo -e "    scp 'ai-tools-server.zip' user@<服务器IP>:/tmp/"
  echo -e "    cd /tmp && unzip -q ai-tools-server.zip && cp -r 'ai-tools-server 2'/* $APP_DIR/"
  echo -e "    bash deploy.sh $CORP_ID $AGENT_ID <Secret> $BASE_URL"
  echo -e "  （或者：git clone 仓库后直接在本仓库内跑 bash ai-tools-server/deploy/deploy.sh ...）"
  exit 2
fi

# ---------- 6. 写企微配置 ----------
say "写入企微配置"
# trusted_ips：把探测到的出口 IP 写进去，体检时自动跟企微看到的 IP 比对。
# 若运维已把某个 IP 加进企微后台白名单，可用第 6 个参数显式指定（逗号分隔）。
TRUSTED_IPS="${6:-}"
if [ -z "$TRUSTED_IPS" ] && [ -n "$OUT_IP" ]; then
  TRUSTED_IPS="$OUT_IP"
fi
# 用 python 生成 JSON（比 shell 拼字符串可靠，避免转义问题）
TRUSTED_IPS_JSON=$(python3 - "$TRUSTED_IPS" <<'PY'
import json, sys
ips = [x.strip() for x in sys.argv[1].split(",") if x.strip()] if len(sys.argv) > 1 else []
print(json.dumps(ips, ensure_ascii=False))
PY
)
cat > "$APP_DIR/wecom_config.json" <<EOF
{
  "_说明": "企业微信扫码登录配置。修改后无需重启服务，下次请求会自动重新读取。",
  "corp_id": "$CORP_ID",
  "agent_id": "$AGENT_ID",
  "secret": "$SECRET",
  "base_url": "$BASE_URL",
  "redirect": "$REDIRECT",
  "_admin_userids说明": "管理员白名单：把你的企微 userid 填进来（数组，可多个）。这些 userid 扫码后自动是本系统管理员，保存即生效。",
  "admin_userids": [],
  "_trusted_ips说明": "已登记到企微后台「企业可信 IP」的地址（数组）。体检时自动跟企微实际看到的出口 IP 比对，不一致会报警。",
  "trusted_ips": $TRUSTED_IPS_JSON
}
EOF
chmod 600 "$APP_DIR/wecom_config.json"
echo "  已写入 $APP_DIR/wecom_config.json（权限 600）"
[ -n "$TRUSTED_IPS" ] && echo "  trusted_ips: $TRUSTED_IPS（体检时自动与企微看到的 IP 比对）"

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

# ---------- 7-b 域名所有权校验文件（企微「可信域名」用） ----------
# 企微保存「网页授权及JS-SDK → 可信域名」时会来取
#   https://<域名>/WW_verify_xxx.txt
# 取不到 → 报「校验域名所有权不通过」。
# 放两处，nginx 与后端各读一份，任何一条通路通就行：
#   1) $WEBROOT_NGINX  → nginx location 直接读磁盘（不经后端，最稳）
#   2) $APP_DIR/data/webroot → 后端 serve_static 兜底（后端在跑时生效）
VERIFY_SRC="${7:-}"
if [ -z "$VERIFY_SRC" ]; then
  # ★ 自动发现：校验文件已随仓库分发（ai-tools-server/web/WW_verify_xxx.txt），
  #   运维 clone 完直接部署即可，**不需要手动传文件**。
  #   若企微后台重新生成了校验串 → 把新文件覆盖到 web/ 再重跑本脚本。
  for cand in "$SRC_ROOT"/web/WW_verify_*.txt "$APP_DIR"/web/WW_verify_*.txt \
              "$APP_DIR"/data/webroot/WW_verify_*.txt; do
    if [ -f "$cand" ]; then VERIFY_SRC="$cand"; break; fi
  done
fi
mkdir -p "$WEBROOT_NGINX" "$APP_DIR/data/webroot"
if [ -n "$VERIFY_SRC" ] && [ -f "$VERIFY_SRC" ]; then
  VF_NAME=$(basename "$VERIFY_SRC")
  case "$VF_NAME" in
    WW_verify_*.txt) ;;
    *) warn "文件名不像企微校验文件（应形如 WW_verify_xxx.txt）：$VF_NAME"; VF_NAME="";;
  esac
  if [ -n "$VF_NAME" ]; then
    cp -f "$VERIFY_SRC" "$WEBROOT_NGINX/$VF_NAME"
    cp -f "$VERIFY_SRC" "$APP_DIR/data/webroot/$VF_NAME"
    chmod 644 "$WEBROOT_NGINX/$VF_NAME" "$APP_DIR/data/webroot/$VF_NAME"
    # nginx worker 要能读
    chown -R "$RUN_USER:$RUN_USER" "$APP_DIR/data/webroot" 2>/dev/null || true
    chmod 755 "$WEBROOT_NGINX" 2>/dev/null || true
    echo "  已放置校验文件：$VF_NAME"
    echo "    → $WEBROOT_NGINX/$VF_NAME（nginx 直读）"
    echo "    → $APP_DIR/data/webroot/$VF_NAME（后端兜底）"
  fi
else
  warn "没找到校验文件（仓库里应有 ai-tools-server/web/WW_verify_xxx.txt）。"
  echo "  如企微后台报「校验域名所有权不通过」，把企微给的 WW_verify_xxx.txt 放到："
  echo "    $WEBROOT_NGINX/  与  $APP_DIR/data/webroot/"
fi

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

# 校验文件公网可达性（企微「可信域名」所有权校验就看这个）
VF_CHECK=$(ls "$WEBROOT_NGINX"/WW_verify_*.txt 2>/dev/null | head -1)
if [ -n "$VF_CHECK" ]; then
  VF_NAME=$(basename "$VF_CHECK")
  echo "  校验文件   : $VF_NAME"
  echo -n "    https 取: "
  VF_HTTPS=$(curl -sk -m 10 "https://$DOMAIN/$VF_NAME")
  [ "$VF_HTTPS" = "$(cat "$VF_CHECK")" ] \
    && echo "✅ 内容一致" \
    || echo "❌ 取不到或内容不一致（取到=[${VF_HTTPS:0:60}]）"
  echo -n "    http  取: "
  VF_HTTP=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "http://$DOMAIN/$VF_NAME")
  [ "$VF_HTTP" = "200" ] && echo "✅ 200" || echo "❌ HTTP $VF_HTTP（企微可能先试 http）"
else
  echo "  校验文件   : 未放置（企微后台若报「校验域名所有权不通过」→ 放 WW_verify_xxx.txt 到 $WEBROOT_NGINX/）"
fi

# ---------- 10. 企微后台三项体检 ----------
say "企微后台配置体检（决定扫码能不能真的登进去）"
if [ -f "$APP_DIR/dev_check.sh" ]; then
  bash "$APP_DIR/dev_check.sh" 2>/dev/null || echo "  （体检脚本执行异常，可手动跑：bash $APP_DIR/dev_check.sh）"
else
  echo "  未找到 dev_check.sh，跳过"
fi

say "完成"
cat <<EOF
下一步（前三项需要人去企微后台点，其余脚本都做完了）：
1. 【企微后台】应用详情页 →「网页授权及JS-SDK」→「设置」→「可信域名」→ 填 $DOMAIN
   → 校验文件已随仓库分发并自动放好（上面已实测公网可达），直接回后台点保存即可
   ⚠️ 若企微重新生成了校验串 → 把新文件覆盖到 $APP_DIR/web/ 再重跑本脚本
   ⚠️ 缺这个 → 报「校验域名所有权不通过」，且**加不了企业可信 IP**
2. 【企微后台】应用 →「企业微信授权登录」→「设置」→「设置授权回调域」
   填：$DOMAIN      ← 缺这个会报 -31020 授权完成回调域名不一致
3. 【企微后台】应用管理 → 自建应用(1000196) → 应用详情页底部「企业可信 IP」→ 配置 → 加服务器出口 IP
   （上面已打印；${OUT_IP:-请在服务器上执行 curl -s https://ifconfig.me}）
   ← 缺这个能扫出码、能扫，但换不到身份，登不进来
4. 打开 https://$DOMAIN → 登录页应出现可点击的「使用企业微信扫码登录」
5. 手机企业微信扫描二维码完成登录
6. 首次登录会自动建号：用户名 wecom_<userid>，姓名/部门从企微通讯录同步
7. 想把工具库重置回出厂状态（当前出厂即空站点；会清空所有人上传的工具和计数）：
   systemctl stop $SERVICE && cd $APP_DIR && \\
     python3 server.py --reseed && systemctl start $SERVICE
   ⚠️ 不要用 --clear：它会清空工具表并打上「不再自动导入种子」的标记，
      重启也不会灌回来，站点会一直是空的。除非你确定要一个完全空库。
8. 实时日志：journalctl -u $SERVICE -f

★ 如果扫码仍不通：改上面三项后台设置即可生效，**不用重新部署**；
  要改本机配置（corp_id/secret/base_url）就编辑 $APP_DIR/wecom_config.json，
  保存后即刻生效（配置热重载），再跑 \`bash $APP_DIR/dev_check.sh\` 复核。
EOF
