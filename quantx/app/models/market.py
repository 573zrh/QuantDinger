"""
QuantX 市场 / K线数据模型

定义 K线、行情数据和市场品种信息的 dataclass。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class KlineBar:
    """单根K线数据"""
    timestamp: datetime          # K线时间戳
    open: float = 0.0           # 开盘价
    high: float = 0.0           # 最高价
    low: float = 0.0            # 最低价
    close: float = 0.0          # 收盘价
    volume: float = 0.0         # 成交量
    turnover: float = 0.0       # 成交额
    symbol: str = ""            # 品种代码
    market: str = ""            # 市场标识
    timeframe: str = "1D"       # 时间周期


@dataclass
class TickerData:
    """实时行情快照"""
    symbol: str = ""            # 品种代码
    market: str = ""            # 市场标识
    last_price: float = 0.0    # 最新价
    bid: float = 0.0            # 买一价
    ask: float = 0.0            # 卖一价
    volume: float = 0.0         # 当日成交量
    turnover: float = 0.0       # 当日成交额
    change_pct: float = 0.0     # 涨跌幅 (%)
    high: float = 0.0           # 当日最高
    low: float = 0.0            # 当日最低
    open: float = 0.0           # 开盘价
    prev_close: float = 0.0    # 昨收价
    timestamp: Optional[datetime] = None


@dataclass
class MarketSymbol:
    """市场品种信息"""
    id: Optional[int] = None
    symbol: str = ""            # 品种代码
    market: str = ""            # 市场标识 (CNStock, HKStock, USStock, CNFutures)
    name: str = ""              # 品种名称
    exchange: str = ""          # 交易所
    is_active: bool = True      # 是否活跃
    updated_at: Optional[datetime] = None
