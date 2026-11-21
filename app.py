import datetime as dt, json, os, re, sys, threading, time, zipfile
from contextlib import contextmanager
from zoneinfo import ZoneInfo
import pandas as pd, streamlit as st, yfinance as yf
import streamlit.components.v1 as components

FILES_VERSION = 1     # engine.py and ui.py must carry the same number

def _files_ok():
    try:
        import engine as _e, ui as _u
        return getattr(_e, "FILES_VERSION", 0) >= FILES_VERSION and getattr(_u, "FILES_VERSION", 0) >= FILES_VERSION
    except Exception:
        return False

if not _files_ok():
    # app.py is newer than engine.py / ui.py (a partly updated repo). Repair them from the zip kept in the repo, if it is there.
    _here = os.path.dirname(os.path.abspath(globals().get("__file__", "app.py")))
    for _zp in (os.path.join(_here, "crypto-advisor.zip"), "crypto-advisor.zip"):
        if os.path.exists(_zp):
            try:
                with zipfile.ZipFile(_zp) as _z:
                    for _n in _z.namelist():
                        if os.path.basename(_n) in ("engine.py", "ui.py", "gemini_client.py", "cloud_store.py") and "/android/" not in _n and "/tests/" not in _n:
                            with open(os.path.join(_here, os.path.basename(_n)), "wb") as _f: _f.write(_z.read(_n))
            except Exception:
                pass
            break
    for _m in ("engine", "ui", "gemini_client", "cloud_store"): sys.modules.pop(_m, None)
    if not _files_ok():
        st.error("Some app files are older than app.py (engine.py or ui.py). Upload the newest crypto-advisor.zip to the repo root "
                 "(replace the old one), wait for the GitHub Action to finish, then reboot the app.")
        st.stop()
import engine as E
import gemini_client as G
import cloud_store as CS
import ui as U

st.set_page_config(page_title="Crypto Advisor", page_icon="🪙", layout="wide", initial_sidebar_state="collapsed")
IST = ZoneInfo("Asia/Kolkata")
now_ist = lambda: dt.datetime.now(IST)
TITLE = '<div class="gw-title">🪙 Crypto Advisor<span>Predicts each coin\'s next 30 days. Practice account with virtual money. Research aid, not a guarantee and not financial advice.</span></div>'
_head = st.container(key="headbar")      # filled in below: title on the left, ⚙️ on the right

user = re.sub(r"[^a-z0-9_-]", "", str(st.session_state.get("user_name", "me")).lower()) or "me"

# ---------- demo account storage (one JSON file per account name, mirrored to your phone and, optionally, a private GitHub Gist) ----------
def _secret(name):
    try: v = st.secrets.get(name, "")
    except Exception: v = ""
    return v or os.environ.get(name, "")
GH_TOKEN = _secret("GITHUB_TOKEN")
os.makedirs("portfolios", exist_ok=True)
path = f"portfolios/{user}.json"

@st.cache_resource
def _cloud_state(): return dict(ok=None, msg="Not used yet", at="")

def cloud_push(force=False):
    """Upload the account to the private Gist in the background (at most once every 30 s unless forced). No token = no-op."""
    if not GH_TOKEN: return
    if not force and time.time() - st.session_state.get("cloud_t", 0) < 30: st.session_state["cloud_dirty"] = True; return
    st.session_state["cloud_t"] = time.time(); st.session_state["cloud_dirty"] = False
    data, u, state = json.loads(json.dumps(st.session_state.acct)), user, _cloud_state()
    def work():
        try: CS.push(GH_TOKEN, u, data); state.update(ok=True, msg="Uploaded", at=dt.datetime.now(IST).strftime("%d %b %H:%M"))
        except Exception as e: state.update(ok=False, msg=str(e)[:160], at=dt.datetime.now(IST).strftime("%d %b %H:%M"))
    threading.Thread(target=work, daemon=True).start()

def load_acct():
    try:
        with open(path) as f: return json.load(f)
    except Exception:
        if GH_TOKEN:       # the server disk was wiped (restart): bring the account back from the Gist
            try:
                d = CS.pull(GH_TOKEN, user)
                if d and isinstance(d.get("trades"), list): return d
            except Exception: pass
        return dict(start_cash=10000.0, trades=[])
if st.session_state.get("acct_user") != user:
    st.session_state.acct, st.session_state.acct_user = load_acct(), user
acct = st.session_state.acct
def save():
    acct["saved_at"] = E.stamp()
    with open(path, "w") as f: json.dump(acct, f)
    cloud_push()
def flash(msg): st.session_state.setdefault("flash", []).append(msg)

def daily_backup():
    """Once a day, from 6 pm India time: a dated snapshot on the server (last 14 kept) and an upload to the Gist."""
    if not (acct.get("trades") or acct.get("orders")) or not E.due_daily_backup(dt.datetime.now(IST), acct.get("last_6pm")): return
    day = f"{dt.datetime.now(IST):%Y-%m-%d}"; os.makedirs("backups", exist_ok=True)
    try:
        with open(f"backups/{user}_{day}.json", "w") as f: json.dump(acct, f)
        for old in sorted(x for x in os.listdir("backups") if x.startswith(user + "_"))[:-14]: os.remove(os.path.join("backups", old))
    except Exception: pass
    acct["last_6pm"] = day; save(); cloud_push(force=True); flash(f"☁️ 6 pm backup for {day} saved on the server")

def show_dialog(name, *args):
    """Open a buy / sell / settings window. Queued, then opened on a full rerun, so it also works from inside a live fragment."""
    st.session_state["pending_dlg"] = (name, args); st.rerun(scope="app")

# ---------- a copy of the account is also kept in this device's browser, so an app update or reboot never wipes it ----------
_BK_KEYS = ("start_cash", "trades", "orders", "levels", "watch", "alias", "recs", "review", "notes", "events", "settings", "theme", "blur", "fs", "hc", "anim", "saved_at", "last_6pm")
_bk_key = f"cta_acct_{user}"
def _valid_acct(d):
    return (isinstance(d, dict) and isinstance(d.get("trades"), list) and isinstance(d.get("start_cash"), (int, float))
            and all(isinstance(t, dict) and {"symbol", "side", "qty", "price", "ts"} <= set(t) for t in d["trades"]))
if not st.session_state.get("cta_restore_done"):
    with st.container(key="ca_restore"):
        _rv = st.text_area("restore", key="cta_restore_box", label_visibility="collapsed")
    components.html("<script>(function(){const KEY=" + json.dumps(_bk_key) + ";let n=0;const t=setInterval(()=>{try{const p=window.parent,d=p.document,"
                    "ta=d.querySelector('.st-key-ca_restore textarea');if(ta){clearInterval(t);const v=p.localStorage.getItem(KEY)||'NONE';"
                    "Object.getOwnPropertyDescriptor(p.HTMLTextAreaElement.prototype,'value').set.call(ta,v);"
                    "ta.dispatchEvent(new p.Event('input',{bubbles:true}));ta.dispatchEvent(new p.FocusEvent('focusout',{bubbles:true}));}"
                    "else if(++n>40)clearInterval(t);}catch(e){clearInterval(t)}},250)})();</script>", height=0)
    if _rv:
        st.session_state["cta_restore_done"] = True
        if _rv != "NONE":
            try:
                _d = json.loads(_rv)
                if _valid_acct(_d) and _d.get("saved_at") and E.newer(_d, acct) == "a":     # the phone copy is newer: upload it to the server
                    acct.clear(); acct.update({k: _d[k] for k in _BK_KEYS if k in _d}); save(); cloud_push(force=True)
                    flash("📱➡️☁️ Synced your newer portfolio from this phone to the server"); st.rerun()
            except Exception as _e:
                if type(_e).__name__ in ("RerunException", "StopException", "Rerun", "Stop"): raise
        elif acct.get("trades"): cloud_push(force=True)

def browser_backup():
    """Write the account into this browser's storage (skipped while the account is empty, so a fresh start can't overwrite a good copy)."""
    if st.session_state.pop("clear_backup", False): act = "L.removeItem(" + json.dumps(_bk_key) + ")"
    elif acct.get("trades") or acct.get("orders"):
        blob = json.dumps(json.dumps({k: acct[k] for k in _BK_KEYS if k in acct}))
        act = ("const K=" + json.dumps(_bk_key) + ",V=" + blob + ";L.setItem(K,V);const d=new Date(),day=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');"
               "L.setItem(K+'_snap_'+day,V);Object.keys(L).filter(k=>k.startsWith(K+'_snap_')).sort().slice(0,-7).forEach(k=>L.removeItem(k))")
    else: return
    components.html("<script>try{const L=window.parent.localStorage;" + act.replace("</", "<\\/") + "}catch(e){}</script>", height=0)

@contextmanager
def box(title, key, sub=""):
    """A titled box around one tool (styled by ui.GROWW_CSS: class card_box_*)."""
    with st.container(border=True, key=f"card_box_{key}"):
        st.markdown(f'<div class="gw-boxh">{U.esc(title)}' + (f'<span>{U.esc(sub)}</span>' if sub else "") + "</div>", unsafe_allow_html=True)
        yield

# ---------- settings: the ⚙️ window in the top bar. Kept for the session and saved with the demo account ----------
APPEARANCE = ("theme", "blur", "fs", "hc", "anim")
DEFAULTS = dict(capital=10000, risk=1.0, maxpos=20.0, fee=0.2, auto_on=True, every=60, pf_auto=True, pf_every=60, topn=8,
                uni=list(E.UNIVERSES)[0], profile=E.DEFAULT_PROFILE, country="India", extra="", auto_exec=True, gmodel="gemini-flash-latest", gsearch=True,
                theme=E.DEFAULT_THEME, blur=16, fs=16, hc=False, anim=True)
S = {**DEFAULTS, **{k: acct[k] for k in APPEARANCE if k in acct}, **acct.get("settings", {}), **st.session_state.get("cfg", {})}
if S["uni"] not in E.UNIVERSES: S["uni"] = DEFAULTS["uni"]
if S["profile"] not in E.PROFILES: S["profile"] = DEFAULTS["profile"]
if S["theme"] not in E.THEMES: S["theme"] = E.DEFAULT_THEME
_persist = {k: S[k] for k in DEFAULTS}
if acct.get("settings") != _persist: acct["settings"] = _persist; save()
capital, risk, maxpos, fee, auto_on, every, pf_auto, pf_every = S["capital"], S["risk"], S["maxpos"], S["fee"] / 100, S["auto_on"], S["every"], S["pf_auto"], S["pf_every"]
topn, uni, profile, extra, auto_exec, gmodel, gsearch = S["topn"], S["uni"], S["profile"], S["extra"], S["auto_exec"], S["gmodel"], S["gsearch"]
country = S["country"] if S["country"] in E.COUNTRIES else "Other"
th, blur, fs, hc, anim = S["theme"], S["blur"], S["fs"], S["hc"], S["anim"]
try: _k = st.secrets.get("GEMINI_API_KEY", "")
except Exception: _k = ""
gkey = st.session_state.get("gkey") or _k or os.environ.get("GEMINI_API_KEY", "")
def inject_css(css_html):
    """Put the CSS straight into the page <head> (a markdown block gets cut off partway and prints as text)."""
    css = re.sub(r"</?style[^>]*>", "", css_html)
    components.html("<script>try{const d=window.parent.document;let s=d.getElementById('gw-css');if(!s){s=d.createElement('style');s.id='gw-css';d.head.appendChild(s)}"
                    "s.textContent=" + json.dumps(css).replace("</", "<\\/") + ";}catch(e){}</script>", height=0)
inject_css(E.theme_css(th, blur, 22, anim, fs, hc) + U.GROWW_CSS)

def _pick(label, options, current, key, fmt=str):
    """Capsule choice: one option highlighted. Never returns None (tapping the chosen one again keeps it)."""
    v = st.pills(label, options, selection_mode="single", default=current if current in options else options[0], format_func=fmt, key=key)
    return current if v is None else v
def _onoff(label, current, key, help=None):
    v = st.pills(label, ["On", "Off"], selection_mode="single", default="On" if current else "Off", key=key, help=help)
    return bool(current) if v is None else v == "On"
_BLUR = {"Off": 0, "Low": 8, "Medium": 16, "High": 24}; _FS = {"Small": 15, "Normal": 16, "Large": 18, "Extra large": 20}
_near = lambda d, v: min(d, key=lambda k: abs(d[k] - v))

