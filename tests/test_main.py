"""Offline seller tests; no keys, real trades, or network connections."""
import os
import sys
import types
import unittest
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import main

NOW=datetime(2026,11,9,19,35,tzinfo=timezone.utc)


def pos(sym="KO",price="111",cost="100",pnl="0.11",qty="0.612345678"):
    return SimpleNamespace(symbol=sym,unrealized_plpc=pnl,
      avg_entry_price=cost,current_price=price,qty=qty,
      asset_class="us_equity",side="long")


class FakeClient:
    def __init__(self,positions=None,orders=None,open_=True):
        self.positions=positions if positions is not None else [pos()]
        self.orders=orders or []
        self.open=open_
        self.submitted=[]
        self.clientids=set()
        self.account=SimpleNamespace(status="ACTIVE",account_blocked=False,
              trading_blocked=False,trade_suspended_by_user=False)
    def get_clock(self):
        return SimpleNamespace(is_open=self.open)
    def get_account(self):
        return self.account
    def get_orders(self,filter=None):
        return self.orders
    def get_all_positions(self):
        return self.positions
    def get_open_position(self,symbol):
        return next(x for x in self.positions if x.symbol==symbol)
    def get_order_by_client_id(self,cid):
        from alpaca.common.exceptions import APIError
        if cid in self.clientids:return SimpleNamespace(id="existing")
        raise APIError("not found")
    def submit_order(self,order_data):
        self.submitted.append(order_data.kwargs)
        self.clientids.add(order_data.kwargs["client_order_id"])
        return SimpleNamespace(id="dummy-broker-order")


def sdk_stub():
    modules={x:types.ModuleType(x) for x in (
        "alpaca","alpaca.trading","alpaca.trading.enums",
        "alpaca.trading.requests","alpaca.common","alpaca.common.exceptions")}
    class OrderRequest:
        def __init__(self,**kwargs):self.kwargs=kwargs
    class APIError(Exception):
        status_code=404
    modules["alpaca.trading.enums"].QueryOrderStatus=SimpleNamespace(OPEN="open")
    modules["alpaca.trading.enums"].OrderSide=SimpleNamespace(SELL="sell")
    modules["alpaca.trading.enums"].TimeInForce=SimpleNamespace(DAY="day")
    modules["alpaca.trading.requests"].GetOrdersRequest=OrderRequest
    modules["alpaca.trading.requests"].MarketOrderRequest=OrderRequest
    modules["alpaca.common.exceptions"].APIError=APIError
    return modules


