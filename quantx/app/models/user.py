"""
QuantX 用户数据模型

定义用户账户的 dataclass。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class User:
    """用户"""
    id: Optional[int] = None
    username: str = ""                # 用户名（唯一）
    email: str = ""                   # 邮箱
    password_hash: str = ""           # 密码哈希
    role: str = "user"               # 角色: admin, user
    is_active: bool = True           # 是否激活
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
