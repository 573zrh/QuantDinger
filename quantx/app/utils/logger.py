"""
QuantX 日志工具

提供统一的日志配置和获取接口。
使用 Python 标准 logging 模块，支持 RotatingFileHandler 滚动日志文件。
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config.settings import settings


def setup_logger() -> None:
    """配置全局日志：控制台 + 滚动文件"""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # 根日志配置（控制台输出）
    logging.basicConfig(level=log_level, format=log_format)

    # 降低第三方库日志噪音
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # 确保日志目录存在
    log_dir = settings.LOG_DIR
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # 滚动文件日志（10MB * 5 个备份）
    file_handler = RotatingFileHandler(
        settings.get_log_path(),
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志记录器

    Args:
        name: 日志记录器名称（通常传入 __name__）

    Returns:
        Logger 实例
    """
    return logging.getLogger(name)
