"""Fill persistence helpers used by pending order execution."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.services.live_trading.leg_context import resolve_leg_context
from app.services.live_trading.records import (
    apply_fill_to_local_position,
    record_trade,
)
from app.services.pending_orders.position_sync_cache import invalidate_position_sync_snapshot_for_exchange
from app.utils.logger import get_logger
from app.utils.trade_close_reason import is_exit_trade_type

logger = get_logger(__name__)


def persist_strategy_fill(
    *,
    strategy_id: int,
    symbol: str,
    signal_type: str,
    filled: float,
    avg_price: float,
    exchange_config: Dict[str, Any],
    market_type: str,
    order_id: int = 0,
    fill_source: str = "worker",
    commission: float = 0.0,
    commission_ccy: str = "",
    profit: Optional[float] = None,
    close_reason: str = "",
    matched_entry_price: Optional[float] = None,
    grid_matched_profit: Optional[float] = None,
    inst_id: str = "",
) -> Tuple[Optional[float], Optional[float]]:
    """Apply a fill to local positions and append a trade row."""
    filled_qty = float(filled or 0.0)
    avg_px = float(avg_price or 0.0)
    if abs(filled_qty) <= 1e-12:
        logger.info(
            "Skip zero-sized strategy fill: strategy_id=%s symbol=%s signal=%s order_id=%s source=%s",
            strategy_id,
            symbol,
            signal_type,
            order_id,
            fill_source,
        )
        return profit, matched_entry_price

    leg = resolve_leg_context(
        strategy_id=int(strategy_id),
        symbol=str(symbol or ""),
        exchange_config=exchange_config,
        market_type=str(market_type or "swap"),
        inst_id=str(inst_id or ""),
        fill_source=str(fill_source or "worker"),
        pending_order_id=int(order_id or 0),
    )
    profit_out, _pos, matched_entry = apply_fill_to_local_position(
        strategy_id=int(strategy_id),
        symbol=str(symbol or ""),
        signal_type=str(signal_type or ""),
        filled=filled_qty,
        avg_price=avg_px,
        leg=leg,
    )
    if profit is None:
        profit = profit_out
    if matched_entry_price is None:
        matched_entry_price = matched_entry

    record_trade(
        strategy_id=int(strategy_id),
        symbol=str(symbol or ""),
        trade_type=str(signal_type or ""),
        price=avg_px,
        amount=filled_qty,
        commission=float(commission or 0.0),
        commission_ccy=str(commission_ccy or ""),
        profit=profit,
        close_reason=str(close_reason or ""),
        matched_entry_price=matched_entry_price,
        grid_matched_profit=grid_matched_profit if grid_matched_profit is not None else profit,
        leg=leg,
    )

    try:
        from app.services.live_trading.records import _get_user_id_from_strategy

        invalidate_position_sync_snapshot_for_exchange(
            user_id=_get_user_id_from_strategy(int(strategy_id)),
            exchange_id=str(exchange_config.get("exchange_id") or "").strip().lower(),
            market_type=str(market_type or "swap"),
            exchange_config=exchange_config if isinstance(exchange_config, dict) else {},
        )
    except Exception:
        pass
    return profit, matched_entry_price


def trade_close_reason_from_payload(payload: Dict[str, Any], signal_type: str) -> str:
    """Return the close reason only for exit-like trade types."""
    if is_exit_trade_type(str(signal_type or "")):
        return str((payload or {}).get("reason") or "").strip()
    return ""
