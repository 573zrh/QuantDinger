"""
QuantX 交易数据模型

定义持仓、交易记录和订单的 dataclass。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class Position:
    """持仓"""
    id: Optional[int] = None
    strategy_id: Optional[int] = None
    symbol: str = ""                # 品种代码
    market: str = ""                # 市场标识
    side: str = ""                  # 方向: long, short
    quantity: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    status: str = "open"           # 状态: open, closed
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


@dataclass
class Trade:
    """交易记录"""
    id: Optional[int] = None
    strategy_id: Optional[int] = None
    position_id: Optional[int] = None
    symbol: str = ""                # 品种代码
    market: str = ""                # 市场标识
    side: str = ""                  # 方向: buy, sell
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")
    signal_type: str = ""          # 信号类型
    executed_at: Optional[datetime] = None


@dataclass
class Order:
    """订单（内存中的订单对象，非持久化）"""
    id: Optional[int] = None
    strategy_id: Optional[int] = None
    symbol: str = ""
    market: str = ""
    side: str = ""                  # buy, sell
    order_type: str = "market"     # market, limit
    quantity: Decimal = Decimal("0")
    price: Optional[Decimal] = None
    status: str = "pending"        # pending, submitted, filled, cancelled, failed
    exchange_order_id: str = ""
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    error_msg: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
