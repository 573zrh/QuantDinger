"""
QuantX 港股数据源 — 三级降级

1. AKShare（首选）：东方财富底层接口
2. yfinance（备选）：Yahoo Finance（.HK 后缀）
3. 腾讯行情（第三备选）：免费 Web API

每个底层数据源均使用 CircuitBreaker 包装。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.base import BaseDataSource
from app.data_sources.circuit_breaker import get_kline_circuit_breaker, get_realtime_circuit_breaker
from app.data_sources.rate_limiter import get_akshare_limiter, get_tencent_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 港股代码最大位数
_HK_CODE_LEN = 5


# ------------------------------------------------------------------
# Symbol 归一化
# ------------------------------------------------------------------

def normalize_hk_code(symbol: str) -> str:
    """将港股代码归一化为 5 位数字（零填充）。

    支持输入: ``0700``, ``700``, ``HK00700``, ``0700.HK``
    """
    s = (symbol or "").strip().upper()
    s = re.sub(r"^HK", "", s)
    s = re.sub(r"\.HK$", "", s)
    digits = re.sub(r"\D", "", s)
    return digits.zfill(_HK_CODE_LEN) if digits else s


def _to_yfinance_symbol(code: str) -> str:
    """港股代码 → yfinance ticker"""
    return f"{int(code):04d}.HK"


# ------------------------------------------------------------------
# HKStockDataSource
# ------------------------------------------------------------------

class HKStockDataSource(BaseDataSource):
    """港股数据源（AKShare + yfinance + 腾讯行情 三级降级）"""

    name = "HKStock"
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
        code = normalize_hk_code(symbol)
        lim = max(int(limit or 300), 1)
        tf = self._normalize_timeframe(timeframe)

        # Tier 1: AKShare
        rows = self._fetch_akshare(code, tf, lim, before_time)
        if rows:
            out = self.filter_and_limit(rows, lim, before_time, after_time)
            self.log_result(code, out, tf)
            return out

        # Tier 2: yfinance
        rows = self._fetch_yfinance(code, tf, lim, before_time)
        if rows:
            out = self.filter_and_limit(rows, lim, before_time, after_time)
            self.log_result(code, out, tf)
            return out

        # Tier 3: 腾讯行情（仅日/周线）
        if tf in ("1D", "1W"):
            rows = self._fetch_tencent(code, tf, lim)
            if rows:
                out = self.filter_and_limit(rows, lim, before_time, after_time)
                self.log_result(code, out, tf)
                return out

        logger.warning(f"HKStock: {code} {tf} 所有数据源均无数据")
        return []

    # ==================================================================
    # 实时行情
    # ==================================================================

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_hk_code(symbol)

        # 腾讯行情
        if self._rt_cb.is_available("tencent_hk"):
            try:
                get_tencent_limiter().wait()
                result = self._fetch_tencent_quote(code)
                if result and result.get("last", 0) > 0:
                    self._rt_cb.record_success("tencent_hk")
                    return result
            except Exception as e:
                self._rt_cb.record_failure("tencent_hk", str(e))
                logger.debug(f"腾讯港股行情失败 {code}: {e}")

        return {"last": 0, "symbol": code}

    # ==================================================================
    # AKShare
    # ==================================================================

    def _fetch_akshare(
        self, code: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        source_key = "akshare_hk_kline"
        if not self._cb.is_available(source_key):
            return []

        try:
            import akshare as ak
        except ImportError:
            return []

        get_akshare_limiter().wait()

        try:
            ak_code = str(int(code)).zfill(5)
            end = datetime.fromtimestamp(before_time) if before_time else datetime.now()
            start = end - timedelta(days=max(limit, 300) * 2)
            fmt = "%Y%m%d"

            if timeframe in ("1m", "5m", "15m", "30m", "1H"):
                period_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60"}
                period = period_map.get(timeframe, "1")
                df = ak.stock_hk_hist_min_em(
                    symbol=ak_code, period=period, adjust="qfq",
                    start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                )
            elif timeframe == "1W":
                df = ak.stock_hk_hist(
                    symbol=ak_code, period="weekly", adjust="qfq",
                    start_date=start.strftime(fmt), end_date=end.strftime(fmt),
                )
            else:
                df = ak.stock_hk_hist(
                    symbol=ak_code, period="daily", adjust="qfq",
                    start_date=start.strftime(fmt), end_date=end.strftime(fmt),
                )

            if df is None or getattr(df, "empty", True):
                self._cb.record_failure(source_key, "空数据")
                return []

            rows = self._parse_akshare_df(df)
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"AKShare 港股 K 线失败 {code} {timeframe}: {e}")
            return []

    @staticmethod
    def _parse_akshare_df(df) -> List[Dict[str, Any]]:
        """解析 AKShare DataFrame"""
        import pandas as pd

        cols = [str(c) for c in df.columns]
        time_col = None
        for c in ("时间", "日期", "Datetime", "Date"):
            if c in cols:
                time_col = c
                break
        if time_col is None and cols:
            time_col = cols[0]

        def _pick(zh: str, en_list: List[str], idx: int) -> Optional[str]:
            if zh in cols:
                return zh
            for en in en_list:
                if en in cols:
                    return en
            return cols[idx] if idx < len(cols) else None

        c_open = _pick("开盘", ["Open"], 1)
        c_close = _pick("收盘", ["Close"], 2)
        c_high = _pick("最高", ["High"], 3)
        c_low = _pick("最低", ["Low"], 4)
        c_vol = _pick("成交量", ["Volume"], 5)

        if not all([time_col, c_open, c_close, c_high, c_low]):
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                t = pd.Timestamp(row[time_col])
                ts = int(t.timestamp())
                o, c, h, low = float(row[c_open]), float(row[c_close]), float(row[c_high]), float(row[c_low])
                v = float(row[c_vol]) if c_vol else 0.0
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
    # yfinance
    # ==================================================================

    def _fetch_yfinance(
        self, code: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        source_key = "yfinance_hk_kline"
        if not self._cb.is_available(source_key):
            return []

        try:
            import yfinance as yf
        except ImportError:
            return []

        yf_sym = _to_yfinance_symbol(code)
        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1H": "1h", "4H": "1h", "1D": "1d", "1W": "1wk",
        }
        interval = interval_map.get(timeframe)
        if not interval:
            return []

        days_map = {
            "1m": lambda n: min(7, max(2, (n // 240) + 2)),
            "5m": lambda n: min(60, max(3, (n // 48) + 3)),
            "15m": lambda n: min(60, max(3, (n // 16) + 3)),
            "30m": lambda n: min(60, max(5, (n // 8) + 5)),
            "1H": lambda n: min(730, max(8, (n // 4) + 8)),
            "4H": lambda n: min(730, max(20, n + 10)),
            "1D": lambda n: min(3650, n + 10),
            "1W": lambda n: min(3650, n * 7 + 30),
        }
        days_fn = days_map.get(timeframe, lambda n: n + 10)
        days = days_fn(limit)

        end = datetime.fromtimestamp(before_time) if before_time else datetime.now()
        start = end - timedelta(days=days)

        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(
                start=start.strftime("%Y-%m-%d"),
                end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                interval=interval,
            )
            if df is None or df.empty:
                self._cb.record_failure(source_key, "空数据")
                return []

            df = df.reset_index()
            time_col = None
            for c in ("Datetime", "Date", "index"):
                if c in df.columns:
                    time_col = c
                    break
            if time_col is None:
                self._cb.record_failure(source_key, "无时间列")
                return []

            rows: List[Dict[str, Any]] = []
            for _, row in df.iterrows():
                try:
                    tv = row[time_col]
                    ts = int(tv.timestamp()) if hasattr(tv, "timestamp") else None
                    if ts is None:
                        continue
                    o, h, low, c, v = (
                        float(row["Open"]), float(row["High"]),
                        float(row["Low"]), float(row["Close"]),
                        float(row["Volume"]),
                    )
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
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"yfinance 港股 K 线失败 {yf_sym}: {e}")
            return []

    # ==================================================================
    # 腾讯行情
    # ==================================================================

    def _fetch_tencent(self, code: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
        source_key = "tencent_hk_kline"
        if not self._cb.is_available(source_key):
            return []

        get_tencent_limiter().wait()

        period = "day" if timeframe == "1D" else "week"
        hk_num = str(int(code)).zfill(4) if code.isdigit() else code
        url = (
            f"http://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
            f"?param=hk{hk_num},{period},,,{limit},qfq"
        )

        try:
            resp = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "http://finance.qq.com",
            })
            resp.raise_for_status()
            data = resp.json()

            kline_data = (data.get("data") or {}).get(f"hk{hk_num}") or {}
            bars = kline_data.get(period) or kline_data.get(f"qfq{period}") or []
            if not bars:
                self._cb.record_failure(source_key, "空数据")
                return []

            rows: List[Dict[str, Any]] = []
            for bar in bars:
                try:
                    if len(bar) < 6:
                        continue
                    dt = datetime.strptime(bar[0], "%Y-%m-%d")
                    ts = int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
                    rows.append({
                        "time": ts,
                        "open": round(float(bar[1]), 4),
                        "high": round(float(bar[3]), 4),
                        "low": round(float(bar[4]), 4),
                        "close": round(float(bar[2]), 4),
                        "volume": round(float(bar[5]), 2),
                    })
                except Exception:
                    continue

            rows.sort(key=lambda x: x["time"])
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.debug(f"腾讯港股 K 线失败 {code}: {e}")
            return []

    def _fetch_tencent_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """腾讯港股实时报价"""
        hk_num = str(int(code)).zfill(4) if code.isdigit() else code
        url = f"http://qt.gtimg.cn/q=hk{hk_num}"
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            text = resp.text
            parts = text.split('"')
            if len(parts) < 2:
                return None
            fields = parts[1].split("~")
            if len(fields) < 10:
                return None
            last = float(fields[3]) if fields[3] else 0
            prev_close = float(fields[4]) if fields[4] else 0
            change = last - prev_close if prev_close else 0
            return {
                "last": last,
                "change": round(change, 4),
                "changePercent": round((change / prev_close * 100) if prev_close else 0, 2),
                "high": float(fields[5]) if len(fields) > 5 and fields[5] else 0,
                "low": float(fields[6]) if len(fields) > 6 and fields[6] else 0,
                "open": float(fields[7]) if len(fields) > 7 and fields[7] else 0,
                "previousClose": prev_close,
                "name": fields[1] if len(fields) > 1 else "",
                "symbol": code,
            }
        except Exception as e:
            logger.debug(f"腾讯港股报价失败 {code}: {e}")
            return None

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
