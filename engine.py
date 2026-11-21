"""Crypto 1-month predictor: features, walk-forward model, trade plans, paper-trading maths, Gemini prompts.

Only pandas / numpy / scikit-learn (no Streamlit, no network), so everything here is unit-tested.

What the model does (and does not do)
-------------------------------------
* It predicts, for every coin, the distribution of the next 30-day return (10th / 50th / 90th percentile) plus the
  probability that the coin is UP and that it BEATS the median coin. There is no minimum-gain threshold: coins are
  simply ranked by their predicted 1-month potential.
* The model is trained on all coins pooled together and is always validated walk-forward (train on the past, predict the
  next month, repeat). Those out-of-sample numbers are shown in the app instead of any claimed accuracy.
* Crypto is extremely noisy. A good result here is "the ranking is better than chance", not "we know the price".
"""
import csv as _csv
import datetime as _dt
import io as _io
import json as _json
import re as _re
from math import ceil, erf, log, sqrt

import numpy as np
import pandas as pd

FILES_VERSION = 1
HORIZON = 30          # days ahead that the model predicts (crypto trades 7 days a week)
DPY = 365             # trading days per year for crypto

# ---------------------------------------------------------------------------------------------------------------
# Coin universe. Yahoo Finance tickers are SYMBOL-USD; some newer coins carry a numeric suffix (YF_MAP). Any symbol
# that returns no data is skipped automatically, so a wrong or retired ticker never breaks the app.
# ---------------------------------------------------------------------------------------------------------------
STABLES = set("""USDT USDC DAI FDUSD TUSD USDE PYUSD USDD USDS BUSD USDP GUSD FRAX LUSD USD1 RLUSD EURC EURT USDG XAUT PAXG""".split())
WRAPPED = set("WBTC WETH STETH WSTETH WEETH CBBTC CBETH RETH WBNB BTCB WBETH SOLVBTC".split())
EXCLUDED = STABLES | WRAPPED

MAJOR = """BTC ETH XRP BNB SOL DOGE TRX ADA LINK AVAX XLM BCH LTC DOT HBAR SHIB XMR ATOM ETC NEAR ALGO VET FIL ICP AAVE
MKR OP INJ RUNE UNI APT ARB SUI TON CRO KAS POL RENDER TAO FET LDO IMX STX""".split()
BROAD = list(dict.fromkeys(MAJOR + """PEPE BONK WIF FLOKI SEI TIA JUP ONDO ENA WLD PYTH EGLD SAND MANA AXS THETA CRV COMP SNX GALA CHZ ENJ
KAVA ZEC DASH EOS XTZ NEO IOTA QTUM ZIL BAT SUSHI YFI CAKE KSM FLOW APE QNT BSV FLR JASMY AR ROSE CELO ANKR GMX DYDX GRT
1INCH LRC ZRX BAND OCEAN STORJ SKL CKB ICX ONT WAVES RVN XEM HOT GLM MINA AUDIO SUPER ENS PENDLE JTO W STRK MANTA
BLUR PEOPLE TRB API3 CFX KDA LUNC LUNA USTC TWT CVX SSV RPL FXS GNO BICO RSR ACH AGIX NMR UMA REN BNT KNC CELR""".split()))
YF_MAP = {"TON": "TON11419-USD", "UNI": "UNI7083-USD", "APT": "APT21794-USD", "ARB": "ARB11841-USD", "GRT": "GRT6719-USD",
          "SUI": "SUI20947-USD", "IMX": "IMX10603-USD", "STX": "STX4847-USD", "PEPE": "PEPE24478-USD", "TIA": "TIA22861-USD",
          "TAO": "TAO22974-USD", "POL": "POL28321-USD", "JUP": "JUP29210-USD", "W": "W-USD", "ENA": "ENA-USD", "WLD": "WLD-USD",
          "PYTH": "PYTH-USD", "JTO": "JTO-USD", "STRK": "STRK22691-USD", "MANTA": "MANTA13631-USD", "ONDO": "ONDO-USD",
          "RENDER": "RENDER-USD", "SEI": "SEI-USD", "KAS": "KAS-USD", "BONK": "BONK-USD", "WIF": "WIF-USD", "FLOKI": "FLOKI-USD"}
_YF_REV = {v: k for k, v in YF_MAP.items()}
UNIVERSES = {"Major coins (~45, fastest)": MAJOR, "Broad (~130 coins)": BROAD,
             "CoinGecko top 300 (slowest, needs internet access to CoinGecko)": None}


def yf_ticker(sym):
    """'BTC' -> 'BTC-USD'. A string that already contains '-' is taken as a Yahoo ticker."""
    sym = str(sym).strip().upper()
    return sym if "-" in sym else YF_MAP.get(sym, sym + "-USD")


def sym_of(ticker):
    t = str(ticker).strip().upper()
    return _YF_REV.get(t) or (t[:-4] if t.endswith("-USD") else t)


def clean_symbols(symbols):
    out = [str(s).strip().upper() for s in symbols]
    return list(dict.fromkeys(s for s in out if s and s not in EXCLUDED))


# ---------------------------------------------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------------------------------------------
def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _prep(close, volume=None):
    close = close.sort_index().astype(float).ffill(limit=3)
    close = close.where(close > 0).dropna(axis=1, how="all")
    vol = None
    if volume is not None:
        vol = volume.reindex(index=close.index, columns=close.columns).astype(float)
        vol = vol.where(vol > 0)
    return close, vol


MACRO = {"DXY": "DX-Y.NYB", "US10Y": "^TNX", "VIX": "^VIX", "NASDAQ": "^IXIC", "GOLD": "GC=F"}   # Yahoo tickers of the global-policy backdrop


def macro_features(macro, index):
    """Daily global backdrop aligned to the crypto calendar (weekends carry the last value): 30-day change of the dollar index,
    Nasdaq and gold, the 10-year yield change in points, and the VIX level and its 30-day change. These move with Fed and
    global policy, so they are the measurable part of 'policy' that the model can learn from."""
    m = macro.sort_index().reindex(macro.index.union(index)).ffill().reindex(index)
    out = {}
    for k in ("DXY", "NASDAQ", "GOLD"):
        if k in m: out[f"mac_{k.lower()}30"] = np.log(m[k] / m[k].shift(30))
    if "US10Y" in m: out["mac_us10y30"] = m["US10Y"] - m["US10Y"].shift(30)
    if "VIX" in m:
        out["mac_vix"] = m["VIX"]; out["mac_vix30"] = np.log(m["VIX"] / m["VIX"].shift(30))
    return out


