"""
QuantX 缓存管理器

自动选择 Redis 或内存缓存：
- 当 ``ENABLE_CACHE=true`` 且 Redis 可用时使用 Redis
- 否则回退到线程安全的内存缓存（MemoryCache）

提供统一的 get / set / delete 接口，内部使用 JSON 序列化。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class MemoryCache:
    """线程安全的内存缓存（带 TTL 支持）"""

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            if key in self._cache:
                data, expiry = self._cache[key]
                if expiry > time.time():
                    return data
                del self._cache[key]
            return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            self._cache[key] = (value, time.time() + ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


class CacheManager:
    """缓存管理器（单例模式）。

    优先使用 Redis，不可用时自动回退到 MemoryCache。
    """

    _instance: Optional[CacheManager] = None
    _lock = threading.Lock()

    def __new__(cls) -> CacheManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._client: Any = None
        self._use_redis = False

        # 从 DatabaseConfig 读取配置
        from app.config.database import db_config

        if not db_config.CACHE_ENABLED:
            self._client = MemoryCache()
            self._use_redis = False
            logger.info("缓存: 使用内存缓存（ENABLE_CACHE=false）")
            return

        # 尝试连接 Redis
        try:
            import redis
            self._client = redis.Redis(
                host=db_config.REDIS_HOST,
                port=db_config.REDIS_PORT,
                db=db_config.REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            self._client.ping()
            self._use_redis = True
            logger.info(f"Redis 缓存已连接 ({db_config.REDIS_HOST}:{db_config.REDIS_PORT})")
        except Exception as e:
            logger.info(f"Redis 不可用，回退到内存缓存: {e}")
            self._client = MemoryCache()
            self._use_redis = False

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        """读取缓存值（自动 JSON 反序列化）"""
        try:
            data = self._client.get(key)
            if data is not None:
                return json.loads(data)
            return None
        except Exception as e:
            logger.error(f"缓存读取失败: {e}")
            return None

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """写入缓存（自动 JSON 序列化）"""
        try:
            self._client.setex(key, ttl, json.dumps(value, default=str))
        except Exception as e:
            logger.error(f"缓存写入失败: {e}")

    def delete(self, key: str) -> None:
        """删除缓存键"""
        try:
            self._client.delete(key)
        except Exception as e:
            logger.error(f"缓存删除失败: {e}")

    @property
    def is_redis(self) -> bool:
        """当前是否使用 Redis"""
        return self._use_redis
