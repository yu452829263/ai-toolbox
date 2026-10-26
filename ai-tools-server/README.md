# AI 成果库 · 内部 AI 工具平台 —— 部署交接说明

> 交付对象：公司运维 / 后端同事
> 交付内容：前端页面 + 轻量后端 + 数据库 + 文件存储，一个目录即可部署

---

## 1. 这是什么

一个"公司内部 AI 工具共享平台"：同事上传自己的 AI 工具（名称/部门/类型/说明/附件/入口链接），其他人能搜到、能下载、能点赞收藏，首页有下载榜和部门贡献榜。

**关键特性：零第三方依赖。** 只用 Python 3 标准库（`http.server` + `sqlite3`），不需要 pip install、不需要 npm、不需要装 MySQL、不需要编译工具。运维拿到目录 → 改一个端口 → 启动，完事。

---

## 2. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **3.8 或以上**（推荐 3.9+） | 只用标准库；`python3 -V` 能出版本号即可 |
| 操作系统 | Linux / macOS | Windows 也能跑（`py server.py`） |
| 端口 | 默认 8080，可改 | 需防火墙放行 |
| 磁盘 | 附件存储空间 | 上传的文件存在 `data/uploads/` |
| 其它 | **无需 root、无需数据库服务、无需构建** | 依赖环境全部由标准库提供 |

> SQLite 数据库就是一个文件 `data/tools.db`，不用装数据库、不用建账号、不用做备份策略以外的运维动作。

---

## 3. 目录结构

```
ai-tools-server/
├── server.py              ★ 服务入口（唯一需要跑的文件）
├── README.md              本文档
├── requirements.txt       注明：本项目零第三方依赖
├── web/
│   └── index.html         前端页面（单文件自包含，图片已内联）
├── seed/
│   └── seed_tools.json    9 个真实工具的初始数据（首次启动自动导入；上线前用 --clear 清掉）
├── data/                  ★ 运行时数据（需备份 / 需可写）
│   ├── tools.db           SQLite 数据库
│   └── uploads/           用户上传的附件原文件
├── deploy/
│   ├── nginx.conf.example nginx 反向代理示例
│   ├── ai-tools.service   systemd 常驻服务示例
│   └── Dockerfile         容器化部署示例
└── tools/                 ★ 随包分发的真实工具原件（只读，走 /tools/xxx 下载）
    ├── macOS日历助手-ical-cli.zip
    ├── 滴答清单助手-dida-cli.zip
    ├── 头脑风暴助手-johari.zip
    ├── 广告法合规检查.zip
    ├── 会议纪要速记-企微七段式.zip
    ├── 金融订单播报-分发模型.png
    ├── 开票资料助手.html
    ├── 合同初审机器人-使用说明.md
    └── 薪资计算机器人-使用说明.md
```

---

## 4. 快速启动（三条命令）

```bash
cd ai-tools-server
PORT=8080 python3 server.py
```

看到这段就起来了：

```
==============================================================
AI 成果库 · 轻量后端已启动
  监听地址   : http://0.0.0.0:8080
  数据目录   : .../ai-tools-server/data
  数据库     : .../ai-tools-server/data/tools.db（28 个工具）
  附件上限   : 50 MB/文件
  健康检查   : http://0.0.0.0:8080/api/health
==============================================================
```

然后浏览器打开 **http://服务器IP:8080** 就能用。

**首次启动会自动做两件事**：建库建表 + 导入 `seed/seed_tools.json` 里的 28 个工具（内置样例数据，可自行改成真实数据）。

**预置账号**：包根目录的 `accounts_preset.json` 会在首次启动（用户表为空时）自动导入两个账号——`admin`（管理员）和 `guest`（普通用户）。**上线后请尽快登录修改这两个默认密码**；修改后可删除该文件（数据已入库，删除不影响使用）。若想让服务重启时不再校验该文件，删除即可。

**重置回种子数据**（会清空所有上传的工具和计数）：
```bash
python3 server.py --reseed
```

## 4.2 账号与权限（2026-10-23 新增）

系统带账号体系，权限规则：

| 操作 | 游客 | 登录用户 | 管理员 |
|---|---|---|---|
| 浏览 / 搜索 / 下载 / 点赞（连赞） | ✅ | ✅ | ✅ |
| 收藏 | ❌ | ✅ | ✅ |
| 上传工具 | ❌（引导登录） | ✅ | ✅ |
| 编辑 / 删除工具、修改展示计数、账号管理 | ❌ | ❌ | ✅ |