def make_dataset(close, volume=None, macro=None):
    """(X, y, yrel): one row per (date, coin).
    X = features known at the close of `date`. y = log return over the NEXT 30 days (NaN for the last 30 days).
    yrel = y minus that day's median y across coins (did it beat the typical coin?)."""
    close, vol = _prep(close, volume)
    lr = np.log(close).diff()
    F = {f"r{n}": np.log(close / close.shift(n)) for n in (3, 7, 14, 30, 60, 90)}
    F["vol30"] = lr.rolling(30, min_periods=20).std() * sqrt(DPY)
    F["vol90"] = lr.rolling(90, min_periods=60).std() * sqrt(DPY)
    F["volchg"] = np.log(F["vol30"] / F["vol90"])
    F["rsi"] = _rsi(close)
    for n in (20, 50, 200):
        F[f"d{n}"] = close / close.rolling(n, min_periods=int(n * 0.75)).mean() - 1
    F["dd90"] = close / close.rolling(90, min_periods=60).max() - 1
    F["hi365"] = close / close.rolling(365, min_periods=120).max() - 1
    F["lo90"] = close / close.rolling(90, min_periods=60).min() - 1
    F["age"] = np.log1p(close.notna().cumsum())
    nan = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    if vol is not None:
        F["vsurge"] = np.log(vol.rolling(7, min_periods=4).mean() / vol.rolling(60, min_periods=30).mean())
        F["dvol"] = np.log10(vol.rolling(30, min_periods=15).mean())
    else:
        F["vsurge"], F["dvol"] = nan, nan.copy()
    mk = np.exp(lr.mean(axis=1).fillna(0).cumsum())                       # equal-weight index of the universe
    M = {"mk_r7": np.log(mk / mk.shift(7)), "mk_r30": np.log(mk / mk.shift(30)), "mk_r90": np.log(mk / mk.shift(90)),
         "mk_d200": mk / mk.rolling(200, min_periods=150).mean() - 1,
         "breadth": (F["r30"] > 0).astype(float).where(F["r30"].notna()).mean(axis=1),
         "mk_vol": F["vol30"].median(axis=1)}
    if macro is not None and len(macro):
        M.update(macro_features(macro, close.index))
    M["btc_r30"] = F["r30"]["BTC"] if "BTC" in close else M["mk_r30"]
    M["btc_d200"] = F["d200"]["BTC"] if "BTC" in close else M["mk_d200"]
    F["rel7"] = F["r7"].sub(M["mk_r7"], axis=0)
    F["rel30"] = F["r30"].sub(M["mk_r30"], axis=0)
    for k, src in (("rk7", "r7"), ("rk30", "r30"), ("rk90", "r90"), ("rkvol", "vol30"), ("rkdv", "dvol")):
        F[k] = F[src].rank(axis=1, pct=True)
    idx = pd.MultiIndex.from_product([close.index, close.columns], names=["date", "coin"])
    X = pd.DataFrame({k: v.reindex(index=close.index, columns=close.columns).to_numpy().ravel() for k, v in F.items()}, index=idx)
    for k, s in M.items():
        X[k] = np.repeat(s.reindex(close.index).to_numpy(), close.shape[1])
    fwd = np.log(close.shift(-HORIZON) / close)
    yrel = fwd.sub(fwd.median(axis=1), axis=0)
    y = pd.Series(fwd.to_numpy().ravel(), index=idx, name="y")
    yrel = pd.Series(yrel.to_numpy().ravel(), index=idx, name="yrel")
    X = X.replace([np.inf, -np.inf], np.nan)
    return X, y, yrel





def _feats(X):
    return list(X.columns)


# ---------------------------------------------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------------------------------------------
PROFILES = {"Balanced (large caps)": dict(min_val=2e7, min_days=365),
            "Growth (mid caps)": dict(min_val=5e6, min_days=180),
            "Aggressive (small caps & memecoins)": dict(min_val=1e6, min_days=60)}
DEFAULT_PROFILE = list(PROFILES)[0]
ALL_MODELS = ("q10", "q50", "q90", "up", "beat")


def eligible(Xr, profile):
    """Coins that are feasible to trade: enough 30-day USD volume and enough history."""
    p = PROFILES[profile]
    ok = Xr["r30"].notna() & Xr["vol30"].notna()
    if Xr["dvol"].notna().any():
        ok &= Xr["dvol"] >= log(p["min_val"]) / log(10)
    ok &= Xr["age"] >= log(1 + p["min_days"])
    return ok


def train(X, y, yrel, last_date, step=3, fast=False, which=ALL_MODELS, max_rows=60000, seed=0):
    """Fit the models on rows with date <= last_date (labels fully known). Returns {name: fitted model} or None."""
    from sklearn.ensemble import HistGradientBoostingClassifier as Clf
    from sklearn.ensemble import HistGradientBoostingRegressor as Reg
    d = X.index.get_level_values(0)
    ok = (d <= last_date) & y.notna().to_numpy() & X["r30"].notna().to_numpy() & X["vol30"].notna().to_numpy()
    dates = np.sort(pd.unique(d[ok]))
    if len(dates) < 150:
        return None
    step = max(step, int(ceil(ok.sum() / max_rows)))
    keep = dates[::-1][::step]                      # newest date first, so the freshest data is always used
    sel = ok & d.isin(keep)
    feats = _feats(X)
    Xtr, ytr, yrtr = X.loc[sel, feats], y[sel].clip(-1.0, 1.0), yrel[sel]
    common = dict(max_iter=60 if fast else 120, learning_rate=0.05, max_depth=3, min_samples_leaf=150,
                  l2_regularization=5.0, early_stopping=False, random_state=seed)
    out = {"_feats": feats, "_base_up": float((ytr > 0).mean())}
    for name, q in (("q10", .1), ("q50", .5), ("q90", .9)):
        if name in which:
            out[name] = Reg(loss="quantile", quantile=q, **common).fit(Xtr, ytr)
    for name, lab in (("up", (ytr > 0).astype(int)), ("beat", (yrtr > 0).astype(int))):
        if name in which:
            out[name] = Clf(**common).fit(Xtr, lab) if lab.nunique() == 2 else float(lab.mean())
    return out


def predict(models, Xr):
    """Predictions as ordinary percentages: g_lo / g_med / g_hi = 10th / 50th / 90th percentile 30-day return."""
    f = Xr[models["_feats"]]
    out = pd.DataFrame(index=Xr.index)
    qs = [k for k in ("q10", "q50", "q90") if k in models]
    if qs:
        arr = np.column_stack([models[k].predict(f) for k in qs])
        if len(qs) == 3:
            arr = np.sort(arr, axis=1)              # quantiles must not cross
        for k, col in zip(qs, arr.T):
            out[{"q10": "g_lo", "q50": "g_med", "q90": "g_hi"}[k]] = np.expm1(col)
    for k, name in (("up", "p_up"), ("beat", "p_beat")):
        if k in models:
            m = models[k]
            out[name] = m.predict_proba(f)[:, 1] if hasattr(m, "predict_proba") else m
    return out


def calibrate(pred, models, k):
    """Shrink the probabilities toward the training base rate by factor k (0..1). The raw model is over-confident out-of-sample;
    k is measured by walk_forward() (slope of what really happened against what was predicted)."""
    pred = pred.copy()
    if "p_up" in pred:
        b = models.get("_base_up", 0.5)
        pred["p_up"] = (b + k * (pred["p_up"] - b)).clip(0.02, 0.98)
    if "p_beat" in pred:
        pred["p_beat"] = (0.5 + k * (pred["p_beat"] - 0.5)).clip(0.02, 0.98)
    return pred


def _rk(x):
    return x.rank(pct=True) * 100


def potential_score(pred):
    """0-100 ranking score: half predicted median gain, half chance of beating the typical coin."""
    return 0.5 * _rk(pred["g_med"]) + 0.5 * _rk(pred["p_beat"])


def snapshot(close, volume=None, min_len=30):
    """Latest price, returns, volatility, RSI, moving averages, ATR and traded value for every coin."""
    close, vol = _prep(close, volume)
    rows = {}
    for c in close.columns:
        s = close[c].dropna()
        if len(s) < min_len:
            continue
        p = s.iloc[-1]
        ret = lambda n: p / s.iloc[-1 - n] - 1 if len(s) > n else np.nan
        lr = np.log(s).diff().dropna().tail(30)
        v = vol[c].dropna() if vol is not None else None
        rows[c] = dict(price=p, r7=ret(7), r30=ret(30), r90=ret(90), r365=ret(365),
                       vol=lr.std() * sqrt(DPY) if len(lr) > 5 else np.nan, rsi=_rsi(s).iloc[-1],
                       dma20=s.rolling(20).mean().iloc[-1], dma50=s.rolling(50).mean().iloc[-1],
                       dma200=s.rolling(200).mean().iloc[-1], hi365=s.iloc[-365:].max(), lo90=s.iloc[-90:].min(),
                       atr=s.diff().abs().rolling(14).mean().iloc[-1], days=len(s),
                       avg_val=float(v.tail(30).mean()) if v is not None and len(v) else np.nan,
                       vsurge=float(v.tail(7).mean() / v.tail(60).mean()) if v is not None and len(v) >= 10 else np.nan)
    return pd.DataFrame(rows).T.astype(float)


