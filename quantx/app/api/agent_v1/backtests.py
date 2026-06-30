"""
Agent Gateway v1 — 回测端点（能力级别: B）

异步提交回测任务，返回 job_id。Agent 通过 /jobs/{job_id} 轮询或 SSE 获取结果。

流程:
  1. POST /backtests → 创建异步任务（job_id）
  2. GET /backtests/{run_id} → 查看回测结果
  3. GET /backtests/list → 回测历史
"""
from __future__ import annotations

from flask import jsonify, request

from app.services.backtest import BacktestEngine
from app.utils.agent_auth import (
    SCOPE_BACKTEST, SCOPE_READ, require_agent_scope,
    current_user_id, current_token, with_idempotency, submit_job,
)
from app.utils.logger import get_logger

from . import agent_v1_bp

logger = get_logger(__name__)

_backtest_engine = BacktestEngine()


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


def _run_backtest(payload: dict):
    """回测执行适配器：将 Agent payload 转换为 BacktestEngine.run() 调用"""
    strategy_id = payload.get("strategy_id")
    if not strategy_id:
        raise ValueError("strategy_id 必填")

    market = payload.get("market", "CNStock")
    symbol = payload.get("symbol", "")
    timeframe = payload.get("timeframe", "1D")
    start_date = payload.get("start_date", "")
    end_date = payload.get("end_date", "")
    initial_capital = float(payload.get("initial_capital", 100000))
    params = payload.get("params")

    if not start_date or not end_date:
        raise ValueError("start_date 和 end_date 必填（格式: YYYY-MM-DD）")

    return _backtest_engine.run(
        strategy_id=int(strategy_id),
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        params=params,
        user_id=int(payload.get("__user_id", 1)),
    )


@agent_v1_bp.route("/backtests", methods=["POST"])
@require_agent_scope(SCOPE_BACKTEST)
def agent_create_backtest():
    """提交回测任务

    异步执行，立即返回 job_id。通过 GET /api/agent/v1/jobs/{job_id} 查询结果。

    请求体字段:
        strategy_id:     策略 ID（必填）
        market:          市场类型（默认 CNStock）
        symbol:          品种代码
        timeframe:       K 线周期（默认 1D）
        start_date:      起始日期 YYYY-MM-DD（必填）
        end_date:        结束日期 YYYY-MM-DD（必填）
        initial_capital: 初始资金（默认 100000）
        params:          策略参数覆盖
    """
    body, err = _get_json_or_400()
    if err:
        return err

    strategy_id = body.get("strategy_id")
    if not strategy_id:
        return _error(400, "strategy_id 必填")

    start_date = body.get("start_date", "")
    end_date = body.get("end_date", "")
    if not start_date or not end_date:
        return _error(400, "start_date 和 end_date 必填（格式: YYYY-MM-DD）")

    # 幂等性检查
    with with_idempotency("backtest") as existing:
        if existing:
            return _envelope({
                "job_id": existing.get("job_id"),
                "status": existing.get("status"),
                "duplicate": True,
            }, message="幂等重放")

    # 构造任务参数
    payload = dict(body)
    payload["__user_id"] = current_user_id()

    token_info = current_token()
    job = submit_job(
        user_id=current_user_id(),
        agent_token_id=token_info.get("token_id", 0),
        kind="backtest",
        request_payload=payload,
        runner=_run_backtest,
    )

    return _envelope(job, message="queued", code=202)


@agent_v1_bp.route("/backtests/<int:run_id>", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_get_backtest(run_id: int):
    """获取回测结果

    Args:
        run_id: 回测运行记录 ID
    """
    user_id = current_user_id()

    try:
        run = BacktestEngine.get_run(run_id, user_id=user_id)
    except Exception as exc:
        logger.error(f"Agent 获取回测结果失败: {exc}", exc_info=True)
        return _error(500, "获取回测结果失败", http=500)

    if not run:
        return _error(404, "回测记录不存在", http=404)

    # 如果回测完成，附带交易记录
    trades = []
    if run.get("status") == "completed":
        try:
            trades = BacktestEngine.get_trades(run_id)
        except Exception:
            pass

    return _envelope({
        "run": run,
        "trades": trades[:100],  # 限制返回交易数
    })


@agent_v1_bp.route("/backtests/list", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_list_backtests():
    """获取回测历史列表

    Query params:
        strategy_id: 按策略过滤（可选）
        limit:       返回数量（默认 50，最大 200）
    """
    user_id = current_user_id()
    strategy_id = request.args.get("strategy_id")
    limit = min(int(request.args.get("limit") or 50), 200)

    sid = int(strategy_id) if strategy_id and strategy_id.isdigit() else None

    try:
        runs = BacktestEngine.list_runs(
            user_id=user_id,
            strategy_id=sid,
            limit=limit,
        )
    except Exception as exc:
        logger.error(f"Agent 获取回测历史失败: {exc}", exc_info=True)
        return _error(500, "获取回测历史失败", http=500)

    return _envelope(runs)
