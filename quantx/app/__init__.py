"""
QuantX Flask 应用工厂

参考 QuantDinger 架构，提供精简的 Flask 应用创建流程：
1. SafeJSONProvider（处理 NaN / Inf / datetime 序列化）
2. CORS 配置
3. 数据库初始化
4. 路由注册
5. 启动钩子
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime

from flask import Flask
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS

from app.utils.logger import get_logger, setup_logger
from app.utils.timeutil import to_utc_iso

logger = get_logger(__name__)


class SafeJSONProvider(DefaultJSONProvider):
    """JSON 序列化提供者：自动处理 NaN / Inf / datetime"""

    @staticmethod
    def default(o):
        if isinstance(o, datetime):
            return to_utc_iso(o)
        if isinstance(o, date):
            return o.isoformat()
        return DefaultJSONProvider.default(o)

    def dumps(self, obj, **kwargs):
        kwargs.setdefault("default", self.default)
        return _safe_json_dumps(obj, **kwargs)


def _safe_json_dumps(obj, **kwargs):
    return json.dumps(_sanitize(obj), **kwargs)


def _sanitize(obj):
    """递归清理 JSON 不可序列化的值"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, datetime):
        return to_utc_iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _configure_cors(app: Flask) -> None:
    """配置 CORS 允许来源"""
    from app.config.settings import settings
    origins = [
        o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()
    ]
    # 添加本地开发常见来源
    local_origins = [
        "https://localhost",
        "http://localhost",
        "capacitor://localhost",
        "ionic://localhost",
    ]
    for origin in local_origins:
        if origin not in origins:
            origins.append(origin)

    CORS(app, origins=origins, supports_credentials=False, send_wildcard=False)
    logger.info(f"CORS 允许来源: {origins}")


def _bootstrap_database() -> None:
    """初始化数据库连接池并执行 Schema 迁移"""
    try:
        from app.utils.db import init_database
        init_database()

        # 执行 init.sql Schema 初始化（幂等：使用 CREATE TABLE IF NOT EXISTS）
        _run_schema_init()

    except Exception as e:
        logger.warning(f"数据库初始化提示: {e}")


def _run_schema_init() -> None:
    """读取并执行 migrations/init.sql"""
    import pathlib
    sql_path = pathlib.Path(__file__).parent.parent / "migrations" / "init.sql"
    if not sql_path.exists():
        logger.warning(f"未找到 Schema 文件: {sql_path}")
        return

    try:
        from app.utils.db import get_connection
        sql = sql_path.read_text(encoding="utf-8")
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            conn.commit()
        logger.info("数据库 Schema 初始化完成")
    except Exception as e:
        logger.warning(f"Schema 初始化跳过（可能已存在）: {e}")


def create_app(config_name: str = "default") -> Flask:
    """
    创建并配置 Flask 应用

    Args:
        config_name: 配置名称（预留，当前未使用）

    Returns:
        配置完成的 Flask 应用实例
    """
    app = Flask(__name__)
    app.json_provider_class = SafeJSONProvider
    app.json = SafeJSONProvider(app)
    app.config["JSON_AS_ASCII"] = False

    # 配置 CORS
    _configure_cors(app)

    # 初始化日志
    setup_logger()

    # 初始化数据库
    _bootstrap_database()

    # 注册路由
    from app.api import register_routes
    register_routes(app)

    # 运行启动钩子
    from app.startup import run_startup_hooks
    run_startup_hooks(app)

    return app