**首次启动自动创建管理员**：用户名 `admin`，随机密码**打印在启动日志里**（搜索「内置管理员」），请立即登录后在「个人主页 → 修改密码」改掉。账号不开放自主注册，全部由管理员在「管理面板 → 账号管理」创建（支持单个添加和按行批量导入：`账号,姓名,部门,初始密码`）。

**登录入口**：页面右上角「登录」。登录后右上角出现用户菜单：个人主页（我上传的 / 我收藏的 / 改密码）、管理面板（仅管理员：工具编辑 / 删除与回收站 / 展示计数修改 / 账号管理 / 操作日志）。

**升级注意**：老库首次用新版启动时自动迁移（tools 表加 `created_by` / `deleted` 列、新建 users/sessions/favorites/audit_log 表），**现有数据无损，无需手工操作**。升级前仍建议把 `data/` 目录拷一份备份。

**管理操作全部留痕**：编辑、删除、恢复、改计数、账号操作都会记入操作日志（管理面板第三个页签可查）。

**清空演示占位数据（上线正式使用前用这个）**：见下方第 4.1 节。

---

## 4.3 企业微信扫码登录（2026-10-26 新增，配置即用）

登录页现在有**两个入口**：账号密码 + 企业微信扫码。**两者并存**，不是替代关系。
**不配置任何东西时，企微按钮自动灰掉并显示「未启用：缺什么」，其余功能完全不受影响。**

### 接入流程（运维/管理员操作，一次性）

**第 1 步：企微管理后台准备（需要管理员权限）**

在企业微信管理后台「应用管理 → 自建应用」里拿到三样东西：

| 拿到的东西 | 后台位置 | 填到配置的哪一项 |
|---|---|---|
| CorpID | 「我的企业 → 企业信息 → 企业ID」 | `corp_id` |
| AgentId | 「应用管理 → 你的自建应用 → AgentId」 | `agent_id` |
| Secret | 同上，点「查看」 | `secret` |

再配两项后台设置（**缺一不可**）：
- **授权回调域**（扫码登录用）：应用详情页 →「企业微信授权登录」→「设置」→
  **设置授权回调域**，填 `aitoolbox.maihaoche.com`（只填域名，不带 `https://` 和路径）。
  没设会直接报 `-31020 redirect_uri 配置的授权完成回调域名不一致`。
- **可信 IP**（扫码后换身份要用）：**应用详情页** → 往下拉到「开发者接口」区域 →
  「**企业可信 IP**」→「配置」→ 加上服务器出口 IP。
  ⚠️ `gettoken` 不校验 IP，但 `auth/getuserinfo`（扫码后换身份）、`user/get`、
  `department/simplelist` **都校验**。没加白名单时扫码后**换不到身份、登不进来**。

> **容易找错的地方**：可信 IP **不在「管理工具」里**，而在
> 「**应用管理 → 自建 → 你的应用（AgentId 1000196）→ 应用详情页底部**」的
> 「企业可信 IP」那一栏。页面上那个「JD-sdk 网页」只是企微提供的
> IP 段查询/校验工具，不是入口 —— 真正的「配置」按钮在应用详情页里。

> 本项目默认走**扫码授权登录**（`qrConnect`，PC 浏览器显示二维码、手机企微扫）。
> 如果要把入口放进企业微信工作台应用，改用 `mode=oauth`（网页授权 snsapi_base，
> 必须在企业微信客户端里打开），此时需要在应用后台配**网页授权及JS-SDK → 可信域名**。

**第 2 步：填配置**

编辑同目录的 `wecom_config.json`：

```json
{
  "corp_id": "ww1234567890abcdef",
  "agent_id": "1000002",
  "secret": "你的Secret",
  "base_url": "https://aitoolbox.maihaoche.com",
  "redirect": "https://aitoolbox.maihaoche.com/api/login/wecom/callback",
  "admin_userids": ["你的企微userid"]
}
```

`base_url` = 本服务的对外访问地址，**必须是 https**（企微强制要求）。
`redirect` = 上报给企微、用来核对「授权回调域」的地址。

### 4.3.1 怎么让自己扫码后就是管理员

系统里有两个「管理员」，别搞混：

