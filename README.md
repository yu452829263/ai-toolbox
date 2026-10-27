# AI 成果库 · 内部 AI 工具平台

> 给运维 / 后端同事的部署仓库。
> 目标：**clone 下来照着做，一次部署成功；万一不通，也不用重新部署。**

---

## 1. 这是什么

一个公司内部的 AI 工具共享平台：同事上传自己的 AI 工具（名称 / 部门 / 类型 / 说明 / 附件 / 入口链接），其他人能搜到、能下载、能点赞，首页有下载榜和部门贡献榜。

**关键特性：零第三方依赖。** 只用 Python 3 标准库（`http.server` + `sqlite3`），不需要 pip install、不需要 npm、不需要装 MySQL、不需要编译。运维拿到 → 改一个端口 → 启动，完事。

---

## 2. 五分钟部署

```bash
# ① 拉代码（公开仓库，无需凭据）
git clone https://github.com/yu452829263/ai-toolbox.git ai-tools && cd ai-tools

# ② 一键部署（会自己装 systemd、测连通、跑体检）
bash ai-tools-server/deploy/deploy.sh <CorpID> <AgentId> <Secret> https://aitoolbox.maihaoche.com

# ③ 体检（输出「还差哪几项」，全绿再去扫码）
bash ai-tools-server/dev_check.sh

# ④ 验证
curl http://127.0.0.1:8080/api/health
```

部署完成后打开 **https://aitoolbox.maihaoche.com** 就能用。

> **想先看效果、不想装 systemd？** 直接跑：
> ```bash
> cd ai-tools-server && PORT=8080 python3 server.py
> ```

---

## 3. 你需要准备什么

| # | 需要的东西 | 从哪来 | 没有会怎样 |
|---|---|---|---|
| 1 | 服务器（Linux，有固定公网 IP） | 公司现有服务器 | 无载体 |
| 2 | 域名 + HTTPS 证书 | `aitoolbox.maihaoche.com`（**已配好**，证书有效至 2026-12-08） | 企微强制要求 https |
| 3 | nginx 反代（域名 → 本服务端口） | **已配好 vhost**（当前返回 503 = 后端没起来，部署完自动恢复） | 域名打不开 |
| 4 | 企微三件套：CorpID / AgentId / Secret | 企微后台 | 无法换取 access_token |
| 5 | 企微后台管理员权限 | 企微超级管理员 | 扫码登不进去 |

**只有第 4、5 项需要人去后台点，第 1–3 项都已就绪。**

---

## 4. 企微后台要改的两项（只改一次，之后不用再动）

### 4.1 授权回调域 ✅ 已登记

后台 → **应用管理 → 自建应用（AgentId 1000196）** → 应用详情页 → 「企业微信授权登录」→ 设置 → 授权回调域

填：`aitoolbox.maihaoche.com`

> ⚠️ 企微只校验 **hostname**，不校验路径。填域名即可，不用带 `/api/login/wecom/callback`。

### 4.2 企业可信 IP

后台 → **应用管理 → 自建应用（AgentId 1000196）** → 应用详情页 → 往下拉到「开发者接口」区域 → 「**企业可信 IP**」→「配置」→ 添加 IP

填：**服务器那台机器的公网出口 IP**（在服务器上 `curl -s https://ifconfig.me` 取）

> ⚠️ **位置容易找错**：不在「管理工具」里。你看到的「JD-sdk 网页 + 下面的可信IP」是企微提供的 **IP 段查询工具**，不是入口——真正的「配置」按钮在应用详情页里。
>
> ⚠️ 企微可信 IP **不支持 IP 段、不支持含 `*` 的写法**，只能一个个加，上限 120 个。所以服务器出口 IP 最好是固定的。
>
> **为什么必须加**：扫码后要把 code 换成成员身份（`auth/getuserinfo`），这个接口**强制校验可信 IP**。不加的后果是「二维码能出来、能扫、能拿到 code，但换不到身份、登不进来」——看起来像成功了，实际最后一步被拒，极易误判。

### 4.3 让系统自动核对可信 IP（推荐）

白名单填错 IP、或服务器换了出口，是最常见的「扫码登不进来」原因。这个功能会**自动比对**「你登记的 IP」和「企微实际看到的出口 IP」，不一致立刻报警：

```bash
# 在 wecom_config.json 里登记已加进企微后台的 IP（可多个）
"trusted_ips": ["115.233.220.92"]
```

之后跑 `bash ai-tools-server/dev_check.sh`，会多出「可信 IP 核对」一项：

```
  ✅ 可信 IP 核对
     企微看到的出口 IP 115.233.220.92 已在登记清单里
```

不一致时会直接告诉你该加哪个：

