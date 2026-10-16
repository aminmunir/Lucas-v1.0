# Lucas v1.0 — Kronos crypto forecast alerts

Lucas v1.0 is an alert-only research prototype that runs the public, pretrained **Kronos-base** financial-candlestick model on public BTC/USDT and ETH/USDT candles, applies explicit multi-timeframe rules, and can send LONG/SHORT candidate alerts to Telegram.

**It does not place trades. It does not guarantee a 2% market move or profitable outcomes.** Kronos forecasts are uncertain. Run historical validation and paper-trading tests before relying on signals.

## What Lucas does

- Uses `NeoQuasar/Kronos-base` and `NeoQuasar/Kronos-Tokenizer-base` by default.
- Reads public Binance spot candles and current prices. No Binance API key is needed.
- Does not send an actionable signal unless the primary timeframe's forecast path reaches the configured ±2% threshold, its terminal forecast remains directionally aligned, and the confirmation-timeframe terminal forecast agrees.
- Uses a fixed 2% take-profit reference and ATR-derived stop-loss; suppresses signals that fail the minimum reward/risk filter.
- Labels confidence heuristically from forecast magnitude and timeframe agreement. This is **not** a calibrated probability of success.
- Classifies volatility using ATR as a percentage of price relative to its recent history.
- Sends messages to Telegram when both required secrets are configured.
- Uses cache-based state to reduce duplicate alerts across scheduled GitHub Actions runs.
- Runs in alert-only mode; there is no order-placement API and no exchange trading key is requested.

## Configured timeframes

| Strategy | Primary candles | Forecast horizon | Confirmation candles | When checked |
|---|---|---|---|---|
| Intraday | 15m | 16 candles = 4 hours | 1h, 4 candles = 4 hours | Every 15 minutes |
| Swing | 4h | 6 candles = 24 hours | 1d, 2 candles = 48 hours | When the last closed 4h candle changes |

The workflow schedule is an evaluation schedule, not a real-time guarantee. GitHub-hosted scheduled jobs can be delayed. The forecast itself cannot reliably guarantee the time at which a target will be reached; Lucas reports the earliest forecast candle whose high/low crosses the reference target.

## Why Base?

Kronos-base is configured as Lucas's primary model. Its published checkpoint is approximately 409 MB and the matching base tokenizer is approximately 15.9 MB; weights and code are downloaded automatically on the first run and the Hugging Face cache is saved between runs where GitHub's cache is available. A larger model is not automatically more accurate. Compare Base with Small using the same historical periods before assuming it is better.

## What you need