| | 企微管理员 | 本系统管理员 |
|---|---|---|
| 干什么用 | 能在企微管理后台改应用、加可信 IP、管通讯录 | 能进本平台的「用户管理 / 审计日志 / 上下架工具」 |
| 怎么设 | 企微后台 →「我的企业 → 权限管理 → 管理员 → +」→ 选自己 → 超级管理员 | 把自己的企微 userid 填进 `wecom_config.json` 的 `admin_userids` |

**为什么必须配 `admin_userids`**：本系统的「用户管理」页面只有管理员能进，
而企微扫码进来的新用户默认是普通用户 —— 不配这个名单，就会出现
「要进后台才能给自己升权」的死循环。填了之后，**这个 userid 扫码即管理员**
（配置热重载，保存即生效，不用重启服务）。

`userid` 在哪看：企微管理后台「通讯录 → 点自己 → 账号详情」，
或用 `wecom-cli contact users search <你的名字>` 查。

配置完成后，`/api/login/wecom/diagnose` 会多一项「管理员白名单」显示当前状态。

> **企微管理员的额外前提**：设置时会提示「成员需要进行实名认证才能成为管理员」——
> 得先在企微客户端完成实名，否则加不上。

> **为什么 base_url 和 redirect 要分开**：
> 企微校验的只是 `redirect` 的**域名**，与本服务实际从哪个地址对外无关。
> 内嵌扫码模式下 code 由服务端长轮询取得，不靠浏览器跳转，所以
> **本机联调时 `base_url` 可以指向隧道地址、`redirect` 仍填正式域名** ——
> 正式域名在后台登记一次，本机联调 / 线上部署 / 换隧道都不用再动后台。
> `redirect` 留空则自动取 `{base_url}/api/login/wecom/callback`。

也可以改用环境变量（**优先级高于配置文件**）：

```bash
WECOM_CORP_ID=ww1234567890abcdef \
WECOM_AGENT_ID=1000002 \
WECOM_SECRET=xxxx \
WECOM_BASE_URL=https://aitoolbox.maihaoche.com \
WECOM_REDIRECT=https://aitoolbox.maihaoche.com/api/login/wecom/callback \
python3 server.py
```

**第 3 步：重启并确认**

```bash
# 启动日志里会看到这两行，说明生效了
#   企微登录   : 已启用（CorpID=ww1234…，对外 https://...）
#   上报回调域 : https://aitoolbox.maihaoche.com/api/login/wecom/callback（企微后台「授权回调域」填 aitoolbox.maihaoche.com）
```

一键体检（真实探测企微接口，直接告诉你缺哪一项）：

```bash
# 本机联调
bash dev_check.sh

# 服务器上（自动识别 systemd / nginx 里的实际端口）
cd /opt/ai-tools-server && bash dev_check.sh

# 或直接调接口
curl http://127.0.0.1:8080/api/login/wecom/diagnose
# 逐项返回 ok / detail / action（action 就是「该去后台哪里填什么」）
# 另含 todo[]（还差哪几项）、ready（是否已就绪）、egress_ip（企微看到的出口 IP）
```

```bash
curl http://127.0.0.1:8080/api/login/wecom/status
# {"enabled": true, "missing": [], "reason": "", ...}
```

登录页的「使用企业微信扫码登录」变成可点的绿色按钮即成功。

> ★ **改配置不用重启服务**：`wecom_config.json` 改了保存即生效（后端每次企微请求都会
> 重新读文件，凭据变更还会自动作废旧的 access_token 缓存）。
> 企微后台的两项设置（授权回调域 / 企业可信 IP）改完也**立即生效**。
> 也就是说：**扫不通时不需要重新部署**，照体检脚本的提示改完重跑一次体检即可。

### 行为说明

| 项 | 行为 |
|---|---|
| 首次扫码 | **自动建号**：用户名 `wecom_<userid>`，姓名取企微姓名，**部门取企微通讯录部门**（多部门用 ` / ` 连接） |
| 再次扫码 | 按 `wecom_userid` 匹配已有账号，只更新姓名/部门/头像，**不重复建号** |
| 角色 | 企微登录建出来的账号是**普通用户**；管理员账号仍由管理员后台分发 |
| 密码 | 企微账号有随机占位密码，**实际走扫码登录**，不用记密码 |
| 会话 | 与账密登录**同一套 7 天 session**，顶栏头像/消息红点/个人主页全部一致 |
| 姓名/部门 | 从企微通讯录同步。**若服务器出口 IP 未加可信 IP 白名单**，`auth/getuserinfo` 也会被拒 → 扫码后换不到身份、登不进来（不只是姓名缺失） |
| token 传递 | 企微回调后 URL 上只带**一次性 code**（用完即失效），前端再换正式 token —— **token 不经过 URL**，不进浏览器历史、不进 nginx 日志 |
| 所属部门 | 企微登录用户的部门是**真的从通讯录同步的**；账密账号的部门仍由管理员在后台填 |

