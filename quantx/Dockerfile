# QuantX Backend Dockerfile
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

WORKDIR /app

# 安装系统依赖（编译 psycopg2-binary 等需要）
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      build-essential \
      python3-dev \
      libffi-dev \
      libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary --timeout 180 -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential python3-dev libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制应用代码
COPY . .

# 创建日志和数据目录
RUN mkdir -p logs data

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=5000

# 使用 gunicorn 启动
CMD ["gunicorn", "-c", "gunicorn_config.py", "run:app"]
