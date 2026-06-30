"""
QuantX 行情数据 API

使用 flask-smorest Blueprint 提供行情数据端点：
- GET /api/market/kline — K 线数据查询
- GET /api/market/ticker — 实时行情
- GET /api/market/search — 品种搜索
- GET /api/market/list — 市场列表
"""
from __future__ import annotations

from flask import request
from flask_smorest import Blueprint
from marshmallow import Schema, fields

from app.services.market_data import market_data_service
from app.utils.logger import get_logger

logger = get_logger(__name__)

blp = Blueprint("market", "market", url_prefix="/api/market", description="行情数据")


# ------------------------------------------------------------------
# 请求 / 响应 Schema
# ------------------------------------------------------------------

class KlineQuerySchema(Schema):
    """K 线查询参数"""
    market = fields.String(required=True, metadata={"description": "市场类型: CNStock, HKStock, USStock, CNFutures"})
    symbol = fields.String(required=True, metadata={"description": "品种代码"})
    timeframe = fields.String(load_default="1D", metadata={"description": "K 线周期: 1m, 5m, 15m, 30m, 1H, 4H, 1D, 1W"})
    limit = fields.Integer(load_default=300, metadata={"description": "数据条数（1-5000）"})
    before_time = fields.Integer(load_default=None, metadata={"description": "获取此时间戳之前的数据（Unix 秒）"})
    after_time = fields.Integer(load_default=None, metadata={"description": "获取此时间戳之后的数据（Unix 秒）"})
    refresh = fields.Boolean(load_default=False, metadata={"description": "强制刷新缓存"})


class KlineBarSchema(Schema):
    """单根 K 线"""
    time = fields.Integer(metadata={"description": "UTC Unix 秒时间戳"})
    open = fields.Float(metadata={"description": "开盘价"})
    high = fields.Float(metadata={"description": "最高价"})
    low = fields.Float(metadata={"description": "最低价"})
    close = fields.Float(metadata={"description": "收盘价"})
    volume = fields.Float(metadata={"description": "成交量"})


class KlineResponseSchema(Schema):
    """K 线响应"""
    market = fields.String()
    symbol = fields.String()
    timeframe = fields.String()
    count = fields.Integer()
    data = fields.List(fields.Nested(KlineBarSchema))


class TickerQuerySchema(Schema):
    """行情查询参数"""
    market = fields.String(required=True, metadata={"description": "市场类型"})
    symbol = fields.String(required=True, metadata={"description": "品种代码"})


class TickerResponseSchema(Schema):
    """行情响应"""
    last = fields.Float(metadata={"description": "最新价"})
    change = fields.Float(metadata={"description": "涨跌额"})
    changePercent = fields.Float(metadata={"description": "涨跌幅 (%)"})
    symbol = fields.String()


class SearchQuerySchema(Schema):
    """品种搜索参数"""
    market = fields.String(required=True, metadata={"description": "市场类型"})
    keyword = fields.String(required=True, metadata={"description": "搜索关键词"})


class SearchItemSchema(Schema):
    """搜索结果项"""
    symbol = fields.String()
    market = fields.String()
    display_name = fields.String()


class MarketInfoSchema(Schema):
    """市场信息"""
    market = fields.String()
    display_name = fields.String()
    exchanges = fields.List(fields.String())
    trading_hours = fields.String()
    timezone = fields.String()
    default_currency = fields.String()
    symbol_examples = fields.List(fields.String())
    description = fields.String()


# ------------------------------------------------------------------
# 端点
# ------------------------------------------------------------------

@blp.route("/kline")
@blp.arguments(KlineQuerySchema, location="query")
@blp.response(200, KlineResponseSchema)
@blp.doc(
    summary="K 线数据查询",
    description="获取指定市场和品种的 K 线数据，支持多种时间周期和数据条数",
)
def get_kline(args):
    """GET /api/market/kline"""
    market = args["market"]
    symbol = args["symbol"]
    timeframe = args.get("timeframe", "1D")
    limit = min(max(int(args.get("limit", 300)), 1), 5000)
    before_time = args.get("before_time")
    after_time = args.get("after_time")
    refresh = args.get("refresh", False)

    data = market_data_service.get_kline(
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
        before_time=before_time,
        after_time=after_time,
        force_refresh=refresh,
    )

    return {
        "market": market,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(data),
        "data": data,
    }


@blp.route("/ticker")
@blp.arguments(TickerQuerySchema, location="query")
@blp.response(200, TickerResponseSchema)
@blp.doc(
    summary="实时行情",
    description="获取指定品种的最新行情数据",
)
def get_ticker(args):
    """GET /api/market/ticker"""
    market = args["market"]
    symbol = args["symbol"]

    result = market_data_service.get_ticker(market=market, symbol=symbol)
    return result


@blp.route("/search")
@blp.arguments(SearchQuerySchema, location="query")
@blp.response(200, fields.List(fields.Nested(SearchItemSchema)))
@blp.doc(
    summary="品种搜索",
    description="根据关键词搜索指定市场中的品种",
)
def search_symbols(args):
    """GET /api/market/search"""
    market = args["market"]
    keyword = args["keyword"]

    results = market_data_service.search_symbols(market=market, keyword=keyword)
    return results


@blp.route("/list")
@blp.response(200, fields.List(fields.Nested(MarketInfoSchema)))
@blp.doc(
    summary="市场列表",
    description="返回所有支持的市场及其基本信息",
)
def list_market_info():
    """GET /api/market/list"""
    return market_data_service.get_market_overview()