```
  ❌ 可信 IP 核对
     企微看到的出口 IP 是 115.233.220.93，但登记清单里只有：115.233.220.92
     → 把 115.233.220.93 加到企微后台「企业可信 IP」…
```

> `deploy.sh` 会自动把探测到的出口 IP 写进 `trusted_ips`，不用手填。

---

## 5. 目录结构

```
.
├── ai-tools-server/              ★ 唯一源码目录，部署的就是它
│   ├── server.py                 ★ 服务入口（唯一需要跑的文件）
│   ├── web/index.html            前端页面（单文件自包含，图片已内联）
│   ├── seed/seed_tools.json      初始数据（默认留空；工具由运维后台上传）
│   ├── tools/                    随包分发的工具原件目录（默认留空，后台上传时放这里）
│   ├── deploy/
│   │   ├── deploy.sh             一键部署脚本
│   │   ├── nginx.conf.example    nginx 反代示例
│   │   ├── ai-tools.service      systemd 常驻服务示例
│   │   └── docker/               容器化三个文件（Dockerfile / compose / 说明）
│   ├── tests/                   回归测试（4 个 Python 零依赖 + 3 个 Node 需 puppeteer）
│   ├── wecom_config.example.json 企微配置模板（复制成 wecom_config.json 填值）
│   ├── accounts_preset.example.json 预置账号模板
│   ├── dev_check.sh              一键体检（部署完在服务器上跑）
│   └── requirements.txt          注明：零第三方依赖
├── docs/
│   ├── 服务器部署与企微扫码登录上线手册.md   ★ 详细版部署手册
│   ├── 后端接口契约-工具库.md               接口文档
│   └── AI工具库-账号权限系统需求文档.md      需求文档
└── build_zip.py                  打包脚本（生成 server.zip / docker.zip）
```

---

## 6. 配置

### 企微配置 `wecom_config.json`

```bash
cp ai-tools-server/wecom_config.example.json ai-tools-server/wecom_config.json
# 然后填 corp_id / agent_id / secret / base_url
```

或直接跑 `deploy.sh`——它会自动生成并设好权限 600。

| 字段 | 说明 |
|---|---|
| `corp_id` | 企微 CorpID |
| `agent_id` | 自建应用 AgentId |
| `secret` | 自建应用 Secret |
| `base_url` | 本服务对外地址（必须是 https），如 `https://aitoolbox.maihaoche.com` |
| `redirect` | 上报给企微的回调地址。留空自动取 `{base_url}/api/login/wecom/callback` |
| `admin_userids` | 管理员白名单数组。填你的企微 userid，扫码后即为管理员 |
| `trusted_ips` | 已加进企微「企业可信 IP」的 IP 数组。体检时自动跟企微实际看到的出口 IP 比对，不一致就报警 |

> **改这个文件保存即生效，不用重启服务**（已做热重载）。

### 管理员怎么来的

| 角色 | 怎么设 |
|---|---|
| **企微管理员**（能改企微后台） | 后台「我的企业 → 权限管理 → 管理员 → 右上角 +」选自己。前提：必须先实名认证 |
| **本系统管理员**（能进用户管理 / 审计日志） | `wecom_config.json` 的 `admin_userids` 填你的企微 userid |

> ⚠️ 不配 `admin_userids` 会形成死循环：用户管理页只有管理员能进，而扫码新用户默认是普通用户——「要进后台才能给自己升权」。配了之后该 userid 扫码即管理员。

---

## 7. 出问题了怎么办（**不用重新部署**）

| 现象 | 原因 | 怎么修 | 要重新部署吗 |
|---|---|---|---|
| 域名 503 | 后端没起来或 nginx 上游不对 | `systemctl status ai-tools` / `ss -lntp` | 否 |
| 登录页「扫码登录」变灰 | 企微三项配置缺 | 跑 `dev_check.sh` 看差哪项 | 否 |
| 提示 `-31020` | 授权回调域没配或填错 | 按 §4.1 填域名 | 否 |
| 提示 `60020` | 可信 IP 没加 | 按 §4.2 加服务器出口 IP | 否 |
| 体检「可信 IP 核对」红 | 登记的 IP ≠ 企微看到的出口 IP | 照着提示把正确的 IP 加进白名单 | 否 |
| 扫码成功但登不进来 | 同上（换身份接口也校验 IP） | 同上 | 否 |
| 体检只显示「缺凭据」 | —— | **已修复**：现在凭据不对时其余各项也会照常显示 | 否 |
| 改了配置不生效 | 文件权限或格式 | `ls -l wecom_config.json`（应 600）、`python3 -c "import json;json.load(open('wecom_config.json'))"` | 否 |

