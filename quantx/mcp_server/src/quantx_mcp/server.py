"""
QuantX MCP Server — 将 Agent Gateway REST API 暴露为 MCP 工具

这是 REST→MCP 的薄封装层:
  * REST 作为唯一真实来源（/api/agent/v1）
  * 暴露 R（读取）、W（写入）、B（回测）工具
  * T（交易）不通过 MCP 暴露 — 需要直接使用 REST
  * 用户提供的 Agent Token 的能力在服务端强制执行

环境变量:
  QUANTX_API_URL       — API 地址（默认 http://localhost:5000）
  QUANTX_AGENT_TOKEN   — Agent Token（必填）
  QUANTX_TIMEOUT_S     — 请求超时秒数（默认 60）
"""
from __future__ import annotations

import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .security import (
    assert_json_dict,
    consume_job_stream,
    poll_job_until_terminal,
    redact_secrets,
)


# ─────────────────────────── 已注册工具名 ───────────────────────────

MCP_TOOL_NAMES = (
    "whoami",
    "check_health",
    "list_markets",
    "search_symbols",
    "get_klines",
    "get_price",
    "list_strategies",
    "get_strategy",
    "create_strategy",
    "run_backtest",
)


# ─────────────────────────── 环境配置 ───────────────────────────

def _env(name: str, required: bool = True) -> str:
    """读取环境变量，缺失时退出"""
    value = (os.environ.get(name) or "").strip()
    if not value and required:
        print(f"[quantx-mcp] 缺少必需的环境变量: {name}", file=sys.stderr)
        sys.exit(2)
    return value


BASE_URL = _env("QUANTX_API_URL", required=False) or "http://localhost:5000"
BASE_URL = BASE_URL.rstrip("/")
AGENT_TOKEN = _env("QUANTX_AGENT_TOKEN")
TIMEOUT_S = float(os.environ.get("QUANTX_TIMEOUT_S", "60"))
JOB_STREAM_MAX_EVENTS = int(os.environ.get("QUANTX_MCP_JOB_STREAM_MAX_EVENTS", "200"))
JOB_STREAM_MAX_SECONDS = float(os.environ.get("QUANTX_MCP_JOB_STREAM_MAX_SECONDS", "300"))
JOB_POLL_MAX_SECONDS = float(os.environ.get("QUANTX_MCP_JOB_POLL_MAX_SECONDS", "300"))

# HTTP 客户端（携带 Agent Token）
_client = httpx.Client(
    base_url=BASE_URL,
    timeout=TIMEOUT_S,
    headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
)

# 公开端点客户端（不带 Token）
_public_client = httpx.Client(base_url=BASE_URL, timeout=min(TIMEOUT_S, 15.0))


# ─────────────────────────── HTTP 辅助 ───────────────────────────

def _unwrap(r: httpx.Response) -> Any:
    """解包 HTTP 响应，提取 data 字段"""
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:
            body = r.text[:2000]
        return {"error": True, "status": r.status_code, "body": body}
    try:
        payload = r.json()
    except Exception:
        return {"error": True, "status": r.status_code, "body": r.text[:2000]}
    # QuantX 响应格式: {"code": ..., "message": ..., "data": ...}
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _get(path: str, params: dict | None = None) -> Any:
    """GET 请求"""
    r = _client.get(path, params=params or {})
    return _unwrap(r)


def _post(path: str, json: dict | None = None, headers: dict | None = None) -> Any:
    """POST 请求"""
    r = _client.post(path, json=json or {}, headers=headers or {})
    return _unwrap(r)


# ─────────────────────────── MCP Server ───────────────────────────

mcp = FastMCP(
    "quantx-mcp",
    version="0.1.0",
    description="QuantX MCP Server — AI Agent 量化交易集成",
)


# ─────────────────────────── 工具定义 ───────────────────────────

@mcp.tool()
def whoami() -> dict:
    """获取当前 Agent Token 的信息（token_id, name, scopes, user_id）"""
    return _get("/api/agent/v1/whoami")


@mcp.tool()
def check_health() -> dict:
    """检查 Agent Gateway 健康状态"""
    r = _public_client.get("/api/agent/v1/health")
    return _unwrap(r)


@mcp.tool()
def list_markets() -> list:
    """列出 QuantX 支持的市场（CNStock/HKStock/USStock/CNFutures）"""
    return _get("/api/agent/v1/markets")


@mcp.tool()
def search_symbols(market: str, keyword: str, limit: int = 20) -> dict:
    """搜索品种（品种代码或名称子串匹配）

    Args:
        market:  市场类型（CNStock/HKStock/USStock/CNFutures）
        keyword: 搜索关键词
        limit:   返回数量上限（默认 20）
    """
    return _get("/api/agent/v1/markets/search", {
        "market": market,
        "keyword": keyword,
        "limit": limit,
    })


