"""
QuantX PostgreSQL 数据库连接池工具

基于 psycopg2.ThreadedConnectionPool，提供：
- 连接池创建（TCP keepalive、UTC 时区）
- 健康检查（SELECT 1）
- 连接获取超时指数退避
- 便捷 SQL 执行函数
"""
from __future__ import annotations

import os
import time
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from app.config.database import db_config
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 尝试导入 psycopg2
try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2 import OperationalError, InterfaceError
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    logger.warning("psycopg2 未安装，PostgreSQL 支持不可用。")

# 全局连接池单例
_connection_pool: Optional[Any] = None
_pool_lock = threading.Lock()


def _parse_database_url(url: str) -> Dict[str, Any]:
    """解析 DATABASE_URL 格式: postgresql://user:password@host:port/dbname"""
    if not url:
        return {}

    # 去除协议前缀
    if url.startswith("postgresql://"):
        url = url[13:]
    elif url.startswith("postgres://"):
        url = url[11:]
    else:
        return {}

    result: Dict[str, Any] = {}

    # 解析 user:password@host:port/dbname
    if "@" in url:
        auth, hostpart = url.rsplit("@", 1)
        if ":" in auth:
            result["user"], result["password"] = auth.split(":", 1)
        else:
            result["user"] = auth
    else:
        hostpart = url

    # 解析 host:port/dbname
    if "/" in hostpart:
        hostport, result["dbname"] = hostpart.split("/", 1)
    else:
        hostpart = hostpart

    if ":" in hostport:
        result["host"], port_str = hostport.split(":", 1)
        result["port"] = int(port_str)
    else:
        result["host"] = hostport
        result["port"] = 5432

    return result


def init_database() -> None:
    """创建数据库连接池（全局单例）"""
    global _connection_pool

    if _connection_pool is not None:
        return

    with _pool_lock:
        # 双重检查锁
        if _connection_pool is not None:
            return

        if not HAS_PSYCOPG2:
            raise RuntimeError("psycopg2 未安装，无法使用 PostgreSQL。")

        db_url = db_config.DATABASE_URL
        if not db_url:
            raise RuntimeError("DATABASE_URL 环境变量未设置。")

        params = _parse_database_url(db_url)
        if not params:
            raise RuntimeError(f"DATABASE_URL 格式无效: {db_url}")

        try:
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=db_config.DB_POOL_MIN,
                maxconn=db_config.DB_POOL_MAX,
                host=params.get("host", "localhost"),
                port=params.get("port", 5432),
                user=params.get("user", "quantx"),
                password=params.get("password", ""),
                dbname=params.get("dbname", "quantx"),
                connect_timeout=10,
                # 连接级别设置 UTC 时区，避免 naive timestamp 歧义
                options="-c timezone=UTC",
                # TCP keepalive：检测断开的连接
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )
            logger.info(
                f"PostgreSQL 连接池已创建: "
                f"{params.get('host')}:{params.get('port')}/{params.get('dbname')} "
                f"(min={db_config.DB_POOL_MIN}, max={db_config.DB_POOL_MAX}, "
                f"acquire_timeout={db_config.DB_POOL_ACQUIRE_TIMEOUT}s)"
            )
        except Exception as e:
            logger.error(f"PostgreSQL 连接池创建失败: {e}")
            raise


def _get_connection_pool():
    """获取连接池（若尚未创建则初始化）"""
    global _connection_pool
    if _connection_pool is None:
        init_database()
    return _connection_pool


def _is_connection_healthy(conn) -> bool:
    """轻量健康检查：SELECT 1"""
    if conn is None:
        return False
    if getattr(conn, "closed", 0):
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return True
    except Exception:
        return False


def _acquire_conn_with_wait(pg_pool):
    """
    从连接池获取连接，支持超时等待和指数退避。
    当连接池耗尽时，最多等待 DB_POOL_ACQUIRE_TIMEOUT 秒。
    """
    if not HAS_PSYCOPG2:
        raise RuntimeError("psycopg2 未安装，无法使用 PostgreSQL。")

    deadline = time.monotonic() + max(1, db_config.DB_POOL_ACQUIRE_TIMEOUT)
    backoff = 0.05  # 初始退避 50ms
    last_err: Optional[Exception] = None
    warned = False

    while True:
        try:
            conn = pg_pool.getconn()
        except pool.PoolError as e:
            last_err = e
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    f"PostgreSQL 连接池耗尽: 所有 {db_config.DB_POOL_MAX} 个连接均在使用，"
                    f"等待 {db_config.DB_POOL_ACQUIRE_TIMEOUT}s 后仍无空闲。"
                )
                raise
            if not warned:
                logger.warning(
                    f"PostgreSQL 连接池耗尽 ({db_config.DB_POOL_MAX} 在使用)，"
                    f"等待最多 {db_config.DB_POOL_ACQUIRE_TIMEOUT}s..."
                )
                warned = True
            time.sleep(min(backoff, max(0.0, remaining)))
            backoff = min(backoff * 2, 0.5)
            continue

        # 健康检查：丢弃死连接
        if not _is_connection_healthy(conn):
            try:
                pg_pool.putconn(conn, close=True)
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last_err or RuntimeError("连接池只返回了死连接")
            time.sleep(min(backoff, max(0.0, remaining)))
            continue

        return conn


@contextmanager
def get_connection():
    """
    获取 PostgreSQL 数据库连接（上下文管理器）

    用法:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ...")
    """
    pg_pool = _get_connection_pool()
    conn = None
    broken = False
    try:
        conn = _acquire_conn_with_wait(pg_pool)
        yield conn
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            if isinstance(e, (OperationalError, InterfaceError)) or getattr(conn, "closed", 0):
                broken = True
        logger.error(f"PostgreSQL 操作错误 ({type(e).__name__}): {e}", exc_info=True)
        raise
    finally:
        if conn is not None:
            try:
                pg_pool.putconn(conn, close=broken)
            except Exception:
                pass


def execute_query(sql: str, params: tuple = None) -> List[Dict[str, Any]]:
    """
    执行 SQL 并返回结果（便捷函数）

    SELECT 语句返回字典列表，其他语句返回空列表。
    """
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(sql, params)
        if sql.strip().upper().startswith("SELECT"):
            rows = cursor.fetchall()
            return [dict(row) for row in rows] if rows else []
        conn.commit()
        return []


def fetch_one(sql: str, params: tuple = None) -> Optional[Dict[str, Any]]:
    """执行 SQL 并返回单行结果"""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row else None


def fetch_all(sql: str, params: tuple = None) -> List[Dict[str, Any]]:
    """执行 SQL 并返回所有结果行"""
    return execute_query(sql, params)


def is_db_available() -> bool:
    """检查 PostgreSQL 是否可用"""
    if not HAS_PSYCOPG2:
        return False
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            return True
    except Exception as e:
        logger.debug(f"PostgreSQL 不可用: {e}")
        return False


def close_pool() -> None:
    """关闭连接池（应用退出时调用）"""
    global _connection_pool
    if _connection_pool:
        try:
            _connection_pool.closeall()
            _connection_pool = None
            logger.info("PostgreSQL 连接池已关闭")
        except Exception as e:
            logger.warning(f"关闭连接池时出错: {e}")
