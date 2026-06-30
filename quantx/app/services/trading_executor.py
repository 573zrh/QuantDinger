"""
QuantX 交易执行引擎

管理策略的实盘/模拟交易执行：
- 每个策略一个 daemon thread
- tick_interval 默认 10 秒循环
- 信号去重：相同 symbol+signal 在 60 秒内不重复执行
- 异常处理：策略线程异常时自动更新状态为 error
- 适配器模式：根据 market 自动选择 PaperAdapter / AlpacaAdapter / CTPAdapter

参考 QuantDinger trading_executor.py 精简实现。
"""
from __future__ import annotations

import json
import os
import time
import threading
import traceback
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from app.data_sources.factory import DataSourceFactory
from app.markets.rules import get_trading_rules
from app.markets.trading_calendar import trading_calendar
from app.services.adapters import create_adapter
from app.services.strategy import get_strategy, update_strategy_status
from app.utils.db import get_connection, fetch_one, fetch_all
from app.utils.logger import get_logger
from app.utils.safe_exec import safe_exec_code

logger = get_logger(__name__)

# K 线周期 → 秒数映射
TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "4H": 14400, "1D": 86400, "1W": 604800,
}

# 默认 tick 间隔（秒）
DEFAULT_TICK_INTERVAL = 10

# 信号去重窗口（秒）
SIGNAL_DEDUP_WINDOW = 60

# 最大策略线程数
MAX_STRATEGY_THREADS = int(os.getenv("STRATEGY_MAX_THREADS", "64"))


