import os, sys, datetime as dt
from unittest import mock
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import engine as E, gemini_client as G, ui as U


def synth(n=1500, k=40, signal=True, seed=1):
    """Fake crypto market. With signal=True each coin has a slowly drifting hidden trend, so past momentum predicts the future."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end="2026-11-20", periods=n, freq="D")
    mkt = rng.normal(0.0003, 0.025, n)
    cols, vols = {}, {}
    for j, nm in enumerate(["BTC", "ETH"] + [f"C{i}" for i in range(k - 2)]):
        mu, m = np.zeros(n), rng.normal(0, 0.0015)
        for t in range(1, n):
            m = 0.985 * m + rng.normal(0, 0.0012); mu[t] = m
        r = (mu if signal else 0) + 0.9 * mkt + rng.normal(0, 0.03 + 0.01 * (j % 4), n)
        cols[nm] = 100 * np.exp(np.cumsum(r)); vols[nm] = np.exp(rng.normal(17 + (j % 5), 0.4, n))
    return pd.DataFrame(cols, index=idx), pd.DataFrame(vols, index=idx)

LOOSE = "Aggressive (small caps & memecoins)"


# ---------- data and leakage ----------
def test_features_never_use_the_future():
    c, v = synth(n=700, k=12)
    X1, y1, _ = E.make_dataset(c, v)
    c2, v2 = c.copy(), v.copy()
    c2.iloc[500:] *= 7.0; v2.iloc[500:] *= 3.0                      # change everything after day 500
    X2, _, _ = E.make_dataset(c2, v2)
    d = c.index[499]
    pd.testing.assert_frame_equal(X1.xs(d, level=0), X2.xs(d, level=0))

def test_label_is_next_30_day_log_return():
    c, v = synth(n=400, k=6)
    X, y, yrel = E.make_dataset(c, v)
    t = c.index[100]
    assert abs(y.loc[(t, "BTC")] - np.log(c["BTC"].iloc[130] / c["BTC"].iloc[100])) < 1e-9
    assert y.xs(c.index[-1], level=0).isna().all() and y.xs(c.index[-30], level=0).isna().all() and y.xs(c.index[-31], level=0).notna().all()
    assert abs(yrel.xs(t, level=0).median()) < 1e-9

def test_universe_helpers():
    assert E.yf_ticker("btc") == "BTC-USD" and E.yf_ticker("TON") == "TON11419-USD" and E.yf_ticker("HYPE-USD") == "HYPE-USD"
    assert E.sym_of("BTC-USD") == "BTC" and E.sym_of("TON11419-USD") == "TON" and E.sym_of("ETH-USD") == "ETH"
    assert E.clean_symbols(["btc", "USDT", " eth ", "BTC", "WBTC", ""]) == ["BTC", "ETH"]
    assert not (set(E.BROAD) & E.EXCLUDED) and set(E.MAJOR) <= set(E.BROAD) and len(E.BROAD) > 100

# ---------- model ----------
def test_predictions_are_sane():
    c, v = synth(k=30)
    tbl, models, asof = E.predict_latest(c, v, LOOSE)
    assert asof == c.index[-1] and len(tbl) == 30 and list(tbl["rank"]) == list(range(1, 31))
    assert (tbl.g_lo <= tbl.g_med + 1e-12).all() and (tbl.g_med <= tbl.g_hi + 1e-12).all()
    assert tbl.p_up.between(0, 1).all() and tbl.p_beat.between(0, 1).all() and tbl.score.between(0, 100).all()
    assert tbl.score.is_monotonic_decreasing
    shrunk = E.predict_latest(c, v, LOOSE, models=models, shrink=0.3)[0]
    assert (shrunk.p_up - tbl.p_up.mean()).abs().mean() < (tbl.p_up - tbl.p_up.mean()).abs().mean()      # shrinking pulls toward the base rate

def test_liquidity_filter_and_errors():
    c, v = synth(k=30)
    v = v.copy(); v.iloc[:, 8:] = 10.0                                  # only 8 coins have real volume
    tbl, _, _ = E.predict_latest(c, v, LOOSE)
    assert len(tbl) == 8 and set(tbl.index) == set(c.columns[:8])
    v.iloc[:, 3:] = 10.0                                                # now only 3 are tradeable: too few to rank
    try: E.predict_latest(c, v, LOOSE); assert False
    except ValueError as e: assert "liquidity" in str(e)
    try: E.predict_latest(c.iloc[:200], v.iloc[:200], LOOSE); assert False
    except ValueError: pass

def test_walk_forward_detects_real_signal_and_rejects_noise():
    c, v = synth(signal=True); X, y, yr = E.make_dataset(c, v)
    good = E.walk_forward(X, y, yr, n_folds=9, profile=LOOSE)
    g = good["summary"]
    assert g["n_folds"] == 9 and g["ic"] > 0.05 and g["top_win"] > g["base_up"] and g["verdict"] in ("Strong", "Moderate")
    assert {"cum_top", "cum_market", "cum_naive"} <= set(good["folds"].columns) and len(good["calib"]) >= 3
    assert 0.2 <= g["shrink"] <= 1.0
    c, v = synth(signal=False, seed=2); X, y, yr = E.make_dataset(c, v)
    bad = E.walk_forward(X, y, yr, n_folds=9, profile=LOOSE)["summary"]
    assert abs(bad["ic"]) < 0.08 and bad["verdict"] != "Strong"          # pure noise must not look strong

def test_walk_forward_only_trains_on_known_outcomes():
    c, v = synth(n=900, k=20); X, y, yr = E.make_dataset(c, v)
    dates = X.index.get_level_values(0).unique().sort_values()
    last_known = dates[600]
    m = E.train(X, y, yr, last_known, fast=True, which=("q50",))
    # same model if labels after the cut-off are destroyed (they must never be read)
    y2 = y.copy(); y2[X.index.get_level_values(0) > last_known] = 99.0
    m2 = E.train(X, y2, yr, last_known, fast=True, which=("q50",))
    row = X.xs(dates[700], level=0)
    assert np.allclose(E.predict(m, row)["g_med"], E.predict(m2, row)["g_med"])

def test_reliability_labels():
    assert E.reliability({})[0] == "Unproven" and E.reliability({"n_folds": 2})[0] == "Unproven"
    base = dict(n_folds=9, ic=0.1, ic_pos=0.8, edge=0.02, top_win=0.60, base_up=0.50, top_ret=0.06, market_ret=0.02)
    assert E.reliability(base)[0] == "Strong"
    assert E.reliability({**base, "top_win": 0.52})[0] == "Promising"       # modest win-rate edge is not "Strong"
    assert E.reliability({**base, "top_ret": 0.03})[0] == "Promising"       # modest return edge is not "Strong"
    assert E.reliability({**base, "n_folds": 6})[0] == "Moderate"           # too few months for Strong
    assert E.reliability({**base, "ic": 0.04, "ic_pos": 0.6})[0] == "Moderate"
    assert E.reliability({**base, "ic": 0.0, "ic_pos": 0.4})[0] == "Weak"

def test_full_pipeline_run():
    c, v = synth(k=25)
    res = E.run(c, v, LOOSE, n_folds=4)
    assert {"table", "asof", "val"} <= set(res) and len(res["table"]) == 25 and res["val"]["summary"]["n_folds"] == 4

# ---------- plan, signals, regime ----------
def test_plan_is_consistent():
    c, v = synth(k=20); tbl, _, _ = E.predict_latest(c, v, LOOSE)
    for _, r in tbl.iterrows():
        p = E.plan(r, 10000, 1.0, 20.0)
        assert 0 < p["stop"] < p["entry"] < p["target"] and p["qty"] >= 0
        assert p["invest"] <= 10000 * 0.2 * 1.001 and p["max_loss"] <= 10000 * 0.01 * 1.001 + 1e-9
        assert 0.07 <= 1 - p["stop"] / p["entry"] <= 0.31
    r = tbl.iloc[0].copy(); r["rsi"], r["dma20"] = 82.0, r["price"] * 0.9
    p = E.plan(r, 10000, 1.0); assert p["wait"] and p["entry"] < r["price"] and "Wait" in p["action"]
    tiny = tbl.iloc[0].copy(); tiny["price"], tiny["atr"], tiny["dma20"] = 1.2e-5, 5e-7, 1.2e-5
    assert E.plan(tiny, 10000, 1.0)["qty"] > 1000

def test_qty_helpers():
    assert E.round_qty(0.123456789) == 0.123457 and E.round_qty(1234.7) == 1235 and E.round_qty(-1) == 0
    assert E.qty_for(100, 90, 10000, 1) == 10.0            # risk 100 / 10 per coin
    assert E.qty_for(100, 99.5, 10000, 1) == 20.0          # capped at 20% of capital
    assert E.qty_for(100, None, 10000, 1) == 10.0 and E.qty_for(0, 90, 10000, 1) == 0.0

def test_signals_and_tags():
    r = pd.Series(dict(p_up=0.7, g_med=0.1, rsi=55.0))
    assert E.idea_signal(r)[0] == "BUY"
    assert E.idea_signal(pd.Series(dict(p_up=0.4, g_med=0.1, rsi=55.0)))[0] == "SELL"
    assert E.idea_signal(pd.Series(dict(p_up=0.7, g_med=0.1, rsi=85.0)))[0] == "HOLD"
    assert E.idea_signal(pd.Series(dict(p_up=0.5, g_med=0.1, rsi=50.0)))[0] == "HOLD"
    h = pd.Series(dict(price=100.0, dma20=98.0, dma50=95.0, rsi=60.0, r30=0.12))
    assert E.signal(h)[0] == "BUY" and E.signal(h, stop=101)[0] == "SELL" and E.signal(h, target=99.5)[0] == "SELL"
    assert E.signal(pd.Series(dict(price=80.0, dma50=95.0, r30=-0.2)))[0] == "SELL"
    assert E.signal(pd.Series(dict(price=100.0, p_up=0.3, g_med=-0.1)))[0] == "SELL"
    tags = E.tags_for(pd.Series(dict(vol=1.5, rsi=80.0, r30=0.4, p_up=0.62, price=100.0, hi365=101.0, avg_val=1e6)), scan=True)
    assert all(k in ("buy", "sell", "hold", "info", "risk", "muted") for _, k in tags)
    assert ("Extreme volatility", "sell") in tags and ("Thin volume", "risk") in tags and ("62% chance up", "buy") in tags

def test_regime():
    c, v = synth(k=10)
    assert E.regime(c, v)[0] in ("bull", "bear", "mixed")
    up = pd.DataFrame({"BTC": np.linspace(100, 300, 400), "ETH": np.linspace(50, 100, 400)}, index=pd.date_range(end="2026-11-20", periods=400))
    assert E.regime(up)[0] == "bull"
    assert E.regime(up.iloc[::-1].set_axis(up.index))[0] == "bear"
    assert E.regime(up.iloc[:100])[0] == "mixed"

def test_p_hit_sane():
    assert 0 < E.p_hit(10, 0.8, 1) < 0.5 and E.p_hit(50, 0.8, 1) < E.p_hit(10, 0.8, 1) and E.p_hit(10, 0, 1) == 0

# ---------- paper trading ----------
def _tr(d, s, side, q, p, **k): return dict(ts=f"2026-11-{d:02d}T10:00:00", symbol=s, side=side, qty=q, price=p, **k)

def test_fractional_paper_trading_math():
    tr = [_tr(10, "BTC", "BUY", 0.5, 90000.0, stop=80000, target=120000), _tr(11, "BTC", "BUY", 0.25, 100000.0), _tr(12, "BTC", "SELL", 0.3, 110000.0)]
    pos, realised = E.positions(tr)
    avg = (0.5 * 90000 + 0.25 * 100000) / 0.75
    assert abs(pos["BTC"]["qty"] - 0.45) < 1e-12 and abs(pos["BTC"]["cost"] - avg * 0.45) < 1e-6 and abs(realised - (110000 - avg) * 0.3) < 1e-6
    assert abs(E.cash_balance(100000, tr) - (100000 - 45000 - 25000 + 33000)) < 1e-6
    assert pos["BTC"]["stop"] == 80000

def test_realised_plus_unrealised_equals_networth_change():
    tr = [_tr(2, "A", "BUY", 10.5, 100.0, stop=90, target=130), _tr(3, "A", "SELL", 10.5, 95.0), _tr(4, "A", "BUY", 5.25, 120.0),
          _tr(5, "B", "BUY", 700000.0, 0.0000123), _tr(6, "B", "SELL", 300000.0, 0.00002), _tr(9, "B", "SELL", 400000.0, 0.00001)]
    perf = E.performance(tr, 100_000, {"A": 125.0})
    assert abs(perf["total"] - (perf["net"] - 100_000)) < 1e-6
    assert abs(sum(c["pnl"] for c in E.ledger(tr)) - E.positions(tr)[1]) < 1e-9
    assert perf["closed"] == 3 and perf["wins"] == 1 and perf["losses"] == 2

def test_old_stop_does_not_leak_and_dust_is_closed():
    tr = [_tr(2, "A", "BUY", 0.1 + 0.2, 100.0, stop=90, target=130), _tr(3, "A", "SELL", 0.3, 95.0), _tr(4, "A", "BUY", 5, 120.0)]
    p = E.positions(tr)[0]["A"]
    assert p["stop"] is None and p["target"] is None and abs(p["cost"] - 600.0) < 1e-9 and p["qty"] == 5
    assert "A" not in E.positions(tr[:2])[0]                           # 0.1+0.2 sold as 0.3 leaves no float dust

def test_history_starts_at_cash_and_follows_prices():
    ix = pd.date_range("2026-11-02", "2026-11-13")
    close = pd.DataFrame({"A": np.linspace(100, 130, len(ix))}, index=ix)
    h = E.history([_tr(2, "A", "BUY", 10, 100.0)], close, 1_000_000)
    assert h.iloc[0] == 1_000_000 and abs(h.iloc[-1] - (1_000_000 + 10 * 30)) < 1e-6 and len(E.history([], close, 1)) == 0

# ---------- Gemini ----------
def test_gemini_client_mocked():
    class R:
        status_code = 200
        def json(self): return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
    with mock.patch("requests.post", return_value=R()):
        assert G.ask("key", "gemini-flash-latest", [{"role": "user", "text": "hi"}]) == "ok"

def test_prompts_have_no_gain_threshold_and_cover_crypto():
    p = E.build_prompt(E.PROMPT_KINDS[0], "", "CTX-DATA", dt.date(2026, 11, 21))
    assert "CTX-DATA" in p and "JSON array" in p and "21 Nov 2026" in p and "NO minimum gain" in p
    for k in ("ANY cryptocurrency", "DERIVATIVES", "ON-CHAIN", "regulat", "ETF", "unlock", "onchain_fundamentals", "30 days"):
        assert k in p, k
    assert "10%" not in p and "₹" not in p and "NSE" not in p
    assert "my question abc" in E.build_prompt(E.PROMPT_KINDS[2], "my question abc", "", dt.date(2026, 11, 21))

def test_parse_recs_and_symbols():
    reply = """Sure:
