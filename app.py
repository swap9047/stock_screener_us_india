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

import re
import uuid
from datetime import date, datetime

import pandas as pd
import streamlit as st

from stock_data import (
    load_watchlists, save_watchlist, fetch_all_markets, validate_ticker, tradingview_url,
    load_settings, save_settings, DEFAULT_SETTINGS, get_benchmarks, get_filterable_metrics,
    MARKETS,
)
from alerts import (load_rules, save_rules, preview_rules, DISCORD_CONFIG_FILE, send_discord, SCOPE_LABELS,
                     build_discord_messages_for_rule)
from filters import (get_market_filters, save_market_filters, apply_filters, describe_filter,
                     describe_chain, describe_chain_with_values)
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
def cached_fetch_all(refresh_token, watchlists_json, settings_json):
    # NOTE: refresh_token and settings_json must NOT be prefixed with "_" --
    # Streamlit's cache_data excludes underscore-prefixed params from the
    # cache key hash, which silently broke the Refresh Data button (clicking
    # it changed refresh_token but the cache never saw that as a new key, so
    # it kept serving stale results). Settings are included in the key too,
    # so changing calculation parameters always forces a fresh fetch.
    watchlists = json.loads(watchlists_json)
    settings = json.loads(settings_json)
    combined, as_of, per_market = fetch_all_markets(watchlists, settings=settings)
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


def parse_filter_value_text(text):
    """Converts a typed 'Value' field (filter/alert condition builders) to a
    float when it looks numeric, otherwise keeps it as a stripped string
    (e.g. "Yes" for Tech Uptrend). The word->0/1 mapping itself happens at
    compare time in filters._coerce_fixed_value, since that needs the row's
    live metric_a value to know whether "Yes" should mean 1."""
    text = (text or "").strip()
    try:
        return float(text)
    except ValueError:
        return text


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
        elif col == "Trend" and isinstance(val, str):
            trend_colors = {
                "Strong Uptrend": "#145a32", "Uptrend": "#1e8449",
                "Downtrend": "#c0392b", "Strong Downtrend": "#7b241c",
            }
            color = trend_colors.get(val)
            if color:
                styles[i] = f"color:{color};font-weight:700"
        elif col == "% Chg" and pd.notna(val):
            styles[i] = "color:#1e8449;font-weight:600" if val > 0 else ("color:#c0392b;font-weight:600" if val < 0 else "")
        elif col == "Vol Trend" and isinstance(val, str):
            vol_colors = {"Exploding": "#1e8449", "Declining": "#c0392b"}
            color = vol_colors.get(val)
            if color:
                styles[i] = f"color:{color};font-weight:600"
        elif col == "Tech Uptrend" and val == "Yes":
            styles[i] = "color:#1e8449;font-weight:700"
        elif col == "Alerts" and isinstance(val, str) and val not in ("—", ""):
            styles[i] = "color:#8e44ad;font-weight:600"
    return styles


def numeric_cols(ema_labels):
    return ["Last", ema_labels["w_fast"], ema_labels["w_mid"], ema_labels["w_slow"],
            ema_labels["d_fast"], ema_labels["d_mid"], ema_labels["d_slow"],
            "RSI-D", "RSI-W", "RSI-M", "RS-D", "RS-W", "RS-M", "VStop-W",
            "52W High", "52W Low"]


VOLUME_COLS = ["Vol 10D", "Vol 100D"]
PCT_COLS = ["% Chg"]


LINK_COLUMN_CONFIG = {
    "Ticker": st.column_config.LinkColumn(
        "Ticker", display_text=r"https://www\.tradingview\.com/chart/\?symbol=(.*)",
        pinned=True,
    ),
}

STICKY_TH_STYLE = (
    # top:60px (not 0) -- Streamlit's own app toolbar/header chrome occupies
    # the top 60px of the viewport and sits at a far higher z-index than
    # anything in page content can reach, so a sticky element at top:0 gets
    # visually painted over by it (confirmed by rendering the app in a real
    # browser and screenshotting: the header was there geometrically at
    # top:0, "visible" per the DOM, but invisible on screen). Sticking 60px
    # down clears Streamlit's own chrome instead of hiding underneath it.
    "position:sticky;top:60px;z-index:2;"
    "background-color:var(--background-color, #ffffff);"
    "box-shadow:0 1px 0 rgba(128,128,128,0.4);"
    "text-align:left;padding:6px 10px;font-size:13px;"
)


