# AgentCore Runtime 要求 linux/arm64;HTTP 协议监听 8080
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    DOCKER_CONTAINER=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝框架、agent 入口、配置
COPY framework/ ./framework/
COPY agents/ ./agents/
COPY hierarchy.json .
COPY control/ ./control/

RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8080 9000

# ENTRY_FILE 指定跑哪个 agent 入口(supervisor 或 leaf),部署时按 agent 设置
ENV ENTRY_FILE=agents/supervisor/ws_main.py
CMD ["sh", "-c", "python -u $ENTRY_FILE"]
