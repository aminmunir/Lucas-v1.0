# Lucas v1.0 — Start here (no coding required)

This is the short setup guide. The detailed explanation is in `README.md`.

## What Lucas does

- Looks at BTC/USDT and ETH/USDT using Kronos-base.
- Checks an intraday setup (15-minute candles, forecast up to 4 hours, 1-hour confirmation) and a swing setup (4-hour candles, forecast up to 24 hours, daily confirmation).
- Sends a Telegram alert only when the forecast path projects at least a 2% move and the confirmation timeframe agrees.
- Does not place trades. Forecasts can be wrong; use alert-only testing first.

## 1. Create a GitHub repository

1. Download and unzip `Lucas_v1.0.zip` on your computer.
2. Go to <https://github.com/new> and create a repository named `lucas-v1`.
3. **For zero-cost standard GitHub-hosted runner time, choose Public.** This means people can see the code and rules. The Telegram token is not in the files; it will be stored as a GitHub Secret. If you choose Private, GitHub Free has a 2,000-minute/month limit and this schedule may exceed it.
4. In the new repository choose **Add file → Upload files**, upload every file/folder from the unzipped project, including `.github/workflows/lucas.yml`, then commit the upload.

## 2. Make a Telegram bot

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts, and copy the token. Treat it like a password.
2. Open the newly created bot and press **Start**; then send it a message such as `hello`.
3. To find your numeric chat ID, open `https://api.telegram.org/botYOUR_TOKEN/getUpdates` in your own browser, replacing `YOUR_TOKEN` with the token from BotFather. In the result, find `message` → `chat` → `id`. Do not share this URL or a screenshot of it: it contains your bot token. Once finished, close the page and avoid saving the URL.

## 3. Add the two secrets to GitHub

In your GitHub repository, open **Settings → Secrets and variables → Actions → New repository secret**. Add:

- Name: `TELEGRAM_BOT_TOKEN` — Value: the BotFather token.
- Name: `TELEGRAM_CHAT_ID` — Value: the numeric chat ID.

Do not add these values to `config.yaml`, a commit, or this conversation.

## 4. Test Telegram without running Kronos

1. Open the repository's **Actions** tab and enable workflows if prompted.
2. Select **Lucas v1.0 signals** → **Run workflow**.
3. Set `test_telegram` to `true` and run it. You should receive a Telegram setup-test message. If not, check the two secrets and the workflow logs.

## 5. Run Kronos-base

1. Go to **Actions → Lucas v1.0 signals → Run workflow** again.
2. Keep `test_telegram` set to `false`. You can leave `force_swing` false for the first run; the bot automatically initializes its first swing scan.
3. The first run downloads Kronos-base and its tokenizer (roughly 425 MB combined) and may take several minutes. Subsequent runs can reuse a GitHub cache, but GitHub may evict caches.
4. The scheduled workflow then attempts to run every 15 minutes. GitHub can delay scheduled jobs; this is not a real-time service.
5. If no qualifying setup exists, Lucas will not send a trade signal. View **Actions → the selected run → run-lucas** for diagnostic logs.

## If Base is too slow

Open `config.yaml` in GitHub, click the pencil icon, change:

`model_id: "NeoQuasar/Kronos-base"`

to:

`model_id: "NeoQuasar/Kronos-small"`

Then commit the change. Base is the default; Small is only a fallback if Base can't run reliably inside your free runner constraints.

## Important limitations

- This is an alert-only research prototype. It never places trades.
- The 2% move is a model-forecast threshold, not a guarantee.
- “Confidence” is a heuristic label, not a calibrated success probability.
- Take-profit/stop-loss rules and assumed fees/slippage need validation against your exchange and product.
- Public repository code is visible to everyone. Store Telegram credentials only in GitHub Actions Secrets.
- The code and signal-rule unit tests were checked here. The actual model download, live Binance request, GitHub schedule and Telegram delivery still need the integration test in your account.
