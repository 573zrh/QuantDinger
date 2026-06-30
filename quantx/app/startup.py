"""
QuantX 启动钩子

在服务启动时执行：
1. 确保管理员账户存在
2. 启动 OrderWorker（异步订单处理）
3. 创建 TradingExecutor 单例
4. 恢复运行中的策略
"""
from __future__ import annotations

import os

from flask import Flask

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 全局单例
_trading_executor = None
_order_worker = None


def get_trading_executor():
    """获取/创建 TradingExecutor 单例"""
    global _trading_executor
    if _trading_executor is None:
        from app.services.trading_executor import TradingExecutor
        _trading_executor = TradingExecutor()
    return _trading_executor


def get_order_worker():
    """获取/创建 OrderWorker 单例"""
    global _order_worker
    if _order_worker is None:
        from app.workers.order_worker import OrderWorker
        _order_worker = OrderWorker()
    return _order_worker


def run_startup_hooks(app: Flask) -> None:
    """
    运行启动钩子（路由注册后调用）

    当前功能:
    - 记录启动信息
    - 确保管理员账户存在
    - 启动 OrderWorker
    - 恢复运行中的策略
    """
    skip_hooks = os.getenv("SKIP_STARTUP_HOOKS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if skip_hooks:
        logger.info("启动钩子已通过 SKIP_STARTUP_HOOKS 环境变量跳过")
        return

    with app.app_context():
        logger.info(f"QuantX API v{_get_version()} 启动钩子执行中...")

        # 确保管理员账户存在
        try:
            from app.services.user_service import ensure_admin_exists
            ensure_admin_exists()
        except Exception as e:
            logger.warning(f"管理员账户初始化跳过: {e}")

        # 启动 OrderWorker
        try:
            worker = get_order_worker()
            worker.start()
            logger.info("OrderWorker 启动成功")
        except Exception as e:
            logger.warning(f"OrderWorker 启动跳过: {e}")

        # 创建 TradingExecutor 并恢复运行中的策略
        try:
            executor = get_trading_executor()
            restored = executor.restore_running_strategies()
            if restored > 0:
                logger.info(f"已恢复 {restored} 个运行中的策略")
        except Exception as e:
            logger.warning(f"策略恢复跳过: {e}")

        logger.info("启动钩子执行完毕")


def _get_version() -> str:
    """获取应用版本号"""
    try:
        from app.config.settings import settings
        return settings.VERSION
    except Exception:
        return "unknown"
