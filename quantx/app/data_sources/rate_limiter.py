"""
QuantX 限流器（Rate Limiter）

提供请求频率控制，防止对第三方 API 的过度调用：
- 最小请求间隔
- 随机 jitter（防止请求同步）
- 线程安全

AKShare 数据源需要特殊限流策略（最小间隔 2s + 1.5-3.5s jitter）。
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """请求频率限制器。

    确保连续请求之间至少有 ``min_interval`` 秒的间隔，
    并叠加随机 jitter 以避免请求同步。

    Args:
        min_interval: 最小请求间隔（秒）
        jitter_min:   随机抖动下限（秒）
        jitter_max:   随机抖动上限（秒）
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        jitter_min: float = 0.5,
        jitter_max: float = 1.5,
    ):
        self.min_interval = min_interval
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self._last_request_time: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self) -> float:
        """阻塞直到可以发起下一次请求。

        Returns:
            实际等待的总时间（秒）
        """
        wait_time = 0.0
        with self._lock:
            if self._last_request_time is not None:
                elapsed = time.time() - self._last_request_time
                if elapsed < self.min_interval:
                    gap = self.min_interval - elapsed
                    time.sleep(gap)
                    wait_time += gap

            jitter = random.uniform(self.jitter_min, self.jitter_max)
            time.sleep(jitter)
            wait_time += jitter

            self._last_request_time = time.time()

        return wait_time

    def reset(self) -> None:
        """重置限流器状态。"""
        with self._lock:
            self._last_request_time = None


# ------------------------------------------------------------------
# 全局共享限流器实例
# ------------------------------------------------------------------

# AKShare 限流器（最严格：最小间隔 2s + 1.5-3.5s jitter）
_akshare_limiter = RateLimiter(
    min_interval=2.0,
    jitter_min=1.5,
    jitter_max=3.5,
)

# 腾讯行情限流器（中等：最小间隔 1s + 0.5-1.5s jitter）
_tencent_limiter = RateLimiter(
    min_interval=1.0,
    jitter_min=0.5,
    jitter_max=1.5,
)

# 通用限流器（宽松：最小间隔 0.5s + 0.2-0.8s jitter）
_general_limiter = RateLimiter(
    min_interval=0.5,
    jitter_min=0.2,
    jitter_max=0.8,
)


def get_akshare_limiter() -> RateLimiter:
    """获取 AKShare 专用限流器"""
    return _akshare_limiter


def get_tencent_limiter() -> RateLimiter:
    """获取腾讯行情限流器"""
    return _tencent_limiter


def get_general_limiter() -> RateLimiter:
    """获取通用限流器"""
    return _general_limiter
