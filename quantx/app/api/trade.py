"""
QuantX 交易管理 API 路由

使用 flask-smorest Blueprint 提供交易相关接口：
- POST   /api/trade/order              提交订单
- GET    /api/trade/positions          获取持仓列表
- GET    /api/trade/portfolio          获取组合概览
- GET    /api/trade/orders             订单历史
- POST   /api/trade/strategy/<id>/start 启动策略交易
- POST   /api/trade/strategy/<id>/stop  停止策略交易
"""
from __future__ import annotations

from flask import g, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from app.utils.auth import require_auth
from app.utils.logger import get_logger

logger = get_logger(__name__)

blp = Blueprint("trade", __name__, url_prefix="/api/trade", description="交易管理")


# =============================================================================
# Marshmallow Schema 定义
# =============================================================================

class OrderSubmitSchema(Schema):
    """提交订单请求体"""
    strategy_id = fields.Int(
        required=True,
        metadata={"description": "策略 ID"},
    )
    symbol = fields.Str(
        required=True,
        metadata={"description": "品种代码（如 600519, AAPL, rb2405）"},
    )
    market = fields.Str(
        required=True,
        validate=validate.OneOf(["CNStock", "HKStock", "USStock", "CNFutures"]),
        metadata={"description": "市场类型"},
    )
    side = fields.Str(
        required=True,
        validate=validate.OneOf(["buy", "sell"]),
        metadata={"description": "方向: buy 或 sell"},
    )
    quantity = fields.Float(
        required=True,
        metadata={"description": "数量（股/手）"},
    )
    price = fields.Float(
        load_default=None,
        metadata={"description": "限价（不传则为市价）"},
    )


class OrderResponseSchema(Schema):
    """订单响应"""
    order_id = fields.Str(metadata={"description": "订单 ID"})
    status = fields.Str(metadata={"description": "订单状态"})
    reason = fields.Str(metadata={"description": "原因（拒绝时）"})
    fill_price = fields.Float(metadata={"description": "成交价格"})
    fee = fields.Float(metadata={"description": "手续费"})
    pnl = fields.Float(metadata={"description": "盈亏"})


class PositionSchema(Schema):
    """持仓"""
    symbol = fields.Str()
    side = fields.Str()
    quantity = fields.Float()
    avg_price = fields.Float()
    latest_price = fields.Float()
    market_value = fields.Float()
    unrealized_pnl = fields.Float()


class PortfolioSchema(Schema):
    """组合概览"""
    total_value = fields.Float(metadata={"description": "组合总值"})
    capital = fields.Float(metadata={"description": "可用现金"})
    market_value = fields.Float(metadata={"description": "持仓市值"})
    total_pnl = fields.Float(metadata={"description": "总盈亏"})
    total_pnl_pct = fields.Float(metadata={"description": "总盈亏百分比"})
    realized_pnl = fields.Float(metadata={"description": "已实现盈亏"})
    unrealized_pnl = fields.Float(metadata={"description": "未实现盈亏"})
    total_fees = fields.Float(metadata={"description": "总手续费"})


class StrategyStatusSchema(Schema):
    """策略运行状态"""
    strategy_id = fields.Int()
    status = fields.Str()
    is_alive = fields.Bool()
    tick_count = fields.Int()
    signal_count = fields.Int()
    order_count = fields.Int()
    error = fields.Str()


# =============================================================================
# 辅助函数
# =============================================================================

def _get_executor():
    """获取交易执行引擎单例"""
    from app.startup import get_trading_executor
    return get_trading_executor()


def _get_order_worker():
    """获取订单 Worker 单例"""
    from app.startup import get_order_worker
    return get_order_worker()


# =============================================================================
# 路由：提交订单
# =============================================================================

@blp.route("/order")
class OrderView(MethodView):
    """提交订单"""

    @blp.arguments(OrderSubmitSchema)
    @blp.response(200, OrderResponseSchema)
    @require_auth
    def post(self, args):
        """提交订单（根据 market 自动选择适配器）

        订单通过 OrderWorker 异步处理，立即返回 worker_order_id。
        """
        strategy_id = args["strategy_id"]
        symbol = args["symbol"]
        market = args["market"]
        side = args["side"]
        quantity = args["quantity"]
        price = args.get("price")

        # 提交到异步 Worker
        worker = _get_order_worker()
        if not worker or not worker.is_running:
            abort(503, message="订单处理服务不可用")

        worker_id = worker.submit({
            "strategy_id": strategy_id,
            "symbol": symbol,
            "market": market,
            "side": side,
            "quantity": quantity,
            "price": price,
        })

        return {
            "order_id": worker_id,
            "status": "queued",
            "reason": "",
            "fill_price": 0,
            "fee": 0,
            "pnl": 0,
        }


# =============================================================================
# 路由：持仓列表
# =============================================================================

@blp.route("/positions")
class PositionsView(MethodView):
    """获取持仓列表"""

    @blp.response(200, schema=fields.List(fields.Nested(PositionSchema)))
    @require_auth
    def get(self):
        """获取当前持仓列表

        支持 strategy_id 查询参数过滤。
        """
        strategy_id = request.args.get("strategy_id", type=int)
        executor = _get_executor()

        if strategy_id:
            status = executor.get_strategy_status(strategy_id)
            return status.get("positions", [])
        else:
            # 获取所有策略的持仓
            all_status = executor.get_all_status()
            positions = []
            for sid, state in all_status.items():
                for pos in state.get("positions", []):
                    pos["strategy_id"] = int(sid)
                    positions.append(pos)
            return positions