@st.dialog("⚙️ Settings", width="large")
def settings_dialog():
    st.caption("Pick an option in each block. Nothing changes until you tap **Save & apply**.")
    with box("👤 Account", "st_acc", "Each name keeps its own virtual portfolio"):
        n_user = st.text_input("Demo account name", user, key="st_user")
        n_cap = st.number_input("Capital for sizing ideas ($)", 100, 100000000, int(S["capital"]), step=1000, key="st_cap")
        n_risk = _pick("Max loss per trade (% of capital)", [0.5, 1.0, 1.5, 2.0, 3.0], float(S["risk"]), "st_risk", lambda x: f"{x:g}%")
        n_maxpos = _pick("Max size of one coin (% of capital)", [10.0, 15.0, 20.0, 30.0], float(S["maxpos"]), "st_maxpos", lambda x: f"{x:g}%")
        n_fee = _pick("Trading cost per order (fee + slippage)", [0.0, 0.1, 0.2, 0.5], float(S["fee"]), "st_fee", lambda x: f"{x:g}%")
    with box("⏱ Price refresh", "st_mk", "Prediction lists and the top bar"):
        n_auto = _onoff("Auto-refresh", bool(S["auto_on"]), "st_auto", "Updates by itself without reloading the page.")
        n_every = st.slider("Refresh every (seconds)", 10, 300, min(max(int(S["every"]), 10), 300), step=5, key="st_every", format="%d sec")
    with box("💼 Portfolio refresh", "st_pf", "Only your holdings"):
        n_pauto = _onoff("Auto-refresh", bool(S["pf_auto"]), "st_pauto", "The dial in the Current value card shows the time left.")
        n_pevery = st.slider("Refresh every (seconds)", 10, 300, min(max(int(S["pf_every"]), 10), 300), step=5, key="st_pevery", format="%d sec")
    with box("🔮 Predictions", "st_id", "Which coins, and how many to show"):
        n_uni = _pick("Coin list", list(E.UNIVERSES), S["uni"], "st_uni")
        n_prof = _pick("Risk profile (which coins are feasible)", list(E.PROFILES), S["profile"], "st_prof")
        p_ = E.PROFILES[n_prof]
        st.caption(f"Keeps coins with at least ${p_['min_val'] / 1e6:g}M traded per day and {p_['min_days']} days of history. Smaller coins can move more, in both directions, and are harder to trade.")
        n_country = _pick("My country (local rules go into the Gemini prompts)", list(E.COUNTRIES), country, "st_country")
        st.caption(E.COUNTRIES[n_country])
        n_topn = _pick("Ideas to show", [5, 8, 10, 15], int(S["topn"]), "st_topn")
        n_extra = st.text_input("Add coins to the list (symbols, comma-separated)", S["extra"], key="st_extra", help="e.g. HYPE, PENGU. Yahoo Finance must have the coin as SYMBOL-USD.")
    with box("🛡 Order rules", "st_or"):
        n_aex = _onoff("Auto-sell at stop-loss / target", bool(S["auto_exec"]), "st_aex", "Checked whenever the app is open (crypto never closes, so a fast move overnight is only caught when you open the app).")
    with box("🤖 Gemini", "st_gm", "Optional: for the in-app chat and one-tap reviews"):
        n_gkey = st.text_input("Gemini API key", gkey, type="password", key="st_gkey", help="Free key from aistudio.google.com. Kept only for this session, never saved to disk.")
        n_gmodel = st.text_input("Gemini model", S["gmodel"], key="st_gmodel")
        n_gsearch = _onoff("Let Gemini search the web (Google Search)", bool(S["gsearch"]), "st_gsearch")
    with box("🎨 Appearance", "st_ap"):
        n_theme = _pick("Theme", list(E.THEMES), S["theme"], "st_theme")
        n_blur = _BLUR[_pick("Glass blur", list(_BLUR), _near(_BLUR, int(S["blur"])), "st_blur")]
        n_fs = _FS[_pick("Text size", list(_FS), _near(_FS, int(S["fs"])), "st_fs")]
        n_hc = _onoff("High contrast (clearest text)", bool(S["hc"]), "st_hc")
        n_anim = _onoff("Animated background", bool(S["anim"]), "st_anim")
    c1, c2 = st.columns(2)
    if c1.button("💾 Save & apply", type="primary", key="st_save", use_container_width=True):
        st.session_state["cfg"] = dict(capital=int(n_cap), risk=float(n_risk), maxpos=float(n_maxpos), fee=float(n_fee), auto_on=bool(n_auto), every=int(n_every),
                                       pf_auto=bool(n_pauto), pf_every=int(n_pevery), topn=int(n_topn), uni=n_uni, profile=n_prof, country=n_country, extra=n_extra.strip(),
                                       auto_exec=bool(n_aex), gmodel=n_gmodel.strip() or DEFAULTS["gmodel"], gsearch=bool(n_gsearch),
                                       theme=n_theme, blur=int(n_blur), fs=int(n_fs), hc=bool(n_hc), anim=bool(n_anim))
        st.session_state["user_name"] = re.sub(r"[^a-z0-9_-]", "", n_user.lower()) or "me"; st.session_state["gkey"] = n_gkey.strip()
        flash("⚙️ Settings saved"); st.rerun()
    if c2.button("↩️  Restore defaults", key="st_reset", use_container_width=True):
        st.session_state["cfg"] = dict(DEFAULTS); flash("Settings restored to defaults"); st.rerun()

daily_backup()
if st.session_state.get("cloud_dirty") and time.time() - st.session_state.get("cloud_t", 0) >= 30: cloud_push(force=True)
for _m in st.session_state.pop("flash", []): st.toast(_m)

# ======================================================================
# Data (Yahoo Finance: SYMBOL-USD, daily, 24/7)
# ======================================================================
def _field(df, name, tickers):
    if isinstance(df.columns, pd.MultiIndex): c = df[name] if name in df.columns.get_level_values(0) else pd.DataFrame(index=df.index)
    elif name in df.columns: c = df[[name]].copy(); c.columns = [tickers[0]]
    else: c = pd.DataFrame(index=df.index)
    if isinstance(c, pd.Series): c = c.to_frame(tickers[0])
    c.index = pd.to_datetime(c.index)
    if getattr(c.index, "tz", None) is not None: c.index = c.index.tz_localize(None)
    c.index = c.index.normalize()
    return c

def _dl(symbols, period="5y"):
    tickers = [E.yf_ticker(s) for s in symbols]
    df = yf.download(tickers, period=period, auto_adjust=True, progress=False, threads=True)
    if df is None or df.empty: return pd.DataFrame(), pd.DataFrame()
    c, v = _field(df, "Close", tickers), _field(df, "Volume", tickers)
    c.columns = [E.sym_of(x) for x in c.columns]; v.columns = [E.sym_of(x) for x in v.columns]
    return c, v

@st.cache_data(ttl=3600, show_spinner="Fetching crypto price history…")
def load(symbols):
    """Daily closes and USD volume for each symbol (5 years where available)."""
    cs, vs = [], []
    for i in range(0, len(symbols), 40):
        for attempt in (1, 2):
            try:
                c, v = _dl(symbols[i:i + 40])
                if len(c): cs.append(c); vs.append(v)
                break
            except Exception: time.sleep(1.5)
    if not cs: raise RuntimeError("No price data returned (Yahoo may be busy). Try again in a minute.")
    c = pd.concat(cs, axis=1); v = pd.concat(vs, axis=1)
    c = c.loc[:, ~c.columns.duplicated()]; v = v.loc[:, ~v.columns.duplicated()]
    return c.dropna(how="all").dropna(axis=1, how="all"), v.reindex(c.index).reindex(columns=c.columns)

