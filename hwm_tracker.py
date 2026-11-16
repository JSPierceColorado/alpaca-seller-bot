"""Persistent position high-water gain history for the V2 seller.

No order APIs or order instructions live in this module. A gain is measured against the currently
reported broker average cost. Peaks start when observation is enabled; they
are NOT historical/intraday highs. New purchases or a changed cost basis
rebase the tracked position rather than importing an old position's peak.
"""
from __future__ import annotations

import logging
from contextlib import closing
import os
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

LOG=logging.getLogger("v2-seller-hwm")
DB_PATH="/data/seller-highwater.sqlite3"
ARM_PCT=Decimal("13")
GIVEBACK_POINTS=Decimal("3")
MIN_GAIN_PCT=Decimal("10")
TICKER=re.compile(r"^[A-Z][A-Z0-9.\-]{0,10}$")


def number(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def connect(path=DB_PATH):
    folder=os.path.dirname(os.path.abspath(path))
    os.makedirs(folder,exist_ok=True)
    connection=sqlite3.connect(path,timeout=15)
    connection.row_factory=sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("""CREATE TABLE IF NOT EXISTS active_peaks(
        symbol TEXT PRIMARY KEY, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, avg_entry TEXT NOT NULL,
        last_qty TEXT NOT NULL, peak_price TEXT NOT NULL,
        peak_gain_pct TEXT NOT NULL, peak_seen TEXT NOT NULL,
        current_gain_pct TEXT NOT NULL, observations INTEGER NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS archived_peaks(
        symbol TEXT NOT NULL, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, ended_at TEXT NOT NULL,
        peak_gain_pct TEXT NOT NULL, reason TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS tracker_meta(
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    # Retain the price path to evaluate alternative giveback rules later.
    connection.execute("""CREATE TABLE IF NOT EXISTS peak_samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, episode_start TEXT NOT NULL,
        observed_at TEXT NOT NULL, avg_entry TEXT NOT NULL,
        qty TEXT NOT NULL, current_price TEXT NOT NULL,
        gain_pct TEXT NOT NULL, peak_gain_pct TEXT NOT NULL)""")
    connection.execute("""CREATE INDEX IF NOT EXISTS sample_symbol_episode_time
        ON peak_samples(symbol,episode_start,observed_at)""")
    connection.commit()
    return connection


def metric(position):
    """Require agreement between broker-reported and cost-based gains."""
    symbol=str(getattr(position,"symbol","") or "").upper()
    asset=getattr(position,"asset_class",None)
    side=getattr(position,"side",None)
    asset=str(getattr(asset,"value",asset)).lower()
    side=str(getattr(side,"value",side)).lower()
    if not TICKER.fullmatch(symbol) or asset!="us_equity" or side!="long":
        return None
    entry=number(getattr(position,"avg_entry_price",None))
    price=number(getattr(position,"current_price",None))
    qty=number(getattr(position,"qty",None))
    broker=number(getattr(position,"unrealized_plpc",None))
    if entry is None or price is None or qty is None or broker is None:
        return None
    if entry<=0 or price<=0 or qty<=0:return None
    gain=(price/entry-1)*Decimal(100)
    if abs(gain-broker*Decimal(100))>Decimal("1"):
        # Do not record a high mark on stale/conflicting quote/cost fields.
        return None
    return {"symbol":symbol,"entry":entry,"price":price,"qty":qty,"gain":gain}


def active_peak_records(*, path=DB_PATH):
    """Read only the current portfolio's persisted position-episode peaks.

    Never synthesize a missing high-water mark. Storage failures propagate to
    the caller, which must disable gain exits rather than sell at +10%.
    """
    with closing(connect(path)) as conn:
        return {row["symbol"]:dict(row) for row in conn.execute(
            "SELECT symbol,first_seen,last_seen,avg_entry,last_qty,"
            "peak_price,peak_gain_pct,peak_seen,current_gain_pct,observations "
            "FROM active_peaks")}


def observe(positions, *, path=DB_PATH, at=None):
    """Persist high-water mark for observed current positions, with no trades."""
    at=at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Observation time must include timezone")
    stamp=at.astimezone(timezone.utc).isoformat()
    summary={"observed":0,"new":0,"new_highs":0,"rebased":0,
             "disappeared":0,"armed":0,"shadow_givebacks":0,
             "invalid":0}
    # A completely empty response might be a transient brokerage glitch.
    # Do not erase the entire record from a single empty list.
    valid={}
    all_equity=set()
    for position in positions:
        symbol=str(getattr(position,"symbol","") or "").upper()
        cls=getattr(position,"asset_class",None)
        side=getattr(position,"side",None)
        if TICKER.fullmatch(symbol) and str(getattr(cls,"value",cls)).lower()=="us_equity" and str(getattr(side,"value",side)).lower()=="long":
            all_equity.add(symbol)
            info=metric(position)
            if info is None:
                summary["invalid"]+=1
            else:
                valid[symbol]=info
    if not all_equity:
        with closing(connect(path)) as conn, conn:
            old=conn.execute("SELECT value FROM tracker_meta WHERE key='empty_observations'").fetchone()
            consecutive=int(old["value"]) if old else 0
            consecutive+=1
            conn.execute("INSERT OR REPLACE INTO tracker_meta VALUES('empty_observations',?)",(str(consecutive),))
            if consecutive>=2:
                for row in conn.execute("SELECT * FROM active_peaks"):
                    conn.execute("INSERT INTO archived_peaks VALUES(?,?,?,?,?,?)",
                        (row["symbol"],row["first_seen"],row["last_seen"],stamp,
                         row["peak_gain_pct"],"POSITION_ABSENT_TWICE"))
                    summary["disappeared"]+=1
                conn.execute("DELETE FROM active_peaks")
        LOG.info("HWM_SNAPSHOT_EMPTY consecutive=%s disappeared=%d",consecutive,summary["disappeared"])
        return summary
    with closing(connect(path)) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO tracker_meta VALUES('empty_observations','0')")
        known={row["symbol"]:row for row in conn.execute("SELECT * FROM active_peaks")}
        for symbol,info in valid.items():
            summary["observed"]+=1
            old=known.get(symbol)
            entry=info["entry"];price=info["price"];qty=info["qty"];gain=info["gain"]
            if old:
                old_entry=Decimal(old["avg_entry"])
                old_qty=Decimal(old["last_qty"])
                changed_cost=abs(entry-old_entry)>max(Decimal(".000001"),old_entry*Decimal(".00001"))
                bigger_position=qty>old_qty+Decimal(".0000001")
                if changed_cost or bigger_position:
                    conn.execute("INSERT INTO archived_peaks VALUES(?,?,?,?,?,?)",
                      (symbol,old["first_seen"],old["last_seen"],stamp,
                       old["peak_gain_pct"],"COST_BASIS_CHANGED_OR_ADDED"))
                    conn.execute("DELETE FROM active_peaks WHERE symbol=?",(symbol,))
                    old=None
                    summary["rebased"]+=1
            if old is None:
                conn.execute("INSERT INTO active_peaks VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (symbol,stamp,stamp,str(entry),str(qty),
                              str(price),str(gain),stamp,str(gain),1))
                summary["new"]+=1
                peak=gain
                LOG.info("HWM_TRACK_NEW symbol=%s observed_gain_pct=%s",symbol,round(gain,3))
            else:
                peak=Decimal(old["peak_gain_pct"])
                if gain>peak:
                    peak=gain;summary["new_highs"]+=1
                    conn.execute("""UPDATE active_peaks SET last_seen=?,
                        avg_entry=?,last_qty=?,peak_price=?,peak_gain_pct=?,
                        peak_seen=?,current_gain_pct=?,observations=observations+1
                        WHERE symbol=?""",
                        (stamp,str(entry),str(qty),str(price),str(peak),
                         stamp,str(gain),symbol))
                    LOG.info("HWM_NEW_HIGH symbol=%s peak_gain_pct=%s",symbol,round(peak,3))
                else:
                    conn.execute("""UPDATE active_peaks SET last_seen=?,
                        avg_entry=?,last_qty=?,current_gain_pct=?,
                        observations=observations+1 WHERE symbol=?""",
                        (stamp,str(entry),str(qty),str(gain),symbol))
            # Store every valid observation, not just record highs. The
            # interval path matters for evaluating future giveback choices.
            conn.execute("""INSERT INTO peak_samples(
                symbol,episode_start,observed_at,avg_entry,qty,current_price,
                gain_pct,peak_gain_pct) VALUES(?,?,?,?,?,?,?,?)""",
                (symbol,old["first_seen"] if old else stamp,stamp,
                 str(entry),str(qty),str(price),str(gain),str(peak)))
            if peak>=ARM_PCT:
                summary["armed"]+=1
                trailing_exit=max(MIN_GAIN_PCT,peak-GIVEBACK_POINTS)
                if MIN_GAIN_PCT<=gain<=trailing_exit:
                    summary["shadow_givebacks"]+=1
                    LOG.info("HWM_GIVEBACK_OBSERVED symbol=%s gain_pct=%s peak_pct=%s trigger_pct=%s no_order_from_observer=TRUE",
                             symbol,round(gain,3),round(peak,3),round(trailing_exit,3))
        for symbol,old in known.items():
            if symbol not in all_equity:
                conn.execute("INSERT INTO archived_peaks VALUES(?,?,?,?,?,?)",
                      (symbol,old["first_seen"],old["last_seen"],stamp,
                       old["peak_gain_pct"],"NOT_HELD_AT_OBSERVATION"))
                conn.execute("DELETE FROM active_peaks WHERE symbol=?",(symbol,))
                summary["disappeared"]+=1
    LOG.info("HWM_SAMPLE_COMPLETE %s", " ".join("%s=%d"%(k,v) for k,v in summary.items()))
    return summary
