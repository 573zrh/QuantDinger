"""
QuantX 市场注册表

定义 QuantX 支持的四个市场模块的基本信息：
- CNStock（A 股）
- HKStock（港股）
- USStock（美股）
- CNFutures（中国期货）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class MarketModule:
    """市场模块描述"""

    market: str              # 标准名称：CNStock, HKStock, USStock, CNFutures
    display_name: str        # 显示名称：A 股, 港股, 美股, 中国期货
    exchanges: List[str]     # 交易所列表
    trading_hours: str       # 交易时间描述
    timezone: str            # 时区（IANA 格式）
    default_currency: str    # 默认货币
    symbol_examples: List[str]  # 示例代码
    description: str = ""    # 市场描述


# ------------------------------------------------------------------
# 四市场定义
# ------------------------------------------------------------------

MARKET_MODULES: Dict[str, MarketModule] = {
    "CNStock": MarketModule(
        market="CNStock",
        display_name="A 股",
        exchanges=["上海证券交易所 (SSE)", "深圳证券交易所 (SZSE)"],
        trading_hours="周一至周五 09:30-11:30, 13:00-15:00 (CST)",
        timezone="Asia/Shanghai",
        default_currency="CNY",
        symbol_examples=["600519", "000001", "300750"],
        description="中国大陆 A 股市场，覆盖沪深两市主板、中小板、创业板和科创板",
    ),
    "HKStock": MarketModule(
        market="HKStock",
        display_name="港股",
        exchanges=["香港交易所 (HKEX)"],
        trading_hours="周一至周五 09:30-12:00, 13:00-16:00 (HKT)",
        timezone="Asia/Hong_Kong",
        default_currency="HKD",
        symbol_examples=["0700", "9988", "1810"],
        description="香港股票市场，包括主板和创业板上市公司",
    ),
    "USStock": MarketModule(
        market="USStock",
        display_name="美股",
        exchanges=["纽约证券交易所 (NYSE)", "纳斯达克 (NASDAQ)"],
        trading_hours="周一至周五 09:30-16:00 (ET)",
        timezone="America/New_York",
        default_currency="USD",
        symbol_examples=["AAPL", "MSFT", "TSLA", "NVDA"],
        description="美国股票市场，覆盖纽交所和纳斯达克上市证券",
    ),
    "CNFutures": MarketModule(
        market="CNFutures",
        display_name="中国期货",
        exchanges=[
            "中国金融期货交易所 (CFFEX)",
            "上海期货交易所 (SHFE)",
            "大连商品交易所 (DCE)",
            "郑州商品交易所 (CZCE)",
            "广州期货交易所 (GFEX)",
        ],
        trading_hours="日盘 09:00-15:00, 夜盘 21:00-次日02:30 (CST)",
        timezone="Asia/Shanghai",
        default_currency="CNY",
        symbol_examples=["IF2401", "rb2401", "IF0", "au2406"],
        description="中国期货市场，覆盖股指期货、商品期货等主要品种",
    ),
}

# 市场显示顺序
MARKET_ORDER = ["CNStock", "HKStock", "USStock", "CNFutures"]


def list_markets() -> List[Dict]:
    """返回所有市场模块的序列化信息"""
    result = []
    for key in MARKET_ORDER:
        m = MARKET_MODULES.get(key)
        if m:
            result.append({
                "market": m.market,
                "display_name": m.display_name,
                "exchanges": m.exchanges,
                "trading_hours": m.trading_hours,
                "timezone": m.timezone,
                "default_currency": m.default_currency,
                "symbol_examples": m.symbol_examples,
                "description": m.description,
            })
    return result


def get_market_module(market: str) -> MarketModule | None:
    """获取指定市场的模块定义"""
    return MARKET_MODULES.get(market)
