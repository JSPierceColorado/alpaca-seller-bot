# V2 Seller — 10% take profits and non-S&P100 5% loss exits

Run a **long-only seller** on Alpaca that retains +10% take-profit exits and adds a -5% stop-loss trigger ONLY for US stocks outside the verified S&P 100 constituent list.
It checks held US stock/ETF positions at **9:35, 10:35, 11:35, 12:35, 1:35,
2:35, and 3:35 p.m./a.m. Eastern as applicable** (09:35–15:35 NY time),
Monday–Friday. The NYSE clock prevents weekend, holiday and early-close
trades. APScheduler is the **internal persistent scheduler**; there is no
Railway Cron. The Railway service is restarted automatically if its process fails.

## Strategy

- Look at long US-equity positions across the configured account.
- Sell for profit if a position's total unrealized gain is at least 10%, as
  reported by Alpaca and as separately calculated from average entry and
  current broker price.
- Submit a normal market **DAY SELL** for all held fractional or whole shares.
- **S&P 100 members have no stop-loss**, regardless of their losses.
- **Non-S&P100 US equities exit at a verified loss of 5% or more** versus broker average entry; both broker `unrealized_plpc` and an independent average-cost/current-price calculation must confirm the threshold.
- Membership comes from the existing V2 Screener Google Sheet (101 constituents). A valid header, scan timestamp, and complete recent constituent list are required. If the Sheet is unavailable, incomplete, or stale, the bot skips all stop-loss decisions rather than guessing membership, while continuing +10% profit exits.
- No trailing stops, short selling, options, or buy logic. These are market orders on the hourly check, **not broker-resident stop orders**: overnight gaps, market moves, or slippage may produce losses beyond 5%.
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
- `GOOGLE_SERVICE_ACCOUNT_JSON` references V2-Screener's service-account credentials (read-only Sheets API).
- `GOOGLE_SHEET_ID=14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ` points to the existing V2 constituent list.

Manual read-only checks: `python -u main.py --check-now` previews trades during market hours. `python -u main.py --check-membership` verifies S&P 100 access without broker calls, regardless of market hours. Neither submits orders.

## Tests

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py
```

Do not deploy an older seller bot to the same account in parallel.
A full position can be liquidated once it meets either applicable threshold, so this
may sell holdings you acquired manually, not only shares bought by V2.
Review what assets are in the live account before activating.
