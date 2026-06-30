"""
QuantX 应用配置（dataclass 模式）

使用 dataclass 替代 metaclass，提供类型提示和默认值支持。
所有配置项优先从环境变量读取，不存在则使用默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    """应用全局配置"""

    # 服务基础配置
    HOST: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    PORT: int = field(default_factory=lambda: int(os.getenv("PORT", 5000)))
    DEBUG: bool = field(default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true")
    APP_NAME: str = "QuantX API"
    VERSION: str = "0.1.0"

    # 安全
    SECRET_KEY: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "quantx-secret-key-change-me"))

    # 管理员账号（首次启动自动创建）
    ADMIN_USER: str = field(default_factory=lambda: os.getenv("ADMIN_USER", "admin"))
    ADMIN_PASSWORD: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", "admin123"))

    # 日志
    LOG_LEVEL: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    LOG_DIR: str = field(default_factory=lambda: os.getenv("LOG_DIR", "logs"))
    LOG_FILE: str = field(default_factory=lambda: os.getenv("LOG_FILE", "app.log"))
    LOG_MAX_BYTES: int = field(default_factory=lambda: int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024)))
    LOG_BACKUP_COUNT: int = field(default_factory=lambda: int(os.getenv("LOG_BACKUP_COUNT", 5)))

    # 缓存
    ENABLE_CACHE: bool = field(default_factory=lambda: os.getenv("ENABLE_CACHE", "false").lower() == "true")

    # CORS 前端来源
    FRONTEND_URL: str = field(default_factory=lambda: os.getenv("FRONTEND_URL", "http://localhost:8888,http://localhost:8000"))

    def get_log_path(self) -> str:
        """返回完整的日志文件路径"""
        return os.path.join(self.LOG_DIR, self.LOG_FILE)


# 全局单例
settings = Settings()
