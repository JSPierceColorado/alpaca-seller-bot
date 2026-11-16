# V2 Seller — live +13% arming / 3-point giveback and non-SP100 5% loss exits

Run a **long-only seller** on Alpaca with a **trailing-giveback profit exit** and the existing -5% stop-loss trigger ONLY for US stocks outside the verified S&P 100 constituent list. The former immediate +10% profit exit has been replaced: profits must first reach +13% and then pull back 3 percentage points, with a +10% minimum exit eligibility and a sell **limit-price floor** at 110% of Alpaca's average position cost.
It checks held US stock/ETF positions at **9:35, 10:35, 11:35, 12:35, 1:35,
2:35, and 3:35 p.m./a.m. Eastern as applicable** (09:35–15:35 NY time),
Monday–Friday. The NYSE clock prevents weekend, holiday and early-close
trades. APScheduler is the **internal persistent scheduler**; there is no
Railway Cron. The Railway service is restarted automatically if its process fails.

## Strategy

- Look at long US-equity positions across the configured account.
- Track each held position's highest **observed** gain since its current tracking episode began. Profit exits **arm once observed peak gain reaches +13%**. If the gain falls by 3 percentage points or more from that peak, and both the broker ratio and independently calculated gain remain **at least +10%**, submit a full-position DAY **LIMIT SELL**. Examples: +13% peak → +10% exit condition; +20% peak → +17% exit condition; +30% peak → +27% exit condition. A stock that only reaches +12% is not yet armed and may return to a loss without selling on the profit rule.
- The limit price is **rounded upward to at least 110% of the broker-reported average entry cost**, to avoid selling below the +10% per-share price floor (before fees/taxes). **Execution is not guaranteed:** a resting DAY limit order may go unfilled and expire; if still open it can block subsequent exit orders for the same symbol until filled/canceled/expired.
- Profit sale is not a broker-native trailing stop; the seller evaluates exits **hourly**. Observations run every ten minutes. Peaks and retracements between observations may be missed.
- Loss exits remain normal **DAY MARKET SELLS**, for verified non-SP100 stocks only.
- **S&P 100 members have no stop-loss**, regardless of their losses.
- **Non-S&P100 US equities exit at a verified loss of 5% or more** versus broker average entry; both broker `unrealized_plpc` and an independent average-cost/current-price calculation must confirm the threshold.
- Membership comes from the existing V2 Screener Google Sheet (101 constituents). A valid header, scan timestamp, and complete recent constituent list are required. If the Sheet is unavailable, incomplete, or stale, the bot skips all stop-loss decisions rather than guessing membership, while continuing valid trailing-giveback profit exits.
- **LIVE**: the persistently stored gain high-water mark informs profit exits when the hourly seller runs. Read-only high-water sampling occurs every 10 minutes (09:32, 09:42, 09:52, etc. New York time while open), and the hourly seller refreshes the peak immediately before evaluating its exit decisions.
- **Configured:** 3-percentage-point giveback, arming at +13% and requiring at least +10% current profit at the time an exit is submitted. There is no automatic fixed +10% profit sell anymore.
- Each mark is the **highest broker position gain observed since the tracker began monitoring the current position**, not the actual all-time stock-price high. Intraday moves between samples, pre-deployment gains, and overnight gaps can be missed.
- Every valid ten-minute observation is retained in `peak_samples` (gain, peak, price, entry cost, quantity, timestamp), so 2-, 3-, and 5-percentage-point giveback rules can be compared later. The peak record survives Railway restarts using an attached persistent volume at `/data` (SQLite). The tracker resets/rebases when new shares are added or Alpaca's average cost changes; a partial sale can retain the peak. Position replacements with identical quantity/cost between observations may be indistinguishable.
- Archive prior marks when a position disappears from a verified portfolio snapshot (two consecutive empty snapshots are required to clear all records). Existing seller operations continue if the tracker fails. The observer's API calls are read-only.
- No broker-native trailing stops, short selling, options, or buy logic. Hourly non-SP100 -5% stops remain **market** exits, not broker-resident stop orders; overnight gaps and price moves can produce losses beyond 5%. Profit exits use limit prices and can remain unfilled.
- Skip if any open order exists for a position (including nested legs).
- Recheck market clock, position and open orders immediately before sending.
- Broker `client_order_id` is stable for the day, symbol, and exit reason: `v2sl-YYYYMMDD-SYMBOL` for a **non-SP100 −5% stop-loss**, `v2sg-YYYYMMDD-SYMBOL` for a trailing **profit** exit. The seller checks both markers plus legacy `v2s-YYYYMMDD-SYMBOL` before sending, preserving one sale attempt per symbol/day. Each buyer checks only *filled* `v2sl-` sells submitted since this week’s Monday 00:00 New York time. Stop-loss exit on Monday blocks repurchases through Sunday, but a Friday stop expires that Monday; profitable exits never cause this lockout. Historical generic `v2s-` orders cannot reliably be classified retrospectively.
  Network uncertainty aborts the current cycle without blind retries.
- Profit DAY limit orders should not fill below their price limits, but may remain unfilled. Commissions, fees, taxes, funding interest, and evolving average cost can mean the final net return is not precisely +10%. Conditional loss exits retain market-order slippage risks.
- Peaks are measured from when monitoring was activated for the current position episode, not an exchange-sourced all-time price high. Re-entry or additional shares can change a position's cost basis and reset its peak.

## Railway

- Source: `JSPierceColorado/alpaca-seller-bot`, branch `main`
- Service: `V2-Seller` in Investing
- Mount a persistent Railway volume named `seller-highwater` at `/data` to retain observations across deploys; it is not shared with the analytics service.
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