def sticky_header_html(styler):
    """Renders a pandas Styler as an HTML table with a sticky header, embedded
    directly in the page (no nested scroll box -- the browser's normal page
    scroll handles it).

    Streamlit's markdown/HTML renderer strips <style> tags outright (along
    with <pre>/<script>/<textarea>) even with unsafe_allow_html=True -- this
    is a fixed list in its own markdown parser, confirmed in Streamlit's
    frontend source, not a guess. That's why a <style>-block-based sticky
    header (via Styler.set_table_styles()) silently does nothing. Inline
    style="..." *attributes* are a different thing and are NOT stripped
    (proven by the fact that this table's per-cell color coding, which is
    exactly that, already renders correctly) -- so the sticky CSS is
    injected directly onto each <th> tag here instead of via a <style> block."""
    html = styler.to_html(escape=False)
    return re.sub(r"<th\b", f'<th style="{STICKY_TH_STYLE}"', html)


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

    st.markdown("**Trend classification**")
    trend_slope_lookback = st.number_input(
        "15. Trend MA slope lookback (weeks)", min_value=2, step=1,
        value=int(settings.get("trend_slope_lookback", 3)), key="set_trend_slope",
        help="Width of the regression window (in weeks) used to judge whether the slow WEMA is "
             "currently rising or falling. Shorter = catches recent rollovers faster (can be "
             "noisier); longer = smoother but slower to detect a real trend change.",
    )

    st.markdown("**Volume & Tech Uptrend classification**")
    vt1, vt2, vt3 = st.columns(3)
    volume_explode_ratio = vt1.number_input(
        "16. Volume 'Exploding' ratio (10D ÷ 100D avg ≥)", min_value=1.0, step=0.1, format="%.2f",
        value=float(settings.get("volume_explode_ratio", 1.4)), key="set_vol_explode",
        help="Vol Trend shows 'Exploding' when 10-day average volume is at least this many times the 100-day average.",
    )
    volume_decline_ratio = vt2.number_input(
        "17. Volume 'Declining' ratio (10D ÷ 100D avg ≤)", min_value=0.0, step=0.1, format="%.2f",
        value=float(settings.get("volume_decline_ratio", 0.7)), key="set_vol_decline",
        help="Vol Trend shows 'Declining' when 10-day average volume is at or below this fraction of the 100-day average.",
    )
    tech_uptrend_min_vstop_weeks = vt3.number_input(
        "18. Tech Uptrend: min weeks held above VStop", min_value=0, step=1,
        value=int(settings.get("tech_uptrend_min_vstop_weeks", 3)), key="set_tech_min_weeks",
        help="Tech Uptrend requires the weekly VStop to have been in an uptrend for MORE than this many weeks "
             "(in addition to close > VStop, close > slow WEMA, and volume ≥ the 'Exploding' ratio above).",
    )

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
                "trend_slope_lookback": int(trend_slope_lookback),
                "volume_explode_ratio": float(volume_explode_ratio),
                "volume_decline_ratio": float(volume_decline_ratio),
                "tech_uptrend_min_vstop_weeks": int(tech_uptrend_min_vstop_weeks),
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
    st.markdown(
        "**Custom filters** — compare any metric to another metric or a fixed value. "
        "Add multiple conditions and chain each one with AND/OR against everything before it "
        "(e.g. cond1 AND cond2 AND cond3 OR cond4), evaluated left to right."
    )
    metric_names = list(filterable_metrics.keys())
    metric_labels = {v: k for k, v in filterable_metrics.items()}

    active_filters = get_market_filters(market)

    if active_filters:
        st.write("Active conditions:")
        st.caption(describe_chain(active_filters, metric_labels))
        remove_id = None
        for i, filt in enumerate(active_filters):
            cc1, cc2 = st.columns([5, 1])
            prefix = "" if i == 0 else f"{filt.get('logic', 'AND')}  "
            cc1.write(f"{prefix}{describe_filter(filt, metric_labels)}")
            if cc2.button("Remove", key=f"cf_rm_{market}_{filt['id']}"):
                remove_id = filt["id"]
        if remove_id:
            remaining = [f for f in active_filters if f["id"] != remove_id]
            save_market_filters(market, remaining)
            st.rerun()

    st.markdown("Add a condition:" if not active_filters else "Add another condition:")
    if active_filters:
        logic_choice = st.radio(
            "Combine with the condition(s) above using", ["AND", "OR"],
            key=f"cf_logic_{market}", horizontal=True,
        )
    else:
        logic_choice = "AND"

    fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1, 1.5, 2, 1])
    metric_a_label = fc1.selectbox("Metric A", metric_names, key=f"cf_a_{market}")
    operator_choice = fc2.selectbox("Op", [">", "<", ">=", "<=", "=="], key=f"cf_op_{market}")
    compare_type = fc3.radio("Compare to", ["Metric", "Fixed value"], key=f"cf_ctype_{market}", horizontal=True)

    multiplier, offset = 1.0, 0.0
    if compare_type == "Metric":
        metric_b_label = fc4.selectbox("Metric B", metric_names, key=f"cf_b_{market}")
        value = None
        mc1, mc2 = st.columns(2)
        multiplier = mc1.number_input(
            "× Multiplier (optional)", value=1.0, step=0.1, format="%.2f", key=f"cf_mult_{market}",
            help="e.g. set to 1.4 for 'Vol 10D Avg >= 1.4 × Vol 100D Avg'.",
        )
        offset = mc2.number_input("+ Offset (optional)", value=0.0, step=0.1, format="%.2f", key=f"cf_off_{market}")
    else:
        value_text = fc4.text_input(
            "Value", value="0", key=f"cf_val_{market}",
            help="A number (e.g. 45) or a word for boolean-like metrics, e.g. Yes / No for Tech Uptrend.",
        )
        metric_b_label = None

    if fc5.button("Add", key=f"cf_add_{market}"):
        new_filter = {
            "id": uuid.uuid4().hex[:8],
            "metric_a": filterable_metrics[metric_a_label],
            "operator": operator_choice,
            "compare_type": "metric" if compare_type == "Metric" else "value",
            "logic": logic_choice,
        }
        if compare_type == "Metric":
            new_filter["metric_b"] = filterable_metrics[metric_b_label]
            if multiplier != 1.0:
                new_filter["multiplier"] = multiplier
            if offset != 0.0:
                new_filter["offset"] = offset
        else:
            new_filter["value"] = parse_filter_value_text(value_text)
        active_filters.append(new_filter)
        save_market_filters(market, active_filters)
        st.rerun()

    return active_filters


