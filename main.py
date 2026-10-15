"""One-shot Alpaca risk-exit job. Designed for Railway Cron, NOT a web server.

The service exits after each check. Defaults are paper trading and dry-run.
Only long US stock/ETF positions are eligible for closing.
"""

from __future__ import annotations

import logging
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

LOGGER = logging.getLogger("alpaca-seller-bot")
STOP_LOSS_PCT = Decimal("-0.05")
TAKE_PROFIT_PCT = Decimal("0.10")
MAX_OPEN_ORDER_RESULTS = 500


def env_flag(name: str, default: bool) -> bool:
    """Parse an environment variable strictly to avoid accidental live orders."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false (received an invalid value)")


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def exit_reason(unrealized_plpc: Any) -> str | None:
    """Alpaca reports unrealized_plpc as a fraction: -0.05 == -5%."""
    if unrealized_plpc is None:
        return None
    try:
        ratio = Decimal(str(unrealized_plpc))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not ratio.is_finite():
        return None
    if ratio <= STOP_LOSS_PCT:
        return "STOP_LOSS"
    if ratio >= TAKE_PROFIT_PCT:
        return "TAKE_PROFIT"
    return None


def order_symbols(order: Any) -> set[str]:
    """Include nested bracket/OTO legs when finding open orders."""
    symbols: set[str] = set()
    symbol = getattr(order, "symbol", None)
    if symbol:
        symbols.add(str(symbol).upper())
    for leg in getattr(order, "legs", None) or []:
        symbols.update(order_symbols(leg))
    return symbols


def run_check(client: Any, dry_run: bool) -> int:
    """Check positions once and return the number of close requests submitted."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    clock = client.get_clock()
    if not clock.is_open:
        LOGGER.info("Market closed at %s; no position checks or orders", clock.timestamp)
        return 0

    positions = client.get_all_positions()
    LOGGER.info("Market open; checking %d position(s); dry_run=%s", len(positions), dry_run)
    if not positions:
        return 0

    # Use one consistent snapshot of open orders. If it reaches the API limit,
    # fail closed rather than risk missing an existing exit order.
    orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=MAX_OPEN_ORDER_RESULTS, nested=True)
    )
    if len(orders) >= MAX_OPEN_ORDER_RESULTS:
        raise RuntimeError("Too many open orders to verify safely; no close orders submitted")

    blocked_symbols: set[str] = set()
    for order in orders:
        blocked_symbols.update(order_symbols(order))

    submissions = 0
    errors = 0
    for position in positions:
        symbol = str(position.symbol).upper()
        if enum_value(position.asset_class) != "us_equity":
            LOGGER.info("SKIP %s: not a US stock/ETF (%s)", symbol, enum_value(position.asset_class))
            continue
        if enum_value(position.side) != "long":
            LOGGER.info("SKIP %s: not a long position", symbol)
            continue
        pnl_ratio = getattr(position, "unrealized_plpc", None)
        if not is_valid_decimal(pnl_ratio):
            LOGGER.warning("SKIP %s: missing/invalid unrealized_plpc=%r", symbol, pnl_ratio)
            continue
        reason = exit_reason(pnl_ratio)
        LOGGER.info("CHECK %s qty=%s unrealized_plpc=%s trigger=%s", symbol, position.qty, pnl_ratio, reason or "NONE")
        if reason is None:
            continue
        if symbol in blocked_symbols:
            LOGGER.warning("SKIP %s: an open order already exists; do not interfere", symbol)
            continue
        if dry_run:
            LOGGER.warning("DRY RUN %s: would close entire position (%s, pnl=%s)", symbol, reason, pnl_ratio)
            continue
        try:
            # This uses Alpaca's close-position endpoint, which submits an
            # order to liquidate the ENTIRE position, including fractional qty.
            order = client.close_position(symbol)
            blocked_symbols.add(symbol)
            submissions += 1
            LOGGER.warning("SUBMITTED %s: %s pnl=%s order_id=%s", symbol, reason, pnl_ratio, order.id)
        except Exception:
            errors += 1
            LOGGER.exception("FAILED %s: close order rejected or API unavailable", symbol)

    if errors:
        raise RuntimeError(f"{errors} close-position request(s) failed; inspect Railway logs")
    LOGGER.info("Run complete: %d close order(s) submitted", submissions)
    return submissions


def is_valid_decimal(value: Any) -> bool:
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, TypeError, ValueError):
        return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        paper = env_flag("ALPACA_PAPER", True)
        dry_run = env_flag("DRY_RUN", True)
        allow_live = env_flag("ENABLE_LIVE_TRADING", False)
        if not paper and not dry_run and not allow_live:
            raise ValueError(
                "Real-money orders are disabled. For live mode set ALPACA_PAPER=false, "
                "DRY_RUN=false, and ENABLE_LIVE_TRADING=true intentionally."
            )
        key = os.getenv("APCA_API_KEY_ID", "").strip()
        secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
        if not key or not secret:
            raise ValueError("Missing APCA_API_KEY_ID or APCA_API_SECRET_KEY in Railway Variables")
        from alpaca.trading.client import TradingClient

        client = TradingClient(api_key=key, secret_key=secret, paper=paper)
        LOGGER.info("Starting one-shot risk check: paper=%s dry_run=%s", paper, dry_run)
        run_check(client, dry_run=dry_run)
        return 0
    except Exception:
        LOGGER.exception("Job failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