@st.cache_data(ttl=86400, show_spinner=False)
def coingecko_top(n=300):
    """Top coins by market cap from CoinGecko's free API (None if it can't be reached)."""
    import requests
    syms = []
    for page in range(1, n // 150 + 1):
        try:
            r = requests.get("https://api.coingecko.com/api/v3/coins/markets", timeout=20, headers={"User-Agent": "Mozilla/5.0"},
                             params=dict(vs_currency="usd", order="market_cap_desc", per_page=150, page=page))
            if r.status_code != 200: break
            syms += E.parse_symbol_list(r.text)
        except Exception: break
    return list(dict.fromkeys(syms)) or None

def universe_symbols(scope, extra_txt):
    base = E.UNIVERSES[scope]
    if base is None:
        base = coingecko_top(300)
        if not base: st.warning("Couldn't reach CoinGecko, so the built-in broad list (~130 coins) is used."); base = E.BROAD
    ex = E.clean_symbols(re.split(r"[\s,;]+", extra_txt or ""))
    return tuple(E.clean_symbols(list(base) + ex))

def _live_fetch(symbols):
    if not symbols: return {}
    try:
        tk = [E.yf_ticker(s) for s in symbols]
        d = yf.download(tk, period="1d", interval="1m" if len(tk) <= 40 else "5m", progress=False, auto_adjust=True)["Close"]
        if isinstance(d, pd.Series): d = d.to_frame(tk[0])
        d = d.ffill().iloc[-1].dropna(); d.index = [E.sym_of(c) for c in d.index]
        return d.to_dict()
    except Exception:
        return {}

@st.cache_data(ttl=20, show_spinner=False)
def live_prices(symbols):
    """Latest traded price for each coin (cached 20 s; the top Refresh button clears it)."""
    return _live_fetch(symbols)

@st.cache_data(ttl=20, show_spinner=False)
def pf_prices(symbols):
    """Prices for your holdings only: its own cache, so the Portfolio refresh never touches the market-wide one."""
    return _live_fetch(symbols), now_ist().strftime("%H:%M:%S")

@st.cache_data(ttl=20, show_spinner=False)
def index_quotes():
    try: d = yf.download(["BTC-USD", "ETH-USD"], period="5d", auto_adjust=True, progress=False)["Close"].ffill()
    except Exception: return {}
    out = {}
    for k, v in (("BTC", "BTC-USD"), ("ETH", "ETH-USD")):
        if v in d.columns and d[v].notna().sum() >= 2:
            s = d[v].dropna(); out[k] = (float(s.iloc[-1]), float(s.iloc[-1] / s.iloc[-2] - 1) * 100)
    return out

def refresh_now(): live_prices.clear(); index_quotes.clear()
def refresh_portfolio(): pf_prices.clear()

@st.cache_data(ttl=3600, show_spinner=False)
def search_coins(q):
    try: res = yf.Search(q, max_results=15).quotes
    except Exception: return []
    out, seen = [], set()
    for r in res:
        t = str(r.get("symbol", ""))
        if t.endswith("-USD") and t not in seen:
            seen.add(t); out.append(dict(symbol=E.sym_of(t), name=r.get("longname") or r.get("shortname") or t))
    return out

@st.cache_data(ttl=1800, show_spinner=False)
def news(sym):
    try:
        out = []
        for it in (yf.Ticker(E.yf_ticker(sym)).news or [])[:5]:
            c = it.get("content") or it
            t = c.get("title"); pub = (c.get("provider") or {}).get("displayName") or it.get("publisher")
            if t: out.append(f"{t} ({pub})" if pub else t)
        return out
    except Exception: return []

MKT = {"Bitcoin": "BTC-USD", "Ethereum": "ETH-USD", "S&P 500": "^GSPC", "Nasdaq": "^IXIC", "US dollar index": "DX-Y.NYB",
       "US 10Y yield": "^TNX", "Gold": "GC=F", "VIX (fear gauge)": "^VIX"}
@st.cache_data(ttl=900, show_spinner=False)
def market_snapshot():
    try: d = yf.download(list(MKT.values()), period="10d", auto_adjust=True, progress=False)["Close"].ffill()
    except Exception: return {}
    out = {}
    for k, v in MKT.items():
        if v in d.columns and d[v].notna().sum() >= 2:
            s = d[v].dropna(); out[k] = (float(s.iloc[-1]), float(s.iloc[-1] / s.iloc[-2] - 1) * 100)
    return out

@st.cache_data(ttl=1800, show_spinner=False)
def fear_greed():
    """Crypto Fear & Greed Index (alternative.me), or None."""
    import requests
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()["data"][0]
        return int(r["value"]), str(r["value_classification"])
    except Exception: return None

@st.cache_data(ttl=10800, show_spinner=False)
def load_macro():
    """Global backdrop (dollar index, US 10-year yield, VIX, Nasdaq, gold): the measurable side of Fed and global policy. None if unavailable."""
    try:
        d = yf.download(list(E.MACRO.values()), period="5y", auto_adjust=True, progress=False)["Close"]
        d.index = pd.to_datetime(d.index)
        if getattr(d.index, "tz", None) is not None: d.index = d.index.tz_localize(None)
        d = d.rename(columns={v: k for k, v in E.MACRO.items()}).dropna(how="all")
        return d if len(d) > 300 else None
    except Exception: return None

@st.cache_data(ttl=1800, show_spinner=False)
def news_monitor():
    """Last 7 days of worldwide headlines on regulation, central banks, ETFs, security, India policy and geopolitics (Google News RSS)."""
    import requests
    from urllib.parse import quote
    out = {}
    for name, q in E.NEWS_QUERIES.items():
        try:
            r = requests.get(f"https://news.google.com/rss/search?q={quote(q + ' when:7d')}&hl=en-US&gl=US&ceid=US:en", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            out[name] = E.parse_rss(r.text) if r.status_code == 200 else []
        except Exception: out[name] = []
    return out

# ---------- load the coin list ----------
base = universe_symbols(uni, extra)
try: close, volume = load(base)
except Exception as e:
    st.error(f"Could not fetch data: {e}"); st.stop()

# ---------- account helpers ----------
def get_pos():
    """Open positions with any stop-loss / target edits you made later (Portfolio > Gemini review)."""
    pos, realised = E.positions(acct["trades"])
    for s, lv in acct.get("levels", {}).items():
        if s in pos:
            if lv.get("stop"): pos[s]["stop"] = lv["stop"]
            if lv.get("target"): pos[s]["target"] = lv["target"]
    return pos, realised

held = list(get_pos()[0])
alias = acct.get("alias", {})
rec_syms = [alias.get(r["symbol"]) or r["symbol"] for r in acct.get("recs", {}).get("items", [])]
order_syms = [o["symbol"] for o in acct.get("orders", [])]
extras = [s for s in dict.fromkeys(acct.get("watch", []) + held + order_syms + rec_syms) if s not in close.columns]
if extras:
    try: _xc, _xv = load(tuple(extras))
    except Exception: _xc = pd.DataFrame()
else: _xc = pd.DataFrame()
close_all = pd.concat([close, _xc.reindex(close.index.union(_xc.index))], axis=1) if len(_xc.columns) else close

# ---------- the model: features -> walk-forward test -> today's predictions ----------
@st.cache_data(ttl=3600, show_spinner="Training the model and testing it on past months… this takes 20 to 60 seconds the first time.")
def run_model(symbols, prof, cache_note=""):
    c, v = load(symbols)
    return E.run(c, v, prof, macro=load_macro())

try: MODEL = run_model(base, profile)
except Exception as e:
    st.error(f"The model could not run: {e}"); st.stop()
VAL, ASOF = MODEL["val"], MODEL["asof"]
VS = VAL["summary"]
NEWS = news_monitor(); RISK = E.policy_risk(NEWS)
def event_list():
    """Your calendar plus, when the worldwide headlines read risk-off, a mild market-wide caution."""
    ev = list(acct.get("events", []))
    if RISK["label"] == "Risk-off":
        ev.append(dict(date=f"{ASOF:%Y-%m-%d}", title="Risk-off policy/news headlines this week", coins=["ALL"], severity="Low", direction="Negative"))
    return ev
tbl = E.apply_events(MODEL["table"], event_list(), ASOF)
tops = list(tbl.head(topn).index)

# ---------- live prices ----------
need = [s for s in dict.fromkeys(acct.get("watch", []) + order_syms + rec_syms + tops) if s in close_all.columns]
live = dict(live_prices(tuple(need)))
_pf_live, PF_STAMP = pf_prices(tuple(s for s in held if s in close_all.columns))
live.update(_pf_live)
utc_today = lambda: dt.datetime.now(dt.timezone.utc).date()
def px(s):
    if s in live: return float(live[s])
    return float(close_all[s].dropna().iloc[-1]) if s in close_all.columns and close_all[s].notna().any() else float("nan")
def prev_close(s):
    if s not in close_all.columns: return float("nan")
    c = close_all[s].dropna()
    if len(c) < 2: return float("nan")
    return float(c.iloc[-2] if c.index[-1].date() == utc_today() else c.iloc[-1])
def day_pct(s, price=None):
    price = px(s) if price is None else price; pc = prev_close(s)
    return (price / pc - 1) * 100 if pc == pc and pc and price == price else float("nan")
def price_of(sym):
    v = px(sym)
    if v == v: return v
    try:
        c, _ = load((sym,)); return float(c[sym].dropna().iloc[-1])
    except Exception: return float("nan")

# ---------- trading (long-only spot; trading cost is built into the fill price, so all P&L maths stays exact) ----------
def fill_price(price, side): return price * (1 + fee) if side == "BUY" else price * (1 - fee)

def trade(sym, side, qty, price, stop=None, target=None, horizon=None):
    pos, _ = E.positions(acct["trades"]); cash = E.cash_balance(acct["start_cash"], acct["trades"]); qty = E.round_qty(qty)
    if qty <= 0: return "Amount is too small."
    if not price == price or price <= 0: return "No price available for this coin."
    fill = fill_price(price, side)
    if side == "BUY" and qty * fill > cash + 1e-6: return f"Not enough virtual cash (${cash:,.2f} available)."
    if side == "SELL" and pos.get(sym, {}).get("qty", 0) < qty - 1e-9: return "You don't hold that many coins."
    acct["trades"].append(dict(ts=now_ist().replace(tzinfo=None).isoformat(timespec="seconds"), symbol=sym, side=side, qty=qty,
                               price=float(f"{fill:.10g}"), stop=stop, target=target, horizon=horizon))
    lv = acct.setdefault("levels", {})
    if side == "BUY" and (stop or target): lv.pop(sym, None)
    if side == "SELL" and sym not in E.positions(acct["trades"])[0]: lv.pop(sym, None)
    save(); return None

def run_orders():
    """Pending limit orders and stop-loss / target auto-sell (checked whenever the app is open)."""
    msgs = []
    for o in list(acct.get("orders", [])):
        last = px(o["symbol"])
        if last != last: continue
        if (last <= o["limit"]) if o["side"] == "BUY" else (last >= o["limit"]):
            err = trade(o["symbol"], o["side"], o["qty"], last, o.get("stop"), o.get("target"), o.get("horizon"))
            acct["orders"].remove(o); save()
            msgs.append(f"✅ Limit {o['side'].lower()} {U.qty_txt(o['qty'])} {o['symbol']} filled @ {U.usd(last)}" if not err else f"⚠️ Limit order for {o['symbol']} rejected: {err}")
    if auto_exec:
        for s, p in get_pos()[0].items():
            q, last = p["qty"], px(s)
            if last != last: continue
            if p["stop"] and last <= p["stop"] and not trade(s, "SELL", q, last): msgs.append(f"🛑 {s}: stop-loss hit, sold {U.qty_txt(q)} @ {U.usd(last)}")
            elif p["target"] and last >= p["target"] and not trade(s, "SELL", q, last): msgs.append(f"🎯 {s}: target hit, sold {U.qty_txt(q)} @ {U.usd(last)}")
    return msgs

_msgs = run_orders()
if _msgs:
    for _m in _msgs: st.toast(_m)
    st.session_state.msgs = _msgs; st.rerun()
for _m in st.session_state.pop("msgs", []): st.info(_m)

# ---------- portfolio numbers (used by Portfolio, AI review and the live bar) ----------
pos, realised = get_pos()
ltp = {s: px(s) for s in pos}
perf = E.performance(acct["trades"], acct["start_cash"], ltp)
mh = E.snapshot(close_all[[s for s in pos if s in close_all.columns]], min_len=3) if pos else pd.DataFrame()

def _hold_row(s):
    r = mh.loc[s].copy()
    if s in tbl.index: r["p_up"], r["g_med"] = tbl.loc[s, "p_up"], tbl.loc[s, "g_med"]
    return r

def build_rows():
    rows = []
    for s, p in pos.items():
        avg = p["cost"] / p["qty"]; last = ltp[s] if ltp[s] == ltp[s] else avg
        cur = p["qty"] * last; pc = prev_close(s)
        d_pnl = (last - pc) * p["qty"] if pc == pc else 0.0
        have = s in mh.index
        sig, why = E.signal(_hold_row(s), last, p["stop"], p["target"]) if have else ("HOLD", "Not enough price history yet")
        rows.append(dict(symbol=s, name=None, qty=p["qty"], avg=avg, ltp=last, invested=p["cost"], current=cur,
                         pnl=cur - p["cost"], pnl_pct=(cur / p["cost"] - 1) * 100 if p["cost"] else 0.0,
                         day_pnl=d_pnl, day_pct=(last / pc - 1) * 100 if pc == pc and pc else float("nan"),
                         stop=p["stop"], target=p["target"], horizon=p.get("horizon"), since=p["since"],
                         signal=sig, reason=why, tags=E.tags_for(_hold_row(s))[:3] if have else []))
    tot = sum(r["current"] for r in rows) or 1.0
    for r in rows: r["weight"] = r["current"] / tot * 100
    return rows
rows = build_rows()
def _day_totals():
    d = sum(r["day_pnl"] for r in rows)
    return d, (d / (perf["current"] - d) * 100 if perf["current"] - d > 0 else 0.0)
day_total, day_total_pct = _day_totals()

def refresh_state():
    """Recompute the portfolio numbers from the latest prices (the live Portfolio fragment calls this every refresh)."""
    global pos, realised, ltp, perf, mh, rows, day_total, day_total_pct
    pos, realised = get_pos(); ltp = {s: px(s) for s in pos}
    perf = E.performance(acct["trades"], acct["start_cash"], ltp)
    mh = E.snapshot(close_all[[s for s in pos if s in close_all.columns]], min_len=3) if pos else pd.DataFrame()
    rows = build_rows(); day_total, day_total_pct = _day_totals()

# live net-worth points for this session (Portfolio > Growth)
_ls = st.session_state.setdefault(f"live_series_{user}", [])
if acct["trades"] and (not _ls or time.time() - _ls[-1][0] >= 10):
    _ls.append((time.time(), perf["net"])); del _ls[:-600]

# ======================================================================
# Shared pieces
# ======================================================================
snap = market_snapshot()
HZ_TAG = "30D"
REGIME, REGIME_TXT = E.regime(close, volume)

def build_context():
    n = lambda x, f="{:.0f}": "n/a" if x is None or x != x else f.format(x)
    L = [f"As of {ASOF:%d %b %Y}. {REGIME_TXT}"]
    if snap: L.append("Market snapshot: " + "; ".join(f"{k} {v:,.2f} ({d:+.1f}% 1d)" for k, (v, d) in snap.items()))
    L.append(f"Trader's country: {country}. {E.COUNTRIES[country]}")
    L.append(f"Worldwide policy/news tone over the last 7 days (headline scan, {RISK['n']} headlines): {RISK['label']} (score {RISK['score']:+.2f}). " + "; ".join(f"{k} {v:+.2f}" for k, v in RISK["by_group"].items()))
    if RISK["worst"]: L.append("Most negative recent headlines:\n" + "\n".join("- " + w["title"] for w in RISK["worst"]))
    evs = sorted(acct.get("events", []), key=lambda e: e["date"])
    L.append("Event calendar (next 30 days, entered by the trader or found earlier):\n" + ("\n".join(f"- {e['date']}: {e['title']} [{e['kind']}, {e['severity']}, {e['direction']}, coins {','.join(e['coins'])}]" for e in evs) or "- none entered yet; search for them"))
    if MODEL.get("macro_used"): L.append("The app's model also uses the global backdrop (dollar index, US 10-year yield, VIX, Nasdaq, gold).")
    if VS: L.append(f"The app's model was tested on {VS['n_folds']} past months: reliability '{VS['verdict']}', top-{VS['topn']} picks rose {VS['top_win']*100:.0f}% of the time.")
    out = [f"{s}: price {r.price:,.6g}, predicted 30d move {r.g_med*100:+.0f}% (range {r.g_lo*100:+.0f}% to {r.g_hi*100:+.0f}%), chance up {r.p_up*100:.0f}%, "
           f"30d {n(r.r30*100)}%, 90d {n(r.r90*100)}%, RSI {n(r.rsi)}, volatility {n(r.vol*100)}%" + (f", EVENT RISK: {r.event_flags}" if r.event_flags else "") for s, r in tbl.head(10).iterrows()]
    L.append("The app's statistical model top-10 for the next 30 days:\n" + "\n".join(out))
    L.append("Demo holdings: " + (", ".join(f"{r['symbol']} x{U.qty_txt(r['qty'])} avg {r['avg']:.6g} now {r['ltp']:.6g}" for r in rows) or "none"))
    if acct.get("notes"): L.append("My own notes on news / regulation / events:\n" + acct["notes"])
    return "\n".join(L)

def copy_open_gemini(prompt, height=110):
    tj = json.dumps(prompt).replace("</", "<\\/")
    components.html(f"""<div style="display:flex;gap:8px">
<button id="c" style="flex:1;padding:12px;border:0;border-radius:10px;background:#00b386;color:#fff;font:600 15px system-ui">📋 Copy prompt</button>
<button id="o" style="flex:1;padding:12px;border:0;border-radius:10px;background:#5367ff;color:#fff;font:600 15px system-ui">🚀 Copy + open Gemini</button></div>
<div id="m" style="font:14px system-ui;margin-top:8px;color:#555"></div>
<script>const t={tj};const $=i=>document.getElementById(i);
async function cp(){{if(window.Android&&Android.copy){{try{{Android.copy(t);return true}}catch(e){{}}}}
try{{await navigator.clipboard.writeText(t);return true}}catch(e){{}}
try{{const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();const ok=document.execCommand('copy');a.remove();return ok}}catch(e){{return false}}}}
$('c').onclick=async()=>{{$('m').textContent=(await cp())?'✅ Copied. Now open Gemini and paste.':'⚠️ Copy failed: open Show prompt text below and long-press to copy.'}};
$('o').onclick=async()=>{{const ok=await cp();$('m').textContent=ok?'✅ Prompt copied. Opening Gemini, paste it there.':'Opening Gemini… (copy the prompt from Show prompt text below)';
if(window.Android&&Android.openGemini){{Android.openGemini()}}
else if(/Android/i.test(navigator.userAgent)){{window.open('intent://gemini.google.com/app#Intent;scheme=https;package=com.google.android.apps.bard;S.browser_fallback_url=https%3A%2F%2Fgemini.google.com%2Fapp;end','_top')}}
else{{window.open('https://gemini.google.com/app','_blank')}}}};</script>""", height=height)
    st.link_button("🌐 Open Gemini in browser instead", "https://gemini.google.com/app", use_container_width=True)

def style_df(df, pnl=(), side=None, fmt=None):
    """Colour profit green / loss red (and BUY green / SELL red) in a table; falls back to the plain table."""
    try:
        sty = df.style; mp = sty.map if hasattr(sty, "map") else sty.applymap
        if pnl: sty = mp(lambda v: f"color:{U.UP};font-weight:600" if isinstance(v, (int, float)) and v > 0 else f"color:{U.DOWN};font-weight:600" if isinstance(v, (int, float)) and v < 0 else "", subset=list(pnl))
        if side:
            mp = sty.map if hasattr(sty, "map") else sty.applymap
            sty = mp(lambda v: f"color:{U.UP};font-weight:700" if v == "BUY" else f"color:{U.DOWN};font-weight:700" if v == "SELL" else "", subset=[side])
        return sty.format(fmt or {}, na_rep="–")
    except Exception:
        return df

# ---------- live bar: BTC / ETH, countdown, refresh ----------
def index_bar():
    iq = index_quotes()
    st.markdown(U.live_bar_html(iq, now_ist().strftime("%H:%M:%S")), unsafe_allow_html=True)
    if iq.get("BTC") and iq["BTC"][1] <= -4.0:
        st.warning(f"Bitcoin is down {iq['BTC'][1]:.1f}% in a day. Crypto often falls together on days like this, so new buys are riskier and your stop-losses matter.")
st.fragment(run_every=(every if auto_on else None))(index_bar)()
with _head:
    _h1, _h2 = st.columns([6, 1], vertical_alignment="center")
    _h1.markdown(TITLE, unsafe_allow_html=True)
    if _h2.button("⚙️", key="open_settings", help="Settings"): show_dialog("settings")
with st.container(key="topbar"):
    _l2, _l3 = st.columns([6, 1], vertical_alignment="center")
    with _l2: components.html(U.countdown_html(every if auto_on else 0, "gw-stamp-mk", "Price refresh in"), height=34)
    _l3.button("🔄", key="live_refresh", on_click=refresh_now, help="Refresh prices now")

# ---------- Buy window ----------
@st.dialog("Buy")
def buy_dialog(sym, name, price, dpct, stop=None, target=None, amount=None):
    p0 = get_pos()[0].get(sym); k = f"bd_{sym}_"
    st.markdown(U.dialog_head_html(sym, name, price, dpct, p0["qty"] if p0 else 0, p0["cost"] / p0["qty"] if p0 else None), unsafe_allow_html=True)
    otype = st.radio("Order type", ["Market", "Limit"], horizontal=True, key=k + "type",
                     help="Market: buy now at the current price. Limit: wait until the price falls to your price (checked whenever the app is open).")
    cash_ = E.cash_balance(acct["start_cash"], acct["trades"])
    c1, c2 = st.columns(2)
    amt = c1.number_input("Amount to invest ($)", 1.0, 1e9, float(round(min(amount or capital * 0.1, max(cash_, 1.0)), 2)), step=50.0, key=k + "amt")
    if otype == "Limit": lim = c2.number_input("Limit price ($)", 0.0000001, 1e9, float(price), format="%.8g", key=k + "lim")
    else: lim = None; c2.text_input("Price", "At market", disabled=True, key=k + "mk")
    at = fill_price(lim or price, "BUY"); q = E.round_qty(amt / at)
    with st.expander("🛡 Stop-loss (required) & target (optional)", expanded=True):
        sl = st.number_input("Stop-loss ($): sells automatically if the price falls to it. Limits your loss.", 0.0, 1e9,
                             float(f"{stop:.8g}") if stop else float(f"{price * 0.85:.8g}"), format="%.8g", key=k + "sl",
                             help="Default is 15% below the price. A stop-loss caps the loss on this coin but can't remove it: crypto can gap through it.")
        tg = st.number_input("Target ($): sells automatically when the price reaches it", 0.0, 1e9, float(f"{target:.8g}") if target else 0.0, format="%.8g", key=k + "tg")
    st.markdown(U.order_box_html([("You get", U.esc(f"{U.qty_txt(q)} {sym}")), ("Order value", U.esc(U.usd(q * at, 2))),
                                  (f"Trading cost ({fee * 100:g}%)", U.esc(U.usd(q * (lim or price) * fee, 2))),
                                  ("Cash after order", f'<span class="gw-{"up" if cash_ >= q * at else "down"}">{U.esc(U.usd(cash_ - q * at, 2))}</span>')]), unsafe_allow_html=True)
    errs = []
    if q <= 0: errs.append("Amount is too small for this coin.")
    if not sl: errs.append("A stop-loss is required: it limits how much this coin can lose.")
    if sl and sl >= (lim or price): errs.append("Stop-loss must be below the buy price.")
    if tg and tg <= (lim or price): errs.append("Target must be above the buy price.")
    if q * at > cash_ + 1e-6: errs.append("Not enough virtual cash for this order.")
    for e_ in errs: st.error(e_)
    if st.button(f"Buy {U.qty_txt(q)} {sym}" + (" (limit order)" if lim else ""), type="primary", key="exec_buy", use_container_width=True, disabled=bool(errs)):
        if lim:
            acct.setdefault("orders", []).append(dict(id=int(time.time() * 1000), ts=now_ist().strftime("%d %b %H:%M"), symbol=sym, side="BUY", qty=float(q),
                                                      limit=float(lim), stop=sl or None, target=tg or None, horizon=HZ_TAG))
            save(); flash(f"🕒 Limit order placed: buy {U.qty_txt(q)} {sym} at {U.usd(lim)} or lower")
        else:
            fresh = live_prices((sym,)).get(sym); mkt = float(fresh) if fresh else price
            if sl >= mkt: st.error("The price just moved to or below your stop-loss. Lower the stop-loss and try again."); return
            err = trade(sym, "BUY", E.round_qty(amt / fill_price(mkt, "BUY")), mkt, sl or None, tg or None, HZ_TAG)
            if err: st.error(err); return
            flash(f"✅ Bought {sym} for about {U.usd(amt, 2)} @ {U.usd(mkt)}")
        st.rerun()

# ---------- Sell window ----------
@st.dialog("Sell")
def sell_dialog(sym, name, price, dpct, qty=None):
    p = get_pos()[0].get(sym); k = f"sd_{sym}_"
    if not p: st.info("You don't hold this coin."); return
    avg = p["cost"] / p["qty"]
    st.markdown(U.dialog_head_html(sym, name, price, dpct, p["qty"], avg), unsafe_allow_html=True)
    mode = st.radio("Quantity", ["All coins", "Some coins"], horizontal=True, index=1 if qty and qty < p["qty"] else 0, key=k + "mode")
    q = p["qty"] if mode == "All coins" else st.number_input("Coins to sell", 0.000001, float(p["qty"]), float(min(max(qty or p["qty"] / 2, 0.000001), p["qty"])), format="%.8g", key=k + "q")
    otype = st.radio("Order type", ["Market", "Limit"], horizontal=True, key=k + "type",
                     help="Market: sell now at the current price. Limit: wait until the price rises to your price (checked whenever the app is open).")
    lim = st.number_input("Limit price ($)", 0.0000001, 1e9, float(price), format="%.8g", key=k + "lim") if otype == "Limit" else None
    at = fill_price(lim or price, "SELL"); pnl_ = (at - avg) * q
    st.markdown(U.order_box_html([("Buy price (avg)", U.esc(U.usd(avg))), ("Sell price", U.esc(U.usd(at)) if lim else "At market ≈ " + U.esc(U.usd(at))),
                                  ("You receive", U.esc(U.usd(q * at, 2))), ("Profit / loss", U.pnl_html(pnl_, (at / avg - 1) * 100))]), unsafe_allow_html=True)
    if st.button(f"Sell {U.qty_txt(q)} {sym}" + (" (limit order)" if lim else ""), key="exec_sell", use_container_width=True):
        if lim:
            acct.setdefault("orders", []).append(dict(id=int(time.time() * 1000), ts=now_ist().strftime("%d %b %H:%M"), symbol=sym, side="SELL", qty=float(q), limit=float(lim)))
            save(); flash(f"🕒 Limit order placed: sell {U.qty_txt(q)} {sym} at {U.usd(lim)} or higher")
        else:
            fresh = live_prices((sym,)).get(sym); mkt = float(fresh) if fresh else price
            err = trade(sym, "SELL", q, mkt)
            if err: st.error(err); return
            fa = fill_price(mkt, "SELL")
            flash(f"✅ Sold {U.qty_txt(q)} {sym} @ {U.usd(mkt)} · P&L {U.usd((fa - avg) * q, 2, True)} ({U.pct((fa / avg - 1) * 100)})")
        st.rerun()

def open_buy(sym, name, price, dpct, p=None, amount=None):
    stop = tgt = None
    if p:   # a "wait for dip" plan is built around the dip price: keep the same distances but measure them from today's price
        shift = price - p["entry"]
        stop, tgt = p["stop"] + shift, p["target"] + shift
        if not (0 < stop < price < tgt): stop = tgt = None
    show_dialog("buy", sym, name, price, dpct, stop, tgt, amount)

_pd = st.session_state.pop("pending_dlg", None)
if _pd: {"buy": buy_dialog, "sell": sell_dialog, "settings": settings_dialog}[_pd[0]](*_pd[1])

def watch_button(c, sym, key):
    w = acct.setdefault("watch", [])
    if sym in w:
        if c.button("★ Watching", key=f"wt_{key}", use_container_width=True): w.remove(sym); save(); st.rerun()
    elif c.button("☆ Watch", key=f"wt_{key}", use_container_width=True): w.append(sym); save(); st.rerun()

# ======================================================================
# Predict: top coins for the next 30 days
# ======================================================================
def move_html(r):
    return (f'<div class="gw-ret"><span class="gw-k">Predicted 30-day move (median)</span>{U.pct_html(r["g_med"] * 100, 1)}</div>'
            f'<div class="gw-sub">Model range (10th to 90th percentile): {U.esc(U.pct(r["g_lo"] * 100, 0))} to {U.esc(U.pct(r["g_hi"] * 100, 0))} · '
            f'chance up <b>{r["p_up"] * 100:.0f}%</b> · beats the typical coin <b>{r["p_beat"] * 100:.0f}%</b></div>')

def idea_card(sym, r, ns, i, price, dpct, scan=False):
    r = r.copy(); r["price"] = price
    p = E.plan(r, capital, risk, maxpos)
    sig, why = E.idea_signal(r)
    if p["wait"] and sig == "BUY": sig = "HOLD"
    tags = U.chip(f"#{int(r['rank'])}", "info") + U.hz_chip(HZ_TAG) + U.action_chip(sig) + U.chips(E.tags_for(r, scan)[:4]) + (U.chip("📅 Event risk", "risk") if r.get("event_flags") else "")
    vol = r.vol if r.vol == r.vol else 0.8
    with st.container(border=True, key=f"card_{ns}_{i}"):
        st.markdown(U.stock_head_html(sym, f"Potential score {r['score']:.0f}/100", price, dpct, tags) + move_html(r) + U.levels_html(price, p["entry"], p["stop"], p["target"]), unsafe_allow_html=True)
        st.caption(f"**{p['action']}** · suggested size {U.usd(p['invest'], 0)} ({U.qty_txt(p['qty'])} {sym}) · worst case at the stop ≈ {U.usd(p['max_loss'], 0)} · "
                   f"'Target' is the model's optimistic case; the median case is {U.usd(p['expected'])}")
        st.caption(f"{why}. 7d {pc_(r.r7)} · 30d {pc_(r.r30)} · 90d {pc_(r.r90)} · RSI {r.rsi:.0f} · volatility {vol * 100:.0f}% a year"
                   + (f" · {(price / r.hi365 - 1) * 100:.0f}% from the 1-year high" if r.hi365 == r.hi365 and r.hi365 else ""))
        if r.get("event_flags"): st.caption("📅 Upcoming: " + r["event_flags"] + ". Confidence is lowered and the range widened for this coin.")
        if scan and r.avg_val == r.avg_val: st.caption(f"Average daily traded value ${r.avg_val / 1e6:,.1f}M" + (" · thinly traded, use limit orders and small size" if r.avg_val < 5e6 else ""))
        if st.checkbox("📰 Latest headlines", key=f"rs_{ns}_{i}"):
            for h in news(sym) or ["No recent headlines found."]: st.caption("• " + h)
        c1, c2 = st.columns(2)
        if c1.button("Buy", key=f"buy_{ns}_{i}", type="primary", use_container_width=True): open_buy(sym, None, price, dpct, p, p["invest"])
        watch_button(c2, sym, f"{ns}_{i}")
pc_ = lambda x: "n/a" if x is None or pd.isna(x) else f"{x * 100:+.0f}%"

def trust_banner():
    if not VS:
        st.warning("The model could not be tested on past months with this coin list. Treat every number as a guess."); return
    {"Strong": st.success, "Moderate": st.info}.get(VS["verdict"], st.warning)(
        f"**Model reliability: {VS['verdict']}.** {VS['verdict_text']} In {VS['n_folds']} out-of-sample months its top {VS['topn']} picks rose "
        f"{VS['top_win'] * 100:.0f}% of the time (all coins: {VS['base_up'] * 100:.0f}%) and averaged {VS['top_ret'] * 100:+.1f}% against {VS['market_ret'] * 100:+.1f}% for the average coin. Details: Tools > Model accuracy.")
    st.caption(f"🌐 Global backdrop in the model: {'used' if MODEL.get('macro_used') else 'tested but not used (it did not help out-of-sample)' if MODEL.get('macro_ic') is not None else 'unavailable'}"
               + (f" (rank correlation {MODEL['macro_ic']:+.3f} with it vs {MODEL['base_ic']:+.3f} without)" if MODEL.get("macro_ic") is not None and MODEL.get("base_ic") is not None else "")
               + f" · Worldwide policy/news headlines this week: **{RISK['label']}**" + (" (all coins carry a mild caution)" if RISK["label"] == "Risk-off" else "") + ". Details: Tools > News & policy.")
    if REGIME == "bear": st.warning("🐻 " + REGIME_TXT)
    elif REGIME == "bull": st.caption("🐂 " + REGIME_TXT)
    else: st.caption(REGIME_TXT)

def predict_section():
    live.update(live_prices(tuple(need)))
    p_top, p_all, p_find = st.tabs(["🔮 Top 30-day picks", "📋 All coins", "🔍 Find a coin & watchlist"])
    with p_top, box("Coins with the highest predicted 1-month potential", "top", f"Ranked from {len(tbl)} liquid coins as of {ASOF:%d %b %Y}. No minimum gain: the best potential, whatever its size."):
        trust_banner()
        cols = st.columns(2)
        for n_, (sym, r) in enumerate(tbl.head(topn).iterrows()):
            price = px(sym); price = price if price == price else r.price
            with cols[n_ % 2]: idea_card(sym, r, "top", n_, price, day_pct(sym, price))
        st.caption("Predictions come from a gradient-boosting model trained on every coin's past 30-day outcomes, using momentum, volatility, volume, trend and market-wide features. It estimates probabilities, not certainties.")
    with p_all, box("Every coin that passes the liquidity filter", "all", "Sortable. Download it as a spreadsheet."):
        show = pd.DataFrame({"Rank": tbl["rank"].astype(int), "Coin": tbl.index, "Price $": tbl.price, "Predicted 30d": tbl.g_med * 100,
                             "Low (10th pct)": tbl.g_lo * 100, "High (90th pct)": tbl.g_hi * 100, "Chance up %": tbl.p_up * 100, "Beats typical coin %": tbl.p_beat * 100,
                             "Score": tbl.score, "30d actual": tbl.r30 * 100, "90d actual": tbl.r90 * 100, "RSI": tbl.rsi,
                             "Volume $M/day": tbl.avg_val / 1e6}).reset_index(drop=True)
        st.dataframe(style_df(show, pnl=("Predicted 30d", "30d actual", "90d actual"),
                              fmt={"Price $": "{:,.6g}", "Predicted 30d": "{:+.1f}%", "Low (10th pct)": "{:+.0f}%", "High (90th pct)": "{:+.0f}%", "Chance up %": "{:.0f}%",
                                   "Beats typical coin %": "{:.0f}%", "Score": "{:.0f}", "30d actual": "{:+.0f}%", "90d actual": "{:+.0f}%", "RSI": "{:.0f}", "Volume $M/day": "{:,.1f}"}),
                     hide_index=True, use_container_width=True)
        st.download_button("⬇️ Download as CSV", show.to_csv(index=False), f"crypto_predictions_{ASOF:%Y%m%d}.csv", "text/csv")
    with p_find: search_view()

def analyze(sym):
    try: c, v = load((sym,))
    except Exception: c = pd.DataFrame()
    if c.empty or sym not in c.columns:
        st.error("No price data found for this coin on Yahoo Finance (it needs a SYMBOL-USD listing). Check the symbol."); return
    sn = E.snapshot(c, v, min_len=3)
    if sym not in sn.index: st.error("Not enough price history yet."); return
    r = sn.loc[sym].copy(); lp = live_prices((sym,)).get(sym); r["price"] = float(lp) if lp else r.price; price = r.price
    dpc = day_pct(sym, price) if sym in close_all.columns else float("nan")
    in_model = sym in tbl.index
    if not in_model and st.session_state.get("ana_run") == sym:
        try:
            _res = run_model(tuple(dict.fromkeys(list(base) + [sym])), profile, f"add_{sym}")
            _rt = E.apply_events(_res["table"], event_list(), ASOF)
            if sym in _rt.index: r = _rt.loc[sym].copy(); r["price"] = price; in_model = True
            else: st.warning("This coin is too thinly traded or too new for the selected risk profile, so no prediction is made. Choose a looser profile in ⚙️ Settings.")
        except Exception as e: st.error(f"Could not run the model: {e}")
    elif in_model: r = tbl.loc[sym].copy(); r["price"] = price
    vol = r.vol if r.vol == r.vol else 0.8
    with st.container(border=True, key=f"card_find_{sym}"):
        st.markdown(U.stock_head_html(sym, "", price, dpc, U.chips(E.tags_for(r)[:4])) + (move_html(r) if in_model else ""), unsafe_allow_html=True)
        st.caption(f"7d {pc_(r.r7)} · 30d {pc_(r.r30)} · 90d {pc_(r.r90)} · RSI {'n/a' if r.rsi != r.rsi else f'{r.rsi:.0f}'} · volatility {vol * 100:.0f}% a year · {int(r.days)} days of history")
        if r.days < 180: st.warning(f"🆕 New coin: only {int(r.days)} days of history, so every number is rough.")
        if in_model:
            p = E.plan(r, capital, risk, maxpos); sig, why = E.idea_signal(r)
            st.markdown(f'<div class="gw-chips">{U.hz_chip(HZ_TAG)}{U.action_chip("HOLD" if p["wait"] and sig == "BUY" else sig)}<span class="gw-sub">{U.esc(why)} · {U.esc(p["action"])}</span></div>'
                        + U.levels_html(price, p["entry"], p["stop"], p["target"]), unsafe_allow_html=True)
            if st.button("Buy", key=f"fbuy_{sym}", type="primary", use_container_width=True): open_buy(sym, None, price, dpc, p, p["invest"])
        else:
            if st.button("🔮 Predict this coin (adds it to the model, 20 to 60 s)", key=f"anarun_{sym}", use_container_width=True): st.session_state["ana_run"] = sym; st.rerun()
            st.caption(f"Without the model: chance of gaining 10% in a month under a plain random-walk assumption is about {E.p_hit(10, vol, 1) * 100:.0f}% (a sanity check, not a forecast).")
        for h in news(sym)[:4]: st.caption("• " + h)
        watch_button(st, sym, f"find_{sym}")

def search_view():
    with box("🔍 Find a coin", "find", "Any coin Yahoo Finance lists as SYMBOL-USD"):
        q = st.text_input("Search by name or symbol", placeholder="e.g. Hyperliquid, HYPE, Solana, SOL")
        if q.strip():
            hits = search_coins(q.strip())
            opts = {f"{h['name']} ({h['symbol']})": h["symbol"] for h in hits}
            manual = re.sub(r"[^A-Za-z0-9-]", "", q).upper()
            if manual and manual not in opts.values(): opts[f"Try symbol directly: {manual}"] = manual
            pick = st.selectbox("Matches", list(opts))
            if st.button("Analyze", type="primary"): st.session_state["find"] = opts[pick]; st.session_state.pop("ana_run", None)
        if st.session_state.get("find"): analyze(st.session_state["find"])
    if acct.get("watch"):
        with box("⭐ My watchlist", "watch"):
            cols = st.columns(2)
            for i, s in enumerate(list(acct["watch"])):
                price = price_of(s); dp = day_pct(s, price)
                with cols[i % 2], st.container(border=True, key=f"card_watch_{i}"):
                    pr = E.idea_signal(tbl.loc[s])[0] if s in tbl.index else None
                    st.markdown(U.stock_head_html(s, f"Predicted 30d {tbl.loc[s, 'g_med'] * 100:+.0f}% · chance up {tbl.loc[s, 'p_up'] * 100:.0f}%" if s in tbl.index else "", price, dp, U.action_chip(pr) if pr else ""), unsafe_allow_html=True)
                    b1, b2, b3 = st.columns(3)
                    if b1.button("Buy", key=f"buy_watch_{i}", type="primary", use_container_width=True): open_buy(s, None, price, dp, None, capital * 0.05)
                    if b2.button("Analyze", key=f"wa_{i}", use_container_width=True): st.session_state["find"] = s; st.session_state.pop("ana_run", None); st.rerun()
                    if b3.button("Remove", key=f"wr_{i}", use_container_width=True): acct["watch"].remove(s); save(); st.rerun()

with st.container(key="mainnav"):      # the main ribbon: styled as iPhone-style pills (see ui.GROWW_CSS)
    t_ex, t_pf, t_ai, t_tools = st.tabs(["🔮 Predict", "💼 Portfolio", "🤖 Gemini", "🛠 Tools"])
with t_ex:
    st.fragment(run_every=(every if auto_on else None))(predict_section)()

# ======================================================================
# Portfolio
# ======================================================================
def protect_all():
    """Give every holding a stop-loss: 15% below the price, or at your buy price once you are comfortably in profit. Never lowers a stop."""
    n = 0
    for r in rows:
        s, last, avg = r["symbol"], r["ltp"], r["avg"]
        want = avg if last >= avg * 1.05 else last * 0.85
        want = float(f"{min(want, last * 0.99):.8g}")
        if want > (r["stop"] or 0):
            acct.setdefault("levels", {}).setdefault(s, {})["stop"] = want; n += 1
    save(); flash(f"🛡 Stop-loss set or raised on {n} holding{'s' if n != 1 else ''}" if n else "All holdings already have a stop-loss at or above this level.")

def holdings_view():
    with st.container(key="pfhero"):
        hc1, hc2 = st.columns([3, 1], vertical_alignment="center")
        hc1.markdown(U.summary_html(perf, day_total, day_total_pct, len(rows), PF_STAMP, plain=True, part="top"), unsafe_allow_html=True)
        with hc2:
            st.markdown(U.summary_html(perf, day_total, day_total_pct, len(rows), PF_STAMP, plain=True, part="stamp"), unsafe_allow_html=True)
            components.html(U.dial_html(pf_every if pf_auto else 0, "gw-stamp-pf", "pf_refresh"), height=52)
        st.markdown(U.summary_html(perf, day_total, day_total_pct, len(rows), PF_STAMP, plain=True, part="rest"), unsafe_allow_html=True)
        st.button("🔄", key="pf_refresh", on_click=refresh_portfolio)      # pressed by the dial; hidden by CSS
    if not rows:
        st.info("No holdings yet. Open **Predict**, pick an idea and tap **Buy**."); return
    with box("🛡 Protection", "prot", "Stop-losses cap how much a coin can lose"):
        nostop = [r["symbol"] for r in rows if not r["stop"]]
        if nostop: st.warning("No stop-loss on: " + ", ".join(nostop) + ". Without one, a falling coin keeps losing.")
        st.button("🛡 Protect all holdings (set / raise stop-loss)", key="protect_all", on_click=protect_all, use_container_width=True,
                  help="Stop-loss 15% below the price, or at your buy price once you are 5% or more in profit. It sells automatically (while the app is open) to cap the loss.")
    with box("📦 Your holdings", "hold"):
        f1, f2 = st.columns(2)
        sort = f1.selectbox("Sort by", ["Returns %", "Returns $", "24h %", "Current value", "A–Z"], key="pf_sort")
        flt = f2.selectbox("Show", ["All", "In profit", "In loss", "SELL signal"], key="pf_flt")
        keyf = {"Returns %": lambda r: -r["pnl_pct"], "Returns $": lambda r: -r["pnl"], "24h %": lambda r: -(r["day_pct"] if r["day_pct"] == r["day_pct"] else -999),
                "Current value": lambda r: -r["current"], "A–Z": lambda r: r["symbol"]}[sort]
        ok = {"All": lambda r: True, "In profit": lambda r: r["pnl"] > 0, "In loss": lambda r: r["pnl"] < 0, "SELL signal": lambda r: r["signal"] == "SELL"}[flt]
        shown = sorted([r for r in rows if ok(r)], key=keyf)
        if not shown: st.caption("No holdings match this filter."); return
        cols = st.columns(2)
        for i, h in enumerate(shown):
            s = h["symbol"]
            with cols[i % 2], st.container(border=True, key=f"card_hold_{i}"):
                st.markdown(U.holding_html(h), unsafe_allow_html=True)
                b1, b2, b3 = st.columns(3)
                if b1.button("Buy more", key=f"buy_hold_{s}", type="primary", use_container_width=True): show_dialog("buy", s, h["name"], h["ltp"], h["day_pct"], h["stop"], h["target"], max(h["current"] * 0.25, 10.0))
                if b2.button("Sell", key=f"sell_hold_{s}", use_container_width=True): show_dialog("sell", s, h["name"], h["ltp"], h["day_pct"])
                if b3.toggle("Details", key=f"det_{s}"):
                    sn = close_all[s].dropna() if s in close_all.columns else pd.Series(dtype=float)
                    sn = sn[sn.index >= pd.Timestamp(h["since"]).normalize() - pd.Timedelta(days=7)]
                    if len(sn) > 1:
                        st.line_chart(pd.DataFrame({"Value of your coins": sn * h["qty"], "Amount invested": h["invested"]}), color=[U.UP, U.GREY], height=170)
                        st.caption("Value of the coins you hold now, over time, against what you paid.")
                    for line in news(s)[:3]: st.caption("• " + line)

def orders_view():
    pend = acct.get("orders", [])
    with box("🕒 Open orders", "oo", "Limit orders waiting for your price"):
        if not pend: st.caption("No pending limit orders. Choose **Limit** in the Buy or Sell window to wait for your price.")
        for o in list(pend):
            with st.container(border=True, key=f"card_ord_{o['id']}"):
                now_p = px(o["symbol"])
                st.markdown(f'<div class="gw-top"><div><div class="gw-sym">{U.esc(o["symbol"])}</div><div class="gw-sub">Placed {U.esc(o["ts"])} · {U.esc(U.qty_txt(o["qty"]))} coins</div></div>'
                            f'<div class="gw-right"><div class="gw-px">{U.esc(U.usd(o["limit"]))}</div><div class="gw-sub">limit · now {U.esc(U.usd(now_p))}</div></div></div>'
                            f'<div class="gw-chips">{U.side_chip(o["side"])}{U.chip("Waiting", "hold")}{U.hz_chip(o.get("horizon"))}</div>', unsafe_allow_html=True)
                if st.button("Cancel order", key=f"cancel_{o['id']}"): pend.remove(o); save(); st.rerun()
    with box("📜 Order history", "oh"):
        if not acct["trades"]: st.caption("Nothing traded yet."); return
        t = pd.DataFrame(acct["trades"]).iloc[::-1]
        show = pd.DataFrame({"Time": t.ts.str.replace("T", " "), "Coin": t.symbol, "Side": t.side, "Coins": t.qty, "Price": t.price, "Value": t.qty * t.price})
        st.dataframe(style_df(show, side="Side", fmt={"Coins": "{:,.6g}", "Price": "${:,.6g}", "Value": "${:,.2f}"}), hide_index=True, use_container_width=True)

def pnl_view():
    closed = E.ledger(acct["trades"])
    with box("📊 Summary", "ps"):
        st.markdown(U.order_box_html([
            ("Realised P&L (booked)", U.pnl_html(perf["realised"], perf["realised_pct"] if closed else None)),
            ("Unrealised P&L (open)", U.pnl_html(perf["unrealised"], perf["unrealised_pct"] if rows else None)),
            ("Total P&L", U.pnl_html(perf["total"], perf["total_pct"])),
            ("Net worth", U.esc(U.usd(perf["net"], 2))), ("Started with", U.esc(U.usd(acct["start_cash"], 2))),
            ("Winning trades", f"{perf['wins']} of {perf['closed']} ({perf['win_rate']:.0f}%)" if perf["closed"] else "–")]), unsafe_allow_html=True)
        st.caption("Realised % is on the cost of the coins you sold. Unrealised % is on what you still hold. Total % is on your starting cash. "
                   "Method: average buy price; the trading cost is already inside every fill price. Total P&L always equals Net worth minus Started with.")
    if not closed: st.info("No closed trades yet. Realised P&L appears here when you sell."); return
    with box("🏆 By coin", "pb", "Best and worst trades"):
        if perf["best"] and perf["worst"]:
            b, w = perf["best"], perf["worst"]
            st.markdown(f'<div class="gw-sub">Best trade: <b>{U.esc(b["symbol"])}</b> {U.pnl_html(b["pnl"], b["pct"])} · Worst trade: <b>{U.esc(w["symbol"])}</b> {U.pnl_html(w["pnl"], w["pct"])}</div>', unsafe_allow_html=True)
        cl = pd.DataFrame(closed); cl["cost"] = cl.buy * cl.qty
        by = cl.groupby("symbol").agg(Trades=("pnl", "size"), PnL=("pnl", "sum"), cost=("cost", "sum")).reset_index()
        by["PnL %"] = by.PnL / by.cost * 100
        st.dataframe(style_df(by.rename(columns={"symbol": "Coin", "PnL": "P&L $"})[["Coin", "Trades", "P&L $", "PnL %"]].sort_values("P&L $", ascending=False),
                              pnl=("P&L $", "PnL %"), fmt={"P&L $": "${:,.2f}", "PnL %": "{:+.2f}%"}), hide_index=True, use_container_width=True)
    with box("🧾 Every closed trade", "pc"):
        tr = pd.DataFrame({"Sold on": cl.ts.str.replace("T", " "), "Coin": cl.symbol, "Coins": cl.qty, "Buy price": cl.buy, "Sell price": cl.sell, "P&L $": cl.pnl, "P&L %": cl.pct}).iloc[::-1]
        st.dataframe(style_df(tr, pnl=("P&L $", "P&L %"), fmt={"Coins": "{:,.6g}", "Buy price": "${:,.6g}", "Sell price": "${:,.6g}", "P&L $": "${:,.2f}", "P&L %": "{:+.2f}%"}), hide_index=True, use_container_width=True)

def growth_view():
    if not acct["trades"]: st.info("Growth appears after your first trade."); return
    hist = E.history(acct["trades"], close_all, acct["start_cash"])
    if len(hist): hist.loc[pd.Timestamp(utc_today())] = perf["net"]       # today's live point
    with box("📈 Net worth", "gn"):
        rng = st.radio("Range", ["1W", "1M", "3M", "6M", "All"], horizontal=True, index=4, key="gr_rng")
        days = {"1W": 7, "1M": 31, "3M": 92, "6M": 183}.get(rng)
        if days and len(hist): hist = hist[hist.index >= hist.index[-1] - pd.Timedelta(days=days)]
        if len(hist) > 1:
            ch = hist.iloc[-1] - hist.iloc[0]
            st.markdown(f'<div class="gw-card"><div class="gw-k">Net worth · {U.esc(rng)}</div><div class="gw-big">{U.esc(U.usd(hist.iloc[-1], 2))}</div>'
                        f'{U.pnl_html(ch, ch / hist.iloc[0] * 100)}</div>', unsafe_allow_html=True)
            st.line_chart(hist.rename("Net worth"), color=U.UP if ch >= 0 else U.DOWN)
            st.caption("Cash plus holdings at each day's closing price; today's point is live.")
        else: st.caption("The daily chart appears after the first full day.")
    ls = st.session_state.get(f"live_series_{user}", [])
    if len(ls) > 1:
        with box("⚡ Live this session", "gl"):
            ser = pd.Series([v for _, v in ls], index=pd.to_datetime([t for t, _ in ls], unit="s", utc=True).tz_convert(IST).tz_localize(None))
            st.line_chart(ser.rename("Net worth"), color=U.UP if ser.iloc[-1] >= ser.iloc[0] else U.DOWN, height=180)
            d = ser.iloc[-1] - ser.iloc[0]
            st.caption(f"Since you opened the app: {U.usd(d, 2, True)} ({U.pct(d / ser.iloc[0] * 100)}). One point per refresh.")

def rec_cards(items, key, banner=None):
    """Cards for a parsed Gemini portfolio review: one per coin, with buttons to act."""
    if banner: st.caption(banner)
    held_ = get_pos()[0]
    for i, a in enumerate(items):
        s = alias.get(a["symbol"]) or a["symbol"]; price = price_of(s); dp = day_pct(s, price) if s in close_all.columns else float("nan")
        tone_ = {"HOLD": "hold", "BUY MORE": "buy", "BUY NEW": "buy", "SELL PART": "sell", "SELL ALL": "sell"}[a["action"]]
        with st.container(border=True, key=f"card_{key}_{i}"):
            st.markdown(f'<div class="gw-top"><div><div class="gw-sym">{U.esc(s)}</div><div class="gw-sub">{U.esc(a["confidence"] and a["confidence"] + " confidence")}</div></div>'
                        f'<div class="gw-right"><div class="gw-px">{U.esc(U.usd(price))}</div></div></div>'
                        f'<div class="gw-chips">{U.chip(a["action"], tone_)}{U.chip("You hold " + U.qty_txt(held_[s]["qty"]), "muted") if s in held_ else ""}</div>', unsafe_allow_html=True)
            if a["reason"]: st.write(a["reason"])
            if a["risk"]: st.caption("⚠️ Risk: " + a["risk"])
            if a["stop"] or a["target"]: st.caption(f"New stop-loss {U.usd(a['stop'])} · new target {U.usd(a['target'])}")
            b = st.columns(3)
            if price != price: st.info("No price found for this symbol, so it can't be traded here."); continue
            if a["action"] in ("SELL PART", "SELL ALL") and s in held_ and b[0].button(f"Sell {U.qty_txt(a['qty']) if a['action'] == 'SELL PART' and a['qty'] else 'all'}", key=f"sell_{key}_{i}", use_container_width=True):
                show_dialog("sell", s, None, price, dp, a["qty"] if a["action"] == "SELL PART" else None)
            if a["action"] in ("BUY MORE", "BUY NEW") and b[0].button("Buy", key=f"buy_{key}_{i}", type="primary", use_container_width=True):
                show_dialog("buy", s, None, price, dp, a["stop"] if a["stop"] and a["stop"] < price else None, a["target"] if a["target"] and a["target"] > price else None,
                            (a["qty"] * price) if a["qty"] else capital * 0.05)
            if s in held_ and ((a["stop"] and a["stop"] < price) or (a["target"] and a["target"] > price)) and b[1].button("Apply stop/target", key=f"lv_{key}_{i}", use_container_width=True):
                lv = acct.setdefault("levels", {}).setdefault(s, {})
                if a["stop"] and a["stop"] < price: lv["stop"] = a["stop"]
                if a["target"] and a["target"] > price: lv["target"] = a["target"]
                save(); flash(f"Updated stop-loss / target for {s}"); st.rerun()

def review_view():
    with box("① Instant check (no AI)", "r1", "Rules of thumb plus the model's view on each holding"):
        if not rows: st.caption("Buy something first. Your holdings will be checked here.")
        for r in sorted(rows, key=lambda r: ["SELL", "HOLD", "BUY"].index(r["signal"])):
            st.markdown(f'<div class="gw-card" style="border:1px solid #8a8fa340;padding:10px 14px;margin-bottom:6px"><div class="gw-top"><div><span class="gw-sym" style="font-size:1rem">{U.esc(r["symbol"])}</span> '
                        f'{U.action_chip(r["signal"])}<div class="gw-sub">{U.esc(r["reason"])}</div></div>'
                        f'<div class="gw-right">{U.pnl_html(r["pnl"], r["pnl_pct"])}<div class="gw-sub">{r["weight"]:.0f}% of holdings</div></div></div></div>', unsafe_allow_html=True)
        big = [r for r in rows if r["weight"] > 35 and len(rows) > 1]
        if big: st.warning("Concentration: " + ", ".join(f"{r['symbol']} is {r['weight']:.0f}%" for r in big) + " of your holdings. Consider spreading the risk.")
        st.caption("Rules of thumb from trend, stop-loss, target and (when the coin is in the model) its predicted month.")
    with box("② Ask Gemini: copy & paste (no key)", "r2", "Only your holdings are included"):
        st.caption("The prompt contains only your holdings. Gemini is told not to mention any other coin, market outlook or new ideas.")
        prompt = E.build_review_prompt(rows, perf, dt.date.today())
        copy_open_gemini(prompt)
        with st.expander("Show prompt text"): st.code(prompt, language=None)
        reply = st.text_area("Paste Gemini's reply here", height=140, key="rv_paste", placeholder="Paste the JSON reply from Gemini…")
        if st.button("✅ Show actions", type="primary", use_container_width=True, key="rv_show"):
            items = E.parse_review(reply)
            if items: acct["review"] = dict(ts=now_ist().strftime("%d %b %Y %H:%M"), items=items); save(); st.rerun()
            elif items is None:
                st.error("Couldn't find the JSON list in that reply. Ask Gemini: 'Reply again with only the JSON array', then paste it.")
                if reply.strip(): st.markdown("**Gemini's raw reply:**"); st.write(reply)
            else: st.warning("The reply had no usable actions.")
        rv = acct.get("review")
        if rv:
            mine = [a for a in rv["items"] if (alias.get(a["symbol"]) or a["symbol"]) in pos or a["symbol"] in pos]
            if len(mine) < len(rv["items"]): st.caption(f"{len(rv['items']) - len(mine)} suggestion(s) about coins you don't hold were hidden.")
            rec_cards(mine, "rv", f"Gemini review pasted {rv['ts']} · AI opinion, can be wrong, not financial advice")
            if st.button("Clear review", key="rv_clear"): acct.pop("review", None); save(); st.rerun()
    with box("③ Ask Gemini: API key", "r3", "One tap if you added a key in ⚙️ Settings"):
        if st.button("⚡ Review my portfolio now", use_container_width=True, key="rv_api"):
            if not gkey: st.error("Add your Gemini API key in ⚙️ Settings first (free at aistudio.google.com).")
            else:
                with st.spinner("Gemini is reviewing your portfolio…"):
                    try:
                        ans = G.ask(gkey, gmodel.strip(), [dict(role="user", text=prompt)], "", gsearch, system=E.REVIEW_SYSTEM)
                        items = E.parse_review(ans)
                        if items: acct["review"] = dict(ts=now_ist().strftime("%d %b %Y %H:%M"), items=items); save(); st.rerun()
                        else: st.session_state["rv_text"] = ans
                    except Exception as e: st.error(str(e))
        if st.session_state.get("rv_text"): st.markdown(st.session_state["rv_text"])

def account_view():
    with box("💾 Backup & restore", "a1", "A copy is also kept in this device's browser and restored automatically after an app update"):
        st.download_button("⬇️ Export account (backup)", json.dumps(acct), f"{user}_demo_account.json", use_container_width=True)
        up = st.file_uploader("Import a backup", type="json")
        if up and st.button("Restore backup"): acct.clear(); acct.update(json.load(up)); save(); st.rerun()
    with box("☁️ Automatic saving", "a0", "Phone every change · daily snapshot · 6 pm server backup · optional private Gist"):
        cs = _cloud_state()
        st.markdown(f"- **On this phone:** saved in the browser after every change, plus a dated snapshot each day (last 7 days kept).\n"
                    f"- **When you open the app:** the newer of the phone copy and the server copy wins, and the server is updated.\n"
                    f"- **6 pm India time:** the first time you open the app after 6 pm a dated backup is saved on the server (last saved: {acct.get('last_6pm') or 'never'}).\n"
                    f"- **Last server save:** {acct.get('saved_at', 'never')[:19].replace('T', ' ')} UTC.\n"
                    f"- **Durable cloud copy (private GitHub Gist):** " + (f"on · last upload {cs['at'] or 'not yet'}: {cs['msg']}" if GH_TOKEN else "off. Add `GITHUB_TOKEN` (a token with only the gist permission) in Streamlit > Settings > Secrets to turn it on."))
        b1, b2 = st.columns(2)
        if b1.button("⬆️ Upload to server now", key="up_now", use_container_width=True, disabled=not GH_TOKEN): cloud_push(force=True); flash("Uploading…"); st.rerun()
        if b2.button("⬇️ Restore from cloud copy", key="dn_now", use_container_width=True, disabled=not GH_TOKEN):
            try:
                d = CS.pull(GH_TOKEN, user)
                if d and _valid_acct(d): acct.clear(); acct.update({k: d[k] for k in _BK_KEYS if k in d}); save(); flash("Restored from the cloud copy"); st.rerun()
                else: st.warning("No cloud copy found yet.")
            except Exception as e: st.error(str(e))
        st.caption("A phone browser cannot run in the background, so nothing can upload at exactly 6 pm while the app is closed; the 6 pm backup happens the first time you open the app after 6 pm, and the phone copy syncs to the server on every open.")
    with box("⚠️ Reset account", "a2", "Starts again with fresh virtual cash"):
        new_cash = st.number_input("Reset with starting cash ($)", 100, 100000000, int(acct["start_cash"]), step=1000)
        if st.button("⚠️ Reset account"):
            keep = {k: acct[k] for k in ("theme", "blur", "fs", "hc", "anim", "settings") if k in acct}
            acct.clear(); acct.update(start_cash=float(new_cash), trades=[], **keep); save()
            st.session_state["clear_backup"] = True; st.session_state["cta_restore_done"] = True; st.rerun()
    st.caption("Demo only: virtual money, spot buying only (no leverage, no shorting). Each order is filled at the latest available price plus your trading-cost setting in Settings (fees + slippage). "
               "Limit orders and stop-loss / target sells are checked whenever the app is open: crypto trades around the clock, so a sharp move while the app is closed is not caught. "
               "On free cloud hosting, saved accounts may reset when the app restarts, so export a backup.")

def portfolio_section():
    """The whole Portfolio area. Reruns by itself every refresh interval (only this part of the page), so nothing else blinks or waits."""
    global PF_STAMP
    pf_live, PF_STAMP = pf_prices(tuple(s for s in get_pos()[0] if s in close_all.columns))
    live.update(pf_live)
    msgs = run_orders()
    if msgs:
        st.session_state.msgs = msgs; st.rerun(scope="app")          # a stop-loss / target / limit order fired: refresh everything
    refresh_state()
    p_h, p_o, p_p, p_g, p_r, p_a = st.tabs(["Holdings", "Orders", "P&L", "Growth", "Gemini", "Account"])
    with p_h: holdings_view()
    with p_o: orders_view()
    with p_p: pnl_view()
    with p_g: growth_view()
    with p_r: review_view()
    with p_a: account_view()

with t_pf:
    st.fragment(run_every=(pf_every if pf_auto else None))(portfolio_section)()

# ======================================================================
# Gemini AI (ideas)
# ======================================================================
with t_ai:
    ta, tb = st.tabs(["📋 Ideas: copy & paste (no key)", "⚡ Chat (API key)"])
    with ta:
        with box("① Build the prompt", "gp1", "Copy it, open Gemini, paste it there"):
            st.write("**1.** Build a prompt  **2.** Copy it and open Gemini  **3.** Paste Gemini's reply below to get recommendations here.")
            st.caption("The prompt tells Gemini to weigh market structure and ETF flows, derivatives, on-chain and project fundamentals, news, macro and regulation, and lets it pick **any** coin. "
                       "To review your own holdings use **Portfolio > Gemini**.")
            kind = st.radio("What do you want to ask?", E.IDEA_KINDS)
            own = st.text_input("Your question") if kind == E.PROMPT_KINDS[2] else ""
            notes = st.text_area("Optional: your own notes on news, regulation or events to include", acct.get("notes", ""), height=90,
                                 placeholder="e.g. Spot ETF inflows hit a record this week; a big token unlock for X next Tuesday; new stablecoin rules")
            if notes != acct.get("notes", ""): acct["notes"] = notes; save()
            prompt = E.build_prompt(kind, own, build_context(), dt.date.today())
            copy_open_gemini(prompt)
            with st.expander("Show prompt text"): st.code(prompt, language=None)
        with box("② Paste Gemini's reply", "gp2", "Gemini's answer becomes buy cards below"):
            reply = st.text_area("Paste Gemini's reply here", height=160, key="greply", placeholder="Paste the JSON reply from Gemini…")
            if st.button("✅ Show recommendations", type="primary", use_container_width=True):
                items = E.parse_recs(reply)
                if items: acct["recs"] = dict(ts=now_ist().strftime("%d %b %Y %H:%M"), items=items); save(); st.rerun()
                elif items is None:
                    st.error("Couldn't find the JSON list in that reply. Ask Gemini: 'Reply again with only the JSON array', then paste it.")
                    if reply.strip(): st.markdown("**Gemini's raw reply:**"); st.write(reply)
                else: st.warning("The reply had no usable recommendations.")
        recs = acct.get("recs")
        if recs:
            st.subheader("Gemini's recommendations")
            st.caption(f"Pasted {recs['ts']} · AI opinion, can be wrong, not financial advice")
            cols = st.columns(2)
            for i, r in enumerate(recs["items"]):
                sym = r["symbol"]; al = acct.setdefault("alias", {}); use = al.get(sym) or sym
                if use not in close_all.columns and sym not in al:
                    hits = search_coins(r.get("name") or sym); al[sym] = hits[0]["symbol"] if hits else ""; save()
                    if al[sym]: st.rerun()
                last = px(use); dp = day_pct(use, last)
                act = str(r["action"]).lower(); sig = "SELL" if "avoid" in act or "sell" in act else "HOLD" if "wait" in act or "hold" in act else "BUY"
                with cols[i % 2], st.container(border=True, key=f"card_rec_{i}"):
                    tags = U.hz_chip(HZ_TAG) + U.action_chip(sig) + U.chips([(f"{r['confidence']} confidence", "info")] if r["confidence"] else [])
                    tags += U.chips([(f"{r['liquidity']} liquidity", "muted")] if r.get("liquidity") else [])
                    if r.get("agrees"): tags += U.chips([(f"Agrees with app model: {r['agrees']}", "muted")])
                    if use in tbl.index: tags += U.chips([(f"App model: {tbl.loc[use, 'p_up'] * 100:.0f}% chance up", "info")])
                    st.markdown(U.stock_head_html(use, r.get("name"), last, dp, tags), unsafe_allow_html=True)
                    if last == last: st.markdown(U.levels_html(last, r["entry"], r["stop"], r["target"]), unsafe_allow_html=True)
                    else: st.caption(f"Entry {U.usd(r['entry'])} · Stop-loss {U.usd(r['stop'])} · Target {U.usd(r['target'])}")
                    if r["reason"]: st.write(r["reason"])
                    for lab, k in (("📈 Market & derivatives", "market_derivs"), ("⛓ On-chain & fundamentals", "onchain_fundamentals"), ("📰 News & regulation", "news_regulation"), ("🏛 Government policy", "policy_view")):
                        if r.get(k): st.caption(f"{lab}: {r[k]}")
                    if r["risk"]: st.caption(f"⚠️ Risk: {r['risk']}")
                    if last == last:
                        if r["entry"] and abs(r["entry"] / last - 1) > 0.10:
                            st.warning(f"Gemini's entry differs from the live price by {abs(r['entry'] / last - 1) * 100:.0f}%. It may have used old prices.")
                        if r["stop"] and r["stop"] >= last: st.warning("Its stop-loss is above the live price, so it is not usable as given.")
                        if r["target"] and r["target"] <= last: st.warning("Its target is below the live price.")
                        stop_ok = r["stop"] if r["stop"] and r["stop"] < last else None
                        tgt_ok = r["target"] if r["target"] and r["target"] > last else None
                        amt = E.qty_for(last, stop_ok, capital, risk, maxpos) * last
                        c1, c2 = st.columns(2)
                        if c1.button("Buy", key=f"gr_{i}_{use}", type="primary", use_container_width=True): show_dialog("buy", use, r.get("name"), last, dp, stop_ok, tgt_ok, max(amt, 10.0))
                        watch_button(c2, use, f"rec_{i}")
                    else: st.info("No price found for this symbol on Yahoo Finance. Check it in Predict > Find a coin (try the coin's name).")
            if st.button("Clear recommendations"): acct.pop("recs", None); save(); st.rerun()
    with tb:
        with box("💬 Ask Gemini inside the app", "chat"):
            st.write("Ask Gemini directly inside the app. It also receives this app's model predictions and your demo holdings as context, and may suggest any coin.")
            chat = st.session_state.setdefault("gchat", [])
            qs = {"Best coins for the next 30 days?": "Which coins look best for the next 30 days? Use market structure, ETF flows, derivatives, on-chain and project fundamentals, news, macro and regulation. Give entry, stop-loss, target and risks. There is no minimum gain.",
                  "Is the market risky now?": "How risky is the crypto market right now for new buys? Cover Bitcoin trend, funding and leverage, ETF flows, macro and upcoming events.",
                  "Review my portfolio": "Review my demo holdings. What are the concentration and risk issues, and what would you change?"}
            cols_ = st.columns(len(qs)); quick = None
            for c_, (lab, txt) in zip(cols_, qs.items()):
                if c_.button(lab, use_container_width=True): quick = txt
            for h in chat: st.chat_message("user" if h["role"] == "user" else "assistant").write(h["text"])
            q = st.chat_input("Ask about any coin, strategy or your portfolio…") or quick
            if q:
                if not gkey: st.error("Add your Gemini API key in ⚙️ Settings first (free at aistudio.google.com).")
                else:
                    chat.append(dict(role="user", text=q)); st.chat_message("user").write(q)
                    with st.chat_message("assistant"), st.spinner("Gemini is thinking…"):
                        try:
                            ans = G.ask(gkey, gmodel.strip(), chat, build_context(), gsearch, system=E.SYSTEM_RULES)
                            chat.append(dict(role="model", text=ans)); st.write(ans)
                        except Exception as e:
                            chat.pop(); st.error(str(e))
            if chat and st.button("Clear chat"): chat.clear(); st.rerun()
            st.caption("Gemini's answers are AI-generated opinions, can be wrong, and are not financial advice. "
                       "Your question, the model's predictions and your demo holdings are sent to Google when you ask. Your API key may be billed by Google if you exceed the free tier.")

# ======================================================================
# Tools
# ======================================================================
with t_tools:
    k1, kn, ke, k2, k3 = st.tabs(["🎯 Model accuracy", "🌐 News & policy", "📅 Events", "🌍 Market context", "ℹ️ How it works"])
    with k1:
        with box("🎯 How good are the predictions? Tested on the past, not claimed", "acc", f"Walk-forward test on this coin list and the '{profile}' profile"):
            if not VS: st.info("Not enough history to test the model on this coin list.")
            else:
                st.markdown(f"**Reliability: {VS['verdict']}.** {VS['verdict_text']}")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Rank correlation (IC)", f"{VS['ic']:+.3f}", help="How well the model's ranking matches what coins really did next month. 0 = no skill, 0.05 = decent, 0.10+ = strong for crypto.")
                c2.metric(f"Top {VS['topn']} picks: avg month", f"{VS['top_ret'] * 100:+.1f}%", f"{(VS['top_ret'] - VS['market_ret']) * 100:+.1f}% vs average coin")
                c3.metric(f"Top {VS['topn']} picks that rose", f"{VS['top_win'] * 100:.0f}%", f"{(VS['top_win'] - VS['base_up']) * 100:+.0f} pts vs all coins")
                c4.metric("Months with positive IC", f"{VS['ic_pos'] * 100:.0f}%", f"of {VS['n_folds']} tested months", delta_color="off")
                f = VAL["folds"].set_index("date")
                st.line_chart(f[["cum_top", "cum_market", "cum_naive"]].rename(columns={"cum_top": "Model's top picks", "cum_market": "Average coin", "cum_naive": "Last month's biggest gainers"}), height=240)
                st.caption("Growth of $1 if, each month, you split money equally across the model's top picks (or the average coin, or last month's top gainers), ignoring trading costs.")
                st.markdown("**Are the probabilities honest?** When the model says X% chance of rising, how often did the coin really rise?")
                cb = VAL["calib"].rename(columns={"bin": "Model said", "n": "Predictions", "predicted": "Avg predicted", "actual": "Actually rose"})
                st.dataframe(cb.style.format({"Avg predicted": "{:.0%}", "Actually rose": "{:.0%}"}) if hasattr(cb, "style") else cb, hide_index=True, use_container_width=True)
                st.caption(f"Probabilities are already shrunk by {VS['shrink']:.2f} toward the base rate because the raw model was over-confident in testing.")
                with st.expander("Month-by-month results"):
                    st.dataframe(f.reset_index()[["date", "coins", "ic", "top_ret", "top_win", "market_ret"]].rename(columns={"coins": "Coins", "ic": "IC", "top_ret": "Top picks", "top_win": "Picks that rose", "market_ret": "Average coin"})
                                 .style.format({"IC": "{:+.2f}", "Top picks": "{:+.1%}", "Picks that rose": "{:.0%}", "Average coin": "{:+.1%}"}), hide_index=True, use_container_width=True)
                st.warning("Read these results with care: only a handful of months are tested; the coin list is today's survivors (coins that collapsed or were delisted are missing, which flatters every crypto backtest); "
                           "and trading costs are ignored. Past accuracy does not guarantee future accuracy.")
    with kn:
        with box("🌐 International news & government policy", "news", "Last 7 days, headlines from around the world (Google News)"):
            tone = {"Risk-off": st.warning, "Supportive": st.success}.get(RISK["label"], st.info)
            tone(f"**Policy/news tone: {RISK['label']}** (score {RISK['score']:+.2f} from {RISK['n']} headlines). " + ("When this reads Risk-off, every coin gets a mild confidence cut. " if RISK["label"] == "Risk-off" else "")
                 + "This is a crude keyword score of headline tone: it flags mood, it does not understand news. It is an overlay, not part of the tested model.")
            if RISK["n"] == 0: st.caption("No headlines could be fetched right now (the news source may be unreachable from this server).")
            for g, items in NEWS.items():
                if items:
                    st.markdown(f"**{g}**" + (f" · tone {RISK['by_group'][g]:+.2f}" if g in RISK["by_group"] else ""))
                    for it in items[:5]:
                        sc = E.score_headline(it["title"]); st.caption(("🔴 " if sc < 0 else "🟢 " if sc > 0 else "⚪ ") + it["title"])
            st.caption("Then use 📅 Events to add dated decisions (rate meetings, ETF rulings, laws, unlocks) and the Gemini tab for deeper policy research.")
    with ke:
        with box("📅 Policy & event calendar", "events", "Dated events lower confidence for the coins they touch"):
            evs = acct.setdefault("events", [])
            st.caption("Events within the next 30 days shrink the predicted chance of rising toward 50%, widen the range and flag the coin. This is a judgment layer: it can't be backtested.")
            c1, c2 = st.columns(2)
            e_date = c1.date_input("Date", dt.date.today() + dt.timedelta(days=7), key="ev_date"); e_title = c2.text_input("What is happening?", key="ev_title", placeholder="e.g. SEC ETF ruling, Fed meeting, SOL token unlock")
            c3, c4 = st.columns(2)
            e_kind = c3.selectbox("Type", E.EVENT_KINDS, key="ev_kind"); e_coins = c4.text_input("Coins affected (ALL or e.g. SOL, ETH)", "ALL", key="ev_coins")
            e_sev = _pick("How big?", list(E.SEVERITY), "Medium", "ev_sev"); e_dir = _pick("Likely effect", E.DIRECTIONS, "Uncertain", "ev_dir")
            if st.button("➕ Add event", type="primary", key="ev_add") and e_title.strip() and not any(e["date"] == f"{e_date:%Y-%m-%d}" and e["title"] == e_title.strip()[:90] for e in evs):
                evs.append(dict(date=f"{e_date:%Y-%m-%d}", title=e_title.strip()[:90], kind=e_kind, coins=E.clean_symbols(re.split(r"[\s,;]+", e_coins)) or ["ALL"] if e_coins.strip().upper() != "ALL" else ["ALL"],
                                severity=e_sev, direction=e_dir, why="")); save(); flash("📅 Event added: predictions updated"); st.rerun()
            for i, e in enumerate(sorted(evs, key=lambda e: e["date"])):
                cc = st.columns([6, 1]); cc[0].markdown(f"**{e['date']}** · {U.esc(e['title'])} · {e['kind']} · {e['severity']} · {e['direction']} · {', '.join(e['coins'])}" + (f"<br><span class='gw-sub'>{U.esc(e.get('why', ''))}</span>" if e.get("why") else ""), unsafe_allow_html=True)
                if cc[1].button("✖", key=f"ev_del_{i}_{e['date']}"): evs.remove(e); save(); st.rerun()
            if not evs: st.caption("No events yet.")
        with box("🤖 Let Gemini find upcoming events", "events_ai", f"Uses your country: {country}"):
            ep = E.build_events_prompt(country, dt.date.today(), list(tbl.head(8).index)); copy_open_gemini(ep)
            with st.expander("Show prompt text"): st.code(ep, language=None)
            rp = st.text_area("Paste Gemini's reply here", height=120, key="ev_paste", placeholder="Paste the JSON reply from Gemini…")
            if st.button("✅ Add these events", key="ev_parse"):
                new = E.parse_events(rp)
                if new:
                    have = {(e["date"], e["title"]) for e in evs}; add = [e for e in new if (e["date"], e["title"]) not in have]
                    evs.extend(add); save(); flash(f"📅 {len(add)} event(s) added"); st.rerun()
                else: st.error("Couldn't read any events. Ask Gemini: 'Reply again with only the JSON array', then paste it.")
    with k2:
        with box("🌍 Market context", "mkt"):
            fg = fear_greed()
            if fg: st.markdown(f'<div class="gw-card" style="border:1px solid #8a8fa340"><div class="gw-k">Crypto Fear &amp; Greed Index</div><div class="gw-px">{fg[0]} / 100 · {U.esc(fg[1])}</div></div>', unsafe_allow_html=True)
            if snap:
                cols = st.columns(3)
                for i, (k, (v, d)) in enumerate(snap.items()):
                    cols[i % 3].markdown(f'<div class="gw-card" style="border:1px solid #8a8fa340"><div class="gw-k">{U.esc(k)}</div><div class="gw-px">{U.esc(f"{v:,.2f}")}</div>{U.pct_html(d, 1)}</div>', unsafe_allow_html=True)
            else: st.info("Market data is not available right now. Try again in a minute.")
            st.caption(f"{REGIME_TXT} Data as of {ASOF:%d %b %Y} · {len(close.columns)} coins loaded" + (f" · {len(base) - len(close.columns)} symbols had no data (renamed, delisted or not on Yahoo)" if len(base) > len(close.columns) else ""))
    with k3:
        with box("ℹ️ How it works", "how"):
            st.markdown(f"""
- **Goal:** rank coins by their predicted gain over the **next {E.HORIZON} days**. There is no minimum gain. A coin at +4% can rank above one at +30% if the model is more sure of it.
- **Model:** one gradient-boosting model trained on every coin's history (5 years where available). Inputs: 3 to 90-day returns, volatility, RSI, distance from moving averages and highs, volume surge and size, coin age, strength versus the whole market, and market-wide trend and breadth.
- **Outputs per coin:** a median 30-day move, a 10th to 90th percentile range, the chance the coin is up, and the chance it beats the typical coin. The ranking score blends the median move and the chance of beating the market.
- **Global policy and news:** the model also tests global-backdrop inputs (dollar index, US 10-year yield, VIX, Nasdaq, gold, which move with Fed and world policy) and keeps them only if they help out-of-sample. Worldwide headlines on regulation, central banks, ETFs, security, India policy and geopolitics are scanned for tone, and your dated events (Tools > Events) lower confidence for the coins they touch. These two are judgment overlays and cannot be backtested.
- **Gemini prompts** carry your country's rules, the headline tone, your event calendar, the model's picks and a required checklist (search last 7 days of news, check events, apply local rules, compare with the model, downgrade risky picks).
- **Trade plan:** stop-loss 2.5 average daily moves below entry (never closer than 8% or further than 30%), target at the model's optimistic case, size so that a stop-out loses only your chosen % of capital.
- **Accuracy is measured, not claimed:** every month of history is predicted using only data that was known at the time, then compared with what happened (Tools > Model accuracy). If the model has no real edge on your coin list, the app tells you ("Weak").
- **Demo trading:** virtual dollars, spot buys only, with fees and slippage built into each fill. Stop-loss, target and limit orders are checked whenever the app is open.
- **Limits:** nobody can predict crypto prices precisely. Single events (hacks, regulation, listings, a big holder selling) can move a coin 30% in a day. Never trade money you cannot afford to lose. In India, check the current tax rules on crypto gains before trading real money. Not financial advice.""")

browser_backup()
