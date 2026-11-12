# Performance Analytics — Alpaca (isolated branch)

**Deployment:** Railway Investing / production / Performance-Analytics.

This is a **read-only, password-protected** service. It cannot submit orders or transfers and does not modify any existing trading bot. HTTP requests to Alpaca are GET-only; account credentials are referenced from the existing V2-Buyer service.

Features:
- Persistent SQLite snapshots on Railway volume at /data
- Portfolio equity, cash, current positions and unrealized P&L
- Broker FILL history, approximate FIFO realized P&L, covered sell-order win rate, gross gains/losses and profit factor
- Bot-source grouping by buy client-order-ID prefix (V2, ATLAS, other)
- S&P 500 SPY benchmark based on dividend/split-adjusted Alpaca IEX daily bars
- Hypothetical SPY strategy starting at first comparable portfolio equity and replicating recorded cash deposits/withdrawals
- Cash-flow-adjusted approximate account time-weighted index, equity-vs-SPY and net-profit graphs
- Protected HTML dashboard, GET /api/report, GET /api/summary, GET /export/snapshots.csv
- Routine refresh every market hour at xx:40, after-close at 18:30 NY, and on service startup
- Summary metric logs tagged ANALYTICS_SUMMARY for inspection from ChatGPT's Railway connector

IMPORTANT LIMITATIONS:
- Cashflow coverage requires complete Alpaca activity history; transfers/ACATS/corporate actions can invalidate the benchmark.
- Realized P&L is **not** authoritative tax-lot accounting; FIFO matching uses available fills and excludes unknown opening lots, fees and tax adjustments.
- Profit rates are by fully covered **sell orders**, not all buys or open positions.
- SPY uses adjusted IEX bars, which may not match official NYSE/Nasdaq consolidated close.
- This is an Alpaca-account analytics service; Coinbase is NOT ingested or benchmarked here. Connect a separate source before reporting combined-account results.
- It is an hourly checkpointing tool, not real-time trading or stop-loss monitoring.

Env vars: APCA_API_KEY_ID, APCA_API_SECRET_KEY, ALPACA_PAPER, DASH_USERNAME, DASH_PASSWORD, PORT, LOG_LEVEL.
Keep DASH_PASSWORD private. Change via Railway if compromised.
