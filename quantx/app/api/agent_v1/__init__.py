"""
QuantX Agent Gateway v1 — AI Agent 专用版本化 API 网关

挂载于 `/api/agent/v1`。

设计原则：
  * 身份认证仅使用 Agent Token（非 JWT 人类会话）
  * 每次调用都记录审计日志到 qd_agent_audit
  * 能力分级（R/W/B/T）按路由强制执行
"""
from __future__ import annotations

from flask import Blueprint, Flask, jsonify

from app.utils.logger import get_logger

logger = get_logger(__name__)


# Agent Gateway v1 Blueprint
agent_v1_bp = Blueprint("agent_v1", __name__)


@agent_v1_bp.errorhandler(404)
def _not_found(_e):
    return jsonify({
        "code": 404,
        "message": "路由不存在: /api/agent/v1",
        "data": None,
    }), 404


@agent_v1_bp.route("/health", methods=["GET"])
def health():
    """公开健康检查端点（无需 Token）"""
    return jsonify({
        "code": 200,
        "message": "ok",
        "data": {"service": "agent-gateway-v1", "status": "healthy"},
    })


@agent_v1_bp.route("/whoami", methods=["GET"])
def whoami():
    """获取当前 Token 信息（需要有效 Token，但不检查特定能力）"""
    from app.utils.agent_auth import require_agent_scope, SCOPE_READ
    # 使用内联认证（避免装饰器嵌套）
    from flask import request as req, g
    from app.utils.agent_auth import _extract_bearer, validate_agent_token, audit_log

    raw = _extract_bearer()
    if not raw:
        return jsonify({"code": 401, "message": "缺少 Agent Token", "data": None}), 401

    info = validate_agent_token(raw)
    if not info:
        return jsonify({"code": 401, "message": "Token 无效", "data": None}), 401

    # 审计日志
    audit_log(info["token_id"], "GET /api/agent/v1/whoami", "whoami",
              ip=req.remote_addr or "")

    return jsonify({
        "code": 200,
        "message": "ok",
        "data": {
            "token_id": info["token_id"],
            "name": info["name"],
            "scopes": info["scopes"],
            "user_id": info["user_id"],
        },
    })


def register(app: Flask) -> None:
    """注册 Agent Gateway Blueprint 及所有子路由

    Args:
        app: Flask 应用实例
    """
    # 导入子模块，触发 @agent_v1_bp.route 注册
    from . import markets    # noqa: F401
    from . import strategies # noqa: F401
    from . import backtests  # noqa: F401
    from . import jobs       # noqa: F401

    app.register_blueprint(agent_v1_bp, url_prefix="/api/agent/v1")
    logger.info("Agent Gateway v1 已挂载于 /api/agent/v1")