1. A GitHub account.
2. A Telegram account.
3. A Telegram bot token from [@BotFather](https://t.me/BotFather).
4. Your Telegram chat ID.

No exchange API key, local Python installation, Hugging Face login, or payment is needed for the standard setup. The official Kronos code and public model weights are fetched during the workflow.

## Easiest setup (no coding)

### A. Create a GitHub repository

1. Download and unzip the Lucas v1.0 package.
2. Go to <https://github.com/new>.
3. Name the repository `lucas-v1`.
4. If **zero GitHub-hosted runner cost** is the priority, make the repository **Public**. Public-repository standard GitHub-hosted runner minutes are free, but source code and strategy rules are visible. Telegram secrets are still stored separately and must never be committed to the repository.
5. Create the repository, then choose **Add file → Upload files**. Upload the contents of this folder, including the hidden `.github` folder and `.github/workflows/lucas.yml`, and commit.

If you choose a Private repository, GitHub Free includes 2,000 standard runner minutes per month. The model setup/inference runtime may exceed that allowance if Lucas runs every 15 minutes; check actual usage before enabling the schedule for a full month.

### B. Create your Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Follow its prompts and copy the bot token it returns. Keep it private.
3. Open the new bot's chat and press **Start**.
4. To get your chat ID, open this URL in a browser, replacing `YOUR_BOT_TOKEN` with your token: `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates`. Look for `message` → `chat` → `id`. Do not share the URL or token with anyone. If there is no update, send the bot another message and refresh the URL.

### C. Add GitHub Secrets

In your repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add these two secrets:

- `TELEGRAM_BOT_TOKEN` — the token from BotFather.
- `TELEGRAM_CHAT_ID` — your numeric chat ID.

Never put either value in `config.yaml` or commit it in source code.

### D. Enable and test the workflow

1. Open the repository's **Actions** tab and enable workflows if GitHub asks.
2. Choose **Lucas v1.0 signals** → **Run workflow**.
3. For the first run, select `test_telegram = true` to send a Telegram setup test without loading the market model. Run it.
4. Run the workflow again with `test_telegram = false`. The first real run downloads Kronos-base and its tokenizer and may take significantly longer than later runs.
5. The workflow also starts automatically every 15 minutes. You can manually set `force_swing = true` to test the swing path regardless of whether a new 4-hour candle has closed.
6. View logs in **Actions → selected run → run-lucas**. If no trade candidate qualifies, no trading-signal message is expected.

## Telegram message fields

1. Token name, pair, strategy category and LONG/SHORT direction.
2. Current price and entry reference (current price at alert generation; not a guaranteed fill).
3. TP, SL, estimated gross target return and rough after-cost estimate.
4. Alert issue time, forecast horizon, and earliest modelled TP-touch candle.
5. Heuristic confidence label and timeframe agreement. This is not a success probability.
6. Volatility category and ATR percentage.

Default assumed costs in `config.yaml` are 0.10% fee per side and 0.05% slippage per side. These are placeholders; set them to assumptions appropriate for your exchange, product and fee tier. Short signals assume a market/product where shorting is possible, but Lucas only sends alerts and does not check your account's permission to open a short.

## Settings you can edit without coding

Open `config.yaml` in GitHub and use the pencil icon to edit values. Examples:

- `model.model_id`: defaults to `NeoQuasar/Kronos-base`. Use `NeoQuasar/Kronos-small` for a comparison run.
- `strategy.forecast_move_threshold_pct`: minimum forecasted excursion (default 2.0%).
- Intraday horizon: 16 × 15-minute forecast candles.
- Swing horizon: 6 × 4-hour forecast candles.
- TP percentage, ATR stop multiplier, minimum reward/risk and estimated costs.
- Watchlist: BTCUSDT and ETHUSDT.

Save your changes with **Commit changes**. Never put secrets in this file.

## What “confidence” means in Lucas

The HIGH/MODERATE label is a transparent heuristic based on the forecasted move magnitude and timeframe agreement. It is not a statistically calibrated confidence, nor an empirically measured win rate. A historical evaluation is needed before confidence can be represented as a probability.

## Free-tier and timing limitations

- GitHub Actions scheduled workflows have a minimum interval of five minutes but can be delayed, especially around busy periods. This is not a seconds-level alerting service.
- A public repository makes the code/config and strategy rules public. Secret values stored in GitHub Actions Secrets are not committed to source, but repository collaborators with workflow write access can trigger runs.
- Model caching helps avoid repeatedly downloading ~425 MB of combined Base and tokenizer weights; GitHub may evict caches, in which case they are downloaded again.
- A standard runner uses CPU in this workflow. If Base inference takes longer than expected, the scan schedule may backlog. Inspect run durations before relying on regular alerts.
- Binance public market data can fail or be regionally unavailable. The workflow tries two public endpoints, but no-data / model errors result in a failed run, not a fabricated signal.

## Validation status

This package includes unit tests for the signal-rule helpers. The actual Kronos model has not been run in this environment because this build environment has no outbound internet access for installing the model and downloading weights. The first GitHub Actions run is the real integration test. Validate historical forecast outcomes and paper-trade before acting on signals.

## References

- [Official Kronos code and README](https://github.com/shiyu-coder/Kronos)
- [Kronos-base weights](https://huggingface.co/NeoQuasar/Kronos-base)
- [Kronos base tokenizer](https://huggingface.co/NeoQuasar/Kronos-Tokenizer-base)
- [Binance public market-data API](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md)
- [GitHub Actions pricing and limits](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub scheduled workflow docs](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)
- [Telegram Bot API](https://core.telegram.org/bots/api)
