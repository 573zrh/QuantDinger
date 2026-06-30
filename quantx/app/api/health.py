"""
QuantX 健康检查接口

提供基础健康检查和数据库连通性检查端点。
"""
from __future__ import annotations

from flask import jsonify
from flask_smorest import Blueprint
from marshmallow import Schema, fields

from app.config.settings import settings

blp = Blueprint("health", "health", url_prefix="/api", description="健康检查")


class HealthResponseSchema(Schema):
    """健康检查响应 Schema"""
    status = fields.String(metadata={"description": "服务状态"})
    version = fields.String(metadata={"description": "API 版本号"})
    service = fields.String(metadata={"description": "服务名称"})


class DbHealthResponseSchema(Schema):
    """数据库健康检查响应 Schema"""
    status = fields.String(metadata={"description": "数据库状态: ok / error"})
    message = fields.String(metadata={"description": "详细信息"})


@blp.route("/health")
@blp.response(200, HealthResponseSchema)
@blp.doc(summary="健康检查", description="返回服务基本状态和版本信息")
def health_check():
    """GET /api/health — 基础健康检查"""
    return {
        "status": "ok",
        "version": settings.VERSION,
        "service": settings.APP_NAME,
    }


@blp.route("/health/db")
@blp.response(200, DbHealthResponseSchema)
@blp.doc(summary="数据库健康检查", description="检查 PostgreSQL 连接是否正常")
def db_health_check():
    """GET /api/health/db — 数据库连通性检查"""
    try:
        from app.utils.db import is_db_available
        if is_db_available():
            return {"status": "ok", "message": "PostgreSQL 连接正常"}
        else:
            return {"status": "error", "message": "PostgreSQL 连接不可用"}
    except Exception as e:
        return {"status": "error", "message": f"数据库检查失败: {str(e)}"}