def build_column_defs(labels):
    """(data key, column label) for every optional (hideable) watchlist
    column, in display order, plus derived lookup dicts. Ticker/Last are
    mandatory and not included here. Shared by both market tabs (via
    render_shared_column_picker) so US and India always offer/show the
    identical set of columns."""
    optional_defs = [
        ("trend", "Trend"),
        ("matched_alerts", "Alerts"),
        ("pct_change_1d", "% Chg"),
        ("week52_high", "52W High"),
        ("week52_low", "52W Low"),
        ("data_end", "Data Thru"),
        ("ema10", labels["w_fast"]),
        ("ema20", labels["w_mid"]),
        ("ema40", labels["w_slow"]),
        ("ema10_daily", labels["d_fast"]),
        ("ema50", labels["d_mid"]),
        ("ema200", labels["d_slow"]),
        ("rsi14_daily", "RSI-D"),
        ("rsi14_weekly", "RSI-W"),
        ("rsi14_monthly", "RSI-M"),
        ("rs_daily", "RS-D"),
        ("rs_weekly", "RS-W"),
        ("rs_monthly", "RS-M"),
        ("vstop_weekly", "VStop-W"),
        ("vstop_weekly_direction", "VStop Dir"),
        ("vstop_change", "VStop Weeks Ago"),
        ("volume_trend", "Vol Trend"),
        ("tech_uptrend_label", "Tech Uptrend"),
        ("avg_volume_10d", "Vol 10D"),
        ("avg_volume_100d", "Vol 100D"),
    ]
    label_by_key = dict(optional_defs)
    key_by_label = {lbl: k for k, lbl in optional_defs}
    all_labels = list(label_by_key.values())
    # Raw 10D/100D volume are hidden by default (Vol Trend already
    # summarizes them); everything else shows by default.
    default_hidden = {"Vol 10D", "Vol 100D"}
    default_visible = [lbl for lbl in all_labels if lbl not in default_hidden]
    return optional_defs, label_by_key, key_by_label, all_labels, default_visible


