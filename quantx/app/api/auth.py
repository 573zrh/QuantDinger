"""
QuantX 认证路由

JWT 注册/登录/用户信息/修改密码。
使用 flask-smorest Blueprint + marshmallow Schema 自动 OpenAPI 文档。
"""
from __future__ import annotations

from flask import g
from flask.views import MethodView
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from app.utils.auth import create_token, require_auth
from app.utils.logger import get_logger

logger = get_logger(__name__)

blp = Blueprint("auth", __name__, url_prefix="/api/auth", description="用户认证")


# =============================================================================
# Marshmallow Schema 定义
# =============================================================================

class RegisterSchema(Schema):
    """注册请求体"""
    username = fields.Str(
        required=True,
        validate=validate.Length(min=3, max=50),
        metadata={"description": "用户名（3-50 字符）"},
    )
    password = fields.Str(
        required=True,
        validate=validate.Length(min=6, max=100),
        metadata={"description": "密码（6-100 字符）"},
    )
    email = fields.Str(
        load_default=None,
        metadata={"description": "邮箱（可选）"},
    )


class LoginSchema(Schema):
    """登录请求体"""
    username = fields.Str(required=True, metadata={"description": "用户名"})
    password = fields.Str(required=True, metadata={"description": "密码"})


class ChangePasswordSchema(Schema):
    """修改密码请求体"""
    old_password = fields.Str(required=True, metadata={"description": "旧密码"})
    new_password = fields.Str(
        required=True,
        validate=validate.Length(min=6, max=100),
        metadata={"description": "新密码（6-100 字符）"},
    )


class TokenResponseSchema(Schema):
    """认证响应"""
    code = fields.Int()
    msg = fields.Str()
    data = fields.Dict()


# =============================================================================
# 路由
# =============================================================================

@blp.route("/register")
class RegisterResource(MethodView):
    @blp.arguments(RegisterSchema)
    @blp.response(200, TokenResponseSchema)
    def post(self, args):
        """注册新用户"""
        from app.services.user_service import create_user

        try:
            user = create_user(
                username=args["username"],
                password=args["password"],
                email=args.get("email"),
            )
        except ValueError as e:
            abort(400, message=str(e))
        except Exception as e:
            logger.error(f"注册失败: {e}")
            abort(500, message="注册失败，请稍后重试")

        if not user:
            abort(500, message="注册失败，请稍后重试")

        token = create_token(
            user_id=user["id"],
            username=user["username"],
            role=user.get("role", "user"),
        )

        return {
            "code": 1,
            "msg": "注册成功",
            "data": {
                "user_id": user["id"],
                "username": user["username"],
                "token": token,
            },
        }


@blp.route("/login")
class LoginResource(MethodView):
    @blp.arguments(LoginSchema)
    @blp.response(200, TokenResponseSchema)
    def post(self, args):
        """用户登录"""
        from app.services.user_service import authenticate

        user = authenticate(args["username"], args["password"])
        if not user:
            abort(401, message="用户名或密码错误")

        token = create_token(
            user_id=user["id"],
            username=user["username"],
            role=user.get("role", "user"),
        )

        return {
            "code": 1,
            "msg": "登录成功",
            "data": {
                "user_id": user["id"],
                "username": user["username"],
                "token": token,
                "role": user.get("role", "user"),
            },
        }


@blp.route("/me")
class MeResource(MethodView):
    @blp.response(200, TokenResponseSchema)
    @require_auth
    def get(self):
        """获取当前用户信息（需认证）"""
        from app.services.user_service import get_user_by_id

        user = get_user_by_id(g.user_id)
        if not user:
            abort(404, message="用户不存在")

        return {
            "code": 1,
            "msg": "success",
            "data": {
                "id": user["id"],
                "username": user["username"],
                "email": user.get("email") or "",
                "role": user.get("role", "user"),
                "created_at": str(user.get("created_at", "")),
            },
        }


@blp.route("/password")
class ChangePasswordResource(MethodView):
    @blp.arguments(ChangePasswordSchema)
    @blp.response(200, TokenResponseSchema)
    @require_auth
    def put(self, args):
        """修改密码（需认证）"""
        from app.services.user_service import change_password

        try:
            ok = change_password(
                user_id=g.user_id,
                old_password=args["old_password"],
                new_password=args["new_password"],
            )
        except ValueError as e:
            abort(400, message=str(e))

        if not ok:
            abort(400, message="旧密码错误或用户不存在")

        return {
            "code": 1,
            "msg": "密码修改成功",
            "data": None,
        }
