"""
QuantX 交易日历管理器

支持四个市场的交易日历判断：
- CNStock（A 股）：周一至周五，排除中国法定假日
- HKStock（港股）：周一至周五，排除香港公众假日
- CNFutures（中国期货）：与 A 股一致
- USStock（美股）：周一至周五，排除美国假日
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Set

from app.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# 假日数据（2024-2026 年主要假日）
# =============================================================================

# 中国法定假日（A 股 + 期货休市日）
_CN_HOLIDAYS: Set[date] = {
    # --- 2024 ---
    # 元旦
    date(2024, 1, 1),
    # 春节（2月10日-2月17日，调休2月18日上班）
    date(2024, 2, 9), date(2024, 2, 10), date(2024, 2, 11),
    date(2024, 2, 12), date(2024, 2, 13), date(2024, 2, 14),
    date(2024, 2, 15), date(2024, 2, 16), date(2024, 2, 17),
    # 清明
    date(2024, 4, 4), date(2024, 4, 5), date(2024, 4, 6),
    # 劳动节
    date(2024, 5, 1), date(2024, 5, 2), date(2024, 5, 3),
    date(2024, 5, 4), date(2024, 5, 5),
    # 端午
    date(2024, 6, 8), date(2024, 6, 9), date(2024, 6, 10),
    # 中秋
    date(2024, 9, 15), date(2024, 9, 16), date(2024, 9, 17),
    # 国庆
    date(2024, 10, 1), date(2024, 10, 2), date(2024, 10, 3),
    date(2024, 10, 4), date(2024, 10, 5), date(2024, 10, 6),
    date(2024, 10, 7),
    # --- 2025 ---
    # 元旦
    date(2025, 1, 1),
    # 春节（1月28日-2月4日）
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 1, 31), date(2025, 2, 1), date(2025, 2, 2),
    date(2025, 2, 3), date(2025, 2, 4),
    # 清明
    date(2025, 4, 4), date(2025, 4, 5), date(2025, 4, 6),
    # 劳动节
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3),
    date(2025, 5, 4), date(2025, 5, 5),
    # 端午
    date(2025, 5, 31), date(2025, 6, 1), date(2025, 6, 2),
    # 中秋+国庆（合并长假）
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
    date(2025, 10, 4), date(2025, 10, 5), date(2025, 10, 6),
    date(2025, 10, 7), date(2025, 10, 8),
    # --- 2026 ---
    # 元旦
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    # 春节（2月17日-2月23日）
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21),
    date(2026, 2, 22),
    # 清明
    date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7),
    # 劳动节
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
    date(2026, 5, 4), date(2026, 5, 5),
    # 端午
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 中秋
    date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
    # 国庆
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6),
    date(2026, 10, 7),
}

# 香港公众假日（港股休市日，与 A 股类似但有差异）
_HK_HOLIDAYS: Set[date] = {
    # --- 2024 ---
    date(2024, 1, 1),   # 元旦
    date(2024, 2, 10), date(2024, 2, 11), date(2024, 2, 12),
    date(2024, 2, 13),  # 农历新年
    date(2024, 3, 29), date(2024, 4, 1),   # 耶稣受难节 + 复活节星期一
    date(2024, 4, 4),   # 清明节
    date(2024, 5, 1),   # 劳动节
    date(2024, 5, 15),  # 佛诞
    date(2024, 6, 10),  # 端午节
    date(2024, 7, 1),   # 香港特别行政区成立纪念日
    date(2024, 9, 18),  # 中秋节翌日
    date(2024, 10, 1),  # 国庆节
    date(2024, 10, 11), # 重阳节
    date(2024, 12, 25), date(2024, 12, 26), # 圣诞节
    # --- 2025 ---
    date(2025, 1, 1),   # 元旦
    date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31),  # 农历新年
    date(2025, 4, 4),   # 清明节
    date(2025, 4, 18), date(2025, 4, 19), date(2025, 4, 21),  # 耶稣受难节 + 复活节
    date(2025, 5, 1),   # 劳动节
    date(2025, 5, 24),  # 佛诞
    date(2025, 6, 2),   # 端午节
    date(2025, 7, 1),   # 香港回归纪念日
    date(2025, 10, 1),  # 国庆节翌日
    date(2025, 10, 7),  # 中秋节翌日
    date(2025, 10, 29), # 重阳节
    date(2025, 12, 25), date(2025, 12, 26), # 圣诞节
    # --- 2026 ---
    date(2026, 1, 1),   # 元旦
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),  # 农历新年
    date(2026, 4, 3), date(2026, 4, 4), date(2026, 4, 6),    # 耶稣受难节 + 复活节
    date(2026, 4, 5),   # 清明节
    date(2026, 5, 1),   # 劳动节
    date(2026, 5, 14),  # 佛诞
    date(2026, 6, 19),  # 端午节
    date(2026, 7, 1),   # 香港回归纪念日
    date(2026, 9, 26),  # 中秋节翌日
    date(2026, 10, 1),  # 国庆节翌日
    date(2026, 10, 18), # 重阳节翌日
    date(2026, 12, 25), date(2026, 12, 26), # 圣诞节
}

# 美国假日（美股休市日）
_US_HOLIDAYS: Set[date] = {
    # --- 2024 ---
    date(2024, 1, 1),   # New Year's Day
    date(2024, 1, 15),  # Martin Luther King Jr. Day
    date(2024, 2, 19),  # Presidents' Day
    date(2024, 3, 29),  # Good Friday
    date(2024, 5, 27),  # Memorial Day
    date(2024, 6, 19),  # Juneteenth
    date(2024, 7, 4),   # Independence Day
    date(2024, 9, 2),   # Labor Day
    date(2024, 11, 28), # Thanksgiving
    date(2024, 12, 25), # Christmas
    # --- 2025 ---
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents' Day
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 27), # Thanksgiving
    date(2025, 12, 25), # Christmas
    # --- 2026 ---
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}

# 市场 → 假日集合映射
_MARKET_HOLIDAYS: Dict[str, Set[date]] = {
    "CNStock": _CN_HOLIDAYS,
    "CNFutures": _CN_HOLIDAYS,   # 期货与 A 股假日一致
    "HKStock": _HK_HOLIDAYS,
    "USStock": _US_HOLIDAYS,
}


class TradingCalendar:
    """交易日历管理器

    根据市场类型判断交易日、获取前后交易日等。
    所有方法均基于内置的假日数据 + 周末判断。
    """

    def __init__(self):
        self._holidays = _MARKET_HOLIDAYS

    def _get_holidays(self, market: str) -> Set[date]:
        """获取指定市场的假日集合"""
        return self._holidays.get(market, set())

    @staticmethod
    def _is_weekend(d: date) -> bool:
        """判断是否为周末（周六=5, 周日=6）"""
        return d.weekday() >= 5

    def is_trading_day(self, market: str, d: date) -> bool:
        """判断是否为交易日

        Args:
            market: 市场类型（CNStock/HKStock/USStock/CNFutures）
            d: 日期

        Returns:
            是否为交易日
        """
        if self._is_weekend(d):
            return False
        holidays = self._get_holidays(market)
        return d not in holidays

    def get_next_trading_day(self, market: str, d: date) -> date:
        """获取下一个交易日（不含当日）

        Args:
            market: 市场类型
            d: 起始日期

        Returns:
            下一个交易日
        """
        next_day = d + timedelta(days=1)
        # 最多向前搜索 30 天（防止极端情况无限循环）
        for _ in range(30):
            if self.is_trading_day(market, next_day):
                return next_day
            next_day += timedelta(days=1)
        # 兜底：返回 d+1（不应到达此处）
        logger.warning(f"无法找到 {market} 在 {d} 之后的下一个交易日")
        return d + timedelta(days=1)

    def get_prev_trading_day(self, market: str, d: date) -> date:
        """获取上一个交易日（不含当日）

        Args:
            market: 市场类型
            d: 起始日期

        Returns:
            上一个交易日
        """
        prev_day = d - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(market, prev_day):
                return prev_day
            prev_day -= timedelta(days=1)
        logger.warning(f"无法找到 {market} 在 {d} 之前的上一个交易日")
        return d - timedelta(days=1)

    def get_trading_days(
        self, market: str, start_date: date, end_date: date
    ) -> List[date]:
        """获取日期范围内的交易日列表（含首尾）

        Args:
            market: 市场类型
            start_date: 起始日期
            end_date: 结束日期

        Returns:
            按升序排列的交易日列表
        """
        if start_date > end_date:
            return []

        trading_days: List[date] = []
        current = start_date
        while current <= end_date:
            if self.is_trading_day(market, current):
                trading_days.append(current)
            current += timedelta(days=1)
        return trading_days

    def count_trading_days(
        self, market: str, start_date: date, end_date: date
    ) -> int:
        """计算日期范围内的交易日数量"""
        return len(self.get_trading_days(market, start_date, end_date))


# 全局单例
trading_calendar = TradingCalendar()
