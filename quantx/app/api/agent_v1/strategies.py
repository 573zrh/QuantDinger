"""
Agent Gateway v1 — 策略管理端点（读: R, 写: W）

提供 AI Agent 管理策略的 CRUD API：
  - 列出策略（R）
  - 获取策略详情（R）
  - 创建策略（W）
  - 更新策略（W）
  - 删除策略（W）

所有操作限定在 Token 所属用户范围内。
"""
from __future__ import annotations

from typing import Any

from flask import jsonify, request

from app.services import strategy as strategy_service
from app.utils.agent_auth import (
    SCOPE_READ, SCOPE_WRITE, require_agent_scope, current_user_id,
)
from app.utils.logger import get_logger

from . import agent_v1_bp

logger = get_logger(__name__)

# 对外暴露的字段（过滤敏感信息）
_PUBLIC_FIELDS = (
    "id", "name", "description", "market", "symbol", "timeframe",
    "status", "strategy_type", "indicator_code", "params",
    "created_at", "updated_at",
)


def _project(row: dict | None) -> dict | None:
    """投影：只暴露公开字段"""
    if not row:
        return None
    return {k: row.get(k) for k in _PUBLIC_FIELDS if k in row}


def _envelope(data, message: str = "ok", code: int = 200):
    return jsonify({"code": code, "message": message, "data": data})


def _error(code: int, message: str, http: int = 400):
    return jsonify({"code": code, "message": message, "data": None}), http


def _get_json_or_400():
    """安全解析 JSON body"""
    if not request.is_json:
        return None, _error(400, "请求体必须是 JSON 格式")
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, _error(400, "请求体必须是 JSON 对象")
        return body, None
    except Exception:
        return None, _error(400, "JSON 解析失败")


@agent_v1_bp.route("/strategies", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_list_strategies():
    """列出当前用户的策略（精简投影）"""
    user_id = current_user_id()
    status = (request.args.get("status") or "").strip() or None
    market = (request.args.get("market") or "").strip() or None
    limit = min(int(request.args.get("limit") or 50), 200)

    try:
        rows = strategy_service.list_strategies(
            user_id=user_id,
            status=status,
            market=market,
        )
    except Exception as exc:
        logger.error(f"Agent 列出策略失败: {exc}", exc_info=True)
        return _error(500, "获取策略列表失败", http=500)

    return _envelope([_project(r) for r in rows[:limit]])


@agent_v1_bp.route("/strategies/<int:strategy_id>", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_get_strategy(strategy_id: int):
    """获取策略详情（租户隔离）"""
    user_id = current_user_id()
    try:
        row = strategy_service.get_strategy(strategy_id, user_id=user_id)
    except Exception as exc:
        logger.error(f"Agent 获取策略失败: {exc}", exc_info=True)
        return _error(500, "获取策略详情失败", http=500)

    if not row:
        return _error(404, "策略不存在", http=404)

    return _envelope(_project(row))


@agent_v1_bp.route("/strategies", methods=["POST"])
@require_agent_scope(SCOPE_WRITE)
def agent_create_strategy():
    """创建策略

    请求体字段:
        name:           策略名称（必填）
        market:         市场类型（必填: CNStock/HKStock/USStock/CNFutures）
        symbol:         品种代码（必填）
        strategy_type:  策略类型（默认 indicator）
        timeframe:      K 线周期（默认 1D）
        description:    策略描述
        indicator_code: 指标代码
        params:         策略参数（JSON 对象）
    """
    body, err = _get_json_or_400()
    if err:
        return err

    name = (body.get("name") or "").strip()
    market = (body.get("market") or "").strip()
    symbol = (body.get("symbol") or "").strip()

    if not name:
        return _error(400, "name（策略名称）必填")
    if not market:
        return _error(400, "market（市场类型）必填")
    if not symbol:
        return _error(400, "symbol（品种代码）必填")

    user_id = current_user_id()

    try:
        result = strategy_service.create_strategy(
            user_id=user_id,
            name=name,
            market=market,
            symbol=symbol,
            strategy_type=body.get("strategy_type", "indicator"),
            timeframe=body.get("timeframe", "1D"),
            description=body.get("description", ""),
            indicator_code=body.get("indicator_code", ""),
            params=body.get("params", {}),
        )
    except ValueError as ve:
        return _error(400, str(ve))
    except Exception as exc:
        logger.error(f"Agent 创建策略失败: {exc}", exc_info=True)
        return _error(500, "创建策略失败", http=500)

    if not result:
        return _error(500, "创建策略失败", http=500)

    return _envelope(
        {"strategy_id": result.get("id"), "strategy": _project(result)},
        message="created",
        code=201,
    )


@agent_v1_bp.route("/strategies/<int:strategy_id>", methods=["PUT"])
@require_agent_scope(SCOPE_WRITE)
def agent_update_strategy(strategy_id: int):
    """更新策略

    只有 draft / stopped / error 状态的策略可编辑。
    可更新字段: name, description, market, symbol, timeframe,
                strategy_type, indicator_code, params
    """
    body, err = _get_json_or_400()
    if err:
        return err

    user_id = current_user_id()

    # 构建更新参数
    update_kwargs = {}
    allowed = {"name", "description", "market", "symbol", "timeframe",
               "strategy_type", "indicator_code", "params"}
    for key in allowed:
        if key in body:
            update_kwargs[key] = body[key]

    if not update_kwargs:
        return _error(400, "未提供可更新的字段")

    try:
        result = strategy_service.update_strategy(
            strategy_id, user_id, **update_kwargs,
        )
    except ValueError as ve:
        return _error(400, str(ve))
    except Exception as exc:
        logger.error(f"Agent 更新策略失败: {exc}", exc_info=True)
        return _error(500, "更新策略失败", http=500)

    if not result:
        return _error(404, "策略不存在或无字段更新", http=404)

    return _envelope(_project(result), message="updated")


@agent_v1_bp.route("/strategies/<int:strategy_id>", methods=["DELETE"])
@require_agent_scope(SCOPE_WRITE)
def agent_delete_strategy(strategy_id: int):
    """删除策略

    运行中的策略不可删除，需先停止。
    """
    user_id = current_user_id()

    try:
        ok = strategy_service.delete_strategy(strategy_id, user_id)
    except ValueError as ve:
        return _error(400, str(ve))
    except Exception as exc:
        logger.error(f"Agent 删除策略失败: {exc}", exc_info=True)
        return _error(500, "删除策略失败", http=500)

    if not ok:
        return _error(404, "策略不存在", http=404)

    return _envelope({"deleted": True, "strategy_id": strategy_id}, message="deleted")
