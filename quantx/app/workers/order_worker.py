"""
QuantX 异步订单处理 Worker

在独立后台线程中处理订单队列：
- 从内存队列取订单
- 通过对应的交易适配器执行
- 更新数据库订单状态
- 错误重试（最多 3 次，指数退避）

参考 QuantDinger pending_order_worker.py 精简实现。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services.adapters import create_adapter
from app.utils.db import get_connection, fetch_one
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 最大重试次数
MAX_RETRIES = 3

# 初始退避时间（秒）
INITIAL_BACKOFF = 1.0

# Worker 循环间隔（秒）
POLL_INTERVAL = 0.5


def _gen_worker_order_id() -> str:
    """生成 Worker 内部订单 ID"""
    return f"W-{uuid.uuid4().hex[:10].upper()}"


class OrderWorker:
    """异步订单处理 Worker

    单例模式，在服务启动时创建并启动。

    用法::

        worker = OrderWorker()
        worker.start()
        worker.submit({
            "strategy_id": 1,
            "symbol": "AAPL",
            "market": "USStock",
            "side": "buy",
            "quantity": 10,
            "adapter_type": "alpaca",
        })
        # ... 稍后
        worker.stop()
    """

    def __init__(self):
        # 订单队列（线程安全的 deque）
        self._queue: deque = deque()
        self._queue_lock = threading.Lock()

        # 运行标志
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 适配器缓存: {(strategy_id, market): adapter}
        self._adapters: Dict[tuple, Any] = {}
        self._adapter_lock = threading.Lock()

        # 统计
        self._processed_count = 0
        self._error_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动 Worker 后台线程"""
        if self._running:
            logger.warning("OrderWorker 已在运行中")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._process_loop,
            name="order-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("OrderWorker 已启动")

    def stop(self) -> None:
        """停止 Worker"""
        if not self._running:
            return

        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info(
            f"OrderWorker 已停止: processed={self._processed_count}, "
            f"errors={self._error_count}"
        )

    @property
    def is_running(self) -> bool:
        """Worker 是否正在运行"""
        return self._running

    @property
    def queue_size(self) -> int:
        """当前队列中的订单数量"""
        return len(self._queue)

    # ------------------------------------------------------------------
    # 订单提交
    # ------------------------------------------------------------------

    def submit(self, order: Dict[str, Any]) -> str:
        """提交订单到处理队列

        Args:
            order: 订单字典，需包含:
                - strategy_id: 策略 ID
                - symbol: 品种代码
                - market: 市场类型
                - side: "buy" 或 "sell"
                - quantity: 数量
                - price: 限价（可选）

        Returns:
            Worker 内部订单 ID
        """
        worker_order_id = _gen_worker_order_id()
        order["worker_order_id"] = worker_order_id
        order["submitted_at"] = time.time()

        with self._queue_lock:
            self._queue.append(order)

        logger.info(
            f"订单已提交到队列: worker_id={worker_order_id}, "
            f"strategy={order.get('strategy_id')}, "
            f"{order.get('side')} {order.get('symbol')} x{order.get('quantity')}"
        )
        return worker_order_id

    # ------------------------------------------------------------------
    # 处理循环
    # ------------------------------------------------------------------

    def _process_loop(self) -> None:
        """后台处理循环：从队列取订单并执行"""
        logger.info("OrderWorker 处理循环开始")

        while self._running:
            order = None

            # 从队列取订单
            with self._queue_lock:
                if self._queue:
                    order = self._queue.popleft()

            if order is None:
                # 队列为空，等待
                time.sleep(POLL_INTERVAL)
                continue

            # 处理订单（带重试）
            self._process_order(order)

        logger.info("OrderWorker 处理循环结束")

    def _process_order(self, order: Dict[str, Any]) -> None:
        """处理单个订单（带重试逻辑）

        Args:
            order: 订单字典
        """
        worker_id = order.get("worker_order_id", "")
        strategy_id = order.get("strategy_id")
        symbol = order.get("symbol", "")
        market = order.get("market", "")
        side = order.get("side", "")
        quantity = order.get("quantity", 0)
        price = order.get("price")

        retries = order.get("retries", 0)
        backoff = INITIAL_BACKOFF * (2 ** retries)

        try:
            # 获取或创建适配器
            adapter = self._get_adapter(strategy_id, market)
            if not adapter:
                raise RuntimeError(f"无法获取交易适配器 (strategy={strategy_id}, market={market})")

            # 更新最新价格（模拟适配器需要）
            if hasattr(adapter, "update_price") and price:
                adapter.update_price(symbol, price)

            # 执行订单
            result = adapter.submit_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
            )

            status = result.get("status", "")
            if status in ("filled", "simulated", "submitted", "new"):
                # 成功
                self._processed_count += 1
                self._update_order_db(
                    worker_id=worker_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    market=market,
                    side=side,
                    quantity=quantity,
                    price=price,
                    result=result,
                    status="filled",
                )
                logger.info(
                    f"订单处理成功: worker_id={worker_id}, "
                    f"{side} {symbol} x{quantity}, status={status}"
                )
            elif status == "rejected":
                # 被拒绝（不重试）
                reason = result.get("reason", "未知")
                self._error_count += 1
                self._update_order_db(
                    worker_id=worker_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    market=market,
                    side=side,
                    quantity=quantity,
                    price=price,
                    result=result,
                    status="rejected",
                    error_msg=reason,
                )
                logger.warning(
                    f"订单被拒绝: worker_id={worker_id}, "
                    f"{side} {symbol} x{quantity}, 原因: {reason}"
                )
            else:
                # 其他状态，可能需要重试
                raise RuntimeError(f"订单状态异常: {status}")

        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"订单处理失败 (retry={retries}): worker_id={worker_id}, "
                f"{side} {symbol} x{quantity}, 错误: {error_msg}"
            )

            # 重试逻辑
            if retries < MAX_RETRIES:
                order["retries"] = retries + 1
                logger.info(
                    f"订单将重试 ({retries + 1}/{MAX_RETRIES})，"
                    f"退避 {backoff:.1f}s: worker_id={worker_id}"
                )
                time.sleep(backoff)
                with self._queue_lock:
                    self._queue.append(order)
            else:
                # 达到最大重试次数
                self._error_count += 1
                self._update_order_db(
                    worker_id=worker_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    market=market,
                    side=side,
                    quantity=quantity,
                    price=price,
                    result={},
                    status="failed",
                    error_msg=f"重试 {MAX_RETRIES} 次后仍失败: {error_msg}",
                )
                logger.error(
                    f"订单最终失败: worker_id={worker_id}, "
                    f"重试 {MAX_RETRIES} 次后放弃"
                )

    # ------------------------------------------------------------------
    # 适配器管理
    # ------------------------------------------------------------------

    def _get_adapter(self, strategy_id: int, market: str) -> Any:
        """获取或创建交易适配器

        Args:
            strategy_id: 策略 ID
            market: 市场类型

        Returns:
            适配器实例
        """
        key = (strategy_id, market)
        with self._adapter_lock:
            if key in self._adapters:
                return self._adapters[key]

        # 创建新适配器
        try:
            adapter = create_adapter(
                market=market,
                strategy_id=strategy_id,
                paper=True,
                initial_capital=100000.0,
            )
            with self._adapter_lock:
                self._adapters[key] = adapter
            return adapter
        except Exception as e:
            logger.error(f"创建适配器失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 数据库操作
    # ------------------------------------------------------------------

    def _update_order_db(
        self,
        worker_id: str,
        strategy_id: int,
        symbol: str,
        market: str,
        side: str,
        quantity: float,
        price: Optional[float],
        result: Dict[str, Any],
        status: str,
        error_msg: str = "",
    ) -> None:
        """更新订单状态到数据库"""
        try:
            order_id = result.get("order_id", result.get("order_ref", worker_id))
            adapter_type = "unknown"

            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO qd_orders
                        (order_id, strategy_id, symbol, market, side, quantity,
                         price, status, adapter_type, filled_price, fee, pnl,
                         error_msg, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (order_id)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        filled_price = EXCLUDED.filled_price,
                        fee = EXCLUDED.fee,
                        pnl = EXCLUDED.pnl,
                        error_msg = EXCLUDED.error_msg,
                        updated_at = NOW()
                    """,
                    (
                        order_id,
                        strategy_id,
                        symbol,
                        market,
                        side,
                        quantity,
                        price or 0,
                        status,
                        adapter_type,
                        result.get("fill_price", 0),
                        result.get("fee", 0),
                        result.get("pnl", 0),
                        error_msg,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"更新订单数据库失败: {e}")

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """获取 Worker 统计信息

        Returns:
            {"running": bool, "queue_size": int, "processed": int, "errors": int}
        """
        return {
            "running": self._running,
            "queue_size": self.queue_size,
            "processed": self._processed_count,
            "errors": self._error_count,
        }
