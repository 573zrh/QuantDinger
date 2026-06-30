"""
Agent Gateway v1 — 异步任务管理端点

两种查询方式:
  * GET /jobs/{job_id}        — 单次快照（轻量）
  * GET /jobs/{job_id}/stream — SSE 进度流（适合实时消费者）

SSE 事件类型:
  * snapshot  — 当前任务状态基线
  * progress  — 进度更新
  * result    — 任务完成（成功/失败/取消）
  * ping      — 保活心跳（~每 15 秒）
"""
from __future__ import annotations

import json
import time

from flask import Response, jsonify, request

from app.utils.agent_auth import (
    SCOPE_READ, require_agent_scope, current_user_id, get_job, list_jobs,
)
from app.utils.logger import get_logger

from . import agent_v1_bp

logger = get_logger(__name__)


def _envelope(data, message: str = "ok", code: int = 200):
    return jsonify({"code": code, "message": message, "data": data})


def _error(code: int, message: str, http: int = 400):
    return jsonify({"code": code, "message": message, "data": None}), http


def _clip_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


@agent_v1_bp.route("/jobs", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_list_jobs():
    """列出当前用户的任务（最新优先）

    Query params:
        kind:  按任务类型过滤（可选）
        limit: 返回数量（1~200，默认 50）
    """
    user_id = current_user_id()
    kind = (request.args.get("kind") or "").strip() or None
    limit = _clip_int(request.args.get("limit"), default=50, lo=1, hi=200)

    rows = list_jobs(user_id=user_id, kind=kind, limit=limit)
    return _envelope(rows)


@agent_v1_bp.route("/jobs/<job_id>", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_get_job(job_id: str):
    """获取任务详情（租户隔离）

    Args:
        job_id: 任务 ID
    """
    user_id = current_user_id()
    row = get_job(job_id, user_id=user_id)
    if not row:
        return _error(404, "任务不存在", http=404)

    # 序列化 datetime 字段
    for key in ("created_at", "started_at", "finished_at"):
        if row.get(key):
            row[key] = str(row[key])

    return _envelope(row)


def _sse_frame(event: str, data) -> bytes:
    """构造 SSE 帧"""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


@agent_v1_bp.route("/jobs/<job_id>/stream", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_stream_job(job_id: str):
    """SSE 进度流

    事件类型:
      snapshot  — 连接时发送当前任务状态快照
      progress  — 进度更新（percent / message）
      result    — 任务完成事件（status / result / error）
      ping      — 保活心跳（每 ~15 秒）

    重连: 客户端可传 ?since=<seq> 或 Last-Event-ID 头部恢复。
    若任务已结束，立即发送 result 并关闭流。
    """
    user_id = current_user_id()
    row = get_job(job_id, user_id=user_id)
    if not row:
        return _error(404, "任务不存在", http=404)

    # 解析恢复序列号
    try:
        since_seq = int(
            request.args.get("since") or
            request.headers.get("Last-Event-ID") or 0
        )
    except (ValueError, TypeError):
        since_seq = 0

    def _gen():
        # 1. 发送当前快照作为基线
        snapshot = dict(row)
        for key in ("created_at", "started_at", "finished_at"):
            if snapshot.get(key):
                snapshot[key] = str(snapshot[key])
        yield _sse_frame("snapshot", snapshot)

        # 2. 若任务已结束，直接发送 result 并关闭
        terminal_statuses = ("completed", "failed", "cancelled")
        if row.get("status") in terminal_statuses:
            yield _sse_frame("result", {
                "job_id": row.get("job_id", job_id),
                "status": row.get("status"),
                "result": row.get("result"),
                "error": row.get("error"),
            })
            return

        # 3. 轮询进度直到任务完成
        last_ping = time.monotonic()
        poll_interval = 2.0  # 秒
        max_wait = 300.0     # 最大等待 5 分钟
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            time.sleep(poll_interval)

            # 重新获取任务状态
            current = get_job(job_id, user_id=user_id)
            if not current:
                yield _sse_frame("error", {"message": "任务丢失"})
                break

            # 发送进度事件
            progress = current.get("progress")
            if progress:
                yield _sse_frame("progress", {
                    "job_id": job_id,
                    "status": current.get("status"),
                    "progress": progress,
                })

            # 保活心跳
            now = time.monotonic()
            if now - last_ping > 15.0:
                yield _sse_frame("ping", {"ts": time.time()})
                last_ping = now

            # 检查是否结束
            if current.get("status") in terminal_statuses:
                # 序列化 datetime
                for key in ("created_at", "started_at", "finished_at"):
                    if current.get(key):
                        current[key] = str(current[key])

                yield _sse_frame("result", {
                    "job_id": current.get("job_id", job_id),
                    "status": current.get("status"),
                    "result": current.get("result"),
                    "error": current.get("error"),
                })
                break

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