@mcp.tool()
def get_klines(
    market: str,
    symbol: str,
    timeframe: str = "1D",
    limit: int = 300,
) -> dict:
    """获取 K 线数据（OHLCV）

    Args:
        market:    市场类型（CNStock/HKStock/USStock/CNFutures）
        symbol:    品种代码
        timeframe: K 线周期（1m/5m/15m/30m/1H/4H/1D/1W）
        limit:     数据条数（默认 300，最大 2000）
    """
    return _get("/api/agent/v1/markets/klines", {
        "market": market,
        "symbol": symbol,
        "timeframe": timeframe,
        "limit": limit,
    })


@mcp.tool()
def get_price(market: str, symbol: str) -> dict:
    """获取品种的实时价格

    Args:
        market: 市场类型（CNStock/HKStock/USStock/CNFutures）
        symbol: 品种代码
    """
    return _get("/api/agent/v1/markets/price", {
        "market": market,
        "symbol": symbol,
    })


@mcp.tool()
def list_strategies(status: str = "", market: str = "", limit: int = 50) -> list:
    """列出当前用户的策略

    Args:
        status: 按状态过滤（draft/ready/running/stopped/error，可选）
        market: 按市场过滤（可选）
        limit:  返回数量上限（默认 50）
    """
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if market:
        params["market"] = market
    return _get("/api/agent/v1/strategies", params)


@mcp.tool()
def get_strategy(strategy_id: int) -> dict:
    """获取策略详情（含 indicator_code 和参数）

    Args:
        strategy_id: 策略 ID
    """
    return _get(f"/api/agent/v1/strategies/{strategy_id}")


@mcp.tool()
def create_strategy(
    name: str,
    market: str,
    symbol: str,
    strategy_type: str = "indicator",
    timeframe: str = "1D",
    description: str = "",
    indicator_code: str = "",
    params: dict | None = None,
) -> dict:
    """创建策略

    Args:
        name:           策略名称
        market:         市场类型（CNStock/HKStock/USStock/CNFutures）
        symbol:         品种代码
        strategy_type:  策略类型（indicator/script）
        timeframe:      K 线周期
        description:    策略描述
        indicator_code: 指标代码（Python）
        params:         策略参数（JSON 对象）
    """
    payload = {
        "name": name,
        "market": market,
        "symbol": symbol,
        "strategy_type": strategy_type,
        "timeframe": timeframe,
        "description": description,
        "indicator_code": indicator_code,
        "params": params or {},
    }
    return _post("/api/agent/v1/strategies", json=payload)


@mcp.tool()
def run_backtest(
    strategy_id: int,
    market: str = "CNStock",
    symbol: str = "",
    timeframe: str = "1D",
    start_date: str = "",
    end_date: str = "",
    initial_capital: float = 100000.0,
    params: dict | None = None,
) -> dict:
    """执行回测（异步提交，等待完成后返回结果）

    提交回测任务到 Agent Gateway，通过 SSE 流等待完成，
    返回完整的回测结果（绩效指标、交易记录等）。

    Args:
        strategy_id:     策略 ID
        market:          市场类型（默认 CNStock）
        symbol:          品种代码
        timeframe:       K 线周期（默认 1D）
        start_date:      起始日期 YYYY-MM-DD
        end_date:        结束日期 YYYY-MM-DD
        initial_capital: 初始资金（默认 100000）
        params:          策略参数覆盖
    """
    payload: dict[str, Any] = {
        "strategy_id": strategy_id,
        "market": market,
        "symbol": symbol,
        "timeframe": timeframe,
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
    }
    if params:
        payload["params"] = params

    # 1. 提交回测任务
    submit_result = _post("/api/agent/v1/backtests", json=payload)
    if isinstance(submit_result, dict) and submit_result.get("error"):
        return submit_result

    job_id = submit_result.get("job_id") if isinstance(submit_result, dict) else None
    if not job_id:
        return {"error": True, "message": "提交回测失败，未获取 job_id", "detail": submit_result}

    # 2. 通过 SSE 流等待完成
    stream_result = consume_job_stream(
        _client,
        f"/api/agent/v1/jobs/{job_id}/stream",
        max_events=JOB_STREAM_MAX_EVENTS,
        max_seconds=JOB_STREAM_MAX_SECONDS,
    )

    if stream_result.get("result"):
        return {
            "job_id": job_id,
            "status": "completed",
            "result": redact_secrets(stream_result["result"]),
            "truncated": stream_result.get("truncated", False),
        }

    # 3. SSE 未获取到结果时，退化为轮询
    poll_result = poll_job_until_terminal(
        lambda jid: _get(f"/api/agent/v1/jobs/{jid}"),
        job_id,
        timeout_s=JOB_POLL_MAX_SECONDS,
    )

    if poll_result.get("timed_out"):
        return {
            "job_id": job_id,
            "status": "timeout",
            "message": f"回测在 {JOB_POLL_MAX_SECONDS}s 内未完成",
            "last_status": (poll_result.get("job") or {}).get("status"),
        }

    return {
        "job_id": job_id,
        "status": "completed",
        "result": redact_secrets(poll_result.get("job", {})),
    }


# ─────────────────────────── 入口 ───────────────────────────

def main():
    """MCP Server 入口（通过 stdio 传输启动）"""
    print(f"[quantx-mcp] 启动: API={BASE_URL}, tools={len(MCP_TOOL_NAMES)}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
