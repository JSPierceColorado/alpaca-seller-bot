"""Offline high-water observation tests; zero broker calls and zero trading."""
import os
import tempfile
import unittest
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace

import hwm_tracker as hwm

NOW=datetime(2026,11,16,15,30,tzinfo=timezone.utc)


def pos(symbol="KO",price="111",entry="100",qty="1",asset="us_equity",side="long"):
    gain=(Decimal(price)/Decimal(entry)-1)
    return SimpleNamespace(symbol=symbol,current_price=price,
        avg_entry_price=entry,qty=qty,unrealized_plpc=str(gain),
        asset_class=asset,side=side)


class HighWaterTrackerTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path=os.path.join(self.dir.name,"persist","seller.sqlite3")

    def observe(self,positions):
        return hwm.observe(positions,path=self.path,at=NOW)

    def state(self):
        with hwm.connect(self.path) as db:
            return [dict(r) for r in db.execute("SELECT * FROM active_peaks ORDER BY symbol")]

    def archived(self):
        with hwm.connect(self.path) as db:
            return [dict(r) for r in db.execute("SELECT * FROM archived_peaks")]

    def test_records_initial_peak_and_raises_it(self):
        a=self.observe([pos(price="108")])
        self.assertEqual(a["new"],1)
        self.assertEqual(self.state()[0]["peak_gain_pct"],"8.00")
        b=self.observe([pos(price="117")])
        self.assertEqual(b["new_highs"],1)
        self.assertEqual(self.state()[0]["peak_gain_pct"],"17.00")
        c=self.observe([pos(price="114")])
        self.assertEqual(c["new_highs"],0)
        self.assertEqual(self.state()[0]["peak_gain_pct"],"17.00")
        self.assertEqual(self.state()[0]["observations"],3)

    def test_sqlite_peak_survives_reopen(self):
        self.observe([pos(price="121")])
        with hwm.connect(self.path) as db:
            r=db.execute("SELECT peak_gain_pct FROM active_peaks WHERE symbol='KO'").fetchone()
        self.assertEqual(Decimal(r["peak_gain_pct"]),Decimal("21"))
        self.assertEqual(self.state()[0]["peak_gain_pct"],"21.00")

    def test_shadow_giveback_is_observation_not_trade(self):
        self.observe([pos(price="113")])
        a=self.observe([pos(price="110")])
        self.assertEqual(a["armed"],1)
        self.assertEqual(a["shadow_givebacks"],1)
        b=self.observe([pos(price="109")])
        self.assertEqual(b["shadow_givebacks"],0)  # below 10% floor
        self.assertEqual(self.state()[0]["peak_gain_pct"],"13.00")

    def test_unarmed_at_twelve_percent(self):
        a=self.observe([pos(price="112")])
        self.assertEqual(a["armed"],0)
        self.assertEqual(a["shadow_givebacks"],0)

    def test_addition_rebases_position_and_archives_prior_peak(self):
        self.observe([pos(price="120",entry="100",qty="1")])
        a=self.observe([pos(price="121",entry="101",qty="1.05")])
        self.assertEqual(a["rebased"],1)
        self.assertEqual(a["new"],1)
        self.assertEqual(self.state()[0]["peak_gain_pct"],
                         str((Decimal("121")/Decimal("101")-1)*100))
        self.assertEqual(self.archived()[0]["reason"],"COST_BASIS_CHANGED_OR_ADDED")

    def test_partial_sale_does_not_erase_peak(self):
        self.observe([pos(price="121",qty="2")])
        a=self.observe([pos(price="118",qty="1.5")])
        self.assertEqual(a["rebased"],0)
        self.assertEqual(self.state()[0]["peak_gain_pct"],"21.00")

    def test_absent_symbol_archived_and_reentry_is_fresh(self):
        self.observe([pos(price="121"),pos("MSFT",price="102")])
        a=self.observe([pos("MSFT",price="103")])
        self.assertEqual(a["disappeared"],1)
        self.assertEqual([r["symbol"] for r in self.state()],["MSFT"])
        self.observe([pos(price="105"),pos("MSFT",price="104")])
        self.assertEqual(next(r for r in self.state() if r["symbol"]=="KO")["peak_gain_pct"],"5.00")

    def test_two_empty_observations_required_before_clearing(self):
        self.observe([pos(price="121")])
        a=self.observe([])
        self.assertEqual(a["disappeared"],0)
        self.assertEqual(self.state()[0]["peak_gain_pct"],"21.00")
        b=self.observe([])
        self.assertEqual(b["disappeared"],1)
        self.assertEqual(self.state(),[])
        self.assertEqual(self.archived()[0]["reason"],"POSITION_ABSENT_TWICE")

    def test_invalid_broker_quote_is_not_recorded_as_peak(self):
        self.observe([pos(price="111")])
        stale=pos(price="150")
        stale.unrealized_plpc="0.01"
        a=self.observe([stale])
        self.assertEqual(a["invalid"],1)
        self.assertEqual(self.state()[0]["peak_gain_pct"],"11.00")

    def test_market_snapshots_not_api_orders(self):
        import inspect
        src=inspect.getsource(hwm)
        self.assertNotIn("submit_order",src)
        self.assertNotIn("close_position",src)
        self.assertNotIn("TradingClient(",src)


if __name__=="__main__":
    unittest.main()