def predict_latest(close, volume=None, profile=DEFAULT_PROFILE, step=3, models=None, data=None, shrink=1.0):
    """Train on everything with a known 30-day outcome, predict the latest day. Returns (table, models, asof)."""
    X, y, yrel = data if data is not None else make_dataset(close, volume)
    dates = X.index.get_level_values(0)
    asof = dates.max()
    last_known = dates.unique().sort_values()[-1 - HORIZON]
    models = models or train(X, y, yrel, last_known, step=step)
    if models is None:
        raise ValueError("Not enough price history to train the model (need about 6 months of data for several coins).")
    Xr = X.xs(asof, level=0)
    Xr = Xr[eligible(Xr, profile)]
    if len(Xr) < 5:
        raise ValueError("Fewer than 5 coins pass the liquidity filter. Choose a looser risk profile or a bigger coin list.")
    pred = calibrate(predict(models, Xr), models, shrink)
    pred["score"] = potential_score(pred)
    snap = snapshot(close, volume)
    tbl = snap.join(pred, how="inner").sort_values("score", ascending=False)
    tbl["rank"] = np.arange(1, len(tbl) + 1)
    return tbl, models, asof


# ---------------------------------------------------------------------------------------------------------------
# Walk-forward validation: the only honest measure of how good the predictions are
# ---------------------------------------------------------------------------------------------------------------
def walk_forward(X, y, yrel, n_folds=9, topn=5, step=3, profile=DEFAULT_PROFILE, fast=True, max_rows=40000):
    """Every 30 days: train ONLY on data whose outcome was already known, predict the next 30 days, compare with what
    really happened. Returns dict(folds=DataFrame, summary=dict, calib=DataFrame)."""
    dates = X.index.get_level_values(0).unique().sort_values()
    last_t = len(dates) - 1 - HORIZON
    folds, pool = [], []
    for tp in [last_t - HORIZON * k for k in range(n_folds)][::-1]:
        if tp - HORIZON < 250:
            continue
        mdl = train(X, y, yrel, dates[tp - HORIZON], step=step, fast=fast, which=("q50", "up", "beat"), max_rows=max_rows)
        if mdl is None:
            continue
        Xt = X.xs(dates[tp], level=0)
        Xt = Xt[eligible(Xt, profile)]
        df = predict(mdl, Xt).join(y.xs(dates[tp], level=0).rename("y")).join(Xt["r30"].rename("r30"))
        df = df.dropna(subset=["y", "g_med", "p_beat", "r30"])
        if len(df) < max(8, topn + 3):
            continue
        df["score"] = potential_score(df)
        real = np.expm1(df["y"])
        med = real.median()
        top = df["score"].nlargest(topn).index
        btop = df["r30"].nlargest(topn).index           # naive baseline: buy last month's biggest gainers
        folds.append(dict(date=dates[tp], coins=len(df),
                          ic=df["score"].corr(df["y"], method="spearman"), ic_naive=df["r30"].corr(df["y"], method="spearman"),
                          top_ret=real[top].mean(), top_win=(real[top] > 0).mean(), top_beat=(real[top] > med).mean(),
                          naive_ret=real[btop].mean(), market_ret=real.mean(), market_median=med, up_share=(real > 0).mean()))
        pool.append(pd.DataFrame({"p_up": df["p_up"], "up": (real > 0).astype(float), "base": mdl["_base_up"]}))
    f = pd.DataFrame(folds)
    if f.empty:
        return dict(folds=f, summary={}, calib=pd.DataFrame())
    f["cum_top"] = (1 + f["top_ret"]).cumprod()
    f["cum_market"] = (1 + f["market_ret"]).cumprod()
    f["cum_naive"] = (1 + f["naive_ret"]).cumprod()
    P = pd.concat(pool)
    var = P["p_up"].var()
    k = float(np.clip(P["p_up"].cov(P["up"]) / var, 0.2, 1.0)) if var and var > 0 else 0.5
    P["p_raw"] = P["p_up"]
    P["p_up"] = (P["base"] + k * (P["p_raw"] - P["base"])).clip(0.02, 0.98)
    base = P["up"].mean()
    summ = dict(shrink=k, n_folds=len(f), ic=f["ic"].mean(), ic_naive=f["ic_naive"].mean(), ic_pos=(f["ic"] > 0).mean(),
                top_ret=f["top_ret"].mean(), market_ret=f["market_ret"].mean(), naive_ret=f["naive_ret"].mean(),
                edge=(f["top_ret"] - f["market_ret"]).mean(), top_win=f["top_win"].mean(), top_beat=f["top_beat"].mean(),
                base_up=f["up_share"].mean(), brier=((P["p_up"] - P["up"]) ** 2).mean(), brier_base=((base - P["up"]) ** 2).mean(),
                topn=topn)
    summ["verdict"], summ["verdict_text"] = reliability(summ)
    P["bin"] = pd.cut(P["p_up"], [0, .3, .4, .5, .6, .7, 1.0])
    calib = P.groupby("bin", observed=True).agg(n=("up", "size"), predicted=("p_up", "mean"), actual=("up", "mean")).reset_index()
    calib["bin"] = calib["bin"].astype(str)
    return dict(folds=f, summary=summ, calib=calib)


def run(close, volume=None, profile=DEFAULT_PROFILE, n_folds=9, topn=5, macro=None):
    """Whole pipeline: features -> walk-forward validation -> final model -> calibrated predictions for today.
    If `macro` (global backdrop) is given, it is tested both with and without; it is only used if it did not hurt
    out-of-sample. Returns dict(table, asof, val, macro_used, macro_ic, base_ic)."""
    data0 = make_dataset(close, volume)
    val0 = walk_forward(*data0, n_folds=n_folds, topn=topn, profile=profile)
    data, val, used, mic = data0, val0, False, None
    if macro is not None and len(macro):
        data1 = make_dataset(close, volume, macro)
        val1 = walk_forward(*data1, n_folds=n_folds, topn=topn, profile=profile)
        mic = val1["summary"].get("ic")
        if val1["summary"] and (not val0["summary"] or mic >= val0["summary"]["ic"]):
            data, val, used = data1, val1, True
    k = val["summary"].get("shrink", 0.5)
    table, _, asof = predict_latest(close, volume, profile, data=data, shrink=k)
    return dict(table=table, asof=asof, val=val, macro_used=used, macro_ic=mic, base_ic=val0["summary"].get("ic"))


# ---------------------------------------------------------------------------------------------------------------
# Policy / event overlay: a judgment layer (not backtestable) on top of the model
# ---------------------------------------------------------------------------------------------------------------
EVENT_KINDS = ["Central bank / macro", "Regulation / law / tax", "ETF or exchange decision", "Token unlock / upgrade", "Hack / failure / other"]
SEVERITY = {"Low": 0.90, "Medium": 0.75, "High": 0.55}
DIRECTIONS = ["Uncertain", "Negative", "Positive"]
COUNTRIES = {"India": "At last check India taxed crypto gains at a flat rate with a 1% TDS on transfers, and exchanges must follow local rules. Confirm current rules.",
             "United States": "Crypto gains are taxable and exchanges report to the IRS; SEC/CFTC rulings and spot-ETF decisions move the whole market.",
             "European Union": "MiCA governs exchanges and stablecoins; coins and stablecoins can be restricted or delisted for EU users.",
             "United Kingdom": "FCA rules limit promotions and some products for retail users; gains are taxable.",
             "UAE / Singapore": "Licensed-exchange regimes apply; check which coins and products your platform may offer.",
             "Other": "Check your local tax and licensing rules before trading real money."}


