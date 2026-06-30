"""
QuantX 数据源异常定义

集中定义数据源层可能抛出的异常类型，便于上层统一捕获和处理。
"""
from __future__ import annotations


class DataSourceError(Exception):
    """数据源相关错误的基类"""


class UnsupportedMarketError(DataSourceError):
    """请求的市场类型不受支持"""

    def __init__(self, market: str):
        self.market = str(market or "")
        super().__init__(f"不支持的市场类型: {self.market}")


class DataFetchError(DataSourceError):
    """数据获取失败（网络错误、API 异常等）"""

    def __init__(self, source: str, symbol: str, detail: str = ""):
        self.source = source
        self.symbol = symbol
        self.detail = detail
        msg = f"数据获取失败 [{source}] {symbol}"
        if detail:
            msg += f" — {detail}"
        super().__init__(msg)


class RateLimitExceeded(DataSourceError):
    """请求频率超限"""

    def __init__(self, source: str, retry_after: float = 0):
        self.source = source
        self.retry_after = retry_after
        super().__init__(f"请求频率超限 [{source}]，建议 {retry_after:.1f}s 后重试")
