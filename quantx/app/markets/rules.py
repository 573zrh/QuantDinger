"""
QuantX 交易规则引擎

四个市场的交易规则实现：
- CNStockRules（A 股）：T+1、涨跌停 ±10%/±20%、印花税、100 股整手
- HKStockRules（港股）：T+0、无涨跌停、印花税 + 交易征费
- CNFuturesRules（中国期货）：T+0、保证金、涨跌停 ±4%/±6%、按手收费
- USStockRules（美股）：T+0、无涨跌停、佣金制
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, Optional

from app.markets.trading_calendar import TradingCalendar, trading_calendar
from app.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# 交易规则基类
# =============================================================================

class TradingRules(ABC):
    """交易规则抽象基类"""

    market: str = ""

    @abstractmethod
    def can_sell(
        self,
        position: Dict[str, Any],
        current_bar: Dict[str, Any],
        trade_date: date,
    ) -> bool:
        """检查是否可以卖出（T+1 等限制）

        Args:
            position: 当前持仓，需含 opened_date 字段
            current_bar: 当前 K 线数据
            trade_date: 当前交易日期

        Returns:
            是否允许卖出
        """
        ...

    @abstractmethod
    def check_price_limit(
        self,
        symbol: str,
        price: float,
        prev_close: float,
    ) -> Dict[str, Any]:
        """检查涨跌停限制

        Args:
            symbol: 品种代码
            price: 当前价格
            prev_close: 前收盘价

        Returns:
            {"is_limit_up": bool, "is_limit_down": bool,
             "upper_limit": float, "lower_limit": float}
        """
        ...

    @abstractmethod
    def calculate_fee(
        self,
        side: str,
        quantity: float,
        price: float,
    ) -> Dict[str, Any]:
        """计算交易费用

        Args:
            side: 方向（buy/sell）
            quantity: 数量
            price: 价格

        Returns:
            {"total_fee": float, "detail": {...}}
        """
        ...

    @abstractmethod
    def calculate_position_size(
        self,
        capital: float,
        price: float,
    ) -> float:
        """计算可买入数量（考虑最小交易单位和手数限制）

        Args:
            capital: 可用资金
            price: 当前价格

        Returns:
            可买入数量（已取整到最小交易单位）
        """
        ...


# =============================================================================
# A 股规则
# =============================================================================

class CNStockRules(TradingRules):
    """A 股交易规则

    - T+1：买入当日不可卖出，下一个交易日才可卖出
    - 涨跌停：主板 ±10%，创业板/科创板 ±20%
      - 300xxx = 创业板，688xxx = 科创板
    - 最小交易单位：100 股（1 手）
    - 费用：
      - 印花税 0.05%（仅卖出收取）
      - 佣金 0.025%（买卖双向，最低 5 元）
      - 过户费 0.001%
    """

    market = "CNStock"

    def can_sell(
        self,
        position: Dict[str, Any],
        current_bar: Dict[str, Any],
        trade_date: date,
    ) -> bool:
        """A 股 T+1 规则：买入当日不可卖出"""
        opened_date = position.get("opened_date")
        if opened_date is None:
            return True
        # 如果建仓日期早于当前交易日，可以卖出
        return opened_date < trade_date

    def check_price_limit(
        self,
        symbol: str,
        price: float,
        prev_close: float,
    ) -> Dict[str, Any]:
        """A 股涨跌停检查"""
        if prev_close <= 0:
            return {
                "is_limit_up": False,
                "is_limit_down": False,
                "upper_limit": float("inf"),
                "lower_limit": 0,
            }

        # 判断板块：创业板(300)、科创板(688)为 ±20%
        limit_pct = 0.20 if symbol.startswith("300") or symbol.startswith("688") else 0.10

        upper_limit = round(prev_close * (1 + limit_pct), 2)
        lower_limit = round(prev_close * (1 - limit_pct), 2)

        return {
            "is_limit_up": price >= upper_limit,
            "is_limit_down": price <= lower_limit,
            "upper_limit": upper_limit,
            "lower_limit": lower_limit,
        }

    def calculate_fee(
        self,
        side: str,
        quantity: float,
        price: float,
    ) -> Dict[str, Any]:
        """A 股费用计算"""
        amount = quantity * price

        # 佣金 0.025%，最低 5 元
        commission = max(amount * 0.00025, 5.0)

        # 过户费 0.001%
        transfer_fee = amount * 0.00001

        # 印花税 0.05%（仅卖出）
        stamp_tax = amount * 0.0005 if side == "sell" else 0.0

        total = commission + transfer_fee + stamp_tax

        return {
            "total_fee": round(total, 2),
            "detail": {
                "commission": round(commission, 2),
                "transfer_fee": round(transfer_fee, 2),
                "stamp_tax": round(stamp_tax, 2),
            },
        }

    def calculate_position_size(
        self,
        capital: float,
        price: float,
    ) -> float:
        """A 股可买入数量：向下取整到 100 股（1 手）"""
        if price <= 0:
            return 0
        raw_shares = capital / price
        # 向下取整到 100 的整数倍
        lots = int(raw_shares // 100)
        return float(lots * 100)


# =============================================================================
# 港股规则
# =============================================================================

class HKStockRules(TradingRules):
    """港股交易规则

    - T+0：当日可买卖
    - 无涨跌停限制
    - 最小交易单位：1 股（简化，实际港股每手股数不固定）
    - 费用：
      - 印花税 0.13%（双向，向上取整到 HKD 1）
      - 交易征费 0.0056%
      - 联交所费 0.0027%
      - 佣金 0.03%（最低 HKD 3）
    """

    market = "HKStock"

    def can_sell(
        self,
        position: Dict[str, Any],
        current_bar: Dict[str, Any],
        trade_date: date,
    ) -> bool:
        """港股 T+0：当日可买卖"""
        return True

    def check_price_limit(
        self,
        symbol: str,
        price: float,
        prev_close: float,
    ) -> Dict[str, Any]:
        """港股无涨跌停限制"""
        return {
            "is_limit_up": False,
            "is_limit_down": False,
            "upper_limit": float("inf"),
            "lower_limit": 0,
        }

    def calculate_fee(
        self,
        side: str,
        quantity: float,
        price: float,
    ) -> Dict[str, Any]:
        """港股费用计算"""
        amount = quantity * price

        # 印花税 0.13%，向上取整到 HKD 1
        stamp_tax_raw = amount * 0.0013
        stamp_tax = math.ceil(stamp_tax_raw) if stamp_tax_raw > 0 else 0.0

        # 交易征费 0.0056%
        levy = amount * 0.000056

        # 联交所费 0.0027%
        exchange_fee = amount * 0.000027

        # 佣金 0.03%，最低 HKD 3
        commission = max(amount * 0.0003, 3.0)

        total = stamp_tax + levy + exchange_fee + commission

        return {
            "total_fee": round(total, 2),
            "detail": {
                "stamp_tax": round(stamp_tax, 2),
                "levy": round(levy, 2),
                "exchange_fee": round(exchange_fee, 2),
                "commission": round(commission, 2),
            },
        }

    def calculate_position_size(
        self,
        capital: float,
        price: float,
    ) -> float:
        """港股可买入数量：向下取整到 1 股"""
        if price <= 0:
            return 0
        return float(int(capital / price))


# =============================================================================
# 中国期货规则
# =============================================================================

class CNFuturesRules(TradingRules):
    """中国期货交易规则

    - T+0：当日可买卖
    - 涨跌停：根据品种不同（简化为 ±4% 或 ±6%）
      - 股指期货（IF/IC/IH/IM）：±10%
      - 商品期货（默认）：±6%
    - 保证金制度：默认 10% 保证金
    - 手续费：
      - 股指期货（IF 等）：万分之 0.23（按金额）
      - 商品期货（rb 等）：万分之一（按金额）
    - 最小交易单位：1 手
    """

    market = "CNFutures"

    # 合约乘数（常见品种）
    CONTRACT_MULTIPLIERS: Dict[str, float] = {
        "IF": 300,    # 沪深300股指期货
        "IC": 200,    # 中证500股指期货
        "IH": 300,    # 上证50股指期货
        "IM": 200,    # 中证1000股指期货
        "rb": 10,     # 螺纹钢
        "au": 1000,   # 黄金
        "ag": 15,     # 白银
        "cu": 5,      # 铜
        "al": 5,      # 铝
        "i": 100,     # 铁矿石
        "m": 10,      # 豆粕
        "TA": 5,      # PTA
        "MA": 10,     # 甲醇
        "sc": 1000,   # 原油
    }

    # 保证金比例（简化：默认 10%）
    DEFAULT_MARGIN_RATE = 0.10

    def _get_contract_multiplier(self, symbol: str) -> float:
        """获取合约乘数"""
        # 提取品种前缀（去掉数字部分）
        prefix = ""
        for ch in symbol:
            if ch.isalpha():
                prefix += ch
            else:
                break
        return self.CONTRACT_MULTIPLIERS.get(prefix, 10.0)

    def _get_limit_pct(self, symbol: str) -> float:
        """获取涨跌停幅度"""
        prefix = ""
        for ch in symbol:
            if ch.isalpha():
                prefix += ch
            else:
                break
        # 股指期货 ±10%，其他默认 ±6%
        if prefix in ("IF", "IC", "IH", "IM"):
            return 0.10
        return 0.06

    def _get_fee_rate(self, symbol: str) -> float:
        """获取手续费率（按金额比例）"""
        prefix = ""
        for ch in symbol:
            if ch.isalpha():
                prefix += ch
            else:
                break
        # 股指期货：万分之 0.23
        if prefix in ("IF", "IC", "IH", "IM"):
            return 0.000023
        # 商品期货默认：万分之一
        return 0.0001

    def can_sell(
        self,
        position: Dict[str, Any],
        current_bar: Dict[str, Any],
        trade_date: date,
    ) -> bool:
        """期货 T+0：当日可买卖"""
        return True

    def check_price_limit(
        self,
        symbol: str,
        price: float,
        prev_close: float,
    ) -> Dict[str, Any]:
        """期货涨跌停检查"""
        if prev_close <= 0:
            return {
                "is_limit_up": False,
                "is_limit_down": False,
                "upper_limit": float("inf"),
                "lower_limit": 0,
            }

        limit_pct = self._get_limit_pct(symbol)
        upper_limit = round(prev_close * (1 + limit_pct), 2)
        lower_limit = round(prev_close * (1 - limit_pct), 2)

        return {
            "is_limit_up": price >= upper_limit,
            "is_limit_down": price <= lower_limit,
            "upper_limit": upper_limit,
            "lower_limit": lower_limit,
        }

    def calculate_fee(
        self,
        side: str,
        quantity: float,
        price: float,
    ) -> Dict[str, Any]:
        """期货手续费计算（按金额比例）"""
        amount = quantity * price
        fee_rate = self._get_fee_rate("")  # 此处简化，实际应按 symbol
        total = amount * fee_rate

        return {
            "total_fee": round(total, 2),
            "detail": {
                "commission": round(total, 2),
                "fee_rate": fee_rate,
            },
        }

    def calculate_fee_for_symbol(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
    ) -> Dict[str, Any]:
        """期货手续费计算（带品种信息）"""
        multiplier = self._get_contract_multiplier(symbol)
        amount = quantity * price * multiplier
        fee_rate = self._get_fee_rate(symbol)
        total = amount * fee_rate

        return {
            "total_fee": round(total, 2),
            "detail": {
                "commission": round(total, 2),
                "fee_rate": fee_rate,
                "multiplier": multiplier,
            },
        }

    def calculate_position_size(
        self,
        capital: float,
        price: float,
        symbol: str = "",
    ) -> float:
        """期货可买入手数（考虑保证金）

        期货按手交易，每手 = 合约乘数 × 价格 × 保证金比例
        """
        if price <= 0:
            return 0
        multiplier = self._get_contract_multiplier(symbol) if symbol else 10.0
        margin_per_lot = price * multiplier * self.DEFAULT_MARGIN_RATE
        if margin_per_lot <= 0:
            return 0
        lots = int(capital / margin_per_lot)
        return float(lots)

    def get_margin_required(
        self,
        symbol: str,
        quantity: float,
        price: float,
    ) -> float:
        """计算所需保证金"""
        multiplier = self._get_contract_multiplier(symbol)
        return quantity * price * multiplier * self.DEFAULT_MARGIN_RATE


# =============================================================================
# 美股规则
# =============================================================================

class USStockRules(TradingRules):
    """美股交易规则

    - T+0：当日可买卖（简化）
    - 无涨跌停限制
    - 最小交易单位：1 股
    - 费用：佣金每股 $0.005，最低 $1
    """

    market = "USStock"

    def can_sell(
        self,
        position: Dict[str, Any],
        current_bar: Dict[str, Any],
        trade_date: date,
    ) -> bool:
        """美股 T+0：当日可买卖"""
        return True

    def check_price_limit(
        self,
        symbol: str,
        price: float,
        prev_close: float,
    ) -> Dict[str, Any]:
        """美股无涨跌停限制"""
        return {
            "is_limit_up": False,
            "is_limit_down": False,
            "upper_limit": float("inf"),
            "lower_limit": 0,
        }

    def calculate_fee(
        self,
        side: str,
        quantity: float,
        price: float,
    ) -> Dict[str, Any]:
        """美股费用：每股 $0.005，最低 $1"""
        per_share_fee = 0.005
        total = max(quantity * per_share_fee, 1.0)

        return {
            "total_fee": round(total, 2),
            "detail": {
                "commission": round(total, 2),
                "per_share": per_share_fee,
            },
        }

    def calculate_position_size(
        self,
        capital: float,
        price: float,
    ) -> float:
        """美股可买入数量：向下取整到 1 股"""
        if price <= 0:
            return 0
        return float(int(capital / price))


# =============================================================================
# 规则工厂
# =============================================================================

_RULES_CACHE: Dict[str, TradingRules] = {}


def get_trading_rules(market: str) -> TradingRules:
    """获取指定市场的交易规则实例

    Args:
        market: 市场类型（CNStock/HKStock/USStock/CNFutures）

    Returns:
        对应的交易规则实例

    Raises:
        ValueError: 不支持的市场类型
    """
    if market in _RULES_CACHE:
        return _RULES_CACHE[market]

    rules_map = {
        "CNStock": CNStockRules,
        "HKStock": HKStockRules,
        "CNFutures": CNFuturesRules,
        "USStock": USStockRules,
    }

    cls = rules_map.get(market)
    if cls is None:
        raise ValueError(
            f"不支持的市场类型: {market}，"
            f"支持: {', '.join(sorted(rules_map.keys()))}"
        )

    instance = cls()
    _RULES_CACHE[market] = instance
    return instance
