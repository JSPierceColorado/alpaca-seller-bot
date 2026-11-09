# V2 Seller — Hourly take-profit-only Alpaca bot

Run the original simple **long-only 10%-profit seller** on an Alpaca account.
It checks held US stock/ETF positions at **9:35, 10:35, 11:35, 12:35, 1:35,
2:35, and 3:35 p.m./a.m. Eastern as applicable** (09:35–15:35 NY time),
Monday–Friday. The NYSE clock prevents weekend, holiday and early-close
trades. APScheduler is the **internal persistent scheduler**; there is no
Railway Cron. The Railway service is restarted automatically if its process fails.

## Strategy

- Look at long US-equity positions across the configured account.
- Sell **only if the position's total unrealized gain is at least 10%** as
  reported by Alpaca and as separately calculated from average entry and
  current broker price.
- Submit a normal market **DAY SELL** for all held fractional or whole shares.
- **No stop losses at any level, no trailing stops, no shorts, no buys.**
  Losing and sub-10%-gain positions are held.
- Skip if any open order exists for a position (including nested legs).
- Recheck market clock, position and open orders immediately before sending.
- Broker `client_order_id` is stable for the day and symbol:
  `v2s-YYYYMMDD-SYMBOL`. Before sending, query Alpaca for that ID.
  Network uncertainty aborts the current cycle without blind retries.
- Uses Alpaca market orders; fills may differ from checked prices.
  **A 10% reported gain does NOT guarantee 10% realized profit** after
  slippage, spread or a price move before execution. This is an hourly
  trigger, not constant price monitoring.

## Railway

- Source: `JSPierceColorado/alpaca-seller-bot`, branch `main`
- Service: `V2-Seller` in Investing
- Always-on worker, one replica, `python -u main.py`, Railway Cron disabled,
  restart policy `ALWAYS`.
- `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` from your Alpaca credentials.
- `ALPACA_PAPER=false` for the actual live brokerage account.
- `DRY_RUN=false` to submit real sells.
- `ENABLE_LIVE_TRADING=true` plus exact
  `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_SELLS` required for real-money orders.
- `LOG_LEVEL=INFO` for operational logs.
- No Google service account required for the seller.

Manual read-only checks: `python -u main.py --check-now`. This forces a
preview even if scheduled mode is enabled; it never submits trades.

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py
```

Do not deploy the old stop-loss implementation to the same account.
A full position can be liquidated once it meets the threshold, so this
may sell holdings you acquired manually, not only shares bought by V2.
Review what assets are in the live account before activating.
