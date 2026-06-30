"""
QuantX 用户服务

用户 CRUD、密码管理、管理员初始化。
使用 app/utils/db.py 进行数据库操作。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.utils.auth import hash_password, verify_password
from app.utils.db import fetch_one, execute_query, get_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def create_user(
    username: str,
    password: str,
    email: Optional[str] = None,
    role: str = "user",
) -> Optional[Dict[str, Any]]:
    """
    创建新用户

    Args:
        username: 用户名（3-50 字符，唯一）
        password: 明文密码（≥6 字符），将使用 bcrypt 哈希存储
        email: 邮箱（可选）
        role: 角色（admin / user），默认 user

    Returns:
        新用户字典 {"id", "username", "email", "role", "created_at"}，失败返回 None
    """
    # 参数验证
    username = (username or "").strip()
    if len(username) < 3 or len(username) > 50:
        raise ValueError("用户名长度必须为 3-50 个字符")
    if len(password) < 6 or len(password) > 100:
        raise ValueError("密码长度必须为 6-100 个字符")

    # 检查用户名是否已存在
    existing = get_user_by_username(username)
    if existing:
        raise ValueError(f"用户名 '{username}' 已被注册")

    pw_hash = hash_password(password)

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO qd_users (username, password_hash, email, role, is_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, true, NOW(), NOW())
                RETURNING id, username, email, role, is_active, created_at
                """,
                (username, pw_hash, email, role),
            )
            row = cursor.fetchone()
            conn.commit()
            if row:
                # psycopg2 默认返回 tuple，手动构建字典
                cols = [desc[0] for desc in cursor.description]
                result = dict(zip(cols, row))
                logger.info(f"用户创建成功: {username} (id={result.get('id')})")
                return result
            return None
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"创建用户失败: {e}")
        return None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 查询用户（不含密码哈希）"""
    try:
        return fetch_one(
            """
            SELECT id, username, email, role, is_active, created_at, updated_at
            FROM qd_users WHERE id = %s
            """,
            (user_id,),
        )
    except Exception as e:
        logger.error(f"按 ID 查询用户失败: {e}")
        return None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """按用户名查询用户（含密码哈希，用于认证）"""
    try:
        return fetch_one(
            """
            SELECT id, username, password_hash, email, role, is_active, created_at, updated_at
            FROM qd_users WHERE username = %s
            """,
            (username,),
        )
    except Exception as e:
        logger.error(f"按用户名查询用户失败: {e}")
        return None


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    验证用户名和密码

    Args:
        username: 用户名
        password: 明文密码

    Returns:
        认证成功返回用户字典（不含密码哈希），失败返回 None
    """
    user = get_user_by_username(username)
    if not user:
        return None

    if not user.get("is_active", False):
        logger.warning(f"尝试登录已禁用账户: {username}")
        return None

    pw_hash = user.get("password_hash", "")
    if not verify_password(password, pw_hash):
        return None

    # 移除密码哈希，返回安全字段
    user.pop("password_hash", None)
    return user


def update_user(user_id: int, **kwargs) -> bool:
    """
    更新用户信息

    支持的字段: email, role, is_active

    Args:
        user_id: 用户 ID
        **kwargs: 要更新的字段和值

    Returns:
        是否更新成功
    """
    allowed_fields = {"email", "role", "is_active"}
    updates = []
    values = []

    for field_name, value in kwargs.items():
        if field_name in allowed_fields:
            updates.append(f"{field_name} = %s")
            values.append(value)

    if not updates:
        return False

    updates.append("updated_at = NOW()")
    values.append(user_id)

    try:
        sql = f"UPDATE qd_users SET {', '.join(updates)} WHERE id = %s"
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(values))
            conn.commit()
            affected = cursor.rowcount
            return affected > 0
    except Exception as e:
        logger.error(f"更新用户失败: {e}")
        return False


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    """
    修改用户密码

    Args:
        user_id: 用户 ID
        old_password: 旧密码
        new_password: 新密码（≥6 字符）

    Returns:
        是否修改成功
    """
    if len(new_password) < 6:
        raise ValueError("新密码长度必须至少为 6 个字符")

    # 获取当前密码哈希
    row = fetch_one(
        "SELECT password_hash FROM qd_users WHERE id = %s",
        (user_id,),
    )
    if not row:
        return False

    current_hash = row.get("password_hash", "")
    if not verify_password(old_password, current_hash):
        return False

    new_hash = hash_password(new_password)
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE qd_users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                (new_hash, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"修改密码失败: {e}")
        return False


def ensure_admin_exists() -> None:
    """
    确保管理员账户存在

    从 settings 读取 ADMIN_USER / ADMIN_PASSWORD，若数据库中无用户则自动创建管理员。
    """
    try:
        from app.config.settings import settings as app_settings

        row = fetch_one("SELECT COUNT(*) AS cnt FROM qd_users")
        count = row["cnt"] if row else 0

        if count == 0:
            admin_user = app_settings.ADMIN_USER
            admin_password = app_settings.ADMIN_PASSWORD

            result = create_user(
                username=admin_user,
                password=admin_password,
                email="admin@quantx.local",
                role="admin",
            )
            if result:
                logger.info(f"管理员账户已创建: {admin_user}")
            else:
                logger.warning("管理员账户创建失败")
        else:
            logger.debug(f"数据库中已有 {count} 个用户，跳过管理员初始化")
    except Exception as e:
        logger.error(f"ensure_admin_exists 失败: {e}")
