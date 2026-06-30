"""
QuantX 数据库 / Redis 连接配置（dataclass 模式）

集中管理 PostgreSQL 连接池参数和 Redis 缓存参数。
所有配置优先从环境变量读取，提供合理默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class DatabaseConfig:
    """数据库和缓存连接配置"""

    # PostgreSQL
    DATABASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql://quantx:quantx123@localhost:5432/quantx",
        )
    )
    DB_POOL_MIN: int = field(default_factory=lambda: int(os.getenv("DB_POOL_MIN", 5)))
    DB_POOL_MAX: int = field(default_factory=lambda: int(os.getenv("DB_POOL_MAX", 50)))
    DB_POOL_ACQUIRE_TIMEOUT: int = field(
        default_factory=lambda: int(os.getenv("DB_POOL_ACQUIRE_TIMEOUT", 10))
    )

    # Redis
    REDIS_HOST: str = field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"))
    REDIS_PORT: int = field(default_factory=lambda: int(os.getenv("REDIS_PORT", 6379)))
    REDIS_DB: int = field(default_factory=lambda: int(os.getenv("REDIS_DB", 0)))

    # 缓存开关
    CACHE_ENABLED: bool = field(
        default_factory=lambda: os.getenv("ENABLE_CACHE", "false").lower() == "true"
    )

    # K线缓存 TTL（秒）
    KLINE_TTL_1M: int = field(default_factory=lambda: int(os.getenv("KLINE_TTL_1M", 120)))
    KLINE_TTL_5M: int = field(default_factory=lambda: int(os.getenv("KLINE_TTL_5M", 300)))
    KLINE_TTL_1H: int = field(default_factory=lambda: int(os.getenv("KLINE_TTL_1H", 1800)))
    KLINE_TTL_1D: int = field(default_factory=lambda: int(os.getenv("KLINE_TTL_1D", 86400)))


# 全局单例
db_config = DatabaseConfig()
