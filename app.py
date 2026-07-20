"""
Stock Watchlist — US & India dashboards + alert rule builder.

Run locally:   streamlit run app.py
Deploy free:   push this repo to GitHub, then deploy on
               https://share.streamlit.io (Streamlit Community Cloud).

Tabs:
  1. US Watchlist     - NYSE/Nasdaq tickers, benchmarked vs SPY (or your configured benchmark).
  2. India Watchlist  - NSE (.NS) / BSE (.BO) tickers, benchmarked vs Nifty 500 (or your configured benchmark).
  3. Alert Rules      - build watchlist-wide or per-ticker rules across BOTH
                        markets combined. Rules are saved to alerts_config.json.
                        NOTE: this app does not send alerts on a schedule by
                        itself (Streamlit Community Cloud has no background
                        cron) -- the actual daily check is alert_check.py,
                        run on a schedule elsewhere (e.g. via Cowork's
                        scheduler, or GitHub Actions / any cron host). This
                        tab is for building rules + previewing what would
                        fire right now, and sending a test Discord message.

Each market tab also has a custom filter builder: compare any metric against
another metric or a fixed value (e.g. "10 WEMA > 40 WEMA", "200 DEMA >= 200"),
combined with AND logic alongside the preset Above/Below/range filters.

Sidebar "Settings" opens a dialog where every calculation parameter (EMA
periods, RSI period, RS lookbacks, VStop length/factor, benchmarks) can be
edited -- changes are saved to settings.json and take effect on next refresh
-- plus a "Login (optional)" section to set/change/disable the username and
password gate directly from the UI (saves to a local auth_config.json).

For a Streamlit Cloud deployment, set AUTH_USERNAME / AUTH_PASSWORD as
Streamlit secrets instead (Settings dialog will say so if a secret is
already active) -- secrets persist across redeploys, a local file doesn't.
If no login is configured anywhere, the app is open.
"""

import uuid
from datetime import date, datetime

import pandas as pd
import streamlit as st

from stock_data import (
    load_watchlists, save_watchlist, fetch_all_markets, validate_ticker, tradingview_url,
    load_settings, save_settings, DEFAULT_SETTINGS, get_benchmarks, get_filterable_metrics,
    MARKETS,
)
from alerts import load_rules, save_rules, preview_rules, build_metrics, DISCORD_CONFIG_FILE, send_discord
from filters import get_market_filters, save_market_filters, apply_filters, describe_filter
import json
import os

st.set_page_config(page_title="Stock Watchlist", layout="wide")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_CONFIG_FILE = os.path.join(SCRIPT_DIR, "auth_config.json")

# ---------- auth gate ----------


def get_auth_credentials():
    """Returns (username, password) if login is configured, else (None, None)
    meaning the app is open (no login required) -- the default for local dev."""
    try:
        if "AUTH_USERNAME" in st.secrets and "AUTH_PASSWORD" in st.secrets:
            return st.secrets["AUTH_USERNAME"], st.secrets["AUTH_PASSWORD"]
    except Exception:
        pass
    if os.path.exists(AUTH_CONFIG_FILE):
        try:
            with open(AUTH_CONFIG_FILE) as f:
                cfg = json.load(f)
            u, p = cfg.get("username"), cfg.get("password")
            if u and p:
                return u, p
        except Exception:
            pass
    return None, None


