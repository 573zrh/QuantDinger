"""
QuantX A 股数据源 — 三级降级

1. AKShare（首选）：东方财富底层接口，国内数据最全面
2. yfinance（备选）：Yahoo Finance，全球可用但国内品种可能受限
3. 腾讯行情（第三备选）：免费 Web API，稳定但品种/周期有限

每个底层数据源均使用 CircuitBreaker 包装，失败自动切换。
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional

import requests

from app.data_sources.base import BaseDataSource, TIMEFRAME_SECONDS
from app.data_sources.circuit_breaker import get_kline_circuit_breaker, get_realtime_circuit_breaker
from app.data_sources.rate_limiter import get_akshare_limiter, get_tencent_limiter
from app.utils.logger import get_logger

logger = get_logger(__name__)

# CST 时区偏移（秒）：UTC+8
_CST_OFFSET = 8 * 3600

# 代理环境变量（AKShare 访问国内站点需要绕过代理）
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@contextmanager
def _bypass_proxy() -> Generator[None, None, None]:
    """临时清除代理环境变量，确保 AKShare 可直连国内站点。"""
    saved: Dict[str, str] = {}
    for key in _PROXY_KEYS:
        val = os.environ.pop(key, None)
        if val is not None:
            saved[key] = val
    try:
        yield
    finally:
        for key, val in saved.items():
            os.environ[key] = val


# ------------------------------------------------------------------
# Symbol 归一化
# ------------------------------------------------------------------

def normalize_cn_code(symbol: str) -> str:
    """将 A 股代码归一化为 6 位纯数字。

    支持输入: ``600519``, ``SH600519``, ``sz000001``, ``600519.SS``
    """
    s = (symbol or "").strip().upper()
    # 去除交易所前缀
    s = re.sub(r"^(SH|SZ)", "", s)
    # 去除 Yahoo 后缀
    s = re.sub(r"\.(SS|SZ)$", "", s)
    # 保留数字部分
    digits = re.sub(r"\D", "", s)
    return digits.zfill(6) if digits else s


def _to_yfinance_symbol(code: str) -> str:
    """6 位数字 → yfinance ticker（.SS/.SZ）"""
    if code.startswith("6"):
        return f"{code}.SS"
    return f"{code}.SZ"


# ------------------------------------------------------------------
# CNStockDataSource
# ------------------------------------------------------------------

class CNStockDataSource(BaseDataSource):
    """A 股数据源（AKShare + yfinance + 腾讯行情 三级降级）"""

    name = "CNStock"

    # K 线熔断器
    _cb = get_kline_circuit_breaker()
    # 实时行情熔断器
    _rt_cb = get_realtime_circuit_breaker()

    # ==================================================================
    # K 线获取（三级降级）
    # ==================================================================

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_cn_code(symbol)
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

        # Tier 3: 腾讯行情（仅支持日线/周线）
        if tf in ("1D", "1W"):
            rows = self._fetch_tencent(code, tf, lim)
            if rows:
                out = self.filter_and_limit(rows, lim, before_time, after_time)
                self.log_result(code, out, tf)
                return out

        logger.warning(f"CNStock: {code} {tf} 所有数据源均无数据")
        return []

    # ==================================================================
    # 实时行情
    # ==================================================================

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_cn_code(symbol)
        # 优先腾讯行情（速度快、免费）
        if self._rt_cb.is_available("tencent_cn"):
            try:
                get_tencent_limiter().wait()
                result = self._fetch_tencent_quote(code)
                if result and result.get("last", 0) > 0:
                    self._rt_cb.record_success("tencent_cn")
                    return result
            except Exception as e:
                self._rt_cb.record_failure("tencent_cn", str(e))
                logger.debug(f"腾讯 A 股行情失败 {code}: {e}")

        # 降级 AKShare
        if self._rt_cb.is_available("akshare_cn"):
            try:
                get_akshare_limiter().wait()
                result = self._fetch_akshare_quote(code)
                if result and result.get("last", 0) > 0:
                    self._rt_cb.record_success("akshare_cn")
                    return result
            except Exception as e:
                self._rt_cb.record_failure("akshare_cn", str(e))
                logger.debug(f"AKShare A 股行情失败 {code}: {e}")

        return {"last": 0, "symbol": code}

    # ==================================================================
    # AKShare 数据源
    # ==================================================================

    def _fetch_akshare(
        self, code: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        """通过 AKShare 获取 A 股 K 线"""
        source_key = "akshare_cn_kline"
        if not self._cb.is_available(source_key):
            return []

        try:
            import akshare as ak
        except ImportError:
            logger.debug("akshare 未安装，跳过 AKShare 数据源")
            return []

        get_akshare_limiter().wait()

        try:
            with _bypass_proxy():
                if timeframe in ("1m", "5m", "15m", "30m", "1H"):
                    df = self._akshare_minute(ak, code, timeframe, limit, before_time)
                elif timeframe == "1W":
                    df = self._akshare_daily(ak, code, "weekly", limit, before_time)
                else:
                    df = self._akshare_daily(ak, code, "daily", limit, before_time)

            if df is None or getattr(df, "empty", True):
                self._cb.record_failure(source_key, "空数据")
                return []

            rows = self._parse_akshare_df(df, timeframe)
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"AKShare A 股 K 线失败 {code} {timeframe}: {e}")
            return []

    @staticmethod
    def _akshare_minute(ak, code: str, timeframe: str, limit: int, before_time: Optional[int]):
        """AKShare 分钟线"""
        period_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60"}
        period = period_map.get(timeframe, "1")
        end = datetime.fromtimestamp(before_time) if before_time else datetime.now()
        start = end - timedelta(days=16)
        fmt = "%Y-%m-%d %H:%M:%S"
        try:
            return ak.stock_zh_a_hist_min_em(
                symbol=code, period=period, adjust="qfq",
                start_date=start.strftime(fmt), end_date=end.strftime(fmt),
            )
        except Exception as e:
            logger.debug(f"AKShare 分钟线异常 {code}: {e}")
            return None

    @staticmethod
    def _akshare_daily(ak, code: str, period: str, limit: int, before_time: Optional[int]):
        """AKShare 日线/周线"""
        end = datetime.fromtimestamp(before_time) if before_time else datetime.now()
        start = end - timedelta(days=max(limit, 300) * 2)
        fmt = "%Y%m%d"
        try:
            return ak.stock_zh_a_hist(
                symbol=code, period=period, adjust="qfq",
                start_date=start.strftime(fmt), end_date=end.strftime(fmt),
            )
        except Exception as e:
            logger.debug(f"AKShare 日/周线异常 {code}: {e}")
            return None

    def _parse_akshare_df(self, df, timeframe: str) -> List[Dict[str, Any]]:
        """将 AKShare DataFrame 解析为统一 K 线格式"""
        import pandas as pd

        cols = [str(c) for c in df.columns]
        # 检测时间列（中文或英文）
        time_col = None
        for candidate in ("时间", "日期", "Datetime", "Date", "date"):
            if candidate in cols:
                time_col = candidate
                break
        if time_col is None and len(cols) > 0:
            time_col = cols[0]

        # 检测 OHLCV 列
        def _pick(zh_name: str, en_names: List[str], idx: int) -> Optional[str]:
            if zh_name in cols:
                return zh_name
            for en in en_names:
                if en in cols:
                    return en
            if idx < len(cols):
                return cols[idx]
            return None

        c_open = _pick("开盘", ["Open", "open"], 1)
        c_close = _pick("收盘", ["Close", "close"], 2)
        c_high = _pick("最高", ["High", "high"], 3)
        c_low = _pick("最低", ["Low", "low"], 4)
        c_vol = _pick("成交量", ["Volume", "volume"], 5)

        if not all([time_col, c_open, c_close, c_high, c_low]):
            logger.debug(f"AKShare DataFrame 列名不匹配: {cols}")
            return []

        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                t = pd.Timestamp(row[time_col])
                ts = int(t.timestamp())
                # 如果是日线且无时区信息，假定 CST → 转 UTC
                if timeframe in ("1D", "1W") and t.tzinfo is None:
                    ts = int(t.timestamp()) - _CST_OFFSET + _CST_OFFSET  # pandas 本地化
                o = float(row[c_open])
                c = float(row[c_close])
                h = float(row[c_high])
                low = float(row[c_low])
                v = float(row[c_vol]) if c_vol else 0.0
                if o == 0 and c == 0:
                    continue
                rows.append(self.format_kline(ts, o, h, low, c, v))
            except Exception:
                continue
        rows.sort(key=lambda x: x["time"])
        return rows

    # ==================================================================
    # yfinance 数据源
    # ==================================================================

    def _fetch_yfinance(
        self, code: str, timeframe: str, limit: int, before_time: Optional[int]
    ) -> List[Dict[str, Any]]:
        """通过 yfinance 获取 A 股 K 线"""
        source_key = "yfinance_cn_kline"
        if not self._cb.is_available(source_key):
            return []

        try:
            import yfinance as yf
        except ImportError:
            logger.debug("yfinance 未安装，跳过 yfinance 数据源")
            return []

        yf_sym = _to_yfinance_symbol(code)
        interval = self._yf_interval(timeframe)
        if not interval:
            return []

        days = self._yf_days(timeframe, limit)
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

            rows = self._parse_yfinance_df(df)
            if rows:
                self._cb.record_success(source_key)
            else:
                self._cb.record_failure(source_key, "解析后无数据")
            return rows

        except Exception as e:
            self._cb.record_failure(source_key, str(e))
            logger.warning(f"yfinance A 股 K 线失败 {yf_sym}: {e}")
            return []

    @staticmethod
    def _parse_yfinance_df(df) -> List[Dict[str, Any]]:
        """解析 yfinance DataFrame"""
        df = df.reset_index()
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
                if hasattr(tv, "timestamp"):
                    ts = int(tv.timestamp())
                else:
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
        return rows

    # ==================================================================
    # 腾讯行情数据源
    # ==================================================================

    def _fetch_tencent(self, code: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
        """通过腾讯 Web API 获取 A 股日/周线 K 线"""
        source_key = "tencent_cn_kline"
        if not self._cb.is_available(source_key):
            return []

        get_tencent_limiter().wait()

        period = "day" if timeframe == "1D" else "week"
        # 确定腾讯代码前缀（sh / sz）
        prefix = "sh" if code.startswith("6") else "sz"
        url = (
            f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={prefix}{code},{period},,,{limit},qfq"
        )

        try:
            resp = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "http://finance.qq.com",
            })
            resp.raise_for_status()
            data = resp.json()

            # 解析返回数据
            kline_data = (data.get("data") or {}).get(f"{prefix}{code}") or {}
            bars = kline_data.get(period) or kline_data.get(f"qfq{period}") or []
            if not bars:
                self._cb.record_failure(source_key, "空数据")
                return []

            rows: List[Dict[str, Any]] = []
            for bar in bars:
                try:
                    # bar 格式: ["2024-01-02", "100.00", "102.00", "99.50", "101.00", "12345"]
                    if len(bar) < 6:
                        continue
                    date_str = bar[0]
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    # 日线时间戳使用 CST 午夜
                    ts = int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
                    rows.append(self.format_kline(
                        ts, float(bar[1]), float(bar[3]),
                        float(bar[4]), float(bar[2]), float(bar[5]),
                    ))
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
            logger.debug(f"腾讯 A 股 K 线失败 {code}: {e}")
            return []

    # ==================================================================
    # 实时行情获取
    # ==================================================================

    def _fetch_tencent_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """腾讯行情实时报价"""
        prefix = "sh" if code.startswith("6") else "sz"
        url = f"http://qt.gtimg.cn/q=s_{prefix}{code}"
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            text = resp.text
            # 解析: v_sh600519="1~贵州茅台~600519~1800.00~..."
            parts = text.split('"')
            if len(parts) < 2:
                return None
            fields = parts[1].split("~")
            if len(fields) < 10:
                return None
            last = float(fields[3]) if fields[3] else 0
            prev_close = float(fields[4]) if fields[4] else 0
            change = last - prev_close if prev_close else 0
            change_pct = (change / prev_close * 100) if prev_close else 0
            return {
                "last": last,
                "change": round(change, 4),
                "changePercent": round(change_pct, 2),
                "high": float(fields[5]) if len(fields) > 5 and fields[5] else 0,
                "low": float(fields[6]) if len(fields) > 6 and fields[6] else 0,
                "open": float(fields[7]) if len(fields) > 7 and fields[7] else 0,
                "previousClose": prev_close,
                "name": fields[1] if len(fields) > 1 else "",
                "symbol": code,
            }
        except Exception as e:
            logger.debug(f"腾讯 A 股报价解析失败 {code}: {e}")
            return None

    def _fetch_akshare_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """AKShare 实时报价（降级方案）"""
        try:
            import akshare as ak
            with _bypass_proxy():
                df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return None
            # 查找匹配行
            row = df[df["代码"] == code]
            if row.empty:
                return None
            r = row.iloc[0]
            last = float(r.get("最新价", 0) or 0)
            prev = float(r.get("昨收", 0) or 0)
            change = last - prev if prev else 0
            return {
                "last": last,
                "change": round(change, 4),
                "changePercent": round((change / prev * 100) if prev else 0, 2),
                "high": float(r.get("最高", 0) or 0),
                "low": float(r.get("最低", 0) or 0),
                "open": float(r.get("今开", 0) or 0),
                "previousClose": prev,
                "name": str(r.get("名称", "")),
                "symbol": code,
            }
        except Exception as e:
            logger.debug(f"AKShare A 股报价失败 {code}: {e}")
            return None

    # ==================================================================
    # 辅助方法
    # ==================================================================

    @staticmethod
    def _normalize_timeframe(tf: str) -> str:
        """标准化时间周期字符串"""
        aliases = {
            "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W",
            "d": "1D", "w": "1W", "day": "1D", "week": "1W",
        }
        t = (tf or "1D").strip()
        return aliases.get(t.lower(), t)

    @staticmethod
    def _yf_interval(timeframe: str) -> Optional[str]:
        """timeframe → yfinance interval"""
        return {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1H": "1h", "4H": "1h", "1D": "1d", "1W": "1wk",
        }.get(timeframe)

    @staticmethod
    def _yf_days(timeframe: str, limit: int) -> int:
        """根据 timeframe 和 limit 计算 yfinance period 天数"""
        m = {
            "1m": lambda n: min(7, max(2, (n // 240) + 2)),
            "5m": lambda n: min(60, max(3, (n // 48) + 3)),
            "15m": lambda n: min(60, max(3, (n // 16) + 3)),
            "30m": lambda n: min(60, max(5, (n // 8) + 5)),
            "1H": lambda n: min(730, max(8, (n // 4) + 8)),
            "4H": lambda n: min(730, max(20, n + 10)),
            "1D": lambda n: min(3650, n + 10),
            "1W": lambda n: min(3650, n * 7 + 30),
        }
        fn = m.get(timeframe, lambda n: n + 10)
        return fn(limit)
