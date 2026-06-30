"""
QuantX 回测引擎

支持四个市场（A 股、港股、中国期货、美股）的向量化回测框架。
参考 QuantDinger backtest.py 架构，适配传统市场规则。

核心流程：
1. 获取历史 K 线数据
2. 在安全沙箱中执行策略 indicator_code 生成信号
3. 按信号模拟交易（应用市场规则：T+1、涨跌停、费用等）
4. 计算绩效指标（Sharpe、MaxDrawdown、WinRate 等）
5. 结果持久化到数据库
"""
from __future__ import annotations

import json
import time as _time
import traceback
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.data_sources.factory import DataSourceFactory
from app.markets.rules import get_trading_rules, TradingRules, CNFuturesRules
from app.markets.trading_calendar import trading_calendar
from app.utils.db import get_connection, fetch_one, fetch_all
from app.utils.logger import get_logger
from app.utils.safe_exec import safe_exec_code

logger = get_logger(__name__)

# K 线周期 → 秒数映射
TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "4H": 14400, "1D": 86400, "1W": 604800,
}

# 无风险利率（年化 3%，适用于中国市场环境）
RISK_FREE_RATE = 0.03


class BacktestEngine:
    """回测引擎

    使用方法::

        engine = BacktestEngine()
        result = engine.run(
            strategy_id=1,
            market="CNStock",
            symbol="600519",
            timeframe="1D",
            start_date="2023-01-01",
            end_date="2024-01-01",
            initial_capital=100000,
        )
    """

    ENGINE_VERSION = "backtest-v1"

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(
        self,
        strategy_id: int,
        market: str,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        initial_capital: float = 100000.0,
        params: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行回测

        Args:
            strategy_id: 策略 ID（用于获取 indicator_code）
            market: 市场类型
            symbol: 品种代码
            timeframe: K 线周期
            start_date: 起始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
            initial_capital: 初始资金
            params: 策略参数覆盖
            user_id: 用户 ID

        Returns:
            回测结果字典
        """
        run_id: Optional[int] = None
        start_ts = _time.time()

        try:
            # 1. 创建回测运行记录
            run_id = self._create_run_record(
                strategy_id=strategy_id,
                user_id=user_id,
                market=market,
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                params=params,
            )

            # 2. 获取策略 indicator_code
            indicator_code = self._get_indicator_code(strategy_id)
            if not indicator_code:
                raise ValueError("策略无 indicator_code，无法生成信号")

            # 3. 获取 K 线数据
            klines = self._fetch_data(market, symbol, timeframe, start_date, end_date)
            if not klines or len(klines) < 2:
                raise ValueError(f"K 线数据不足（仅 {len(klines or [])} 根），无法回测")

            # 4. 生成交易信号
            signals = self._generate_signals(klines, indicator_code, params or {})
            if not signals:
                raise ValueError("信号生成失败或无有效信号")

            # 5. 模拟交易
            rules = get_trading_rules(market)
            sim_result = self._simulate_trading(
                klines=klines,
                signals=signals,
                rules=rules,
                market=market,
                symbol=symbol,
                initial_capital=initial_capital,
            )

            # 6. 计算绩效
            performance = self._calculate_performance(
                equity_curve=sim_result["equity_curve"],
                trades=sim_result["trades"],
                initial_capital=initial_capital,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
            )

            elapsed = round(_time.time() - start_ts, 2)
            performance["elapsed_seconds"] = elapsed

            # 7. 持久化结果
            self._save_results(
                run_id=run_id,
                performance=performance,
                trades=sim_result["trades"],
                equity_curve=sim_result["equity_curve"],
                final_capital=sim_result["final_capital"],
            )

            return {
                "run_id": run_id,
                "status": "completed",
                "performance": performance,
                "trades": sim_result["trades"],
                "equity_curve": sim_result["equity_curve"],
            }

        except Exception as e:
            logger.error(f"回测执行失败: {e}\n{traceback.format_exc()}")
            # 更新失败状态
            if run_id:
                self._update_run_failed(run_id, str(e))
            return {
                "run_id": run_id,
                "status": "failed",
                "error": str(e),
                "performance": {},
                "trades": [],
                "equity_curve": [],
            }

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------

    def _fetch_data(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> List[Dict[str, Any]]:
        """获取回测 K 线数据

        通过 DataSourceFactory.get_kline() 获取，使用 after_time/before_time 限定范围。
        回测需要完整数据窗口，不做截断。
        """
        # 将日期字符串转为 Unix 时间戳
        dt_start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt_end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        after_time = int(dt_start.timestamp())
        before_time = int(dt_end.timestamp()) + 86400  # 包含 end_date 当天

        # 估算需要的 K 线数量（留足缓冲）
        days = (dt_end - dt_start).days
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 86400)
        estimated_bars = int(days * 86400 / tf_seconds * 1.5) + 100
        limit = max(estimated_bars, 1000)

        klines = DataSourceFactory.get_kline(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            before_time=before_time,
            after_time=after_time,
        )

        logger.info(
            f"回测数据获取: {market}:{symbol} {timeframe} "
            f"{start_date}~{end_date} → {len(klines)} 根K线"
        )
        return klines

    # ------------------------------------------------------------------
    # 信号生成
    # ------------------------------------------------------------------

    def _generate_signals(
        self,
        klines: List[Dict[str, Any]],
        indicator_code: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """在安全沙箱中执行 indicator_code 生成交易信号

        将 K 线数据转为 pandas DataFrame 传入沙箱环境，
        期望 indicator_code 设置 ``result`` 变量为信号列表。

        信号格式::

            [{"time": int, "signal": "buy"|"sell"|"hold", "strength": float}]
        """
        # 构建 DataFrame 作为沙箱输入
        df = pd.DataFrame(klines)
        if df.empty:
            return []

        # 准备沙箱上下文
        context = {
            "df": df,
            "klines": df,  # 别名
            "params": params,
            "pd": pd,
            "np": np,
        }

        # 执行 indicator_code
        exec_result = safe_exec_code(
            code=indicator_code,
            context=context,
            timeout=30,
        )

        if not exec_result["success"]:
            raise ValueError(f"indicator_code 执行失败: {exec_result['error']}")

        raw_signals = exec_result.get("result")
        if raw_signals is None:
            raise ValueError("indicator_code 未设置 result 变量")

        # 标准化信号格式
        signals = self._normalize_signals(raw_signals, klines)
        logger.info(
            f"信号生成完成: {len(signals)} 个信号 "
            f"(buy={sum(1 for s in signals if s['signal']=='buy')}, "
            f"sell={sum(1 for s in signals if s['signal']=='sell')})"
        )
        return signals

    def _normalize_signals(
        self,
        raw: Any,
        klines: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """将 indicator_code 的输出标准化为信号列表

        支持多种返回格式：
        1. list of dict: [{"time": ..., "signal": "buy", ...}, ...]
        2. pandas Series: index=time, value="buy"/"sell"/"hold"
        3. pandas DataFrame: columns=["time", "signal"]
        4. dict with "buy"/"sell" Series (布尔型)
        """
        signals: List[Dict[str, Any]] = []

        if isinstance(raw, list):
            # 格式 1: list of dict
            for item in raw:
                if isinstance(item, dict):
                    signals.append({
                        "time": int(item.get("time", 0)),
                        "signal": str(item.get("signal", "hold")).lower(),
                        "strength": float(item.get("strength", 1.0)),
                    })

        elif isinstance(raw, pd.Series):
            # 格式 2: Series（index → time, value → signal）
            for idx, val in raw.items():
                if val and str(val).lower() in ("buy", "sell"):
                    signals.append({
                        "time": int(idx) if isinstance(idx, (int, float)) else 0,
                        "signal": str(val).lower(),
                        "strength": 1.0,
                    })

        elif isinstance(raw, pd.DataFrame):
            # 格式 3: DataFrame
            for _, row in raw.iterrows():
                sig = str(row.get("signal", "hold")).lower()
                if sig in ("buy", "sell"):
                    signals.append({
                        "time": int(row.get("time", 0)),
                        "signal": sig,
                        "strength": float(row.get("strength", 1.0)),
                    })

        elif isinstance(raw, dict) and "buy" in raw and "sell" in raw:
            # 格式 4: dict with buy/sell Series
            buy_series = raw.get("buy")
            sell_series = raw.get("sell")
            kline_times = {k["time"]: k for k in klines}

            if isinstance(buy_series, pd.Series):
                for idx, val in buy_series.items():
                    if val:
                        t = int(idx) if isinstance(idx, (int, float)) else 0
                        signals.append({"time": t, "signal": "buy", "strength": 1.0})

            if isinstance(sell_series, pd.Series):
                for idx, val in sell_series.items():
                    if val:
                        t = int(idx) if isinstance(idx, (int, float)) else 0
                        signals.append({"time": t, "signal": "sell", "strength": 1.0})

            # 按时间排序
            signals.sort(key=lambda s: s["time"])

        return signals

    # ------------------------------------------------------------------
    # 交易模拟
    # ------------------------------------------------------------------

    def _simulate_trading(
        self,
        klines: List[Dict[str, Any]],
        signals: List[Dict[str, Any]],
        rules: TradingRules,
        market: str,
        symbol: str,
        initial_capital: float,
    ) -> Dict[str, Any]:
        """模拟交易执行

        遍历 K 线 + 信号，按市场规则执行买卖。

        Args:
            klines: K 线数据列表
            signals: 信号列表
            rules: 交易规则实例
            market: 市场类型
            symbol: 品种代码
            initial_capital: 初始资金

        Returns:
            {"trades": [...], "equity_curve": [...], "final_capital": float}
        """
        # 建立时间 → 信号的映射
        signal_map: Dict[int, Dict[str, Any]] = {}
        for sig in signals:
            signal_map[sig["time"]] = sig

        capital = initial_capital
        position: Optional[Dict[str, Any]] = None  # 当前持仓
        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []
        prev_close = 0.0  # 前收盘价（用于涨跌停判断）
        total_fees = 0.0

        for i, bar in enumerate(klines):
            bar_time = int(bar["time"])
            bar_open = float(bar["open"])
            bar_close = float(bar["close"])
            bar_date = datetime.fromtimestamp(bar_time, tz=timezone.utc).date()

            # 当前 bar 使用的价格：信号在 close 触发，用 close 价执行
            exec_price = bar_close

            # 检查交易日历：非交易日跳过（不生成信号，但权益按收盘价更新）
            if not trading_calendar.is_trading_day(market, bar_date):
                # 更新权益（持仓市值按收盘价）
                equity = capital
                if position:
                    if rules.market == "CNFutures":
                        multiplier = getattr(rules, '_get_contract_multiplier', lambda s: 10)(symbol)
                        equity = capital + position["quantity"] * (bar_close - position["entry_price"]) * multiplier
                    else:
                        equity = capital + position["quantity"] * bar_close
                equity_curve.append({
                    "time": bar_time,
                    "equity": round(equity, 2),
                    "drawdown": 0,  # 稍后计算
                })
                continue

            # 检查涨跌停（只在有前收盘价时检查）
            price_limit = rules.check_price_limit(symbol, exec_price, prev_close) if prev_close > 0 else {
                "is_limit_up": False, "is_limit_down": False,
                "upper_limit": float("inf"), "lower_limit": 0,
            }

            # 获取当前 bar 的信号
            sig = signal_map.get(bar_time)

            if sig:
                action = sig["signal"]

                # ---- 买入信号 ----
                if action == "buy" and position is None:
                    # 检查涨跌停：涨停不可买入
                    if price_limit.get("is_limit_up", False):
                        logger.debug(f"涨停不可买入: time={bar_time}, price={exec_price}")
                    else:
                        # 计算可买数量
                        if rules.market == "CNFutures":
                            quantity = rules.calculate_position_size(
                                capital, exec_price, symbol=symbol
                            )
                        else:
                            quantity = rules.calculate_position_size(capital, exec_price)

                        if quantity > 0:
                            # 计算费用
                            if rules.market == "CNFutures" and isinstance(rules, CNFuturesRules):
                                fee_info = rules.calculate_fee_for_symbol(
                                    symbol, "buy", quantity, exec_price
                                )
                            else:
                                fee_info = rules.calculate_fee("buy", quantity, exec_price)

                            fee = fee_info["total_fee"]

                            # 计算实际持仓成本
                            if rules.market == "CNFutures":
                                multiplier = rules._get_contract_multiplier(symbol)
                                margin = rules.get_margin_required(symbol, quantity, exec_price)
                                cost = margin + fee
                            else:
                                cost = quantity * exec_price + fee

                            if cost <= capital:
                                capital -= cost
                                position = {
                                    "entry_price": exec_price,
                                    "entry_time": bar_time,
                                    "quantity": quantity,
                                    "side": "long",
                                    "opened_date": bar_date,
                                    "fee_paid": fee,
                                }
                                total_fees += fee

                # ---- 卖出信号 ----
                elif action == "sell" and position is not None:
                    # 检查 T+1 等规则
                    can_sell = rules.can_sell(position, bar, bar_date)
                    # 检查跌停不可卖出
                    if price_limit.get("is_limit_down", False):
                        can_sell = False
                        logger.debug(f"跌停不可卖出: time={bar_time}, price={exec_price}")

                    if can_sell:
                        # 计算费用
                        if rules.market == "CNFutures" and isinstance(rules, CNFuturesRules):
                            fee_info = rules.calculate_fee_for_symbol(
                                symbol, "sell", position["quantity"], exec_price
                            )
                        else:
                            fee_info = rules.calculate_fee(
                                "sell", position["quantity"], exec_price
                            )
                        fee = fee_info["total_fee"]

                        # 计算盈亏
                        if rules.market == "CNFutures":
                            multiplier = rules._get_contract_multiplier(symbol)
                            pnl = position["quantity"] * (exec_price - position["entry_price"]) * multiplier - fee
                            # 归还保证金
                            margin = rules.get_margin_required(
                                symbol, position["quantity"], position["entry_price"]
                            )
                            capital += margin + pnl
                        else:
                            proceeds = position["quantity"] * exec_price
                            pnl = proceeds - position["quantity"] * position["entry_price"] - fee
                            capital += proceeds - fee

                        total_fees += fee

                        # 记录交易
                        trades.append({
                            "entry_time": position["entry_time"],
                            "exit_time": bar_time,
                            "side": "long",
                            "quantity": position["quantity"],
                            "entry_price": position["entry_price"],
                            "exit_price": exec_price,
                            "pnl": round(pnl, 2),
                            "fee": round(fee + position.get("fee_paid", 0), 2),
                        })
                        position = None

            # 更新权益曲线
            equity = capital
            if position:
                if rules.market == "CNFutures":
                    multiplier = rules._get_contract_multiplier(symbol)
                    unrealized = position["quantity"] * (bar_close - position["entry_price"]) * multiplier
                    equity = capital + unrealized
                else:
                    equity = capital + position["quantity"] * bar_close

            equity_curve.append({
                "time": bar_time,
                "equity": round(equity, 2),
                "drawdown": 0,  # 稍后统一计算
            })

            # 更新前收盘价
            prev_close = bar_close

        # 如果回测结束仍有持仓，强制平仓
        if position and klines:
            last_bar = klines[-1]
            last_price = float(last_bar["close"])
            last_time = int(last_bar["time"])
            last_date = datetime.fromtimestamp(last_time, tz=timezone.utc).date()

            if rules.market == "CNFutures" and isinstance(rules, CNFuturesRules):
                fee_info = rules.calculate_fee_for_symbol(
                    symbol, "sell", position["quantity"], last_price
                )
                fee = fee_info["total_fee"]
                multiplier = rules._get_contract_multiplier(symbol)
                pnl = position["quantity"] * (last_price - position["entry_price"]) * multiplier - fee
                margin = rules.get_margin_required(
                    symbol, position["quantity"], position["entry_price"]
                )
                capital += margin + pnl
            else:
                fee_info = rules.calculate_fee("sell", position["quantity"], last_price)
                fee = fee_info["total_fee"]
                proceeds = position["quantity"] * last_price
                pnl = proceeds - position["quantity"] * position["entry_price"] - fee
                capital += proceeds - fee

            total_fees += fee
            trades.append({
                "entry_time": position["entry_time"],
                "exit_time": last_time,
                "side": "long",
                "quantity": position["quantity"],
                "entry_price": position["entry_price"],
                "exit_price": last_price,
                "pnl": round(pnl, 2),
                "fee": round(fee + position.get("fee_paid", 0), 2),
            })
            position = None

        # 计算回撤
        self._compute_drawdown(equity_curve)

        final_capital = equity_curve[-1]["equity"] if equity_curve else initial_capital

        logger.info(
            f"交易模拟完成: {len(trades)} 笔交易, "
            f"总手续费={total_fees:.2f}, 最终资金={final_capital:.2f}"
        )

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "final_capital": final_capital,
            "total_fees": total_fees,
        }

    @staticmethod
    def _compute_drawdown(equity_curve: List[Dict[str, Any]]) -> None:
        """原地计算权益曲线的回撤序列（就地修改 drawdown 字段）"""
        if not equity_curve:
            return
        peak = equity_curve[0]["equity"]
        for point in equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            point["drawdown"] = round(-dd, 4)

    # ------------------------------------------------------------------
    # 绩效计算
    # ------------------------------------------------------------------

    def _calculate_performance(
        self,
        equity_curve: List[Dict[str, Any]],
        trades: List[Dict[str, Any]],
        initial_capital: float,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> Dict[str, Any]:
        """计算绩效指标

        返回:
            total_return, annual_return, max_drawdown, sharpe_ratio,
            win_rate, profit_factor, total_trades, avg_profit, avg_loss,
            total_fees
        """
        if not equity_curve:
            return {}

        final_value = equity_curve[-1]["equity"]
        total_return = (final_value - initial_capital) / initial_capital * 100

        # 年化收益率（简单线性年化）
        try:
            actual_start = datetime.fromtimestamp(
                equity_curve[0]["time"], tz=timezone.utc
            )
            actual_end = datetime.fromtimestamp(
                equity_curve[-1]["time"], tz=timezone.utc
            )
            actual_days = (actual_end - actual_start).total_seconds() / 86400
        except (KeyError, ValueError, IndexError):
            dt_s = datetime.strptime(start_date, "%Y-%m-%d")
            dt_e = datetime.strptime(end_date, "%Y-%m-%d")
            actual_days = (dt_e - dt_s).total_seconds() / 86400

        years = actual_days / 365.0
        annual_return = total_return / years if years > 0 else 0

        # 最大回撤
        values = [e["equity"] for e in equity_curve]
        max_drawdown = self._calc_max_drawdown(values)

        # 夏普比率
        sharpe = self._calc_sharpe(values, timeframe)

        # 交易统计
        win_trades = [t for t in trades if t["pnl"] > 0]
        loss_trades = [t for t in trades if t["pnl"] < 0]
        even_trades = [t for t in trades if t["pnl"] == 0]
        total_trades = len(trades)
        win_rate = len(win_trades) / total_trades * 100 if total_trades > 0 else 0

        # 盈亏比
        total_wins = sum(t["pnl"] for t in win_trades)
        total_losses = abs(sum(t["pnl"] for t in loss_trades))
        profit_factor = (
            total_wins / total_losses if total_losses > 0
            else (total_wins if total_wins > 0 else 0)
        )

        # 平均盈亏
        avg_profit = total_wins / len(win_trades) if win_trades else 0
        avg_loss = total_losses / len(loss_trades) if loss_trades else 0

        # 总手续费
        total_fees = sum(t.get("fee", 0) for t in trades)

        return {
            "total_return": round(total_return, 2),
            "annual_return": round(annual_return, 2),
            "max_drawdown": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 2),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "total_trades": total_trades,
            "win_trades": len(win_trades),
            "loss_trades": len(loss_trades),
            "even_trades": len(even_trades),
            "avg_profit": round(avg_profit, 2),
            "avg_loss": round(avg_loss, 2),
            "total_profit": round(final_value - initial_capital, 2),
            "total_fees": round(total_fees, 2),
            "initial_capital": initial_capital,
            "final_capital": round(final_value, 2),
        }

    @staticmethod
    def _calc_max_drawdown(values: List[float]) -> float:
        """计算最大回撤百分比（返回负值，如 -15.3 表示 15.3% 回撤）"""
        if not values:
            return 0
        peak = values[0]
        max_dd = 0
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        return -max_dd

    @staticmethod
    def _calc_sharpe(
        values: List[float],
        timeframe: str = "1D",
        risk_free_rate: float = RISK_FREE_RATE,
    ) -> float:
        """计算夏普比率

        Sharpe = (年化收益 - 无风险利率) / 年化波动率
        """
        if len(values) < 2:
            return 0

        # 过滤零值
        valid = [v for v in values if v > 0]
        if len(valid) < 2:
            return 0

        # 年化因子
        ann_factor = {
            "1m": 252 * 24 * 60,
            "5m": 252 * 24 * 12,
            "15m": 252 * 24 * 4,
            "30m": 252 * 24 * 2,
            "1H": 252 * 24,
            "4H": 252 * 6,
            "1D": 252,
            "1W": 52,
        }.get(timeframe, 252)

        try:
            arr = np.array(valid, dtype=np.float64)
            returns = np.diff(arr) / arr[:-1]

            # 过滤异常值
            returns = returns[np.isfinite(returns)]
            if len(returns) == 0:
                return 0

            avg_ret = np.mean(returns) * ann_factor
            std_ret = np.std(returns) * np.sqrt(ann_factor)

            if std_ret == 0 or not np.isfinite(std_ret):
                return 0

            return (avg_ret - risk_free_rate) / std_ret

        except Exception as e:
            logger.warning(f"Sharpe 计算异常: {e}")
            return 0

    # ------------------------------------------------------------------
    # 数据库操作
    # ------------------------------------------------------------------

    def _get_indicator_code(self, strategy_id: int) -> str:
        """从数据库获取策略的 indicator_code"""
        row = fetch_one(
            "SELECT indicator_code FROM qd_strategies WHERE id = %s",
            (strategy_id,),
        )
        if not row:
            raise ValueError(f"策略 {strategy_id} 不存在")
        return row.get("indicator_code", "") or ""

    def _create_run_record(
        self,
        strategy_id: int,
        user_id: Optional[int],
        market: str,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
        initial_capital: float,
        params: Optional[Dict[str, Any]],
    ) -> int:
        """创建回测运行记录，返回 run_id"""
        params_json = json.dumps(params or {})
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO qd_backtest_runs
                        (strategy_id, user_id, market, symbol, timeframe,
                         start_date, end_date, initial_capital, status, params, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running', %s::jsonb, NOW())
                    RETURNING id
                    """,
                    (strategy_id, user_id, market, symbol, timeframe,
                     start_date, end_date, initial_capital, params_json),
                )
                row = cursor.fetchone()
                conn.commit()
                if row:
                    return row[0]
                raise RuntimeError("INSERT 未返回 id")
        except Exception as e:
            logger.error(f"创建回测记录失败: {e}")
            raise

    def _update_run_failed(self, run_id: int, error_msg: str) -> None:
        """更新回测运行记录为失败状态"""
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE qd_backtest_runs
                    SET status = 'failed',
                        result = %s::jsonb,
                        completed_at = NOW()
                    WHERE id = %s
                    """,
                    (json.dumps({"error": error_msg}), run_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"更新回测失败状态出错 (run_id={run_id}): {e}")

    def _save_results(
        self,
        run_id: int,
        performance: Dict[str, Any],
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        final_capital: float,
    ) -> None:
        """将回测结果持久化到数据库"""
        try:
            with get_connection() as conn:
                cursor = conn.cursor()

                # 更新运行记录
                cursor.execute(
                    """
                    UPDATE qd_backtest_runs
                    SET status = 'completed',
                        final_capital = %s,
                        total_return = %s,
                        max_drawdown = %s,
                        sharpe_ratio = %s,
                        win_rate = %s,
                        total_trades = %s,
                        result = %s::jsonb,
                        completed_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        final_capital,
                        performance.get("total_return"),
                        performance.get("max_drawdown"),
                        performance.get("sharpe_ratio"),
                        performance.get("win_rate"),
                        performance.get("total_trades"),
                        json.dumps(performance),
                        run_id,
                    ),
                )

                # 批量插入交易记录
                if trades:
                    trade_values = []
                    for t in trades:
                        entry_ts = datetime.fromtimestamp(
                            t["entry_time"], tz=timezone.utc
                        )
                        exit_ts = datetime.fromtimestamp(
                            t["exit_time"], tz=timezone.utc
                        )
                        trade_values.append((
                            run_id,
                            entry_ts,
                            exit_ts,
                            t.get("side", "long"),
                            t["quantity"],
                            t["entry_price"],
                            t["exit_price"],
                            t["pnl"],
                            t.get("fee", 0),
                        ))
                    cursor.executemany(
                        """
                        INSERT INTO qd_backtest_trades
                            (run_id, entry_time, exit_time, side, quantity,
                             entry_price, exit_price, pnl, fee)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        trade_values,
                    )

                # 批量插入权益曲线（采样：最多保留 1000 个点）
                if equity_curve:
                    step = max(1, len(equity_curve) // 1000)
                    sampled = equity_curve[::step]
                    # 确保最后一个点也被包含
                    if sampled[-1] is not equity_curve[-1]:
                        sampled.append(equity_curve[-1])

                    equity_values = []
                    for e in sampled:
                        ts = datetime.fromtimestamp(
                            e["time"], tz=timezone.utc
                        )
                        equity_values.append((
                            run_id,
                            ts,
                            e["equity"],
                            e.get("drawdown", 0),
                        ))
                    cursor.executemany(
                        """
                        INSERT INTO qd_backtest_equity
                            (run_id, time, equity, drawdown)
                        VALUES (%s, %s, %s, %s)
                        """,
                        equity_values,
                    )

                conn.commit()
                logger.info(
                    f"回测结果已保存: run_id={run_id}, "
                    f"trades={len(trades)}, equity_points={len(equity_curve)}"
                )

        except Exception as e:
            logger.error(f"保存回测结果失败 (run_id={run_id}): {e}")
            raise

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    @staticmethod
    def get_run(run_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """获取回测运行记录"""
        if user_id is not None:
            row = fetch_one(
                "SELECT * FROM qd_backtest_runs WHERE id = %s AND user_id = %s",
                (run_id, user_id),
            )
        else:
            row = fetch_one(
                "SELECT * FROM qd_backtest_runs WHERE id = %s",
                (run_id,),
            )
        if row:
            # 序列化 datetime 字段
            for key in ("start_date", "end_date", "created_at", "completed_at"):
                if row.get(key):
                    row[key] = str(row[key])
        return row

    @staticmethod
    def list_runs(
        user_id: int,
        strategy_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """获取回测历史列表"""
        conditions = ["user_id = %s"]
        params: list = [user_id]

        if strategy_id is not None:
            conditions.append("strategy_id = %s")
            params.append(strategy_id)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT id, strategy_id, market, symbol, timeframe,
                   start_date, end_date, initial_capital, final_capital,
                   total_return, max_drawdown, sharpe_ratio, win_rate,
                   total_trades, status, created_at, completed_at
            FROM qd_backtest_runs
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT %s
        """
        params.append(limit)

        rows = fetch_all(sql, tuple(params))
        for row in rows:
            for key in ("start_date", "end_date", "created_at", "completed_at"):
                if row.get(key):
                    row[key] = str(row[key])
        return rows

    @staticmethod
    def get_trades(run_id: int) -> List[Dict[str, Any]]:
        """获取回测交易记录"""
        rows = fetch_all(
            "SELECT * FROM qd_backtest_trades WHERE run_id = %s ORDER BY entry_time",
            (run_id,),
        )
        for row in rows:
            for key in ("entry_time", "exit_time"):
                if row.get(key):
                    row[key] = str(row[key])
        return rows

    @staticmethod
    def get_equity_curve(run_id: int) -> List[Dict[str, Any]]:
        """获取回测权益曲线"""
        rows = fetch_all(
            "SELECT * FROM qd_backtest_equity WHERE run_id = %s ORDER BY time",
            (run_id,),
        )
        for row in rows:
            if row.get("time"):
                row["time"] = str(row["time"])
        return rows

    @staticmethod
    def delete_run(run_id: int, user_id: int) -> bool:
        """删除回测记录（级联删除交易和权益曲线）"""
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                # 先检查权限
                cursor.execute(
                    "SELECT id FROM qd_backtest_runs WHERE id = %s AND user_id = %s",
                    (run_id, user_id),
                )
                if not cursor.fetchone():
                    return False
                cursor.execute(
                    "DELETE FROM qd_backtest_runs WHERE id = %s AND user_id = %s",
                    (run_id, user_id),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"删除回测记录失败 (run_id={run_id}): {e}")
            return False
