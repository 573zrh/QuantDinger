"""
QuantX 数据源工厂

根据市场类型返回对应的数据源实例。
采用懒加载 + 单例缓存策略，避免重复创建数据源对象。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.errors import UnsupportedMarketError
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------
# 市场别名映射
# ------------------------------------------------------------------

_MARKET_ALIASES: Dict[str, str] = {
    # A 股
    "cnstock": "CNStock",
    "cn_stock": "CNStock",
    "a_stock": "CNStock",
    "astock": "CNStock",
    "a股": "CNStock",
    "sh": "CNStock",
    "sz": "CNStock",
    # 港股
    "hkstock": "HKStock",
    "hk_stock": "HKStock",
    "hk": "HKStock",
    "港股": "HKStock",
    "h股": "HKStock",
    # 美股
    "usstock": "USStock",
    "us_stock": "USStock",
    "us_stocks": "USStock",
    "stock": "USStock",
    "stocks": "USStock",
    "equity": "USStock",
    "equities": "USStock",
    "美股": "USStock",
    # 中国期货
    "cnfutures": "CNFutures",
    "cn_futures": "CNFutures",
    "futures": "CNFutures",
    "期货": "CNFutures",
    "中国期货": "CNFutures",
}


class DataSourceFactory:
    """数据源工厂。

    K 线 / 报价使用哪个接口完全由调用方传入的 ``market`` 决定。
    不做根据 symbol 字符串的推断。
    """

    _sources: Dict[str, BaseDataSource] = {}

    # 标准市场名称（经过 normalize_market 后不变的值）
    _CANONICAL_MARKETS = ("CNStock", "HKStock", "USStock", "CNFutures")

    @classmethod
    def normalize_market(cls, market: str) -> str:
        """标准化市场字符串。

        Args:
            market: 原始市场标识（如 ``"a_stock"``, ``"HKStock"``）

        Returns:
            标准市场名称（如 ``"CNStock"``, ``"HKStock"``）

        Raises:
            不会抛出异常；未知输入原样返回，由 ``get_source`` 处理
        """
        if not market:
            logger.warning(
                "DataSourceFactory.normalize_market(): 空市场标识 — "
                "调用方必须传入明确的市场类型（CNStock/HKStock/USStock/CNFutures）"
            )
            return ""
        raw = str(market).strip()
        if raw in cls._CANONICAL_MARKETS:
            return raw
        key = raw.lower().replace(" ", "").replace("-", "_")
        if key in _MARKET_ALIASES:
            return _MARKET_ALIASES[key]
        logger.warning(
            f"DataSourceFactory.normalize_market(): 未知市场 {raw!r} — "
            "原样返回，get_source() 可能会失败"
        )
        return raw

    @classmethod
    def get_source(cls, market: str) -> BaseDataSource:
        """获取指定市场的数据源（懒加载 + 单例缓存）。

        Args:
            market: 市场类型

        Returns:
            数据源实例

        Raises:
            UnsupportedMarketError: 市场类型不受支持
        """
        market = cls.normalize_market(market or "")
        if not market:
            raise UnsupportedMarketError(market)
        if market not in cls._sources:
            cls._sources[market] = cls._create_source(market)
        return cls._sources[market]

    @classmethod
    def _create_source(cls, market: str) -> BaseDataSource:
        """根据市场名称创建对应的数据源实例"""
        if market == "CNStock":
            from app.data_sources.cn_stock import CNStockDataSource
            return CNStockDataSource()
        elif market == "HKStock":
            from app.data_sources.hk_stock import HKStockDataSource
            return HKStockDataSource()
        elif market == "USStock":
            from app.data_sources.us_stock import USStockDataSource
            return USStockDataSource()
        elif market == "CNFutures":
            from app.data_sources.cn_futures import CNFuturesDataSource
            return CNFuturesDataSource()
        else:
            raise UnsupportedMarketError(market)

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    @classmethod
    def get_kline(
        cls,
        market: str,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据的便捷方法。

        Args:
            market: 市场类型
            symbol: 品种代码
            timeframe: 时间周期
            limit: 数据条数
            before_time: 获取此时间之前的数据（Unix 秒）
            after_time: 可选左边界

        Returns:
            K 线数据列表（按 time 升序）
        """
        try:
            source = cls.get_source(market)
            klines = source.get_kline(symbol, timeframe, limit, before_time, after_time)
            klines.sort(key=lambda x: x["time"])
            return klines
        except Exception as e:
            logger.error(f"K 线获取失败 {market}:{symbol} — {e}")
            return []

    @classmethod
    def get_ticker(cls, market: str, symbol: str) -> Dict[str, Any]:
        """获取实时报价的便捷方法。

        Args:
            market: 市场类型
            symbol: 品种代码

        Returns:
            实时报价数据
        """
        try:
            source = cls.get_source(market)
            return source.get_ticker(symbol)
        except NotImplementedError:
            logger.warning(f"get_ticker 未实现: {market}")
            return {"last": 0, "symbol": symbol}
        except Exception as e:
            logger.error(f"行情获取失败 {market}:{symbol} — {e}")
            return {"last": 0, "symbol": symbol}

    @classmethod
    def list_markets(cls) -> List[str]:
        """返回所有支持的标准市场名称"""
        return list(cls._CANONICAL_MARKETS)
