# V2 Seller — PROFIT-ONLY exits for all equities (no stop-losses)

This is the live Alpaca account's **single seller**. It manages all held long US equity positions — both S&P 100 investments and smaller exploratory positions — with the same **high-water profit-exit rule**. **There are no automated stop-loss exits for any symbol.** A position can remain at an unrealized loss indefinitely unless separately sold by an authorized party or broker.

## Trading rule

- Record the highest **observed gain relative to Alpaca's average entry** over the current position episode, using high-water samples every ten minutes during market hours.
- **Arm** the profit mechanism when the observed gain first reaches **+13%**. After a subsequent **3-percentage-point giveback**, a profit exit becomes eligible **only while the broker and independent cost/price check both verify at least +10% profit**. For example: peak +13% -> potential exit at +10%; peak +20% -> potential exit at +17%.
- At eligible exits, submit a **full-position DAY LIMIT SELL** at a price rounded upward to **at least 110% of average entry cost**. The order may not fill; no exit is guaranteed. A retracement can continue below cost if the profitable limit never fills. Market gaps and between-sample moves can be missed.
- **Never sell for a loss threshold**, regardless of the ticker's S&P 100 membership. The former non-S&P 100 -5% market-sale path has been removed; all submitted seller orders must be **profit DAY LIMIT sells** with client IDs `v2sg-YYYYMMDD-SYMBOL`.
- Preserve one sell submission per symbol per trading day, open-order guards, position/clock rechecks, order exception handling, no blind retries, and verified position type (long US equity). Historical `v2sl-` stop-order IDs and generic `v2s-` IDs remain checked for **same-day deduplication only**; the seller does not create them anymore.
- The seller runs **hourly at 9:35 through 15:35 New York time**, Monday–Friday while the Alpaca market clock is open. The independent high-water observer samples at minutes 2,12,22,32,42,52. APScheduler lives inside the Railway service; Railway Cron is disabled.
- A newly purchased lot can change quantity and average cost, causing the high-water tracking episode to rebase. The original 13% arm cannot be treated as a permanent all-time high. A blank/corrupt high-water ledger disables profitable exits and does **not** fall back to stop-loss selling.
- The old stop-loss buyer cooldown continues to recognize **historical filled `v2sl-` exits** until their existing next-Monday expiry; it is expected to become dormant once no new stops occur. Successful profit exits never impose that cooldown.

## Service & safeguards

- Repository: `JSPierceColorado/alpaca-seller-bot` (`main`), service **V2-Seller**, Railway project Investing (production).
- High-water observations persist on attached `seller-highwater` volume mounted at `/data`; do not remove or replace the volume casually.
- Live trading remains guarded by `ALPACA_PAPER=false`, `DRY_RUN=false`, `ENABLE_LIVE_TRADING=true`, and `LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_SELLS`. Alpaca credentials are configured in Railway; never paste keys in chat or logs.
- `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_SHEET_ID` remain optional for the manually invoked read-only `--check-membership` diagnostic. **Live seller decisions no longer fetch or require S&P 100 membership.**
- `--check-now` previews current eligible exits without posting orders. Neither this switch nor `--check-membership` places orders.
- The seller does not control broker-forced liquidations, account margin requirements, or any other bot's sell orders. The no-stop policy increases potential losses on speculative stocks and may increase margin-call risk.

## Verification

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py hwm_tracker.py
```

**No sale is guaranteed until the profit limit fills.** Long-held positions may remain below cost, and performance monitoring should evaluate drawdowns as well as realized profit and available margin.