def apply_events(tbl, events, asof):
    """Adjust a prediction table for upcoming events (dicts: date 'YYYY-MM-DD', title, kind, coins list or ['ALL'], severity, direction).
    Only events inside the next 30 days count. Effect: lower confidence (probabilities toward 50%, wider range, smaller median move);
    'Negative' / 'Positive' also nudges the median. Adds an event_flags column and re-ranks."""
    t = tbl.copy(); t["event_flags"] = ""
    end = pd.Timestamp(asof) + pd.Timedelta(days=HORIZON)
    for ev in events or []:
        try: d = pd.Timestamp(ev["date"])
        except Exception: continue
        if not (pd.Timestamp(asof).normalize() <= d <= end): continue
        f = SEVERITY.get(ev.get("severity"), 0.75)
        coins = ev.get("coins") or ["ALL"]
        hit = t.index if "ALL" in [c.upper() for c in coins] else [c for c in t.index if c in {x.upper() for x in coins}]
        for c in hit:
            med, lo, hi = t.at[c, "g_med"], t.at[c, "g_lo"], t.at[c, "g_hi"]
            nudge = {"Negative": -1, "Positive": 1}.get(ev.get("direction"), 0) * (1 - f) * 0.2
            t.at[c, "g_lo"] = med - (med - lo) / f; t.at[c, "g_hi"] = med + (hi - med) / f
            t.at[c, "g_med"] = med * f + nudge
            t.at[c, "p_up"] = float(np.clip(0.5 + f * (t.at[c, "p_up"] - 0.5) + nudge, 0.02, 0.98))
            t.at[c, "p_beat"] = float(np.clip(0.5 + f * (t.at[c, "p_beat"] - 0.5) + nudge / 2, 0.02, 0.98))
            t.at[c, "event_flags"] += ("; " if t.at[c, "event_flags"] else "") + f"{d:%d %b}: {ev.get('title', 'event')} ({ev.get('severity', 'Medium')})"
    t["score"] = potential_score(t)
    t = t.sort_values("score", ascending=False); t["rank"] = np.arange(1, len(t) + 1)
    return t


def build_events_prompt(country, today, coins):
    return (f"You are a cautious cryptocurrency policy and macro analyst. Today is {today:%d %b %Y}. The trader is in: {country}.\n"
            f"TASK: List scheduled or highly likely events in the NEXT 30 DAYS (using Google Search / web browsing) that could move crypto prices: "
            f"central-bank meetings and rate decisions (Fed, ECB, BoJ, RBI), key inflation/jobs data, government or regulator decisions and deadlines "
            f"(SEC/CFTC, spot-ETF decisions, MiCA, tax or ban proposals, stablecoin laws, court rulings, sanctions, elections), exchange listings or delistings, "
            f"major token unlocks and protocol upgrades. Prefer events with a known date. Include events specific to these coins where relevant: {', '.join(coins) or 'any'}. "
            f"Do not predict prices and never promise outcomes.\n"
            f'Reply ONLY with a JSON array (no other text, no markdown) of up to 12 objects, each exactly like: '
            f'{{"date":"YYYY-MM-DD","title":"short title","kind":"{" | ".join(EVENT_KINDS)}","coins":["ALL"] or ["SOL","ETH"],'
            f'"severity":"Low | Medium | High","direction":"Uncertain | Negative | Positive","why":"1 sentence"}}')


def parse_events(text):
    """Pasted Gemini reply -> list of event dicts, or None if no JSON list was found."""
    m = _re.search(r"\[.*\]", text or "", _re.S)
    if not m: return None
    try: data = _json.loads(m.group(0))
    except Exception: return None
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not d.get("title"): continue
        try: date = pd.Timestamp(str(d.get("date"))).strftime("%Y-%m-%d")
        except Exception: continue
        sev = str(d.get("severity", "Medium")).title(); dr = str(d.get("direction", "Uncertain")).title()
        coins = d.get("coins") or ["ALL"]; coins = [norm_symbol(c) for c in (coins if isinstance(coins, list) else [coins])] or ["ALL"]
        out.append(dict(date=date, title=str(d["title"])[:90], kind=str(d.get("kind", "")) or EVENT_KINDS[-1], coins=coins,
                        severity=sev if sev in SEVERITY else "Medium", direction=dr if dr in DIRECTIONS else "Uncertain", why=str(d.get("why", ""))))
    return out[:20]


def reliability(s):
    """('Strong' | 'Moderate' | 'Weak' | 'Unproven', plain-language explanation) from out-of-sample results."""
    if not s or s.get("n_folds", 0) < 3:
        return "Unproven", "Not enough history to test the model out-of-sample yet. Treat every number as a guess."
    ic, pos, edge = s["ic"], s["ic_pos"], s["edge"]
    if ic >= 0.08 and pos >= 0.65 and edge > 0 and s["n_folds"] >= 8:
        return "Strong", ("In past out-of-sample months the ranking clearly beat chance and the market. That is encouraging, but it is "
                          "still a probability, never a promise, and a handful of months is a small sample.")
    if ic >= 0.03 and pos >= 0.55 and s["n_folds"] >= 4:
        return "Moderate", ("The ranking was somewhat better than chance in past out-of-sample months. Use it to shortlist coins, "
                            "size positions small and always use stop-losses.")
    return "Weak", ("In past out-of-sample months the ranking was barely better than chance. Use it only as a shortlist and do not "
                    "rely on any single prediction.")


# ---------------------------------------------------------------------------------------------------------------
# Trade plan, signals, tags
# ---------------------------------------------------------------------------------------------------------------
def _f(x):
    try:
        x = float(x)
    except Exception:
        return None
    return x if x == x else None


def round_qty(q):
    q = max(float(q), 0.0)
    return round(q) if q >= 1000 else round(q, 4) if q >= 1 else round(q, 6)


def plan(r, capital, risk_pct, max_pos_pct=20.0):
    """Entry, stop-loss, targets and position size for a 1-month trade. r: a row of the prediction table (price, atr, rsi,
    dma20, g_med, g_hi). The stop sits 2.5 average daily moves below entry (8%-30% away), the target is the model's
    optimistic (90th percentile) case with the median case as the 'expected' price."""
    p = float(r["price"])
    atr = _f(r.get("atr")) or 0.04 * p
    rsi_, d20 = _f(r.get("rsi")), _f(r.get("dma20"))
    wait = bool(rsi_ and rsi_ >= 75 and d20 and d20 < p)
    entry = d20 if wait else p
    dist = min(max(2.5 * atr, 0.08 * entry), 0.30 * entry)
    stop = entry - dist
    hi, med = _f(r.get("g_hi")), _f(r.get("g_med"))
    target = entry * (1 + max(hi if hi is not None else 0.15, 0.05))
    expected = entry * (1 + (med if med is not None else 0.0))
    qty = round_qty(min(capital * risk_pct / 100 / dist, capital * max_pos_pct / 100 / entry))
    return dict(action=f"Wait for dip to ~${entry:,.4g}" if wait else "Buy now", entry=entry, stop=stop, target=target,
                expected=expected, qty=qty, invest=qty * entry, max_loss=qty * dist, wait=wait)


def qty_for(price, stop, capital, risk_pct, max_pos_pct=20.0):
    """Coins to buy: risk-based if the stop is below the price, else 10% of capital; never above max_pos_pct of capital."""
    if not price or price <= 0:
        return 0.0
    q = capital * risk_pct / 100 / (price - stop) if stop and stop < price else capital * 0.10 / price
    return round_qty(min(q, capital * max_pos_pct / 100 / price))


def idea_signal(r):
    """('BUY' | 'HOLD' | 'SELL', reason) for a prediction-table row."""
    pu, gm, rsi_ = _f(r.get("p_up")), _f(r.get("g_med")), _f(r.get("rsi"))
    if pu is None or gm is None:
        return "HOLD", "No model output"
    if gm <= 0 or pu < 0.45:
        return "SELL", "Model expects a flat or falling month: avoid"
    if rsi_ and rsi_ > 78:
        return "HOLD", "Overheated (RSI above 78): wait for a pullback"
    if pu < 0.52:
        return "HOLD", "Model leans up only slightly"
    return "BUY", "Model leans up with a positive expected month"


