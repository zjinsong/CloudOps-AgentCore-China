FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir bedrock-agentcore==1.24.0
COPY app.py .
RUN useradd -m -u 1000 appuser
USER appuser
EXPOSE 8080
CMD ["python", "app.py"]