```json
[{"symbol":"BINANCE:BTCUSDT","name":"Bitcoin","action":"Buy now","entry":"$97,000.5","stop_loss":90000,"target":110000,"horizon":"1 month",
  "confidence":"medium","reason":"ETF inflows.","risk":"Macro.","market_derivs":"Funding neutral"},
 {"symbol":"eth-usd","action":"Wait for dip","entry":3000,"stop_loss":2800,"target":3600},
 {"symbol":"SOL/USD"},{"symbol":"USDT"}]
```
Not advice."""
    items = E.parse_recs(reply)
    assert [i["symbol"] for i in items] == ["BTC", "ETH", "SOL", "USDT"]      # Buy now first, then wait / unclear
    assert items[0]["entry"] == 97000.5 and items[0]["stop"] == 90000 and items[0]["market_derivs"] == "Funding neutral" and items[1]["risk"] == ""
    assert E.parse_recs("no json here") is None and E.parse_recs("[not json]") is None and E.parse_recs('[{"foo":1}]') == []
    assert [E.norm_symbol(x) for x in ("Coinbase:ETH-USD", "SOLUSDT", "doge", "ADA/USDC")] == ["ETH", "SOL", "DOGE", "ADA"]

def test_parse_symbol_list_formats():
    assert E.parse_symbol_list('[{"symbol":"btc"},{"symbol":"usdt"},{"symbol":"eth"}]') == ["BTC", "ETH"]
    assert E.parse_symbol_list("symbol,name\nbtc,Bitcoin\nsol,Solana\n") == ["BTC", "SOL"]
    assert E.parse_symbol_list("btc, eth  sol;USDC") == ["BTC", "ETH", "SOL"]

def test_review_prompt_and_parse():
    rows = [dict(symbol="BTC", qty=0.0123, avg=90000, ltp=97000, pnl=86.1, pnl_pct=7.8, weight=60, stop=85000, target=120000)]
    perf = dict(current=1e4, invested=9e3, cash=9e3, unrealised=1e3, unrealised_pct=11.1, realised=500, closed=2, win_rate=50)
    p = E.build_review_prompt(rows, perf, dt.date(2026, 11, 21))
    assert "BTC: 0.0123 coins" in p and "JSON array" in p and "Sell part" in p and "ONLY the holdings" in p and "ANY cryptocurrency" not in p
    out = E.parse_review('ok [{"symbol":"BINANCE:BTCUSDT","action":"Sell part","qty":"0.005","new_stop_loss":88000},{"symbol":"ETH","action":"Buy new","qty":1.5},{"symbol":"A","action":"Hold"}]')
    assert [(o["symbol"], o["action"], o["qty"]) for o in out] == [("BTC", "SELL PART", 0.005), ("ETH", "BUY NEW", 1.5), ("A", "HOLD", 0.0)]
    assert out[0]["stop"] == 88000 and E.parse_review("nothing") is None

# ---------- themes and UI helpers ----------
def test_themes_generate_valid_css():
    assert E.theme_css("Classic") == "" and E.DEFAULT_THEME in E.THEMES
    for name in E.THEMES:
        c = E.theme_css(name, blur=18, animate=True, font_px=18)
        assert c.count("{") == c.count("}")
    assert "backdrop-filter" not in E.theme_css("Aurora", contrast=True) and "#ffffff" in E.theme_css("Aurora", contrast=True)

def test_ui_helpers():
    assert U.usd(97123.456) == "$97,123.46" and U.usd(2.3141) == "$2.314" and U.usd(0.00001234) == "$0.0000123"
    assert U.usd(-1234.5, 2, True) == "-$1,234.50" and U.usd(0.004, 2, True) == "$0.00" and U.usd(float("nan")) == "–"
    assert U.qty_txt(0.00123) == "0.00123" and U.qty_txt(1.5) == "1.5" and U.qty_txt(12500.7) == "12,501" and U.qty_txt(10) == "10"
    assert U.pct(2.3456) == "+2.35%" and U.pct(float("nan")) == "–"
    assert "gw-up" in U.pnl_html(10, 1) and "gw-down" in U.pnl_html(-10, -1) and "gw-flat" in U.pnl_html(None)
    assert "<script>" not in U.chip("<script>", "buy")
    assert "gw-stamp-mk" in U.countdown_html(30, "gw-stamp-mk") and "Auto-refresh is off" in U.countdown_html(0, "x")
    d = U.dial_html(60, "gw-stamp-pf", "pf_refresh"); assert "st-key-pf_refresh" in d and "stroke-dashoffset" in d
    perf = dict(current=1, invested=1, unrealised=0, unrealised_pct=0, realised=0, realised_pct=0, closed=0, cash=1, net=1, total_pct=0)
    assert "gw-stamp-pf" in U.summary_html(perf, 0, 0, 1, "10:00:00", plain=True)
    assert U.GROWW_CSS.count("{") == U.GROWW_CSS.count("}")
    bar = U.live_bar_html({"BTC": (97000.0, 1.2)}, "10:00:00")
    assert "24/7" in bar and "BTC" in bar and "gw-stamp-mk" in bar
    h = U.holding_html(dict(symbol="BTC", name="Bitcoin", qty=0.0123, avg=90000, ltp=97000, invested=1107, current=1193, pnl=86, pnl_pct=7.7, day_pnl=5,
                            day_pct=0.4, stop=85000, target=110000, horizon="30D", signal="HOLD", reason="x", tags=[]))
    assert "0.0123 BTC" in h and "30D" in h and "share" not in h


# ---------- global policy backdrop and event overlay ----------
def _macro(index, seed=3):
    rng = np.random.default_rng(seed); bd = index[index.dayofweek < 5]
    return pd.DataFrame({"DXY": 100 * np.exp(np.cumsum(rng.normal(0, .004, len(bd)))), "US10Y": 4 + np.cumsum(rng.normal(0, .03, len(bd))),
                         "VIX": 18 + np.cumsum(rng.normal(0, .5, len(bd))), "NASDAQ": 15000 * np.exp(np.cumsum(rng.normal(0, .01, len(bd)))),
                         "GOLD": 2000 * np.exp(np.cumsum(rng.normal(0, .007, len(bd))))}, index=bd)

def test_macro_features_aligned_and_leak_free():
    c, v = synth(n=700, k=10); mac = _macro(c.index)
    X, _, _ = E.make_dataset(c, v, mac)
    assert {"mac_dxy30", "mac_us10y30", "mac_vix", "mac_vix30", "mac_nasdaq30", "mac_gold30"} <= set(X.columns)
    mac2 = mac.copy(); mac2.loc[mac2.index > c.index[500]] *= 5                      # change the future of the macro series
    X2, _, _ = E.make_dataset(c, v, mac2)
    d = c.index[499]
    pd.testing.assert_frame_equal(X.xs(d, level=0), X2.xs(d, level=0))
    assert "mac_vix" not in E.make_dataset(c, v)[0].columns

def test_run_with_macro_reports_both_and_picks_honestly():
    c, v = synth(k=25); res = E.run(c, v, LOOSE, n_folds=4, macro=_macro(c.index))
    assert res["macro_ic"] is not None and res["base_ic"] is not None and len(res["table"]) == 25
    assert res["macro_used"] == (res["macro_ic"] >= res["base_ic"])
    assert E.run(c, v, LOOSE, n_folds=4)["macro_used"] is False

def _tbl():
    return pd.DataFrame({"g_lo": [-.2, -.2], "g_med": [.10, .12], "g_hi": [.4, .5], "p_up": [.7, .65], "p_beat": [.6, .7]}, index=["AAA", "BBB"])

def test_events_lower_confidence_only_for_affected_coins_in_window():
    asof = pd.Timestamp("2026-11-20")
    ev = [dict(date="2026-12-01", title="ETF ruling", coins=["AAA"], severity="High", direction="Uncertain"),
          dict(date="2027-03-01", title="Far away", coins=["ALL"], severity="High"), dict(date="2026-11-01", title="Past", coins=["ALL"], severity="High")]
    out = E.apply_events(_tbl(), ev, asof)
    a, b = out.loc["AAA"], out.loc["BBB"]
    assert abs(a.p_up - 0.5) < abs(0.7 - 0.5) and a.g_med < 0.10 and (a.g_hi - a.g_lo) > (0.4 + 0.2) and "ETF ruling" in a["event_flags"]
    assert b.p_up == 0.65 and b.g_med == 0.12 and b["event_flags"] == ""                      # untouched
    neg = E.apply_events(_tbl(), [dict(date="2026-12-01", title="Ban", coins=["ALL"], severity="Medium", direction="Negative")], asof)
    unc = E.apply_events(_tbl(), [dict(date="2026-12-01", title="Ban", coins=["ALL"], severity="Medium", direction="Uncertain")], asof)
    assert (neg.g_med < unc.g_med).all() and list(out["rank"]) == [1, 2] and E.apply_events(_tbl(), [], asof)["event_flags"].eq("").all()

def test_events_prompt_and_parse():
    p = E.build_events_prompt("India", dt.date(2026, 11, 21), ["BTC", "SOL"])
    assert "India" in p and "NEXT 30 DAYS" in p and "RBI" in p and "SEC/CFTC" in p and "JSON array" in p and "BTC, SOL" in p
    ev = E.parse_events('x [{"date":"2026-12-10","title":"Fed decision","kind":"Central bank / macro","coins":"ALL","severity":"high","direction":"negative"},'
                        '{"date":"bad","title":"skip"},{"date":"2026-12-12","title":"SOL unlock","coins":["SOLUSDT"],"severity":"weird"}]')
    assert [e["title"] for e in ev] == ["Fed decision", "SOL unlock"] and ev[0]["severity"] == "High" and ev[0]["direction"] == "Negative"
    assert ev[0]["coins"] == ["ALL"] and ev[1]["coins"] == ["SOL"] and ev[1]["severity"] == "Medium" and E.parse_events("none") is None
    assert "India" in E.COUNTRIES and all(isinstance(v, str) for v in E.COUNTRIES.values())


# ---------- news / policy monitor, persistence, cloud backup ----------
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>SEC approves new ether ETF as inflows hit record</title><source>Wire</source><pubDate>Fri, 20 Nov 2026</pubDate><link>http://x/1</link></item>
<item><title>Exchange hacked, regulators open probe</title><source>News</source><link>http://x/2</link></item>
<item><title>Bitcoin trades sideways</title></item></channel></rss>"""

