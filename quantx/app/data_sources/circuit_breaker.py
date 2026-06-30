"""
QuantX 熔断器（Circuit Breaker）

三态熔断器，用于管理数据源的可用性：
- CLOSED（正常）→ 连续失败 N 次 → OPEN（熔断）
- OPEN → 冷却时间到 → HALF_OPEN（试探恢复）
- HALF_OPEN → 成功 → CLOSED；失败 → OPEN

线程安全实现，每个数据源独立维护状态。
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"        # 正常
    OPEN = "open"            # 熔断（不可用）
    HALF_OPEN = "half_open"  # 半开（试探性请求）


class CircuitBreaker:
    """三态熔断器。

    Args:
        failure_threshold: 连续失败多少次后进入熔断
        recovery_timeout:  熔断冷却时间（秒）
        half_open_max_calls: 半开状态允许的最大试探请求数
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 内部状态管理
    # ------------------------------------------------------------------

    def _get_state(self, source: str) -> Dict[str, Any]:
        """获取或初始化指定数据源的状态记录。"""
        if source not in self._states:
            self._states[source] = {
                "state": CircuitState.CLOSED,
                "failures": 0,
                "last_failure_time": 0.0,
                "half_open_calls": 0,
                "last_error": None,
            }
        return self._states[source]

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def is_available(self, source: str) -> bool:
        """检查数据源是否可用。

        Returns:
            True — 可以尝试请求
            False — 应跳过该数据源
        """
        with self._lock:
            info = self._get_state(source)
            now = time.time()

            if info["state"] == CircuitState.CLOSED:
                return True

            if info["state"] == CircuitState.OPEN:
                elapsed = now - info["last_failure_time"]
                if elapsed >= self.recovery_timeout:
                    info["state"] = CircuitState.HALF_OPEN
                    info["half_open_calls"] = 0
                    logger.info(f"[熔断器] {source} 冷却完成，进入半开状态")
                    return True
                remaining = self.recovery_timeout - elapsed
                logger.debug(f"[熔断器] {source} 熔断中，剩余冷却 {remaining:.0f}s")
                return False

            if info["state"] == CircuitState.HALF_OPEN:
                return info["half_open_calls"] < self.half_open_max_calls

            return True

    def record_success(self, source: str) -> None:
        """记录一次成功请求，重置熔断状态。"""
        with self._lock:
            info = self._get_state(source)
            if info["state"] == CircuitState.HALF_OPEN:
                logger.info(f"[熔断器] {source} 半开试探成功，恢复正常")
            info["state"] = CircuitState.CLOSED
            info["failures"] = 0
            info["half_open_calls"] = 0
            info["last_error"] = None

    def record_failure(self, source: str, error: Optional[str] = None) -> None:
        """记录一次失败请求，累计失败计数。"""
        with self._lock:
            info = self._get_state(source)
            now = time.time()

            info["failures"] += 1
            info["last_failure_time"] = now
            info["last_error"] = error

            if info["state"] == CircuitState.HALF_OPEN:
                info["state"] = CircuitState.OPEN
                info["half_open_calls"] = 0
                logger.warning(
                    f"[熔断器] {source} 半开试探失败，继续熔断 "
                    f"{self.recovery_timeout}s"
                )
            elif info["failures"] >= self.failure_threshold:
                info["state"] = CircuitState.OPEN
                logger.warning(
                    f"[熔断器] {source} 连续失败 {info['failures']} 次，"
                    f"进入熔断（冷却 {self.recovery_timeout}s）"
                )
                if error:
                    logger.warning(f"[熔断器] 最后错误: {error}")

            # 半开状态下增加调用计数
            if info["state"] == CircuitState.HALF_OPEN:
                info["half_open_calls"] += 1

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有数据源的熔断状态快照。"""
        with self._lock:
            return {
                source: {
                    "state": info["state"].value,
                    "failures": info["failures"],
                    "last_error": info["last_error"],
                }
                for source, info in self._states.items()
            }

    def reset(self, source: Optional[str] = None) -> None:
        """重置熔断器状态。

        Args:
            source: 指定数据源名称；为 None 时重置所有
        """
        with self._lock:
            if source:
                self._states.pop(source, None)
                logger.info(f"[熔断器] 已重置 {source}")
            else:
                self._states.clear()
                logger.info("[熔断器] 已重置所有数据源")


# ------------------------------------------------------------------
# 全局共享实例
# ------------------------------------------------------------------

# K 线数据源熔断器（较宽容：5 次失败，60s 冷却）
_kline_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60.0,
    half_open_max_calls=1,
)

# 实时行情熔断器（较严格：2 次失败，180s 冷却）
_realtime_circuit_breaker = CircuitBreaker(
    failure_threshold=2,
    recovery_timeout=180.0,
    half_open_max_calls=1,
)


def get_kline_circuit_breaker() -> CircuitBreaker:
    """获取 K 线数据源熔断器"""
    return _kline_circuit_breaker


def get_realtime_circuit_breaker() -> CircuitBreaker:
    """获取实时行情熔断器"""
    return _realtime_circuit_breaker
