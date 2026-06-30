"""
QuantX 时间工具

提供 UTC 时间序列化和常用时间辅助函数。
所有时间戳以 UTC ISO 8601 + Z 后缀输出，确保前端浏览器正确解析。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def now_utc() -> datetime:
    """返回当前 UTC 时间（带时区信息）"""
    return datetime.now(timezone.utc)


def timestamp_now() -> float:
    """返回当前 UTC 时间的 Unix 时间戳（秒）"""
    return now_utc().timestamp()


def to_utc_iso(value: Any) -> Optional[str]:
    """
    将值转换为 UTC ISO 8601 字符串（带 Z 后缀）

    支持:
    - datetime 对象（有/无时区信息）
    - Unix 时间戳（秒或毫秒）
    - ISO 8601 字符串
    - None / 空字符串 → None
    """
    if value is None or value == "":
        return None

    dt: Optional[datetime] = None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        # 大于 1e12 视为毫秒
        if ts > 1e12:
            ts /= 1000.0
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            # 处理尾部 Z（Python < 3.11 不直接支持）
            normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
            if " " in normalized and "T" not in normalized:
                normalized = normalized.replace(" ", "T", 1)
            dt = datetime.fromisoformat(normalized)
        except Exception:
            return None
    else:
        return None

    if dt is None:
        return None

    # naive datetime 假定为 UTC（PostgreSQL 连接池已设置 timezone=UTC）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_utc = dt.astimezone(timezone.utc)
    # 输出秒级精度，去掉微秒，加 Z 后缀
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["now_utc", "timestamp_now", "to_utc_iso"]
