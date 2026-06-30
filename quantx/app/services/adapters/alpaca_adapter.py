"""
QuantX Alpaca 美股交易适配器

使用 alpaca-py SDK 与 Alpaca Markets 交互，支持：
- Paper Trading（模拟交易，默认）
- Live Trading（实盘交易）

参考 QuantDinger alpaca_trading/client.py 精简实现。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _num(value: Any, default: float = 0.0) -> float:
    """安全转浮点数"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _as_str_id(value: Union[str, UUID, None]) -> str:
    """将 UUID 或字符串 ID 转为字符串"""
    if value is None:
        return ""
    return str(value)


def _enum_value(value: Any) -> str:
    """安全获取枚举值"""
    if value is None:
        return ""
    return str(value.value) if hasattr(value, "value") else str(value)


# ------------------------------------------------------------------
# 懒加载 alpaca-py SDK（未安装时优雅降级）
# ------------------------------------------------------------------

_alpaca_modules = None


def _ensure_alpaca():
    """确保 alpaca-py SDK 已导入"""
    global _alpaca_modules
    if _alpaca_modules is not None:
        return _alpaca_modules

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import (
            MarketOrderRequest,
            LimitOrderRequest,
            GetOrdersRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce
        _alpaca_modules = {
            "TradingClient": TradingClient,
            "MarketOrderRequest": MarketOrderRequest,
            "LimitOrderRequest": LimitOrderRequest,
            "GetOrdersRequest": GetOrdersRequest,
            "OrderSide": OrderSide,
            "TimeInForce": TimeInForce,
        }
        return _alpaca_modules
    except ImportError:
        raise ImportError(
            "alpaca-py 未安装。请运行: pip install alpaca-py"
        )


class AlpacaAdapter:
    """Alpaca 美股交易适配器

    封装 alpaca-py SDK，提供与 PaperAdapter / CTPAdapter 统一的接口。

    用法::

        # Paper Trading（默认）
        adapter = AlpacaAdapter(paper=True)
        result = adapter.submit_order("AAPL", "buy", 10)

        # Live Trading
        adapter = AlpacaAdapter(paper=False)
    """

    adapter_type = "alpaca"

    def __init__(self, paper: bool = True):
        """
        初始化 Alpaca 适配器

        Args:
            paper: True 使用 Paper Trading API，False 使用 Live Trading API
        """
        self.paper = paper
        self._trading_client = None

        # 从环境变量读取 API 密钥
        self._api_key = os.getenv("ALPACA_API_KEY", "").strip()
        self._secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
        self._base_url = os.getenv("ALPACA_BASE_URL", "").strip() or None

        if not self._api_key or not self._secret_key:
            logger.warning(
                "Alpaca 密钥未配置（ALPACA_API_KEY / ALPACA_SECRET_KEY），"
                "请设置环境变量后使用。"
            )

    def _ensure_connected(self) -> None:
        """确保已连接到 Alpaca"""
        if self._trading_client is not None:
            return

        modules = _ensure_alpaca()

        if not self._api_key or not self._secret_key:
            raise RuntimeError(
                "Alpaca 密钥未配置，请设置 ALPACA_API_KEY 和 ALPACA_SECRET_KEY 环境变量"
            )

        self._trading_client = modules["TradingClient"](
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=self.paper,
            url_override=self._base_url,
        )

        # 验证连接
        account = self._trading_client.get_account()
        logger.info(
            f"Alpaca 连接成功: paper={self.paper}, "
            f"account_id={_as_str_id(getattr(account, 'id', ''))}"
        )

    # ------------------------------------------------------------------
    # 账户与持仓
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """获取 Alpaca 账户信息

        Returns:
            {
                "account_id": str,
                "cash": float,
                "portfolio_value": float,
                "buying_power": float,
                "status": str,
                "paper": bool,
            }
        """
        try:
            self._ensure_connected()
            account = self._trading_client.get_account()
            return {
                "account_id": _as_str_id(getattr(account, "id", "")),
                "cash": _num(getattr(account, "cash", 0)),
                "portfolio_value": _num(getattr(account, "portfolio_value", 0)),
                "buying_power": _num(getattr(account, "buying_power", 0)),
                "status": _enum_value(getattr(account, "status", "")),
                "paper": self.paper,
                "adapter_type": self.adapter_type,
            }
        except Exception as e:
            logger.error(f"Alpaca 获取账户信息失败: {e}")
            return {
                "account_id": "",
                "cash": 0,
                "portfolio_value": 0,
                "buying_power": 0,
                "status": "error",
                "paper": self.paper,
                "adapter_type": self.adapter_type,
                "error": str(e),
            }

    def get_positions(self) -> List[Dict[str, Any]]:
        """获取 Alpaca 持仓列表

        Returns:
            [{"symbol", "side", "quantity", "avg_price", "market_value", "unrealized_pnl"}, ...]
        """
        try:
            self._ensure_connected()
            positions = self._trading_client.get_all_positions()
            result = []
            for pos in positions:
                result.append({
                    "symbol": getattr(pos, "symbol", ""),
                    "side": _enum_value(getattr(pos, "side", "")),
                    "quantity": _num(getattr(pos, "qty", 0)),
                    "avg_price": _num(getattr(pos, "avg_entry_price", 0)),
                    "market_value": _num(getattr(pos, "market_value", 0)),
                    "unrealized_pnl": _num(getattr(pos, "unrealized_pl", 0)),
                    "unrealized_pnl_pct": _num(getattr(pos, "unrealized_plpc", 0)),
                })
            return result
        except Exception as e:
            logger.error(f"Alpaca 获取持仓失败: {e}")
            return []

    def get_portfolio_value(self) -> float:
        """获取组合总值"""
        account = self.get_account()
        return account.get("portfolio_value", 0.0)

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        提交订单到 Alpaca

        Args:
            symbol: 股票代码（如 "AAPL"）
            side: "buy" 或 "sell"
            quantity: 数量
            order_type: "market" 或 "limit"
            price: 限价单的价格

        Returns:
            {"order_id": str, "status": str, "reason": str, ...}
        """
        try:
            self._ensure_connected()
            modules = _ensure_alpaca()

            side_enum = (
                modules["OrderSide"].BUY if side.lower() == "buy"
                else modules["OrderSide"].SELL
            )

            # 构造订单请求
            if order_type == "limit" and price and price > 0:
                order_request = modules["LimitOrderRequest"](
                    symbol=symbol.upper(),
                    qty=quantity,
                    side=side_enum,
                    time_in_force=modules["TimeInForce"].DAY,
                    limit_price=price,
                )
            else:
                order_request = modules["MarketOrderRequest"](
                    symbol=symbol.upper(),
                    qty=quantity,
                    side=side_enum,
                    time_in_force=modules["TimeInForce"].DAY,
                )

            # 提交订单
            order = self._trading_client.submit_order(order_data=order_request)
            order_id = _as_str_id(getattr(order, "id", ""))

            logger.info(
                f"Alpaca 订单提交: {side} {symbol} x{quantity}, "
                f"type={order_type}, order_id={order_id}"
            )

            return {
                "order_id": order_id,
                "status": _enum_value(getattr(order, "status", "new")),
                "reason": "",
                "fill_price": _num(getattr(order, "filled_avg_price", 0)),
                "fee": 0.0,
                "pnl": 0.0,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
            }

        except Exception as e:
            logger.error(f"Alpaca 订单提交失败: {e}")
            return {
                "order_id": "",
                "status": "rejected",
                "reason": str(e),
                "fill_price": 0.0,
                "fee": 0.0,
                "pnl": 0.0,
            }

    def cancel_order(self, order_id: str) -> bool:
        """取消订单

        Args:
            order_id: Alpaca 订单 ID

        Returns:
            是否取消成功
        """
        try:
            self._ensure_connected()
            self._trading_client.cancel_order_by_id(order_id)
            logger.info(f"Alpaca 订单取消成功: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Alpaca 取消订单失败 ({order_id}): {e}")
            return False

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """获取订单详情

        Args:
            order_id: Alpaca 订单 ID

        Returns:
            订单详情字典
        """
        try:
            self._ensure_connected()
            order = self._trading_client.get_order_by_id(order_id)
            return {
                "order_id": _as_str_id(getattr(order, "id", "")),
                "symbol": getattr(order, "symbol", ""),
                "side": _enum_value(getattr(order, "side", "")),
                "quantity": _num(getattr(order, "qty", 0)),
                "filled_quantity": _num(getattr(order, "filled_qty", 0)),
                "status": _enum_value(getattr(order, "status", "")),
                "avg_fill_price": _num(getattr(order, "filled_avg_price", 0)),
                "order_type": _enum_value(getattr(order, "order_type", "")),
            }
        except Exception as e:
            logger.error(f"Alpaca 获取订单详情失败 ({order_id}): {e}")
            return {"order_id": order_id, "status": "error", "error": str(e)}

    def get_orders(
        self, status: str = "all", limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取订单历史

        Args:
            status: 订单状态过滤（all, open, closed）
            limit: 返回数量上限

        Returns:
            订单列表
        """
        try:
            self._ensure_connected()
            modules = _ensure_alpaca()

            request = modules["GetOrdersRequest"](limit=limit)
            orders = self._trading_client.get_orders(filter=request)

            result = []
            for order in orders:
                result.append({
                    "order_id": _as_str_id(getattr(order, "id", "")),
                    "symbol": getattr(order, "symbol", ""),
                    "side": _enum_value(getattr(order, "side", "")),
                    "quantity": _num(getattr(order, "qty", 0)),
                    "filled_quantity": _num(getattr(order, "filled_qty", 0)),
                    "status": _enum_value(getattr(order, "status", "")),
                    "avg_fill_price": _num(getattr(order, "filled_avg_price", 0)),
                    "created_at": str(getattr(order, "created_at", "")),
                })
            return result

        except Exception as e:
            logger.error(f"Alpaca 获取订单列表失败: {e}")
            return []