class SellerTests(unittest.TestCase):
    def setUp(self):
        self.membership_patcher=patch.object(
            main,"load_sp100_membership",return_value={"KO","JNJ","PG","BRK.B"})
        self.membership_patcher.start()
        self.addCleanup(self.membership_patcher.stop)

    def test_exact_profit_threshold(self):
        self.assertFalse(main.position_profit_trigger(pos(price="80",pnl="-0.20")))
        self.assertFalse(main.position_profit_trigger(pos(price="109.99",pnl="0.0999")))
        self.assertTrue(main.position_profit_trigger(pos(price="110",pnl="0.10")))
        self.assertFalse(main.position_profit_trigger(pos(price="109",pnl="0.12")))
        self.assertFalse(main.position_profit_trigger(pos(price="111",pnl="NaN")))
        self.assertFalse(main.position_profit_trigger(pos(price="111",cost="0")))

    def test_submits_exactly_one_live_exit_with_full_fractional_qty(self):
        client=FakeClient()
        with patch.dict(sys.modules,sdk_stub()):
            a=main.check_once(client,dry_run=False,now=NOW)
            b=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(a["submitted"],1)
        self.assertEqual(b["submitted"],0)
        self.assertEqual(len(client.submitted),1)
        x=client.submitted[0]
        self.assertEqual(x["qty"],0.612345678)
        self.assertEqual(x["side"],"sell")
        self.assertEqual(x["time_in_force"],"day")
        self.assertEqual(x["client_order_id"],"v2s-20261109-KO")
        self.assertFalse(x["extended_hours"])
        self.assertNotIn("stop_loss",x)
        self.assertNotIn("take_profit",x)

    def test_dry_run_never_sends(self):
        client=FakeClient()
        with patch.dict(sys.modules,sdk_stub()):
            r=main.check_once(client,dry_run=True,now=NOW)
        self.assertEqual(r["preview"],1)
        self.assertEqual(client.submitted,[])

    def test_no_sale_of_sp100_loser_and_no_short_or_crypto(self):
        client=FakeClient([pos(sym="KO",price="40",pnl="-.6"),
                           pos(sym="SHORT",price="140",pnl=".4"),
                           pos(sym="BTC",price="140",pnl=".4")])
        client.positions[1].side="short"
        client.positions[2].asset_class="crypto"
        with patch.dict(sys.modules,sdk_stub()):
            a=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(a["submitted"],0)


    def test_stop_loss_threshold_uses_both_broker_ratio_and_cost(self):
        self.assertTrue(main.position_stop_loss_trigger(pos(sym="ABCD",price="95",pnl="-0.05")))
        self.assertTrue(main.position_stop_loss_trigger(pos(sym="ABCD",price="94",pnl="-0.06")))
        self.assertFalse(main.position_stop_loss_trigger(pos(sym="ABCD",price="95.1",pnl="-0.049")))
        self.assertFalse(main.position_stop_loss_trigger(pos(sym="ABCD",price="94",pnl="-0.04")))
        self.assertFalse(main.position_stop_loss_trigger(pos(sym="ABCD",price="96",pnl="-0.06")))
        self.assertFalse(main.position_stop_loss_trigger(pos(sym="ABCD",price="94",pnl="NaN")))

    def test_sp100_losing_stock_never_stopped(self):
        client=FakeClient([pos(sym="KO",price="60",pnl="-.4")])
        with patch.dict(sys.modules,sdk_stub()):
            report=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(report["eligible"],0)
        self.assertEqual(client.submitted,[])

    def test_non_sp100_loss_triggers_full_sell_once(self):
        client=FakeClient([pos(sym="QLYS",price="95",pnl="-.05",qty="0.512345678")])
        with patch.dict(sys.modules,sdk_stub()):
            first=main.check_once(client,dry_run=False,now=NOW)
            second=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(first["submitted"],1)
        self.assertEqual(second["submitted"],0)
        self.assertEqual(len(client.submitted),1)
        self.assertEqual(client.submitted[0]["qty"],0.512345678)
        self.assertEqual(client.submitted[0]["client_order_id"],"v2s-20261109-QLYS")

    def test_non_sp100_does_not_stop_before_five_percent(self):
        client=FakeClient([pos(sym="QLYS",price="95.5",pnl="-.045")])
        with patch.dict(sys.modules,sdk_stub()):
            report=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(report["submitted"],0)

    def test_unavailable_membership_disables_only_loss_exits(self):
        client=FakeClient([pos(sym="QLYS",price="80",pnl="-.20"),
                           pos(sym="KO",price="110",pnl=".10")])
        with patch.object(main,"load_sp100_membership",side_effect=RuntimeError("offline")):
            with patch.dict(sys.modules,sdk_stub()):
                report=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(report["submitted"],1)
        self.assertEqual(client.submitted[0]["symbol"],"KO")

    def test_membership_snapshot_validates_freshness_and_completeness(self):
        head=["symbol","close","sma_200","sma_50","pos_52w",
              "dollar_vol_m","score","","last_scan_utc","completed_session_date"]
        rows=[head]+[[f"S{i:03d}"] for i in range(101)]
        rows[1]=["S000",1,1,1,0.5,2,80,"","2026-11-09T16:00:00+00:00","2026-11-06"]
        members,stamp=main.validated_sp100_snapshot(rows,NOW)
        self.assertEqual(len(members),101)
        self.assertIn("S000",members)
        with self.assertRaisesRegex(RuntimeError,"INCOMPLETE"):
            main.validated_sp100_snapshot(rows[:35],NOW)
        with self.assertRaisesRegex(RuntimeError,"STALE"):
            main.validated_sp100_snapshot(
                rows,datetime(2026,11,16,19,35,tzinfo=timezone.utc))
        copy=[r.copy() for r in rows]
        copy[2][0]="S000"
        with self.assertRaisesRegex(RuntimeError,"DUPLICATE"):
            main.validated_sp100_snapshot(copy,NOW)

    def test_closed_market_returns_without_snapshot(self):
        client=FakeClient(open_=False)
        client.get_all_positions=lambda: (_ for _ in ()).throw(AssertionError())
        with patch.dict(sys.modules,sdk_stub()):
            r=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(r["status"],"CLOSED")

    def test_pending_nested_order_blocks_sell(self):
        client=FakeClient(orders=[SimpleNamespace(symbol="AAPL",legs=[
            SimpleNamespace(symbol="KO",legs=None)])])
        with patch.dict(sys.modules,sdk_stub()):
            a=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(a["submitted"],0)

    def test_open_order_truncation_fails_closed(self):
        client=FakeClient(orders=[SimpleNamespace(symbol="QQQ",legs=None)]*500)
        with patch.dict(sys.modules,sdk_stub()):
            with self.assertRaisesRegex(RuntimeError,"OPEN_ORDERS_UNVERIFIABLE"):
                main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(len(client.submitted),0)

    def test_ambiguous_post_no_retry(self):
        client=FakeClient()
        def unknown(order_data):
            raise TimeoutError("mock client error after write")
        client.submit_order=unknown
        with patch.dict(sys.modules,sdk_stub()):
            with self.assertRaisesRegex(RuntimeError,"SELL_ORDER_STATUS_UNCERTAIN_NO_RETRY"):
                main.check_once(client,dry_run=False,now=NOW)

    def test_account_blocked_fails_closed(self):
        client=FakeClient()
        client.account.trading_blocked=True
        with patch.dict(sys.modules,sdk_stub()):
            with self.assertRaisesRegex(RuntimeError,"BROKER_ACCOUNT_NOT_ACTIVE"):
                main.check_once(client,dry_run=False,now=NOW)

    def test_live_trading_requires_ack(self):
        class TradingClient:
            def __init__(self,**kwargs):self.options=kwargs
        modules=sdk_stub()
        modules["alpaca.trading.client"]=types.ModuleType("alpaca.trading.client")
        modules["alpaca.trading.client"].TradingClient=TradingClient
        values={"APCA_API_KEY_ID":"dummy","APCA_API_SECRET_KEY":"dummy",
                "ALPACA_PAPER":"false","DRY_RUN":"false",
                "ENABLE_LIVE_TRADING":"true"}
        with patch.dict(sys.modules,modules),patch.dict(os.environ,values,clear=True):
            with self.assertRaisesRegex(ValueError,"LIVE_TRADING_ACK"):
                main.clients()
            os.environ["LIVE_TRADING_ACK"]="I_ACCEPT_REAL_MONEY_SELLS"
            client,dry,paper=main.clients()
        self.assertFalse(dry)
        self.assertFalse(paper)
        self.assertFalse(client.options["paper"])

    def test_scheduler_uses_ny_market_hours_and_no_cron(self):
        trace={}
        class Stub:
            def __init__(self,**kw):trace["init"]=kw
            def add_job(self,*a,**kw):trace["job"]=(a,kw)
            def start(self):trace["start"]=True
        mods=sdk_stub()
        for name in ("apscheduler","apscheduler.schedulers","apscheduler.schedulers.blocking"):
            mods[name]=types.ModuleType(name)
        mods["apscheduler.schedulers.blocking"].BlockingScheduler=Stub
        with patch.dict(sys.modules,mods), patch("sys.argv",["main.py"]):
            with patch.object(main,"clients",return_value=(None,False,False)):
                main.main()
        self.assertTrue(trace["start"])
        self.assertEqual(trace["init"]["timezone"],main.NEW_YORK)
        self.assertEqual(trace["job"][1]["day_of_week"],"mon-fri")
        self.assertEqual(trace["job"][1]["hour"],"9-15")
        self.assertEqual(trace["job"][1]["minute"],35)


if __name__=="__main__":
    unittest.main()
