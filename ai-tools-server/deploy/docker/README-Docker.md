# AI 工具库 · 容器部署说明

## 包内容

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 构建镜像（python:3.12-slim 基础，零第三方依赖） |
| `docker-compose.yml` | 可选，用 compose 一键起 |
| `server.py` | 后端服务（SQLite 自动建库，无需安装数据库） |
| `web/index.html` | 前端页面（用这份替换服务器上原有的旧页面） |
| `requirements.txt` | 依赖说明（空，仅标准库） |

**镜像里没有演示数据**：种子文件已剔除，容器首次启动自动建库且直接是空库，不存在占位数据，重启也不会冒出来。

## 方式一：docker 命令（三步）

```bash
# 1. 解压后进入本目录，构建镜像
docker build -t ai-tools:1.0 .

# 2. 启动（数据目录挂载到宿主机，容器重建不丢数据）
docker run -d --name ai-tools --restart unless-stopped \
  -p 8080:8080 -v /data/ai-tools:/app/data ai-tools:1.0

# 3. 验证
curl http://127.0.0.1:8080/api/health
# 看到 "tools": 0 即成功（空库）
```

## 方式二：docker-compose

```bash
docker compose up -d --build
```

## nginx 接入（把域名指向容器）

把 `aitoolbox.maihaoche.com` 的静态页面配置改成反向代理：

```nginx
server {
    listen 443 ssl;
    server_name aitoolbox.maihaoche.com;

    ssl_certificate     /etc/nginx/ssl/aitoolbox.crt;   # 按公司实际证书路径改
    ssl_certificate_key /etc/nginx/ssl/aitoolbox.key;

    client_max_body_size 100M;                          # 上传附件需要

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

改完 `nginx -s reload`。

## 数据在哪

- 容器内：`/app/data/tools.db`（SQLite，自动生成）+ `/app/data/uploads/`（附件）
- 宿主机：挂载卷 `/data/ai-tools/`，**备份就拷这个目录**

## 验收清单

1. `docker ps` → ai-tools 状态 Up（healthy）
2. `curl http://127.0.0.1:8080/api/health` → `"tools": 0`
3. 浏览器打开首页 → 「已上传工具」为 0、列表空态提示（记得 Ctrl+F5 强刷，别看缓存的旧页面）
4. 上传一个测试工具 → 刷新后还在 → 完成

## 常用运维命令

```bash
docker logs -f ai-tools          # 看日志
docker restart ai-tools          # 重启（数据在卷里，不丢）
docker stop ai-tools && docker rm ai-tools   # 停止并删除容器（数据仍在 /data/ai-tools）
docker build -t ai-tools:1.0 . && docker run -d ...   # 升级后重建
```
