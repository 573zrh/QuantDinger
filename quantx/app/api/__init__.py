"""
QuantX API 路由注册

使用 flask-smorest 初始化 OpenAPI，并注册所有 Blueprint。
"""
from __future__ import annotations

from flask import Flask
from flask_smorest import Api

from app.config.settings import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


def register_routes(app: Flask) -> None:
    """
    初始化 flask-smorest Api 并注册所有 Blueprint。

    Args:
        app: Flask 应用实例
    """
    # 配置 flask-smorest / OpenAPI
    app.config["API_TITLE"] = "QuantX API"
    app.config["API_VERSION"] = settings.VERSION
    app.config["OPENAPI_VERSION"] = "3.0.3"
    app.config["OPENAPI_URL_PREFIX"] = "/"
    app.config.setdefault(
        "OPENAPI_DESCRIPTION",
        "QuantX 量化交易后端 REST API",
    )

    # 开启 Swagger UI（开发和调试用）
    app.config.setdefault("OPENAPI_SWAGGER_UI_PATH", "/api/docs/swagger")
    app.config.setdefault(
        "OPENAPI_SWAGGER_UI_URL",
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist/",
    )

    api = Api(app)

    # 注册健康检查 Blueprint
    from app.api.health import blp as health_blp
    api.register_blueprint(health_blp)

    # 注册认证 Blueprint
    from app.api.auth import blp as auth_blp
    api.register_blueprint(auth_blp)

    # 注册策略 Blueprint
    from app.api.strategy import blp as strategy_blp
    api.register_blueprint(strategy_blp)

    # 注册行情数据 Blueprint
    from app.api.market import blp as market_blp
    api.register_blueprint(market_blp)

    # 注册回测 Blueprint
    from app.api.backtest import blp as backtest_blp
    api.register_blueprint(backtest_blp)

    # 注册交易管理 Blueprint
    from app.api.trade import blp as trade_blp
    api.register_blueprint(trade_blp)

    logger.info("API 路由注册完成")

    # ── Agent Gateway v1（独立 Flask Blueprint，非 smorest）──
    from app.api.agent_v1 import register as register_agent_v1
    register_agent_v1(app)
    logger.info("Agent Gateway v1 注册完成")
