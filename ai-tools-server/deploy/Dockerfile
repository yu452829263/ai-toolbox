# AI 成果库 · 容器化部署示例
# 构建：docker build -t ai-tools-server .
# 运行：docker run -d --name ai-tools -p 8080:8080 -v /data/ai-tools:/app/data ai-tools-server

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/app/data

WORKDIR /app

# 只拷代码；data 目录用卷挂载，保证容器重建不丢数据
COPY server.py /app/
COPY web/ /app/web/
COPY seed/ /app/seed/
COPY requirements.txt /app/

# 依赖目录（data）交给卷
RUN mkdir -p /app/data/uploads

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)"

CMD ["python3", "server.py"]