def signal(r, price=None, stop=None, target=None):
    """('BUY' | 'HOLD' | 'SELL', reason) for a holding, from stop/target and trend. Rules of thumb, not advice."""
    p = _f(price) or _f(r.get("price"))
    if not p:
        return "HOLD", "No price available"
    d20, d50, rsi_, r30 = (_f(r.get(k)) for k in ("dma20", "dma50", "rsi", "r30"))
    pu, gm = _f(r.get("p_up")), _f(r.get("g_med"))
    if stop and p <= stop * 1.01:
        return "SELL", "At or below your stop-loss"
    if target and p >= target * 0.99:
        return "SELL", "Target almost reached: consider booking profit"
    if d50 and p < d50 and r30 is not None and r30 < -0.10:
        return "SELL", "Below the 50-day average with falling momentum"
    if pu is not None and gm is not None and gm < 0 and pu < 0.45:
        return "SELL", "Model now expects a falling month"
    if rsi_ and rsi_ > 78:
        return "HOLD", "Overheated (RSI above 78): avoid adding until it cools"
    if d50 and d20 and p > d20 > d50 and (r30 or 0) > 0.05 and (rsi_ or 50) < 72:
        return "BUY", "Uptrend with healthy momentum"
    return "HOLD", "No strong signal either way"


def tags_for(r, scan=False):
    """Tags for a coin card: list of (text, kind) where kind is buy | sell | hold | info | risk | muted."""
    out = []
    vol, rsi_, r30, hi, p, av = (_f(r.get(k)) for k in ("vol", "rsi", "r30", "hi365", "price", "avg_val"))
    if vol is not None:
        out.append(("Extreme volatility", "sell") if vol >= 1.2 else ("High volatility", "risk") if vol >= 0.8
                   else ("Medium volatility", "hold") if vol >= 0.5 else ("Lower volatility", "buy"))
    pu = _f(r.get("p_up"))
    if pu is not None:
        out.append((f"{pu * 100:.0f}% chance up", "buy" if pu >= 0.55 else "hold" if pu >= 0.48 else "sell"))
    if r30 is not None and r30 > 0.30:
        out.append(("Strong 30d momentum", "buy"))
    if p and hi and p / hi > 0.95:
        out.append(("Near 1Y high", "info"))
    if rsi_ is not None and rsi_ > 75:
        out.append(("Overbought", "risk"))
    if rsi_ is not None and rsi_ < 30:
        out.append(("Oversold", "info"))
    if scan and av is not None:
        out.append(("Thin volume", "risk") if av < 5e6 else ("Liquid", "muted"))
    return out


def regime(close, volume=None):
    """One-line market backdrop: ('bull' | 'bear' | 'mixed', text) from BTC trend and breadth of the coin list."""
    c, _ = _prep(close, volume)
    ref = c["BTC"].dropna() if "BTC" in c else c.mean(axis=1).dropna()
    if len(ref) < 210:
        return "mixed", "Not enough history for a market-trend read."
    above = ref.iloc[-1] > ref.rolling(200).mean().iloc[-1]
    r30 = ref.iloc[-1] / ref.iloc[-31] - 1
    r30s = (c.iloc[-1] / c.iloc[-31] - 1).dropna()
    br = (r30s > 0).mean() if len(r30s) else float("nan")
    txt = (f"Bitcoin is {'above' if above else 'below'} its 200-day average, {r30 * 100:+.0f}% over 30 days; "
           f"{br * 100:.0f}% of coins rose over the last 30 days.")
    if above and br >= 0.5:
        return "bull", txt
    if not above and br < 0.4:
        return "bear", txt + " Bear-market conditions: rallies fail more often, so keep positions small."
    return "mixed", txt


def p_hit(gain_pct, vol, months=1):
    """Rough chance of gaining >= gain_pct (zero-drift lognormal). A sanity check next to the model, not a forecast."""
    s = vol * sqrt(months / 12)
    if s <= 0:
        return 0.0
    z = (log(1 + gain_pct / 100) + 0.5 * s * s) / s
    return 0.5 * (1 - erf(z / sqrt(2)))


# ---------------------------------------------------------------------------------------------------------------
# Demo (paper) trading. Quantities are fractional coins; fees/slippage are baked into the fill price by the app.
# ---------------------------------------------------------------------------------------------------------------
EPS = 1e-9


def positions(trades):
    """Open positions (average-cost method) and realised P&L from a trade list."""
    pos, realised = {}, 0.0
    for t in trades:
        p = pos.setdefault(t["symbol"], dict(qty=0.0, cost=0.0, stop=None, target=None, since=t["ts"], horizon=None))
        if t["side"] == "BUY":
            if p["qty"] <= EPS:
                p.update(qty=0.0, since=t["ts"], cost=0.0, stop=None, target=None, horizon=None)
            p["cost"] += t["qty"] * t["price"]
            p["qty"] += t["qty"]
            if t.get("horizon"):
                p["horizon"] = t["horizon"]
            if t.get("stop"):
                p["stop"], p["target"] = t["stop"], t.get("target")
        elif p["qty"] > EPS:
            q = min(t["qty"], p["qty"])
            avg = p["cost"] / p["qty"]
            realised += (t["price"] - avg) * q
            p["cost"] -= avg * q
            p["qty"] -= q
            if p["qty"] <= EPS:
                p["qty"], p["cost"] = 0.0, 0.0
    return {k: v for k, v in pos.items() if v["qty"] > EPS}, realised


def ledger(trades):
    """Every SELL as a closed trade with its own realised P&L (same average-cost method as positions())."""
    pos, out = {}, []
    for t in trades:
        p = pos.setdefault(t["symbol"], dict(qty=0.0, cost=0.0))
        if t["side"] == "BUY":
            if p["qty"] <= EPS:
                p["qty"], p["cost"] = 0.0, 0.0
            p["cost"] += t["qty"] * t["price"]
            p["qty"] += t["qty"]
        elif p["qty"] > EPS:
            q = min(t["qty"], p["qty"])
            avg = p["cost"] / p["qty"]
            out.append(dict(ts=t["ts"], symbol=t["symbol"], qty=q, buy=avg, sell=t["price"],
                            pnl=(t["price"] - avg) * q, pct=(t["price"] / avg - 1) * 100))
            p["cost"] -= avg * q
            p["qty"] -= q
            if p["qty"] <= EPS:
                p["qty"], p["cost"] = 0.0, 0.0
    return out


def _live(ltp, s, fallback):
    try:
        v = float(ltp.get(s))
    except Exception:
        return fallback
    return v if v == v and v > 0 else fallback


def cash_balance(start_cash, trades):
    return start_cash + sum((1 if t["side"] == "SELL" else -1) * t["qty"] * t["price"] for t in trades)


def performance(trades, start_cash, ltp):
    """Realised + unrealised P&L in $ and %, win rate, net worth. Always: total = net worth - starting cash."""
    pos, realised = positions(trades)
    closed = ledger(trades)
    cash = cash_balance(start_cash, trades)
    inv = sum(p["cost"] for p in pos.values())
    cur = sum(p["qty"] * _live(ltp, s, p["cost"] / p["qty"]) for s, p in pos.items())
    sold_cost = sum(c["buy"] * c["qty"] for c in closed)
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    unreal = cur - inv
    total = realised + unreal
    return dict(realised=realised, realised_pct=realised / sold_cost * 100 if sold_cost else 0.0,
                unrealised=unreal, unrealised_pct=unreal / inv * 100 if inv else 0.0,
                total=total, total_pct=total / start_cash * 100 if start_cash else 0.0,
                invested=inv, current=cur, cash=cash, net=cash + cur,
                closed=len(closed), wins=len(wins), losses=len(losses),
                win_rate=len(wins) / len(closed) * 100 if closed else 0.0,
                best=max(closed, key=lambda c: c["pnl"]) if closed else None,
                worst=min(closed, key=lambda c: c["pnl"]) if closed else None)


