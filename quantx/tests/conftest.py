"""
QuantX pytest 基础 fixtures
"""
import os
import sys
import pytest

# 确保测试时项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置测试环境变量
os.environ.setdefault("DATABASE_URL", "postgresql://quantx:quantx123@localhost:5432/quantx_test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SKIP_STARTUP_HOOKS", "true")


@pytest.fixture
def app():
    """创建测试用 Flask 应用"""
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Flask 测试客户端"""
    return app.test_client()


@pytest.fixture
def app_context(app):
    """Flask 应用上下文"""
    with app.app_context() as ctx:
        yield ctx
