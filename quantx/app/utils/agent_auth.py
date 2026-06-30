"""
QuantX Agent Gateway 认证、能力分级、审计日志

独立于 JWT 人类用户认证（app.utils.auth），专门用于 AI Agent / MCP Server 的
Token 认证体系。

能力级别:
  R — 读取（市场数据、策略列表）
  W — 写入（创建/修改策略、指标代码）
  B — 回测（执行回测任务）
  T — 交易（执行交易，默认禁用）

Token 使用 SHA-256 哈希存储，明文只在创建时返回一次。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Optional

from flask import g, jsonify, request

from app.utils.db import get_connection, fetch_one, fetch_all
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────── 能力级别 ───────────────────────────

SCOPE_READ = "R"       # 读取市场数据、策略列表
SCOPE_WRITE = "W"      # 创建/修改策略、指标代码
SCOPE_BACKTEST = "B"   # 执行回测
SCOPE_TRADE = "T"      # 执行交易（默认禁用）

ALL_SCOPES = "RWBT"

TOKEN_PREFIX = "qx_"

# ─────────────────────────── Schema 守护 ───────────────────────────

_schema_ready = False
_schema_lock = threading.Lock()


def _ensure_schema() -> None:
    """幂等的运行时 Schema 守护。

    确保 qd_agent_tokens / qd_agent_audit / qd_agent_jobs 表存在。
    主 Schema 在 migrations/init.sql 中定义，此处处理运行时升级场景。
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        ddl = """
        CREATE TABLE IF NOT EXISTS qd_agent_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES qd_users(id),
            token_hash VARCHAR(255) NOT NULL,
            name VARCHAR(200),
            scopes VARCHAR(100) DEFAULT 'R',
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS qd_agent_audit (
            id SERIAL PRIMARY KEY,
            token_id INTEGER REFERENCES qd_agent_tokens(id),
            action VARCHAR(100) NOT NULL,
            resource VARCHAR(200),
            details JSONB DEFAULT '{}',
            ip_address VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS qd_agent_jobs (
            id BIGSERIAL PRIMARY KEY,
            job_id VARCHAR(40) NOT NULL UNIQUE,
            user_id INTEGER REFERENCES qd_users(id),
            agent_token_id INTEGER REFERENCES qd_agent_tokens(id),
            kind VARCHAR(40) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            result JSONB,
            error TEXT,
            progress JSONB,
            idempotency_key VARCHAR(120),
            created_at TIMESTAMP DEFAULT NOW(),
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tokens_hash
            ON qd_agent_tokens(token_hash);
        CREATE INDEX IF NOT EXISTS idx_agent_tokens_user
            ON qd_agent_tokens(user_id);
        CREATE INDEX IF NOT EXISTS idx_agent_audit_token
            ON qd_agent_audit(token_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_jobs_user
            ON qd_agent_jobs(user_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_idem
            ON qd_agent_jobs(agent_token_id, kind, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
                    cursor.execute(stmt)
                conn.commit()
            _schema_ready = True
        except Exception as exc:
            logger.warning(f"agent_auth: Schema 守护失败（将重试）: {exc}")


def ensure_agent_gateway_schema() -> None:
    """确保 Agent Gateway 所需的数据表存在（幂等操作）。"""
    _ensure_schema()


# ─────────────────────────── Token 原语 ───────────────────────────

def _hash_token(token: str) -> str:
    """SHA-256 哈希 Token"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_agent_token(
    user_id: int,
    name: str,
    scopes: str = "R",
) -> dict:
    """创建 Agent Token

    Args:
        user_id: 所属用户 ID
        name: Token 名称（描述用途）
        scopes: 能力级别字符串，如 'R'、'RW'、'RWB'

    Returns:
        包含明文 token 和元信息的字典。明文 token 只在此处返回一次。
        {"token": "qx_...", "token_id": int, "name": str, "scopes": str}
    """
    _ensure_schema()

    # 验证 scopes
    clean_scopes = "".join(s for s in scopes.upper() if s in ALL_SCOPES) or "R"

    # 生成 token: qx_ + 32字节hex = 64字符
    body = secrets.token_hex(32)
    full_token = f"{TOKEN_PREFIX}{body}"
    token_hash = _hash_token(full_token)

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO qd_agent_tokens (user_id, token_hash, name, scopes, is_active, created_at)
                VALUES (%s, %s, %s, %s, true, NOW())
                RETURNING id
                """,
                (user_id, token_hash, name, clean_scopes),
            )
            row = cursor.fetchone()
            conn.commit()
            token_id = row[0] if row else 0
    except Exception as exc:
        logger.error(f"创建 Agent Token 失败: {exc}")
        raise

    logger.info(f"Agent Token 已创建: name={name}, user_id={user_id}, scopes={clean_scopes}")
    return {
        "token": full_token,
        "token_id": token_id,
        "name": name,
        "scopes": clean_scopes,
    }


def validate_agent_token(token: str) -> Optional[dict]:
    """验证 Agent Token

    Args:
        token: 明文 Token 字符串

    Returns:
        Token 信息字典（user_id, scopes, token_id, name），无效返回 None
    """
    _ensure_schema()

    if not token or not token.startswith(TOKEN_PREFIX):
        return None

    token_hash = _hash_token(token)
    row = fetch_one(
        """
        SELECT id, user_id, name, scopes, is_active, last_used_at
        FROM qd_agent_tokens
        WHERE token_hash = %s
        """,
        (token_hash,),
    )

    if not row:
        return None

    if not row.get("is_active"):
        return None

    # 更新最后使用时间（非关键路径，失败不影响验证）
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE qd_agent_tokens SET last_used_at = NOW() WHERE id = %s",
                (row["id"],),
            )
            conn.commit()
    except Exception as exc:
        logger.debug(f"更新 last_used_at 失败: {exc}")

    return {
        "token_id": row["id"],
        "user_id": row["user_id"],
        "scopes": row.get("scopes", "R"),
        "name": row.get("name", ""),
    }


# ─────────────────────────── 审计日志 ───────────────────────────

def audit_log(
    token_id: Optional[int],
    action: str,
    resource: str = "",
    details: Optional[dict] = None,
    ip: str = "",
) -> None:
    """记录 Agent API 审计日志到 qd_agent_audit 表

    Args:
        token_id: Agent Token ID
        action: 操作描述（如 GET /api/agent/v1/markets）
        resource: 资源标识
        details: 附加详情字典
        ip: 客户端 IP 地址
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO qd_agent_audit (token_id, action, resource, details, ip_address, created_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, NOW())
                """,
                (
                    token_id,
                    action,
                    resource,
                    json.dumps(details or {}, default=str)[:8000],
                    ip,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"审计日志写入失败: {exc}")


# ─────────────────────────── Token 管理 ───────────────────────────

def get_agent_tokens(user_id: int) -> list:
    """获取用户的 Agent Token 列表（不含明文和哈希）

    Args:
        user_id: 用户 ID

    Returns:
        Token 信息列表
    """
    _ensure_schema()
    return fetch_all(
        """
        SELECT id, name, scopes, is_active, created_at, last_used_at
        FROM qd_agent_tokens
        WHERE user_id = %s
        ORDER BY created_at DESC
        """,
        (user_id,),
    )


def revoke_agent_token(token_id: int, user_id: int) -> bool:
    """吊销 Agent Token

    Args:
        token_id: Token ID
        user_id: 用户 ID（只能吊销自己的 Token）

    Returns:
        是否成功吊销
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE qd_agent_tokens
                SET is_active = false
                WHERE id = %s AND user_id = %s AND is_active = true
                """,
                (token_id, user_id),
            )
            conn.commit()
            ok = cursor.rowcount > 0
            if ok:
                logger.info(f"Agent Token 已吊销: token_id={token_id}, user_id={user_id}")
            return ok
    except Exception as exc:
        logger.error(f"吊销 Agent Token 失败: {exc}")
        return False


# ─────────────────────────── 能力检查 ───────────────────────────

def _has_scope(token_scopes: str, required: str) -> bool:
    """检查 Token 是否具有指定能力"""
    return required in (token_scopes or "")


# ─────────────────────────── 认证装饰器 ───────────────────────────

def _extract_bearer() -> Optional[str]:
    """从 Authorization 头部提取 Bearer Token"""
    auth_header = request.headers.get("Authorization", "")
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def require_agent_scope(required_scope: str = SCOPE_READ):
    """Agent Token 认证装饰器

    验证 Bearer Token → 检查能力级别 → 注入 g.agent_token / g.agent_user_id。
    每个请求都记录审计日志。

    Args:
        required_scope: 所需的能力级别（R/W/B/T）

    用法:
        @agent_v1_bp.route("/markets")
        @require_agent_scope(SCOPE_READ)
        def list_markets():
            ...
    """
    if required_scope not in ALL_SCOPES:
        raise ValueError(f"无效的能力级别: {required_scope}")

    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            _ensure_schema()
            t0 = time.time()
            ip = request.remote_addr or ""

            # 1. 提取 Token
            raw_token = _extract_bearer()
            if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
                audit_log(None, f"{request.method} {request.path}", "auth",
                          {"reason": "missing_token"}, ip)
                return jsonify({
                    "code": 401,
                    "message": "缺少或格式错误的 Agent Token",
                    "data": None,
                }), 401

            # 2. 验证 Token
            token_info = validate_agent_token(raw_token)
            if not token_info:
                audit_log(None, f"{request.method} {request.path}", "auth",
                          {"reason": "invalid_token"}, ip)
                return jsonify({
                    "code": 401,
                    "message": "Agent Token 无效或已停用",
                    "data": None,
                }), 401

            # 3. 检查能力级别
            if not _has_scope(token_info["scopes"], required_scope):
                audit_log(
                    token_info["token_id"],
                    f"{request.method} {request.path}",
                    "auth",
                    {"reason": "insufficient_scope",
                     "required": required_scope,
                     "granted": token_info["scopes"]},
                    ip,
                )
                return jsonify({
                    "code": 403,
                    "message": f"Token 缺少所需能力: {required_scope}",
                    "data": None,
                }), 403

            # 4. 注入上下文
            g.agent_token = token_info
            g.agent_user_id = token_info["user_id"]

            # 5. 执行业务逻辑
            try:
                response = fn(*args, **kwargs)
            except Exception as exc:
                logger.error(f"Agent 路由异常: {exc}", exc_info=True)
                duration_ms = int((time.time() - t0) * 1000)
                audit_log(
                    token_info["token_id"],
                    f"{request.method} {request.path}",
                    "error",
                    {"error": str(exc)[:500], "duration_ms": duration_ms},
                    ip,
                )
                return jsonify({
                    "code": 500,
                    "message": "服务器内部错误",
                    "data": None,
                }), 500

            # 6. 记录成功审计
            duration_ms = int((time.time() - t0) * 1000)
            status_code = 200
            if isinstance(response, tuple) and len(response) >= 2:
                status_code = int(response[1])

            audit_log(
                token_info["token_id"],
                f"{request.method} {request.path}",
                request.path,
                {"status": status_code, "duration_ms": duration_ms,
                 "args": dict(request.args)},
                ip,
            )
            return response

        return wrapper
    return decorator


# ─────────────────────────── 幂等性 ───────────────────────────

@contextmanager
def with_idempotency(kind: str):
    """幂等性上下文管理器

    检查是否已有相同 (token_id, kind, idempotency_key) 的任务。
    若存在则返回已有结果（跳过重复执行），否则返回 None 让调用者执行。

    Args:
        kind: 任务类型（如 'backtest'）

    用法:
        with with_idempotency("backtest") as existing:
            if existing:
                return jsonify({"data": existing})
            # 执行实际工作...
    """
    token_info = getattr(g, "agent_token", None) or {}
    key = request.headers.get("Idempotency-Key")
    if not key or not token_info.get("token_id"):
        yield None
        return

    try:
        existing = fetch_one(
            """
            SELECT job_id, status, result, error
            FROM qd_agent_jobs
            WHERE agent_token_id = %s AND kind = %s AND idempotency_key = %s
            ORDER BY id DESC LIMIT 1
            """,
            (token_info["token_id"], kind, key),
        )
    except Exception as exc:
        logger.warning(f"幂等性检查失败: {exc}")
        existing = None

    yield existing


# ─────────────────────────── 任务管理 ───────────────────────────

def submit_job(
    user_id: int,
    agent_token_id: int,
    kind: str,
    request_payload: dict,
    runner: Callable,
) -> dict:
    """提交异步任务到 qd_agent_jobs 表，并使用线程池执行

    Args:
        user_id: 用户 ID
        agent_token_id: Agent Token ID
        kind: 任务类型
        request_payload: 请求参数
        runner: 执行函数，接收 request_payload 并返回结果

    Returns:
        {"job_id": str, "status": "queued"}
    """
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    job_id = f"{kind}_{uuid.uuid4().hex[:12]}"
    idempotency_key = request.headers.get("Idempotency-Key")

    # 插入任务记录
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO qd_agent_jobs
                    (job_id, user_id, agent_token_id, kind, status,
                     request_payload, idempotency_key, created_at)
                VALUES (%s, %s, %s, %s, 'queued', %s::jsonb, %s, NOW())
                """,
                (job_id, user_id, agent_token_id, kind,
                 json.dumps(request_payload, default=str),
                 idempotency_key),
            )
            conn.commit()
    except Exception as exc:
        logger.error(f"提交任务失败: {exc}")
        raise

    # 在线程池中异步执行
    _executor = _get_executor()

    def _run():
        _update_job_status(job_id, "running")
        try:
            result = runner(request_payload)
            _update_job_result(job_id, result)
        except Exception as exc:
            logger.error(f"任务 {job_id} 执行失败: {exc}", exc_info=True)
            _update_job_error(job_id, str(exc))

    _executor.submit(_run)

    return {"job_id": job_id, "status": "queued"}


