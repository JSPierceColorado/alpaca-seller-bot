"""Read-only performance comparison for core VT/SGOV vs active remainder.

Both sleeves share one margin account. Core market value and dividend cash
are virtual; active value equals total account equity less core value.
Core buy fills transfer capital from active to core. External cash transfers
enter the active sleeve first. Only confirmed Alpaca fills and after-close
snapshots are accepted. This is NOT an independently margined account.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

CORE={"VT","SGOV"}
TRANS={"CSD","CSW"}
DIVS={"DIV","DIVCGL","DIVCGS","DIVROC","DIVTXEX",
      "DIVFEE","DIVFT","DIVNRA","DIVTW"}


def num(value):
    try:
        x=Decimal(str(value))
        if x.is_finite():return x
    except (TypeError,InvalidOperation,ValueError):
        pass
    raise ValueError("INVALID_CORE_METRIC")


def result(status,reason,**more):
    return {"status":status,"reason":reason,**more}


def compare(account_closes,core_positions,activities,order_client_ids,
            dateval,activity_complete,account_reconciled):
    """Return two time-weighted indexes starting at the first funded close."""
    if not activity_complete or not account_reconciled:
        return result("suspended","ACTIVITY_OR_ACCOUNT_CASHFLOW_UNVERIFIED",series=[])
    flows=defaultdict(lambda:Decimal(0))
    core_flows=defaultdict(lambda:Decimal(0))
    dividends=defaultdict(lambda:Decimal(0))
    filled=defaultdict(lambda:Decimal(0))
    issues=set()
    core_fills=[]
    unknown_fills=[]
    for a in activities:
        typ=str(a.get("activity_type","")).upper()
        day=dateval(a.get("transaction_time") or a.get("date"))
        if not day:
            issues.add("UNKNOWN_ACTIVITY_DATE")
            continue
        if typ in TRANS:
            amt=num(a.get("net_amount"))
            if typ=="CSD":
                if amt<=0:issues.add("INVALID_CSD")
                flows[day]+=abs(amt)
            else:flows[day]-=abs(amt)
        elif typ=="FILL" and str(a.get("symbol","")).upper() in CORE:
            symbol=str(a.get("symbol")).upper()
            cid=str(order_client_ids.get(str(a.get("order_id","")),""))
            qty=num(a.get("qty"));price=num(a.get("price"))
            if qty<=0 or price<=0:
                issues.add("CORE_FILL_INVALID")
                continue
            if not cid.startswith("v2core-") or not cid.endswith("-"+symbol):
                unknown_fills.append(day)
                continue
            if str(a.get("side","")).lower()!="buy":
                issues.add("CORE_SELL_DETECTED")
                continue
            core_flows[day]+=qty*price
            filled[(day,symbol)]+=qty
            core_fills.append(day)
        elif typ in DIVS and str(a.get("symbol","")).upper() in CORE:
            dividends[day]+=num(a.get("net_amount"))
    if not core_fills:
        if any(num(p.get("qty","0"))>0 for d,items in core_positions.items()
               for sym,p in items.items() if sym in CORE):
            return result("suspended","CORE_HOLDINGS_WITHOUT_TRACKED_FILLS",series=[])
        return result("awaiting_first_fill","NO_CORE_BUYS_YET",series=[])
    first=min(core_fills)
    if any(day>=first for day in unknown_fills):
        issues.add("UNATTRIBUTED_CORE_ETF_FILL")
    closes=sorted((x for x in account_closes if x["date"]>=first),
                  key=lambda x:x["date"])
    if len(closes)<2 or closes[0]["date"]!=first:
        return result("awaiting_closes","NEED_TWO_VERIFIED_CORE_DAILY_CLOSES",series=[])
    days=[x["date"] for x in closes]
    for prior,following in zip(days,days[1:]):
        if (datetime.strptime(following,"%Y-%m-%d")-
            datetime.strptime(prior,"%Y-%m-%d")).days>4:
            issues.add("CORE_CLOSING_HISTORY_GAP")
    def target(day):
        return next((x for x in days if x>=day),None)
    mapped={}
    for original in (flows,core_flows,dividends):
        dest=defaultdict(lambda:Decimal(0))
        for day,v in original.items():
            if day<first:continue
            dest_day=target(day)
            if dest_day is None:
                if v:issues.add("CORE_CASHFLOW_BEYOND_LAST_CLOSE")
            else:dest[dest_day]+=v
        mapped[id(original)]=dest
    ext=mapped[id(flows)]
    core_cash=mapped[id(core_flows)]
    div_cash=mapped[id(dividends)]
    shares={"VT":Decimal(0),"SGOV":Decimal(0)}
    by_day=defaultdict(list)
    for (day,symbol),qty in filled.items():
        mapped_day=target(day)
        if mapped_day:by_day[mapped_day].append((symbol,qty))
    core_index=Decimal(100);active_index=Decimal(100)
    prev_core=None;prev_active=None
    cumulative_div=Decimal(0);contributed=Decimal(0)
    series=[]
    for snap in closes:
        day=snap["date"]
        for sym,qty in by_day[day]:shares[sym]+=qty
        current=core_positions.get(day,{})
        mark=Decimal(0)
        for sym in CORE:
            p=current.get(sym)
            actual=num(p.get("qty")) if p else Decimal(0)
            if abs(actual-shares[sym])>Decimal(".0001"):
                issues.add("CORE_QTY_DISAGREES_"+day+"_"+sym)
            if p:mark+=num(p.get("market_value"))
        cumulative_div+=div_cash[day]
        core_nav=mark+cumulative_div
        active_nav=num(snap["equity"])-core_nav
        if core_nav<0 or active_nav<=0:issues.add("NAV_NEGATIVE_"+day)
        cflow=core_cash[day]
        flow=ext[day]
        contributed+=cflow
        if prev_core is not None:
            if prev_core<=0 or prev_active<=0:
                issues.add("PRIOR_NAV_INVALID_"+day)
            else:
                cr=(core_nav-cflow)/prev_core
                ar=(active_nav-flow+cflow)/prev_active
                if cr<=0 or ar<=0:issues.add("TWR_FACTOR_INVALID_"+day)
                else:
                    core_index*=cr
                    active_index*=ar
        series.append({"date":day,
                      "core_index":round(float(core_index),5),
                      "active_index":round(float(active_index),5),
                      "core_equity":round(float(core_nav),2),
                      "active_equity":round(float(active_nav),2),
                      "core_contribution":round(float(cflow),2),
                      "external_cashflow":round(float(flow),2),
                      "cumulative_dividend_cash":round(float(cumulative_div),2)})
        prev_core=core_nav;prev_active=active_nav
    if issues:return result("suspended",";".join(sorted(issues)),series=[])
    return result("ready","CASHFLOW_ADJUSTED_AFTER_CLOSE",series=series,
                  benchmark_start=series[0]["date"],
                  core_return_pct=round(float(core_index-100),3),
                  active_return_pct=round(float(active_index-100),3),
                  core_contributions=round(float(contributed),2),
                  core_equity=series[-1]["core_equity"],
                  active_equity=series[-1]["active_equity"],
                  dividends=series[-1]["cumulative_dividend_cash"],
                  caveat="Same margin account; remainder includes unassigned cash "
                         "and manual trades; dividend cash is virtual, not ring-fenced.")
