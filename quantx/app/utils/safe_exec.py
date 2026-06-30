"""
QuantX Python 代码安全沙箱

在受限环境中安全执行用户提交的 Python 代码。
参考 QuantDinger safe_exec.py 精简实现。

安全策略:
- 白名单 import（仅允许 math, numpy, pandas, datetime 等）
- 禁止危险操作（文件 I/O、网络、os/system/exec/eval 等）
- 超时保护（默认 10 秒）
- 禁止 getattr/setattr（防止绕过限制）
"""
from __future__ import annotations

import io
import re
import signal
import sys
import threading
import traceback
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 默认超时（秒）
DEFAULT_TIMEOUT = 10

# 允许的安全模块
SAFE_MODULES: Set[str] = {
    "math", "numpy", "pandas", "datetime", "json",
    "collections", "functools", "itertools", "statistics",
    "decimal", "fractions", "copy", "time",
}

# 禁止的代码模式及其描述
_FORBIDDEN_PATTERNS = [
    (r"\bimport\s+os\b", "禁止导入 os 模块"),
    (r"\bimport\s+sys\b", "禁止导入 sys 模块"),
    (r"\bimport\s+subprocess\b", "禁止导入 subprocess 模块"),
    (r"\bfrom\s+os\b", "禁止导入 os 模块"),
    (r"\bfrom\s+sys\b", "禁止导入 sys 模块"),
    (r"\bfrom\s+subprocess\b", "禁止导入 subprocess 模块"),
    (r"\b__import__\s*\(", "禁止使用 __import__()"),
    (r"\bexec\s*\(", "禁止使用 exec()"),
    (r"\beval\s*\(", "禁止使用 eval()"),
    (r"\bcompile\s*\(", "禁止使用 compile()"),
    (r"\bopen\s*\(", "禁止使用 open() 进行文件操作"),
    (r"\bgetattr\s*\(", "禁止使用 getattr()（防止绕过限制）"),
    (r"\bsetattr\s*\(", "禁止使用 setattr()（防止绕过限制）"),
    (r"\bdelattr\s*\(", "禁止使用 delattr()"),
    (r"\bglobals\s*\(\s*\)", "禁止访问 globals()"),
    (r"\blocals\s*\(\s*\)", "禁止访问 locals()"),
]


def _check_forbidden(code: str) -> List[str]:
    """
    检查代码中是否包含禁止的模式

    Args:
        code: Python 代码

    Returns:
        违规描述列表，空列表表示无违规
    """
    violations: List[str] = []
    for pattern, message in _FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            violations.append(message)
    return violations


def _make_safe_import():
    """创建受限的 __import__ 函数，仅允许白名单模块"""
    import builtins as _builtins_mod
    _real_import = _builtins_mod.__import__

    def safe_import(name: str, *args, **kwargs):
        root = name.split(".")[0]
        if root not in SAFE_MODULES:
            raise ImportError(f"模块导入被禁止: {name}（仅允许 {', '.join(sorted(SAFE_MODULES))}）")
        return _real_import(name, *args, **kwargs)

    return safe_import


def _build_safe_builtins() -> Dict[str, Any]:
    """构建受限的 __builtins__ 字典"""
    import builtins as _builtins

    # 白名单：仅保留计算类内置函数
    allowed = {
        # 类型构造
        "bool", "int", "float", "complex", "str", "bytes", "bytearray",
        "list", "tuple", "dict", "set", "frozenset",
        "range", "slice",
        # 数学
        "abs", "round", "pow", "divmod", "min", "max", "sum",
        # 迭代
        "len", "enumerate", "zip", "map", "filter", "sorted", "reversed",
        "iter", "next", "all", "any",
        # 字符串
        "repr", "ascii", "chr", "ord", "format", "bin", "hex", "oct",
        "hash", "id",
        # 类型检查
        "isinstance", "issubclass", "callable",
        # 输出
        "print",
        # 异常
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "AttributeError", "ZeroDivisionError", "StopIteration",
        "RuntimeError", "OverflowError", "ArithmeticError",
        "NotImplementedError", "NameError", "ImportError",
        # 常量
        "True", "False", "None",
        # 函数装饰
        "staticmethod", "classmethod", "property", "super", "object",
    }

    safe = {}
    for name in allowed:
        val = getattr(_builtins, name, None)
        if val is not None:
            safe[name] = val

    # 添加受限的 __import__
    safe["__import__"] = _make_safe_import()

    return safe


@contextmanager
def _timeout_context(seconds: int):
    """
    超时上下文管理器

    Unix 主线程使用 SIGALRM，其他情况使用 threading.Timer。
    """
    is_main = threading.current_thread() is threading.main_thread()

    if sys.platform != "win32" and is_main:
        # Unix: 使用 SIGALRM
        def handler(signum, frame):
            raise TimeoutError(f"代码执行超时（{seconds} 秒）")

        old_handler = signal.signal(signal.SIGALRM, handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    else:
        # Windows / 非主线程: 简单 yield（不做超时保护）
        yield


class TimeoutError(Exception):
    """代码执行超时"""
    pass


def safe_exec_code(
    code: str,
    context: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    在沙箱环境中安全执行 Python 代码

    Args:
        code: 要执行的 Python 代码
        context: 额外的上下文变量（注入到执行环境）
        timeout: 超时秒数（默认 10 秒）

    Returns:
        {
            "success": bool,
            "result": Any,       # 执行环境中 'result' 变量的值
            "error": str,        # 错误信息（成功时为空）
            "stdout": str,       # 标准输出捕获内容
        }
    """
    # 前置检查：禁止的模式
    violations = _check_forbidden(code)
    if violations:
        return {
            "success": False,
            "result": None,
            "error": "代码安全检查失败:\n" + "\n".join(f"- {v}" for v in violations),
            "stdout": "",
        }

    # 语法检查
    try:
        compile(code, "<user_code>", "exec")
    except SyntaxError as e:
        return {
            "success": False,
            "result": None,
            "error": f"语法错误: 第 {e.lineno} 行 - {e.msg}",
            "stdout": "",
        }

    # 构建执行环境
    safe_builtins = _build_safe_builtins()
    exec_globals = {
        "__builtins__": safe_builtins,
    }
    if context:
        exec_globals.update(context)

    # 捕获 stdout
    old_stdout = sys.stdout
    captured_stdout = io.StringIO()
    sys.stdout = captured_stdout

    try:
        with _timeout_context(timeout):
            exec(code, exec_globals)

        result = exec_globals.get("result", None)
        stdout_output = captured_stdout.getvalue()

        return {
            "success": True,
            "result": result,
            "error": "",
            "stdout": stdout_output,
        }

    except TimeoutError as e:
        return {
            "success": False,
            "result": None,
            "error": str(e),
            "stdout": captured_stdout.getvalue(),
        }
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "success": False,
            "result": None,
            "error": f"{type(e).__name__}: {e}\n{tb}",
            "stdout": captured_stdout.getvalue(),
        }
    finally:
        sys.stdout = old_stdout