def test_headline_scoring_rss_and_policy_risk():
    items = E.parse_rss(RSS)
    assert [i["title"][:3] for i in items] == ["SEC", "Exc", "Bit"] and items[0]["source"] == "Wire" and E.parse_rss("<bad") == []
    assert E.score_headline(items[0]["title"]) > 0 > E.score_headline(items[1]["title"]) and E.score_headline(items[2]["title"]) == 0
    bad = {"Global regulation": [dict(title="Country bans crypto exchanges in crackdown")] * 8}
    good = {"ETFs": [dict(title="Regulator approves ETF, record inflows")] * 8}
    assert E.policy_risk(bad)["label"] == "Risk-off" and E.policy_risk(bad)["worst"] and E.policy_risk(good)["label"] == "Supportive"
    assert E.policy_risk({"x": items})["label"] == "No data" and set(E.NEWS_QUERIES) >= {"Global regulation", "India policy", "Central banks & macro"}

def test_newest_copy_wins_and_6pm_rule():
    a, b = {"saved_at": "2026-11-21T10:00:00"}, {"saved_at": "2026-11-20T10:00:00"}
    assert E.newer(a, b) == "a" and E.newer(b, a) == "b" and E.newer(a, dict(a)) == "same" and E.newer({}, b) == "b" and E.newer(a, None) == "a"
    t = lambda h, d=21: dt.datetime(2026, 11, d, h, 0)
    assert not E.due_daily_backup(t(17), "2026-11-20") and E.due_daily_backup(t(18), "2026-11-20") and E.due_daily_backup(t(23), None)
    assert not E.due_daily_backup(t(19), "2026-11-21") and len(E.stamp()) >= 19