def history(trades, close, start_cash):
    """Daily net worth (cash + holdings at closing prices) from the day of the first trade."""
    if not trades:
        return pd.Series(dtype=float)
    t = pd.DataFrame(trades)
    t["d"] = pd.to_datetime(t.ts).dt.normalize()
    close = close.ffill()
    days = close.index[close.index >= t.d.min()]
    if len(days) == 0:
        return pd.Series(dtype=float)
    syms = list(dict.fromkeys(t.symbol))
    sg = np.where(t.side == "BUY", 1, -1)
    di = np.clip(days.searchsorted(t.d.values), 0, len(days) - 1)
    Q = np.zeros((len(days), len(syms)))
    cf = np.zeros(len(days))
    np.add.at(Q, (di, [syms.index(x) for x in t.symbol]), sg * t.qty.values)
    np.add.at(cf, di, -sg * t.qty.values * t.price.values)
    Q = Q.cumsum(0)
    cash = start_cash + cf.cumsum()
    P = np.zeros_like(Q)
    for j, s_ in enumerate(syms):
        if s_ in close.columns:
            P[:, j] = close[s_].reindex(days).ffill().bfill().fillna(0).values
        else:
            P[:, j] = float(t[t.symbol == s_].price.iloc[-1])
    return pd.Series(cash + (Q * P).sum(1), index=days)


# ---------------------------------------------------------------------------------------------------------------
# International news and government-policy monitor (headline lexicon score; an overlay, not a backtested model input)
# ---------------------------------------------------------------------------------------------------------------
NEWS_QUERIES = {"Global regulation": "crypto regulation OR SEC crypto OR MiCA OR crypto ban OR stablecoin law",
                "Central banks & macro": "Federal Reserve rate decision OR ECB rate OR inflation data bitcoin",
                "ETFs & institutions": "bitcoin ETF OR ether ETF OR crypto ETF flows",
                "Security & failures": "crypto exchange hack OR crypto exploit OR crypto bankruptcy",
                "India policy": "India crypto tax OR RBI crypto OR SEBI crypto OR India crypto regulation",
                "Geopolitics & sanctions": "sanctions crypto OR tariffs bitcoin OR war markets crypto"}
_NEG = ("ban", "bans", "banned", "crackdown", "lawsuit", "sues", "sued", "charges", "probe", "investigation", "fraud", "hack", "hacked", "exploit", "stolen",
        "bankrupt", "insolvency", "sanction", "sanctions", "restrict", "restriction", "delist", "delisting", "collapse", "plunge", "crash", "slump", "outflow",
        "outflows", "rate hike", "tariff", "tariffs", "penalty", "fine", "seize", "warning", "risk-off", "liquidation", "liquidations", "shutdown", "tax hike", "rejects", "rejected", "denies")
_POS = ("approve", "approves", "approved", "approval", "adopt", "adoption", "legalize", "legal tender", "license", "licence", "clarity", "rate cut", "cuts rates",
        "easing", "partnership", "inflow", "inflows", "record", "rally", "surge", "soars", "launch", "launches", "win", "wins", "backs", "friendly", "greenlight", "etf approval")


def score_headline(title):
    """-1 (bad for crypto) ... +1 (good) from a keyword count. Crude on purpose: it flags tone, it does not understand news."""
    t = " " + _re.sub(r"[^a-z0-9\- ]", " ", str(title).lower()) + " "
    n = sum(1 for w in _NEG if f" {w} " in t)
    p = sum(1 for w in _POS if f" {w} " in t)
    return 0.0 if n + p == 0 else (p - n) / (p + n)


def parse_rss(xml_text, limit=15):
    """Items (title, source, date, link) from a Google News RSS reply."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if title:
            out.append(dict(title=title, source=(it.findtext("source") or "").strip(), date=(it.findtext("pubDate") or "").strip(), link=(it.findtext("link") or "").strip()))
    return out[:limit]


def policy_risk(groups):
    """groups: {name: [headline dicts]} -> dict(score, label, n, by_group, worst). label: 'Risk-off' | 'Mixed' | 'Supportive' | 'No data'."""
    by, allsc, worst = {}, [], []
    for g, items in groups.items():
        sc = [(score_headline(i["title"]), i) for i in items]
        if sc:
            by[g] = sum(s for s, _ in sc) / len(sc)
        allsc += sc
        worst += [(s, i) for s, i in sc if s < 0]
    n = len(allsc)
    score = sum(s for s, _ in allsc) / n if n else 0.0
    label = "No data" if n < 6 else "Risk-off" if score <= -0.2 else "Supportive" if score >= 0.2 else "Mixed"
    return dict(score=score, label=label, n=n, by_group=by, worst=[i for _, i in sorted(worst, key=lambda x: x[0])[:5]])


# ---------------------------------------------------------------------------------------------------------------
# Saving the account: newest copy wins (phone <-> server), daily 6 pm snapshot
# ---------------------------------------------------------------------------------------------------------------
BACKUP_HOUR = 18   # 6 pm India time


def stamp():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def newer(a, b):
    """Which account dict is newer by its 'saved_at': 'a', 'b' or 'same'. A copy with no stamp is oldest."""
    sa, sb = (a or {}).get("saved_at") or "", (b or {}).get("saved_at") or ""
    return "same" if sa == sb else "a" if sa > sb else "b"


def due_daily_backup(now_local, last_date):
    """True when it is 6 pm or later and today's snapshot has not been taken yet. now_local: a datetime in India time."""
    return now_local.hour >= BACKUP_HOUR and str(last_date or "") != f"{now_local:%Y-%m-%d}"


# ---------------------------------------------------------------------------------------------------------------
# Gemini: copy-and-paste flow and prompts
# ---------------------------------------------------------------------------------------------------------------
PROMPT_KINDS = ["Next 30-day gainers", "Review my demo portfolio", "My own question"]
IDEA_KINDS = [PROMPT_KINDS[0], PROMPT_KINDS[2]]
_ASK = {PROMPT_KINDS[0]: "Pick the cryptocurrencies with the highest realistic gain potential over roughly the next 30 days, from ANY coin that is liquid "
                         "enough to trade on major exchanges. There is NO minimum gain: rank by realistic potential, whatever the size.",
        PROMPT_KINDS[1]: "Review my demo holdings below and say what to buy more of, hold or avoid, and suggest better alternatives."}

RESEARCH_RULES = (
    "You may recommend ANY cryptocurrency that trades on major exchanges (large, mid or small cap, new listings, memecoins), not only the coins in the app data; "
    "the app data is only one input. Avoid illiquid, pump-and-dump, rug-pull-risk or freshly launched tokens and say how liquid each pick is.\n"
    "Base every pick on ALL of these, using the latest information you can find (use Google Search / web browsing):\n"
    "1. MARKET STRUCTURE: Bitcoin trend and dominance, the total market cap trend, risk-on/risk-off mood, stablecoin supply, exchange inflows/outflows, "
    "spot ETF flows (BTC/ETH and any others), large-wallet (whale) activity.\n"
    "2. DERIVATIVES: funding rates, open interest, liquidations, options positioning. Crowded long positioning raises the risk of a sharp drop.\n"
    "3. ON-CHAIN AND PROJECT FUNDAMENTALS: active addresses, transaction counts, fees/revenue, TVL, developer activity, staking, token supply, "
    "upcoming token unlocks and emissions, burns, buybacks, upgrades or mainnet launches, exchange listings or delistings, partnerships.\n"
    "4. NEWS, MACRO AND REGULATION: US Fed and interest-rate path, dollar strength, inflation data, equity-market risk appetite, SEC/CFTC and other regulator actions, "
    "stablecoin and ETF rules, MiCA, tax changes, exchange hacks or failures, geopolitics.\n"
    "5. PRICE ACTION AND RISK: trend, volume, volatility, key support/resistance, and what could go wrong.\n"
    "Be balanced and include what the bears say. Crypto is extremely volatile: any coin can lose most of its value, so never promise returns and never say a move is certain. "
    "This is not financial advice.\n"
    "REQUIRED ACTIONS (do these in order before answering): (a) search for the last 7 days of crypto, regulation and macro news worldwide; "
    "(b) check the event calendar given below and search for any other scheduled event in the next 30 days that could move the coins you consider; "
    "(c) apply the trader's country rules given below; (d) compare each pick with the app's statistical model and say whether you agree, and why if not; "
    "(e) downgrade or drop any pick that faces a high-severity negative event, and say so in the risk field; (f) state your policy_view for each pick.")

