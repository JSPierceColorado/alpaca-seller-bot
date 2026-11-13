# Performance Analytics — Alpaca (isolated branch)

**Deployment:** Railway Investing / production / Performance-Analytics.

This is a **read-only, password-protected** service. It cannot submit orders or transfers and does not modify any existing trading bot. HTTP requests to Alpaca are GET-only; account credentials are referenced from the existing V2-Buyer service.

Features:
- Persistent SQLite snapshots on Railway volume at /data
- Portfolio equity, cash, current positions and unrealized P&L
- Broker FILL history, approximate FIFO realized P&L, covered sell-order win rate, gross gains/losses and profit factor
- Bot-source grouping by buy client-order-ID prefix (V2, ATLAS, other)
- S&P 500 SPY benchmark based on dividend/split-adjusted Alpaca IEX daily bars
- Hypothetical SPY strategy beginning at the **verified after-close (18:00+ New York)** account snapshot on the benchmark-start trading day, not an intraday equity observation
- All post-baseline cash deposits and withdrawals enter both portfolios on the next observed market close; **deposits never count toward investment profit or percentage return**
- Daily time-weighted account return calculated as (closing equity - external net cash flow) / previous closing equity - 1
- SPY comparison purchases/withdrawals with the same external flows at adjusted closing prices, with matching P&L and return charts
- Strict daily cash-ledger reconciliation against FILLs, ordinary dividends/fees, cash deposits and withdrawals. Unexplained cash movements and transfers such as ACATS/INSTANT FUNDING/JNL **disable all strategy-vs-SPY return claims** until reconciled; never assume an unrecorded deposit was market profit
- Broker-reported raw equity history is displayed separately and always labeled as including contributions/withdrawals
- Protected HTML dashboard, GET /api/report, GET /api/summary, GET /export/snapshots.csv
- Routine refresh every market hour at xx:40, after-close at 18:30 NY, and on service startup
- Summary metric logs tagged ANALYTICS_SUMMARY for inspection from ChatGPT's Railway connector

IMPORTANT LIMITATIONS:
- A valid return series requires **two complete end-of-day snapshots**, matching SPY daily bars, complete brokerage activities and a reconciled cash ledger for every intervening trading day. An old incomplete intraday observation or missing funding entry must not generate a return.
- The baseline is taken from the saved after-close snapshot, including deposits received before that close. The first baseline day's activity is not treated as return.
- Cash flow treatment assumes external funding is invested into SPY at the day's adjusted close (and end-of-day adjustment for the Alpaca account); intraday cash-flow timing cannot be represented precisely by daily observations.
- Transfers/ACATS/journals/instant funding/corporate actions and unknown activity codes suspend the benchmark pending reconciliation; most ordinary dividends/interest/fees remain inside investment P&L, not treated as external funding.
- Cash-ledger reconciliation checks cash balance changes against broker-confirmed fills and recognized cash activities within a conservative tolerance. Other broker settlement mechanics can cause the benchmark to be unavailable rather than report possibly misleading performance.
- Realized P&L is **not** authoritative tax-lot accounting; FIFO matching uses available fills and excludes unknown opening lots, fees and tax adjustments.
- Profit rates are by fully covered **sell orders**, not all buys or open positions.
- SPY uses adjusted IEX bars, which may not match official NYSE/Nasdaq consolidated close.
- This is an Alpaca-account analytics service; Coinbase is NOT ingested or benchmarked here. Connect a separate source before reporting combined-account results.
- It is an hourly checkpointing tool, not real-time trading or stop-loss monitoring.

Env vars: APCA_API_KEY_ID, APCA_API_SECRET_KEY, ALPACA_PAPER, DASH_USERNAME, DASH_PASSWORD, PORT, LOG_LEVEL.
Keep DASH_PASSWORD private. Change via Railway if compromised.
