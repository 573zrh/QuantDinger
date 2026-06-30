"""
QuantX 策略数据模型

定义交易策略的 dataclass，支持指标策略和脚本策略两种类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class Strategy:
    """交易策略"""
    id: Optional[int] = None
    user_id: Optional[int] = None
    name: str = ""                      # 策略名称
    description: str = ""               # 策略描述
    market: str = ""                    # 市场 (CNStock, HKStock, USStock, CNFutures)
    symbol: str = ""                    # 品种代码
    timeframe: str = "1D"              # K线周期
    status: str = "draft"              # 状态: draft, ready, running, stopped, error
    strategy_type: str = "indicator"   # 类型: indicator, script
    indicator_code: str = ""           # 指标代码
    params: Dict[str, Any] = field(default_factory=dict)  # 策略参数
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
