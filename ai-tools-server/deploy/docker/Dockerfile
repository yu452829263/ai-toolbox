FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/app/data

WORKDIR /app

COPY server.py /app/
COPY web/ /app/web/
COPY requirements.txt /app/

# 工具数据：seed 是初始数据（当前为空数组），tools/ 是工具原件目录（默认留空，后台上传时放入）。
# 少了这两行，容器里工具库会是空的、附件也下载不到。
COPY seed/ /app/seed/
COPY tools/ /app/tools/

# ⚠️ 企微配置（含 corp_id / secret / admin_userids）与预置账号（含默认密码）
#    **故意不进交付包**（防密钥泄漏），所以这里只放模板，不能 COPY 真文件
#    ——COPY 一个不存在的文件会让 docker build 直接失败。
#    正式部署：在服务器上 cp 模板改好，再 volume 挂载覆盖（见 README-Docker.md）。
COPY wecom_config.example.json /app/wecom_config.example.json
COPY accounts_preset.example.json /app/accounts_preset.example.json

RUN mkdir -p /app/data/uploads /app/data/webroot

VOLUME /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)"

CMD ["python3", "server.py"]