### 接口一览

| 接口 | 说明 |
|---|---|
| `GET /api/login/wecom/status` | 是否已启用 + 缺哪些配置（前端据此决定按钮可点与否） |
| `GET /api/login/wecom/diagnose` | **一键体检**：真实探测企微接口，逐项返回「凭据 / 授权回调域 / 可信IP」是否就绪，并给出该去后台哪里填什么 |
| `GET /api/login/wecom/qr` | 申请二维码会话（内嵌扫码模式，返回图片代理地址 + sid） |
| `GET /api/login/wecom/qrcode?sid=` | 代理企微二维码图片（避免跨域/防盗链） |
| `GET /api/login/wecom/scan?sid=` | 轮询扫码状态，成功时直接返回本地 token |
| `GET /api/login/wecom/start` | 302 跳转到企微**扫码登录**页（带 state 防 CSRF）。加 `?mode=oauth` 走网页授权（工作台内 H5） |
| `GET /api/login/wecom/callback` | 企微回调：换 userid → 建号/更新 → 302 回首页带一次性 code |
| `POST /api/login/wecom/exchange` | 一次性 code 换正式 token |

### 注意事项

- **本机 `localhost` 跑不起来**——企微要能回调你的域名，`localhost` 无法在企微后台登记。授权回调域+出口 IP 都齐了才能真正走通。
  想在本机联调：用 `dev_guard.sh` 起 Cloudflare 隧道 + 把 `redirect` 指向正式域名（见 4.3 节）。
- `base_url` 必须是**公网可达的 https 地址**；服务本身只听 HTTP，靠 nginx 反代做 https（见第 6 节）。
- 企微的 `access_token` 有效期 7200 秒，服务端已做内存缓存（提前 5 分钟刷新），不用每次请求都换。
- 联调排错看服务端日志：每次扫码跳转、回调、建号都会 `log()` 一行。
- **可信 IP 填的是「跑后端那台机器」的公网出口 IP**，不是域名的 A 记录、不是你自己电脑的 IP。
  企微可信 IP **不支持 IP 段、不支持 `*`**，只能逐个加（上限 120 个）→ 服务器出口 IP 最好固定。
- **排查顺序**：先跑 `bash dev_check.sh` 看「还差哪几项」，全绿再去扫码。
  常见误区是「二维码出来了就以为成功了」——真正会卡住的是扫码后换身份那一步（可信 IP 没加）。

---

## 4.1 种子数据 = 9 个真实工具（2026-10-26 起）

`seed/seed_tools.json` 里现在是 **9 个真实工具**，不是编造的占位数据：
macOS 日历助手、滴答清单助手、头脑风暴助手、开票资料助手、广告法合规检查、
会议纪要速记、金融订单播报、合同初审机器人、薪资计算机器人。

工具原件放在 `tools/` 目录（只读，随包分发），走 `/tools/<文件名>` 下载，
不依赖 `data/` 目录 —— 重装、换机器都还在。

### 换成自己的工具

```bash
# 1. 把新工具文件放进 tools/
cp 我的工具.zip /path/to/ai-tools-server/tools/

# 2. 在 seed/seed_tools.json 里加一条（name/dept/type/desc/detail 必填）
#    files[].url 写成 /tools/<文件名>

# 3. 让数据库重新导入
python3 server.py --reseed
```

### 如果想让工具库从零开始（不要这 9 个真实工具）

```bash
sudo systemctl stop ai-tools
cd /path/to/ai-tools-server
python3 server.py --clear        # 清空且不再自动导入
sudo systemctl start ai-tools
```

验证：

```bash
curl http://127.0.0.1:8080/api/health
# → "tools": 0
```

`--clear` 会打上「种子已处理」标记，**之后重启多少次都不会又冒出来**。
想反悔、恢复这 9 个真实工具：`python3 server.py --reseed`。

