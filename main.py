"""V2 SELLER: live-account 10%-profit exits, hourly in regular NYSE hours.

Always-on Railway worker (not Railway Cron). 09:35–15:35 Eastern, weekdays;
Alpaca market clock is the authoritative open/closed check. Long US equities
only. No buy logic, stop losses, shorts, trailing orders, options or crypto.
Each sell uses an idempotent per-day client order ID and full held qty.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from zoneinfo import ZoneInfo

LOG=logging.getLogger("v2-seller")
NEW_YORK=ZoneInfo("America/New_York")
TAKE_PROFIT=Decimal("0.10")
Q9=Decimal("0.000000001")
SYMBOL=re.compile(r"^[A-Z][A-Z0-9.\\-]{0,10}$")
MAX_OPEN=500


def flag(name,default):
    value=os.getenv(name,str(default)).strip().lower()
    if value in ("true","yes","1"):return True
    if value in ("false","no","0"):return False
    raise ValueError(name+" must be true or false")


def dec(value):
    try:
        number=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError("Invalid broker numeric position field") from None
    if not number.is_finite():raise ValueError("Nonfinite broker position field")
    return number


def position_profit_trigger(position)->bool:
    """Alpaca unrealized_plpc is a fraction of total entry cost, NOT intraday."""
    try:
        ratio=dec(getattr(position,"unrealized_plpc",None))
        cost=dec(getattr(position,"avg_entry_price",None))
        price=dec(getattr(position,"current_price",None))
        qty=dec(getattr(position,"qty",None))
        if qty<=0 or cost<=0 or price<=0:return False
        # If the broker's ratio and its actual average cost / current-price
        # disagree, fail closed rather than sell a position at a loss.
        computed=price/cost-1
        return ratio>=TAKE_PROFIT and computed>=TAKE_PROFIT
    except ValueError:return False


def unique_id(day,symbol):
    if not SYMBOL.fullmatch(symbol):
        raise ValueError("Unsupported stock ticker")
    return ("v2s-"+day.replace("-","")+"-"+symbol.replace(".","-"))[:48]


def already_submitted(client,cid):
    from alpaca.common.exceptions import APIError
    try:
        return client.get_order_by_client_id(cid) is not None
    except APIError as exc:
        if str(getattr(exc,"status_code",""))=="404":
            return False
        raise


def market_snapshot(client):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    account=client.get_account()
    if (str(getattr(account,"status","")).upper().split(".")[-1]!="ACTIVE"
        or any(bool(getattr(account,x,False)) for x in (
            "account_blocked","trading_blocked","trade_suspended_by_user"))):
        raise RuntimeError("BROKER_ACCOUNT_NOT_ACTIVE")
    orders=client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,limit=MAX_OPEN,nested=True))
    if orders is None or len(orders)>=MAX_OPEN:
        raise RuntimeError("OPEN_ORDERS_UNVERIFIABLE")
    occupied=set()
    def add_order(order):
        sym=str(getattr(order,"symbol","") or "").upper()
        if sym:occupied.add(sym)
        for leg in (getattr(order,"legs",None) or []):
            add_order(leg)
    for o in orders:add_order(o)
    return occupied


def check_once(client,*,dry_run,now=None):
    from alpaca.trading.enums import OrderSide,TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    now=now or datetime.now(timezone.utc)
    clock=client.get_clock()
    if not clock.is_open:
        LOG.info("V2_SELLER_MARKET_CLOSED")
        return {"status":"CLOSED","submitted":0,"eligible":0}
    blocked=market_snapshot(client)
    positions=client.get_all_positions()
    if positions is None:raise RuntimeError("BROKER_POSITIONS_UNVERIFIABLE")
    date=now.astimezone(NEW_YORK).date().isoformat()
    eligible=0
    submitted=0
    previews=0
    for p in positions:
        sym=str(getattr(p,"symbol","")).upper()
        if not SYMBOL.fullmatch(sym):continue
        cls=str(getattr(getattr(p,"asset_class",None),"value",getattr(p,"asset_class",None))).lower()
        side=str(getattr(getattr(p,"side",None),"value",getattr(p,"side",None))).lower()
        if cls!="us_equity" or side!="long":continue
        if not position_profit_trigger(p):continue
        eligible+=1
        if sym in blocked:
            LOG.info("SELL_SKIP_OPEN_ORDER symbol=%s",sym)
            continue
        cid=unique_id(date,sym)
        if already_submitted(client,cid):
            LOG.info("SELL_SKIP_PREVIOUS_ATTEMPT symbol=%s",sym)
            continue
        # The market/account/position could change between reads. Verify
        # the actual current position again before any order write.
        if not client.get_clock().is_open:
            LOG.info("V2_SELLER_MARKET_CLOSED_DURING_RUN")
            break
        current=client.get_open_position(sym)
        if (str(getattr(getattr(current,"side",None),"value",getattr(current,"side",None))).lower()!="long"
            or not position_profit_trigger(current)):
            LOG.info("SELL_SKIP_GAIN_MOVED symbol=%s",sym)
            continue
        if sym in market_snapshot(client):
            LOG.info("SELL_SKIP_OPEN_ORDER_RECHECK symbol=%s",sym)
            continue
        qty=dec(current.qty).quantize(Q9,rounding=ROUND_DOWN)
        if qty<=0:
            LOG.info("SELL_SKIP_ZERO_QTY symbol=%s",sym)
            continue
        if dry_run:
            previews+=1
            LOG.info("SELL_PREVIEW symbol=%s qty=%s broker_gain=%s",
                     sym,qty,current.unrealized_plpc)
            continue
        # Limit to exactly one broker POST; on an ambiguous network error
        # do not retry this run, and reconcile by client ID next cycle.
        request=MarketOrderRequest(symbol=sym,qty=float(qty),
            side=OrderSide.SELL,time_in_force=TimeInForce.DAY,
            client_order_id=cid,extended_hours=False)
        try:
            response=client.submit_order(order_data=request)
        except Exception:
            LOG.error("SELL_ORDER_UNCERTAIN_ABORT symbol=%s cid=%s",sym,cid)
            raise RuntimeError("SELL_ORDER_STATUS_UNCERTAIN_NO_RETRY") from None
        blocked.add(sym)
        submitted+=1
        LOG.warning("LIVE_SELL_SENT symbol=%s cid=%s order_id=%s trigger=GAIN_GE_10_PCT",
                    sym,cid,getattr(response,"id","ACK_PENDING"))
    LOG.info("SELL_CYCLE_COMPLETE eligible=%d submitted=%d preview=%d",
             eligible,submitted,previews)
    return {"status":"OK","eligible":eligible,"submitted":submitted,"preview":previews}


def clients():
    from alpaca.trading.client import TradingClient
    key=os.getenv("APCA_API_KEY_ID","").strip()
    secret=os.getenv("APCA_API_SECRET_KEY","").strip()
    if not key or not secret:raise ValueError("Missing Alpaca credentials")
    paper=flag("ALPACA_PAPER",True)
    dry=flag("DRY_RUN",True)
    if not paper and not dry and os.getenv("LIVE_TRADING_ACK")!="I_ACCEPT_REAL_MONEY_SELLS":
        raise ValueError("Live sell orders require explicit LIVE_TRADING_ACK")
    if flag("ENABLE_LIVE_TRADING",False) and paper:
        raise ValueError("Live flag must not be paired with paper credentials")
    if not paper and not dry and not flag("ENABLE_LIVE_TRADING",False):
        raise ValueError("Live trading must be explicitly enabled")
    return TradingClient(api_key=key,secret_key=secret,paper=paper),dry,paper


def scheduled_check():
    try:
        trader,dry,paper=clients()
        status=check_once(trader,dry_run=dry)
        LOG.info("HOURLY_SELLER_DONE mode=%s dry_run=%s submitted=%d status=%s",
                 "PAPER" if paper else "LIVE",dry,
                 status.get("submitted",0),status["status"])
    except Exception as exc:
        # Never log credentials or full SDK request bodies.
        LOG.error("HOURLY_SELLER_FAILED error_type=%s",type(exc).__name__)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-now",action="store_true",
        help="One read-only preview only; respects Alpaca market-open clock")
    args=parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
        format="%(asctime)s %(levelname)s %(message)s",stream=sys.stdout)
    if args.check_now:
        client,_,_=clients()
        check_once(client,dry_run=True)
        return
    # Validate mode and keys immediately, before claiming service is ready.
    _,dry,paper=clients()
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler=BlockingScheduler(timezone=NEW_YORK,
        job_defaults={"coalesce":True,"max_instances":1,"misfire_grace_time":900})
    scheduler.add_job(scheduled_check,"cron",day_of_week="mon-fri",
        hour="9-15",minute=35,id="v2_seller_hourly",
        replace_existing=True)
    LOG.info("V2_SELLER_STARTED schedule=09:35..15:35 America/New_York"
             " every_hour=TRUE no_stop_loss=TRUE mode=%s dry_run=%s",
             "PAPER" if paper else "LIVE",dry)
    scheduler.start()


if __name__=="__main__":
    main()