# 全局线程池
_executor: Optional[Any] = None
_executor_lock = threading.Lock()


def _get_executor():
    """获取或创建全局线程池"""
    global _executor
    if _executor is not None:
        return _executor
    with _executor_lock:
        if _executor is not None:
            return _executor
        from concurrent.futures import ThreadPoolExecutor
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-job")
        return _executor


def _update_job_status(job_id: str, status: str) -> None:
    """更新任务状态"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE qd_agent_jobs SET status = %s, started_at = NOW()
                WHERE job_id = %s
                """,
                (status, job_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"更新任务状态失败: {exc}")


def _update_job_result(job_id: str, result: Any) -> None:
    """更新任务结果"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE qd_agent_jobs
                SET status = 'completed', result = %s::jsonb, finished_at = NOW()
                WHERE job_id = %s
                """,
                (json.dumps(result, default=str), job_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"更新任务结果失败: {exc}")


def _update_job_error(job_id: str, error: str) -> None:
    """更新任务错误"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE qd_agent_jobs
                SET status = 'failed', error = %s, finished_at = NOW()
                WHERE job_id = %s
                """,
                (error[:2000], job_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"更新任务错误失败: {exc}")


def _update_job_progress(job_id: str, progress: dict) -> None:
    """更新任务进度（供 SSE 读取）"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE qd_agent_jobs SET progress = %s::jsonb
                WHERE job_id = %s
                """,
                (json.dumps(progress, default=str), job_id),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"更新任务进度失败: {exc}")


