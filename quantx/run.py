"""
QuantX 开发入口
用法: python run.py
"""
import os
import sys

# 确保 Windows 环境下控制台输出 UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 加载 .env 环境变量（本地开发便捷模式）
try:
    from dotenv import load_dotenv
    this_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(this_dir, ".env"), override=False)
except Exception:
    # python-dotenv 为可选依赖，也可直接通过系统环境变量提供
    pass

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.config.settings import settings

# 创建 Flask 应用实例（gunicorn 入口: gunicorn -c gunicorn_config.py "run:app"）
app = create_app()


def main():
    """启动 Flask 开发服务器"""
    print(f"QuantX API v{settings.VERSION}")

    # 安全检查：生产环境不允许使用默认 SECRET_KEY
    default_secret = "quantx-secret-key-change-me"
    if not settings.DEBUG and settings.SECRET_KEY == default_secret:
        import secrets as _secrets
        new_key = _secrets.token_hex(32)
        os.environ["SECRET_KEY"] = new_key
        settings.SECRET_KEY = new_key
        print("[AUTO] SECRET_KEY 使用默认值，已为本会话生成随机密钥。")
        print("[TIP]  请在 .env 中设置持久化的 SECRET_KEY 以用于生产环境。")

    print(f"服务启动地址: http://{settings.HOST}:{settings.PORT}")

    app.run(
        host=settings.HOST,
        port=settings.PORT,
        debug=settings.DEBUG,
        threaded=True,
    )


if __name__ == "__main__":
    main()