### ⚠️ 前端也要是新版

如果页面上的数据**清完库还在**，说明前端还是旧版（旧版在接口返回空列表时
会回退显示内置的演示数据）。把新版 `web/index.html` 覆盖上去，然后浏览器 Ctrl+F5 强刷。

新版前端的行为：**接口通了就以接口为准**（库里 0 条就显示空态），
只有后端连不上时才回退内置的 9 条真实工具。

---

## 5. 配置项（环境变量，都有默认值，正常不用改）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址。只想本机访问就设 `127.0.0.1`（配合 nginx 反代） |
| `PORT` | `8080` | 监听端口 |
| `DATA_DIR` | `./data` | 数据目录（数据库 + 附件）。**换盘/换路径就设这个** |
| `WEB_DIR` | `./web` | 前端页面目录 |
| `SEED_FILE` | `./seed/seed_tools.json` | 种子数据文件 |
| `MAX_UPLOAD_MB` | `50` | 单个附件大小上限（MB），前后端一致 |
| `CORS_ORIGIN` | 空 | 跨域放行来源。**同源部署不要设**；前后端分域时才填，如 `https://ai.corp.com` |
| `WECOM_CORP_ID` | 空 | 企业微信 CorpID。不设则企微扫码登录不启用（详见 4.3 节） |
| `WECOM_AGENT_ID` | 空 | 企微自建应用 AgentId |
| `WECOM_SECRET` | 空 | 企微自建应用 Secret |
| `WECOM_BASE_URL` | 空 | 本服务对外 https 地址（用户浏览器实际打开的地址） |
| `WECOM_REDIRECT` | 空 | 上报给企微、用来核对「授权回调域」的地址。留空则自动取 `{WECOM_BASE_URL}/api/login/wecom/callback`。本机联调时把它固定为正式域名，本机/线上/换隧道都不用再改企微后台 |
| `WECOM_ADMIN_USERIDS` | 空 | 管理员白名单，逗号分隔的企微 userid。这些 userid 扫码登录后自动是本系统管理员（详见 4.3.1 节） |
| `TOOLS_DIR` | `./tools` | 随包分发的真实工具原件目录（只读，`/tools/<文件名>` 下载） |

示例：
```bash
PORT=9000 DATA_DIR=/data/ai-tools MAX_UPLOAD_MB=100 python3 server.py
```

---

## 6. 对外发布（推荐：nginx 反代 + HTTPS）

服务本身只监听 HTTP，域名、HTTPS、访问控制交给公司现有的 nginx。

`deploy/nginx.conf.example` 内容（按公司实际域名/证书路径改）：

```nginx
server {
    listen 443 ssl;
    server_name ai.corp.com;                 # ← 改成你们的域名

    ssl_certificate     /etc/nginx/ssl/ai.corp.com.crt;
    ssl_certificate_key /etc/nginx/ssl/ai.corp.com.key;

    client_max_body_size 100M;               # 要 ≥ 后端 MAX_UPLOAD_MB

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

配套：把后端改成只监听本机 + 交给 nginx：
```bash
HOST=127.0.0.1 PORT=8080 python3 server.py
```

---

## 7. 做成常驻服务（systemd）

把 `deploy/ai-tools.service` 复制到 `/etc/systemd/system/`，改好 `WorkingDirectory` / `User` / `ExecStart` 路径后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-tools
sudo systemctl status ai-tools      # 看状态
sudo journalctl -u ai-tools -f      # 看实时日志
```

---

## 8. 容器化部署（可选）

```bash
cd ai-tools-server
docker build -t ai-tools-server .
docker run -d --name ai-tools -p 8080:8080 -v /data/ai-tools:/app/data ai-tools-server
```

---

## 9. 数据与备份

所有需要持久化的东西都在 `data/` 目录里：

| 内容 | 位置 | 说明 |
|---|---|---|
| 数据库 | `data/tools.db` | 工具记录 + 下载/点赞/收藏计数 |
| 附件原文件 | `data/uploads/` | 上传的 zip/pdf/docx/图片等 |

**备份就是把整个 `data/` 目录拷走**：
```bash
tar czf ai-tools-backup-$(date +%F).tar.gz data/
```

**恢复**：停服务 → 用备份的 `data/` 覆盖回去 → 起服务。

