# Crypto Advisor (1-month predictor with demo trading)

Predicts, for every liquid coin, what it is likely to do over the **next 30 days**, ranks the coins, and gives an
entry, stop-loss, target and position size. There is **no minimum-gain rule** (no "must gain 10%"): coins are ranked
by realistic potential, whatever its size. A virtual-money account lets you practise and track results.

## Run on your computer
    pip install -r requirements.txt
    streamlit run app.py

## Free hosting (works on your phone)
Upload the files to a GitHub repo, then at share.streamlit.io choose the repo, main file `app.py`, Deploy.
The GitHub workflow (`.github/workflows/ci.yml`) compiles, lints and tests on every push and can build an Android APK
(set a repository variable `APP_URL` to your Streamlit link).

## How the prediction works
- One gradient-boosting model (scikit-learn) trained on every coin's history (5 years where available).
- Inputs: 3 to 90-day returns, volatility, RSI, distance from 20/50/200-day averages and highs, volume surge and size,
  coin age, strength versus the whole market, market trend and breadth, Bitcoin trend.
- Outputs per coin: median 30-day move, 10th and 90th percentile range, chance of being up, chance of beating the typical coin.
  Ranking score = half median move, half chance of beating the market. Probabilities are shrunk toward the base rate by a factor
  measured in testing, because the raw model is over-confident.
- Coins are filtered for feasibility (daily USD volume and history length) by risk profile: Balanced, Growth, Aggressive.

## Precision is measured, not promised
**Tools > Model accuracy** runs a walk-forward test: for each of the last 9 months the model is trained only on outcomes known
at that time, predicts the next 30 days, and is compared with what happened (rank correlation, share of top picks that rose,
return versus the average coin, probability calibration). The app labels the result Strong / Moderate / Weak / Unproven and shows
it above the picks. If the model has no real edge on your coin list, you will see "Weak". Nobody can predict crypto precisely.

Caveats shown in the app: few test months; the coin list is today's survivors (dead coins are missing, which flatters any backtest); costs ignored in the test.

## Global policy and news
- **In the model:** dollar index, US 10-year yield, VIX, Nasdaq and gold (30-day changes) are tested with and without; kept only if the out-of-sample rank correlation did not get worse.
- **News & policy tab:** Google News headlines from the last 7 days (regulation, central banks, ETFs, security, India policy, geopolitics) with a keyword tone score. If it reads Risk-off, every coin gets a mild confidence cut.
- **Events tab:** add dated events (rate meetings, ETF rulings, laws, token unlocks) or let Gemini find them. Events in the next 30 days lower the chance-up, widen the range and flag the coin.
- These two layers are judgment overlays and cannot be backtested; the app says so.
- **Gemini prompts** include your country (Settings), the headline tone, the event calendar and a required action checklist, and Gemini must state whether it agrees with the app's model.

## Never lose the portfolio
- Phone: the browser copy is rewritten after every change, plus a dated snapshot each day (7 days kept).
- Every time you open the app the newer of the phone copy and the server copy wins and the server is updated.
- 6 pm India time: the first open after 6 pm saves a dated backup on the server (14 kept). A phone browser cannot run while closed, so nothing can upload at exactly 6 pm with the app shut.
- Durable copy: add `GITHUB_TOKEN` (token with only the *gist* permission) to Streamlit secrets. The account is then saved to a private Gist after changes and restored automatically if the free server wipes its disk.
- The GitHub Action pings the app at 6 pm India time to wake it (the 6 pm backup itself runs when you open the app).

## Demo trading
Virtual dollars (default $10,000), spot buys only, market or limit orders, stop-loss and target on every buy, fractional coins.
Fees + slippage (default 0.2%) are built into each fill price, so Total P&L always equals Net worth minus Starting cash.
Stop-loss, target and limit orders are checked whenever the app is open (crypto never closes).
Accounts are saved per name and mirrored in the browser; use Export backup on free hosting.

## Gemini (optional)
Copy-and-paste flow (no key) or in-app chat/reviews with a free key from aistudio.google.com (sidebar > Settings, or Streamlit secrets
`GEMINI_API_KEY`). Prompts cover market structure, ETF flows, derivatives, on-chain and tokenomics, regulation and macro, and allow any coin.

## Coin list and data
Yahoo Finance (`SYMBOL-USD`, a few coins carry a numeric suffix, see `YF_MAP` in `engine.py`). Tickers that return no data are skipped.
Lists: Major (~45), Broad (~130), CoinGecko top 300 (needs access to api.coingecko.com). Add your own symbols in Settings.
Stablecoins and wrapped tokens are excluded.

## Not financial advice
Crypto can lose most of its value quickly. This is a research aid. If you are in India, check the current tax rules on crypto gains before trading real money.