def render_shared_column_picker(labels):
    """Single 'Columns to show / reorder' control, rendered ONCE (in the
    sidebar) and shared by both the US and India watchlist tables, so
    picking/reordering columns always applies to both.

    This used to be two separate widgets (one per tab) kept in sync by
    force-writing the shared value into each widget's session_state before
    it was instantiated -- that turns out to be unreliable in Streamlit:
    doing that write on the SAME render where the user just interacted with
    THAT widget silently clobbers their pending change before the widget
    ever sees it (confirmed empirically, not just suspected). A single
    shared widget sidesteps the problem entirely rather than working around
    it. Returns (visible_keys, label_by_key)."""
    optional_defs, label_by_key, key_by_label, all_labels, default_visible = build_column_defs(labels)

    SHARED_ORDER_KEY = "shared_col_order"
    if SHARED_ORDER_KEY not in st.session_state:
        st.session_state[SHARED_ORDER_KEY] = [key_by_label[lbl] for lbl in default_visible]
    # Drop any keys that no longer exist (e.g. a future app update renames
    # or removes a column) so stale saved state can't crash the lookup below.
    st.session_state[SHARED_ORDER_KEY] = [k for k in st.session_state[SHARED_ORDER_KEY] if k in label_by_key]

    with st.sidebar.expander("Columns to show / reorder", expanded=False):
        st.caption("Applies to both the US and India tables. Ticker and Last always show first.")
        current_labels = [label_by_key[k] for k in st.session_state[SHARED_ORDER_KEY]]
        visible_labels = st.multiselect(
            "Columns to show", options=all_labels, default=current_labels, key="shared_cols_multiselect",
        )
        selected_keys = [key_by_label[lbl] for lbl in visible_labels if lbl in key_by_label]

        # Keep the existing order for columns that are still selected,
        # append any newly-selected ones at the end, drop deselected ones.
        order = [k for k in st.session_state[SHARED_ORDER_KEY] if k in selected_keys]
        for k in selected_keys:
            if k not in order:
                order.append(k)
        st.session_state[SHARED_ORDER_KEY] = order

        if order:
            st.caption("Order (use ↑ / ↓ to move a column left/right in the table):")
            move = None  # (index, direction)
            for i, k in enumerate(order):
                oc1, oc2, oc3 = st.columns([6, 1, 1])
                oc1.write(f"{i + 1}. {label_by_key[k]}")
                if oc2.button("↑", key=f"colup_{k}", disabled=(i == 0), width="stretch"):
                    move = (i, -1)
                if oc3.button("↓", key=f"coldown_{k}", disabled=(i == len(order) - 1), width="stretch"):
                    move = (i, 1)
            if move is not None:
                i, d = move
                order[i], order[i + d] = order[i + d], order[i]
                st.session_state[SHARED_ORDER_KEY] = order
                st.rerun()

    return st.session_state[SHARED_ORDER_KEY], label_by_key


