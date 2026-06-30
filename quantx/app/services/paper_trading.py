"""
QuantX 模拟交易引擎

提供虚拟撮合的模拟交易功能，支持四个市场（A股、港股、美股、期货）。
核心功能：
- 模拟订单提交与撮合（市价/限价）
- 持仓管理（实时更新）
- 盈亏计算（已实现/未实现）
- 交易规则集成（T+1、涨跌停、费用等）
- 交易记录持久化到数据库
"""
from __future__ import annotations

import uuid
import threading
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from app.markets.rules import get_trading_rules, TradingRules, CNFuturesRules
from app.markets.trading_calendar import trading_calendar
from app.utils.db import get_connection, fetch_all
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _gen_order_id() -> str:
    """生成唯一订单 ID"""
    return f"PAPER-{uuid.uuid4().hex[:12].upper()}"


def _to_float(val) -> float:
    """安全转为 float"""
    if isinstance(val, Decimal):
        return float(val)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


class PaperTradingEngine:
    """模拟交易引擎（虚拟撮合）

    每个策略对应一个 PaperTradingEngine 实例，维护独立的资金、持仓和交易记录。

    用法::

        engine = PaperTradingEngine(strategy_id=1, market="CNStock", initial_capital=100000)
        result = engine.submit_order("600519", "buy", 100)
        positions = engine.get_positions()
        pnl = engine.get_pnl()
    """

    def __init__(
        self,
        strategy_id: int,
        market: str,
        initial_capital: float = 100000.0,
    ):
        """
        初始化模拟交易引擎

        Args:
            strategy_id: 策略 ID
            market: 市场类型（CNStock/HKStock/USStock/CNFutures）
            initial_capital: 初始资金
        """
        self.strategy_id = strategy_id
        self.market = market
        self.capital = float(initial_capital)
        self.initial_capital = float(initial_capital)

        # symbol -> 持仓字典
        # 持仓结构: {symbol, side, quantity, avg_price, opened_date, fee_paid, unrealized_pnl}
        self.positions: Dict[str, Dict[str, Any]] = {}

        # 交易记录列表
        self.trades: List[Dict[str, Any]] = []

        # 订单记录列表
        self.orders: List[Dict[str, Any]] = []

        # 获取交易规则
        self.rules: TradingRules = get_trading_rules(market)

        # 最新价格缓存（用于市价单和未实现盈亏计算）
        self._latest_prices: Dict[str, float] = {}

        # 线程锁
        self._lock = threading.Lock()

        logger.info(
            f"模拟交易引擎创建: strategy={strategy_id}, market={market}, "
            f"capital={initial_capital:.2f}"
        )

    # ------------------------------------------------------------------
    # 价格更新
    # ------------------------------------------------------------------

    def update_price(self, symbol: str, price: float) -> None:
        """更新品种最新价格（用于市价单成交和盈亏计算）

        Args:
            symbol: 品种代码
            price: 最新价格
        """
        with self._lock:
            self._latest_prices[symbol] = price

    def get_latest_price(self, symbol: str) -> float:
        """获取缓存的最新价格"""
        return self._latest_prices.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # 订单提交
    # ------------------------------------------------------------------

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        提交模拟订单

        Args:
            symbol: 品种代码
            side: 方向，"buy" 或 "sell"
            quantity: 数量
            price: 指定价格，None 表示市价（使用最新价）

        Returns:
            {
                "order_id": str,
                "status": "filled" | "rejected",
                "reason": str,
                "fill_price": float,
                "fee": float,
                "pnl": float,
            }
        """
        with self._lock:
            order_id = _gen_order_id()
            side = (side or "").strip().lower()
            symbol = (symbol or "").strip()

            # 基础验证
            if not symbol:
                return self._reject(order_id, symbol, side, quantity, "品种代码不能为空")
            if side not in ("buy", "sell"):
                return self._reject(order_id, symbol, side, quantity, f"无效的订单方向: {side}")
            if quantity <= 0:
                return self._reject(order_id, symbol, side, quantity, "数量必须大于 0")

            # 确定成交价格
            exec_price = price if price and price > 0 else self._latest_prices.get(symbol, 0.0)
            if exec_price <= 0:
                return self._reject(
                    order_id, symbol, side, quantity,
                    f"无法确定成交价格（无最新报价且未指定限价）"
                )

            # 交易日检查
            today = datetime.now(tz=timezone.utc).date()
            if not trading_calendar.is_trading_day(self.market, today):
                return self._reject(
                    order_id, symbol, side, quantity,
                    f"今日非 {self.market} 交易日，无法下单"
                )

            # 执行买卖
            if side == "buy":
                return self._execute_buy(order_id, symbol, quantity, exec_price)
            else:
                return self._execute_sell(order_id, symbol, quantity, exec_price)

    def _execute_buy(
        self, order_id: str, symbol: str, quantity: float, price: float
    ) -> Dict[str, Any]:
        """执行买入"""
        # 计算费用
        if self.market == "CNFutures" and isinstance(self.rules, CNFuturesRules):
            fee_info = self.rules.calculate_fee_for_symbol(symbol, "buy", quantity, price)
            multiplier = self.rules._get_contract_multiplier(symbol)
            margin = self.rules.get_margin_required(symbol, quantity, price)
            cost = margin + fee_info["total_fee"]
        else:
            fee_info = self.rules.calculate_fee("buy", quantity, price)
            cost = quantity * price + fee_info["total_fee"]

        # 检查资金
        if cost > self.capital:
            return self._reject(
                order_id, symbol, "buy", quantity,
                f"资金不足: 需要 {cost:.2f}，可用 {self.capital:.2f}"
            )

        fee = fee_info["total_fee"]

        # 扣除资金
        self.capital -= cost

        # 更新持仓（如果已有持仓，加仓取均价）
        today = datetime.now(tz=timezone.utc).date()
        if symbol in self.positions:
            pos = self.positions[symbol]
            old_total = pos["quantity"] * pos["avg_price"]
            new_total = quantity * price
            pos["quantity"] += quantity
            if pos["quantity"] > 0:
                pos["avg_price"] = (old_total + new_total) / pos["quantity"]
            pos["fee_paid"] = pos.get("fee_paid", 0) + fee
        else:
            self.positions[symbol] = {
                "symbol": symbol,
                "side": "long",
                "quantity": quantity,
                "avg_price": price,
                "opened_date": today,
                "fee_paid": fee,
                "unrealized_pnl": 0.0,
            }

        # 记录订单
        order = self._make_order(order_id, symbol, "buy", quantity, price, "filled", fee=fee)
        self.orders.append(order)

        # 记录交易
        self._record_trade(symbol, "buy", quantity, price, fee, pnl=0.0)

        # 持久化到数据库
        self._persist_trade(symbol, "buy", quantity, price, fee, pnl=0.0)

        logger.info(
            f"[模拟] 买入成交: strategy={self.strategy_id}, {symbol} x{quantity} "
            f"@{price:.4f}, 手续费={fee:.2f}, 剩余资金={self.capital:.2f}"
        )

        return {
            "order_id": order_id,
            "status": "filled",
            "reason": "",
            "fill_price": price,
            "fee": fee,
            "pnl": 0.0,
        }

    def _execute_sell(
        self, order_id: str, symbol: str, quantity: float, price: float
    ) -> Dict[str, Any]:
        """执行卖出"""
        # 检查持仓
        pos = self.positions.get(symbol)
        if not pos or pos["quantity"] <= 0:
            return self._reject(
                order_id, symbol, "sell", quantity,
                f"无 {symbol} 持仓，无法卖出"
            )

        if quantity > pos["quantity"]:
            return self._reject(
                order_id, symbol, "sell", quantity,
                f"卖出数量 {quantity} 超过持仓 {pos['quantity']}"
            )

        # T+1 检查（A股）
        today = datetime.now(tz=timezone.utc).date()
        bar_stub = {"time": int(datetime.now(tz=timezone.utc).timestamp())}
        if not self.rules.can_sell(pos, bar_stub, today):
            return self._reject(
                order_id, symbol, "sell", quantity,
                f"T+1 限制: {symbol} 买入当日不可卖出"
            )

        # 计算费用
        if self.market == "CNFutures" and isinstance(self.rules, CNFuturesRules):
            fee_info = self.rules.calculate_fee_for_symbol(symbol, "sell", quantity, price)
            multiplier = self.rules._get_contract_multiplier(symbol)
            # 期货盈亏
            pnl = quantity * (price - pos["avg_price"]) * multiplier - fee_info["total_fee"]
            # 归还保证金
            margin = self.rules.get_margin_required(symbol, quantity, pos["avg_price"])
            self.capital += margin + pnl
        else:
            fee_info = self.rules.calculate_fee("sell", quantity, price)
            fee = fee_info["total_fee"]
            proceeds = quantity * price
            pnl = proceeds - quantity * pos["avg_price"] - fee
            self.capital += proceeds - fee

        fee = fee_info["total_fee"]

        # 更新持仓
        pos["quantity"] -= quantity
        if pos["quantity"] <= 0:
            del self.positions[symbol]

        # 记录订单
        order = self._make_order(order_id, symbol, "sell", quantity, price, "filled", fee=fee, pnl=pnl)
        self.orders.append(order)

        # 记录交易
        self._record_trade(symbol, "sell", quantity, price, fee, pnl=pnl)

        # 持久化到数据库
        self._persist_trade(symbol, "sell", quantity, price, fee, pnl=pnl)

        logger.info(
            f"[模拟] 卖出成交: strategy={self.strategy_id}, {symbol} x{quantity} "
            f"@{price:.4f}, 手续费={fee:.2f}, 盈亏={pnl:.2f}, 剩余资金={self.capital:.2f}"
        )

        return {
            "order_id": order_id,
            "status": "filled",
            "reason": "",
            "fill_price": price,
            "fee": fee,
            "pnl": round(pnl, 2),
        }

    def _reject(
        self, order_id: str, symbol: str, side: str, quantity: float, reason: str
    ) -> Dict[str, Any]:
        """生成拒绝结果并记录订单"""
        logger.warning(
            f"[模拟] 订单拒绝: strategy={self.strategy_id}, {side} {symbol} x{quantity}, "
            f"原因: {reason}"
        )
        order = self._make_order(order_id, symbol, side, quantity, 0, "rejected", error_msg=reason)
        self.orders.append(order)
        return {
            "order_id": order_id,
            "status": "rejected",
            "reason": reason,
            "fill_price": 0.0,
            "fee": 0.0,
            "pnl": 0.0,
        }

    # ------------------------------------------------------------------
    # 持仓与盈亏
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Dict[str, Any]]:
        """获取当前持仓列表

        Returns:
            持仓列表，每项包含 symbol, side, quantity, avg_price, unrealized_pnl, market_value
        """
        with self._lock:
            result = []
            for symbol, pos in self.positions.items():
                latest_price = self._latest_prices.get(symbol, pos["avg_price"])
                # 计算未实现盈亏
                if self.market == "CNFutures" and isinstance(self.rules, CNFuturesRules):
                    multiplier = self.rules._get_contract_multiplier(symbol)
                    unrealized = pos["quantity"] * (latest_price - pos["avg_price"]) * multiplier
                    market_value = self.rules.get_margin_required(
                        symbol, pos["quantity"], latest_price
                    )
                else:
                    unrealized = pos["quantity"] * (latest_price - pos["avg_price"])
                    market_value = pos["quantity"] * latest_price

                result.append({
                    "symbol": symbol,
                    "side": pos["side"],
                    "quantity": pos["quantity"],
                    "avg_price": round(pos["avg_price"], 4),
                    "latest_price": round(latest_price, 4),
                    "market_value": round(market_value, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "fee_paid": round(pos.get("fee_paid", 0), 2),
                })
            return result

    def get_portfolio_value(self) -> float:
        """获取组合总值（现金 + 持仓市值）

        Returns:
            组合总值
        """
        with self._lock:
            total = self.capital
            for symbol, pos in self.positions.items():
                latest_price = self._latest_prices.get(symbol, pos["avg_price"])
                if self.market == "CNFutures" and isinstance(self.rules, CNFuturesRules):
                    # 期货：保证金 + 未实现盈亏
                    multiplier = self.rules._get_contract_multiplier(symbol)
                    margin = self.rules.get_margin_required(
                        symbol, pos["quantity"], pos["avg_price"]
                    )
                    unrealized = pos["quantity"] * (latest_price - pos["avg_price"]) * multiplier
                    total += margin + unrealized
                else:
                    total += pos["quantity"] * latest_price
            return round(total, 2)

    def get_pnl(self) -> Dict[str, Any]:
        """获取盈亏信息

        Returns:
            {
                "total_value": float,       # 组合总值
                "capital": float,           # 可用现金
                "market_value": float,      # 持仓市值
                "total_pnl": float,         # 总盈亏
                "total_pnl_pct": float,     # 总盈亏百分比
                "realized_pnl": float,      # 已实现盈亏
                "unrealized_pnl": float,    # 未实现盈亏
                "total_fees": float,        # 总手续费
            }
        """
        with self._lock:
            portfolio_value = self.capital
            market_value = 0.0
            unrealized_pnl = 0.0

            for symbol, pos in self.positions.items():
                latest_price = self._latest_prices.get(symbol, pos["avg_price"])
                if self.market == "CNFutures" and isinstance(self.rules, CNFuturesRules):
                    multiplier = self.rules._get_contract_multiplier(symbol)
                    margin = self.rules.get_margin_required(
                        symbol, pos["quantity"], pos["avg_price"]
                    )
                    unrealized = pos["quantity"] * (latest_price - pos["avg_price"]) * multiplier
                    market_value += margin + unrealized
                    portfolio_value += margin + unrealized
                else:
                    mv = pos["quantity"] * latest_price
                    market_value += mv
                    portfolio_value += mv
                    unrealized += pos["quantity"] * (latest_price - pos["avg_price"])

            # 已实现盈亏 = 所有卖出交易的 pnl 总和
            realized_pnl = sum(t.get("pnl", 0.0) for t in self.trades if t["side"] == "sell")
            total_fees = sum(t.get("fee", 0.0) for t in self.trades)
            total_pnl = portfolio_value - self.initial_capital
            total_pnl_pct = (total_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0

            return {
                "total_value": round(portfolio_value, 2),
                "capital": round(self.capital, 2),
                "market_value": round(market_value, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl_pct, 2),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_fees": round(total_fees, 2),
                "initial_capital": self.initial_capital,
            }

    def get_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取订单历史（最近 N 条）

        Args:
            limit: 返回数量上限

        Returns:
            订单列表（按时间倒序）
        """
        with self._lock:
            return list(reversed(self.orders[-limit:]))

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _make_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        status: str,
        fee: float = 0.0,
        pnl: float = 0.0,
        error_msg: str = "",
    ) -> Dict[str, Any]:
        """构造订单字典"""
        now = datetime.now(tz=timezone.utc)
        return {
            "order_id": order_id,
            "strategy_id": self.strategy_id,
            "symbol": symbol,
            "market": self.market,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": status,
            "adapter_type": "paper",
            "fee": fee,
            "pnl": pnl,
            "error_msg": error_msg,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    def _record_trade(
        self, symbol: str, side: str, quantity: float, price: float, fee: float, pnl: float
    ) -> None:
        """记录内存中的交易"""
        self.trades.append({
            "strategy_id": self.strategy_id,
            "symbol": symbol,
            "market": self.market,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "pnl": pnl,
            "executed_at": datetime.now(tz=timezone.utc).isoformat(),
        })

    def _persist_trade(
        self, symbol: str, side: str, quantity: float, price: float, fee: float, pnl: float
    ) -> None:
        """将交易记录持久化到数据库（异步，不阻塞主逻辑）"""
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO qd_trades
                        (strategy_id, symbol, market, side, quantity, price, fee, pnl, executed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (self.strategy_id, symbol, self.market, side, quantity, price, fee, pnl),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"交易记录持久化失败（不影响模拟交易）: {e}")
