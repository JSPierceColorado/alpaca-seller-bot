"""Offline seller tests; no keys, real trades, or network connections."""
import os
import tempfile
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
    class ForbiddenMarketSell:
        def __init__(self,**kwargs):
            raise AssertionError("Profit-only seller may never construct a market sell")
    modules["alpaca.trading.requests"].MarketOrderRequest=ForbiddenMarketSell
    modules["alpaca.trading.requests"].LimitOrderRequest=OrderRequest
    modules["alpaca.common.exceptions"].APIError=APIError
    return modules


class SellerTests(unittest.TestCase):
    def setUp(self):
        self.membership_patcher=patch.object(
            main,"load_sp100_membership",return_value={"KO","JNJ","PG","BRK.B"})
        self.membership_patcher.start()
        self.addCleanup(self.membership_patcher.stop)
        self.tmpdir=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbpath=os.path.join(self.tmpdir.name,"seller-peaks.sqlite3")
        db_patcher=patch.object(main.hwm_tracker,"DB_PATH",self.dbpath)
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

    def seed_peak(self,position=None,peak_price="115"):
        position=position or pos()
        old_price=position.current_price
        old_ratio=position.unrealized_plpc
        entry=Decimal(position.avg_entry_price)
        position.current_price=peak_price
        position.unrealized_plpc=str(Decimal(peak_price)/entry-1)
        main.hwm_tracker.observe([position],path=self.dbpath,at=NOW)
        position.current_price=old_price
        position.unrealized_plpc=old_ratio

    def test_exact_profit_threshold(self):
        self.assertFalse(main.position_profit_trigger(pos(price="80",pnl="-0.20")))
        self.assertFalse(main.position_profit_trigger(pos(price="109.99",pnl="0.0999")))
        self.assertTrue(main.position_profit_trigger(pos(price="110",pnl="0.10")))
        self.assertFalse(main.position_profit_trigger(pos(price="109",pnl="0.12")))
        self.assertFalse(main.position_profit_trigger(pos(price="111",pnl="NaN")))
        self.assertFalse(main.position_profit_trigger(pos(price="111",cost="0")))

    def test_submits_exactly_one_live_exit_with_full_fractional_qty(self):
        client=FakeClient()
        self.seed_peak(client.positions[0])
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
        self.assertEqual(x["client_order_id"],"v2sg-20261109-KO")
        self.assertFalse(x["extended_hours"])
        self.assertEqual(x["limit_price"],110.0)
        self.assertNotIn("stop_loss",x)
        self.assertNotIn("take_profit",x)

    def test_dry_run_never_sends(self):
        client=FakeClient()
        self.seed_peak(client.positions[0])
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


    def test_protected_core_etfs_never_sell_at_any_gain_or_loss(self):
        for symbol in ("VT","SGOV"):
            for price in ("40","100","110","113","120","150"):
                p=pos(sym=symbol,price=price,pnl=str(Decimal(price)/100-1))
                self.seed_peak(p,peak_price="150")
                self.assertIsNone(main.exit_trigger(
                    p,symbol,main.hwm_tracker.active_peak_records(path=self.dbpath).get(symbol)))
                broker=FakeClient([p])
                with patch.dict(sys.modules,sdk_stub()):
                    result=main.check_once(broker,dry_run=False,now=NOW)
                self.assertEqual(result["submitted"],0,(symbol,price))
                self.assertEqual(broker.submitted,[])

    def test_protected_and_regular_profitable_in_same_cycle(self):
        core=pos(sym="VT",price="110",pnl=".10")
        regular=pos(sym="UPST",price="110",pnl=".10")
        self.seed_peak(core,peak_price="113")
        self.seed_peak(regular,peak_price="113")
        broker=FakeClient([core,regular])
        with patch.dict(sys.modules,sdk_stub()):
            result=main.check_once(broker,dry_run=False,now=NOW)
        self.assertEqual(result["submitted"],1)
        self.assertEqual(len(broker.submitted),1)
        self.assertEqual(broker.submitted[0]["symbol"],"UPST")

    def test_no_loss_based_exit_for_any_equity_even_deep_drawdowns(self):
        for symbol in ("KO","QLYS","AMRC","TTMI","UPST"):
            for price in ("95","94","50","1"):
                p=pos(sym=symbol,price=price,
                      pnl=str(Decimal(price)/Decimal(100)-1))
                self.assertIsNone(main.exit_trigger(p,symbol))
                broker=FakeClient([p])
                with patch.dict(sys.modules,sdk_stub()):
                    result=main.check_once(broker,dry_run=False,now=NOW)
                self.assertEqual(result["submitted"],0,(symbol,price))
                self.assertEqual(broker.submitted,[])

    def test_sp100_losing_stock_never_stopped(self):
        client=FakeClient([pos(sym="KO",price="60",pnl="-.4")])
        with patch.dict(sys.modules,sdk_stub()):
            report=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(report["eligible"],0)
        self.assertEqual(client.submitted,[])

    def test_non_sp100_loss_never_triggers_sell_even_at_threshold(self):
        client=FakeClient([pos(sym="QLYS",price="95",pnl="-.05",qty="0.512345678")])
        with patch.dict(sys.modules,sdk_stub()):
            first=main.check_once(client,dry_run=False,now=NOW)
            second=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(first["submitted"],0)
        self.assertEqual(second["submitted"],0)
        self.assertEqual(client.submitted,[])

    def test_non_sp100_does_not_stop_before_five_percent(self):
        client=FakeClient([pos(sym="QLYS",price="95.5",pnl="-.045")])
        with patch.dict(sys.modules,sdk_stub()):
            report=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(report["submitted"],0)

    def test_seller_does_not_need_sp100_membership_to_take_profit(self):
        client=FakeClient([pos(sym="QLYS",price="80",pnl="-.20"),
                           pos(sym="UPST",price="110",pnl=".10")])
        self.seed_peak(client.positions[1],peak_price="113")
        with patch.object(main,"load_sp100_membership",
                          side_effect=RuntimeError("offline")) as membership:
            with patch.dict(sys.modules,sdk_stub()):
                report=main.check_once(client,dry_run=False,now=NOW)
        membership.assert_not_called()
        self.assertEqual(report["submitted"],1)
        self.assertEqual(client.submitted[0]["symbol"],"UPST")
        self.assertEqual(client.submitted[0]["client_order_id"],"v2sg-20261109-UPST")
        self.assertIn("limit_price",client.submitted[0])


    def test_seller_order_ids_label_stop_not_profit(self):
        self.assertEqual(main.unique_id("2026-11-16","TTMI","LOSS_LE_5_PCT_NON_SP100"),
                         "v2sl-20261116-TTMI")
        self.assertEqual(main.unique_id("2026-11-16","TTMI","GAIN_TRAILING_3PTS_MIN_10PCT"),
                         "v2sg-20261116-TTMI")
        self.assertEqual(len(main.daily_sell_ids("2026-11-16","TTMI")),3)
        self.assertIn("v2s-20261116-TTMI",main.daily_sell_ids("2026-11-16","TTMI"))

    def test_historical_stop_id_prevents_same_day_duplicate_profit_sell(self):
        position=pos(sym="QLYS",price="110",pnl=".10")
        client=FakeClient([position])
        self.seed_peak(position,peak_price="113")
        client.clientids.add("v2sl-20261109-QLYS")
        with patch.dict(sys.modules,sdk_stub()):
            result=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(result["submitted"],0)
        self.assertEqual(client.submitted,[])

    def test_profit_does_not_sell_at_initial_ten_twelve_or_thirteen(self):
        for pct in ("0.10","0.12","0.13"):
            price=str(Decimal("100")*(Decimal(1)+Decimal(pct)))
            client=FakeClient([pos(price=price,pnl=pct)])
            with patch.dict(sys.modules,sdk_stub()):
                result=main.check_once(client,dry_run=False,now=NOW)
            self.assertEqual(result["submitted"],0,
                             "No immediate fixed 10% profit sell")
            self.assertEqual(client.submitted,[])

    def test_13_peak_then_10_giveback_sells_at_limit_floor(self):
        current=pos(sym="KO",price="110",pnl="0.10",qty="1.25")
        self.seed_peak(current,peak_price="113")
        client=FakeClient([current])
        with patch.dict(sys.modules,sdk_stub()):
            out=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(out["submitted"],1)
        order=client.submitted[0]
        self.assertEqual(order["limit_price"],110)
        self.assertEqual(order["qty"],1.25)

    def test_20_peak_then_17_sells_and_17point01_does_not(self):
        for price,should_submit in (("117.01",0),("117",1)):
            current=pos(sym="KO",price=price,
                        pnl=str(Decimal(price)/Decimal(100)-1))
            self.seed_peak(current,peak_price="120")
            broker=FakeClient([current])
            with patch.dict(sys.modules,sdk_stub()):
                out=main.check_once(broker,dry_run=False,now=NOW)
            self.assertEqual(out["submitted"],should_submit)
            if should_submit:
                self.assertEqual(broker.submitted[0]["limit_price"],110)

    def test_armed_peak_does_not_order_if_gain_below_10(self):
        current=pos(price="109",pnl=".09")
        self.seed_peak(current,peak_price="120")
        broker=FakeClient([current])
        with patch.dict(sys.modules,sdk_stub()):
            self.assertEqual(main.check_once(broker,dry_run=False,now=NOW)["submitted"],0)
        self.assertEqual(broker.submitted,[])

    def test_profit_limit_rounded_up_not_below_ten_percent(self):
        self.assertEqual(main.profit_floor_limit(pos(price="115",cost="100.005",pnl=".14")),Decimal("110.01"))
        self.assertEqual(main.profit_floor_limit(pos(price="0.95",cost="0.8",pnl=".1875")),Decimal(".88"))
        self.assertEqual(main.profit_floor_limit(pos(price="1.20",cost="1.01",pnl=".188")),Decimal("1.12"))

    def test_storage_unavailable_disables_all_exits_without_profit_proof(self):
        client=FakeClient([pos(sym="KO",price="110",pnl=".10"),
                           pos(sym="QLYS",price="94",pnl="-.06")])
        with patch.dict(sys.modules,sdk_stub()),patch.object(
            main.hwm_tracker,"active_peak_records",side_effect=OSError("disk unavailable")):
            out=main.check_once(client,dry_run=False,now=NOW)
        self.assertEqual(out["submitted"],0)
        self.assertEqual(client.submitted,[])

    def test_buy_addition_rebases_and_rejects_old_peak(self):
        current=pos(price="111",pnl=".11",qty="1")
        self.seed_peak(current,peak_price="120")
        current.qty="1.5"
        client=FakeClient([current])
        with patch.dict(sys.modules,sdk_stub()):
            self.assertEqual(main.check_once(client,dry_run=False,now=NOW)["submitted"],0)
        self.assertEqual(client.submitted,[])

    def test_inconsistent_broker_price_gain_never_triggers_profit(self):
        current=pos(price="110",pnl=".14")
        self.seed_peak(current,peak_price="120")
        client=FakeClient([current])
        with patch.dict(sys.modules,sdk_stub()):
            self.assertEqual(main.check_once(client,dry_run=False,now=NOW)["submitted"],0)
        self.assertEqual(client.submitted,[])

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
        self.seed_peak(client.positions[0])
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
        self.seed_peak(client.positions[0])
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


    def test_highwater_observer_does_not_submit_any_orders(self):
        client=FakeClient([pos(price="121",pnl="0.21")])
        with patch.object(main,"clients",return_value=(client,False,False)),patch.object(
            main.hwm_tracker,"observe",return_value={"observed":1}) as observer:
            main.observe_highwater()
        self.assertEqual(client.submitted,[])
        observer.assert_called_once()
        self.assertEqual(len(observer.call_args[0][0]),1)

    def test_highwater_observer_skips_market_closed(self):
        client=FakeClient(open_=False)
        client.get_all_positions=lambda: (_ for _ in ()).throw(AssertionError())
        with patch.object(main,"clients",return_value=(client,False,False)),patch.object(
            main.hwm_tracker,"observe") as observer:
            main.observe_highwater()
        observer.assert_not_called()
        self.assertEqual(client.submitted,[])

    def test_scheduler_uses_ny_market_hours_and_no_cron(self):
        trace={}
        class Stub:
            def __init__(self,**kw):trace["init"]=kw
            def add_job(self,*a,**kw):trace.setdefault("jobs",[]).append((a,kw))
            def start(self):trace["start"]=True
        mods=sdk_stub()
        for name in ("apscheduler","apscheduler.schedulers","apscheduler.schedulers.blocking"):
            mods[name]=types.ModuleType(name)
        mods["apscheduler.schedulers.blocking"].BlockingScheduler=Stub
        with patch.dict(sys.modules,mods), patch("sys.argv",["main.py"]):
            with patch.object(main,"clients",return_value=(None,False,False)),patch.object(
                main.hwm_tracker,"connect",return_value=SimpleNamespace(close=lambda: None)):
                main.main()
        self.assertTrue(trace["start"])
        self.assertEqual(trace["init"]["timezone"],main.NEW_YORK)
        self.assertEqual(len(trace["jobs"]),2)
        seller=next(k for a,k in trace["jobs"] if k["id"]=="v2_seller_hourly")
        observer=next(k for a,k in trace["jobs"] if k["id"]=="v2_highwater_observer")
        self.assertEqual(seller["day_of_week"],"mon-fri")
        self.assertEqual(seller["hour"],"9-15")
        self.assertEqual(seller["minute"],35)
        self.assertEqual(observer["hour"],"9-15")
        self.assertEqual(observer["minute"],"2,12,22,32,42,52")
        self.assertEqual(observer["day_of_week"],"mon-fri")


if __name__=="__main__":
    unittest.main()
