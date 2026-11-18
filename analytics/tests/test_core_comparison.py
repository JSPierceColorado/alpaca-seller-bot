"""Read-only core vs active return attribution tests."""
import unittest
import core_comparison as cmp


def d(x):
    return str(x)[:10]


def fill(oid,sym,qty,price,day,side="buy"):
    return {"activity_type":"FILL","id":oid+"-fill","order_id":oid,
            "date":day,"transaction_time":day+"T12:00:00-05:00",
            "symbol":sym,"qty":str(qty),"price":str(price),"side":side}


def deposit(amount,day):
    return {"activity_type":"CSD","id":"csd-"+day,
            "date":day,"net_amount":str(amount)}


def pos(qty,value):
    return {"qty":str(qty),"market_value":str(value)}


class CoreComparisonTests(unittest.TestCase):
    def setup(self):
        dates=["2026-11-18","2026-11-19","2026-11-20"]
        closes=[{"date":dates[0],"equity":"1200"},
                {"date":dates[1],"equity":"1220"},
                {"date":dates[2],"equity":"1320"}]
        positions={
            dates[0]:{"VT":pos(".5",50),"SGOV":pos(".5",50)},
            dates[1]:{"VT":pos(".5",55),"SGOV":pos(".5",55)},
            dates[2]:{"VT":pos(".75",80),"SGOV":pos(".75",80)}
        }
        ids={"buy-vt-1":"v2core-a1-VT","buy-sgov-1":"v2core-a1-SGOV",
             "buy-vt-2":"v2core-a2-VT","buy-sgov-2":"v2core-a2-SGOV"}
        activities=[deposit(200,dates[0]),
            fill("buy-vt-1","VT",".5","100",dates[0]),
            fill("buy-sgov-1","SGOV",".5","100",dates[0]),
            deposit(100,dates[2]),
            fill("buy-vt-2","VT",".25","100",dates[2]),
            fill("buy-sgov-2","SGOV",".25","100",dates[2])]
        return closes,positions,activities,ids
    def test_core_and_active_flow_adjusted_indexes(self):
        out=cmp.compare(*self.setup(),dateval=d,
            activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"ready",out["reason"])
        self.assertEqual(out["core_contributions"],150)
        self.assertEqual(out["core_equity"],160)
        self.assertEqual(out["active_equity"],1160)
        self.assertEqual(out["core_return_pct"],10)
        self.assertAlmostEqual(out["active_return_pct"],.909,places=2)
        self.assertEqual(out["series"][-1]["core_index"],110)
    def test_dividend_added_without_mistaking_for_external_deposit(self):
        closes,pos,acts,ids=self.setup()
        acts.append({"activity_type":"DIV","date":"2026-11-20",
                     "symbol":"VT","net_amount":"2"})
        closes[-1]["equity"]="1322"
        out=cmp.compare(closes,pos,acts,ids,dateval=d,
                        activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"ready",out["reason"])
        self.assertEqual(out["dividends"],2)
        self.assertEqual(out["core_equity"],162)
        self.assertAlmostEqual(out["core_return_pct"],12,places=2)
    def test_unknown_etf_fill_suspends_comparison(self):
        closes,pos,acts,ids=self.setup()
        acts.append(fill("manual","VT",".01","100","2026-11-20"))
        out=cmp.compare(closes,pos,acts,ids,dateval=d,
                activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"suspended")
        self.assertIn("UNATTRIBUTED",out["reason"])
    def test_unverified_account_activities_fail_closed(self):
        closes,pos,acts,ids=self.setup()
        for complete,reconciled in ((False,True),(True,False)):
            out=cmp.compare(closes,pos,acts,ids,dateval=d,
                activity_complete=complete,account_reconciled=reconciled)
            self.assertEqual(out["status"],"suspended")
    def test_no_fills_yet_shows_waiting(self):
        closes,pos,acts,ids=self.setup()
        out=cmp.compare(closes,{},[deposit(200,"2026-11-18")],{},
                dateval=d,activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"awaiting_first_fill")
    def test_core_position_without_matching_fill_is_not_counted(self):
        closes,pos,acts,ids=self.setup()
        out=cmp.compare(closes,pos,[deposit(200,"2026-11-18")],{},
                dateval=d,activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"suspended")
    def test_no_early_claim_with_single_close(self):
        closes,pos,acts,ids=self.setup()
        out=cmp.compare(closes[:1],pos,acts,ids,dateval=d,
                activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"awaiting_closes")
    def test_invalid_broker_mark_does_not_return_misleading_pct(self):
        closes,pos,acts,ids=self.setup()
        pos["2026-11-19"]["VT"]["qty"]="9"
        out=cmp.compare(closes,pos,acts,ids,dateval=d,
                activity_complete=True,account_reconciled=True)
        self.assertEqual(out["status"],"suspended")
        self.assertIn("QTY_DISAGREES",out["reason"])


if __name__=="__main__":unittest.main()
