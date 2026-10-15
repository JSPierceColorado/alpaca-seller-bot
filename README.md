# Alpaca Seller Bot (Railway Cron)

A **one-shot** sell-only exit checker for **long U.S. stock/ETF positions** in an Alpaca Trading account. It checks every held U.S. stock/ETF during normal U.S. equity market hours using Alpaca's market clock, and submits an order to close the **entire position** when the reported total unrealized return reaches either threshold:

- **Stop loss:** `unrealized_plpc <= -0.05` (loss of 5% or more)
- **Take profit:** `unrealized_plpc >= 0.10` (gain of 10% or more)

`unrealized_plpc` is Alpaca's fraction based on the position's **average entry price**, not today's price change. Thresholds are fixed in `main.py`. The bot does not open positions or place buy orders. It ignores options, crypto, and short positions; it does not cancel open orders. It skips a position if any open order already exists for that symbol, to avoid order conflicts.

**Important:** This is NOT an exchange-held stop order or a real-time price monitor. Checks can be late, markets can gap, Alpaca quote/position updates can lag, market orders can slip, and execution/fills are not guaranteed. A 10-minute interval can miss a stop or fill at a substantially worse price. Consider native Alpaca stop/bracket orders if you need protection between checks.

## Files

- `main.py`: a single poll-and-exit run; market clock, thresholds, safe filtering, open-order guard, close-position requests, logging.
- `requirements.txt`: pins Alpaca's Python SDK.
- `Dockerfile`: lightweight Python job; exits after the check.
- `tests/test_main.py`: local simulation tests; no credentials or real orders needed.
- `.env.example`: variable names only, never actual secrets.

## Local verification

```bash
python -m unittest discover -s tests -v # logic tests use fake SDK classes; no install needed
python -m pip install -r requirements.txt # for a real Alpaca dry-run connection
cp .env.example .env # Fill in your paper credentials locally
set -a; source .env; set +a # bash/zsh only
python main.py
```

The code is **paper mode + dry-run** by default. You need paper Alpaca API keys to test connection. Do not push `.env` (it is gitignored).

## GitHub -> Railway deployment

1. Create a **private** GitHub repository, such as `alpaca-seller-bot`, and commit these files at the repository root. If you use the GitHub CLI from this directory, run:

   ```bash
   git init
   git add .
   git commit -m "Initial Alpaca sell-only risk bot"
   gh repo create alpaca-seller-bot --private --source=. --remote=origin --push
   ```

   Otherwise create a private repo on GitHub and upload these files through the GitHub UI.
2. In Railway, choose **New Project -> Deploy from GitHub Repo** and select the repository. The root `Dockerfile` is automatically detected.
3. Set Railway service **Variables** (do NOT commit keys):

   ```dotenv
   APCA_API_KEY_ID=your_key
   APCA_API_SECRET_KEY=your_secret
   ALPACA_PAPER=true
   DRY_RUN=true
   ENABLE_LIVE_TRADING=false
   ```

4. In the Railway service **Settings -> Cron Schedule**, set **`*/10 * * * 1-5`**. Railway cron uses **UTC**; the bot itself uses Alpaca's market clock to skip holidays, early closings, and any time outside regular U.S. equity trading hours. The service should have **one replica** and **no public domain**. Leave **Start Command** blank to use the Dockerfile's `CMD` (or set it to `python main.py`).
5. Deploy, and inspect **Railway logs**. A run will either say `Market closed`, log `CHECK` rows and potential `DRY RUN` exits, or report an error (nonzero exit).
6. After confirming behavior with paper keys, set `DRY_RUN=false` to enable actual *paper* sell orders. Test with small paper positions first.
7. **Only if you explicitly decide to trade real money:** switch to live trading keys and simultaneously set `ALPACA_PAPER=false`, `DRY_RUN=false`, `ENABLE_LIVE_TRADING=true` after reviewing the code. All eligible holdings may be automatically liquidated when thresholds trigger.

Railway runs this process once per cron tick and it exits. Do **not** use both a 24/7 service loop and Railway Cron. Railway does not guarantee minute-perfect starts and skips the next invocation if a previous one is still running.

## Behavior and guardrails

- Alpaca market clock is checked *before* retrieving orders and positions. Only Alpaca trading days/hours are eligible; no time-zone guesswork.
- If a symbol has any **open order**, it is skipped without canceling or modifying orders, even if it passes a threshold. Check those orders manually.
- If 500 open orders are returned (possible truncation), the job aborts **without placing orders**, to avoid missing an open order.
- Dry run submits **no** orders; outputs actions to logs.
- Submission failures make the job fail, and the next Railway invocation can retry if needed. Inspect pending/open orders before manually rerunning.
- This bot runs only on a single Alpaca account identified by the supplied keys. Never store keys in GitHub or screenshots.

## Useful links

- Alpaca: https://alpaca.markets/sdks/python/api_reference/trading/positions.html
- Railway cron: https://docs.railway.com/cron-jobs
- Railway Dockerfiles: https://docs.railway.com/builds/dockerfiles
