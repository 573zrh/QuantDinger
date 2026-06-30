"""
QuantX 交易适配器包

提供统一的交易适配器接口，根据市场类型自动选择适配器：
- CNStock / HKStock → PaperAdapter（MVP 仅模拟交易）
- USStock → AlpacaAdapter（支持 Paper 和 Live）
- CNFutures → CTPAdapter（SimNow 模拟）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


def create_adapter(
    market: str,
    strategy_id: int,
    paper: bool = True,
    initial_capital: float = 100000.0,
) -> Any:
    """根据市场类型创建对应的交易适配器

    Args:
        market: 市场类型（CNStock/HKStock/USStock/CNFutures）
        strategy_id: 策略 ID
        paper: 是否使用模拟交易（默认 True）
        initial_capital: 模拟交易初始资金

    Returns:
        适配器实例（统一接口：submit_order, get_positions, get_portfolio_value）

    Raises:
        ValueError: 不支持的市场类型
    """
    if market in ("CNStock", "HKStock"):
        # A 股和港股 MVP 仅支持模拟交易
        from app.services.adapters.paper_adapter import PaperAdapter
        return PaperAdapter(
            strategy_id=strategy_id,
            market=market,
            initial_capital=initial_capital,
        )
    elif market == "USStock":
        from app.services.adapters.alpaca_adapter import AlpacaAdapter
        return AlpacaAdapter(paper=paper)
    elif market == "CNFutures":
        from app.services.adapters.ctp_adapter import CTPAdapter
        adapter = CTPAdapter(paper=paper)
        return adapter
    else:
        raise ValueError(
            f"不支持的市场类型: {market}，"
            f"支持: CNStock, HKStock, USStock, CNFutures"
        )


__all__ = ["create_adapter"]