def test_cloud_store_roundtrip_with_fake_github():
    import cloud_store as C, json
    class Resp:
        def __init__(s, code, js): s.status_code, s._js, s.text = code, js, json.dumps(js)
        def json(s): return s._js
    class Fake:
        def __init__(s): s.gists = {}
        def get(s, url, **k):
            if url.endswith("/gists"): return Resp(200, [{"id": i, "description": g["description"]} for i, g in s.gists.items()])
            return Resp(200, {"files": {n: {"content": f["content"]} for n, f in s.gists[url.rsplit("/", 1)[1]]["files"].items()}})
        def post(s, url, json=None, **k): s.gists["g1"] = dict(json); return Resp(201, {"id": "g1"})
        def patch(s, url, json=None, **k): s.gists[url.rsplit("/", 1)[1]]["files"].update(json["files"]); return Resp(200, {"id": "g1"})
    h = Fake()
    assert C.pull("t", "me", http=h) is None
    gid = C.push("t", "me", {"start_cash": 5, "trades": [], "saved_at": "x"}, http=h)
    assert gid == "g1" and h.gists["g1"]["public"] is False and h.gists["g1"]["description"] == C.DESC
    C.push("t", "me", {"start_cash": 7, "trades": []}, http=h); C.push("t", "other", {"start_cash": 1, "trades": []}, http=h)
    assert C.pull("t", "me", http=h)["start_cash"] == 7 and C.pull("t", "other", http=h)["start_cash"] == 1 and C.pull("t", "nobody", http=h) is None
    class Bad(Fake):
        def get(s, url, **k): return Resp(401, {"message": "Bad credentials"})
    try: C.pull("t", "me", http=Bad()); assert False
    except RuntimeError as e: assert "401" in str(e)