def require_login():
    username, password = get_auth_credentials()
    if not username or not password:
        return  # no credentials configured anywhere -> open access
    if st.session_state.get("authenticated"):
        return

    st.title("Stock Watchlist")
    st.subheader("Sign in")
    with st.form("login_form"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if u == username and p == password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect username or password.")
    st.stop()


require_login()

# ---------- helpers ----------


@st.cache_data(show_spinner="Fetching latest prices...")
def cached_fetch_all(_refresh_token, watchlists_json):
    watchlists = json.loads(watchlists_json)
    combined, as_of, per_market = fetch_all_markets(watchlists)
    return as_of, per_market


def get_discord_webhook():
    try:
        if "DISCORD_WEBHOOK_URL" in st.secrets:
            return st.secrets["DISCORD_WEBHOOK_URL"]
    except Exception:
        pass
    if os.path.exists(DISCORD_CONFIG_FILE):
        try:
            with open(DISCORD_CONFIG_FILE) as f:
                return json.load(f).get("webhook_url")
        except Exception:
            return None
    return None


def save_discord_webhook_local(url):
    with open(DISCORD_CONFIG_FILE, "w") as f:
        json.dump({"webhook_url": url}, f, indent=2)


def save_auth_credentials_local(username, password):
    with open(AUTH_CONFIG_FILE, "w") as f:
        json.dump({"username": username, "password": password}, f, indent=2)


def clear_auth_credentials_local():
    if os.path.exists(AUTH_CONFIG_FILE):
        os.remove(AUTH_CONFIG_FILE)


def ema_col_labels(settings):
    """Column-header labels for the 6 EMA slots, reflecting the currently
    configured periods (e.g. {'w_fast': '10 WEMA', ...})."""
    wf, wm, ws = settings["ema_weekly"]
    df_, dm, ds = settings["ema_daily"]
    return {
        "w_fast": f"{wf} WEMA", "w_mid": f"{wm} WEMA", "w_slow": f"{ws} WEMA",
        "d_fast": f"{df_} DEMA", "d_mid": f"{dm} DEMA", "d_slow": f"{ds} DEMA",
    }


def style_row(row, ema_labels):
    styles = [""] * len(row)
    last = row["Last"]
    ema_cols = set(ema_labels.values())
    for i, col in enumerate(row.index):
        val = row[col]
        if col in ema_cols and pd.notna(val):
            styles[i] = "color:#c0392b;font-weight:600" if last < val else "color:#1e8449;font-weight:600"
        elif col in ("RSI-D", "RSI-W", "RSI-M") and pd.notna(val):
            if val <= 30:
                styles[i] = "color:#1e8449;font-weight:600"
            elif val >= 70:
                styles[i] = "color:#c0392b;font-weight:600"
        elif col in ("RS-D", "RS-W", "RS-M") and pd.notna(val):
            styles[i] = "color:#1e8449;font-weight:600" if val > 0 else ("color:#c0392b;font-weight:600" if val < 0 else "")
        elif col == "VStop Dir" and val in ("Up", "Down"):
            styles[i] = "color:#1e8449;font-weight:600" if val == "Up" else "color:#c0392b;font-weight:600"
        elif col == "Data Thru" and isinstance(val, str) and val != "—":
            try:
                age_days = (date.today() - datetime.strptime(val, "%Y-%m-%d").date()).days
                if age_days >= 3:
                    styles[i] = "color:#c0392b;font-weight:600"
            except ValueError:
                pass
    return styles


def numeric_cols(ema_labels):
    return ["Last", ema_labels["w_fast"], ema_labels["w_mid"], ema_labels["w_slow"],
            ema_labels["d_fast"], ema_labels["d_mid"], ema_labels["d_slow"],
            "RSI-D", "RSI-W", "RSI-M", "RS-D", "RS-W", "RS-M", "VStop-W"]


LINK_COLUMN_CONFIG = {
    "Ticker": st.column_config.LinkColumn(
        "Ticker", display_text=r"https://www\.tradingview\.com/chart/\?symbol=(.*)",
    ),
}


# ---------- Settings dialog ----------


@st.dialog("Settings")
def settings_dialog():
    settings = load_settings()
    st.caption(
        "Every parameter used to calculate metrics in this app, editable here. "
        "Save applies immediately on your next Refresh Data."
    )

    st.markdown("**Weekly EMA periods** (fast / medium / slow)")
    w1, w2, w3 = st.columns(3)
    ema_w_fast = w1.number_input("1. Weekly EMA fast", min_value=1, step=1, value=int(settings["ema_weekly"][0]), key="set_ema_w_fast")
    ema_w_mid = w2.number_input("2. Weekly EMA medium", min_value=1, step=1, value=int(settings["ema_weekly"][1]), key="set_ema_w_mid")
    ema_w_slow = w3.number_input("3. Weekly EMA slow", min_value=1, step=1, value=int(settings["ema_weekly"][2]), key="set_ema_w_slow")

    st.markdown("**Daily EMA periods** (fast / medium / slow)")
    d1, d2, d3 = st.columns(3)
    ema_d_fast = d1.number_input("4. Daily EMA fast", min_value=1, step=1, value=int(settings["ema_daily"][0]), key="set_ema_d_fast")
    ema_d_mid = d2.number_input("5. Daily EMA medium", min_value=1, step=1, value=int(settings["ema_daily"][1]), key="set_ema_d_mid")
    ema_d_slow = d3.number_input("6. Daily EMA slow", min_value=1, step=1, value=int(settings["ema_daily"][2]), key="set_ema_d_slow")

    st.markdown("**RSI**")
    rsi_period = st.number_input("7. RSI period", min_value=2, step=1, value=int(settings["rsi_period"]), key="set_rsi_period")

    st.markdown("**Mansfield RS lookback (bars)**")
    r1, r2, r3 = st.columns(3)
    rs_daily = r1.number_input("8. RS lookback — daily", min_value=1, step=1, value=int(settings["rs_lookback_daily"]), key="set_rs_daily")
    rs_weekly = r2.number_input("9. RS lookback — weekly", min_value=1, step=1, value=int(settings["rs_lookback_weekly"]), key="set_rs_weekly")
    rs_monthly = r3.number_input("10. RS lookback — monthly", min_value=1, step=1, value=int(settings["rs_lookback_monthly"]), key="set_rs_monthly")

    st.markdown("**Weekly VStop (Volatility Stop)**")
    v1, v2 = st.columns(2)
    vstop_length = v1.number_input("11. VStop length", min_value=2, step=1, value=int(settings["vstop_length"]), key="set_vstop_length")
    vstop_factor = v2.number_input("12. VStop ATR factor", min_value=0.1, step=0.1, format="%.1f", value=float(settings["vstop_factor"]), key="set_vstop_factor")

    st.markdown("**Benchmarks**")
    b1, b2 = st.columns(2)
    benchmark_us = b1.text_input("13. US benchmark ticker", value=settings["benchmark_us"], key="set_bench_us")
    benchmark_india = b2.text_input("14. India benchmark ticker", value=settings["benchmark_india"], key="set_bench_india")

    st.divider()
    c1, c2 = st.columns(2)
    if c1.button("Save", type="primary", width="stretch"):
        new_weekly = [int(ema_w_fast), int(ema_w_mid), int(ema_w_slow)]
        new_daily = [int(ema_d_fast), int(ema_d_mid), int(ema_d_slow)]
        if not (new_weekly[0] < new_weekly[1] < new_weekly[2]):
            st.error("Weekly EMA periods must be increasing: fast < medium < slow.")
        elif not (new_daily[0] < new_daily[1] < new_daily[2]):
            st.error("Daily EMA periods must be increasing: fast < medium < slow.")
        elif not benchmark_us.strip() or not benchmark_india.strip():
            st.error("Benchmark tickers can't be empty.")
        else:
            save_settings({
                "ema_weekly": new_weekly,
                "ema_daily": new_daily,
                "rsi_period": int(rsi_period),
                "rs_lookback_daily": int(rs_daily),
                "rs_lookback_weekly": int(rs_weekly),
                "rs_lookback_monthly": int(rs_monthly),
                "vstop_length": int(vstop_length),
                "vstop_factor": float(vstop_factor),
                "benchmark_us": benchmark_us.strip().upper(),
                "benchmark_india": benchmark_india.strip(),
            })
            st.session_state.refresh_token += 1
            st.success("Settings saved.")
            st.rerun()
    if c2.button("Reset to defaults", width="stretch"):
        save_settings(dict(DEFAULT_SETTINGS))
        st.session_state.refresh_token += 1
        st.success("Reset to defaults.")
        st.rerun()

    st.divider()
    st.markdown("**Login (optional)**")
    cloud_secret_active = False
    try:
        cloud_secret_active = "AUTH_USERNAME" in st.secrets and "AUTH_PASSWORD" in st.secrets
    except Exception:
        pass

    if cloud_secret_active:
        st.caption(
            "A login is already configured via Streamlit secrets (AUTH_USERNAME/AUTH_PASSWORD) — "
            "that takes priority over anything set here. To change it, edit the secret in your "
            "Streamlit Cloud app's Settings → Secrets."
        )
    else:
        current_user, _ = get_auth_credentials()
        if current_user:
            st.caption(f"Login is currently required. Signed-in username: **{current_user}**.")
        else:
            st.caption(
                "No login is required yet — anyone with the app URL can use it. Set a username "
                "and password below to require sign-in."
            )
        l1, l2 = st.columns(2)
        new_username = l1.text_input("Username", value=current_user or "", key="set_auth_username")
        new_password = l2.text_input("Password", type="password", key="set_auth_password")

        lc1, lc2 = st.columns(2)
        if lc1.button("Save login", width="stretch"):
            if not new_username.strip() or not new_password:
                st.error("Enter both a username and a password.")
            else:
                save_auth_credentials_local(new_username.strip(), new_password)
                st.success("Login saved. It applies on your next visit (local file, not committed to git).")
        if current_user and lc2.button("Disable login", width="stretch"):
            clear_auth_credentials_local()
            st.session_state.authenticated = False
            st.success("Login disabled — app is open again.")
            st.rerun()

        st.caption(
            "This saves to a local `auth_config.json` file (gitignored). For an app deployed on "
            "Streamlit Community Cloud, prefer setting AUTH_USERNAME/AUTH_PASSWORD as Streamlit "
            "secrets instead — local file changes there don't survive a redeploy."
        )


def render_watchlist_editor(market, watchlists):
    tickers = watchlists.get(market, [])
    st.caption(f"{len(tickers)} tickers")

    if tickers:
        per_row = 6
        for i in range(0, len(tickers), per_row):
            row_tickers = tickers[i:i + per_row]
            cols = st.columns(per_row)
            for col, t in zip(cols, row_tickers):
                if col.button(f"{t}  ✕", key=f"rm_{market}_{t}"):
                    new_list = [x for x in tickers if x != t]
                    save_watchlist(market, new_list)
                    st.session_state.refresh_token += 1
                    st.rerun()
    else:
        st.write("No tickers yet — add one below.")

    ac1, ac2 = st.columns([3, 1])
    placeholder = "e.g. AAPL" if market == "US" else "e.g. RELIANCE.NS"
    new_ticker = ac1.text_input("Add ticker", key=f"add_{market}", placeholder=placeholder, label_visibility="collapsed")
    if ac2.button("Add", key=f"addbtn_{market}"):
        t = new_ticker.strip().upper()
        if not t:
            pass
        elif t in tickers:
            st.warning(f"{t} is already in the watchlist.")
        else:
            with st.spinner(f"Checking {t} on Yahoo Finance..."):
                valid = validate_ticker(t)
            if not valid:
                st.error(
                    f"'{t}' doesn't return any price data from Yahoo Finance — check the "
                    f"symbol and exchange suffix (India tickers need .NS or .BO) and try again."
                )
            else:
                save_watchlist(market, tickers + [t])
                st.session_state.refresh_token += 1
                st.rerun()


def render_custom_filter_builder(market, filterable_metrics):
    st.markdown("**Custom filters** — compare any metric to another metric or a fixed value")
    metric_names = list(filterable_metrics.keys())
    metric_labels = {v: k for k, v in filterable_metrics.items()}

    fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1, 1.5, 2, 1])
    metric_a_label = fc1.selectbox("Metric", metric_names, key=f"cf_a_{market}")
    operator_choice = fc2.selectbox("Op", [">", "<", ">=", "<=", "=="], key=f"cf_op_{market}")
    compare_type = fc3.radio("Compare to", ["Metric", "Fixed value"], key=f"cf_ctype_{market}", horizontal=True)

    if compare_type == "Metric":
        metric_b_label = fc4.selectbox("Metric B", metric_names, key=f"cf_b_{market}")
        value = None
    else:
        value = fc4.number_input("Value", value=0.0, step=0.1, format="%.1f", key=f"cf_val_{market}")
        metric_b_label = None

    if fc5.button("Add filter", key=f"cf_add_{market}"):
        new_filter = {
            "id": uuid.uuid4().hex[:8],
            "metric_a": filterable_metrics[metric_a_label],
            "operator": operator_choice,
            "compare_type": "metric" if compare_type == "Metric" else "value",
        }
        if compare_type == "Metric":
            new_filter["metric_b"] = filterable_metrics[metric_b_label]
        else:
            new_filter["value"] = value
        current = get_market_filters(market)
        current.append(new_filter)
        save_market_filters(market, current)
        st.rerun()

    active_filters = get_market_filters(market)
    if active_filters:
        st.write("Active custom filters:")
        for filt in active_filters:
            cc1, cc2 = st.columns([5, 1])
            cc1.write(describe_filter(filt, metric_labels))
            if cc2.button("Remove", key=f"cf_rm_{market}_{filt['id']}"):
                remaining = [f for f in active_filters if f["id"] != filt["id"]]
                save_market_filters(market, remaining)
                st.rerun()

    return active_filters


