"""
QuantX 数据源基类

定义所有市场数据适配器必须实现的抽象接口，以及统一的 K 线格式。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone

from app.utils.logger import get_logger

logger = get_logger(__name__)


# 时间周期 → 秒数映射
TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1H": 3600,
    "4H": 14400,
    "1D": 86400,
    "1W": 604800,
}


class BaseDataSource(ABC):
    """市场数据源抽象基类。

    所有子类必须实现 ``get_kline``；``get_ticker`` 为可选接口。
    K 线统一格式::

        {
            "time": int,      # UTC Unix 秒
            "open": float,
            "high": float,
            "low": float,
            "close": float,
            "volume": float,
        }
    """

    name: str = "base"

    # ------------------------------------------------------------------
    # 抽象接口
    # ------------------------------------------------------------------

    @abstractmethod
    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。

        Args:
            symbol: 品种代码（如 ``600519``, ``AAPL``）
            timeframe: K 线周期（``1m``, ``5m``, ``1H``, ``1D``, ``1W`` 等）
            limit: 请求条数
            before_time: 获取此时间戳（Unix 秒）**之前**的数据
            after_time: 可选左边界，仅保留 ``time >= after_time`` 的行

        Returns:
            按 ``time`` 升序排列的 K 线列表
        """
        ...

    # ------------------------------------------------------------------
    # 可选接口
    # ------------------------------------------------------------------

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取品种最新行情（best-effort）。

        默认抛出 ``NotImplementedError``，子类按需实现。
        """
        raise NotImplementedError(f"{self.name} 未实现 get_ticker")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def format_kline(
        self,
        timestamp: int,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> Dict[str, Any]:
        """将原始数据标准化为统一 K 线格式。"""
        return {
            "time": int(timestamp),
            "open": round(float(open_price), 4),
            "high": round(float(high), 4),
            "low": round(float(low), 4),
            "close": round(float(close), 4),
            "volume": round(float(volume), 2),
        }

    def filter_and_limit(
        self,
        klines: List[Dict[str, Any]],
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        truncate: bool = True,
    ) -> List[Dict[str, Any]]:
        """过滤并截取 K 线行数。

        Args:
            klines: 原始 K 线列表
            limit: 最大保留条数
            before_time: 仅保留 ``time < before_time``
            after_time: 仅保留 ``time >= after_time``
            truncate: 为 False 时不按 limit 截断（回测场景需要完整窗口）
        """
        klines.sort(key=lambda x: x["time"])

        if before_time:
            klines = [k for k in klines if k["time"] < before_time]
        if after_time is not None:
            klines = [k for k in klines if k["time"] >= after_time]

        if truncate and len(klines) > limit:
            klines = klines[-limit:]

        return klines

    def calculate_time_range(
        self,
        timeframe: str,
        limit: int,
        buffer_ratio: float = 1.2,
    ) -> int:
        """计算获取 ``limit`` 根 K 线所需的时间范围（秒）。

        Args:
            timeframe: K 线周期
            limit: 请求条数
            buffer_ratio: 额外缓冲比例
        """
        seconds_per_candle = TIMEFRAME_SECONDS.get(timeframe, 86400)
        return int(seconds_per_candle * limit * buffer_ratio)

    def log_result(
        self,
        symbol: str,
        klines: List[Dict[str, Any]],
        timeframe: str,
    ) -> None:
        """记录获取结果的质量日志。

        检测最新 K 线时间戳与当前 UTC 时间的差值，
        超出阈值时输出 WARNING。
        """
        if not klines:
            logger.warning(f"{self.name}: {symbol} 无数据返回")
            return

        latest_ts = int(klines[-1]["time"])
        latest_utc = datetime.fromtimestamp(latest_ts, tz=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        time_diff = (now_utc - latest_utc).total_seconds()

        tf_sec = TIMEFRAME_SECONDS.get(timeframe, 3600)
        if tf_sec < 86400:
            max_diff = tf_sec * 2
        elif tf_sec == 86400:
            max_diff = 5 * 86400
        else:
            max_diff = max(tf_sec * 2, 21 * 86400)

        if time_diff > max_diff:
            logger.warning(
                f"{self.name}: {symbol} 数据延迟 "
                f"({time_diff:.0f}s, 最新K线={latest_utc.isoformat()}, "
                f"阈值={max_diff:.0f}s, tf={timeframe})"
            )
