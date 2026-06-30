"""
QuantX 数据源模块

提供四市场（A 股、港股、美股、中国期货）的数据源适配器，
以及熔断器、限流器等基础设施。
"""
from app.data_sources.errors import DataSourceError, UnsupportedMarketError, DataFetchError, RateLimitExceeded
from app.data_sources.base import BaseDataSource, TIMEFRAME_SECONDS
from app.data_sources.circuit_breaker import CircuitBreaker, get_kline_circuit_breaker, get_realtime_circuit_breaker
from app.data_sources.rate_limiter import RateLimiter, get_akshare_limiter, get_tencent_limiter
from app.data_sources.factory import DataSourceFactory
