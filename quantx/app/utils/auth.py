"""
QuantX 认证工具

JWT Token 生成/验证、密码哈希/验证、认证装饰器。
参考 QuantDinger utils/auth.py，精简实现。
"""
from __future__ import annotations

import datetime
from functools import wraps
from typing import Optional

import bcrypt
import jwt
from flask import g, jsonify, request

from app.config.settings import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# 密码哈希
# =============================================================================

def hash_password(password: str) -> str:
    """使用 bcrypt 对密码进行哈希处理"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码是否与哈希匹配"""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except Exception:
        return False


# =============================================================================
# JWT Token
# =============================================================================

def create_token(
    user_id: int,
    username: str,
    role: str = "user",
    expires_hours: int = 24,
) -> str:
    """
    创建 JWT Token

    Args:
        user_id: 用户 ID
        username: 用户名
        role: 用户角色（admin / user）
        expires_hours: 过期时间（小时），默认 24 小时

    Returns:
        JWT Token 字符串
    """
    try:
        payload = {
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=expires_hours),
            "iat": datetime.datetime.utcnow(),
            "sub": username,
            "user_id": user_id,
            "role": role,
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    except Exception as e:
        logger.error(f"Token 生成失败: {e}")
        return ""


def decode_token(token: str) -> Optional[dict]:
    """
    解码并验证 JWT Token

    Args:
        token: JWT Token 字符串

    Returns:
        Token payload 字典，无效时返回 None
    """
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.debug("Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"Token 无效: {e}")
        return None


# =============================================================================
# Flask 请求级别认证
# =============================================================================

def get_current_user() -> Optional[dict]:
    """
    从 Flask 请求 header 中获取当前用户信息

    读取 Authorization: Bearer <token>，解码后返回用户信息字典。
    若 token 缺失或无效则返回 None。
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    payload = decode_token(parts[1])
    if not payload:
        return None

    return {
        "user_id": payload.get("user_id"),
        "username": payload.get("sub"),
        "role": payload.get("role", "user"),
    }


def require_auth(f):
    """
    JWT 认证装饰器

    验证请求中的 Bearer Token，成功后将用户信息注入 flask.g：
    - g.user_id: 用户 ID
    - g.username: 用户名
    - g.user_role: 用户角色
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"code": 401, "msg": "未认证，请先登录", "data": None}), 401

        g.user_id = user["user_id"]
        g.username = user["username"]
        g.user_role = user["role"]
        return f(*args, **kwargs)

    return decorated