SYSTEM_RULES = ("You are a cautious, thorough cryptocurrency research assistant for a retail trader (prices in US dollars). "
                "Use the app data provided as one input and say when something is not in it. " + RESEARCH_RULES)

_FORMAT = ('Reply ONLY with a JSON array (no other text, no markdown) of up to 8 objects, each exactly like: '
           '{"symbol":"ticker e.g. BTC","name":"coin name","action":"Buy now | Wait for dip | Avoid",'
           '"entry":number in US dollars,"stop_loss":number,"target":number,"horizon":"1 month",'
           '"confidence":"low | medium | high","liquidity":"high | medium | low","reason":"max 2 sentences",'
           '"market_derivs":"market structure / ETF flows / derivatives view, 1 sentence",'
           '"onchain_fundamentals":"on-chain activity, tokenomics, unlocks or upgrades, 1 sentence",'
           '"news_regulation":"key news, macro or regulatory driver, 1 sentence",'
           '"policy_view":"how global and local government policy affects this coin in the next 30 days, 1 sentence",'
           '"agrees_with_app_model":"yes | partly | no","risk":"main risk, 1 sentence"}')


def build_prompt(kind, own, context, today):
    ask = (own.strip() or "Suggest cryptocurrencies worth researching.") if kind == PROMPT_KINDS[2] else _ASK[kind]
    return (f"You are a cautious, thorough cryptocurrency research assistant for a retail trader (prices in US dollars). "
            f"Today is {today:%d %b %Y}.\nTASK: {ask}\n{RESEARCH_RULES}\n\nApp data (one input only):\n{context}\n\n{_FORMAT}")


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("USD", "").strip())
    except Exception:
        return None


def norm_symbol(s):
    """'BINANCE:BTCUSDT', 'btc-usd', 'ETH/USD', 'SOL' -> 'BTC', 'BTC', 'ETH', 'SOL'."""
    s = _re.sub(r"^[A-Z]+:", "", str(s).upper().strip())
    t = _re.sub(r"[-/_ ]?(USDT|USDC|USD|PERP)$", "", s)
    return t if t else s


def parse_recs(text):
    """Extract recommendation objects from a pasted Gemini reply. Returns a list (maybe empty) or None if no JSON found."""
    m = _re.search(r"\[.*\]", text or "", _re.S)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except Exception:
        return None
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not d.get("symbol"):
            continue
        g = lambda k: str(d.get(k, "") or "")
        out.append(dict(symbol=norm_symbol(d["symbol"]), name=g("name") or g("company"), action=g("action"), entry=_num(d.get("entry")),
                        stop=_num(d.get("stop_loss", d.get("stop"))), target=_num(d.get("target")), horizon=g("horizon"),
                        confidence=g("confidence"), liquidity=g("liquidity"), reason=g("reason"), risk=g("risk"),
                        market_derivs=g("market_derivs"), onchain_fundamentals=g("onchain_fundamentals"), news_regulation=g("news_regulation"),
                        policy_view=g("policy_view"), agrees=g("agrees_with_app_model")))
    return out[:10]


REVIEW_SYSTEM = ("You are a cautious cryptocurrency research assistant for a retail trader (prices in US dollars). "
                 "You review ONLY the holdings the trader lists. Never suggest, compare or mention any coin that is not in the list, "
                 "and do not give market outlook or new coin ideas. Never promise returns. This is not financial advice.")
_REVIEW_FORMAT = ('Reply ONLY with a JSON array (no other text, no markdown): exactly one object for EVERY holding listed above and no other objects. '
                  'Each object exactly like: {"symbol":"ticker as listed","action":"Hold | Buy more | Sell part | Sell all",'
                  '"qty":number of coins to buy or sell (0 for Hold),"new_stop_loss":number or null,"new_target":number or null,'
                  '"horizon":"1 month","confidence":"low | medium | high","reason":"max 2 sentences","risk":"1 sentence"}')


def build_review_prompt(rows, perf, today):
    """Prompt that covers ONLY the user's holdings. rows: dicts with symbol, qty, avg, ltp, pnl, pnl_pct, weight, stop, target, horizon."""
    n = lambda v, d=2: "n/a" if v is None or v != v else f"{v:,.{d}f}"
    nq = lambda v: "n/a" if v is None or v != v else f"{v:,.6g}"
    lines = [f"- {r['symbol']}: {nq(r['qty'])} coins, bought avg ${nq(r['avg'])}, now ${nq(r['ltp'])}, P&L ${n(r['pnl'])} ({n(r['pnl_pct'])}%), "
             f"{n(r['weight'], 1)}% of holdings, stop-loss {nq(r.get('stop'))}, target {nq(r.get('target'))}" for r in rows] or ["- (no holdings yet)"]
    head = (f"Holdings value ${n(perf['current'], 0)} (invested ${n(perf['invested'], 0)}), unrealised P&L ${n(perf['unrealised'], 0)} "
            f"({n(perf['unrealised_pct'])}%).")
    return (f"You are a cautious, thorough cryptocurrency research assistant for a retail trader (prices in US dollars). Today is {today:%d %b %Y}.\n"
            f"TASK: Review ONLY the holdings listed below. Do not suggest, compare or mention any coin that is not in the list, and do not give market outlook or new ideas.\n"
            f"For each holding say Hold, Buy more, Sell part or Sell all, with a quantity, a better stop-loss and target, and the reason. "
            f"Use the latest information you can find (Google Search / web browsing) about THESE coins only: on-chain and project news, token unlocks, ETF or exchange news, "
            f"derivatives positioning, and regulation or macro events that directly affect them. Also point out if any one holding is too large a share of the list, "
            f"losers that are down for a reason that has not changed, and winners worth protecting with a higher stop-loss. Be balanced and include the bear case. "
            f"Never promise returns. This is not financial advice.\n\n"
            f"My holdings:\n{head}\n" + "\n".join(lines) + f"\n\n{_REVIEW_FORMAT}")


def parse_review(text):
    """Pasted portfolio-review reply -> list of action dicts (maybe empty), or None if no JSON list was found."""
    m = _re.search(r"\[.*\]", text or "", _re.S)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except Exception:
        return None
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not d.get("symbol"):
            continue
        a = str(d.get("action", "")).lower()
        act = ("HOLD" if a.strip().startswith("hold") else "SELL ALL" if ("sell" in a and ("all" in a or "exit" in a))
               else "SELL PART" if ("sell" in a or "trim" in a or "reduce" in a) else "BUY NEW" if "new" in a
               else "BUY MORE" if ("buy" in a or "add" in a or "accumulate" in a) else "HOLD")
        q = _num(d.get("qty"))
        g = lambda k: str(d.get(k, "") or "")
        out.append(dict(symbol=norm_symbol(d["symbol"]), action=act, qty=q if q and q > 0 else 0.0,
                        stop=_num(d.get("new_stop_loss", d.get("stop_loss"))), target=_num(d.get("new_target", d.get("target"))),
                        horizon=g("horizon"), confidence=g("confidence"), reason=g("reason"), risk=g("risk")))
    return out[:25]


def parse_symbol_list(text):
    """Symbols from a CoinGecko /coins/markets JSON reply, a CSV with a symbol column, or a comma/space separated list."""
    text = text or ""
    try:
        data = _json.loads(text)
        if isinstance(data, list):
            return clean_symbols(d.get("symbol", "") for d in data if isinstance(d, dict))
    except Exception:
        pass
    try:
        rows = list(_csv.DictReader(_io.StringIO(text)))
        if rows and any((k or "").strip().lower() == "symbol" for k in rows[0]):
            return clean_symbols((r.get("symbol") or r.get("Symbol") or "") for r in rows)
    except Exception:
        pass
    return clean_symbols(_re.split(r"[\s,;]+", text))


