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
            def __init__(self,has_marker):self.has_marker=has_marker
            def execute(self,*args):return self
            def fetchone(self):return {"val":"true"} if self.has_marker else None
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