def render_market_tab(market, results, settings):
    benchmarks = get_benchmarks(settings)
    bench = benchmarks[market]
    labels = ema_col_labels(settings)
    filterable_metrics = get_filterable_metrics(settings)

    st.caption(
        f"{labels['w_fast']}/{labels['w_mid']}/{labels['w_slow']} · "
        f"{labels['d_fast']}/{labels['d_mid']}/{labels['d_slow']} · "
        f"RSI({settings['rsi_period']}) D/W/M · Mansfield RS vs {bench} "
        f"(D lookback={settings['rs_lookback_daily']}, W lookback={settings['rs_lookback_weekly']}, "
        f"M lookback={settings['rs_lookback_monthly']})"
    )

    if results:
        starts = [r["data_start"] for r in results if r.get("data_start")]
        ends = [r["data_end"] for r in results if r.get("data_end")]
        stale = [r["ticker"] for r in results if r.get("data_end_age_days", 0) >= 3]
        if starts and ends:
            range_msg = f"Price history used: **{min(starts)} → {max(ends)}**"
            if len(set(ends)) > 1:
                range_msg += f" (latest close date varies by ticker, oldest is {min(ends)})"
            st.caption(range_msg)
            if stale:
                st.caption(
                    f"⚠️ {len(stale)} ticker(s) haven't updated in 3+ days (likely a data-provider lag, "
                    f"common for NSE/.NS tickers): {', '.join(stale[:8])}"
                    + (f" +{len(stale) - 8} more" if len(stale) > 8 else "")
                    + ". See the 'Data Thru' column below for the exact date per ticker."
                )

    watchlists = load_watchlists()
    with st.expander(f"Edit {market} watchlist", expanded=False):
        render_watchlist_editor(market, watchlists)

    if not results:
        st.info(f"No {market} tickers with enough data yet. Add tickers above.")
        return

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    f_ema10 = c1.selectbox(labels["w_fast"], ["Any", "Above", "Below"], key=f"f_ema10_{market}")
    f_ema20 = c2.selectbox(labels["w_mid"], ["Any", "Above", "Below"], key=f"f_ema20_{market}")
    f_ema40 = c3.selectbox(labels["w_slow"], ["Any", "Above", "Below"], key=f"f_ema40_{market}")
    f_ema10d = c4.selectbox(labels["d_fast"], ["Any", "Above", "Below"], key=f"f_ema10d_{market}")
    f_ema50 = c5.selectbox(labels["d_mid"], ["Any", "Above", "Below"], key=f"f_ema50_{market}")
    f_ema200 = c6.selectbox(labels["d_slow"], ["Any", "Above", "Below"], key=f"f_ema200_{market}")

    c7, c8, c9, c10, c11, c12 = st.columns(6)
    f_rsi_d = c7.slider("RSI Daily", 0, 100, (0, 100), key=f"f_rsid_{market}")
    f_rsi_w = c8.slider("RSI Weekly", 0, 100, (0, 100), key=f"f_rsiw_{market}")
    f_rsi_m = c9.slider("RSI Monthly", 0, 100, (0, 100), key=f"f_rsim_{market}")
    f_rs_d = c10.slider("RS Daily", -150, 150, (-150, 150), key=f"f_rsd_{market}")
    f_rs_w = c11.slider("RS Weekly", -150, 150, (-150, 150), key=f"f_rsw_{market}")
    f_rs_m = c12.slider("RS Monthly", -150, 150, (-150, 150), key=f"f_rsm_{market}")

    search = st.text_input("Ticker search", "", key=f"search_{market}").strip().upper()

    with st.expander("Custom filters (metric vs metric, or metric vs fixed value)", expanded=False):
        active_custom_filters = render_custom_filter_builder(market, filterable_metrics)

    def passes_ema(row, key, mode):
        if mode == "Any":
            return True
        val = row[key]
        if val is None:
            return False
        below = row["last_close"] < val
        return below if mode == "Below" else not below

    def in_range(val, lo, hi):
        if val is None:
            return True
        return lo <= val <= hi

    filtered = []
    for row in results:
        if not passes_ema(row, "ema10", f_ema10):
            continue
        if not passes_ema(row, "ema20", f_ema20):
            continue
        if not passes_ema(row, "ema40", f_ema40):
            continue
        if not passes_ema(row, "ema10_daily", f_ema10d):
            continue
        if not passes_ema(row, "ema50", f_ema50):
            continue
        if not passes_ema(row, "ema200", f_ema200):
            continue
        if not in_range(row["rsi14_daily"], *f_rsi_d):
            continue
        if not in_range(row["rsi14_weekly"], *f_rsi_w):
            continue
        if not in_range(row["rsi14_monthly"], *f_rsi_m):
            continue
        if not in_range(row["rs_daily"], *f_rs_d):
            continue
        if not in_range(row["rs_weekly"], *f_rs_w):
            continue
        if not in_range(row["rs_monthly"], *f_rs_m):
            continue
        if search and search not in row["ticker"]:
            continue
        filtered.append(row)

    filtered = apply_filters(filtered, active_custom_filters)

    st.write(f"**Showing {len(filtered)} of {len(results)} tickers**")

    if filtered:
        def vstop_change_str(row):
            if row["vstop_weekly_last_change"] is None:
                return "—"
            return str(row["vstop_weekly_weeks_since_change"])

        df = pd.DataFrame(filtered)[[
            "ticker", "last_close", "data_end", "ema10", "ema20", "ema40", "ema10_daily", "ema50", "ema200",
            "rsi14_daily", "rsi14_weekly", "rsi14_monthly", "rs_daily", "rs_weekly", "rs_monthly",
            "vstop_weekly", "vstop_weekly_direction",
        ]].copy()
        df["vstop_change"] = [vstop_change_str(r) for r in filtered]
        df["ticker"] = [tradingview_url(r["ticker"]) for r in filtered]
        df.columns = ["Ticker", "Last", "Data Thru", labels["w_fast"], labels["w_mid"], labels["w_slow"],
                      labels["d_fast"], labels["d_mid"], labels["d_slow"],
                      "RSI-D", "RSI-W", "RSI-M", "RS-D", "RS-W", "RS-M",
                      "VStop-W", "VStop Dir", "VStop Weeks Ago"]

        num_cols = numeric_cols(labels)
        styled = df.style.apply(lambda row: style_row(row, labels), axis=1).format("{:.1f}", subset=num_cols, na_rep="—")
        st.dataframe(
            styled, width="stretch", height=min(600, 40 + 35 * len(filtered)),
            column_config=LINK_COLUMN_CONFIG,
        )
    else:
        st.info("No tickers match the current filters.")

    st.caption(
        f"Mansfield RS = ((price/{bench} ratio today ÷ SMA of that ratio, n) − 1) × 100. "
        "Positive = outperforming the benchmark's trend, negative = underperforming. "
        "WEMA = weekly EMA, DEMA = daily EMA. "
        f"VStop-W = weekly Volatility Stop (Wilder's ATR stop-and-reverse system, "
        f"length={settings['vstop_length']}, factor={settings['vstop_factor']}) — not independently "
        "cross-checked against your chart the way RS/RSI were, so compare a few readings before relying "
        "on it. All values shown to 1 decimal. Edit any of these parameters via Settings in the sidebar."
    )