def get_job(job_id: str, user_id: Optional[int] = None) -> Optional[dict]:
    """获取任务详情

    Args:
        job_id: 任务 ID
        user_id: 用户 ID（用于租户隔离）

    Returns:
        任务信息字典，不存在返回 None
    """
    if user_id is not None:
        return fetch_one(
            """
            SELECT job_id, user_id, kind, status, request_payload,
                   result, error, progress, idempotency_key,
                   created_at, started_at, finished_at
            FROM qd_agent_jobs WHERE job_id = %s AND user_id = %s
            """,
            (job_id, user_id),
        )
    return fetch_one(
        """
        SELECT job_id, user_id, kind, status, request_payload,
               result, error, progress, idempotency_key,
               created_at, started_at, finished_at
        FROM qd_agent_jobs WHERE job_id = %s
        """,
        (job_id,),
    )


def list_jobs(user_id: int, kind: Optional[str] = None, limit: int = 50) -> list:
    """获取用户的任务列表

    Args:
        user_id: 用户 ID
        kind: 按任务类型过滤
        limit: 返回数量上限

    Returns:
        任务列表
    """
    conditions = ["user_id = %s"]
    params: list = [user_id]

    if kind:
        conditions.append("kind = %s")
        params.append(kind)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT job_id, kind, status, created_at, started_at, finished_at
        FROM qd_agent_jobs
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT %s
    """
    params.append(limit)
    return fetch_all(sql, tuple(params))


# ─────────────────────────── 便捷函数 ───────────────────────────

def current_token() -> dict:
    """获取当前请求的 Agent Token 信息"""
    return getattr(g, "agent_token", {}) or {}


def current_user_id() -> int:
    """获取当前请求的用户 ID"""
    return int(getattr(g, "agent_user_id", 0) or 0)