# =============================================================================
# 路由：组合概览
# =============================================================================

@blp.route("/portfolio")
class PortfolioView(MethodView):
    """获取组合概览"""

    @blp.response(200, PortfolioSchema)
    @require_auth
    def get(self):
        """获取组合概览（总值、现金、持仓市值、盈亏）"""
        strategy_id = request.args.get("strategy_id", type=int)
        executor = _get_executor()

        if strategy_id:
            status = executor.get_strategy_status(strategy_id)
            return status.get("pnl", {
                "total_value": 0,
                "capital": 0,
                "market_value": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "total_fees": 0,
            })
        else:
            # 汇总所有策略
            all_status = executor.get_all_status()
            total_value = 0
            total_capital = 0
            total_market_value = 0
            total_pnl = 0
            total_fees = 0

            for sid, state in all_status.items():
                pnl = state.get("pnl", {})
                total_value += pnl.get("total_value", 0)
                total_capital += pnl.get("capital", 0)
                total_market_value += pnl.get("market_value", 0)
                total_pnl += pnl.get("total_pnl", 0)
                total_fees += pnl.get("total_fees", 0)

            return {
                "total_value": round(total_value, 2),
                "capital": round(total_capital, 2),
                "market_value": round(total_market_value, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": 0,  # 无法直接汇总百分比
                "realized_pnl": 0,
                "unrealized_pnl": round(total_pnl, 2),
                "total_fees": round(total_fees, 2),
            }


# =============================================================================
# 路由：订单历史
# =============================================================================

@blp.route("/orders")
class OrdersView(MethodView):
    """获取订单历史"""

    @require_auth
    def get(self):
        """获取订单历史（从数据库查询）

        支持查询参数：
        - strategy_id: 按策略过滤
        - limit: 返回数量（默认 50，最大 200）
        """
        strategy_id = request.args.get("strategy_id", type=int)
        limit = min(request.args.get("limit", 50, type=int), 200)

        try:
            from app.utils.db import fetch_all

            if strategy_id:
                rows = fetch_all(
                    """
                    SELECT order_id, strategy_id, symbol, market, side, quantity,
                           price, status, adapter_type, filled_price, fee, pnl,
                           error_msg, created_at, updated_at
                    FROM qd_orders
                    WHERE strategy_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (strategy_id, limit),
                )
            else:
                rows = fetch_all(
                    """
                    SELECT order_id, strategy_id, symbol, market, side, quantity,
                           price, status, adapter_type, filled_price, fee, pnl,
                           error_msg, created_at, updated_at
                    FROM qd_orders
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )

            # 序列化 datetime 字段
            for row in rows:
                for key in ("created_at", "updated_at"):
                    if row.get(key):
                        row[key] = str(row[key])

            return {"code": 200, "data": rows}

        except Exception as e:
            logger.error(f"获取订单历史失败: {e}")
            return {"code": 500, "msg": f"获取订单历史失败: {e}", "data": []}


# =============================================================================
# 路由：启动策略交易
# =============================================================================

@blp.route("/strategy/<int:strategy_id>/start")
class StrategyStartView(MethodView):
    """启动策略交易"""

    @require_auth
    def post(self, strategy_id: int):
        """启动指定策略的交易执行

        策略将在独立线程中运行，按 indicator_code 生成信号并自动执行交易。
        """
        # 验证策略归属
        from app.services.strategy import get_strategy
        strategy = get_strategy(strategy_id, user_id=g.user_id)
        if not strategy:
            abort(404, message="策略不存在或无权访问")

        executor = _get_executor()
        success = executor.start_strategy(strategy_id)

        if success:
            return {
                "code": 200,
                "msg": f"策略 {strategy_id} 已启动",
                "data": executor.get_strategy_status(strategy_id),
            }
        else:
            return {
                "code": 400,
                "msg": f"策略 {strategy_id} 启动失败",
                "data": None,
            }


# =============================================================================
# 路由：停止策略交易
# =============================================================================

@blp.route("/strategy/<int:strategy_id>/stop")
class StrategyStopView(MethodView):
    """停止策略交易"""

    @require_auth
    def post(self, strategy_id: int):
        """停止指定策略的交易执行"""
        # 验证策略归属
        from app.services.strategy import get_strategy
        strategy = get_strategy(strategy_id, user_id=g.user_id)
        if not strategy:
            abort(404, message="策略不存在或无权访问")

        executor = _get_executor()
        success = executor.stop_strategy(strategy_id)

        if success:
            return {
                "code": 200,
                "msg": f"策略 {strategy_id} 已停止",
                "data": None,
            }
        else:
            return {
                "code": 400,
                "msg": f"策略 {strategy_id} 未运行或停止失败",
                "data": None,
            }


# =============================================================================
# 路由：策略状态
# =============================================================================

@blp.route("/strategy/<int:strategy_id>/status")
class StrategyStatusView(MethodView):
    """获取策略运行状态"""

    @blp.response(200, StrategyStatusSchema)
    @require_auth
    def get(self, strategy_id: int):
        """获取指定策略的运行状态"""
        executor = _get_executor()
        return executor.get_strategy_status(strategy_id)


# =============================================================================
# 路由：Worker 状态
# =============================================================================

@blp.route("/worker/status")
class WorkerStatusView(MethodView):
    """获取订单 Worker 状态"""

    @require_auth
    def get(self):
        """获取 OrderWorker 的运行状态和统计"""
        worker = _get_order_worker()
        if not worker:
            return {"code": 200, "data": {"running": False}}
        return {"code": 200, "data": worker.get_stats()}
