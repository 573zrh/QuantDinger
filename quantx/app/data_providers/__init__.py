"""
QuantX 数据提供者层 — SWR 缓存 + Single-flight

核心功能：
- ``cached_or_compute``: 缓存优先包装器，支持 stale-while-revalidate (SWR)
- Single-flight: 同一缓存键的并发请求只执行一次计算
- ``set_cached`` / ``get_cached``: 兼容接口
- ``clear_cache``: 清除所有 ``dp:*`` 前缀的缓存键
- ``safe_float``: 安全类型转换工具

SWR 模式的意义：上游数据源（AKShare、yfinance 等）通常较慢且有频率限制。
不使用 SWR 时，TTL 过期瞬间会触发多个并发请求；使用 SWR 后，触发刷新的
用户仍可立即获取旧值，同时后台异步更新缓存。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------
# 缓存 TTL 表（各类数据的默认缓存时长，单位：秒）
# ------------------------------------------------------------------

CACHE_TTL = {
    "kline_1m": 120,       # 1 分钟 K 线：2 分钟
    "kline_5m": 300,       # 5 分钟 K 线：5 分钟
    "kline_15m": 600,      # 15 分钟 K 线：10 分钟
    "kline_1h": 1800,      # 1 小时 K 线：30 分钟
    "kline_4h": 3600,      # 4 小时 K 线：1 小时
    "kline_1d": 86400,     # 日线：24 小时
    "kline_1w": 604800,    # 周线：7 天
    "ticker": 30,           # 实时行情：30 秒
    "market_overview": 120, # 市场概览：2 分钟
    "search": 3600,         # 搜索结果：1 小时
}

_DEFAULT_TTL = 60

# SWR 乘数：软 TTL 过期后，在 hard_ttl * SWR_MULTIPLIER 范围内仍可返回旧值
SWR_MULTIPLIER = 5


# ------------------------------------------------------------------
# Single-flight + SWR 基础设施
# ------------------------------------------------------------------

# 每个缓存键一把锁，确保同一进程内同一时刻只有一个线程在计算
_inflight_locks: dict[str, threading.Lock] = {}
_inflight_master = threading.Lock()

# 追踪正在进行后台刷新的键，避免重复触发
_bg_refreshing: set[str] = set()
_bg_refreshing_lock = threading.Lock()

# 后台刷新线程池
_swr_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dp-swr")


def _get_lock(key: str) -> threading.Lock:
    """获取（或懒创建）指定键的计算锁"""
    lock = _inflight_locks.get(key)
    if lock is not None:
        return lock
    with _inflight_master:
        lock = _inflight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _inflight_locks[key] = lock
        return lock


def _cm():
    """懒加载 CacheManager 单例（避免导入时副作用）"""
    from app.utils.cache import CacheManager
    return CacheManager()


# ------------------------------------------------------------------
# 信封包装 / 解包
# ------------------------------------------------------------------

def _wrap_envelope(value: Any, hard_ttl: int) -> dict:
    """将缓存值包装为带新鲜度元数据的信封"""
    now = time.time()
    return {
        "__v": 1,
        "value": value,
        "stored_at": now,
        "hard_until": now + max(1, int(hard_ttl)),
    }


def _unwrap_envelope(envelope: Any) -> tuple[Optional[Any], bool]:
    """从信封中解包值和新鲜度。

    Returns:
        (value, is_fresh)：旧格式数据被视为新鲜
    """
    if not isinstance(envelope, dict) or envelope.get("__v") != 1:
        if envelope is None:
            return None, False
        return envelope, True
    value = envelope.get("value")
    hard_until = float(envelope.get("hard_until") or 0)
    return value, time.time() < hard_until


# ------------------------------------------------------------------
# 公开 API
# ------------------------------------------------------------------

def cached_or_compute(
    key: str,
    compute: Callable[[], Any],
    *,
    ttl: Optional[int] = None,
    force: bool = False,
    allow_stale: bool = True,
) -> Any:
    """缓存优先包装器，支持 Single-flight + SWR。

    行为：
    1. ``force=True`` → 跳过缓存，直接计算并存储
    2. 缓存命中（新鲜）→ 返回缓存值
    3. 缓存命中（过期，SWR 窗口内）→ 返回旧值 + 后台刷新
    4. 缓存未命中 / SWR 窗口外 → 加锁，第一个线程计算，后续线程读取结果

    Args:
        key: 缓存键
        compute: 无参数计算函数（必须是 JSON 可序列化的返回值）
        ttl: 缓存时长（秒），None 时自动查 CACHE_TTL 表
        force: 强制刷新
        allow_stale: 是否允许返回过期值（SWR 模式）
    """
    effective_ttl = ttl or CACHE_TTL.get(key, _DEFAULT_TTL)

    if force:
        return _compute_and_store(key, compute, effective_ttl)

    raw = _cm().get(f"dp:{key}")
    value, is_fresh = _unwrap_envelope(raw)

    if value is not None and is_fresh:
        return value

    # SWR：立即返回旧值，后台刷新
    if value is not None and allow_stale:
        _schedule_background_refresh(key, compute, effective_ttl)
        return value

    # 完全未命中 — 加锁串行化
    lock = _get_lock(key)
    with lock:
        # double-check：其他线程可能已完成计算
        raw = _cm().get(f"dp:{key}")
        value, is_fresh = _unwrap_envelope(raw)
        if value is not None and is_fresh:
            return value
        return _compute_and_store(key, compute, effective_ttl)


def _compute_and_store(key: str, compute: Callable[[], Any], ttl: int) -> Any:
    """执行计算并持久化结果。计算失败时返回 None，不影响已有缓存。"""
    try:
        result = compute()
    except Exception as e:
        logger.error(f"缓存计算失败 [{key}]: {e}", exc_info=True)
        return None
    try:
        envelope = _wrap_envelope(result, ttl)
        _cm().set(f"dp:{key}", envelope, ttl=ttl * SWR_MULTIPLIER)
    except Exception as e:
        logger.error(f"缓存写入失败 [{key}]: {e}")
    return result


def _schedule_background_refresh(key: str, compute: Callable[[], Any], ttl: int) -> None:
    """为指定键调度后台刷新任务（去重）"""
    with _bg_refreshing_lock:
        if key in _bg_refreshing:
            return
        _bg_refreshing.add(key)

    def _runner():
        try:
            lock = _get_lock(key)
            if not lock.acquire(blocking=False):
                return  # 其他线程正在同步计算，跳过
            try:
                _compute_and_store(key, compute, ttl)
            finally:
                lock.release()
        finally:
            with _bg_refreshing_lock:
                _bg_refreshing.discard(key)

    try:
        _swr_executor.submit(_runner)
    except Exception as e:
        logger.warning(f"SWR 调度失败 [{key}]: {e}")
        with _bg_refreshing_lock:
            _bg_refreshing.discard(key)


# ------------------------------------------------------------------
# 兼容接口
# ------------------------------------------------------------------

def get_cached(key: str, ttl: Optional[int] = None) -> Optional[Any]:
    """读取缓存（仅在新鲜时返回）"""
    raw = _cm().get(f"dp:{key}")
    value, is_fresh = _unwrap_envelope(raw)
    if value is None or not is_fresh:
        return None
    return value


def set_cached(key: str, data: Any, ttl: Optional[int] = None) -> None:
    """写入缓存"""
    effective_ttl = ttl or CACHE_TTL.get(key, _DEFAULT_TTL)
    envelope = _wrap_envelope(data, effective_ttl)
    _cm().set(f"dp:{key}", envelope, ttl=effective_ttl * SWR_MULTIPLIER)


def clear_cache() -> None:
    """清除所有 ``dp:*`` 前缀的缓存键"""
    cm = _cm()
    if hasattr(cm, "_client") and hasattr(cm._client, "clear"):
        cm._client.clear()
    else:
        try:
            import redis as _redis
            if isinstance(cm._client, _redis.Redis):
                for k in cm._client.scan_iter("dp:*"):
                    cm._client.delete(k)
        except Exception:
            pass


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------

def safe_float(v: Any, default: float = 0.0) -> float:
    """安全地将值转换为 float，失败时返回 default"""
    try:
        return float(v)
    except Exception:
        return default