# ---------------------------------------------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------------------------------------------
# ---------- Themes (ColorOS 7 style glass etc.) ----------
DEFAULT_THEME = "Clean Light"
THEMES = {
    "Classic": None,
    "Clean Light": dict(
        dark=False, base="#f4f5f7", acc_text="#ffffff", blobs=[], radius=14,
        card="#ffffff", side="#ffffff", border="#e6e8ec", shadow="0 1px 3px rgba(16,24,40,.07)",
        accent="#00b386", text="#1f2230", muted="#6b7083", pop="#ffffff"),
    "ColorOS 7 Glass": dict(
        dark=False, base="#eaf1fa", acc_text="#ffffff",
        blobs=[("120,200,255,.55", "12% 8%"), ("150,230,190,.50", "88% 35%"), ("255,205,170,.35", "40% 95%")],
        card="rgba(255,255,255,.80)", side="rgba(255,255,255,.88)", border="rgba(255,255,255,.95)",
        shadow="0 8px 28px rgba(31,60,120,.14)", accent="#0e9f5a", text="#0b1424", muted="#3b4759", pop="#ffffff"),
    "Midnight Glass": dict(
        dark=True, base="#070b16", acc_text="#06101f",
        blobs=[("60,90,220,.40", "10% 10%"), ("130,60,210,.30", "90% 30%"), ("0,160,170,.25", "45% 95%")],
        card="rgba(22,30,56,.72)", side="rgba(8,12,26,.88)", border="rgba(255,255,255,.22)",
        shadow="0 8px 28px rgba(0,0,0,.50)", accent="#7ab8ff", text="#f4f7ff", muted="#c4cde3", pop="#1a2038"),
    "Aurora": dict(
        dark=True, base="#08121f", acc_text="#04201b",
        blobs=[("0,230,190,.35", "8% 12%"), ("150,80,255,.35", "92% 25%"), ("255,90,170,.25", "50% 100%")],
        card="rgba(18,34,56,.72)", side="rgba(8,18,32,.88)", border="rgba(255,255,255,.26)",
        shadow="0 10px 32px rgba(0,0,0,.45)", accent="#35f0cf", text="#f3fcff", muted="#bcd3e0", pop="#16263c"),
    "Sunset": dict(
        dark=False, base="#fff1e6", acc_text="#ffffff",
        blobs=[("255,150,100,.45", "10% 10%"), ("255,110,160,.35", "90% 30%"), ("255,215,120,.40", "45% 95%")],
        card="rgba(255,255,255,.82)", side="rgba(255,248,242,.90)", border="rgba(255,255,255,.95)",
        shadow="0 8px 28px rgba(160,70,40,.16)", accent="#d9482a", text="#2a1510", muted="#5e4036", pop="#ffffff"),
    "Neon Terminal": dict(
        dark=True, base="#04060a", acc_text="#00140a", font="ui-monospace, SFMono-Regular, Menlo, monospace",
        blobs=[("0,255,140,.08", "15% 10%"), ("0,200,255,.06", "90% 70%")],
        card="rgba(6,18,12,.85)", side="rgba(4,8,6,.92)", border="rgba(0,255,140,.55)",
        shadow="0 0 16px rgba(0,255,140,.25)", accent="#00ff8c", text="#e4ffef", muted="#9fd8b8", pop="#0c1a14"),
}

def theme_css(name, blur=16, radius=22, animate=True, font_px=16, contrast=False):
    t = THEMES.get(name)
    if not t:
        return f"<style>html{{font-size:{font_px}px}}</style>" if font_px != 16 else ""
    t = dict(t); radius = t.get("radius", radius)
    if contrast:  # solid, no blur, maximum contrast
        dark = t["dark"]
        t.update(card="#000000" if dark else "#ffffff", side="#000000" if dark else "#ffffff",
                 border="#ffffff" if dark else "#000000", text="#ffffff" if dark else "#000000",
                 muted="#e6e6e6" if dark else "#1a1a1a", base="#000000" if dark else "#ffffff", blobs=[], shadow="none")
        blur, animate = 0, False
    layers = ",".join(f"radial-gradient(circle at {pos}, rgba({c}) 0, transparent 45%)" for c, pos in t["blobs"])
    bg = f"{layers}, {t['base']}" if layers else t["base"]
    glass = f"backdrop-filter: blur({blur}px) saturate(160%); -webkit-backdrop-filter: blur({blur}px) saturate(160%);" if blur else ""
    anim = "background-size: 150% 150%; animation: oslide 22s ease-in-out infinite alternate;" if animate and layers else ""
    font = f"font-family: {t['font']};" if t.get("font") else ""
    tx, mu, ac, at, bd = t["text"], t["muted"], t["accent"], t["acc_text"], t["border"]
    return f"""<style>
html {{ font-size: {font_px}px; }}
@keyframes oslide {{ 0% {{ background-position: 0% 0%; }} 100% {{ background-position: 100% 100%; }} }}
@media (prefers-reduced-motion: reduce) {{ .stApp {{ animation: none !important; }} }}
.stApp {{ background: {bg}; background-attachment: fixed; {anim} color: {tx}; {font} }}
header[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li, [data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2, [data-testid="stMarkdownContainer"] h3, [data-testid="stMarkdownContainer"] h4,
[data-testid="stMarkdownContainer"] strong, [data-testid="stWidgetLabel"] p, [data-testid="stMetricLabel"] p, [data-testid="stMetricValue"],
h1, h2, h3, label, .stRadio label p, .stCheckbox label p, .stButton button p, .stDownloadButton button p,
[data-testid^="stBaseLinkButton"] p, [data-testid="stExpander"] summary p, [data-testid="stAlert"] p {{ color: {tx} !important; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: {mu} !important; }}
input, textarea {{ color: {tx} !important; -webkit-text-fill-color: {tx} !important; }}
::placeholder {{ color: {mu} !important; opacity: 1 !important; -webkit-text-fill-color: {mu} !important; }}
[data-testid="stSidebar"] > div:first-child {{ background: {t['side']}; {glass} border-right: 1px solid {bd}; }}
[class*="st-key-card"], [data-testid="stMetric"], [data-testid="stExpander"], [data-testid="stForm"] {{
  background: {t['card']} !important; {glass} border: 1px solid {bd} !important; border-radius: {radius}px !important; box-shadow: {t['shadow']}; }}
[data-testid="stAlert"] {{ {glass} border: 1px solid {bd}; border-radius: {radius}px; }}
[data-testid="stMetric"] {{ padding: 10px 14px; }}
[data-testid="stExpander"] details {{ border: 0 !important; }}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button, [data-testid^="stBaseLinkButton"] {{
  border-radius: 999px !important; border: 1px solid {bd} !important; background: {t['card']}; {glass} box-shadow: {t['shadow']}; }}
button[kind="primary"], [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {{ background: {ac} !important; border: 0 !important; }}
button[kind="primary"] p, [data-testid="stBaseButton-primary"] p {{ color: {at} !important; }}
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div {{ border-radius: 16px !important; background: {t['card']} !important; border-color: {bd} !important; }}
[data-baseweb="popover"] > div, [data-baseweb="menu"], ul[role="listbox"] {{ background: {t['pop']} !important; }}
ul[role="listbox"] li, [data-baseweb="menu"] li {{ color: {'#f4f7ff' if t['dark'] else '#0b1424'} !important; }}
[data-testid="stVegaLiteChart"], [data-testid="stArrowVegaLiteChart"], [data-testid="stDataFrame"] {{
  border-radius: {radius}px; overflow: hidden; background: rgba(255,255,255,.95); padding: 6px; }}
</style>"""
