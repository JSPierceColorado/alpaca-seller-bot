"""Isolated tests; no Alpaca credentials and no live trading.

Dependencies are installed for CI/local execution using requirements.txt.
"""

import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


def pos(symbol, ratio, asset_class="us_equity", side="long"):
    return SimpleNamespace(symbol=symbol, unrealized_plpc=ratio, asset_class=asset_class, side=side, qty="1.2")


class StubClient:
    def __init__(self, positions, orders=(), open_=True):
        self.positions = list(positions)
        self.orders = list(orders)
        self.clock = SimpleNamespace(is_open=open_, timestamp="2026-10-14T14:00:00-04:00")
        self.closed = []
        self.orders_requested = False

    def get_clock(self):
        return self.clock

    def get_all_positions(self):
        return self.positions

    def get_orders(self, filter):
        self.orders_requested = True
        return self.orders

    def close_position(self, symbol):
        self.closed.append(symbol)
        return SimpleNamespace(id=f"order-{symbol}")


class ExitRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mock only the SDK request container / enums. The production import is
        # unchanged; no credentials, network, or Alpaca installation is needed.
        alpaca = types.ModuleType("alpaca")
        trading = types.ModuleType("alpaca.trading")
        enums = types.ModuleType("alpaca.trading.enums")
        requests = types.ModuleType("alpaca.trading.requests")
        enums.QueryOrderStatus = SimpleNamespace(OPEN="open")
        requests.GetOrdersRequest = lambda **kwargs: kwargs
        cls.sdk_patch = patch.dict(sys.modules, {
            "alpaca": alpaca,
            "alpaca.trading": trading,
            "alpaca.trading.enums": enums,
            "alpaca.trading.requests": requests,
        })
        cls.sdk_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.sdk_patch.stop()

    def test_threshold_boundaries(self):
        self.assertEqual(main.exit_reason("-0.0500"), "STOP_LOSS")
        self.assertEqual(main.exit_reason("0.1000"), "TAKE_PROFIT")
        self.assertIsNone(main.exit_reason("-0.0499"))
        self.assertIsNone(main.exit_reason("0.0999"))
        self.assertIsNone(main.exit_reason(None))
        self.assertIsNone(main.exit_reason("NaN"))

    def test_dry_run_never_sells(self):
        client = StubClient([pos("AAPL", "-0.06"), pos("MSFT", "0.12")])
        self.assertEqual(main.run_check(client, dry_run=True), 0)
        self.assertEqual(client.closed, [])

    def test_live_paper_triggers_only_long_equities(self):
        client = StubClient([
            pos("AAPL", "-0.06"), pos("MSFT", "0.12"),
            pos("QQQ", "0.05"), pos("BTC/USD", "0.30", "crypto"),
            pos("TSLA", "-0.30", side="short"),
        ])
        self.assertEqual(main.run_check(client, dry_run=False), 2)
        self.assertEqual(client.closed, ["AAPL", "MSFT"])

    def test_market_closed_does_not_check_positions(self):
        client = StubClient([pos("AAPL", "-0.40")], open_=False)
        self.assertEqual(main.run_check(client, dry_run=False), 0)
        self.assertEqual(client.closed, [])
        self.assertFalse(client.orders_requested)

    def test_existing_orders_block_selling_including_nested_legs(self):
        existing = SimpleNamespace(symbol="AAPL", legs=[SimpleNamespace(symbol="MSFT", legs=None)])
        client = StubClient([pos("AAPL", "-0.06"), pos("MSFT", "0.15")], [existing])
        self.assertEqual(main.run_check(client, dry_run=False), 0)
        self.assertEqual(client.closed, [])

    def test_fail_closed_if_order_list_might_be_truncated(self):
        client = StubClient([pos("AAPL", "-0.10")],
                            [SimpleNamespace(symbol="X", legs=None)] * 500)
        with self.assertRaises(RuntimeError):
            main.run_check(client, dry_run=False)
        self.assertEqual(client.closed, [])

    def test_invalid_position_pnl_skipped(self):
        client = StubClient([pos("AAPL", "NaN"), pos("MSFT", None)])
        self.assertEqual(main.run_check(client, dry_run=False), 0)
        self.assertEqual(client.closed, [])

    def test_live_trading_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {
            "ALPACA_PAPER": "false",
            "DRY_RUN": "false",
            "ENABLE_LIVE_TRADING": "false",
            "APCA_API_KEY_ID": "fake",
            "APCA_API_SECRET_KEY": "fake",
        }, clear=True):
            self.assertEqual(main.main(), 1)

    def test_env_flag_fail_closed(self):
        with patch.dict(os.environ, {"DRY_RUN": "not-sure"}):
            with self.assertRaises(ValueError):
                main.env_flag("DRY_RUN", True)


if __name__ == "__main__":
    unittest.main()