**建议**：加一条每日 cron 备份 `data/` 目录（附件占空间，量大时可只备份 `tools.db` + 增量附件）。

---

## 10. 健康检查与监控

```bash
curl http://127.0.0.1:8080/api/health
```
返回：
```json
{"ok":true,"data":{"status":"ok","tools":28,"uptime_sec":3600,
 "db":"/app/data/tools.db","data_dir":"/app/data"}}
```

把这个 URL 配到你们的监控探活即可。服务本身会往标准输出打访问日志和错误日志（systemd 下用 `journalctl` 看）。

---

## 11. 接口一览（给后端/二次开发同事）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tools?q=&tag=&page=&pageSize=` | 工具列表。**不传 `page` 返回全量**（前端自己做搜索/筛选/分页）；传了则分页返回 |
| POST | `/api/tools` | 新建工具（JSON），返回新建记录（含真实 id） |
| POST | `/api/upload` | 上传附件（multipart/form-data，字段名 `file`），返回 `[{name,size,type,url}]` |
| POST | `/api/tools/{id}/like` | 点赞 +1，返回 `{likes: n}` |
| POST | `/api/tools/{id}/favorite` | 收藏 +1。**当前前端不调用**（无登录态不做收藏，预留接口） |
| POST | `/api/tools/{id}/download` | 下载计数 +1，返回 `{downloads: n}` |
| GET | `/uploads/{文件名}` | 下载附件原文件 |
| GET | `/api/health` | 健康检查 |

统一响应格式：成功 `{"ok":true,"data":...}`，失败 `{"ok":false,"msg":"原因"}`（HTTP 4xx/5xx）。

计数器是**数据库层原子自增**（`UPDATE ... SET likes = likes + 1`），多人同时点不会丢数。

详细字段说明见 `../docs/后端接口契约-工具库.md`。

---

## 12. 已知限制（请知悉）

1. **不做登录** → 点赞/收藏是全局计数、可连点无取消、**任何人都能刷高**。这是产品上主动接受的设计（内部工具、无账号体系）。若要防刷，最小成本是前端加 localStorage 限流。
2. **单机 SQLite** → 适合内部几十到几千条工具的规模；并发写入极高（每秒数百次自增）时需换 MySQL/PG。若要换，只需改 `server.py` 里的 SQL，接口不变。
3. **附件存本机磁盘** → 多台机器部署需要共享存储（COS/OSS/NFS）。当前实现是单机版，扩容时把 `data/uploads` 换成对象存储即可。
4. **上传大小上限**由 `MAX_UPLOAD_MB` 控制（默认 50MB），前端和后端一致；nginx 的 `client_max_body_size` 也要同步调大，否则会被 nginx 先拒掉。
5. **无 HTTPS**（服务本身不提供）→ 请务必在 nginx 层配 HTTPS。

---

## 13. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 启动报 `Address already in use` | 端口被占。换端口：`PORT=8081 python3 server.py`，或 `lsof -ti:8080 \| xargs kill` |
| 启动报 `Permission denied`（写 data/） | 当前用户对目录没写权限。`chown -R <运行用户> data/` |
| 页面打得开但上传 413 | 超过大小上限。调大 `MAX_UPLOAD_MB` + nginx `client_max_body_size` |
| 页面打得开但数据一直是演示态 | 前端在 `file://` 下打开才会走演示态；用 `http://` 访问才会连后端 |
| **清空库后重启，占位数据又回来了** | 用了旧版前端或没走 `--clear`。新版做法：停服务 → `python3 server.py --clear` → 起服务（会自动打标记，重启不恢复）。详见 4.1 节 |
| **库里 0 条但页面还有 28 个工具** | 前端是旧版。覆盖新版 `web/index.html` 后 Ctrl+F5 强刷 |
| 附件下载 404 | 文件被手工从 `data/uploads/` 删了；或 URL 里的中文没做编码（服务端已兼容） |
| 改了前端没生效 | 改的是 `web/index.html`；浏览器可能缓存了旧页面，Ctrl+F5 强刷 |

---

## 14. 升级 / 发布新版本

1. 停服务（`systemctl stop ai-tools` 或 kill 进程）
2. 覆盖代码目录（**`data/` 目录不要动**）
3. 起服务，`curl /api/health` 确认
4. 浏览器打开首页确认

数据库会自动做向前兼容的建表，不会因为升级丢数据。
