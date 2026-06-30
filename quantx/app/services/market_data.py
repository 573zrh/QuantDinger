"""
QuantX 行情数据服务

封装 DataSourceFactory + 缓存层，为 API 路由提供统一的业务接口。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.data_sources.factory import DataSourceFactory
from app.data_providers import cached_or_compute, CACHE_TTL
from app.markets.registry import list_markets, get_market_module
from app.utils.logger import get_logger

logger = get_logger(__name__)


class MarketDataService:
    """行情数据服务（单例）"""

    # ------------------------------------------------------------------
    # K 线
    # ------------------------------------------------------------------

    @staticmethod
    def get_kline(
        market: str,
        symbol: str,
        timeframe: str = "1D",
        limit: int = 300,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据（带缓存）。

        缓存键格式: ``kline:{market}:{symbol}:{timeframe}:{limit}``
        使用 ``before_time`` 或 ``after_time`` 时跳过缓存（回测场景需要精确数据）。

        Args:
            market: 市场类型
            symbol: 品种代码
            timeframe: K 线周期
            limit: 数据条数
            before_time: 结束时间戳
            after_time: 开始时间戳
            force_refresh: 强制刷新缓存

        Returns:
            K 线数据列表
        """
        # 回测场景（带时间边界）不走缓存
        use_cache = before_time is None and after_time is None

        if not use_cache:
            return DataSourceFactory.get_kline(
                market, symbol, timeframe, limit, before_time, after_time,
            )

        # 根据 timeframe 选择缓存 TTL
        ttl_key = f"kline_{timeframe.lower()}"
        ttl = CACHE_TTL.get(ttl_key, CACHE_TTL.get("kline_1d", 86400))

        cache_key = f"kline:{market}:{symbol}:{timeframe}:{limit}"

        def compute():
            return DataSourceFactory.get_kline(market, symbol, timeframe, limit)

        try:
            result = cached_or_compute(
                cache_key, compute, ttl=ttl, force=force_refresh,
            )
            return result or []
        except Exception as e:
            logger.error(f"K 线服务异常 {market}:{symbol} {timeframe}: {e}")
            return []

    # ------------------------------------------------------------------
    # 实时行情
    # ------------------------------------------------------------------

    @staticmethod
    def get_ticker(market: str, symbol: str) -> Dict[str, Any]:
        """获取实时行情（短缓存 30s）。

        Args:
            market: 市场类型
            symbol: 品种代码

        Returns:
            行情数据字典
        """
        cache_key = f"ticker:{market}:{symbol}"

        def compute():
            return DataSourceFactory.get_ticker(market, symbol)

        try:
            result = cached_or_compute(
                cache_key, compute, ttl=CACHE_TTL.get("ticker", 30),
            )
            return result or {"last": 0, "symbol": symbol}
        except Exception as e:
            logger.error(f"行情服务异常 {market}:{symbol}: {e}")
            return {"last": 0, "symbol": symbol}

    # ------------------------------------------------------------------
    # 品种搜索
    # ------------------------------------------------------------------

    @staticmethod
    def search_symbols(market: str, keyword: str) -> List[Dict[str, Any]]:
        """搜索品种（简单关键词匹配）。

        当前实现返回市场示例代码中匹配的结果。
        后续可对接完整的品种数据库。

        Args:
            market: 市场类型
            keyword: 搜索关键词

        Returns:
            匹配的品种列表
        """
        keyword = (keyword or "").strip().lower()
        if not keyword:
            return []

        cache_key = f"search:{market}:{keyword}"

        def compute():
            module = get_market_module(
                DataSourceFactory.normalize_market(market)
            )
            if not module:
                return []
            results = []
            for example in module.symbol_examples:
                if keyword in example.lower():
                    results.append({
                        "symbol": example,
                        "market": module.market,
                        "display_name": module.display_name,
                    })
            return results

        try:
            result = cached_or_compute(
                cache_key, compute, ttl=CACHE_TTL.get("search", 3600),
            )
            return result or []
        except Exception as e:
            logger.error(f"品种搜索异常 {market}:{keyword}: {e}")
            return []

    # ------------------------------------------------------------------
    # 市场概览
    # ------------------------------------------------------------------

    @staticmethod
    def get_market_overview() -> List[Dict[str, Any]]:
        """返回所有市场的基本信息。

        Returns:
            市场列表（含名称、交易所、交易时间等）
        """
        return list_markets()


# 全局单例
market_data_service = MarketDataService()
