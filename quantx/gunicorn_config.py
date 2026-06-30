"""
QuantX Gunicorn 生产环境配置

使用 gthread worker（单进程多线程）模式，在保持单进程简洁性的同时
提供并发 I/O 能力。通过 GUNICORN_WORKERS 增加进程数以提升多核吞吐量。
"""
import os

bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '5000')}"

# 默认：1 worker + 4 threads，与 Flask 开发服务器并发模型一致
workers = int(os.getenv("GUNICORN_WORKERS", 1))
threads = int(os.getenv("GUNICORN_THREADS", 4))

worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5

# 禁止 preload，确保后台线程在 worker 进程内启动（避免 fork 后丢失）
preload_app = False

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")

limit_request_line = 8190
limit_request_fields = 100
