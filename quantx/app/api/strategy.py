"""
QuantX 策略 CRUD 路由

使用 flask-smorest Blueprint 提供策略的完整生命周期管理接口。
所有接口均需 JWT 认证，用户只能操作自己的策略。
"""
from __future__ import annotations

from flask import g, request
from flask.views import MethodView
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from app.utils.auth import require_auth
from app.utils.logger import get_logger

logger = get_logger(__name__)

blp = Blueprint("strategy", __name__, url_prefix="/api/strategy", description="策略管理")


# =============================================================================
# Marshmallow Schema 定义
# =============================================================================

class StrategyCreateSchema(Schema):
    """创建策略请求体"""
    name = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=200),
        metadata={"description": "策略名称"},
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
    strategy_type = fields.Str(
        load_default="indicator",
        validate=validate.OneOf(["indicator", "script"]),
        metadata={"description": "策略类型"},
    )
    description = fields.Str(
        load_default="",
        metadata={"description": "策略描述"},
    )
    indicator_code = fields.Str(
        load_default="",
        metadata={"description": "指标代码"},
    )
    params = fields.Dict(
        load_default=dict,
        metadata={"description": "策略参数"},
    )


class StrategyUpdateSchema(Schema):
    """更新策略请求体（所有字段可选）"""
    name = fields.Str(validate=validate.Length(min=1, max=200))
    market = fields.Str(validate=validate.OneOf(["CNStock", "HKStock", "USStock", "CNFutures"]))
    symbol = fields.Str()
    timeframe = fields.Str()
    strategy_type = fields.Str(validate=validate.OneOf(["indicator", "script"]))
    description = fields.Str()
    indicator_code = fields.Str()
    params = fields.Dict()


class StrategyResponseSchema(Schema):
    """策略响应"""
    code = fields.Int()
    msg = fields.Str()
    data = fields.Raw()


# =============================================================================
# 路由
# =============================================================================

@blp.route("")
class StrategyListResource(MethodView):
    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def get(self):
        """策略列表（需认证，支持 status/market 过滤）"""
        from app.services.strategy import list_strategies

        status = request.args.get("status")
        market = request.args.get("market")

        strategies = list_strategies(
            user_id=g.user_id,
            status=status,
            market=market,
        )

        # 序列化 datetime 字段
        for s in strategies:
            for key in ("created_at", "updated_at"):
                if s.get(key):
                    s[key] = str(s[key])

        return {
            "code": 1,
            "msg": "success",
            "data": strategies,
        }

    @blp.arguments(StrategyCreateSchema)
    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def post(self, args):
        """创建策略（需认证）"""
        from app.services.strategy import create_strategy

        try:
            strategy = create_strategy(
                user_id=g.user_id,
                name=args["name"],
                market=args["market"],
                symbol=args["symbol"],
                timeframe=args.get("timeframe", "1D"),
                strategy_type=args.get("strategy_type", "indicator"),
                description=args.get("description", ""),
                indicator_code=args.get("indicator_code", ""),
                params=args.get("params", {}),
            )
        except ValueError as e:
            abort(400, message=str(e))
        except Exception as e:
            logger.error(f"创建策略失败: {e}")
            abort(500, message="创建策略失败")

        if not strategy:
            abort(500, message="创建策略失败")

        # 序列化 datetime
        for key in ("created_at", "updated_at"):
            if strategy.get(key):
                strategy[key] = str(strategy[key])

        return {
            "code": 1,
            "msg": "策略创建成功",
            "data": strategy,
        }


@blp.route("/<int:strategy_id>")
class StrategyResource(MethodView):
    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def get(self, strategy_id):
        """获取单个策略（需认证）"""
        from app.services.strategy import get_strategy

        strategy = get_strategy(strategy_id, user_id=g.user_id)
        if not strategy:
            abort(404, message="策略不存在或无权访问")

        for key in ("created_at", "updated_at"):
            if strategy.get(key):
                strategy[key] = str(strategy[key])

        return {
            "code": 1,
            "msg": "success",
            "data": strategy,
        }

    @blp.arguments(StrategyUpdateSchema)
    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def put(self, args, strategy_id):
        """更新策略（需认证）"""
        from app.services.strategy import update_strategy

        try:
            strategy = update_strategy(
                strategy_id=strategy_id,
                user_id=g.user_id,
                **args,
            )
        except ValueError as e:
            abort(400, message=str(e))
        except Exception as e:
            logger.error(f"更新策略失败: {e}")
            abort(500, message="更新策略失败")

        if not strategy:
            abort(500, message="更新策略失败")

        for key in ("created_at", "updated_at"):
            if strategy.get(key):
                strategy[key] = str(strategy[key])

        return {
            "code": 1,
            "msg": "策略更新成功",
            "data": strategy,
        }

    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def delete(self, strategy_id):
        """删除策略（需认证）"""
        from app.services.strategy import delete_strategy

        try:
            ok = delete_strategy(strategy_id=strategy_id, user_id=g.user_id)
        except ValueError as e:
            abort(400, message=str(e))

        if not ok:
            abort(500, message="删除策略失败")

        return {
            "code": 1,
            "msg": "策略已删除",
            "data": None,
        }


@blp.route("/<int:strategy_id>/start")
class StrategyStartResource(MethodView):
    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def post(self, strategy_id):
        """启动策略（需认证，状态更新为 running）"""
        from app.services.strategy import get_strategy, update_strategy_status

        # 验证策略归属
        strategy = get_strategy(strategy_id, user_id=g.user_id)
        if not strategy:
            abort(404, message="策略不存在或无权访问")

        try:
            ok = update_strategy_status(strategy_id, "running")
        except ValueError as e:
            abort(400, message=str(e))

        if not ok:
            abort(500, message="启动策略失败")

        return {
            "code": 1,
            "msg": "策略已启动",
            "data": {"id": strategy_id, "status": "running"},
        }


@blp.route("/<int:strategy_id>/stop")
class StrategyStopResource(MethodView):
    @blp.response(200, StrategyResponseSchema)
    @require_auth
    def post(self, strategy_id):
        """停止策略（需认证，状态更新为 stopped）"""
        from app.services.strategy import get_strategy, update_strategy_status

        strategy = get_strategy(strategy_id, user_id=g.user_id)
        if not strategy:
            abort(404, message="策略不存在或无权访问")

        try:
            ok = update_strategy_status(strategy_id, "stopped")
        except ValueError as e:
            abort(400, message=str(e))

        if not ok:
            abort(500, message="停止策略失败")

        return {
            "code": 1,
            "msg": "策略已停止",
            "data": {"id": strategy_id, "status": "stopped"},
        }
