"""
Agent Gateway v1 — 市场数据端点（能力级别: R）

提供 AI Agent 访问市场数据的只读 API：
  - 列出支持的市场
  - 搜索品种
  - 获取 K 线数据
  - 获取实时价格
"""
from __future__ import annotations

from flask import jsonify, request

from app.markets.registry import list_markets, get_market_module
from app.services.market_data import MarketDataService
from app.utils.agent_auth import SCOPE_READ, require_agent_scope
from app.utils.logger import get_logger

from . import agent_v1_bp

logger = get_logger(__name__)

# 允许的市场列表
_VALID_MARKETS = {"CNStock", "HKStock", "USStock", "CNFutures"}


def _clip_int(value, default: int, lo: int, hi: int) -> int:
    """安全地将值限制在 [lo, hi] 范围内"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _envelope(data, message: str = "ok", code: int = 200):
    """统一响应格式"""
    return jsonify({"code": code, "message": message, "data": data})


def _error(code: int, message: str, http: int = 400):
    """统一错误格式"""
    return jsonify({"code": code, "message": message, "data": None}), http


@agent_v1_bp.route("/markets", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_list_markets():
    """列出支持的市场

    返回 QuantX 支持的四个市场的基本信息：
    CNStock（A 股）、HKStock（港股）、USStock（美股）、CNFutures（中国期货）
    """
    markets = list_markets()
    return _envelope(markets)


@agent_v1_bp.route("/markets/search", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_search_symbols():
    """搜索品种

    Query params:
        market:  市场类型（CNStock/HKStock/USStock/CNFutures）
        keyword: 搜索关键词（品种代码或名称子串）
        limit:   返回数量上限（1~100，默认 20）
    """
    market = (request.args.get("market") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    limit = _clip_int(request.args.get("limit"), default=20, lo=1, hi=100)

    if not market:
        return _error(400, "参数 market 必填")
    if market not in _VALID_MARKETS:
        return _error(400, f"不支持的市场: {market}")
    if not keyword:
        return _error(400, "参数 keyword 必填")

    try:
        results = MarketDataService.search_symbols(market, keyword)
        # 限制返回数量
        results = results[:limit] if results else []
    except Exception as exc:
        logger.error(f"Agent 搜索品种失败: {exc}", exc_info=True)
        return _error(500, "搜索失败", http=500)

    return _envelope({
        "market": market,
        "keyword": keyword,
        "count": len(results),
        "symbols": results,
    })


@agent_v1_bp.route("/markets/klines", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_get_klines():
    """获取 K 线数据

    Query params:
        market:    市场类型（必填）
        symbol:    品种代码（必填）
        timeframe: K 线周期（默认 1D）
        limit:     数据条数（1~2000，默认 300）
    """
    market = (request.args.get("market") or "").strip()
    symbol = (request.args.get("symbol") or "").strip()
    timeframe = (request.args.get("timeframe") or "1D").strip()
    limit = _clip_int(request.args.get("limit"), default=300, lo=1, hi=2000)

    if not market or not symbol:
        return _error(400, "参数 market 和 symbol 必填")
    if market not in _VALID_MARKETS:
        return _error(400, f"不支持的市场: {market}")

    try:
        klines = MarketDataService.get_kline(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
        )
    except Exception as exc:
        logger.error(f"Agent 获取 K 线失败: {exc}", exc_info=True)
        return _error(500, "获取 K 线数据失败", http=502)

    return _envelope({
        "market": market,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(klines),
        "klines": klines,
    })


@agent_v1_bp.route("/markets/price", methods=["GET"])
@require_agent_scope(SCOPE_READ)
def agent_get_price():
    """获取实时价格

    Query params:
        market: 市场类型（必填）
        symbol: 品种代码（必填）
    """
    market = (request.args.get("market") or "").strip()
    symbol = (request.args.get("symbol") or "").strip()

    if not market or not symbol:
        return _error(400, "参数 market 和 symbol 必填")
    if market not in _VALID_MARKETS:
        return _error(400, f"不支持的市场: {market}")

    try:
        ticker = MarketDataService.get_ticker(market, symbol)
        price = ticker.get("last") if isinstance(ticker, dict) else None
    except Exception as exc:
        logger.error(f"Agent 获取价格失败: {exc}", exc_info=True)
        return _error(500, "获取价格失败", http=502)

    return _envelope({
        "market": market,
        "symbol": symbol,
        "price": price,
        "raw": ticker,
    })
