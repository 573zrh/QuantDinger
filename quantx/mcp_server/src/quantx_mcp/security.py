"""
QuantX MCP Server — 客户端安全工具

提供:
  - redact_secrets: 脱敏敏感信息
  - assert_json_dict: 验证 JSON 输入
  - assert_indicator_code_size: 验证指标代码大小
  - consume_job_stream: 消费 SSE 任务流
  - poll_job_until_terminal: 轮询任务直到完成
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping

import httpx

# 指标代码大小上限（512 KiB）
MAX_INDICATOR_CODE_BYTES = 512 * 1024

# 需要脱敏的敏感键名
_SECRET_KEYS = frozenset({
    "api_key", "secret_key", "passphrase", "apiKey", "secret", "password",
    "private_key", "access_token", "refresh_token", "bot_token",
    "webhook_secret", "signing_secret", "client_secret", "token",
})

# SSE 事件解析正则
_SSE_EVENT_RE = re.compile(r"^event:\s*(\S+)\s*$", re.MULTILINE)
_SSE_DATA_RE = re.compile(r"^data:\s*(.+)$", re.MULTILINE)


def assert_indicator_code_size(code: str) -> None:
    """验证指标代码不超过大小限制

    Args:
        code: 指标代码字符串

    Raises:
        ValueError: 超过限制时抛出
    """
    if len((code or "").encode("utf-8")) > MAX_INDICATOR_CODE_BYTES:
        raise ValueError(
            f"指标代码超过 {MAX_INDICATOR_CODE_BYTES // 1024} KiB MCP 限制"
        )


def assert_json_dict(name: str, value: Any) -> dict:
    """验证输入是 JSON 对象

    Args:
        name: 参数名（用于错误消息）
        value: 待验证的值

    Returns:
        验证通过的字典

    Raises:
        ValueError: 值不是字典时抛出
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 JSON 对象")
    return value


def redact_secrets(value: Any, *, depth: int = 0, max_depth: int = 6) -> Any:
    """递归脱敏敏感信息

    遍历字典/列表，将敏感键的值替换为 "***"。

    Args:
        value: 待脱敏的数据
        depth: 当前递归深度
        max_depth: 最大递归深度

    Returns:
        脱敏后的数据
    """
    if depth > max_depth:
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key in _SECRET_KEYS and v not in (None, "", False):
                out[key] = "***"
            elif isinstance(v, Mapping):
                out[key] = redact_secrets(v, depth=depth + 1, max_depth=max_depth)
            elif isinstance(v, list):
                out[key] = [
                    redact_secrets(item, depth=depth + 1, max_depth=max_depth)
                    for item in v
                ]
            else:
                out[key] = v
        return out
    if isinstance(value, list):
        return [redact_secrets(item, depth=depth + 1, max_depth=max_depth) for item in value]
    return value


def parse_sse_chunk(text: str) -> list[tuple[str, Any]]:
    """解析一个或多个 SSE 帧

    Args:
        text: SSE 文本块

    Returns:
        [(event_name, parsed_data), ...] 列表
    """
    import json

    frames: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_m = _SSE_EVENT_RE.search(block)
        data_m = _SSE_DATA_RE.search(block)
        if not event_m or not data_m:
            continue
        event = event_m.group(1)
        try:
            payload = json.loads(data_m.group(1))
        except Exception:
            payload = data_m.group(1)
        frames.append((event, payload))
    return frames


def consume_job_stream(
    client: httpx.Client,
    path: str,
    *,
    since_seq: int = 0,
    max_events: int = 200,
    max_seconds: float = 300.0,
) -> dict[str, Any]:
    """消费 SSE 任务流直到收到 result 事件或达到安全限制

    Args:
        client: httpx 客户端（已配置 Authorization）
        path: SSE 端点路径（如 /api/agent/v1/jobs/xxx/stream）
        since_seq: 恢复序列号
        max_events: 最大事件数
        max_seconds: 最大等待秒数

    Returns:
        {
            "events": [...],
            "result": {...} | None,
            "truncated": bool,
            "event_count": int,
        }
    """
    import json

    params = {"since": int(since_seq)} if since_seq else None
    events: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    truncated = False
    started = time.monotonic()

    with client.stream("GET", path, params=params) as resp:
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:2000]
            return {
                "error": True,
                "status": resp.status_code,
                "body": body,
                "events": events,
            }

        buffer = ""
        for chunk in resp.iter_text():
            if time.monotonic() - started > max_seconds:
                truncated = True
                break
            buffer += chunk
            while "\n\n" in buffer:
                part, buffer = buffer.split("\n\n", 1)
                for event, payload in parse_sse_chunk(part + "\n\n"):
                    events.append({"event": event, "data": payload})
                    if len(events) > max_events:
                        truncated = True
                        break
                    if event == "result":
                        result = payload if isinstance(payload, dict) else {"value": payload}
                if truncated or result is not None:
                    break
            if truncated or result is not None:
                break

    return {
        "events": events,
        "result": result,
        "truncated": truncated,
        "event_count": len(events),
    }


def poll_job_until_terminal(
    get_job_fn,
    job_id: str,
    *,
    timeout_s: float = 300.0,
    interval_s: float = 2.0,
) -> dict[str, Any]:
    """轮询任务快照直到达到终态或超时

    Args:
        get_job_fn: 获取任务快照的函数（接收 job_id，返回 dict）
        job_id: 任务 ID
        timeout_s: 超时秒数
        interval_s: 轮询间隔秒数

    Returns:
        {"job": {...}, "timed_out": bool}
    """
    deadline = time.monotonic() + max(1.0, timeout_s)
    last: Any = None
    while time.monotonic() < deadline:
        last = get_job_fn(job_id)
        if isinstance(last, dict) and last.get("error"):
            return last
        status = (last or {}).get("status") if isinstance(last, dict) else None
        if status in ("completed", "failed", "cancelled"):
            return {"job": last, "timed_out": False}
        time.sleep(max(0.5, interval_s))
    return {"job": last, "timed_out": True}
