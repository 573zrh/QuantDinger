"""
QuantX 数据源 API Key 管理

集中管理第三方数据源的 API 密钥，从环境变量读取。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class APIKeys:
    """数据源 API Key 配置"""

    # Tushare（A股数据）
    TUSHARE_TOKEN: str = field(default_factory=lambda: os.getenv("TUSHARE_TOKEN", ""))

    # Alpaca（美股交易）
    ALPACA_API_KEY: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    ALPACA_SECRET_KEY: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    ALPACA_BASE_URL: str = field(
        default_factory=lambda: os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    )

    @property
    def has_tushare(self) -> bool:
        """Tushare Token 是否已配置"""
        return bool(self.TUSHARE_TOKEN)

    @property
    def has_alpaca(self) -> bool:
        """Alpaca 密钥是否已配置"""
        return bool(self.ALPACA_API_KEY and self.ALPACA_SECRET_KEY)


# 全局单例
api_keys = APIKeys()
