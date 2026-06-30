"""
QuantX 回测 API 路由

使用 flask-smorest Blueprint 提供回测相关接口：
- POST /api/backtest/run       执行回测
- GET  /api/backtest/<run_id>  获取回测结果
- GET  /api/backtest/list      回测历史列表
- DELETE /api/backtest/<run_id> 删除回测记录
"""
from __future__ import annotations

from flask import g, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from app.services.backtest import BacktestEngine
from app.utils.auth import require_auth
from app.utils.logger import get_logger

logger = get_logger(__name__)

blp = Blueprint("backtest", __name__, url_prefix="/api/backtest", description="回测引擎")

# 全局回测引擎实例
_engine = BacktestEngine()


# =============================================================================
# Marshmallow Schema 定义
# =============================================================================

class BacktestRunSchema(Schema):
    """执行回测请求体"""
    strategy_id = fields.Int(
        required=True,
        metadata={"description": "策略 ID"},
    )
    market = fields.Str(
        required=True,
        validate=validate.OneOf(["CNStock", "HKStock", "USStock", "CNFutures"]),
        metadata={"description": "市场类型"},
    )
    symbol = fields.Str(
        required=True,
        metadata={"description": "品种代码"},
    )
    timeframe = fields.Str(
        load_default="1D",
        metadata={"description": "K 线周期（如 1D, 1H, 5m）"},
    )
    start_date = fields.Str(
        required=True,
        metadata={"description": "起始日期（YYYY-MM-DD）"},
    )
    end_date = fields.Str(
        required=True,
        metadata={"description": "结束日期（YYYY-MM-DD）"},
    )
    initial_capital = fields.Float(
        load_default=100000.0,
        metadata={"description": "初始资金"},
    )
    params = fields.Dict(
        load_default=dict,
        metadata={"description": "策略参数覆盖"},
    )


class BacktestResponseSchema(Schema):
    """回测响应"""
    code = fields.Int()
    msg = fields.Str()
    data = fields.Raw()


# =============================================================================
# 路由
# =============================================================================

@blp.route("/run")
class BacktestRunResource(MethodView):
    @blp.arguments(BacktestRunSchema)
    @blp.response(200, BacktestResponseSchema)
    @require_auth
    def post(self, args):
        """执行回测（需认证）

        根据策略的 indicator_code 生成信号，在指定市场模拟交易，
        返回完整的绩效指标、交易记录和权益曲线。
        """
        # 日期格式校验
        import re
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not date_pattern.match(args["start_date"]):
            abort(400, message="start_date 格式无效，应为 YYYY-MM-DD")
        if not date_pattern.match(args["end_date"]):
            abort(400, message="end_date 格式无效，应为 YYYY-MM-DD")

        if args["start_date"] >= args["end_date"]:
            abort(400, message="start_date 必须早于 end_date")

        try:
            result = _engine.run(
                strategy_id=args["strategy_id"],
                market=args["market"],
                symbol=args["symbol"],
                timeframe=args.get("timeframe", "1D"),
                start_date=args["start_date"],
                end_date=args["end_date"],
                initial_capital=args.get("initial_capital", 100000),
                params=args.get("params"),
                user_id=g.user_id,
            )
        except ValueError as e:
            abort(400, message=str(e))
        except Exception as e:
            logger.error(f"回测执行异常: {e}", exc_info=True)
            abort(500, message=f"回测执行失败: {e}")

        if result.get("status") == "failed":
            return {
                "code": 0,
                "msg": result.get("error", "回测失败"),
                "data": {"status": "failed", "run_id": result.get("run_id")},
            }

        return {
            "code": 1,
            "msg": "回测完成",
            "data": result,
        }


@blp.route("/<int:run_id>")
class BacktestDetailResource(MethodView):
    @blp.response(200, BacktestResponseSchema)
    @require_auth
    def get(self, run_id):
        """获取回测结果详情（需认证）"""
        run = BacktestEngine.get_run(run_id, user_id=g.user_id)
        if not run:
            abort(404, message="回测记录不存在或无权访问")

        trades = BacktestEngine.get_trades(run_id)
        equity_curve = BacktestEngine.get_equity_curve(run_id)

        return {
            "code": 1,
            "msg": "success",
            "data": {
                "run": run,
                "trades": trades,
                "equity_curve": equity_curve,
            },
        }

    @blp.response(200, BacktestResponseSchema)
    @require_auth
    def delete(self, run_id):
        """删除回测记录（需认证，级联删除交易和权益曲线）"""
        ok = BacktestEngine.delete_run(run_id, user_id=g.user_id)
        if not ok:
            abort(404, message="回测记录不存在或无权访问")

        return {
            "code": 1,
            "msg": "回测记录已删除",
            "data": None,
        }


@blp.route("/list")
class BacktestListResource(MethodView):
    @blp.response(200, BacktestResponseSchema)
    @require_auth
    def get(self):
        """回测历史列表（需认证，支持 strategy_id 过滤）"""
        strategy_id = request.args.get("strategy_id", type=int)
        limit = request.args.get("limit", type=int, default=50)
        limit = min(max(limit, 1), 200)

        runs = BacktestEngine.list_runs(
            user_id=g.user_id,
            strategy_id=strategy_id,
            limit=limit,
        )

        return {
            "code": 1,
            "msg": "success",
            "data": runs,
        }
