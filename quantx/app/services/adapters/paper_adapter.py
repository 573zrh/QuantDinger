"""
QuantX 统一模拟交易适配器

为 A 股和港股提供模拟交易能力，封装 PaperTradingEngine。
所有模拟交易通过内存撮合完成，不连接真实交易所。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.paper_trading import PaperTradingEngine
from app.utils.logger import get_logger

logger = get_logger(__name__)


class PaperAdapter:
    """统一模拟交易适配器（A 股 / 港股用）

    封装 PaperTradingEngine，提供与 AlpacaAdapter / CTPAdapter 相同的接口。
    MVP 阶段 A 股和港股仅支持模拟交易。

    用法::

        adapter = PaperAdapter(strategy_id=1, market="CNStock", initial_capital=100000)
        result = adapter.submit_order("600519", "buy", 100)
        positions = adapter.get_positions()
    """

    adapter_type = "paper"

    def __init__(
        self,
        strategy_id: int,
        market: str,
        initial_capital: float = 100000.0,
    ):
        """
        创建模拟交易适配器

        Args:
            strategy_id: 策略 ID
            market: 市场类型（CNStock / HKStock）
            initial_capital: 初始资金
        """
        self.strategy_id = strategy_id
        self.market = market
        self.engine = PaperTradingEngine(
            strategy_id=strategy_id,
            market=market,
            initial_capital=initial_capital,
        )
        logger.info(
            f"模拟适配器创建: strategy={strategy_id}, market={market}, "
            f"capital={initial_capital:.2f}"
        )

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "market",
    ) -> Dict[str, Any]:
        """
        提交模拟订单

        Args:
            symbol: 品种代码
            side: "buy" 或 "sell"
            quantity: 数量
            price: 限价（None 为市价）
            order_type: 订单类型（模拟交易中 market 和 limit 行为一致）

        Returns:
            {"order_id": str, "status": "filled"|"rejected", ...}
        """
        return self.engine.submit_order(symbol, side, quantity, price)

    def get_positions(self) -> List[Dict[str, Any]]:
        """获取模拟持仓列表"""
        return self.engine.get_positions()

    def get_portfolio_value(self) -> float:
        """获取组合总值（现金 + 持仓市值）"""
        return self.engine.get_portfolio_value()

    def get_pnl(self) -> Dict[str, Any]:
        """获取盈亏信息"""
        return self.engine.get_pnl()

    def get_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取订单历史"""
        return self.engine.get_orders(limit)

    def get_account(self) -> Dict[str, Any]:
        """获取账户信息（模拟）"""
        pnl = self.engine.get_pnl()
        return {
            "strategy_id": self.strategy_id,
            "market": self.market,
            "adapter_type": self.adapter_type,
            "cash": pnl["capital"],
            "portfolio_value": pnl["total_value"],
            "initial_capital": pnl["initial_capital"],
        }

    def update_price(self, symbol: str, price: float) -> None:
        """更新最新报价（供模拟撮合使用）"""
        self.engine.update_price(symbol, price)
