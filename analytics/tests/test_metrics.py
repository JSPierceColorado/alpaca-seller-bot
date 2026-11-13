"""Pure math and safety tests; do not contact Alpaca or send orders."""
import sys,unittest
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main

class AnalyticsTests(unittest.TestCase):
    def test_fifo_one_winner_one_loser(self):
        data=[
            {"id":"1","activity_type":"FILL","symbol":"KO","side":"buy","qty":"2","price":"100","order_id":"b1","transaction_time":"2026-01-01T00:00:00Z"},
            {"id":"2","activity_type":"FILL","symbol":"KO","side":"sell","qty":"1","price":"110","order_id":"s1","transaction_time":"2026-02-01T00:00:00Z"},
            {"id":"3","activity_type":"FILL","symbol":"KO","side":"sell","qty":"1","price":"95","order_id":"s2","transaction_time":"2026-03-01T00:00:00Z"}
        ]
        result=main.fifo(data,{"b1":"v2b-20260101-KO"})
        self.assertEqual(result["wins"],1)
        self.assertEqual(result["losses"],1)
        self.assertEqual(result["win_rate"],50)
        self.assertEqual(result["realized"],5)
        self.assertEqual(result["source_realized"]["V2"],5)

    def test_fifo_unknown_basis_is_not_counted(self):
        f=[{"id":"1","activity_type":"FILL","symbol":"SPY","side":"sell","qty":"1","price":"300","order_id":"s1","transaction_time":"2026-01-01T00:00:00Z"}]
        r=main.fifo(f,{})
        self.assertEqual(r["closed_orders"],0)
        self.assertEqual(r["unmatched_sells"],1)
        self.assertIsNone(r["win_rate"])

    def test_order_backfill_and_incremental_pagination(self):
        from unittest.mock import patch
        class DB:
            def __init__(self,has_marker):self.has_marker=has_marker;self.query=""
            def execute(self,query,*args):self.query=query;return self
            def fetchone(self):
                if "COUNT" in self.query:return {"n":0}
                return {"val":"true"} if self.has_marker else None
            def close(self):pass
        for marker in (False,True):
            seen=[]
            def fake_get(path,params):
                seen.append(params)
                return []
            with patch.object(main,"connect",return_value=DB(marker)),patch.object(main,"get",side_effect=fake_get):
                rows,complete,full=main.orders()
            self.assertEqual(rows,[])
            self.assertTrue(complete)
            self.assertEqual(full,not marker)
            self.assertEqual("after" in seen[0],marker)

    def eod(self,day,equity,cash):
        return {"day":day,"updated":day+"T23:30:00+00:00",
                "equity":equity,"cash":cash}

    def benchmark(self,rows,activities=None,prices=None,complete=True):
        return main.verified_spy_benchmark(
            rows,prices or {"2026-11-12":100,"2026-11-13":100},
            activities or [],"2026-11-12",complete)

    def test_baseline_uses_after_close_funding_not_intraday_equity(self):
        rows=[
            {"day":"2026-11-12","updated":"2026-11-12T14:40:00+00:00",
             "equity":3416.89,"cash":2704.86},
            self.eod("2026-11-12",4411.22,3704.86),
            self.eod("2026-11-13",4413.47,3704.86)]
        closes=main.completed_daily_snapshots(rows)
        self.assertEqual(closes[0]["equity"],main.Decimal("4411.22"))
        series,dd,issues=self.benchmark(rows)
        self.assertEqual(issues,[])
        self.assertEqual(series[0]["account_index"],100)
        self.assertAlmostEqual(series[1]["account_index"],100.051,places=2)
        self.assertAlmostEqual(series[1]["account_net"],2.25,places=2)
        self.assertLess(series[1]["account_index"],101)

    def test_deposit_is_never_return_and_spy_receives_same_funding(self):
        rows=[self.eod("2026-11-12",1000,1000),
              self.eod("2026-11-13",2000,2000)]
        deposit=[{"activity_type":"CSD","id":"deposit-1","date":"2026-11-13",
                  "net_amount":"1000"}]
        series,dd,issues=self.benchmark(rows,deposit)
        self.assertEqual(issues,[])
        self.assertEqual(series[-1]["cashflow"],1000)
        self.assertEqual(series[-1]["cumulative_external_cashflow"],1000)
        self.assertEqual(series[-1]["account_index"],100)
        self.assertEqual(series[-1]["account_net"],0)
        self.assertEqual(series[-1]["spy_equity"],2000)
        self.assertEqual(series[-1]["spy_net"],0)

    def test_unrecorded_funding_suspends_instead_of_fabricating_return(self):
        rows=[self.eod("2026-11-12",1000,1000),
              self.eod("2026-11-13",2000,2000)]
        series,dd,issues=self.benchmark(rows)
        self.assertEqual(series,[])
        self.assertIn("CASH_LEDGER_MISMATCH_2026-11-13",issues)

    def test_withdrawal_excluded_from_loss_and_spy_sells_shares(self):
        rows=[self.eod("2026-11-12",1000,1000),
              self.eod("2026-11-13",600,600)]
        withdrawal=[{"activity_type":"CSW","id":"wd","date":"2026-11-13",
                     "net_amount":"-400"}]
        series,dd,issues=self.benchmark(rows,withdrawal)
        self.assertEqual(issues,[])
        self.assertEqual(series[-1]["account_net"],0)
        self.assertEqual(series[-1]["spy_net"],0)
        self.assertEqual(series[-1]["account_index"],100)

    def test_ordinary_trade_cash_movement_is_not_deposit(self):
        rows=[self.eod("2026-11-12",1000,700),
              self.eod("2026-11-13",1010,810)]
        sale=[{"activity_type":"FILL","id":"f","transaction_time":"2026-11-13T15:00:00Z",
               "symbol":"ABC","side":"sell","qty":"1","price":"110"}]
        series,dd,issues=self.benchmark(rows,sale)
        self.assertEqual(issues,[])
        self.assertEqual(series[-1]["account_net"],10)
        self.assertAlmostEqual(series[-1]["account_index"],101)

    def test_journal_and_instant_funding_block_benchmark(self):
        rows=[self.eod("2026-11-12",1000,1000),
              self.eod("2026-11-13",2000,2000)]
        for kind in ("JNLC","ACATS","MEM","FOPT","UNRECOGNIZED"):
            events=[{"activity_type":kind,"date":"2026-11-13",
                     "net_amount":"1000"}]
            series,dd,issues=self.benchmark(rows,events)
            self.assertEqual(series,[])
            self.assertIn("UNRECONCILED_ACTIVITY_"+kind,issues)

    def test_missing_market_close_or_incomplete_activities_suspends(self):
        rows=[self.eod("2026-11-12",1000,1000),
              {"day":"2026-11-13","updated":"2026-11-13T15:00:00Z",
               "equity":1000,"cash":1000}]
        series,dd,issues=self.benchmark(rows)
        self.assertEqual(series,[])
        self.assertIn("NEED_TWO_COMPLETED_MARKET_DAYS",issues)
        rows[-1]=self.eod("2026-11-13",1000,1000)
        series,dd,issues=self.benchmark(rows,complete=False)
        self.assertEqual(series,[])
        self.assertIn("ACTIVITY_HISTORY_INCOMPLETE",issues)

    def test_weekend_funding_maps_to_next_trading_close(self):
        rows=[self.eod("2026-11-12",1000,1000),
              self.eod("2026-11-13",1000,1000),
              self.eod("2026-11-16",1200,1200)]
        flow=[{"activity_type":"CSD","date":"2026-11-14","net_amount":"200"}]
        series,dd,issues=self.benchmark(
            rows,flow,prices={"2026-11-12":100,"2026-11-13":100,
                              "2026-11-16":100})
        self.assertEqual(issues,[])
        self.assertEqual(series[-1]["account_index"],100)
        self.assertEqual(series[-1]["cashflow"],200)

    def test_missing_market_day_cannot_smuggle_cashflow_into_returns(self):
        rows=[self.eod("2026-11-12",1000,1000),
              self.eod("2026-11-16",1000,1000)]
        series,dd,issues=self.benchmark(
            rows,prices={"2026-11-12":100,"2026-11-13":100,
                         "2026-11-16":100})
        self.assertEqual(series,[])
        self.assertIn("MISSING_DAILY_CLOSE_SNAPSHOT",issues)

    def test_trade_api_is_read_only(self):
        import inspect
        code=inspect.getsource(main.get)
        self.assertIn('method="GET"',code)
        self.assertNotIn("submit_order",inspect.getsource(main))

    def test_date_and_amount(self):
        self.assertEqual(main.dateval("2026-11-11T16:00:00Z"),"2026-11-11")
        self.assertEqual(main.dec("NaN"),0)
        self.assertEqual(str(main.dec("12.35")),"12.35")

if __name__=="__main__":unittest.main()
