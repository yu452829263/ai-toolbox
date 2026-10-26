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
COPY accounts_preset.json /app/

# 真实工具数据：seed 是 9 个真实工具的元数据，tools/ 是它们的原件。
# 少了这两行，容器里工具库会是空的、附件也下载不到。
COPY seed/ /app/seed/
COPY tools/ /app/tools/

# 企微扫码登录配置（corp_id / secret / admin_userids 等）。
# 镜像内先放一份空模板；正式部署建议用 volume 挂载覆盖，
# 这样改配置不用重新 build 镜像。
COPY wecom_config.json /app/

RUN mkdir -p /app/data/uploads

VOLUME /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)"

CMD ["python3", "server.py"]
