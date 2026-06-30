"""
QuantX 中国期货数据源

使用 AKShare 获取中国期货市场的 K 线和行情数据：
- 日线：``ak.futures_zh_daily_sina`` / ``ak.futures_main_sina``
- 分钟线：``ak.futures_zh_minute_sina``
- 实时行情：``ak.futures_zh_spot``

CTP 实时数据接入留到后续 Task。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.circuit_breaker import get_kline_circuit_breaker, get_realtime_circuit_breaker
from app.data_sources.rate_limiter import get_akshare_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 主力合约标识正则（如 IF0, IFL0, rb0）
_MAIN_CONTRACT_RE = re.compile(r"^([A-Za-z]+)0$", re.IGNORECASE)


def normalize_futures_symbol(symbol: str) -> str:
    """归一化期货合约代码。

    支持输入: ``IF2401``, ``rb2401``, ``IF0``（主力）, ``IFL0``（连续）
    返回标准化的原始代码字符串。
    """
    s = (symbol or "").strip()
    return s


def is_main_contract(symbol: str) -> bool:
    """判断是否为主力/连续合约（以 ``0`` 结尾）"""
    return bool(_MAIN_CONTRACT_RE.match(symbol.strip()))


class CNFuturesDataSource(BaseDataSource):
    """中国期货数据源（AKShare）"""

    name = "CNFutures"
    _cb = get_kline_circuit_breaker()
    _rt_cb = get_realtime_circuit_breaker()

    # ==================================================================
    # K 线
    # ==================================================================

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        sym = normalize_futures_symbol(symbol)
        tf = self._normalize_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # AKShare 分钟线
        if tf in ("1m", "5m", "15m", "30m", "1H"):
            rows = self._fetch_akshare_minute(sym, tf, lim, before_time)
            if rows:
                out = self.filter_and_limit(rows, lim, before_time, after_time)
                self.log_result(sym, out, tf)
                return out

        # AKShare 日线（包括主力合约）
        if tf in ("1D", "1W"):
            rows = self._fetch_akshare_daily(sym, tf, lim, before_time)
            if rows:
                out = self.filter_and_limit(rows, lim, before_time, after_time)
                self.log_result(sym, out, tf)
                return out

        logger.warning(f"CNFutures: {sym} {tf} 无数据")
        return []

    # ==================================================================
    # 实时行情
    # ==================================================================

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        sym = normalize_futures_symbol(symbol)
        source_key = "akshare_futures_ticker"

        if not self._rt_cb.is_available(source_key):
            return {"last": 0, "symbol": sym}

        try:
            import akshare as ak
            get_akshare_limiter().wait()

            df = ak.futures_zh_spot()
            if df is None or df.empty:
                self._rt_cb.record_failure(source_key, "空数据")
                return {"last": 0, "symbol": sym}

            # 查找匹配行（按 symbol 或合约简称匹配）
            code_lower = sym.lower()
            for _, row in df.iterrows():
                row_str = str(row.get("symbol", "")).lower()
                if code_lower in row_str:
                    last = float(row.get("current_price", 0) or row.get("最新价", 0) or 0)
                    if last > 0:
                        self._rt_cb.record_success(source_key)
                        return {
                            "last": last,
                            "symbol": sym,
                            "change": 0,
                            "changePercent": 0,
                        }

            self._rt_cb.record_failure(source_key, "未找到匹配合约")
            return {"last": 0, "symbol": sym}

        except Exception as e:
            self._rt_cb.record_failure(source_key, str(e))
            logger.debug(f"AKShare 期货行情失败 {sym}: {e}")
            return {"last": 0, "symbol": sym}

    # ==================================================================
    # AKShare 分钟线
    # ==================================================================

    def _fetch_akshare_minute(
        self, symbol: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        source_key = "akshare_futures_minute"
        if not self._cb.is_available(source_key):
            return []

        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装")
            return []

        get_akshare_limiter().wait()

        period_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60"}
        period = period_map.get(timeframe, "1")

        try:
            df = ak.futures_zh_minute_sina(symbol=symbol, period=period)
            if df is None or getattr(df, "empty", True):
                self._cb.record_failure(source_key, "空数据")
                return []

            rows = self._parse_minute_df(df)
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"AKShare 期货分钟线失败 {symbol} {timeframe}: {e}")
            return []

    @staticmethod
    def _parse_minute_df(df) -> List[Dict[str, Any]]:
        """解析期货分钟线 DataFrame"""
        import pandas as pd

        cols = [str(c) for c in df.columns]
        # AKShare 期货分钟线列名：datetime, open, high, low, close, volume
        time_col = None
        for c in ("datetime", "Datetime", "时间", cols[0] if cols else ""):
            if c in cols:
                time_col = c
                break

        if not time_col:
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                t = pd.Timestamp(row[time_col])
                ts = int(t.timestamp())
                o = float(row.get("open", row.iloc[1]))
                h = float(row.get("high", row.iloc[2]))
                low = float(row.get("low", row.iloc[3]))
                c = float(row.get("close", row.iloc[4]))
                v = float(row.get("volume", row.iloc[5]) if len(row) > 5 else 0)
                if o == 0 and c == 0:
                    continue
                rows.append({
                    "time": ts,
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(low, 4), "close": round(c, 4),
                    "volume": round(v, 2),
                })
            except Exception:
                continue
        rows.sort(key=lambda x: x["time"])
        return rows

    # ==================================================================
    # AKShare 日线
    # ==================================================================

    def _fetch_akshare_daily(
        self, symbol: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        source_key = "akshare_futures_daily"
        if not self._cb.is_available(source_key):
            return []

        try:
            import akshare as ak
        except ImportError:
            return []

        get_akshare_limiter().wait()

        try:
            # 主力合约使用 futures_main_sina
            if is_main_contract(symbol):
                df = ak.futures_main_sina(symbol=symbol)
            else:
                df = ak.futures_zh_daily_sina(symbol=symbol)

            if df is None or getattr(df, "empty", True):
                self._cb.record_failure(source_key, "空数据")
                return []

            rows = self._parse_daily_df(df, timeframe, limit, before_time)
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"AKShare 期货日线失败 {symbol}: {e}")
            return []

    @staticmethod
    def _parse_daily_df(
        df, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        """解析期货日线 DataFrame"""
        import pandas as pd

        cols = [str(c) for c in df.columns]
        time_col = None
        for c in ("date", "Date", "日期", "datetime", cols[0] if cols else ""):
            if c in cols:
                time_col = c
                break
        if not time_col:
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                t = pd.Timestamp(row[time_col])
                ts = int(t.timestamp())
                if before_time and ts >= before_time:
                    continue
                o = float(row.get("open", row.iloc[1]))
                h = float(row.get("high", row.iloc[2]))
                low = float(row.get("low", row.iloc[3]))
                c = float(row.get("close", row.iloc[4]))
                v = float(row.get("volume", row.iloc[5]) if len(row) > 5 else 0)
                if o == 0 and c == 0:
                    continue
                rows.append({
                    "time": ts,
                    "open": round(o, 4), "high": round(h, 4),
                    "low": round(low, 4), "close": round(c, 4),
                    "volume": round(v, 2),
                })
            except Exception:
                continue

        rows.sort(key=lambda x: x["time"])

        # 周线合并（5 个交易日 → 1 周）
        if timeframe == "1W" and len(rows) >= 5:
            merged: List[Dict[str, Any]] = []
            for i in range(0, len(rows) - len(rows) % 5, 5):
                chunk = rows[i: i + 5]
                merged.append({
                    "time": chunk[0]["time"],
                    "open": chunk[0]["open"],
                    "high": max(b["high"] for b in chunk),
                    "low": min(b["low"] for b in chunk),
                    "close": chunk[-1]["close"],
                    "volume": round(sum(b["volume"] for b in chunk), 2),
                })
            rows = merged

        return rows[-limit:] if len(rows) > limit else rows

    # ==================================================================
    # 辅助
    # ==================================================================

    @staticmethod
    def _normalize_timeframe(tf: str) -> str:
        aliases = {
            "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W",
            "d": "1D", "w": "1W", "day": "1D", "week": "1W",
        }
        t = (tf or "1D").strip()
        return aliases.get(t.lower(), t)
