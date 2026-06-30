"""
QuantX 美股数据源 — 两级降级

1. yfinance（首选）：Yahoo Finance 免费接口，全球可用
2. Alpaca（备选）：需要 API Key，提供更实时的数据

每个底层数据源均使用 CircuitBreaker 包装。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.circuit_breaker import get_kline_circuit_breaker, get_realtime_circuit_breaker
from app.config.api_keys import api_keys
from app.utils.logger import get_logger

logger = get_logger(__name__)


class USStockDataSource(BaseDataSource):
    """美股数据源（yfinance + Alpaca 两级降级）"""

    name = "USStock"
    _cb = get_kline_circuit_breaker()
    _rt_cb = get_realtime_circuit_breaker()

    # yfinance 参数映射
    INTERVAL_MAP = {
        "1m": "1m", "3m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1wk",
    }
    DAYS_MAP = {
        "1m": lambda n: min(7, max(1, (n // 390) + 2)),
        "3m": lambda n: min(7, max(1, (n // 130) + 2)),
        "5m": lambda n: min(60, max(1, (n // 78) + 2)),
        "15m": lambda n: min(60, max(2, (n // 26) + 3)),
        "30m": lambda n: min(60, max(2, (n // 13) + 3)),
        "1H": lambda n: min(730, max(5, int(n / 6.5 * 7 / 5 * 1.5) + 5)),
        "4H": lambda n: min(730, max(10, int(n / 1.625 * 7 / 5 * 1.5) + 5)),
        "1D": lambda n: min(3650, n + 1),
        "1W": lambda n: min(3650, (n * 7) + 7),
    }
    MERGE_FACTOR_MAP = {"3m": 3}

    TIMEFRAME_ALIASES = {
        "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W",
        "d": "1D", "w": "1W", "day": "1D", "week": "1W",
        "60m": "1H", "240m": "4H",
    }

    def __init__(self):
        self._alpaca_available = api_keys.has_alpaca

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
        sym = (symbol or "").strip().upper()
        tf = self._normalize_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 1: yfinance
        rows = self._fetch_yfinance(sym, tf, lim, before_time)
        if rows:
            out = self.filter_and_limit(rows, lim, before_time, after_time)
            self.log_result(sym, out, tf)
            return out

        # Tier 2: Alpaca（如果配置了 API Key）
        if self._alpaca_available:
            rows = self._fetch_alpaca(sym, tf, lim, before_time)
            if rows:
                out = self.filter_and_limit(rows, lim, before_time, after_time)
                self.log_result(sym, out, tf)
                return out

        logger.warning(f"USStock: {sym} {tf} 所有数据源均无数据")
        return []

    # ==================================================================
    # 实时行情
    # ==================================================================

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        sym = (symbol or "").strip().upper()

        # yfinance fast_info
        if self._rt_cb.is_available("yfinance_us_ticker"):
            try:
                import yfinance as yf
                ticker = yf.Ticker(self._yahoo_symbol(sym))
                fast_info = ticker.fast_info
                last = fast_info.get("lastPrice") or fast_info.get("last_price")
                prev = fast_info.get("previousClose") or fast_info.get("previous_close")
                if last:
                    last = float(last)
                    prev = float(prev) if prev else 0
                    change = last - prev if prev else 0
                    self._rt_cb.record_success("yfinance_us_ticker")
                    return {
                        "last": last,
                        "change": round(change, 4),
                        "changePercent": round((change / prev * 100) if prev else 0, 2),
                        "high": float(fast_info.get("dayHigh") or fast_info.get("day_high") or last),
                        "low": float(fast_info.get("dayLow") or fast_info.get("day_low") or last),
                        "open": float(fast_info.get("open") or last),
                        "previousClose": prev,
                    }
            except Exception as e:
                self._rt_cb.record_failure("yfinance_us_ticker", str(e))
                logger.debug(f"yfinance ticker 失败 {sym}: {e}")

        # yfinance history 降级
        try:
            import yfinance as yf
            ticker = yf.Ticker(self._yahoo_symbol(sym))
            hist = ticker.history(period="1d", interval="1m")
            if hist is not None and not hist.empty:
                last_price = float(hist["Close"].iloc[-1])
                open_price = float(hist["Open"].iloc[0])
                return {
                    "last": last_price,
                    "change": round(last_price - open_price, 4),
                    "changePercent": round(
                        (last_price - open_price) / open_price * 100 if open_price else 0, 2
                    ),
                    "high": float(hist["High"].max()),
                    "low": float(hist["Low"].min()),
                    "open": open_price,
                    "previousClose": open_price,
                }
        except Exception as e:
            logger.debug(f"yfinance history 降级失败 {sym}: {e}")

        return {"last": 0, "symbol": sym}

    # ==================================================================
    # yfinance
    # ==================================================================

    def _fetch_yfinance(
        self, symbol: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        source_key = "yfinance_us_kline"
        if not self._cb.is_available(source_key):
            return []

        try:
            import yfinance as yf
        except ImportError:
            return []

        interval = self.INTERVAL_MAP.get(timeframe, "1d")
        merge_factor = self.MERGE_FACTOR_MAP.get(timeframe, 1)
        effective_limit = limit * merge_factor
        days_func = self.DAYS_MAP.get(timeframe, lambda n: n + 1)
        days = days_func(effective_limit)

        if before_time:
            end_date = datetime.fromtimestamp(before_time)
        else:
            end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        try:
            ticker = yf.Ticker(self._yahoo_symbol(symbol))
            df = ticker.history(
                start=start_date.strftime("%Y-%m-%d"),
                end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                interval=interval,
            )
            if df is None or df.empty:
                self._cb.record_failure(source_key, "空数据")
                return []

            rows = self._convert_df(df, effective_limit)
            if merge_factor > 1 and rows:
                rows = self._merge_bars(rows, merge_factor)
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"yfinance 美股 K 线失败 {symbol}: {e}")
            return []

    # ==================================================================
    # Alpaca
    # ==================================================================

    def _fetch_alpaca(
        self, symbol: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        source_key = "alpaca_us_kline"
        if not self._cb.is_available(source_key):
            return []

        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
        except ImportError:
            logger.debug("alpaca-py 未安装，跳过 Alpaca 数据源")
            self._alpaca_available = False
            return []

        tf_map = {
            "1m": TimeFrame.Minute, "5m": TimeFrame.Minute,
            "15m": TimeFrame.Minute, "1H": TimeFrame.Hour,
            "1D": TimeFrame.Day, "1W": TimeFrame.Week,
        }
        tf_obj = tf_map.get(timeframe)
        if tf_obj is None:
            return []

        if before_time:
            end = datetime.fromtimestamp(before_time)
        else:
            end = datetime.now()
        start = end - timedelta(days=self.DAYS_MAP.get(timeframe, lambda n: n + 1)(limit))

        try:
            client = StockHistoricalDataClient(
                api_key=api_keys.ALPACA_API_KEY,
                secret_key=api_keys.ALPACA_SECRET_KEY,
            )
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf_obj,
                start=start,
                end=end,
            )
            bars = client.get_stock_bars(request)
            df = bars.df
            if df is None or df.empty:
                self._cb.record_failure(source_key, "空数据")
                return []

            rows: List[Dict[str, Any]] = []
            for idx, row in df.iterrows():
                try:
                    ts = int(idx[1].timestamp()) if isinstance(idx, tuple) else int(idx.timestamp())
                    rows.append(self.format_kline(
                        ts, row["open"], row["high"], row["low"], row["close"], row["volume"],
                    ))
                except Exception:
                    continue
            rows.sort(key=lambda x: x["time"])
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows[-limit:]

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"Alpaca 美股 K 线失败 {symbol}: {e}")
            return []

    # ==================================================================
    # 工具方法
    # ==================================================================

    @staticmethod
    def _yahoo_symbol(symbol: str) -> str:
        """将 symbol 转换为 yfinance 兼容格式"""
        sym = (symbol or "").strip().upper()
        if "." in sym:
            return sym.replace(".", "-")
        return sym

    def _normalize_timeframe(self, tf: str) -> str:
        t = (tf or "1D").strip()
        return self.TIMEFRAME_ALIASES.get(t.lower(), t)

    @staticmethod
    def _convert_df(df, limit: int) -> List[Dict[str, Any]]:
        """将 yfinance DataFrame 转换为 K 线列表"""
        df = df.tail(limit).reset_index()
        time_col = None
        for c in ("Datetime", "Date", "index"):
            if c in df.columns:
                time_col = c
                break
        if time_col is None:
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                tv = row[time_col]
                ts = int(tv.timestamp()) if hasattr(tv, "timestamp") else None
                if ts is None:
                    continue
                rows.append({
                    "time": ts,
                    "open": round(float(row["Open"]), 4),
                    "high": round(float(row["High"]), 4),
                    "low": round(float(row["Low"]), 4),
                    "close": round(float(row["Close"]), 4),
                    "volume": round(float(row["Volume"]), 2),
                })
            except Exception:
                continue
        return rows

    @staticmethod
    def _merge_bars(bars: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        """将 n 根相邻 K 线合并为 1 根"""
        if n <= 1 or len(bars) < n:
            return bars
        bars = sorted(bars, key=lambda x: x["time"])
        out: List[Dict[str, Any]] = []
        for i in range(0, len(bars) - len(bars) % n, n):
            chunk = bars[i: i + n]
            out.append({
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "high": max(b["high"] for b in chunk),
                "low": min(b["low"] for b in chunk),
                "close": chunk[-1]["close"],
                "volume": round(sum(b["volume"] for b in chunk), 2),
            })
        return out