class TradingExecutor:
    """交易执行引擎

    管理多个策略的并发交易执行。每个策略在独立的 daemon thread 中运行。

    用法::

        executor = TradingExecutor()
        executor.start_strategy(1)   # 启动策略 ID=1
        status = executor.get_strategy_status(1)
        executor.stop_strategy(1)    # 停止策略
    """

    def __init__(self):
        # 运行中的策略线程: {strategy_id: Thread}
        self.running_strategies: Dict[int, threading.Thread] = {}

        # 停止信号: {strategy_id: threading.Event}
        self._stop_events: Dict[int, threading.Event] = {}

        # 信号去重缓存: {(strategy_id, symbol, signal): last_executed_timestamp}
        self._signal_dedup: Dict[str, float] = {}
        self._dedup_lock = threading.Lock()

        # 策略状态: {strategy_id: {"status": str, "last_tick": float, "error": str, ...}}
        self._strategy_states: Dict[str, Dict[str, Any]] = {}
        self._state_lock = threading.Lock()

        # 交易适配器缓存: {strategy_id: adapter_instance}
        self._adapters: Dict[int, Any] = {}
        self._adapter_lock = threading.Lock()

        # 全局锁
        self._lock = threading.Lock()

        logger.info("交易执行引擎初始化完成")

    # ------------------------------------------------------------------
    # 策略启停
    # ------------------------------------------------------------------

    def start_strategy(self, strategy_id: int) -> bool:
        """启动策略（创建执行线程）

        Args:
            strategy_id: 策略 ID

        Returns:
            是否启动成功
        """
        with self._lock:
            # 检查线程数上限
            if len(self.running_strategies) >= MAX_STRATEGY_THREADS:
                logger.error(
                    f"策略线程数已达上限 ({MAX_STRATEGY_THREADS})，无法启动策略 {strategy_id}"
                )
                return False

            # 检查是否已在运行
            if strategy_id in self.running_strategies:
                thread = self.running_strategies[strategy_id]
                if thread.is_alive():
                    logger.warning(f"策略 {strategy_id} 已在运行中")
                    return False
                # 清理死线程
                del self.running_strategies[strategy_id]

            # 加载策略配置
            strategy = get_strategy(strategy_id)
            if not strategy:
                logger.error(f"策略 {strategy_id} 不存在")
                return False

            market = strategy.get("market", "")
            if market not in ("CNStock", "HKStock", "USStock", "CNFutures"):
                logger.error(f"策略 {strategy_id} 市场类型无效: {market}")
                return False

            # 创建交易适配器
            try:
                adapter = create_adapter(
                    market=market,
                    strategy_id=strategy_id,
                    paper=True,  # MVP 默认模拟
                    initial_capital=100000.0,
                )
            except Exception as e:
                logger.error(f"创建适配器失败 (strategy={strategy_id}): {e}")
                return False

            with self._adapter_lock:
                self._adapters[strategy_id] = adapter

            # 更新策略状态为 running
            try:
                update_strategy_status(strategy_id, "running")
            except Exception as e:
                logger.warning(f"更新策略状态失败 (可能已在 running): {e}")

            # 创建停止事件
            stop_event = threading.Event()
            self._stop_events[strategy_id] = stop_event

            # 初始化状态
            with self._state_lock:
                self._strategy_states[str(strategy_id)] = {
                    "status": "running",
                    "started_at": time.time(),
                    "last_tick": 0,
                    "tick_count": 0,
                    "signal_count": 0,
                    "order_count": 0,
                    "error": "",
                    "market": market,
                    "symbol": strategy.get("symbol", ""),
                }

            # 创建并启动 daemon 线程
            thread = threading.Thread(
                target=self._strategy_loop,
                args=(strategy_id,),
                name=f"strategy-{strategy_id}",
                daemon=True,
            )
            self.running_strategies[strategy_id] = thread
            thread.start()

            logger.info(
                f"策略 {strategy_id} 已启动: market={market}, "
                f"symbol={strategy.get('symbol')}, thread={thread.name}"
            )
            return True

    def stop_strategy(self, strategy_id: int) -> bool:
        """停止策略

        Args:
            strategy_id: 策略 ID

        Returns:
            是否停止成功
        """
        with self._lock:
            if strategy_id not in self.running_strategies:
                logger.warning(f"策略 {strategy_id} 未在运行")
                return False

            # 发送停止信号
            stop_event = self._stop_events.get(strategy_id)
            if stop_event:
                stop_event.set()

            # 等待线程结束（最多 5 秒）
            thread = self.running_strategies.get(strategy_id)
            if thread and thread.is_alive():
                thread.join(timeout=5)

            # 清理资源
            self.running_strategies.pop(strategy_id, None)
            self._stop_events.pop(strategy_id, None)

            with self._adapter_lock:
                adapter = self._adapters.pop(strategy_id, None)
                # CTP 适配器需要断开连接
                if adapter and hasattr(adapter, "disconnect"):
                    try:
                        adapter.disconnect()
                    except Exception as e:
                        logger.warning(f"断开适配器连接失败: {e}")

            # 更新数据库状态
            try:
                update_strategy_status(strategy_id, "stopped")
            except Exception as e:
                logger.warning(f"更新策略状态失败: {e}")

            # 更新内存状态
            with self._state_lock:
                state = self._strategy_states.get(str(strategy_id), {})
                state["status"] = "stopped"

            logger.info(f"策略 {strategy_id} 已停止")
            return True

    # ------------------------------------------------------------------
    # 策略执行主循环
    # ------------------------------------------------------------------

    def _strategy_loop(self, strategy_id: int) -> None:
        """策略执行主循环（每个策略一个 daemon thread）

        流程：
        1. 加载策略配置（indicator_code, params, symbol, timeframe）
        2. 获取交易规则
        3. 循环：
           a. 获取最新 K 线数据
           b. 在沙箱中执行 indicator_code 生成信号
           c. 信号去重
           d. 通过适配器执行交易
           e. 更新仓位和盈亏
           f. sleep(tick_interval)
        """
        stop_event = self._stop_events.get(strategy_id)
        tick_interval = DEFAULT_TICK_INTERVAL

        try:
            # 1. 加载策略配置
            strategy = get_strategy(strategy_id)
            if not strategy:
                self._set_error(strategy_id, "策略不存在")
                return

            symbol = strategy.get("symbol", "")
            market = strategy.get("market", "")
            timeframe = strategy.get("timeframe", "1D")
            indicator_code = strategy.get("indicator_code", "")
            params = strategy.get("params", {})
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except (json.JSONDecodeError, TypeError):
                    params = {}

            if not indicator_code:
                self._set_error(strategy_id, "策略无 indicator_code，无法生成信号")
                return

            # 2. 获取交易规则和适配器
            rules = get_trading_rules(market)
            adapter = self._adapters.get(strategy_id)
            if not adapter:
                self._set_error(strategy_id, "交易适配器未创建")
                return

            tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 86400)

            logger.info(
                f"策略循环开始: id={strategy_id}, symbol={symbol}, "
                f"market={market}, timeframe={timeframe}"
            )

            # 3. 主循环
            while not (stop_event and stop_event.is_set()):
                try:
                    tick_start = time.time()

                    # 检查交易日
                    today = datetime.now(tz=timezone.utc).date()
                    if not trading_calendar.is_trading_day(market, today):
                        # 非交易日，等待下一个 tick
                        self._update_state(strategy_id, last_tick=tick_start, status="idle")
                        stop_event.wait(timeout=tick_interval)
                        continue

                    # a. 获取最新 K 线
                    klines = DataSourceFactory.get_kline(
                        market=market,
                        symbol=symbol,
                        timeframe=timeframe,
                        limit=100,
                    )
                    if not klines:
                        logger.warning(f"策略 {strategy_id}: K 线数据为空")
                        stop_event.wait(timeout=tick_interval)
                        continue

                    # 更新最新价格到适配器（供模拟交易使用）
                    latest_price = float(klines[-1].get("close", 0))
                    if latest_price > 0 and hasattr(adapter, "update_price"):
                        adapter.update_price(symbol, latest_price)

                    # b. 执行 indicator_code 生成信号
                    signals = self._generate_signals(klines, indicator_code, params)

                    # c. 信号去重 + d. 执行交易
                    if signals:
                        latest_signal = signals[-1]
                        signal_type = latest_signal.get("signal", "")

                        if signal_type in ("buy", "sell"):
                            # 去重检查
                            dedup_key = f"{strategy_id}:{symbol}:{signal_type}"
                            if self._is_dedup(dedup_key):
                                logger.debug(
                                    f"策略 {strategy_id}: 信号去重 {signal_type} "
                                    f"(60秒内已执行)"
                                )
                            else:
                                # 执行交易
                                self._execute_signal(
                                    strategy_id=strategy_id,
                                    adapter=adapter,
                                    rules=rules,
                                    symbol=symbol,
                                    market=market,
                                    signal=latest_signal,
                                    price=latest_price,
                                )
                                self._mark_dedup(dedup_key)

                    # e. 更新状态
                    self._update_state(
                        strategy_id,
                        last_tick=time.time(),
                        tick_count=self._get_state_val(strategy_id, "tick_count", 0) + 1,
                    )

                    # f. 等待下一个 tick
                    elapsed = time.time() - tick_start
                    sleep_time = max(0, tick_interval - elapsed)
                    if sleep_time > 0 and stop_event:
                        stop_event.wait(timeout=sleep_time)

                except Exception as e:
                    logger.error(
                        f"策略 {strategy_id} tick 异常: {e}\n{traceback.format_exc()}"
                    )
                    # 短暂等待后重试
                    if stop_event:
                        stop_event.wait(timeout=tick_interval)

        except Exception as e:
            error_msg = f"策略线程异常退出: {e}\n{traceback.format_exc()}"
            logger.error(f"策略 {strategy_id}: {error_msg}")
            self._set_error(strategy_id, error_msg)
        finally:
            # 清理
            with self._lock:
                self.running_strategies.pop(strategy_id, None)
                self._stop_events.pop(strategy_id, None)

            logger.info(f"策略 {strategy_id} 循环结束")

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

        只返回最新 K 线对应的信号（用于实时执行）。

        Args:
            klines: K 线数据
            indicator_code: 策略指标代码
            params: 策略参数

        Returns:
            信号列表
        """
        try:
            df = pd.DataFrame(klines)
            if df.empty:
                return []

            context = {
                "df": df,
                "klines": df,
                "params": params,
                "pd": pd,
                "np": np,
            }

            result = safe_exec_code(code=indicator_code, context=context, timeout=15)

            if not result["success"]:
                logger.warning(f"indicator_code 执行失败: {result['error']}")
                return []

            raw = result.get("result")
            if raw is None:
                return []

            # 标准化信号
            signals = self._normalize_signals(raw, klines)

            # 只保留最新信号
            if signals:
                return [signals[-1]]
            return []

        except Exception as e:
            logger.error(f"信号生成异常: {e}")
            return []

    def _normalize_signals(
        self, raw: Any, klines: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """标准化信号格式"""
        signals = []

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    sig = str(item.get("signal", "hold")).lower()
                    if sig in ("buy", "sell"):
                        signals.append({
                            "time": int(item.get("time", 0)),
                            "signal": sig,
                            "strength": float(item.get("strength", 1.0)),
                        })

        elif isinstance(raw, pd.Series):
            for idx, val in raw.items():
                if val and str(val).lower() in ("buy", "sell"):
                    signals.append({
                        "time": int(idx) if isinstance(idx, (int, float)) else 0,
                        "signal": str(val).lower(),
                        "strength": 1.0,
                    })

        elif isinstance(raw, dict) and "signal" in raw:
            sig = str(raw["signal"]).lower()
            if sig in ("buy", "sell"):
                signals.append({
                    "time": int(raw.get("time", 0)),
                    "signal": sig,
                    "strength": float(raw.get("strength", 1.0)),
                })

        return signals

    # ------------------------------------------------------------------
    # 交易执行
    # ------------------------------------------------------------------

    def _execute_signal(
        self,
        strategy_id: int,
        adapter: Any,
        rules: Any,
        symbol: str,
        market: str,
        signal: Dict[str, Any],
        price: float,
    ) -> None:
        """执行交易信号

        Args:
            strategy_id: 策略 ID
            adapter: 交易适配器
            rules: 交易规则
            symbol: 品种代码
            market: 市场类型
            signal: 信号数据
            price: 当前价格
        """
        side = signal["signal"]  # "buy" 或 "sell"
        strength = signal.get("strength", 1.0)

        if price <= 0:
            logger.warning(f"策略 {strategy_id}: 无法获取有效价格，跳过信号 {side}")
            return

        # 计算交易数量
        try:
            portfolio_value = adapter.get_portfolio_value()
        except Exception:
            portfolio_value = 100000.0

        # 根据信号强度调整仓位比例（strength 0~1）
        allocation = min(strength, 1.0) * 0.95  # 最多使用 95% 资金

        if market == "CNFutures":
            quantity = rules.calculate_position_size(
                portfolio_value * allocation, price, symbol=symbol
            )
        else:
            quantity = rules.calculate_position_size(
                portfolio_value * allocation, price
            )

        if quantity <= 0:
            logger.debug(f"策略 {strategy_id}: 计算数量为 0，跳过")
            return

        # 提交订单
        try:
            result = adapter.submit_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
            )

            status = result.get("status", "")
            if status in ("filled", "simulated", "submitted", "new"):
                logger.info(
                    f"策略 {strategy_id} 交易执行成功: {side} {symbol} x{quantity} "
                    f"@{price:.4f}, status={status}"
                )
                # 更新信号计数
                with self._state_lock:
                    state = self._strategy_states.get(str(strategy_id), {})
                    state["order_count"] = state.get("order_count", 0) + 1
                    state["signal_count"] = state.get("signal_count", 0) + 1

                # 持久化订单记录到数据库
                self._persist_order(strategy_id, symbol, market, side, quantity, price, result)
            else:
                reason = result.get("reason", "未知原因")
                logger.warning(
                    f"策略 {strategy_id} 交易被拒: {side} {symbol} x{quantity}, "
                    f"原因: {reason}"
                )

        except Exception as e:
            logger.error(f"策略 {strategy_id} 交易执行异常: {e}")

    # ------------------------------------------------------------------
    # 信号去重
    # ------------------------------------------------------------------

    def _is_dedup(self, key: str) -> bool:
        """检查信号是否在去重窗口内"""
        with self._dedup_lock:
            last_ts = self._signal_dedup.get(key, 0)
            return (time.time() - last_ts) < SIGNAL_DEDUP_WINDOW

    def _mark_dedup(self, key: str) -> None:
        """标记信号已执行"""
        with self._dedup_lock:
            self._signal_dedup[key] = time.time()

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def _update_state(self, strategy_id: int, **kwargs) -> None:
        """更新策略状态"""
        with self._state_lock:
            state = self._strategy_states.setdefault(str(strategy_id), {})
            state.update(kwargs)

    def _get_state_val(self, strategy_id: int, key: str, default=None):
        """获取状态值"""
        with self._state_lock:
            state = self._strategy_states.get(str(strategy_id), {})
            return state.get(key, default)

    def _set_error(self, strategy_id: int, error_msg: str) -> None:
        """设置策略错误状态"""
        logger.error(f"策略 {strategy_id} 错误: {error_msg}")
        with self._state_lock:
            state = self._strategy_states.setdefault(str(strategy_id), {})
            state["status"] = "error"
            state["error"] = error_msg

        # 更新数据库
        try:
            update_strategy_status(strategy_id, "error")
        except Exception as e:
            logger.warning(f"更新错误状态失败: {e}")

    def get_strategy_status(self, strategy_id: int) -> Dict[str, Any]:
        """获取策略运行状态

        Args:
            strategy_id: 策略 ID

        Returns:
            状态信息字典
        """
        with self._state_lock:
            state = self._strategy_states.get(str(strategy_id), {})

        # 检查线程是否存活
        thread = self.running_strategies.get(strategy_id)
        is_alive = thread.is_alive() if thread else False

        # 获取适配器的持仓和盈亏信息
        adapter = self._adapters.get(strategy_id)
        positions = []
        pnl = {}
        if adapter:
            try:
                positions = adapter.get_positions()
            except Exception:
                pass
            try:
                if hasattr(adapter, "get_pnl"):
                    pnl = adapter.get_pnl()
            except Exception:
                pass

        return {
            "strategy_id": strategy_id,
            "status": state.get("status", "unknown"),
            "is_alive": is_alive,
            "started_at": state.get("started_at"),
            "last_tick": state.get("last_tick"),
            "tick_count": state.get("tick_count", 0),
            "signal_count": state.get("signal_count", 0),
            "order_count": state.get("order_count", 0),
            "error": state.get("error", ""),
            "market": state.get("market", ""),
            "symbol": state.get("symbol", ""),
            "positions": positions,
            "pnl": pnl,
        }

    def get_all_status(self) -> Dict[str, Any]:
        """获取所有策略状态

        Returns:
            {strategy_id: status_dict, ...}
        """
        result = {}
        # 合并运行中的和已有状态的策略
        all_ids = set()
        all_ids.update(self.running_strategies.keys())
        with self._state_lock:
            all_ids.update(int(k) for k in self._strategy_states.keys())

        for sid in all_ids:
            result[str(sid)] = self.get_strategy_status(sid)

        return result

    # ------------------------------------------------------------------
    # 数据库持久化
    # ------------------------------------------------------------------

    def _persist_order(
        self,
        strategy_id: int,
        symbol: str,
        market: str,
        side: str,
        quantity: float,
        price: float,
        result: Dict[str, Any],
    ) -> None:
        """将订单记录持久化到数据库"""
        try:
            adapter = self._adapters.get(strategy_id)
            adapter_type = getattr(adapter, "adapter_type", "unknown") if adapter else "unknown"

            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO qd_orders
                        (order_id, strategy_id, symbol, market, side, quantity,
                         price, status, adapter_type, filled_price, fee, pnl,
                         error_msg, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (
                        result.get("order_id", result.get("order_ref", "")),
                        strategy_id,
                        symbol,
                        market,
                        side,
                        quantity,
                        price,
                        result.get("status", ""),
                        adapter_type,
                        result.get("fill_price", 0),
                        result.get("fee", 0),
                        result.get("pnl", 0),
                        result.get("reason", ""),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"订单持久化失败（不影响交易）: {e}")

    # ------------------------------------------------------------------
    # 恢复运行
    # ------------------------------------------------------------------

    def restore_running_strategies(self) -> int:
        """恢复数据库中标记为 running 的策略

        在服务重启时调用，自动恢复之前运行的策略。

        Returns:
            恢复的策略数量
        """
        try:
            running = fetch_all(
                "SELECT id, name, market, symbol FROM qd_strategies WHERE status = 'running'"
            )
            if not running:
                logger.info("无需恢复的策略")
                return 0

            restored = 0
            for s in running:
                sid = s.get("id")
                if sid:
                    logger.info(f"恢复策略: id={sid}, name={s.get('name')}")
                    if self.start_strategy(sid):
                        restored += 1
                    else:
                        # 启动失败，标记为 error
                        try:
                            update_strategy_status(sid, "error")
                        except Exception:
                            pass

            logger.info(f"策略恢复完成: {restored}/{len(running)} 成功")
            return restored

        except Exception as e:
            logger.error(f"恢复策略失败: {e}")
            return 0

    def stop_all(self) -> int:
        """停止所有运行中的策略

        Returns:
            停止的策略数量
        """
        strategy_ids = list(self.running_strategies.keys())
        stopped = 0
        for sid in strategy_ids:
            if self.stop_strategy(sid):
                stopped += 1
        logger.info(f"已停止 {stopped} 个策略")
        return stopped