# ============================================================
# APP LAYOUT
# ============================================================

if "refresh_token" not in st.session_state:
    st.session_state.refresh_token = 0

st.sidebar.title("Stock Watchlist")
sb1, sb2 = st.sidebar.columns(2)
if sb1.button("Refresh Data", type="primary", width="stretch"):
    st.session_state.refresh_token += 1
if sb2.button("⚙️ Settings", width="stretch"):
    settings_dialog()

settings_now = load_settings()
watchlists_now = load_watchlists()
as_of, per_market = cached_fetch_all(st.session_state.refresh_token, json.dumps(watchlists_now, sort_keys=True))
st.sidebar.caption(f"Data as of: {as_of}")
st.sidebar.caption(f"US: {len(per_market.get('US', []))} · India: {len(per_market.get('INDIA', []))}")

tab_us, tab_india, tab_alerts = st.tabs(["US Watchlist", "India Watchlist", "Alert Rules"])

with tab_us:
    render_market_tab("US", per_market.get("US", []), settings_now)

with tab_india:
    render_market_tab("INDIA", per_market.get("INDIA", []), settings_now)

with tab_alerts:
    st.subheader("Alert rules")
    st.caption(
        "Rules here apply across BOTH watchlists combined and are saved to alerts_config.json. "
        "The actual daily check that sends Discord messages runs separately (alert_check.py, "
        "on a schedule) — this tab is for building rules and previewing what would currently fire."
    )

    combined_tickers = watchlists_now.get("US", []) + watchlists_now.get("INDIA", [])
    combined_results = per_market.get("US", []) + per_market.get("INDIA", [])

    alert_metrics = build_metrics(settings_now)
    rules = load_rules()

    st.markdown("**Add a rule**")
    rc1, rc2, rc3 = st.columns([1, 2, 1])
    scope_choice = rc1.selectbox("Scope", ["All watchlist"] + combined_tickers, key="rule_scope")
    metric_choice = rc2.selectbox("Metric", list(alert_metrics.keys()), format_func=lambda k: alert_metrics[k]["label"], key="rule_metric")
    needs_threshold = alert_metrics[metric_choice]["kind"] in ("threshold_below", "threshold_above")
    threshold_val = rc3.number_input("Threshold", value=30.0 if "rsi" in metric_choice else 0.0, step=0.1, format="%.1f", disabled=not needs_threshold, key="rule_threshold")
    if st.button("Add rule"):
        new_rule = {
            "id": uuid.uuid4().hex[:8],
            "scope": "ALL" if scope_choice == "All watchlist" else scope_choice,
            "metric": metric_choice,
            "threshold": threshold_val if needs_threshold else None,
            "enabled": True,
        }
        rules.append(new_rule)
        save_rules(rules)
        st.success("Rule added.")
        st.rerun()

    st.markdown("**Current rules**")
    if not rules:
        st.info("No rules yet — add one above.")
    else:
        for rule in rules:
            cols = st.columns([1, 2, 1, 1, 1])
            if rule["scope"] == "ALL":
                cols[0].write("ALL")
            else:
                cols[0].markdown(f"[{rule['scope']}]({tradingview_url(rule['scope'])})")
            cols[1].write(alert_metrics.get(rule["metric"], {}).get("label", rule["metric"]))
            cols[2].write(rule["threshold"] if rule["threshold"] is not None else "—")
            enabled = cols[3].checkbox("On", value=rule.get("enabled", True), key=f"en_{rule['id']}")
            if enabled != rule.get("enabled", True):
                rule["enabled"] = enabled
                save_rules(rules)
                st.rerun()
            if cols[4].button("Delete", key=f"del_{rule['id']}"):
                rules = [r for r in rules if r["id"] != rule["id"]]
                save_rules(rules)
                st.rerun()

    st.divider()
    st.markdown("**Preview: what would fire right now**")
    if st.button("Run preview"):
        preview = preview_rules(rules, combined_results, metrics=alert_metrics)
        active = [p for p in preview if p["is_true_now"]]
        if not active:
            st.write("No rule conditions are currently true.")
        else:
            pdf = pd.DataFrame(active)[["ticker", "label", "value", "threshold"]]
            pdf["value"] = pdf["value"].astype(str)
            pdf["threshold"] = pdf["threshold"].astype(str)
            pdf["ticker"] = [tradingview_url(t) for t in pdf["ticker"]]
            pdf.columns = ["Ticker", "Rule", "Current value", "Threshold"]
            st.dataframe(pdf, width="stretch", column_config=LINK_COLUMN_CONFIG)

    st.divider()
    st.markdown("**Discord webhook**")
    st.caption(
        "For cloud deployment, set this as a Streamlit secret named DISCORD_WEBHOOK_URL "
        "instead of typing it here (don't commit webhook URLs to a public repo)."
    )
    current_webhook = get_discord_webhook() or ""
    webhook_input = st.text_input("Webhook URL", value=current_webhook, type="password")
    wc1, wc2 = st.columns(2)
    if wc1.button("Save locally"):
        save_discord_webhook_local(webhook_input)
        st.success("Saved to discord_config.json (local only, not committed to git).")
    if wc2.button("Send test message"):
        if not webhook_input:
            st.error("Enter a webhook URL first.")
        else:
            ok = send_discord(webhook_input, "✅ Test alert from your Stock Watchlist app.")
            st.success("Sent!") if ok else st.error("Failed to send — check the webhook URL.")