**所有企微侧配置都是后台即时生效的，服务器侧配置保存即热重载——只有改代码才需要重新部署。**

体检接口也可以直接查：

```bash
curl http://127.0.0.1:8080/api/login/wecom/diagnose
# 逐项返回 ok / detail / action，action 就是「该去后台哪里填什么」
```

---

## 8. 常用命令

```bash
# 服务管理
sudo systemctl start|stop|restart ai-tools
sudo systemctl status ai-tools
sudo journalctl -u ai-tools -f        # 看实时日志（首次启动的随机管理员密码在这里）

# 体检
bash ai-tools-server/dev_check.sh

# 健康检查
curl http://127.0.0.1:8080/api/health

# 备份数据（SQLite 单文件 + uploads 目录）
tar czf ai-tools-backup-$(date +%F).tar.gz ai-tools-server/data/
```

---

## 9. 数据与容器

- **数据库**：SQLite 单文件 `data/tools.db`，不用装数据库服务
- **上传附件**：`data/uploads/`
- **随包工具原件**：`tools/`（只读，走 `/tools/<文件名>` 下载，**与 data 目录无关，重装不丢**）。仓库内默认为空目录，运维上传工具时把原件放这里

容器化部署（可选）：

```bash
cd ai-tools-server/deploy/docker
cp ../../wecom_config.json .          # 配置挂载进容器
docker build -t ai-tools:1.0 ..
docker run -d --name ai-tools --restart unless-stopped \
  -p 8080:8080 -v /data/ai-tools:/app/data ai-tools:1.0
```

---

## 10. 想改代码

```bash
# 本地跑（零依赖）
cd ai-tools-server && PORT=8080 python3 server.py

# 跑回归测试（Python 四个零依赖）
python3 ai-tools-server/tests/test_wecom_qr.py        # 企微二维码链路
python3 ai-tools-server/tests/test_wecom_login.py      # 企微登录链路
python3 ai-tools-server/tests/test_admin_whitelist.py  # 管理员白名单
python3 ai-tools-server/tests/test_trusted_ip.py       # 可信 IP 自动核对（离线，不打企微接口）

# 前端 UI 测试（需 puppeteer：npm i -D puppeteer，或设 PUPPETEER_PATH 指向安装目录）
# 这三个都要先起服务：cd ai-tools-server && PORT=8080 python3 server.py
node ai-tools-server/tests/test_login_ui.js           http://127.0.0.1:8080
node ai-tools-server/tests/test_upload_login_gate.js  http://127.0.0.1:8080
node ai-tools-server/tests/test_upload_real_login.js  http://127.0.0.1:8080 guest 'guest@2026'
```

> ⚠️ `test_upload_real_login.js` 登录的是**真实数据库里的账号**。密码不在 git 里（`accounts_preset.json` 是 gitignore 文件），
> 所以**新建的 clone 必须先建预置账号再跑这个测试**，否则会报「用户名或密码错误」：
>
> ```bash
> cp ai-tools-server/accounts_preset.example.json ai-tools-server/accounts_preset.json
> # 编辑它，把 password 改成你要用的密码（guest 账号的密码要与测试命令行传的密码一致）
> cd ai-tools-server && PORT=8080 python3 server.py   # 首次启动自动导入预置账号
> ```
>
> 前两个前端测试不涉及账号，clone 完直接可跑。

改完重新打包：

```bash
python3 build_zip.py    # 生成 ai-tools-server.zip / ai-tools-docker.zip
```

> ⚠️ 打包**必须用 `build_zip.py`**，不要用系统 `zip`——macOS 的 zip 会把中文文件名按 CP437 存且不设 UTF-8 标志位，Linux 解压后文件名乱码，工具附件全部 404。

---

## 11. 常见问题

**Q：端口 8080 被占用怎么办？**
`deploy.sh` 会自动从 nginx vhost 读出真实端口（也可手动传 `PORT=xxxx`）。

**Q：首次启动管理员密码在哪？**
`sudo journalctl -u ai-tools | grep -A2 管理员`。若 `accounts_preset.json` 存在则用预设账号，否则自动生成随机密码并打印在日志里。

**Q：怎么把网站里已有的工具清空重来？**
`python3 ai-tools-server/server.py --reseed`（按 `seed/seed_tools.json` 重置，当前为空即重置成空站点）。
⚠️ **别用 `--clear`**：它会清空工具表并打上「不再自动导入种子」的标记，重启也不会灌回来，站点会一直是空的。

**Q：改前端页面？**
前端是单文件 `ai-tools-server/web/index.html`，图片已内联，**不需要构建**。改完覆盖服务器上的同名文件，重启服务即可。
