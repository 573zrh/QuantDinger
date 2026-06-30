"""
QuantX 指标代码管理

指标代码验证（语法、安全、大小限制）和模板生成。
参考 QuantDinger indicator_code_quality.py 的简化版。
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 代码大小限制（字节）
MAX_CODE_SIZE = 50 * 1024  # 50KB

# 禁止的 import 模式
_FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests", "ftplib",
    "ctypes", "multiprocessing", "threading", "signal",
    "pickle", "shelve", "marshal",
    "importlib", "code", "codeop", "compile",
    "io", "builtins", "types", "operator",
}

# 禁止的函数/操作模式（正则）
_FORBIDDEN_PATTERNS = [
    (r"\bexec\s*\(", "禁止使用 exec()"),
    (r"\beval\s*\(", "禁止使用 eval()"),
    (r"\bcompile\s*\(", "禁止使用 compile()"),
    (r"\b__import__\s*\(", "禁止使用 __import__()"),
    (r"\bopen\s*\(", "禁止使用 open() 文件操作"),
    (r"\bgetattr\s*\(", "禁止使用 getattr()（防止绕过限制）"),
    (r"\bsetattr\s*\(", "禁止使用 setattr()（防止绕过限制）"),
    (r"\bdelattr\s*\(", "禁止使用 delattr()"),
    (r"\bglobals\s*\(\s*\)", "禁止访问全局变量"),
    (r"\blocals\s*\(\s*\)", "禁止访问局部变量"),
    (r"\bvars\s*\(", "禁止使用 vars()"),
    (r"\bdir\s*\(", "禁止使用 dir() 反射"),
    (r"\btype\s*\(\s*\w+\s*\)\s*\.\s*__", "禁止通过 type() 访问元类"),
    (r"\bos\s*\.\s*", "禁止访问 os 模块"),
    (r"\bsys\s*\.\s*", "禁止访问 sys 模块"),
    (r"\bsubprocess\s*\.\s*", "禁止使用 subprocess"),
]

# 允许的安全模块
SAFE_MODULES = {"math", "numpy", "pandas", "datetime", "json", "collections", "itertools", "functools"}


def validate_indicator_code(code: str) -> Dict[str, Any]:
    """
    验证指标代码

    执行以下检查:
    1. 语法检查（AST parse）
    2. 安全检查（禁止危险 import 和操作）
    3. 大小限制（最大 50KB）

    Args:
        code: 指标 Python 代码

    Returns:
        {"valid": bool, "errors": list, "warnings": list}
    """
    errors: List[str] = []
    warnings: List[str] = []

    # 空代码检查
    if not code or not code.strip():
        return {"valid": False, "errors": ["代码不能为空"], "warnings": []}

    # 大小限制
    code_bytes = len(code.encode("utf-8"))
    if code_bytes > MAX_CODE_SIZE:
        return {
            "valid": False,
            "errors": [f"代码大小 {code_bytes} 字节超过限制 {MAX_CODE_SIZE} 字节"],
            "warnings": [],
        }

    # 语法检查
    try:
        ast.parse(code)
    except SyntaxError as e:
        errors.append(f"语法错误: 第 {e.lineno} 行 - {e.msg}")

    # 安全检查: 禁止的 import
    for node in ast.walk(ast.parse(code)) if not errors else []:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_IMPORTS:
                    errors.append(f"禁止导入模块: {alias.name}")
                elif root not in SAFE_MODULES:
                    warnings.append(f"未经验证的模块导入: {alias.name}（仅允许 {', '.join(sorted(SAFE_MODULES))}）")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _FORBIDDEN_IMPORTS:
                    errors.append(f"禁止导入模块: {node.module}")
                elif root not in SAFE_MODULES:
                    warnings.append(f"未经验证的模块导入: {node.module}")

    # 安全检查: 禁止的函数/操作模式
    for pattern, message in _FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            errors.append(message)

    # 警告检查
    if "def " not in code and "class " not in code:
        warnings.append("建议至少定义一个函数或类")

    if "df" not in code and "dataframe" not in code.lower():
        warnings.append("未检测到 DataFrame 操作，指标通常需要处理数据")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def get_indicator_template(market: str) -> str:
    """
    获取指标代码模板

    根据市场类型返回基础的 IndicatorStrategy 模板代码。

    Args:
        market: 市场类型（CNStock/HKStock/USStock/CNFutures）

    Returns:
        Python 代码模板字符串
    """
    market_name = {
        "CNStock": "A 股",
        "HKStock": "港股",
        "USStock": "美股",
        "CNFutures": "国内期货",
    }.get(market, market)

    return f'''"""
{market_name}指标策略模板

使用方法:
1. 在 calculate() 中编写指标计算逻辑
2. 通过 df 参数获取 K 线数据（包含 open, high, low, close, volume 列）
3. 在 df 中添加信号列: open_long, close_long, open_short, close_short
4. 通过 output 字典返回结果
"""

import numpy as np
import pandas as pd

# 策略元信息
# @strategy name 双均线交叉策略
# @strategy description 短期均线上穿长期均线做多，下穿平仓

# 可调参数
# @param short_period int 10 短期均线周期
# @param long_period int 30 长期均线周期


def calculate(df: pd.DataFrame, params: dict) -> dict:
    """
    指标计算入口

    Args:
        df: K 线数据 DataFrame，包含 open, high, low, close, volume 列
        params: 策略参数字典

    Returns:
        包含可视化数据和信号的字典
    """
    df = df.copy()

    # 读取参数
    short_period = int(params.get("short_period", 10))
    long_period = int(params.get("long_period", 30))

    # 计算均线
    df["ma_short"] = df["close"].rolling(window=short_period).mean()
    df["ma_long"] = df["close"].rolling(window=long_period).mean()

    # 生成交易信号
    df["open_long"] = np.where(
        (df["ma_short"] > df["ma_long"]) & (df["ma_short"].shift(1) <= df["ma_long"].shift(1)),
        1, None
    ).tolist()

    df["close_long"] = np.where(
        (df["ma_short"] < df["ma_long"]) & (df["ma_short"].shift(1) >= df["ma_long"].shift(1)),
        1, None
    ).tolist()

    # 默认无做空信号
    df["open_short"] = [None] * len(df)
    df["close_short"] = [None] * len(df)

    # 返回结果
    output = {{
        "plots": [
            {{"name": "短期均线", "data": df["ma_short"].tolist(), "color": "#FF6B6B"}},
            {{"name": "长期均线", "data": df["ma_long"].tolist(), "color": "#4ECDC4"}},
        ],
        "signals": [
            {{"name": "做多", "data": df["open_long"].tolist(), "color": "#FF6B6B", "marker": "triangle-up"}},
            {{"name": "平仓", "data": df["close_long"].tolist(), "color": "#4ECDC4", "marker": "triangle-down"}},
        ],
    }}

    return output
'''