def test_gemini_prompt_has_required_actions_and_policy_fields():
    p = E.build_prompt(E.PROMPT_KINDS[0], "", "CTX", dt.date(2026, 11, 21))
    for k in ("REQUIRED ACTIONS", "last 7 days", "event calendar", "country", "agree", "policy_view", "agrees_with_app_model", "downgrade"):
        assert k in p, k
    it = E.parse_recs('[{"symbol":"BTC","policy_view":"ETF flows support","agrees_with_app_model":"partly"}]')[0]
    assert it["policy_view"] == "ETF flows support" and it["agrees"] == "partly"


def test_buy_first_order_tags_and_scope():
    reply = ('[{"symbol":"AAA","action":"Avoid"},{"symbol":"BBB","action":"Wait for dip","entry":1,"stop_loss":0.9,"target":1.2},'
             '{"symbol":"CCC","action":"Buy now","entry":2,"stop_loss":1.8,"target":2.5},{"symbol":"DDD","action":"Buy now","entry":3,"stop_loss":2,"target":4}]')
    assert [i["symbol"] for i in E.parse_recs(reply)] == ["CCC", "DDD", "BBB", "AAA"]   # buys first, stable inside a group
    assert E.rec_signal("Buy now") == "BUY" and E.rec_signal("Wait for dip") == "HOLD" and E.rec_signal("") == "HOLD"
    assert E.rec_signal("Avoid") == "AVOID" and E.rec_signal("Avoid", held=True) == "SELL"
    import datetime as dt
    p = E.build_prompt(E.PROMPT_KINDS[0], "", "ctx", dt.date(2026, 11, 22), "Major coins", ("BTC", "ETH"))
    assert "BUY IDEAS FIRST" in p and "choose ONLY from these 2 coins" in p and "BTC, ETH" in p and p.index("Buy now") < p.index("Avoid")
    free = E.build_prompt(E.PROMPT_KINDS[0], "", "ctx", dt.date(2026, 11, 22), E.ANY_COIN, None)
    assert "any liquid coin" in free and "choose ONLY" not in free