def render_market_tab(market, results, settings, visible_keys, label_by_key):
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

    src1, src2, src3, src4 = st.columns([3, 1.3, 1.3, 1])
    search = src1.text_input("Ticker search", "", key=f"search_{market}").strip().upper()
    f_trend = src2.selectbox(
        "Trend", ["Any", "Strong Uptrend", "Uptrend", "Downtrend", "Strong Downtrend"],
        key=f"f_trend_{market}",
    )
    f_vol_trend = src3.selectbox(
        "Vol Trend", ["Any", "Exploding", "In-line", "Declining"], key=f"f_voltrend_{market}",
    )
    f_tech_only = src4.checkbox("Tech Uptrend only", key=f"f_tech_{market}")

    with st.expander("Custom filters (metric vs metric, or metric vs fixed value; chain with AND/OR)", expanded=False):
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
        if f_trend != "Any" and row.get("trend") != f_trend:
            continue
        if f_vol_trend != "Any" and row.get("volume_trend") != f_vol_trend:
            continue
        if f_tech_only and not row.get("tech_uptrend"):
            continue
        filtered.append(row)

    filtered = apply_filters(filtered, active_custom_filters)

    st.write(f"**Showing {len(filtered)} of {len(results)} tickers**")

    if filtered:
        def vstop_change_str(row):
            if row["vstop_weekly_last_change"] is None:
                return "—"
            return str(row["vstop_weekly_weeks_since_change"])

        raw_df = pd.DataFrame(filtered)
        raw_df["vstop_change"] = [vstop_change_str(r) for r in filtered]
        raw_df["tech_uptrend_label"] = raw_df["tech_uptrend"].apply(lambda v: "Yes" if v else "No")
        raw_df["ticker_link"] = [
            f'<a href="{tradingview_url(r["ticker"])}" target="_blank" rel="noopener noreferrer">{r["ticker"]}</a>'
            for r in filtered
        ]

        # Which enabled alert rules is each ticker currently matching?
        # Reuses the same preview engine the Alert Rules tab's "Run preview"
        # button uses, scoped to this market's own rows so ALL/US/INDIA/
        # per-ticker scope all resolve correctly without cross-market leakage.
        # Shown as a number (1, 2, 3...) rather than the full rule name to
        # keep the column compact -- numbers are assigned by each rule's
        # position in alerts_config.json, so they mean the same thing on
        # both the US and India tabs; the footer legend below spells out
        # what each number actually means.
        alert_rules_all = load_rules()
        numbered_rules = [r for r in alert_rules_all if r.get("enabled", True) and r.get("conditions")]
        rule_number = {r["id"]: i + 1 for i, r in enumerate(numbered_rules)}
        alert_matches = {}
        if alert_rules_all:
            for p in preview_rules(alert_rules_all, results):
                if p["is_true_now"]:
                    num = rule_number.get(p["rule_id"])
                    if num is not None:
                        alert_matches.setdefault(p["ticker"], []).append(num)
        raw_df["matched_alerts"] = [
            ", ".join(str(n) for n in sorted(alert_matches.get(r["ticker"], []))) or "—" for r in filtered
        ]

        # Column visibility/order is chosen ONCE via the shared sidebar
        # picker (render_shared_column_picker) and passed in, so US and
        # India always show identical columns in identical order.
        df = raw_df[["ticker_link", "last_close"] + visible_keys].copy()
        df.columns = ["Ticker", "Last"] + [label_by_key[k] for k in visible_keys]
        if "Trend" in df.columns:
            df["Trend"] = df["Trend"].fillna("—")

        num_cols = [c for c in numeric_cols(labels) if c in df.columns]
        pct_cols = [c for c in PCT_COLS if c in df.columns]
        vol_cols = [c for c in VOLUME_COLS if c in df.columns]

        styled = (
            df.style
            .hide(axis="index")
            .apply(lambda row: style_row(row, labels), axis=1)
            .format("{:.1f}", subset=num_cols, na_rep="—")
            .format("{:,.0f}", subset=vol_cols, na_rep="—")
        )
        if pct_cols:
            styled = styled.format(lambda v: f"{v:+.2f}%" if pd.notna(v) else "—", subset=pct_cols)
        st.markdown(sticky_header_html(styled), unsafe_allow_html=True)

        # Footer legend: spell out what each number in the Alerts column
        # actually means, so you don't have to jump to the Alert Rules tab
        # to remember what e.g. "2" refers to.
        if "Alerts" in df.columns and alert_matches:
            used_numbers = {n for nums in alert_matches.values() for n in nums}
            metric_labels_market = {v: k for k, v in filterable_metrics.items()}
            legend_bits = []
            for r in numbered_rules:
                num = rule_number[r["id"]]
                if num in used_numbers:
                    name = r.get("name") or "(unnamed)"
                    legend_bits.append(f"**{num}** = {name} — {describe_chain(r['conditions'], metric_labels_market)}")
            if legend_bits:
                st.caption("Alert legend: " + " · ".join(legend_bits))
    else:
        st.info("No tickers match the current filters.")

    st.caption(
        f"Mansfield RS = ((price/{bench} ratio today ÷ SMA of that ratio, n) − 1) × 100. "
        "Positive = outperforming the benchmark's trend, negative = underperforming. "
        "WEMA = weekly EMA, DEMA = daily EMA. "
        f"VStop-W = weekly Volatility Stop (Wilder's ATR stop-and-reverse system, "
        f"length={settings['vstop_length']}, factor={settings['vstop_factor']}) — not independently "
        "cross-checked against your chart the way RS/RSI were, so compare a few readings before relying "
        "on it. Trend = a 4-level read (Strong Uptrend / Uptrend / Downtrend / Strong Downtrend) combining "
        f"price vs. the slow WEMA, that WEMA's {settings.get('trend_slope_lookback', 3)}-week slope, and "
        "weekly RS for direction; 'Strong' additionally requires price within 10% of its 52-week high/low "
        "AND rising volume (10D avg > 100D avg) — a sort/filter aid, not a precise signal. "
        "52W High/Low = trailing 12-month intraday extremes. Vol 10D/100D = average daily share volume "
        "over the last 10 / 100 trading days. % Chg = 1-day close-to-close change. Vol Trend classifies "
        f"10D-vs-100D average volume as Exploding (≥{settings.get('volume_explode_ratio', 1.4)}×), "
        f"Declining (≤{settings.get('volume_decline_ratio', 0.7)}×), or In-line — thresholds editable in "
        "Settings. Tech Uptrend = close above the weekly VStop (held for more than "
        f"{settings.get('tech_uptrend_min_vstop_weeks', 3)} weeks) AND close above the slow WEMA AND 10D "
        "volume surging past the Exploding ratio above. All values shown to 1 decimal. Use 'Columns to "
        "show' above the table to hide/show columns. Edit any of these parameters via Settings in the "
        "sidebar."
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
as_of, per_market = cached_fetch_all(
    st.session_state.refresh_token,
    json.dumps(watchlists_now, sort_keys=True),
    json.dumps(settings_now, sort_keys=True),
)
st.sidebar.caption(f"Data as of: {as_of}")
st.sidebar.caption(f"US: {len(per_market.get('US', []))} · India: {len(per_market.get('INDIA', []))}")

shared_visible_keys, shared_label_by_key = render_shared_column_picker(ema_col_labels(settings_now))

tab_us, tab_india, tab_alerts = st.tabs(["US Watchlist", "India Watchlist", "Alert Rules"])

with tab_us:
    render_market_tab("US", per_market.get("US", []), settings_now, shared_visible_keys, shared_label_by_key)

with tab_india:
    render_market_tab("INDIA", per_market.get("INDIA", []), settings_now, shared_visible_keys, shared_label_by_key)

with tab_alerts:
    st.subheader("Alert rules")
    st.caption(
        "Each rule has a name, a scope, and one or more conditions. Conditions compare any "
        "metric to another metric or a fixed value — the same building blocks as the watchlist "
        "custom filters — and chain together with AND/OR, evaluated left to right "
        "(e.g. cond1 AND cond2 AND cond3 OR cond4). An alert fires once when the combined "
        "condition becomes true, not every day it stays true. The daily check "
        "(alert_check.py / GitHub Actions) sends these to Discord — this tab is for building "
        "rules and previewing what would fire right now."
    )

    combined_tickers = watchlists_now.get("US", []) + watchlists_now.get("INDIA", [])
    combined_results = per_market.get("US", []) + per_market.get("INDIA", [])
    filterable_metrics_alert = get_filterable_metrics(settings_now)
    metric_names_alert = list(filterable_metrics_alert.keys())
    metric_labels_alert = {v: k for k, v in filterable_metrics_alert.items()}

    rules = load_rules()

    # ── Rule builder ────────────────────────────────────────────────────────
    st.markdown("**Add a rule**")

    if "draft_rule_conditions" not in st.session_state:
        st.session_state.draft_rule_conditions = []

    top1, top2 = st.columns([2, 3])
    scope_options = ["All watchlist", "US watchlist", "India watchlist"] + combined_tickers
    scope_choice = top1.selectbox("Scope", scope_options, key="rule_scope")
    rule_name = top2.text_input("Name (optional)", key="rule_name", placeholder="e.g. Stage 2 breakout")

    if st.session_state.draft_rule_conditions:
        st.caption("Conditions in this rule so far:")
        st.caption(describe_chain(st.session_state.draft_rule_conditions, metric_labels_alert))
        remove_idx = None
        for i, cond in enumerate(st.session_state.draft_rule_conditions):
            cc1, cc2 = st.columns([5, 1])
            prefix = "" if i == 0 else f"{cond.get('logic', 'AND')}  "
            cc1.write(f"{prefix}{describe_filter(cond, metric_labels_alert)}")
            if cc2.button("Remove", key=f"dr_rm_{i}"):
                remove_idx = i
        if remove_idx is not None:
            st.session_state.draft_rule_conditions.pop(remove_idx)
            st.rerun()

    st.markdown("Add a condition to this rule:" if not st.session_state.draft_rule_conditions
                else "Add another condition:")
    if st.session_state.draft_rule_conditions:
        dr_logic = st.radio(
            "Combine with the condition(s) above using", ["AND", "OR"],
            key="dr_logic", horizontal=True,
        )
    else:
        dr_logic = "AND"

    rc1, rc2, rc3, rc4, rc5 = st.columns([2, 1, 1.5, 2, 1])
    dr_metric_a = rc1.selectbox("Metric A", metric_names_alert, key="dr_metric_a")
    dr_operator = rc2.selectbox("Op", [">", "<", ">=", "<=", "=="], key="dr_operator")
    dr_compare_type = rc3.radio("Compare to", ["Metric", "Fixed value"], key="dr_compare_type", horizontal=True)
    dr_multiplier, dr_offset = 1.0, 0.0
    if dr_compare_type == "Metric":
        dr_metric_b = rc4.selectbox("Metric B", metric_names_alert, key="dr_metric_b")
        dr_value = None
        dmc1, dmc2 = st.columns(2)
        dr_multiplier = dmc1.number_input(
            "× Multiplier (optional)", value=1.0, step=0.1, format="%.2f", key="dr_mult",
            help="e.g. set to 1.4 for 'Vol 10D Avg >= 1.4 × Vol 100D Avg'.",
        )
        dr_offset = dmc2.number_input("+ Offset (optional)", value=0.0, step=0.1, format="%.2f", key="dr_off")
    else:
        dr_value_text = rc4.text_input(
            "Value", value="0", key="dr_value",
            help="A number (e.g. 45) or a word for boolean-like metrics, e.g. Yes / No for Tech Uptrend.",
        )
        dr_metric_b = None

    if rc5.button("＋ Add condition"):
        new_cond = {
            "metric_a": filterable_metrics_alert[dr_metric_a],
            "operator": dr_operator,
            "compare_type": "metric" if dr_compare_type == "Metric" else "value",
            "logic": dr_logic,
        }
        if dr_compare_type == "Metric":
            new_cond["metric_b"] = filterable_metrics_alert[dr_metric_b]
            if dr_multiplier != 1.0:
                new_cond["multiplier"] = dr_multiplier
            if dr_offset != 0.0:
                new_cond["offset"] = dr_offset
        else:
            new_cond["value"] = parse_filter_value_text(dr_value_text)
        st.session_state.draft_rule_conditions.append(new_cond)
        st.rerun()

    save_col, clear_col = st.columns([1, 1])
    if save_col.button("Save rule", type="primary"):
        if not st.session_state.draft_rule_conditions:
            st.error("Add at least one condition first.")
        else:
            if scope_choice == "All watchlist":
                scope_val = "ALL"
            elif scope_choice == "US watchlist":
                scope_val = "US"
            elif scope_choice == "India watchlist":
                scope_val = "INDIA"
            else:
                scope_val = scope_choice
            new_rule = {
                "id": uuid.uuid4().hex[:8],
                "name": st.session_state.get("rule_name", "").strip(),
                "scope": scope_val,
                "conditions": list(st.session_state.draft_rule_conditions),
                "enabled": True,
            }
            rules.append(new_rule)
            save_rules(rules)
            st.session_state.draft_rule_conditions = []
            st.success("Rule added.")
            st.rerun()
    if clear_col.button("Clear draft"):
        st.session_state.draft_rule_conditions = []
        st.rerun()

    # ── Current rules ───────────────────────────────────────────────────────
    st.markdown("**Current rules**")
    if not rules:
        st.info("No rules yet — add one above.")
    else:
        for rule in rules:
            scope_label = SCOPE_LABELS.get(rule.get("scope"), rule.get("scope"))
            name_label = rule.get("name") or "(unnamed)"
            n_conds = len(rule.get("conditions", []))
            expander_title = f"{name_label} — {scope_label} ({n_conds} condition{'s' if n_conds != 1 else ''})"

            with st.expander(expander_title, expanded=False):
                if rule.get("conditions"):
                    st.write(describe_chain(rule["conditions"], metric_labels_alert))
                else:
                    st.warning("This rule has no conditions (from an older rule format) — delete it and re-add with the current builder.")
                if rule.get("scope") not in ("ALL", "US", "INDIA"):
                    st.markdown(f"Ticker: [{rule['scope']}]({tradingview_url(rule['scope'])})")

                en_col, del_col = st.columns([1, 1])
                enabled = en_col.checkbox("Enabled", value=rule.get("enabled", True), key=f"en_{rule['id']}")
                if enabled != rule.get("enabled", True):
                    rule["enabled"] = enabled
                    save_rules(rules)
                    st.rerun()
                if del_col.button("🗑 Delete rule", key=f"del_{rule['id']}"):
                    rules = [r for r in rules if r["id"] != rule["id"]]
                    save_rules(rules)
                    st.rerun()

    # ── Preview ─────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Preview: what would fire right now**")
    if st.button("Run preview"):
        preview = preview_rules(rules, combined_results)
        st.session_state.preview_active = [p for p in preview if p["is_true_now"]]
        st.session_state.preview_as_of = as_of

    active = st.session_state.get("preview_active")
    if active is not None:
        if not active:
            st.write("No rule conditions are currently true.")
        else:
            rows_preview = []
            for p in active:
                rows_preview.append({
                    "Ticker": tradingview_url(p["ticker"]),
                    "Rule": p.get("rule_name") or "(unnamed)",
                    "Conditions": describe_chain_with_values(p["row"], p["conditions"], metric_labels_alert),
                })
            pdf = pd.DataFrame(rows_preview)
            st.dataframe(pdf, width="stretch", column_config=LINK_COLUMN_CONFIG)

            if st.button("📤 Send this preview to Discord"):
                webhook = get_discord_webhook()
                if not webhook:
                    st.error("No Discord webhook configured yet — set one in the Discord section below first.")
                else:
                    # One table per rule (Ticker + the metrics that rule's
                    # conditions reference), not a flat per-ticker line list —
                    # same format used by the daily automated alert send.
                    rules_by_id = {r["id"]: r for r in rules}
                    by_ticker_preview = {p["ticker"]: p["row"] for p in active}
                    tickers_by_rule = {}
                    for p in active:
                        tickers_by_rule.setdefault(p["rule_id"], []).append(p["ticker"])

                    all_msgs = [f"**Alert Preview — {st.session_state.get('preview_as_of', as_of)}**"]
                    for rule_id, tickers in tickers_by_rule.items():
                        rule = rules_by_id.get(rule_id)
                        if not rule:
                            continue
                        all_msgs.extend(
                            build_discord_messages_for_rule(rule, tickers, by_ticker_preview, metric_labels_alert)
                        )

                    ok = all(send_discord(webhook, m) for m in all_msgs)
                    if ok:
                        st.success(
                            f"Sent {len(active)} match(es) across {len(tickers_by_rule)} rule(s) to Discord "
                            f"({len(all_msgs)} message{'s' if len(all_msgs) != 1 else ''})."
                        )
                    else:
                        st.error("Failed to send — check the webhook URL in the Discord section below.")

    # ── Discord ───────────────────────────────────────────────────────────────
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
