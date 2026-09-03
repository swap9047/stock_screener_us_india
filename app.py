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
another metric or a fixed value (e.g. "10 WEMA > 40 WEMA", "200 DSMA >= 200"),
combined with AND logic alongside the preset Above/Below/range filters.

Sidebar "Settings" opens a dialog where every calculation parameter (SMA
periods, RSI period, RS lookbacks, VStop length/factor, benchmarks) can be
edited -- changes are saved to settings.json and take effect on next refresh
-- plus a "Login (optional)" section to set/change/disable the username and
password gate directly from the UI (saves to a local auth_config.json).

For a Streamlit Cloud deployment, set AUTH_USERNAME / AUTH_PASSWORD as
Streamlit secrets instead (Settings dialog will say so if a secret is
already active) -- secrets persist across redeploys, a local file doesn't.
If no login is configured anywhere, the app is open.
"""

import copy
import hashlib
import hmac
import html
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

from streamlit_sortables import sort_items

from stock_data import (
    load_watchlists, save_watchlist, fetch_all_markets, validate_ticker, tradingview_url,
    load_settings, save_settings, DEFAULT_SETTINGS, get_benchmarks, get_filterable_metrics,
    load_markets_registry, load_data_snapshot, snapshot_is_usable, save_data_snapshot,
    rebuild_snapshot_for_market, fill_snapshot_gaps,
    load_watchlist_groups, save_watchlist_groups,
)
from alerts import (load_rules, save_rules, preview_rules, DISCORD_CONFIG_FILE,
                     send_discord_batch, build_discord_messages_for_rule, describe_schedule,
                     DAY_CODES, DAY_LABELS, DEFAULT_DAYS, ALLOWED_HOURS, HOUR_LABELS,
                     compute_rule_truth, RULE_COLOR_HEX, _metrics_used_in_conditions,
                     active_alerts_for_prompt, alerts_text_for)

# Alerts-column color picker, shared by the add-rule builder and the
# existing-rule editor -- UI label <-> stored rule["color"] value ("green"/
# "red"/None). One definition so the two pickers can never drift apart.
RULE_COLOR_UI_OPTIONS = ["None", "🟢 Green", "🔴 Red"]
RULE_COLOR_UI_TO_VALUE = {"None": None, "🟢 Green": "green", "🔴 Red": "red"}
RULE_COLOR_VALUE_TO_UI = {v: k for k, v in RULE_COLOR_UI_TO_VALUE.items()}
from weekly_wrapup import (
    build_wrapup, eligible_rules, load_wrapup_state, _pretty_date,
    build_discord_messages as build_wrapup_messages,
)
from filters import (get_market_filters, save_market_filters, apply_filters, describe_filter,
                     describe_chain, describe_chain_with_values, passes_filter_chain, CATEGORICAL_METRICS)
from github_sync import get_github_config, push_all_config, trigger_github_workflow, SYNCABLE_FILES
from news_summary import load_news_summary, MARKET_LABELS, get_gemini_api_key
from expert_views import (load_expert_views, save_expert_views, analyze_single_ticker,
                          generate_expert_view, _is_valid_view, is_pending_view,
                          VERDICT_RULES, VERDICT_GUARD_RULES,
                          validate_verdict, verdict_flag_note)
from fundamentals_eval import (
    load_fundamentals, _validate_sentiment, SENTIMENT_STALE_DAYS,
    analyze_single_ticker_sentiment, _is_valid_view as _is_valid_sentiment_view,
)
from custom_columns import (
    load_custom_columns, save_custom_columns, validate_formula, column_key,
    FORMAT_CHOICES, CUSTOM_COLUMNS_FILE, apply_custom_columns_to_rows,
)
from ticker_notes import (
    load_ticker_notes, save_ticker_notes, set_ticker_note, get_ticker_note, get_ticker_flag,
    apply_notes_to_rows, flag_marker_html, FLAG_CHOICES, FLAG_EMOJI, TICKER_NOTES_FILE,
)
import json
import os

st.set_page_config(page_title="Stock Watchlist", layout="wide")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTH_CONFIG_FILE = os.path.join(SCRIPT_DIR, "auth_config.json")
COLUMN_PREFS_FILE = os.path.join(SCRIPT_DIR, "column_prefs.json")


def load_column_prefs_full():
    if not os.path.exists(COLUMN_PREFS_FILE): return {}
    try:
        with open(COLUMN_PREFS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"order": data}
    except Exception:
        return {}

def update_column_prefs(key, value):
    data = load_column_prefs_full()
    data[key] = value
    with open(COLUMN_PREFS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_column_prefs():
    """Returns the saved column order (list of data keys, visible ones only,
    in display order) from column_prefs.json, or None if there's no file yet
    or it's malformed -- callers fall back to the built-in default order in
    that case. Unlike discord_config.json/auth_config.json, this file is NOT
    secret -- it's meant to be committed/pushed like watchlist.json etc. so
    a column layout set once (locally or on the deployed app) is shared by
    everyone who opens the app, not just the browser session that set it."""
    return load_column_prefs_full().get("order")


def save_column_prefs(order):
    update_column_prefs("order", order)

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


REMEMBER_COOKIE_NAME = "swa_remember"
REMEMBER_STORAGE_KEY = "swa_remember"
REMEMBER_DAYS = 30


def _remember_signature(username, expiry_iso, secret):
    msg = f"{username}|{expiry_iso}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _make_remember_token(username, secret, days=REMEMBER_DAYS):
    """A signed, stateless 'remember me' token -- no server-side session
    store needed. Signed with the account password itself, so rotating the
    password automatically invalidates every outstanding 'stay signed in'
    cookie, everywhere, without needing to track/revoke sessions."""
    expiry_iso = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    sig = _remember_signature(username, expiry_iso, secret)
    return json.dumps({"u": username, "exp": expiry_iso, "sig": sig})


def _verify_remember_token(token_value, expected_username, secret):
    if not token_value:
        return False
    try:
        data = json.loads(token_value)
        username, expiry_iso, sig = data["u"], data["exp"], data["sig"]
    except Exception:
        return False
    if username != expected_username:
        return False
    try:
        expiry = datetime.fromisoformat(expiry_iso)
    except Exception:
        return False
    if datetime.now(timezone.utc) > expiry:
        return False
    expected_sig = _remember_signature(username, expiry_iso, secret)
    return hmac.compare_digest(sig, expected_sig)


def _query_param_token_key():
    return "_swa_t"


def _set_auth_token(token):
    """Persists the remember-me token on the device. Uses
    st.html(unsafe_allow_javascript=True) so the script runs in the app's own
    top-level document, writing both a real browser cookie and a localStorage
    entry. Community Cloud strips cookies from what the server sees
    (st.context.cookies is empty there), so the query param -- re-injected by
    _restore_stored_token() on later visits -- is the reliable channel there.
    A short delayed reload lets the JS write finish before the page reruns."""
    from urllib.parse import quote
    encoded = quote(token, safe="")
    js_token = json.dumps(token)
    st.html(
        f"<script>"
        f'document.cookie = "{REMEMBER_COOKIE_NAME}={encoded}; path=/; max-age={REMEMBER_DAYS * 86400}; SameSite=Lax";'
        f"try {{ localStorage.setItem({REMEMBER_STORAGE_KEY!r}, {js_token}); }} catch(e) {{}}"
        f"setTimeout(function() {{ window.location.reload(); }}, 150);"
        f"</script>",
        unsafe_allow_javascript=True,
    )
    st.session_state["_remember_token"] = token
    st.query_params[_query_param_token_key()] = token


def _clear_auth_token():
    """Clears the remember-me token from wherever it was stored."""
    if _query_param_token_key() in st.query_params:
        del st.query_params[_query_param_token_key()]
    st.session_state.pop("_remember_token", None)
    st.html(
        f"<script>"
        f'document.cookie = "{REMEMBER_COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax";'
        f"try {{ localStorage.removeItem({REMEMBER_STORAGE_KEY!r}); }} catch(e) {{}}"
        f"setTimeout(function() {{ window.location.reload(); }}, 150);"
        f"</script>",
        unsafe_allow_javascript=True,
    )


def _restore_stored_token():
    """Renders an invisible script that, on a fresh visit, re-injects a
    locally-stored remember-me token (localStorage or cookie) into the URL as
    ?_swa_t=... and reloads. Community Cloud strips cookies from the server's
    view, so the token is read back from the query param instead. Runs before
    the login form renders to minimize any login-screen flash."""
    qp_key = _query_param_token_key()
    st.html(
        "<script>"
        "(function() {"
        "  var tok = null;"
        f"  try {{ tok = localStorage.getItem({REMEMBER_STORAGE_KEY!r}); }} catch(e) {{}}"
        "  if (!tok) {"
        f"    var m = document.cookie.match(/(?:^|;\\s*){REMEMBER_COOKIE_NAME}=([^;]*)/);"
        "    if (m) tok = decodeURIComponent(m[1]);"
        "  }"
        f"  if (tok && !new URLSearchParams(window.location.search).has({qp_key!r})) {{"
        "    var sep = window.location.href.indexOf('?') === -1 ? '?' : '&';"
        "    window.location.replace(window.location.href + sep + "
        f"{qp_key!r} + '=' + encodeURIComponent(tok));"
        "  }"
        "})();"
        "</script>",
        unsafe_allow_javascript=True,
    )


def _get_stored_token(username, password):
    """Retrieves and validates the stored remember-me token from all
    possible locations: query params (the reliable channel on Cloud, re-
    injected from localStorage by _restore_stored_token), session state, and
    browser cookies (works locally; Community Cloud strips them server-side).
    Returns the token string if valid, or None."""
    from urllib.parse import unquote

    candidates = []

    # 1. Query param (primary channel on Streamlit Cloud)
    qp = st.query_params.get(_query_param_token_key())
    if qp:
        candidates.append(qp)

    # 2. Session state (in-session memory)
    ss = st.session_state.get("_remember_token")
    if ss:
        candidates.append(ss)

    # 3. Browser cookie (local dev; empty on Community Cloud)
    raw = st.context.cookies.get(REMEMBER_COOKIE_NAME)
    if raw:
        candidates.append(unquote(raw))

    for token in candidates:
        if _verify_remember_token(token, username, password):
            return token
    return None


def require_login():
    username, password = get_auth_credentials()
    if not username or not password:
        return  # no credentials configured anywhere -> open access
    if st.session_state.get("authenticated"):
        return

    # Check all stored token locations (cookie, query param, session state)
    token = _get_stored_token(username, password)
    if token:
        st.session_state.authenticated = True
        # Ensure token is also in session state for this session
        st.session_state["_remember_token"] = token
        return

    # No token seen yet. On Cloud the server can't read cookies, so if the
    # device still holds a saved token (localStorage/cookie), re-inject it
    # into the URL as ?_swa_t= and reload before showing the login form.
    _restore_stored_token()

    st.title("Stock Watchlist")
    st.subheader("Sign in")
    with st.form("login_form"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        remember = st.checkbox("Stay signed in on this device for 30 days", value=True)
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if u == username and p == password:
            st.session_state.authenticated = True
            if remember:
                token = _make_remember_token(username, password)
                _set_auth_token(token)
                # JS saves the token (cookie + localStorage) and reloads;
                # session state keeps this session authenticated meanwhile.
                st.stop()
            else:
                st.rerun()
        else:
            st.error("Incorrect username or password.")
    st.stop()


def render_logout_button():
    """Sidebar 'Log out' -- only shown once login is actually configured
    (get_auth_credentials returns real values), since there's nothing to
    log out of on an open/no-auth deployment."""
    username, password = get_auth_credentials()
    if not username or not password:
        return
    if not st.session_state.get("authenticated"):
        return
    if st.sidebar.button("Log out", width="stretch"):
        st.session_state.authenticated = False
        # The click already triggered this rerun; _clear_auth_token's JS
        # removes the token (cookie + localStorage) and reloads to login.
        _clear_auth_token()


require_login()

# ---------- helpers ----------


@st.cache_data(show_spinner="Fetching latest prices...")
def cached_fetch_all(refresh_nonce, watchlists_json, settings_json):
    # NOTE: refresh_nonce and settings_json must NOT be prefixed with "_" --
    # Streamlit's cache_data excludes underscore-prefixed params from the
    # cache key hash, which would silently break cache-busting (see
    # _bump_refresh() for why refresh_nonce -- a fresh uuid per refresh event,
    # not a small incrementing counter -- is used here at all: a counter that
    # predictably restarts at 0/1/2... per session causes different
    # sessions' first-ever refresh to collide on the same cache entry).
    # Settings are included in the key too, so changing calculation
    # parameters always forces a fresh fetch.
    watchlists = json.loads(watchlists_json)
    settings = json.loads(settings_json)
    combined, as_of, per_market = fetch_all_markets(watchlists, settings=settings)
    return as_of, per_market


def _bump_refresh():
    """Marks that this session should bypass the daily snapshot and do a live
    fetch (refresh_token), AND forces cached_fetch_all() to actually run a
    fresh fetch instead of reusing a cached result (refresh_nonce).

    These are two different jobs and need two different values: refresh_token
    only needs to flip away from 0 (checked via `== 0` elsewhere to decide
    snapshot vs. live-fetch), but using it ALSO as cached_fetch_all's
    cache-busting argument is a bug -- st.cache_data's cache is keyed on
    argument values only, not per-session, so two different users' (or two
    reloads') first-ever refresh both land on refresh_token=1 with identical
    watchlists/settings and collide onto the SAME cache entry. Whoever's
    fetch got cached first (even a transient rate-limited/bad one) then gets
    served to everyone else's "fresh" refresh too, indefinitely, until that
    exact (token, watchlists, settings) combination changes. refresh_nonce is
    a fresh uuid every time, so it can never collide with a previous refresh."""
    st.session_state.refresh_token += 1
    st.session_state.refresh_nonce = uuid.uuid4().hex


def _persist_and_serve(per_market, as_of, settings):
    """Writes a freshly-computed fetch result to data_snapshot.json AND stashes
    it in session state so the rerun following the action serves this exact
    result instead of fetching again. Called by the Refresh Data button and
    watchlist-save: those actions are the allowed live-fetch triggers, and the
    persisted snapshot also keeps later login/reloads clean."""
    save_data_snapshot(as_of, per_market, settings=settings)
    st.session_state["_served_snapshot"] = (as_of, per_market)
    st.session_state["last_refresh_summary"] = {
        "as_of": as_of,
        "per_market_counts": {mkt: len(rows) for mkt, rows in per_market.items()},
        "total": sum(len(rows) for rows in per_market.values()),
    }


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
    """Column-header labels for the 6 SMA slots, reflecting the currently
    configured periods (e.g. {'w_fast': '10 WEMA', ...})."""
    wf, wm, ws = settings["ema_weekly"]
    df_, dm, ds = settings["ema_daily"]
    return {
        "w_fast": f"{wf} WEMA", "w_mid": f"{wm} WEMA", "w_slow": f"{ws} WEMA",
        "d_fast": f"{df_} DSMA", "d_mid": f"{dm} DSMA", "d_slow": f"{ds} DSMA",
    }


_SUMMARY_TEXT_RE = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL)


def _plain_text(val):
    """Trend/Vol Trend/Tech Uptrend cells are wrapped in a <details><summary>
    disclosure (see with_tooltip) -- unwrap back to the plain label so the
    coloring logic below (which compares against exact label strings like
    "Strong Uptrend") keeps working."""
    if isinstance(val, str):
        m = _SUMMARY_TEXT_RE.search(val)
        if m:
            return html.unescape(m.group(1))
    return val


def style_row(row, ema_labels):
    styles = [""] * len(row)
    # If there are duplicate 'Last' columns, row["Last"] might be a Series.
    # Take the first one if that happens.
    last = row["Last"]
    if isinstance(last, pd.Series):
        last = last.iloc[0]
        
    ema_cols = set(ema_labels.values())
    for i, col in enumerate(row.index):
        # Access by index rather than label to avoid returning a Series
        # when duplicate column names exist in the DataFrame.
        val = row.iloc[i]
        if col in ("Trend", "Vol Trend", "Tech Uptrend", "Net Vol 10D"):
            val = _plain_text(val)
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
        elif col in ("% Chg", "Qtr Profit Growth %", "Qtr Revenue Growth %",
                     "Perf 1M %", "Perf 3M %", "Perf 6M %", "Perf 1Y %") and pd.notna(val):
            styles[i] = "color:#1e8449;font-weight:600" if val > 0 else ("color:#c0392b;font-weight:600" if val < 0 else "")
        elif col == "Vol Trend" and isinstance(val, str):
            vol_colors = {"Exploding": "#1e8449", "Declining": "#c0392b"}
            color = vol_colors.get(val)
            if color:
                styles[i] = f"color:{color};font-weight:600"
        elif col == "Tech Uptrend" and val == "Yes":
            styles[i] = "color:#1e8449;font-weight:700"
        elif col == "Net Vol 10D" and isinstance(val, str):
            net_colors = {"Positive": "#1e8449", "Negative": "#c0392b"}
            color = net_colors.get(val)
            if color:
                styles[i] = f"color:{color};font-weight:600"
        elif col == "Alerts" and isinstance(val, str) and val not in ("—", ""):
            styles[i] = "color:#8e44ad;font-weight:600"
        elif col == "Expert Take" and isinstance(val, str):
            if "Accumulate" in val:
                styles[i] = "color:#1e8449;font-weight:700"
            elif "Caution" in val:
                styles[i] = "color:#c0392b;font-weight:700"
            elif "Hold" in val:
                styles[i] = "color:#d4ac0d;font-weight:700"
            elif "Failed" in val:
                styles[i] = "color:#e67e22;font-weight:700"
    return styles


def _mark(passed):
    """✓/✗/n·a marker for a tri-state condition (True/False/None -- None
    means the condition wasn't evaluable, e.g. RS unavailable)."""
    if passed is None:
        return "n/a"
    return "✓" if passed else "✗"


def _note_preview(text, limit=40):
    """Short, table-cell-friendly preview of a ticker's free-text note --
    see with_tooltip, used right after this to show the full text on
    hover/tap when it's been truncated."""
    if not text:
        return "—"
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def with_tooltip(display_value, tooltip_text):
    """Wraps a cell's display text so the cell itself stays compact (just the
    label, e.g. "Uptrend") while the full breakdown is reachable two ways:
    a native hover tooltip (title=...) for desktop, and a click-to-expand
    <details>/<summary> disclosure for mobile/touch, where hover tooltips
    don't fire at all. <details>/<summary> need no CSS/JS -- important
    because Streamlit's markdown renderer strips <style>/<script> tags
    outright even with unsafe_allow_html=True (see sticky_header_html's
    docstring for the same constraint).

    IMPORTANT: the title attribute uses the `&#10;` entity instead of a raw
    "\\n" for line breaks. A literal blank line ("\\n\\n", which
    trend_tooltip's section separator produces) embedded in raw HTML makes
    Streamlit's/CommonMark's HTML-block parser treat the blank line as the
    end of the raw-HTML block -- everything after it then gets dumped back
    out as plain visible markdown text instead of staying hidden inside the
    attribute (this was the exact bug: the "Strong also needs BOTH..." tail
    spilling into the cell). Encoding newlines as an entity means the
    generated markup never contains a literal blank line, so it can't
    happen. The expandable body below uses real <br> tags instead (safe --
    those are tag content, not an attribute value)."""
    value_esc = html.escape(str(display_value))
    if not tooltip_text:
        return value_esc
    tooltip_esc = html.escape(tooltip_text)
    title_attr = tooltip_esc.replace("\n", "&#10;")
    body_html = tooltip_esc.replace("\n", "<br>")
    return (
        f'<details style="display:inline-block" title="{title_attr}">'
        f'<summary style="cursor:help">{value_esc}</summary>'
        f'<div style="font-size:11px;font-weight:400;line-height:1.5;'
        f'white-space:normal;margin-top:4px;">{body_html}</div>'
        f'</details>'
    )


def trend_tooltip(row, labels):
    """Builds the hover-tooltip text for a Trend cell: which of the 4 hard
    requirements passed/failed, plus the Strong criteria -- so you can see
    at a glance which condition is blocking a better/worse read."""
    detail = row.get("trend_detail")
    if not detail:
        return "Not enough weekly history yet to compute Trend."
    w_slow = labels["w_slow"]
    w_fast = labels["w_fast"]
    lines = ["Uptrend requires ALL of (else Downtrend):"]
    lines.append(f"{_mark(detail['price_above_ma'])} Price > {w_slow} ({detail['last_close']:.1f} vs {detail['last_ma']:.1f})")
    lines.append(f"{_mark(detail['slope_rising'])} {w_slow} slope rising ({detail['slope']:+.3f}/wk)")
    if detail.get("ema_aligned") is not None:
        lines.append(f"{_mark(detail['ema_aligned'])} {w_fast} > {w_slow} ({detail['ema_fast']:.1f} vs {detail['last_ma']:.1f})")
    if detail.get("rs_positive") is not None:
        lines.append(f"{_mark(detail['rs_positive'])} Weekly RS positive ({detail['rs_weekly']:+.1f})")
    lines.append(f"→ {detail['direction']}")
    lines.append("")
    lines.append("Strong also needs BOTH:")
    ref_price = detail["week52_high"] if detail["direction"] == "Uptrend" else detail["week52_low"]
    ref_label = "52W high" if detail["direction"] == "Uptrend" else "52W low"
    pct_label = f"{detail['near_high_low_pct'] * 100:.0f}%"
    if ref_price is not None:
        lines.append(f"{_mark(detail['near_high_low_pass'])} Within {pct_label} of {ref_label} ({detail['last_close']:.1f} vs {ref_price:.1f})")
    else:
        lines.append(f"n/a Within {pct_label} of {ref_label} (no 52W data)")
    if detail.get("avg_volume_10d") is not None and detail.get("avg_volume_100d") is not None:
        ratio = detail["avg_volume_10d"] / detail["avg_volume_100d"] if detail["avg_volume_100d"] else None
        ratio_str = f"{ratio:.2f}×" if ratio is not None else "n/a"
        lines.append(f"{_mark(detail['volume_rising'])} Vol 10D ≥ {detail['volume_ratio']}× Vol 100D ({ratio_str})")
    else:
        lines.append(f"n/a Vol 10D ≥ {detail['volume_ratio']}× Vol 100D (no volume data)")
    lines.append(f"→ {'Strong' if detail['strong'] else 'Not Strong'}")
    return "\n".join(lines)


def vol_trend_tooltip(row, settings):
    """Builds the hover-tooltip text for a Vol Trend cell: the actual 10D/100D
    ratio against both thresholds."""
    v10, v100 = row.get("avg_volume_10d"), row.get("avg_volume_100d")
    explode = settings.get("volume_explode_ratio", 1.4)
    decline = settings.get("volume_decline_ratio", 0.7)
    if v10 is None or v100 is None or not v100:
        return "Not enough volume history yet to compute Vol Trend."
    ratio = v10 / v100
    lines = [
        f"Vol 10D ÷ Vol 100D = {v10:,.0f} ÷ {v100:,.0f} = {ratio:.2f}×",
        f"Exploding needs ≥ {explode}×",
        f"Declining needs ≤ {decline}×",
        f"→ {row.get('volume_trend') or '—'}",
    ]
    return "\n".join(lines)


def tech_uptrend_tooltip(row, settings, labels):
    """Builds the hover-tooltip text for a Tech Uptrend cell: each of the 4
    requirements and whether it passed."""
    vstop = row.get("vstop_weekly")
    weeks_since = row.get("vstop_weekly_weeks_since_change")
    ema40 = row.get("ema40")
    last_close = row.get("last_close")
    v10, v100 = row.get("avg_volume_10d"), row.get("avg_volume_100d")
    min_weeks = settings.get("tech_uptrend_min_vstop_weeks", 3)
    vol_ratio = settings.get("tech_uptrend_volume_ratio", 1.4)
    w_slow = labels["w_slow"]

    if vstop is None or weeks_since is None or ema40 is None or v10 is None or v100 is None:
        return "Not enough data yet to compute Tech Uptrend."

    close_above_vstop = last_close > vstop
    held_long_enough = weeks_since > min_weeks
    close_above_wema = last_close > ema40
    vol_surging = v10 > vol_ratio * v100
    ratio = v10 / v100 if v100 else None

    lines = [
        "Tech Uptrend requires ALL of:",
        f"{_mark(close_above_vstop)} Close > Weekly VStop ({last_close:.1f} vs {vstop:.1f})",
        f"{_mark(held_long_enough)} Held > {min_weeks} weeks since VStop flip ({weeks_since} weeks)",
        f"{_mark(close_above_wema)} Close > {w_slow} ({last_close:.1f} vs {ema40:.1f})",
        f"{_mark(vol_surging)} Vol 10D ≥ {vol_ratio}× Vol 100D ({ratio:.2f}× )" if ratio is not None else f"{_mark(vol_surging)} Vol 10D ≥ {vol_ratio}× Vol 100D",
        f"→ {'Yes' if row.get('tech_uptrend') else 'No'}",
    ]
    return "\n".join(lines)


# Sentinels the Expert Take search stage writes when it found nothing. An empty
# news_used means the same thing. Kept as a helper so the enrichment loop and
# the cell tooltip agree on what "no news behind this verdict" means.
_EXPERT_NO_NEWS = ("no recent news found", "no news found", "no material news found", "nothing")


def _expert_view_has_news(view):
    text = str((view or {}).get("news_used") or "").strip().lower().rstrip(".")
    if not text:
        return False
    return not any(text.startswith(m) for m in _EXPERT_NO_NEWS)


# Expert Take dropdown label -> the value row["expert_take"] carries. "Any" maps
# to None, which never equals a row value, and is short-circuited before the
# lookup anyway. Kept as one mapping so the option list and the comparison
# cannot drift apart.
EXPERT_FILTER_VALUES = {
    "Any": None,
    "🟢 Accumulate": "Accumulate",
    "🟡 Hold": "Hold",
    "🔴 Caution": "Caution",
    "⚪ Pending": "Pending",
}


def sentiment_flag_note(flag, as_of):
    """Plain-English note for a _validate_sentiment guard flag, or "" when the
    view passed clean. Shared by the Sentiment cell tooltip and the AI-review
    payload so the two can't drift apart."""
    if flag == "STALE":
        return f"[STALE: as_of {as_of} is older than {SENTIMENT_STALE_DAYS} days]"
    if flag == "STALE_QUARTER":
        return "[STALE_QUARTER: a confirmed earnings report exists that the news used didn't verifiably account for]"
    if flag == "NO_DATA":
        return "[NO_DATA: earnings, guidance and analyst coverage are all N/A]"
    if flag == "PARTIAL":
        return "[PARTIAL: only revenue/soft data — no EPS, guidance or analyst action; capped at Neutral]"
    return ""


# The deterministic guard _validate_sentiment applies on top of whatever the
# model wrote. Spelled out for the AI-review payload so the reader knows the
# displayed Sentiment isn't raw model output.
SENTIMENT_GUARD_RULES = f"""A deterministic guard runs after the model answers and can override it:
- View older than {SENTIMENT_STALE_DAYS} days -> Unknown (STALE)
- Earnings, guidance and analyst coverage all missing/N/A -> Unknown (NO_DATA)
- A confirmed earnings report the news didn't account for -> Unknown (STALE_QUARTER)
- Positive/Negative without hard evidence (no EPS figure, no guidance change, no
  analyst action) -> capped at Neutral (PARTIAL)"""

AI_REVIEW_INSTRUCTION = (
    "Do a thorough analysis from an investment perspective, considering valuation, "
    "technicals, future guidance etc."
)

# Always included in the AI review payload's Metrics section, regardless of
# which columns are currently toggled visible in the watchlist table. The
# table's own default_hidden set (build_column_defs) exists to keep the
# table narrow -- "convenient to look at" -- which is a different goal from
# "what a thorough investment review needs", so a column toggled off the
# table to save space shouldn't silently vanish from every AI review too.
# Every key here already has a real entry in column_definitions(), so no
# glossary work is needed alongside this list.
AI_REVIEW_CORE_KEYS = [
    "ema200", "rsi14_daily", "rs_daily", "rs_monthly", "adx_weekly_14",
    "overhead_supply", "week52_high_age", "week52_high", "week52_low",
    "high_5y", "high_5y_distance", "rel_ret_1m_n500", "rel_ret_6m_n500",
]


def _fmt_ai_value(v):
    """Render a cell value for the payload: trim float noise, drop empties."""
    if v is None or v == "" or v == "—":
        return None
    if isinstance(v, float):
        if pd.isna(v):
            return None
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def build_ai_review_payload(
    rows, *, market, settings, labels, visible_keys, label_by_key, definitions,
    export_value, expert_views, fundamentals, alert_hits, metric_labels, rule_by_id,
):
    """One markdown blob describing the selected tickers, for pasting into a chat.

    Per-ticker sections carry values, the signal breakdowns that are otherwise
    only reachable by hovering a cell, the alert rules that actually fired, and
    the stored Expert Take / Sentiment text. Everything that is identical across
    tickers -- column definitions and the rules governing Expert Take and
    Sentiment -- is hoisted into a single reference section at the end, so
    selecting five tickers doesn't repeat ~7KB of boilerplate five times.
    """
    show_expert = "expert_take" in visible_keys
    show_sentiment = "fundamentals" in visible_keys
    out = []

    # Metrics/definitions cover visible_keys PLUS the fixed AI_REVIEW_CORE_KEYS
    # set (see its docstring) -- order: visible columns first (matches what
    # you're already looking at in the table), then the core additions not
    # already covered.
    metric_keys = list(visible_keys) + [k for k in AI_REVIEW_CORE_KEYS if k not in visible_keys]
    # Metrics a triggered alert's own conditions reference (e.g. "Alpha
    # Leaders" testing ADX-M) aren't necessarily in metric_keys -- their live
    # values already appear inline in the "Triggered alerts" text below, but
    # without this they'd have no definition anywhere in the payload, and
    # the AI would be left guessing what e.g. "ADX-M[32.5]" even measures.
    referenced_alert_metrics = []

    if len(rows) == 1:
        r = rows[0]
        who = f"{r.get('company_name') or r['ticker']} ({r['ticker']})"
    else:
        who = f"the following {len(rows)} companies: " + ", ".join(
            f"{r.get('company_name') or r['ticker']} ({r['ticker']})" for r in rows
        )
    out.append(f"Included data captures metrics for {who}. {AI_REVIEW_INSTRUCTION}\n")

    out.append("# Ticker data")
    for r in rows:
        t = r["ticker"]
        out.append(f"\n## {t} — {r.get('company_name') or t}")
        meta = [f"Market: {market}"]
        if r.get("last_close") is not None:
            meta.append(f"Last close: {_fmt_ai_value(r['last_close'])}")
        if r.get("data_end"):
            meta.append(f"Price data through: {r['data_end']}")
        out.append(" · ".join(meta))

        out.append("\n### Metrics")
        for k in metric_keys:
            val = _fmt_ai_value(export_value(r, k))
            if val is not None:
                out.append(f"- **{label_by_key[k]}**: {val}")

        breakdowns = []
        if "trend" in visible_keys:
            breakdowns.append(("Trend", trend_tooltip(r, labels)))
        if "volume_trend" in visible_keys:
            breakdowns.append(("Vol Trend", vol_trend_tooltip(r, settings)))
        if "tech_uptrend_label" in visible_keys:
            breakdowns.append(("Tech Uptrend", tech_uptrend_tooltip(r, settings, labels)))
        if breakdowns:
            out.append("\n### Signal breakdowns")
            for name, text in breakdowns:
                out.append(f"\n**{name}**\n{text}")

        hits = alert_hits.get(t) or []
        if hits:
            out.append("\n### Triggered alerts")
            for p in hits:
                out.append(f"\n**{p['rule_name'] or '(unnamed)'}**")
                out.append(describe_chain_with_values(
                    p["row"], p["conditions"], metric_labels, rule_by_id))
                for mkey in _metrics_used_in_conditions(p["conditions"]):
                    if mkey not in referenced_alert_metrics:
                        referenced_alert_metrics.append(mkey)

        if show_expert:
            v = expert_views.get(t) or {}
            out.append("\n### Expert Take")
            if is_pending_view(v):
                # A placeholder record stores verdict "HOLD", so the old
                # `if v.get("verdict")` branch reported a failed generation to
                # the reader as a real Hold verdict.
                out.append("No usable analysis stored -- the last generation failed "
                           f"({v.get('headline', '')}).")
            elif v.get("verdict"):
                # Guarded verdict, not the raw stored one, so the payload agrees
                # with the table -- same reasoning as the Sentiment block below.
                verdict, vflag = validate_verdict(v, r)
                out.append(f"Verdict: {verdict} — {v.get('headline', '')}"
                           + (f" ({vflag})" if vflag else ""))
                note = verdict_flag_note(vflag, v.get("as_of", "unknown"))
                if note:
                    out.append(note)
                for fld in ("technical_summary", "catalyst_summary", "actionable_take"):
                    if v.get(fld):
                        out.append(f"- {fld.replace('_', ' ').title()}: {v[fld]}")
                out.append(f"(model: {v.get('model_used', '?')}, as of {v.get('as_of', '?')})")
            else:
                out.append("No AI analysis stored for this ticker yet.")

        if show_sentiment:
            v = fundamentals.get(t) or {}
            sentiment, flag = _validate_sentiment(v)
            out.append("\n### Sentiment")
            out.append(f"Sentiment: {sentiment}" + (f" ({flag})" if flag else ""))
            note = sentiment_flag_note(flag, v.get("as_of", "unknown"))
            if note:
                out.append(note)
            for lbl, fld in (("Earnings", "earnings_summary"), ("Guidance", "future_guidance"),
                             ("Analyst Coverage", "analyst_coverage"), ("Reasoning", "reasoning")):
                if v.get(fld):
                    out.append(f"- {lbl}: {v[fld]}")
            if v.get("as_of"):
                out.append(f"(model: {v.get('model_used', '?')}, as of {v['as_of']})")

    out.append("\n\n# Reference — how to read the above")
    out.append("\n## Column definitions")
    definition_keys = metric_keys + [k for k in referenced_alert_metrics if k not in metric_keys]
    for k in definition_keys:
        lbl = label_by_key.get(k)
        if not lbl:
            continue
        d = definitions.get(lbl)
        if d:
            out.append(f"- **{lbl}**: {d}")
    if show_expert:
        out.append('\n## How "Expert Take" is decided')
        out.append("An LLM assigns the verdict under these rules:\n")
        out.append(VERDICT_RULES)
        out.append("")
        out.append(VERDICT_GUARD_RULES)
    if show_sentiment:
        out.append('\n## How "Sentiment" is decided')
        out.append("An LLM reads recent earnings/guidance/analyst news and returns "
                   "Positive / Neutral / Negative / Unknown.\n")
        out.append(SENTIMENT_GUARD_RULES)

    return "\n".join(out)


def price_cols(ema_labels):
    """Price-denominated columns -- shown as whole numbers (no decimal),
    since sub-dollar/rupee precision isn't meaningful at a glance here."""
    return ["Last", ema_labels["w_fast"], ema_labels["w_mid"], ema_labels["w_slow"],
            ema_labels["d_fast"], ema_labels["d_mid"], ema_labels["d_slow"],
            "VStop-W", "VStop-W (14)", "52W High", "52W Low", "5Y High"]


def ratio_cols():
    """Oscillator/ratio columns -- kept at 1 decimal (whole numbers would
    lose meaningful resolution for RSI/RS reads)."""
    return ["RSI-D", "RSI-W", "RSI-M", "RSI-M (12)", "ADX-W", "ADX-M",
            "RS-D", "RS-W", "RS-M", "P/E (TTM)", "P/E (Fwd)", "P/B", "EV/EBITDA", "P/Cashflow"]


VOLUME_COLS = ["Vol 10D", "Vol 20D", "Vol 100D"]
PCT_COLS = ["% Chg", "Qtr Profit Growth %", "Qtr EPS Growth %", "Qtr Revenue Growth %", "ROE %", "ROCE %",
            "PAT Growth TTM %", "Revenue Growth TTM %"]
PERF_PCT_COLS = ["Perf 1M %", "Perf 3M %", "Perf 6M %", "Perf 1Y %", "Perf 3Y %"]
# Signed like PERF_PCT_COLS (+ = ahead of the benchmark) but kept at 1
# decimal, not 0: these are scan inputs tested against thresholds as tight
# as ">= 1", where PERF_PCT_COLS' whole-number rounding would render a 0.4
# and a 1.4 identically.
REL_PCT_COLS = ["1M Ret vs Nifty 500", "6M Ret vs Nifty 500"]
# Unsigned percentages -- always >= 0, so formatted WITHOUT a sign prefix.
# PCT_COLS' "+12.5%" would read as 12.5% ABOVE the high for the distances,
# the exact opposite of what they mean, and a signed share-of-volume makes
# no sense either.
DIST_PCT_COLS = ["26WH Distance", "52WH Distance", "Overhead Supply", "5Y High Distance"]
# A count of trading days, not a price or a ratio -- whole numbers only.
COUNT_COLS = ["Breakout Window", "52W High Age"]
# Stored as a raw ratio (e.g. 0.85), unlike PCT_COLS' ROE %/ROCE % which are
# pre-multiplied by 100 at computation time (stock_data.py) -- formatted here
# with Python's native "%" presentation type so the display multiplies by
# 100 without touching the underlying stored value (any future alert/filter
# threshold against cfo_op_5yr, e.g. ">= 1", stays meaningful in that scale).
RATIO_PCT_COLS = ["CFO/OP 5Y"]

# Column keys considered "fundamental" (company financials/valuation, as
# opposed to technical/price-derived) -- hideable as a group via the
# "Show fundamental columns" toggle, independent of the per-column picker.
FUNDAMENTAL_COLUMN_KEYS = {"fundamentals", "qtr_profit_growth", "qtr_eps_growth", "qtr_revenue_growth", "ttm_profit_growth", "ttm_revenue_growth", "trailing_pe", "forward_pe", "pb_ratio", "ev_ebitda", "p_cashflow", "reported_qtr", "roe", "cfo_op_5yr", "roce"}


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

# The Ticker column (always first, since the index is hidden) is additionally
# pinned horizontally so it stays visible while scrolling right through the
# wide table. The header's Ticker cell sticks on BOTH axes at once (it's the
# top-left corner), so it needs its own variant layered over the header row
# variant above, with a higher z-index since it overlaps both the sticky
# header row and the sticky Ticker column. Body-cell Ticker entries only need
# the left axis. Both get a right-edge box-shadow so the column reads as
# visually separate from whatever's scrolled underneath it.
STICKY_TH_CORNER_STYLE = (
    "position:sticky;top:60px;left:0;z-index:3;"
    "background-color:var(--background-color, #ffffff);"
    "box-shadow:0 1px 0 rgba(128,128,128,0.4), 2px 0 2px -1px rgba(128,128,128,0.3);"
    "text-align:left;padding:6px 10px;font-size:13px;"
)
STICKY_TD_TICKER_STYLE = (
    "position:sticky;left:0;z-index:1;"
    "background-color:var(--background-color, #ffffff);"
    "box-shadow:2px 0 2px -1px rgba(128,128,128,0.3);"
)


def sticky_header_html(styler):
    """Renders a pandas Styler as an HTML table with a sticky header AND a
    sticky (horizontally pinned) Ticker column, embedded directly in the page
    (no nested scroll box -- the browser's normal page scroll handles it).

    Streamlit's markdown/HTML renderer strips <style> tags outright (along
    with <pre>/<script>/<textarea>) even with unsafe_allow_html=True -- this
    is a fixed list in its own markdown parser, confirmed in Streamlit's
    frontend source, not a guess. That's why a <style>-block-based sticky
    header (via Styler.set_table_styles()) silently does nothing. Inline
    style="..." *attributes* are a different thing and are NOT stripped, so
    all sticky CSS here (header row AND Ticker column) is injected directly
    onto each <th>/<td> tag via regex instead of via a <style> block.

    Since the index is hidden, the Ticker column is always the first cell of
    every row -- <th> in the header row, <td> in every body row -- so
    "first cell in this row" is a reliable, order-based way to target it
    without needing to know pandas' generated column id/class names."""
    html = styler.to_html(escape=False)
    html = re.sub(r"<th\b", f'<th style="{STICKY_TH_STYLE}"', html)
    # The very first <th> emitted (Ticker's header cell) additionally sticks
    # left -- swap it for the corner variant. count=1 targets only that one.
    html = re.sub(re.escape(f'<th style="{STICKY_TH_STYLE}"'), f'<th style="{STICKY_TH_CORNER_STYLE}"', html, count=1)

    def _stick_first_td(row_match):
        return re.sub(r"<td\b", f'<td style="{STICKY_TD_TICKER_STYLE}"', row_match.group(0), count=1)

    return re.sub(r"<tr\b.*?</tr>", _stick_first_td, html, flags=re.DOTALL)


def column_definitions(settings, labels):
    """label -> plain-language definition, for the header info-icon hover
    tooltip. Bench/period/threshold numbers are pulled from `settings` so
    the tooltip always reflects your current configuration, not defaults."""
    bench_note = "your configured benchmark"
    defs = {
        "Ticker": "Click to open this symbol's chart on TradingView.",
        "Company Name": "Full name of the company or ETF.",
        "Index": "Benchmark or index the stock belongs to (e.g. S&P 500, Nasdaq 100, Nifty 500).",
        "Last": "Most recent daily closing price.",
        labels["w_fast"]: f"Weekly EMA, fast period ({settings['ema_weekly'][0]} weeks).",
        labels["w_mid"]: f"Weekly EMA, medium period ({settings['ema_weekly'][1]} weeks).",
        labels["w_slow"]: f"Weekly EMA, slow period ({settings['ema_weekly'][2]} weeks). The 'slow WEMA' referenced by Trend and Tech Uptrend.",
        labels["d_fast"]: f"Daily SMA, fast period ({settings['ema_daily'][0]} days).",
        labels["d_mid"]: f"Daily SMA, medium period ({settings['ema_daily'][1]} days).",
        labels["d_slow"]: f"Daily SMA, slow period ({settings['ema_daily'][2]} days).",
        "RSI-D": f"Daily RSI, {settings['rsi_period']}-period. ≤30 oversold, ≥70 overbought.",
        "RSI-W": f"Weekly RSI, {settings['rsi_period']}-period. ≤30 oversold, ≥70 overbought.",
        "RSI-M": f"Monthly RSI, {settings['rsi_period']}-period. ≤30 oversold, ≥70 overbought.",
        "RS-D": f"Mansfield RS (daily) vs {bench_note}. Positive = outperforming, negative = underperforming.",
        "RS-W": f"Mansfield RS (weekly) vs {bench_note}. Positive = outperforming, negative = underperforming.",
        "RS-M": f"Mansfield RS (monthly) vs {bench_note}. Positive = outperforming, negative = underperforming.",
        "VStop-W": (f"Weekly Volatility Stop (ATR stop-and-reverse, length={settings['vstop_length']}, factor={settings['vstop_factor']}). "
                    f"Engine: {'TradingView-exact (Source=close)' if settings.get('vstop_mode', 'tv') == 'tv' else 'legacy app formula'}."),
        "VStop Dir": "Current direction of the weekly VStop: Up or Down.",
        "VStop Weeks Ago": "Weeks since the weekly VStop last flipped direction.",
        "Trend": (
            "Strong Uptrend / Uptrend / Downtrend / Strong Downtrend. Uptrend requires ALL of: price above "
            f"slow WEMA, slow WEMA slope rising over {settings.get('trend_slope_lookback', 3)} weeks, fast "
            "WEMA above slow WEMA, and weekly RS positive (when available) -- no partial credit, anything "
            "short of unanimous is Downtrend. Strong additionally needs price within "
            f"{settings.get('trend_near_high_low_pct', 0.10) * 100:.0f}% of the 52W high/low AND 10D avg "
            f"volume ≥ {settings.get('trend_volume_ratio', 1.0)}× the 100D avg. Hover a cell for the "
            "per-condition breakdown."
        ),
        "Alerts": "Numbers of the enabled alert/scan rules currently matching this ticker -- see the legend below the table.",
        "% Chg": "1-day close-to-close percent change.",
        "52W High": "Trailing 52-week (~252 trading day) intraday high.",
        "52W Low": "Trailing 52-week (~252 trading day) intraday low.",
        "Breakout Window": (
            "Trading days back to the level price is now testing — i.e. how old the overhead "
            "resistance is. 250 means price is back at a level it last saw ~250 days ago. "
            "Resistance is measured against 105% of today's close, not today's close, so a minor "
            "overshoot part-way through a base doesn't reset the window: the count runs past every "
            "close within 5% above today, back to the last one that genuinely exceeded it. A close "
            "below today was never overhead, so it never interrupts the count. It does NOT skip "
            "over a >5% spike to reach an older level — a stock that peaked 7% above today last "
            "week reads a few days, because that peak is live overhead rather than a stale level. "
            "0 means no prior close was ever this high (blue sky, nothing left to break out of), "
            "which is what an all-time high reads. This is a SETUP measure, not a confirmation: a "
            "stock that actually clears its old level drops to 0 that same day. Looks back at most "
            "5 years. Powers the Long-term breakout scan (≥200 with 52WH Distance <10%) and the "
            "Short-term one (40–200 with 26WH Distance <10%)."
        ),
        "Overhead Supply": (
            "Percent of the last year's TRADED VOLUME that changed hands at closes above today's "
            "price — i.e. how much stock is underwater and liable to sell into a rally. 0 means "
            "nothing above is trapped; 30 means roughly a third of a year's turnover is sitting on "
            "a loss overhead. Weighs shares, not sessions, so a heavy distribution day counts for "
            "far more than a quiet drift day. Complements Breakout Window rather than repeating it: "
            "that gives the AGE of the nearest barrier, this gives the WEIGHT of all of it — "
            "WINDLAS.NS shows a 190-day window but ~30% supply because ten more levels sit above, "
            "while UFBL.NS has twelve levels above yet ~0% because all of them predate the year."
        ),
        "52W High Age": (
            "Trading days since the 52-week high was SET — 0 means the high is today's bar. Pairs "
            "with 52WH Distance to tell apart two setups that otherwise look identical: a small "
            "age means the stock is making new highs right now (breaking out), while a large age "
            "means it is climbing back to a high set months ago (still approaching). Uses the same "
            "trailing 252-day intraday High that 52W High and 52WH Distance use, so they always "
            "agree on which bar the high is. If several bars share the high, reports the oldest."
        ),
        "26WH Distance": (
            "Percent BELOW the trailing 26-week (~126 trading day) intraday high, as a POSITIVE "
            "number: 0 = sitting at the high, 12.5 = 12.5% below it. Positive on purpose so scans "
            "read literally as '26WH Distance < 10'."
        ),
        "52WH Distance": (
            "Percent BELOW the trailing 52-week (~252 trading day) intraday high, as a POSITIVE "
            "number: 0 = sitting at the high, 12.5 = 12.5% below it. Positive on purpose so scans "
            "read literally as '52WH Distance < 10'. This is the same distance the '% Off 52W High' "
            "custom column reports as a negative (-12.5%), so showing both gives you one number "
            "twice with opposite signs — they can disagree by up to ~0.2pp on low-priced stocks, "
            "because this metric uses the exact close while that column recomputes from the "
            "rounded stored close."
        ),
        "Data Thru": "Most recent date with price data for this ticker. Shown in red if 3+ days stale.",
        "Net Vol 10D": (
            "Sums each of the last 10 trading days' volume as UP-volume (close higher than the "
            "prior day) or DOWN-volume (close lower). Positive means more volume traded on up "
            "days than down days over that window, Negative the reverse. Hover a cell for the "
            "exact net/total ratio."
        ),
        "Vol Trend": (
            f"Exploding: 10D avg volume ≥ {settings.get('volume_explode_ratio', 1.4)}× the 100D avg. "
            f"Declining: ≤ {settings.get('volume_decline_ratio', 0.7)}× the 100D avg. Otherwise In-line. "
            "Hover a cell for the actual ratio."
        ),
        "Tech Uptrend": (
            "Yes only if ALL of: close > weekly VStop, VStop held its direction for more than "
            f"{settings.get('tech_uptrend_min_vstop_weeks', 3)} weeks, close > slow WEMA, and 10D avg volume "
            f"≥ {settings.get('tech_uptrend_volume_ratio', 1.4)}× the 100D avg. Hover a cell for the "
            "per-condition breakdown."
        ),
        "Vol 10D": "Average daily share volume over the last 10 trading days.",
        "Vol 100D": "Average daily share volume over the last 100 trading days.",
        "Flag": (
            "A colored marker, also shown next to the ticker symbol itself. Manually set via the sidebar "
            "'Ticker Notes' panel always wins; otherwise auto-computed from a vote across Expert Take, "
            "Trend, Tech Uptrend, and Sentiment -- Green needs 3+ of 4 bullish, Red needs 3+ of 4 bearish, "
            "and a strong contradicting signal downgrades either to Yellow ('further study'). Hover a "
            "flagged cell for the exact vote/veto breakdown."
        ),
        "Expert News?": (
            "Whether the Expert Take verdict had any news behind it. \"No\" means the grounded search "
            "returned nothing for this ticker, so the verdict is a technicals-only read -- still valid "
            "(the rules say absent news leans Hold), but not news-informed. Roughly 30% of verdicts are "
            "technicals-only on a typical day."
        ),
        "Notes": "Your free-text note for this ticker, set via the sidebar 'Ticker Notes' panel. Hover/tap a truncated note to see the full text.",
        "Interested": "Whether you ticked this ticker as Interested in the watchlist editor.",
        "Sentiment": "AI fundamental sentiment (Positive / Neutral / Negative) from the most recent earnings, guidance and analyst coverage. 'Unknown' means the view is stale, predates a confirmed earnings report, or had no hard evidence to stand on -- not that sentiment is neutral.",
        "Qtr Profit Growth %": "Year-over-year net income growth for the most recent reported quarter, vs. the same quarter a year ago (Yahoo Finance). Ignores share count -- compare against Qtr EPS Growth % to spot dilution.",
        "Qtr EPS Growth %": "Year-over-year growth in DILUTED earnings per share for the most recent reported quarter, vs. the same quarter a year ago. Same profit figure as Qtr Profit Growth % but divided by share count, so growth funded by issuing equity (QIP, warrant conversion) shows up lower here -- a materially smaller number than Qtr Profit Growth % means shareholders were diluted. Blank when Yahoo has fewer than 5 quarters of statements or the year-ago quarter was loss-making (growth undefined), which is why it is sparser than Qtr Profit Growth %.",
        "Qtr Revenue Growth %": "Year-over-year revenue growth for the most recent reported quarter, vs. the same quarter a year ago (Yahoo Finance).",
        "P/E (TTM)": "Trailing 12-month Price to Earnings ratio (Yahoo Finance).",
        "P/E (Fwd)": "Forward Price to Earnings ratio based on analyst estimates for the next fiscal year (Yahoo Finance).",
        "P/B": "Price to Book ratio (Yahoo Finance).",
        "EV/EBITDA": "Enterprise Value to EBITDA ratio (Yahoo Finance).",
        "P/Cashflow": "Calculated as Market Cap divided by Operating Cash Flow (Yahoo Finance).",
        "ROE %": "Return on Equity: Net Income / Shareholders Equity as a percentage (Yahoo Finance TTM).",
        "CFO/OP 5Y": "Cash Flow from Operations divided by Operating Income, summed over up to 5 fiscal years. Values > 1 indicate high earnings quality (the company converts more than its reported profit into actual cash). From yfinance annual statements.",
        "ROCE %": "Return on Capital Employed: Operating Income / (Stockholders Equity + Long-term Debt) as a percentage, using the most recent annual figures from yfinance.",
        "Reported Qtr": "The most recent quarter for which the company reported earnings and revenue growth, mapped to the local financial year (Yahoo Finance).",
        "Perf 1M %": "Price return over the past ~1 month (22 trading days).",
        "Perf 3M %": "Price return over the past ~3 months (63 trading days).",
        "Perf 6M %": "Price return over the past ~6 months (126 trading days).",
        "Perf 1Y %": "Price return over the past ~1 year (252 trading days).",
        "Perf 3Y %": "Price return over the past ~3 years (756 trading days). Blank when fewer than 3 years of daily data are available.",
        "5Y High": "Highest intraday high over the full 5 years of daily history. Sits at or above 52W High by definition, and the gap between the two is what separates a stock at a 1-year high from one at a genuine multi-year high.",
        "5Y High Distance": "How far below the 5-year high price is now, as a positive percent: 0 = at the 5-year high, 20 = 20% below it. Same sign convention as 26WH/52WH Distance (and the opposite of the '% Off 52W High' custom column).",
        "Vol 20D": "Average daily volume over the last 20 sessions. A short-window baseline: paired with Vol 10D it detects a volume surge that Vol 100D would already have absorbed.",
        "ADX-W": "Average Directional Index on WEEKLY bars, Wilder period 14. Measures trend STRENGTH only and says nothing about direction — 25+ is the classic 'trending' threshold, below 20 is chop. Needs 28 weekly bars before it reports anything.",
        "ADX-M": "Average Directional Index on MONTHLY bars, Wilder period 12. Same strength-not-direction reading as ADX-W but over a multi-year horizon. Needs 24 monthly bars.",
        "RSI-M (12)": "Monthly RSI at period 12, kept separate from RSI-M (period 14). On the ~60 monthly bars most tickers have, the two periods differ by several points, so they are not interchangeable.",
        "VStop-W (14)": "Weekly Volatility Stop at length 14, factor 2 — the same engine and settings as VStop-W, which uses length 10. A longer length sits further from price and flips less often.",
        "1M Ret vs Nifty 500": "Stock's 1-month return minus the benchmark's, in percentage points. Positive = outperforming. Measured over a date-aligned calendar, so both sides span exactly the same sessions.",
        "6M Ret vs Nifty 500": "Stock's 6-month return minus the benchmark's, in percentage points. Positive = outperforming.",
        "PAT Growth TTM %": "Net income over the trailing four quarters versus the four before that — needs eight quarters of income-statement history. CURRENTLY BLANK FOR EVERY TICKER: yfinance returns only about five quarters, so the comparison cannot be made. Use Qtr Profit Growth % (single quarter, year-on-year) instead; this column populates automatically if a deeper data source is ever wired in.",
        "Revenue Growth TTM %": "Total revenue over the trailing four quarters versus the four before that — needs eight quarters of income-statement history. CURRENTLY BLANK FOR EVERY TICKER: yfinance returns only about five quarters. Use Qtr Revenue Growth % instead; this column populates automatically if a deeper data source is ever wired in.",
    }
    return defs


def get_all_filterable_metrics(settings, custom_columns=None):
    """Built-in metrics (stock_data.get_filterable_metrics) PLUS any
    enabled user-defined custom columns, as one label->key dict -- used
    everywhere the custom filter builder and alert condition builder pick
    a metric, so a custom column becomes usable in filters/alerts the
    moment it's created, with zero special-casing at those call sites."""
    metrics = dict(get_filterable_metrics(settings))
    for col in (custom_columns if custom_columns is not None else load_custom_columns()):
        if col.get("enabled", True):
            metrics[col["name"]] = column_key(col)
    return metrics


def custom_column_tooltips(custom_columns=None):
    """label -> tooltip text for custom columns, merged into
    column_definitions()'s dict so the header info-icon works for these
    too -- just shows the formula, since that's the whole definition."""
    tooltips = {}
    for col in (custom_columns if custom_columns is not None else load_custom_columns()):
        if col.get("enabled", True):
            tooltips[col["name"]] = f"Custom column. Formula: {col.get('formula', '')}"
    return tooltips


def metric_definitions(settings, labels, custom_columns=None):
    """metric field key -> definition, for the surfaces that pick a METRIC
    (the condition builders, the sidebar glossary) rather than a table
    column.

    column_definitions() is keyed by COLUMN LABEL, and several metrics carry
    a different label in each place -- "Last Close" vs "Last", "10 WEMA" vs
    labels["w_fast"] -- so this bridges through build_column_defs' key->label
    map instead of matching label strings, which would silently lose those
    metrics and break again on any future label rename.

    A metric with no definition is simply ABSENT from the result; callers
    must render nothing for it rather than treat it as an error."""
    col_defs = column_definitions(settings, labels)
    _, label_by_key, *_ = build_column_defs(labels, custom_columns)
    out = {key: col_defs[lbl] for key, lbl in label_by_key.items() if lbl in col_defs}

    # Second pass by metric label, for metrics whose FIELD KEY differs from
    # the column key backing them -- "Tech Uptrend" is the metric tech_uptrend
    # but the column tech_uptrend_label, and "VStop Weeks Ago" is
    # vstop_weekly_weeks_since_change vs the column vstop_change. Both already
    # have definitions; keying only on the column key silently dropped them.
    for label, key in get_all_filterable_metrics(settings, custom_columns).items():
        if key not in out and label in col_defs:
            out[key] = col_defs[label]

    # Ticker/Last are mandatory columns and so aren't in build_column_defs,
    # but last_close IS a filterable metric -- map it explicitly.
    if col_defs.get("Last"):
        out.setdefault("last_close", col_defs["Last"])
    for col in (custom_columns if custom_columns is not None else load_custom_columns()):
        if col.get("enabled", True):
            out[column_key(col)] = f"Custom column. Formula: {col.get('formula', '')}"
    return {k: v for k, v in out.items() if v}


def add_header_tooltips(html_str, definitions):
    """Appends a small 'ⓘ' info icon next to each column header found in
    `definitions`. Same <details>/<summary> technique as with_tooltip (see
    its docstring): a native title= gives a hover tooltip on desktop, and
    the <details> disclosure makes it tappable on mobile/touch, where hover
    never fires -- a plain hover-only <span title=...> (the previous
    approach) worked on desktop but had no way to reveal itself on a phone,
    which is what made the icon look "broken" there. Newlines are encoded
    as &#10; in the title attribute for the same reason as with_tooltip:
    a literal blank line inside raw HTML can trick Streamlit's/CommonMark's
    HTML-block parser into ending the block early and dumping the rest as
    visible text."""
    def _inject(m):
        opening, label, closing = m.group(1), m.group(2), m.group(3)
        definition = definitions.get(label.strip())
        if not definition:
            return m.group(0)
        def_esc = html.escape(definition)
        title_attr = def_esc.replace("\n", "&#10;")
        body_html = def_esc.replace("\n", "<br>")
        icon = (
            f' <details style="display:inline-block;vertical-align:middle" title="{title_attr}">'
            f'<summary style="cursor:help;opacity:0.55;font-size:11px;">ⓘ</summary>'
            f'<div style="font-size:11px;font-weight:400;line-height:1.5;'
            f'white-space:normal;max-width:260px;margin-top:2px;">{body_html}</div>'
            f'</details>'
        )
        return f"{opening}{label}{icon}{closing}"
    return re.sub(r"(<th\b[^>]*>)([^<]*)(</th>)", _inject, html_str)


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

    st.markdown("**Daily SMA periods** (fast / medium / slow)")
    d1, d2, d3 = st.columns(3)
    ema_d_fast = d1.number_input("4. Daily SMA fast", min_value=1, step=1, value=int(settings["ema_daily"][0]), key="set_ema_d_fast")
    ema_d_mid = d2.number_input("5. Daily SMA medium", min_value=1, step=1, value=int(settings["ema_daily"][1]), key="set_ema_d_mid")
    ema_d_slow = d3.number_input("6. Daily SMA slow", min_value=1, step=1, value=int(settings["ema_daily"][2]), key="set_ema_d_slow")

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
    vstop_include_incomplete = st.checkbox(
        "Include the in-progress (partial) week in VStop — matches TradingView's live value",
        value=bool(settings.get("vstop_include_incomplete_week", True)),
        key="set_vstop_include_incomplete",
        help=("ON (default): the current, not-yet-closed week is included, so VStop-W matches "
              "TradingView's Volatility Stop at any point during the week. OFF: only fully completed "
              "weekly bars are used, which avoids a false stop flip from a partial Friday bar but "
              "makes VStop-W lag TradingView by one week until Friday's close."),
    )
    _vstop_mode_labels = {"tv": "TradingView exact (default)", "app": "Legacy (app)"}
    vstop_mode = st.radio(
        "13. VStop engine",
        options=["tv", "app"],
        index=0 if settings.get("vstop_mode", "tv") != "app" else 1,
        key="set_vstop_mode",
        format_func=lambda m: _vstop_mode_labels[m],
        help=("TV (default): exact port of TradingView's built-in Volatility Stop (Source=close, "
              "using the length/factor above). The stop anchors to the running close max/min since "
              "the last stop-and-reverse flip, so it keeps ratcheting with the trend exactly like "
              "TradingView. Legacy: the app's older close-anchored Wilder stop."),
    )

    st.markdown("**Watchlists**")
    st.caption(
        "Each watchlist's benchmark ticker and display label, plus adding a new watchlist. Renaming a "
        "label is always safe (nothing else changes). Deleting an existing watchlist isn't available "
        "here by design -- it's a deliberate, code-level-only action, not a clickable button."
    )
    from stock_data import load_markets_registry, save_markets_registry, add_watchlist

    markets_registry = load_markets_registry()
    for mkey, minfo in list(markets_registry.items()):
        mc1, mc2, mc3 = st.columns([2, 2, 1])
        new_mlabel = mc1.text_input(f"Label ({mkey})", value=minfo["label"], key=f"mkt_label_{mkey}")
        new_mbench = mc2.text_input(f"Benchmark ticker ({mkey})", value=minfo["benchmark"], key=f"mkt_bench_{mkey}")
        mc3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if mc3.button("Save", key=f"mkt_save_{mkey}"):
            changed = False
            if new_mlabel.strip() and new_mlabel.strip() != minfo["label"]:
                markets_registry[mkey]["label"] = new_mlabel.strip()
                changed = True
            if new_mbench.strip() and new_mbench.strip() != minfo["benchmark"]:
                markets_registry[mkey]["benchmark"] = new_mbench.strip()
                changed = True
            if changed:
                save_markets_registry(markets_registry)
                gh_token, gh_repo, gh_branch = get_github_config(getattr(st, "secrets", None))
                if gh_token and gh_repo:
                    with st.spinner("Pushing rename to GitHub..."):
                        ok, msg = push_all_config(gh_token, gh_repo, gh_branch, filenames=["markets.json"], message=f"Rename watchlist {mkey}")
                        if not ok:
                            st.error(f"Failed to push to GitHub: {msg}")
                st.success(f"Saved changes to {mkey}.")
                time.sleep(1)
                st.rerun()
            else:
                st.info("No changes detected.")

    st.markdown("➕ **Add Watchlist**")
    aw1, aw2, aw3 = st.columns([2, 2, 1])
    new_wl_label = aw1.text_input("Label", key="new_watchlist_label", placeholder="e.g. UK Watchlist")
    new_wl_bench = aw2.text_input("Benchmark ticker", key="new_watchlist_bench", placeholder="e.g. ^FTSE")
    aw3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    if aw3.button("Add", key="add_watchlist_btn"):
        if not new_wl_label.strip() or not new_wl_bench.strip():
            st.error("Both a label and a benchmark ticker are required.")
        else:
            add_watchlist(new_wl_label.strip(), new_wl_bench.strip())
            gh_token, gh_repo, gh_branch = get_github_config(getattr(st, "secrets", None))
            if gh_token and gh_repo:
                with st.spinner("Pushing new watchlist to GitHub..."):
                    ok, msg = push_all_config(gh_token, gh_repo, gh_branch, filenames=["markets.json", "watchlist.json", "custom_filters.json"], message=f"Add watchlist {new_wl_label.strip()}")
                    if not ok:
                        st.error(f"Failed to push to GitHub: {msg}")
            st.success(f"Added \"{new_wl_label.strip()}\".")
            time.sleep(1)
            st.rerun()

    st.markdown("**Trend column** (Strong Uptrend / Uptrend / Downtrend / Strong Downtrend)")
    st.caption(
        "Uptrend requires ALL of: price above slow WEMA, the WEMA's own slope rising, fast WEMA above "
        "slow WEMA (e.g. 10 WEMA > 40 WEMA), and Mansfield RS positive (when available) — no partial "
        "credit; anything short of unanimous is Downtrend. "
        "\"Strong\" additionally needs BOTH of the two thresholds below — parameters here only affect "
        "this column, independent of Vol Trend or Tech Uptrend."
    )
    tr1, tr2, tr3 = st.columns(3)
    trend_slope_lookback = tr1.number_input(
        "15. MA slope lookback (weeks)", min_value=2, step=1,
        value=int(settings.get("trend_slope_lookback", 3)), key="set_trend_slope",
        help="Width of the regression window (in weeks) used to judge whether the slow WEMA is "
        "currently rising or falling. Shorter = catches recent rollovers faster (can be "
        "noisier); longer = smoother but slower to detect a real trend change.",
    )
    trend_near_pct = tr2.number_input(
        "16. \"Strong\": within % of 52W high/low", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
        value=float(settings.get("trend_near_high_low_pct", 0.10)), key="set_trend_near_pct",
        help="\"Strong\" requires price within this fraction of its 52-week high (uptrend) or low "
             "(downtrend). Default 0.10 = within 10%. This column's own parameter, separate from "
             "Vol Trend/Tech Uptrend's ratios below.",
    )
    trend_vol_ratio = tr3.number_input(
        "17. \"Strong\": min 10D ÷ 100D vol ratio", min_value=0.0, step=0.1, format="%.2f",
        value=float(settings.get("trend_volume_ratio", 1.0)), key="set_trend_vol_ratio",
        help="\"Strong\" also requires 10-day average volume ≥ this many times the 100-day average. "
             "Default 1.0 = just needs to be higher, no minimum multiple. This column's own "
             "parameter, separate from Vol Trend's and Tech Uptrend's ratios.",
    )

    st.markdown("**Vol Trend column** (Exploding / In-line / Declining)")
    vt1, vt2 = st.columns(2)
    volume_explode_ratio = vt1.number_input(
        "18. 'Exploding' ratio (10D ÷ 100D avg ≥)", min_value=1.0, step=0.1, format="%.2f",
        value=float(settings.get("volume_explode_ratio", 1.4)), key="set_vol_explode",
        help="Vol Trend shows 'Exploding' when 10-day average volume is at least this many times the "
             "100-day average. Independent of Trend's and Tech Uptrend's volume ratios above/below.",
    )
    volume_decline_ratio = vt2.number_input(
        "19. 'Declining' ratio (10D ÷ 100D avg ≤)", min_value=0.0, step=0.1, format="%.2f",
        value=float(settings.get("volume_decline_ratio", 0.7)), key="set_vol_decline",
        help="Vol Trend shows 'Declining' when 10-day average volume is at or below this fraction of the 100-day average.",
    )

    st.markdown("**Tech Uptrend column** (boolean)")
    tu1, tu2 = st.columns(2)
    tech_uptrend_min_vstop_weeks = tu1.number_input(
        "20. Min weeks held above VStop", min_value=0, step=1,
        value=int(settings.get("tech_uptrend_min_vstop_weeks", 3)), key="set_tech_min_weeks",
        help="Tech Uptrend requires the weekly VStop to have been in an uptrend for MORE than this "
             "many weeks (in addition to close > VStop, close > slow WEMA, and the volume ratio below).",
    )
    tech_uptrend_vol_ratio = tu2.number_input(
        "21. Min 10D ÷ 100D vol ratio", min_value=0.0, step=0.1, format="%.2f",
        value=float(settings.get("tech_uptrend_volume_ratio", 1.4)), key="set_tech_vol_ratio",
        help="Tech Uptrend also requires 10-day average volume ≥ this many times the 100-day average. "
             "This column's own parameter — independent of Vol Trend's 'Exploding' ratio above, even "
             "though both default to the same value.",
    )

    st.markdown("**Ticker Notes**")
    note_dropdown_options = st.text_input(
        "22. Dropdown Options (comma-separated)",
        value=settings.get("note_dropdown_options", ""),
        key="set_note_opts",
        help="If provided, the Ticker Notes field in the sidebar will become a dropdown menu with these specific values (along with a 'Custom...' option for free text)."
    )

    st.divider()
    c1, c2 = st.columns(2)
    if c1.button("Save", type="primary", width="stretch"):
        new_weekly = [int(ema_w_fast), int(ema_w_mid), int(ema_w_slow)]
        new_daily = [int(ema_d_fast), int(ema_d_mid), int(ema_d_slow)]
        if not (new_weekly[0] < new_weekly[1] < new_weekly[2]):
            st.error("Weekly EMA periods must be increasing: fast < medium < slow.")
        elif not (new_daily[0] < new_daily[1] < new_daily[2]):
            st.error("Daily SMA periods must be increasing: fast < medium < slow.")
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
                "vstop_mode": vstop_mode,
                "vstop_include_incomplete_week": bool(vstop_include_incomplete),
                "trend_slope_lookback": int(trend_slope_lookback),
                "trend_near_high_low_pct": float(trend_near_pct),
                "trend_volume_ratio": float(trend_vol_ratio),
                "volume_explode_ratio": float(volume_explode_ratio),
                "volume_decline_ratio": float(volume_decline_ratio),
                "tech_uptrend_min_vstop_weeks": int(tech_uptrend_min_vstop_weeks),
                "tech_uptrend_volume_ratio": float(tech_uptrend_vol_ratio),
                "note_dropdown_options": note_dropdown_options.strip(),
            })
            st.success("Settings saved. Click **Refresh Data** to recompute with the new settings.")
            st.rerun()
    if c2.button("Reset to defaults", width="stretch"):
        save_settings(dict(DEFAULT_SETTINGS))
        st.success("Reset to defaults. Click **Refresh Data** to recompute with default settings.")
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


def _apply_watchlist_tickers(market, market_label, existing_tickers, candidate_tickers, interested):
    from stock_data import save_interested
    """Validates `candidate_tickers` against Yahoo Finance (skipping
    tickers already known-valid in `existing_tickers`), saves the
    watchlist + Interested flags, pushes to GitHub if configured, and
    reruns. Shared by the manual per-row editor's Save button and the
    bulk-upload handler so both go through the exact same validate/save/
    push sequence instead of duplicating it."""
    with st.spinner("Validating new tickers..."):
        valid_tickers = []
        for t in candidate_tickers:
            if t in existing_tickers:  # Already known to be valid
                valid_tickers.append(t)
            elif validate_ticker(t):
                valid_tickers.append(t)
            else:
                st.error(f"'{t}' doesn't return any price data from Yahoo Finance — dropping it.")
                interested.discard(t)

    save_watchlist(market, valid_tickers)
    save_interested(interested)

    # Saving a watchlist is an allowed refresh trigger, but only the tickers
    # that have no snapshot row actually need one. This used to call
    # fetch_all_markets(None) -- the None loads EVERY watchlist, so editing one
    # market refetched all ~118 tickers, and since it ran unconditionally, even
    # deleting a ticker or saving an unchanged list paid the full price. At
    # roughly 5 Yahoo requests per ticker plus a deliberate 0.5s pause between
    # them (see fetch_snapshot), that is minutes of blocked UI for a one-ticker
    # edit.
    #
    # Reusing a row fetched under a DIFFERENT watchlist is safe: rows are
    # per-ticker, not per-market. fetch_all_markets groups by each ticker's
    # permanent ticker_index.json assignment "independent of which watchlist(s)
    # it's filed under" (see its docstring), so the same ticker yields the same
    # row whichever list it sits in.
    from stock_data import load_settings as _load_settings_now
    _curr_settings = _load_settings_now()

    _snapshot = load_data_snapshot() or {}
    _as_of_box = [_snapshot.get("as_of")]

    def _fetch_new(tickers_needed):
        with st.spinner(f"Fetching {len(tickers_needed)} new ticker(s) from Yahoo Finance..."):
            # Pass the real market key so the benchmark fallback is right for any
            # ticker backfill_ticker_indices could not classify.
            _c, _fetched_as_of, _fetched = fetch_all_markets({market: tickers_needed}, settings=_curr_settings)
        _as_of_box[0] = _fetched_as_of
        return _fetched.get(market, [])

    _merged, _to_fetch = rebuild_snapshot_for_market(
        _snapshot.get("per_market") or {}, market, valid_tickers, _fetch_new
    )
    _as_of = _as_of_box[0] or datetime.now().strftime("%Y-%m-%d %H:%M")

    _persist_and_serve(_merged, _as_of, _curr_settings)
    _bump_refresh()
    _reused = len(valid_tickers) - len(_to_fetch)
    if _to_fetch:
        st.success(
            f"Saved {market_label} with {len(valid_tickers)} tickers — "
            f"fetched {len(_to_fetch)} new, reused {_reused} from the last snapshot."
        )
    else:
        st.success(
            f"Saved {market_label} with {len(valid_tickers)} tickers — "
            "no Yahoo fetch needed, every ticker was already in the snapshot."
        )

    gh_token, gh_repo, gh_branch = get_github_config(st.secrets)
    if gh_token and gh_repo:
        with st.spinner("Pushing watchlist to GitHub..."):
            ok, msg = push_all_config(gh_token, gh_repo, gh_branch, filenames=["watchlist.json", "interested.json", "data_snapshot.json", "ticker_index.json"], message=f"Update {market_label}")
            if ok:
                st.success("Successfully pushed to GitHub!")
            else:
                st.error(f"Failed to push to GitHub: {msg}")

    time.sleep(1)
    st.rerun()


def render_watchlist_editor(market, watchlists):
    from stock_data import load_interested, load_markets_registry
    import pandas as pd

    market_label = load_markets_registry().get(market, {}).get("label", market)
    tickers = watchlists.get(market, [])
    st.caption(f"{len(tickers)} tickers")

    interested = load_interested()

    with st.expander("⬆️ Bulk add tickers (.csv or .txt)", expanded=False):
        st.caption(
            "One ticker per line, or comma-separated. For a CSV, put tickers in a column named "
            "'Ticker' (or the first column if there's no header). New tickers are added to the "
            "existing watchlist -- nothing gets removed."
        )
        uploaded = st.file_uploader(
            "Upload file", type=["csv", "txt"], key=f"bulk_upload_{market}", label_visibility="collapsed",
        )
        if uploaded is not None:
            raw_tickers = []
            try:
                if uploaded.name.lower().endswith(".csv"):
                    up_df = pd.read_csv(uploaded)
                    col = next((c for c in up_df.columns if str(c).strip().lower() == "ticker"), None)
                    if col is not None:
                        raw_tickers = up_df[col].astype(str).tolist()
                    else:
                        # No column literally named "Ticker" -- pandas always
                        # treats row 1 as a header, so for a bare headerless
                        # list (just tickers, one per line) the very first
                        # ticker would otherwise be silently swallowed as the
                        # column name instead of a value. Recover it by
                        # treating the header text as a candidate ticker too;
                        # if it turns out to be a genuine unrecognized header
                        # (e.g. "Symbol"), that's a harmless extra entry --
                        # validate_ticker() rejects it later same as any typo,
                        # which is a much safer failure mode than silently
                        # losing real data.
                        first_col = up_df.columns[0]
                        raw_tickers = [str(first_col)] + up_df[first_col].astype(str).tolist()
                else:
                    text = uploaded.read().decode("utf-8", errors="ignore")
                    raw_tickers = [line for line in text.replace(",", "\n").splitlines() if line.strip()]
            except Exception as e:
                st.error(f"Couldn't read the uploaded file: {e}")

            cleaned = []
            for t in raw_tickers:
                t = str(t).strip().upper()
                if t and t != "NAN" and t not in cleaned:
                    cleaned.append(t)

            new_from_upload = [t for t in cleaned if t not in tickers]
            already_present = len(cleaned) - len(new_from_upload)

            if not cleaned:
                st.warning("No tickers found in the uploaded file.")
            else:
                st.write(
                    f"Found {len(cleaned)} ticker(s) in the file: {len(new_from_upload)} new, "
                    f"{already_present} already in the watchlist."
                )
                if st.button(
                    f"Add {len(new_from_upload)} new ticker(s)",
                    key=f"bulk_add_confirm_{market}",
                    disabled=not new_from_upload,
                    type="primary",
                ):
                    merged = tickers + new_from_upload
                    _apply_watchlist_tickers(market, market_label, tickers, merged, interested)

    data = []
    for t in tickers:
        data.append({
            "Ticker": t,
            "Interested": t in interested,
        })

    df = pd.DataFrame(data, columns=["Ticker", "Interested"])
    if df.empty:
        df = pd.DataFrame(columns=["Ticker", "Interested"])

    df = df.astype({"Ticker": "string", "Interested": "boolean"})
    
    st.caption("💡 **Tip:** To remove a ticker, click the row's leftmost edge to select the entire row, then press Delete or click the trash icon.")
        
    edited_df = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        key=f"watchlist_editor_{market}",
        hide_index=True,
        column_config={
            "Ticker": st.column_config.TextColumn(
                "Ticker" if market == "us_invested" else ("Ticker (needs .NS/.BO)" if market == "india_invested" else "Ticker (as recognized by Yahoo Finance)"),
                required=True
            ),
            "Interested": st.column_config.CheckboxColumn(
                "Interested", help="Tick the tickers you're interested in. Shows as the Interested column in the table."
            ),
        }
    )
    
    if st.button(f"Save {market_label}", type="primary", key=f"save_watchlist_{market}"):
        new_tickers = []
        for idx, row in edited_df.iterrows():
            t = str(row["Ticker"]).strip().upper()
            if not t or t == "NAN": continue
            if t not in new_tickers:
                new_tickers.append(t)

            # A never-touched checkbox in a newly added row comes back as pd.NA,
            # and bool(pd.NA) RAISES rather than returning False -- so guard on
            # pd.notna first. Can't use `is True` either: iterrows() hands back
            # numpy bools, which fail an identity test against the builtin.
            flag = row["Interested"]
            if pd.notna(flag) and bool(flag):
                interested.add(t)
            else:
                interested.discard(t)

        _apply_watchlist_tickers(market, market_label, tickers, new_tickers, interested)


OPERATOR_CHOICES = [">", "<", ">=", "<=", "=="]


def reset_builder_keys(key_prefix):
    """Drops every st.session_state entry belonging to one condition
    builder.

    Needed because Streamlit applies a widget's index=/value=/default= only
    the FIRST time it sees that widget's key -- after that, whatever is in
    session_state wins. So opening the edit form on a condition would show
    leftovers from the last time that same key rendered (the previous
    condition edited in that slot, or a half-typed value someone cancelled
    out of) instead of the condition actually being edited. Clearing the
    keys first makes the next render a genuine first render, so the
    initial= defaults land.

    Call on ENTERING and on LEAVING edit mode: leaving matters too, or the
    edit form's values would leak into the plain "add a condition" builder,
    which shares the widget-key namespace by design."""
    for k in [k for k in st.session_state if k.startswith(f"{key_prefix}_")]:
        del st.session_state[k]


def _initial_index(options, value, default=0):
    """index= for a selectbox/radio, tolerating a value that isn't in the
    list (a metric hidden by a settings toggle, a referenced alert since
    deleted). Falls back rather than raising, so a stale condition stays
    editable instead of breaking the whole tab."""
    try:
        return options.index(value)
    except (ValueError, AttributeError):
        return default


def render_condition_builder(key_prefix, metric_names, filterable_metrics, logic_choice, available_rules=None, exclude_rule_id=None, definitions=None, initial=None, submit_label="＋ Add condition"):
    """Renders one full condition-builder row (Metric A / Operator / Compare
    to / Value / Add button) shared by the watchlist custom-filter builder,
    the new-rule builder, and each existing rule's inline condition editor
    -- one implementation instead of three near-duplicates.

    When Metric A is a categorical field (Trend, Vol Trend, VStop Dir, Tech
    Uptrend -- see filters.CATEGORICAL_METRICS), the Operator/Compare-to/
    Value widgets are replaced with a multiselect of that metric's real
    values and the operator is fixed to "in" -- so instead of typing
    "Downtrend" and hoping it's spelled/capitalized exactly right, you pick
    from the real list, and can match several at once (e.g. Trend in
    [Downtrend, Strong Downtrend]).

    A condition can alternatively reference another saved alert
    ({"type": "rule", "rule_id": ...}) -- true iff that alert is currently
    matching this ticker. `available_rules` supplies the pickable alerts;
    `exclude_rule_id` hides the rule being edited so it can't reference
    itself.

    `definitions` (metric key -> text, from metric_definitions()) drives a
    caption under each metric picker explaining the currently-selected
    metric. Optional and sparse by design: metrics without an entry just get
    no caption, so a metric that has never been documented is still usable.

    `initial` pre-fills every widget from an existing condition, turning the
    builder into an edit form (pair it with submit_label="💾 Save changes").
    The caller must call reset_builder_keys(key_prefix) when entering edit
    mode -- see that function for why the defaults alone aren't enough.

    Returns a finished condition dict (with "logic" set to `logic_choice`,
    ready to append to a conditions list) the moment the submit button is
    clicked with valid inputs, else None."""
    definitions = definitions or {}
    initial = initial or {}
    label_by_metric = {v: k for k, v in filterable_metrics.items()}

    def _explain(metric_key, container=st):
        text = definitions.get(metric_key)
        if text:
            container.caption(f"ℹ️ {text}")
    condtype_options = ["Metric comparison", "Reference another alert"]
    condition_type = st.radio(
        "Condition type",
        condtype_options,
        index=1 if initial.get("type") == "rule" else 0,
        key=f"{key_prefix}_condtype",
        horizontal=True,
        help="A metric comparison tests a metric value; 'Reference another alert' is true "
             "whenever that saved alert is currently matching this ticker.",
    )
    if condition_type == "Reference another alert":
        options = [
            r for r in (available_rules or [])
            if r.get("enabled", True) and r.get("conditions") and r.get("id") != exclude_rule_id
        ]
        if not options:
            st.caption("No other saved alerts to reference yet — create one in the Alert Rules tab first.")
            return None
        rule_labels = [f"{r.get('name') or '(unnamed)'} [{r['id']}]" for r in options]
        rule_by_label = dict(zip(rule_labels, options))
        # The referenced alert may since have been disabled, emptied or
        # deleted, which drops it out of `options` -- fall back to the first
        # rather than raising, so the condition stays editable.
        init_ref = next((lbl for lbl, r in rule_by_label.items()
                         if r["id"] == initial.get("rule_id")), None)
        rc1, rc2 = st.columns([4, 1])
        sel_label = rc1.selectbox("Alert to reference", rule_labels,
                                  index=_initial_index(rule_labels, init_ref),
                                  key=f"{key_prefix}_refrule")
        if rc2.button(submit_label, key=f"{key_prefix}_addbtn"):
            return {"type": "rule", "rule_id": rule_by_label[sel_label]["id"], "logic": logic_choice}
        return None

    init_a_label = label_by_metric.get(initial.get("metric_a"))
    metric_a_label = st.selectbox("Metric A", metric_names,
                                  index=_initial_index(metric_names, init_a_label),
                                  key=f"{key_prefix}_a")
    metric_a_key = filterable_metrics[metric_a_label]
    _explain(metric_a_key)
    categorical_options = CATEGORICAL_METRICS.get(metric_a_key)

    if categorical_options:
        # Only reuse the stored value as the default when it belongs to THIS
        # metric's option list -- switching Metric A mid-edit (Trend ->
        # Vol Trend) must not carry "Strong Uptrend" into a list that has no
        # such option, which Streamlit would reject.
        init_cat = [v for v in (initial.get("value") or []) if v in categorical_options] \
            if isinstance(initial.get("value"), list) else []
        c1, c2 = st.columns([4, 1])
        selected = c1.multiselect(
            "Value(s) — matches if Trend/Vol Trend/etc. is ANY of these",
            options=categorical_options, default=init_cat, key=f"{key_prefix}_catval",
        )
        if c2.button(submit_label, key=f"{key_prefix}_addbtn"):
            if not selected:
                st.warning("Select at least one value.")
                return None
            return {
                "metric_a": metric_a_key, "operator": "in", "compare_type": "value",
                "value": selected, "logic": logic_choice,
            }
        return None

    c1, c2, c3 = st.columns([1, 1.5, 2])
    operator_choice = c1.selectbox("Op", OPERATOR_CHOICES,
                                   index=_initial_index(OPERATOR_CHOICES, initial.get("operator")),
                                   key=f"{key_prefix}_op")
    ctype_options = ["Metric", "Fixed value"]
    # Defaults to "Metric" for a fresh add, matching the pre-edit-mode
    # behaviour where this radio simply took its first option.
    ctype_index = 1 if initial.get("compare_type") == "value" else 0
    compare_type = c2.radio("Compare to", ctype_options, index=ctype_index,
                            key=f"{key_prefix}_ctype", horizontal=True)
    if compare_type == "Metric":
        init_b_label = label_by_metric.get(initial.get("metric_b"))
        metric_b_label = c3.selectbox("Metric B", metric_names,
                                      index=_initial_index(metric_names, init_b_label),
                                      key=f"{key_prefix}_b")
        _explain(filterable_metrics[metric_b_label], container=c3)
        mc1, mc2, mc3 = st.columns([1, 1, 1])
        multiplier = mc1.number_input(
            "× Multiplier (optional)", value=float(initial.get("multiplier") or 1.0),
            step=0.1, format="%.2f", key=f"{key_prefix}_mult",
            help="e.g. set to 1.4 for 'Vol 10D Avg >= 1.4 × Vol 100D Avg'.",
        )
        offset = mc2.number_input("+ Offset (optional)", value=float(initial.get("offset") or 0.0),
                                  step=0.1, format="%.2f", key=f"{key_prefix}_off")
        if mc3.button(submit_label, key=f"{key_prefix}_addbtn"):
            cond = {
                "metric_a": metric_a_key, "operator": operator_choice, "compare_type": "metric",
                "metric_b": filterable_metrics[metric_b_label], "logic": logic_choice,
            }
            if multiplier != 1.0:
                cond["multiplier"] = multiplier
            if offset != 0.0:
                cond["offset"] = offset
            return cond
        return None
    else:
        vc1, vc2 = st.columns([2, 1])
        # A stored 45.0 must come back as "45", not "45.0" -- round-tripping
        # through parse_filter_value_text would otherwise rewrite every
        # untouched integer threshold the first time a condition is edited.
        init_val = initial.get("value") if initial.get("compare_type") == "value" else None
        if isinstance(init_val, float) and init_val.is_integer():
            init_val_text = str(int(init_val))
        elif init_val is None or isinstance(init_val, list):
            init_val_text = "0"
        else:
            init_val_text = str(init_val)
        value_text = vc1.text_input(
            "Value", value=init_val_text, key=f"{key_prefix}_val",
            help="A number (e.g. 45) or a word for boolean-like metrics, e.g. Yes / No for Tech Uptrend.",
        )
        if vc2.button(submit_label, key=f"{key_prefix}_addbtn"):
            return {
                "metric_a": metric_a_key, "operator": operator_choice, "compare_type": "value",
                "value": parse_filter_value_text(value_text), "logic": logic_choice,
            }
        return None


def render_condition_list(conditions, key_prefix, metric_labels, filterable_metrics,
                          rule_by_id=None, available_rules=None, exclude_rule_id=None,
                          definitions=None):
    """Renders a saved condition chain with per-condition edit / reorder /
    remove controls, and returns (conditions, changed).

    Shared by the three places that hold a chain -- the new-rule draft, an
    existing rule's editor, and the watchlist custom-filter builder -- which
    previously each carried their own copy of a describe-and-Remove loop.
    Same consolidation as render_condition_builder (see its docstring). The
    three copies had already drifted: two printed the chain summary right
    above the list, while the rule editor printed it further up at the top
    of the expander, so it read as a description of the rule rather than of
    the list underneath. They now all show it in the same place.

    `conditions` is mutated in place and also returned. `changed` is True
    when the chain DATA differs from what was passed in, so the CALLER
    decides how to persist -- save_rules, save_market_filters, or just
    leaving it in session_state. This function never writes to disk.

    It does call st.rerun() for the two transitions that are pure UI state
    and carry no data change (entering edit mode, cancelling out of it),
    since the caller has nothing to persist for those and would only be
    forwarding a second flag back.

    Order is semantic, not cosmetic: chains evaluate left to right, so
    moving a condition across an OR changes what the rule matches. That is
    why reordering is offered at all.
    """
    if not conditions:
        return conditions, False

    editing_key = f"{key_prefix}_editing"
    editing_idx = st.session_state.get(editing_key)

    st.caption(describe_chain(conditions, metric_labels, rule_by_id))

    changed = False
    remove_idx = None
    swap = None
    for i, cond in enumerate(conditions):
        if editing_idx == i:
            with st.container(border=True):
                st.caption(f"Editing condition {i + 1} of {len(conditions)}")
                # The AND/OR joiner belongs to this condition, so it is
                # editable here rather than only at append time -- flipping
                # it used to require deleting and rebuilding the condition.
                if i == 0:
                    st.caption("First condition — joins nothing above it.")
                    edit_logic = cond.get("logic", "AND")
                else:
                    edit_logic = st.radio(
                        "Combine with the condition(s) above using", ["AND", "OR"],
                        index=0 if cond.get("logic", "AND") == "AND" else 1,
                        key=f"{key_prefix}_editlogic_{i}", horizontal=True,
                    )
                updated = render_condition_builder(
                    f"{key_prefix}_edit", list(filterable_metrics.keys()), filterable_metrics,
                    edit_logic, available_rules=available_rules,
                    exclude_rule_id=exclude_rule_id, definitions=definitions,
                    initial=cond, submit_label="💾 Save changes",
                )
                if st.button("Cancel", key=f"{key_prefix}_editcancel_{i}"):
                    # Clear on the way out as well as on the way in, or a
                    # cancelled edit's half-typed values would surface in the
                    # plain "add a condition" builder, which shares this
                    # widget-key namespace.
                    reset_builder_keys(f"{key_prefix}_edit")
                    del st.session_state[editing_key]
                    st.rerun()
                if updated:
                    # Custom filters carry a stable "id" that the builder
                    # doesn't produce; carry it across so an edit doesn't
                    # silently mint a different filter. Only "id" is
                    # preserved -- copying every unknown key would resurrect
                    # a stale metric_b when switching metric -> fixed value.
                    if "id" in cond:
                        updated["id"] = cond["id"]
                    conditions[i] = updated
                    reset_builder_keys(f"{key_prefix}_edit")
                    del st.session_state[editing_key]
                    changed = True
            continue

        c_txt, c_up, c_dn, c_ed, c_rm = st.columns([7, 0.6, 0.6, 0.8, 0.8])
        prefix = "" if i == 0 else f"**{cond.get('logic', 'AND')}**  "
        c_txt.write(f"{prefix}{describe_filter(cond, metric_labels, rule_by_id)}")
        if c_up.button("▲", key=f"{key_prefix}_up_{i}", disabled=(i == 0),
                       help="Move up"):
            swap = (i - 1, i)
        if c_dn.button("▼", key=f"{key_prefix}_dn_{i}", disabled=(i == len(conditions) - 1),
                       help="Move down"):
            swap = (i, i + 1)
        if c_ed.button("✏️", key=f"{key_prefix}_ed_{i}", help="Edit this condition",
                       disabled=editing_idx is not None):
            reset_builder_keys(f"{key_prefix}_edit")
            st.session_state[editing_key] = i
            st.rerun()
        if c_rm.button("🗑", key=f"{key_prefix}_rm_{i}", help="Remove this condition",
                       disabled=editing_idx is not None):
            remove_idx = i

    if swap:
        a, b = swap
        conditions[a], conditions[b] = conditions[b], conditions[a]
        # The first condition's logic is never shown or evaluated, so a
        # chain that starts on an OR after a swap would silently read as
        # something the UI can't display. Normalise it.
        if conditions and conditions[0].get("logic") == "OR":
            conditions[0] = {**conditions[0], "logic": "AND"}
        changed = True
    if remove_idx is not None:
        conditions.pop(remove_idx)
        if conditions and conditions[0].get("logic") == "OR":
            conditions[0] = {**conditions[0], "logic": "AND"}
        changed = True

    return conditions, changed


def render_custom_filter_builder(market, filterable_metrics, definitions=None):
    st.markdown(
        "**Custom filters** — compare any metric to another metric or a fixed value, "
        "or reference another saved alert. "
        "Add multiple conditions and chain each one with AND/OR against everything before it "
        "(e.g. cond1 AND cond2 AND cond3 OR cond4), evaluated left to right."
    )
    metric_names = list(filterable_metrics.keys())
    metric_labels = {v: k for k, v in filterable_metrics.items()}

    active_filters = get_market_filters(market)
    from alerts import load_rules as _load_rules_now
    custom_rules = _load_rules_now()
    rule_by_id_cf = {r["id"]: r for r in custom_rules}

    if active_filters:
        st.write("Active conditions:")
        active_filters, cf_changed = render_condition_list(
            active_filters, f"cf_{market}", metric_labels, filterable_metrics,
            rule_by_id=rule_by_id_cf, available_rules=custom_rules, definitions=definitions,
        )
        if cf_changed:
            save_market_filters(market, active_filters)
            st.rerun()

    st.markdown("Add a condition:" if not active_filters else "Add another condition:")
    if active_filters:
        logic_choice = st.radio(
            "Combine with the condition(s) above using", ["AND", "OR"],
            key=f"cf_logic_{market}", horizontal=True,
        )
    else:
        logic_choice = "AND"

    new_filter = render_condition_builder(f"cf_{market}", metric_names, filterable_metrics, logic_choice, available_rules=custom_rules, definitions=definitions)
    if new_filter:
        new_filter["id"] = uuid.uuid4().hex[:8]
        active_filters.append(new_filter)
        save_market_filters(market, active_filters)
        st.rerun()

    return active_filters


def build_column_defs(labels, custom_columns=None):
    """(data key, column label) for every optional (hideable) watchlist
    column, in display order, plus derived lookup dicts. Ticker/Last are
    mandatory and not included here. Shared by both market tabs (via
    render_shared_column_picker) so US and India always offer/show the
    identical set of columns. Enabled custom columns are appended at the
    end, using their stable custom_<id> key (see custom_columns.py)."""
    optional_defs = [
        ("company_name", "Company Name"),
        ("index_name", "Index"),
        ("expert_take", "Expert Take"),
        ("expert_news_backed", "Expert News?"),
        ("trend", "Trend"),
        ("flag", "Flag"),
        ("note", "Notes"),
        ("interested_label", "Interested"),
        ("matched_alerts", "Alerts"),
        ("pct_change_1d", "% Chg"),
        ("week52_high", "52W High"),
        ("week52_low", "52W Low"),
        ("breakout_window", "Breakout Window"),
        ("week26_distance", "26WH Distance"),
        ("week52_distance", "52WH Distance"),
        ("week52_high_age", "52W High Age"),
        ("overhead_supply", "Overhead Supply"),
        ("high_5y", "5Y High"),
        ("high_5y_distance", "5Y High Distance"),
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
        ("rsi12_monthly", "RSI-M (12)"),
        ("adx_weekly_14", "ADX-W"),
        ("adx_monthly_12", "ADX-M"),
        ("rs_daily", "RS-D"),
        ("rs_weekly", "RS-W"),
        ("rs_monthly", "RS-M"),
        ("vstop_weekly", "VStop-W"),
        ("vstop_weekly_14", "VStop-W (14)"),
        ("vstop_weekly_direction", "VStop Dir"),
        ("vstop_change", "VStop Weeks Ago"),
        ("volume_trend", "Vol Trend"),
        ("net_volume_10d_dir", "Net Vol 10D"),
        ("tech_uptrend_label", "Tech Uptrend"),
        ("fundamentals", "Sentiment"),
        ("qtr_profit_growth", "Qtr Profit Growth %"),
        ("qtr_eps_growth", "Qtr EPS Growth %"),
        ("qtr_revenue_growth", "Qtr Revenue Growth %"),
        ("ttm_profit_growth", "PAT Growth TTM %"),
        ("ttm_revenue_growth", "Revenue Growth TTM %"),
        ("reported_qtr", "Reported Qtr"),
        ("trailing_pe", "P/E (TTM)"),
        ("forward_pe", "P/E (Fwd)"),
        ("pb_ratio", "P/B"),
        ("ev_ebitda", "EV/EBITDA"),
        ("p_cashflow", "P/Cashflow"),
        ("roe", "ROE %"),
        ("cfo_op_5yr", "CFO/OP 5Y"),
        ("roce", "ROCE %"),
        ("avg_volume_10d", "Vol 10D"),
        ("avg_volume_20d", "Vol 20D"),
        ("avg_volume_100d", "Vol 100D"),
        ("rel_ret_1m_n500", "1M Ret vs Nifty 500"),
        ("rel_ret_6m_n500", "6M Ret vs Nifty 500"),
        ("perf_1m", "Perf 1M %"),
        ("perf_3m", "Perf 3M %"),
        ("perf_6m", "Perf 6M %"),
        ("perf_1y", "Perf 1Y %"),
        ("perf_3y", "Perf 3Y %"),
    ]
    from stock_data import load_settings
    if not load_settings().get("show_fundamental_columns", True):
        optional_defs = [(k, lbl) for k, lbl in optional_defs if k not in FUNDAMENTAL_COLUMN_KEYS]
    for col in (custom_columns if custom_columns is not None else load_custom_columns()):
        if col.get("enabled", True):
            optional_defs.append((column_key(col), col["name"]))
    label_by_key = dict(optional_defs)
    key_by_label = {lbl: k for k, lbl in optional_defs}
    all_labels = list(label_by_key.values())
    # Raw 10D/20D/100D volume are hidden by default (Vol Trend already
    # summarizes them); Flag and Invested are personal annotations shown
    # via the ticker-flag marker/watchlist editor already, so they're
    # opt-in here too; the breakout trio are primarily alert/scan inputs
    # (and 52WH Distance duplicates the "% Off 52W High" custom column with
    # the opposite sign), so they're offered in the picker but stay off
    # until asked for; everything else shows by default.
    #
    # The scan-driven family (5Y High pair, ADX, RSI-M (12), VStop-W (14),
    # the relative-return pair, the TTM growth pair) follows the same rule:
    # each exists to back a specific stockscans rule, and defaulting nine
    # more columns on would push the table sideways for everyone who never
    # runs those scans.
    default_hidden = {"Vol 10D", "Vol 20D", "Vol 100D", "Flag", "Interested",
                      "Breakout Window", "26WH Distance", "52WH Distance", "52W High Age",
                      "Overhead Supply",
                      "5Y High", "5Y High Distance", "ADX-W", "ADX-M", "RSI-M (12)",
                      "VStop-W (14)", "1M Ret vs Nifty 500", "6M Ret vs Nifty 500",
                      "PAT Growth TTM %", "Revenue Growth TTM %"}
    default_visible = [lbl for lbl in all_labels if lbl not in default_hidden]
    return optional_defs, label_by_key, key_by_label, all_labels, default_visible


# CATEGORICAL_METRICS declares every categorical best-first EXCEPT flag, whose
# list is FLAG_CHOICES -- the order the flag PICKER offers colors in, not a
# ranking. Taken literally it made Flag the one column where "Top→Bottom" put
# the worst value first, so you had to invert Flag alone to line it up with
# every other column. Ranked here instead: good, watch, bad, then Blue, which
# carries no good/bad meaning at all.
CATEGORY_ORDER_DEFAULTS = {
    "flag": ["Green", "Yellow", "Red", "Blue"],
}


def load_category_order():
    """{field: [values, best-first]} for every categorical metric.

    CATEGORICAL_METRICS (filters.py) supplies the defaults -- it already
    declares most of these in a meaningful order (trend runs Strong Uptrend
    -> Strong Downtrend, sentiment Positive -> Unknown), with
    CATEGORY_ORDER_DEFAULTS overriding the ones it doesn't -- and anything
    the user has dragged in the sidebar panel wins over both. Merging rather
    than replacing means a categorical metric added later gets a sensible
    ranking with nobody having to touch saved prefs.

    Only values still declared in CATEGORICAL_METRICS survive from the saved
    order, with any newly-declared ones appended, so renaming a category can't
    strand a dead entry at the top of the ranking.
    """
    saved = load_column_prefs_full().get("category_order") or {}
    out = {}
    for field, values in CATEGORICAL_METRICS.items():
        defaults = CATEGORY_ORDER_DEFAULTS.get(field, values)
        chosen = [v for v in saved.get(field, []) if v in defaults]
        out[field] = chosen + [v for v in defaults if v not in chosen]
    return out


def _rank_index(rank, value):
    """Position of `value` within `rank`, case-insensitively.

    Two behaviours worth stating:
      - An unranked value returns len(rank), so it sorts AFTER everything
        ranked but still ahead of rows where the field is missing entirely
        (apply_sort pins those to the very end regardless of direction).
      - Yes/No rankings accept the numeric and boolean forms the row actually
        carries -- Tech Uptrend is stored 0/1 and Interested as a bool, so
        neither would ever match a "Yes"/"No" string. Keyed off the rank
        list's own contents rather than a hardcoded field list, so another
        Yes/No metric works without being registered anywhere.
    """
    lowered = [str(v).strip().lower() for v in rank]
    if isinstance(value, (bool, int, float)) and not isinstance(value, str) and set(lowered) == {"yes", "no"}:
        value = "Yes" if value else "No"
    try:
        return lowered.index(str(value).strip().lower())
    except ValueError:
        return len(rank)


def apply_sort(rows, sort_field, ascending, rank=None):
    """Sorts `rows` (list of raw row dicts, BEFORE they become a DataFrame)
    by `sort_field`. Missing values (None, "", or NaN) always sort to the
    end regardless of direction -- the usual spreadsheet convention --
    rather than clustering at the front on a descending sort. This is the
    practical stand-in for 'click a column header to sort' -- see
    render_sort_control's docstring for why a literal header-click
    isn't feasible with this table.

    `rank` is an ordered list of values for a categorical field, in which
    case rows sort by position in that list instead of alphabetically --
    alphabetical is actively wrong for these (Trend would run Downtrend,
    Strong Downtrend, Strong Uptrend, Uptrend, which means nothing)."""
    if not sort_field:
        return rows

    def is_missing(v):
        return v is None or v == "" or (isinstance(v, float) and pd.isna(v))

    present = [r for r in rows if not is_missing(r.get(sort_field))]
    missing = [r for r in rows if is_missing(r.get(sort_field))]

    def keyfn(r):
        v = r[sort_field]
        if rank:
            return _rank_index(rank, v)
        return v.lower() if isinstance(v, str) else v

    return sorted(present, key=keyfn, reverse=not ascending) + missing


def apply_sort_levels(rows, levels, ranks=None):
    """Multi-key sort: apply_sort is already a stable sort, so chaining it
    once per level in REVERSE priority order (lowest-priority key first,
    highest-priority key last) produces correct multi-key ordering without
    any new sort logic -- each later pass only reorders rows that tied on
    every higher-priority key so far, exactly the semantics 'Sort by X,
    then by Y' implies. `levels` is a list of (sort_field, ascending)
    tuples in priority order; an empty list is a no-op.

    `ranks` is {field: ordered values} (see load_category_order); a field
    without an entry sorts naturally."""
    ranks = load_category_order() if ranks is None else ranks
    for sort_field, ascending in reversed(levels):
        rows = apply_sort(rows, sort_field, ascending, rank=ranks.get(sort_field))
    return rows


_SORT_LEVELS = 6

# Direction labels per field kind. The stored/canonical value is ALWAYS the
# arrow -- these are display-only, applied through the selectbox's format_func.
# That's deliberate: saved prefs, DEFAULT_SORT and the `dir == "↑"` ascending
# test all keep working untouched, with no migration and no chance of a saved
# "↑" failing to match a relabelled option.
DIR_LABELS = {
    "number": {"↑": "↑", "↓": "↓"},
    "text": {"↑": "A-Z", "↓": "Z-A"},
    "date": {"↑": "Old→New", "↓": "New→Old"},
    # Categorical columns sort by your ranking (see load_category_order), so
    # A-Z would be a lie -- "top" is whatever sits first in the drag list.
    "rank": {"↑": "Top→Bottom", "↓": "Bottom→Top"},
}
_DATE_SHAPES = (re.compile(r"^\d{4}-\d{2}-\d{2}$"), re.compile(r"^Q[1-4] \d{4}$"))


def _metric_kind(field, sample_rows):
    """"rank" | "number" | "text" | "date" for one sortable field.

    "rank" wins outright for any categorical metric -- its order is the one
    you defined, not the one its values happen to spell. The rest is inferred
    from the first non-empty value the field actually holds.

    Inferred rather than read off a hand-maintained list on purpose: such a
    list goes stale the moment a column is added (exactly how 14 columns ended
    up unfilterable), and inference covers user-defined custom columns for
    free. Dates are recognised by SHAPE -- they're stored as strings and sort
    lexicographically, which for ISO dates and "Qn YYYY" is precisely
    chronological, so they deserve chronological wording rather than A-Z.

    A field with no value anywhere in the sample falls back to "number", the
    same arrows the control has always shown.
    """
    if field in CATEGORICAL_METRICS:
        return "rank"
    for row in sample_rows:
        val = row.get(field)
        if val is None or val == "":
            continue
        if not isinstance(val, str):
            return "number"
        return "date" if any(p.match(val) for p in _DATE_SHAPES) else "text"
    return "number"


def _sort_label_to_field(sort_label, key_by_label):
    # "Interested" resolves to the raw boolean field ("interested"), not the
    # display key ("interested_label") key_by_label would otherwise give --
    # apply_sort operates on the row dict, which only ever carries the raw
    # field, same reasoning as Ticker/Company Name/Index/Last below.
    return {
        "Ticker": "ticker",
        "Company Name": "company_name",
        "Index": "index_name",
        "Last": "last_close",
        "Interested": "interested",
        # Same key-differs-from-column case: the column is tech_uptrend_label
        # (a tooltip-wrapped display string built after filtering), the
        # sortable value is the raw 0/1 tech_uptrend on the row.
        "Tech Uptrend": "tech_uptrend",
        # Sentiment's column key is "fundamentals", whose raw_df cell is a
        # <details> HTML block, not a sortable value -- the sortable form is
        # the plain verdict string attached to the row as "sentiment".
        "Sentiment": "sentiment",
    }.get(sort_label) or key_by_label.get(sort_label)


# Where every watchlist starts before you touch its sort control, as display
# labels (resolved through _sort_label_to_field, so they survive a column being
# renamed). Was a single global setting shared by all tabs; each tab now keeps
# its own, and this is what an untouched one falls back to.
DEFAULT_SORT = [("Index", "↑"), ("Data Thru", "↓"), ("% Chg", "↓")]


def _sort_pref_keys(market, i):
    """(field_pref_key, dir_pref_key) in column_prefs.json for one market's
    level `i`. Namespaced by market -- the un-namespaced sort_by_{i} /
    sort_dir_{i} keys are the retired global setting, left on disk but no
    longer read, since their values are exactly what DEFAULT_SORT reproduces."""
    return f"sort_by_{market}_{i}", f"sort_dir_{market}_{i}"


def saved_sort_levels(market, key_by_label):
    """This market's sort as [(row_field, ascending)], read straight from
    column_prefs.json -- no widgets, so EVERY tab can call it on every run,
    not just whichever one is on screen. The sidebar control below renders
    only for the active tab, but all seven tabs still have to sort their own
    table on the same pass.

    Levels stop at the first "(default order)"/"(none)", matching the control's
    own gating: a tie-breaker under an unset level would just be a confusingly
    labelled primary key."""
    prefs = load_column_prefs_full()
    levels = []
    for i in range(1, _SORT_LEVELS + 1):
        field_pref, dir_pref = _sort_pref_keys(market, i)
        default_label, default_dir = (
            DEFAULT_SORT[i - 1] if i <= len(DEFAULT_SORT) else ("(none)", "↑")
        )
        sort_label = prefs.get(field_pref, default_label)
        sort_dir = prefs.get(dir_pref, default_dir)
        if sort_label in ("(default order)", "(none)", None):
            break
        sort_field = _sort_label_to_field(sort_label, key_by_label)
        if sort_field:
            levels.append((sort_field, sort_dir == "↑"))
    return levels


def render_sort_control(market, market_label, label_by_key, key_by_label, sample_rows=(), other_tabs=()):
    """'Sort by' control for ONE watchlist, sidebar, always visible (not
    tucked into an expander -- used often enough to want one click, not two).

    Rendered only for the tab you're looking at, and it edits only that tab's
    saved sort. It used to be one global setting applied identically to every
    tab, which meant an ordering that suited the India watchlist was forced on
    the US one too. Widget keys carry the market, so switching tabs
    instantiates a different widget set rather than re-pointing one -- which
    also sidesteps the force-write-into-session_state hazard described in
    render_shared_column_picker's docstring, since only one market's widgets
    ever exist on a given run.

    True click-on-column-header sorting isn't feasible with the current
    table: it's rendered as a static HTML block (via st.markdown, needed
    for the sticky header/frozen ticker column -- see sticky_header_html's
    docstring) and Streamlit's markdown renderer strips <script>/<style>
    tags outright even with unsafe_allow_html=True, so there's no way to
    attach a client-side click handler to a <th>. This dropdown gives the
    same practical outcome -- sort the whole table by up to 3 columns at
    once, in priority order -- without rearchitecting the table onto a
    JS-executing surface (e.g. st.iframe), which would mean giving up the
    dynamic page-filling height that took real effort to get right (see
    'Make watchlist tables fill page height with sticky header' in the
    project history) since iframes need a fixed height.

    Returns a list of up to 3 (sort_field_key, ascending) tuples, in
    priority order (empty list = no sort, i.e. raw list order). Level 1 is
    the primary key and offers "(default order)"; levels 2-3 are optional
    tie-breakers and offer "(none)", each only shown once the level above
    it has a real column chosen."""
    # matched_alerts (Alerts) and vstop_change (VStop Weeks Ago) are computed
    # AFTER filtering, inside render_market_tab, so they aren't on the raw row
    # dict at sort time -- excluded rather than silently sorting wrong (or
    # crashing on a missing key).
    #
    # fundamentals (Sentiment) and tech_uptrend_label (Tech Uptrend) USED to
    # belong here for the same reason. Both now sort off a raw field that IS
    # on the row -- "sentiment" and "tech_uptrend" respectively -- reached
    # through _sort_label_to_field's key-differs-from-column cases, so only
    # their display cells are built late, not their sortable values.
    sort_labels = ["Ticker", "Company Name", "Index", "Last"] + [
        lbl for key, lbl in label_by_key.items()
        if key not in ("matched_alerts", "vstop_change", "company_name", "index_name")
    ]

    prefs = load_column_prefs_full()
    st.sidebar.caption(f"Sort — {market_label}")
    levels = []
    for i in range(1, _SORT_LEVELS + 1):
        field_key = f"sort_field_{market}_{i}"
        dir_key = f"sort_dir_{market}_{i}"
        field_pref, dir_pref = _sort_pref_keys(market, i)
        empty_option = "(default order)" if i == 1 else "(none)"
        options = [empty_option] + sort_labels
        default_label, default_dir = (
            DEFAULT_SORT[i - 1] if i <= len(DEFAULT_SORT) else (empty_option, "↑")
        )
        if field_key not in st.session_state:
            saved = prefs.get(field_pref, default_label) or empty_option
            st.session_state[field_key] = saved if saved in options else empty_option
        if dir_key not in st.session_state:
            st.session_state[dir_key] = prefs.get(dir_pref, default_dir) or "↑"

        # A tie-breaker level only makes sense once the level above it is
        # actually sorting on something -- otherwise it's a lower-priority
        # key with nothing above it to break ties for, which is just level 1
        # under a confusing label.
        if i > 1 and not levels:
            break

        # [3, 2] rather than [3, 1]: the direction box now has to fit
        # "Old→New", not just a single arrow.
        sc1, sc2 = st.sidebar.columns([3, 2])
        label = "Sort by" if i == 1 else "Then by"
        sort_label = sc1.selectbox(label, options, key=field_key)
        # Labels follow the chosen column's type -- ↑/↓ reads fine for % Chg
        # but says nothing about Company Name. Options stay the canonical
        # arrows; only the rendering changes (see DIR_LABELS).
        kind = _metric_kind(_sort_label_to_field(sort_label, key_by_label), sample_rows)
        sort_dir = sc2.selectbox(
            "Dir", ["↑", "↓"], key=dir_key,
            format_func=lambda d, _k=kind: DIR_LABELS[_k][d],
            label_visibility="collapsed" if i > 1 else "visible",
        )
        ascending = sort_dir == "↑"

        if sort_label != prefs.get(field_pref) or sort_dir != prefs.get(dir_pref):
            update_column_prefs(field_pref, sort_label)
            update_column_prefs(dir_pref, sort_dir)

        if sort_label == empty_option:
            break
        sort_field = _sort_label_to_field(sort_label, key_by_label)
        if sort_field:
            levels.append((sort_field, ascending))

    _render_copy_sort_from(market, other_tabs)
    return levels


def _render_copy_sort_from(market, other_tabs):
    """"Copy sort from <tab>" -- lifts another tab's whole sort setup into
    this one, instead of re-picking six levels and six directions by hand.

    Two-step (pick, then press Copy) on purpose: a one-click dropdown would
    overwrite the sort you're looking at the moment you browsed the list.
    """
    if not other_tabs:
        return
    st.sidebar.markdown("---")
    labels = [lbl for lbl, _ in other_tabs]
    by_label = dict(other_tabs)
    cc1, cc2 = st.sidebar.columns([3, 2])
    source_label = cc1.selectbox("Copy sort from", labels, key=f"copy_sort_src_{market}")
    if not cc2.button("Copy", key=f"copy_sort_btn_{market}", width="stretch"):
        return

    source = by_label[source_label]
    prefs = load_column_prefs_full()
    for i in range(1, _SORT_LEVELS + 1):
        src_field, src_dir = _sort_pref_keys(source, i)
        dst_field, dst_dir = _sort_pref_keys(market, i)
        default_label, default_dir = (
            DEFAULT_SORT[i - 1] if i <= len(DEFAULT_SORT) else ("(none)" if i > 1 else "(default order)", "↑")
        )
        update_column_prefs(dst_field, prefs.get(src_field, default_label))
        update_column_prefs(dst_dir, prefs.get(src_dir, default_dir))
        # The widgets only seed themselves from prefs when their session_state
        # entry is ABSENT, so leaving these behind would re-render the old
        # values and write them straight back over the copy.
        st.session_state.pop(f"sort_field_{market}_{i}", None)
        st.session_state.pop(f"sort_dir_{market}_{i}", None)
    st.toast(f"Copied sort from {source_label}.")
    st.rerun()


def render_shared_column_picker(labels, active_market=None, active_market_label=None, sample_rows=(), other_tabs=()):
    """Single 'Columns to show / reorder' control, rendered ONCE (in the
    sidebar) and shared by both the US and India watchlist tables, so
    picking/reordering columns always applies to both.

    Columns stay shared; SORT does not. `active_market` is the tab currently on
    screen -- its sort control is rendered here, above the column picker, and
    edits only that tab's saved order. Pass None for a tab with no table (News,
    Alert Rules) and no sort control is drawn at all.

    This used to be two separate widgets (one per tab) kept in sync by
    force-writing the shared value into each widget's session_state before
    it was instantiated -- that turns out to be unreliable in Streamlit:
    doing that write on the SAME render where the user just interacted with
    THAT widget silently clobbers their pending change before the widget
    ever sees it (confirmed empirically, not just suspected). A single
    shared widget sidesteps the problem entirely rather than working around
    it. Returns (visible_keys, label_by_key, key_by_label)."""
    custom_columns = load_custom_columns()
    optional_defs, label_by_key, key_by_label, all_labels, default_visible = build_column_defs(labels, custom_columns)

    if active_market:
        render_sort_control(active_market, active_market_label or active_market,
                            label_by_key, key_by_label, sample_rows, other_tabs)

    SHARED_ORDER_KEY = "shared_col_order"
    if SHARED_ORDER_KEY not in st.session_state:
        # Saved preference (column_prefs.json) wins if present -- this is
        # what makes a layout chosen once, then pushed to GitHub, show up
        # identically for anyone who opens the app (including a fresh
        # session on the deployed instance), instead of only ever living in
        # that one browser session's memory.
        saved_order = load_column_prefs()
        if saved_order:
            st.session_state[SHARED_ORDER_KEY] = saved_order
        else:
            st.session_state[SHARED_ORDER_KEY] = [key_by_label[lbl] for lbl in default_visible]
            # Write the file immediately so it always exists after the app's
            # very first render -- push_all_config (the GitHub push button)
            # fails the WHOLE atomic push if any targeted file is missing,
            # so column_prefs.json can't be allowed to only appear lazily
            # after the user first touches the picker.
            save_column_prefs(st.session_state[SHARED_ORDER_KEY])
    # Drop any keys that no longer exist (e.g. a future app update renames
    # or removes a column) so stale saved state can't crash the lookup below.
    st.session_state[SHARED_ORDER_KEY] = [k for k in st.session_state[SHARED_ORDER_KEY] if k in label_by_key]

    # Ensure every fundamental column (Sentiment, Qtr Profit/Revenue Growth %)
    # is present in the active order if missing -- e.g. a saved
    # column_prefs.json from before these columns existed, or before the
    # "Show fundamental columns" toggle was last turned on, would otherwise
    # never surface them even though they're now available in label_by_key.
    # Skipped for any key currently toggled off (it won't be in label_by_key
    # in that case -- nothing to force back in).
    # Iterate in optional_defs order (not the set) so multiple new fundamental
    # columns are inserted in the correct sequence (sets have no guaranteed order).
    #
    # Restricted to columns that are VISIBLE by default. A saved order is the
    # visible-column list, so force-inserting here turns a column on in every
    # existing layout -- which is right for a genuinely new everyday column,
    # but wrong for a fundamental that ships hidden on purpose (PAT/Revenue
    # Growth TTM are scan inputs, and blank on any ticker whose data source
    # doesn't reach back eight quarters). Without this filter, declaring such
    # a column in FUNDAMENTAL_COLUMN_KEYS silently overrides default_hidden.
    fund_keys_in_order = [k for k, lbl in optional_defs
                          if k in FUNDAMENTAL_COLUMN_KEYS and lbl in default_visible]
    inserted = False
    for fund_key in fund_keys_in_order:
        if fund_key in label_by_key and fund_key not in st.session_state[SHARED_ORDER_KEY]:
            order = st.session_state[SHARED_ORDER_KEY]
            # Land the new column in its declared optional_defs position rather
            # than at the end of the fundamental group: sit it right after the
            # nearest fundamental column that already precedes it in
            # optional_defs. That keeps siblings together (Qtr EPS Growth %
            # belongs beside Qtr Profit Growth %, not eight slots away after
            # ROE %). Falls back to the end of the group when no such
            # predecessor is on screen.
            preceding = [k for k in fund_keys_in_order[:fund_keys_in_order.index(fund_key)]
                         if k in order]
            fund_positions = [order.index(k) for k in FUNDAMENTAL_COLUMN_KEYS if k in order]
            if preceding:
                idx = order.index(preceding[-1])
            elif fund_positions:
                idx = max(fund_positions)
            elif "tech_uptrend_label" in order:
                idx = order.index("tech_uptrend_label")
            else:
                order.append(fund_key)
                inserted = True
                continue
            order.insert(idx + 1, fund_key)
            inserted = True

    if inserted:
        save_column_prefs(st.session_state[SHARED_ORDER_KEY])

    with st.sidebar.expander("Columns to show / reorder", expanded=False):
        st.caption(
            "Applies to both the US and India tables. Ticker and Last always show first. "
            "Saved to column_prefs.json -- push it via the Alert Rules tab's GitHub button "
            "to make this layout show up on the deployed app too."
        )
        from stock_data import load_settings, save_settings
        fund_settings = load_settings()
        show_fundamentals = st.checkbox(
            "Show fundamental columns (Sentiment, Qtr Profit/Revenue Growth %)",
            value=fund_settings.get("show_fundamental_columns", True),
            key="show_fundamental_columns_toggle",
            help="Turn off to hide all fundamental-data columns at once, instead of unchecking them individually below.",
        )
        if show_fundamentals != fund_settings.get("show_fundamental_columns", True):
            fund_settings["show_fundamental_columns"] = show_fundamentals
            save_settings(fund_settings)
            st.rerun()

        current_labels = [label_by_key[k] for k in st.session_state[SHARED_ORDER_KEY] if k in label_by_key]
        multiselect_key = f"shared_col_multiselect_{hash(tuple(st.session_state[SHARED_ORDER_KEY]))}"
        visible_labels = st.multiselect(
            "Columns to show", options=all_labels, default=current_labels, key=multiselect_key
        )
        selected_keys = [key_by_label[lbl] for lbl in visible_labels if lbl in key_by_label]

        # Keep the existing order for columns that are still selected,
        # append any newly-selected ones at the end, drop deselected ones.
        order = [k for k in st.session_state[SHARED_ORDER_KEY] if k in selected_keys]
        for k in selected_keys:
            if k not in order:
                order.append(k)
        if order != st.session_state[SHARED_ORDER_KEY]:
            st.session_state[SHARED_ORDER_KEY] = order
            save_column_prefs(order)

        if order:
            st.caption("Drag to reorder (top = leftmost column in the table):")
            # Key intentionally depends on the SET of selected columns, not
            # their order -- streamlit_sortables keeps its own internal drag
            # state pinned to a fixed key, so pure reordering (same set,
            # different order) needs a stable key to feel like a normal
            # controlled widget. But if the set changes (a column added or
            # removed via the multiselect above), we WANT a clean remount
            # with the fresh item list rather than stale internal state, so
            # the key is derived from the sorted set of item labels.
            sortable_key = "shared_col_sortable_" + "|".join(sorted(order))
            sorted_labels = sort_items(
                [label_by_key[k] for k in order],
                direction="vertical",
                key=sortable_key,
            )
            new_order = [key_by_label[lbl] for lbl in sorted_labels if lbl in key_by_label]
            if new_order and new_order != order:
                st.session_state[SHARED_ORDER_KEY] = new_order
                save_column_prefs(new_order)
                st.rerun()

    render_category_order_manager(label_by_key)
    render_metric_glossary(labels, custom_columns, key_by_label)
    render_custom_columns_manager()
    render_ticker_notes_manager()

    return st.session_state[SHARED_ORDER_KEY], label_by_key, key_by_label


def render_category_order_manager(label_by_key):
    """Sidebar panel for ranking each categorical column's values.

    These columns can't sort alphabetically in any useful way -- Trend would
    run Downtrend, Strong Downtrend, Strong Uptrend, Uptrend -- so the order
    is yours to set, and it drives apply_sort via load_category_order().

    Deliberately GLOBAL rather than per-tab: "Strong Uptrend outranks
    Uptrend" is a fact about the metric, not about a watchlist. Each tab's
    own direction control still flips the ranking independently.
    """
    saved = load_column_prefs_full().get("category_order") or {}
    current = load_category_order()

    # Metric field -> the label its column is shown under, so the panel reads
    # in the same words as the sort dropdown. Falls back to the field name for
    # a metric with no column of its own.
    label_for = {
        "trend": "Trend", "volume_trend": "Vol Trend", "sentiment": "Sentiment",
        "expert_take": "Expert Take", "expert_news_backed": "Expert News?",
        "flag": "Flag", "tech_uptrend": "Tech Uptrend",
        "vstop_weekly_direction": "VStop Dir", "interested": "Interested",
    }

    with st.sidebar.expander("Category sort order", expanded=False):
        st.caption(
            "Drag to rank each column's values (top = sorted first). Applies to "
            "every tab; a tab's own ↑/↓ still reverses it."
        )
        for field, values in current.items():
            st.markdown(f"**{label_for.get(field, field)}**")
            # Same keying rule as the column reorder above: stable across pure
            # reordering (streamlit_sortables pins its internal drag state to
            # the key), but remounted if the SET of values ever changes.
            sortable_key = f"cat_order_{field}_" + "|".join(sorted(values))
            new_values = sort_items(values, direction="vertical", key=sortable_key)
            if new_values and new_values != values:
                saved[field] = new_values
                update_column_prefs("category_order", saved)
                st.rerun()


def render_metric_glossary(labels, custom_columns, column_key_by_label):
    """Sidebar reference for every metric the filter/alert builders offer,
    sitting right under the column picker -- which is where you look when a
    metric name means nothing to you.

    Deliberately keyed off get_all_filterable_metrics rather than the column
    list, because the two differ: a metric can be filterable without being a
    table column (the breakout metrics were exactly that, which is why
    'Breakout Window' was findable in the alert builder but nowhere else).
    Those are marked so it's clear why they're missing from the picker
    above. Metrics with no definition still get listed, by label alone --
    hiding them would disguise the gap."""
    settings = load_settings()
    metrics = get_all_filterable_metrics(settings, custom_columns)
    defs = metric_definitions(settings, labels, custom_columns)

    def _is_a_column(label, key):
        """A metric shows up as a column under EITHER the same label
        ("Tech Uptrend" is the metric tech_uptrend but the column
        tech_uptrend_label) OR the same key (the EMA columns, whose label
        tracks the configured period). last_close is the mandatory "Last"
        column, which build_column_defs deliberately omits. Matching on only
        one of the three mislabels real columns as alert-only."""
        return (label in column_key_by_label
                or key in set(column_key_by_label.values())
                or key == "last_close")

    with st.sidebar.expander("Metric glossary", expanded=False):
        st.caption(
            "Every metric available in the custom filter and alert condition builders. "
            "‘Not a table column’ means it can be filtered/alerted on but isn't offered in "
            "the column picker above."
        )
        query = st.text_input(
            "Search metrics", "", key="metric_glossary_search",
            placeholder="e.g. breakout",
        ).strip().lower()

        shown = 0
        for label in sorted(metrics):
            key = metrics[label]
            text = defs.get(key, "")
            if query and query not in label.lower() and query not in text.lower():
                continue
            shown += 1
            note = "" if _is_a_column(label, key) else " · *not a table column*"
            st.markdown(f"**{label}**{note}")
            st.caption(text or "_No definition written yet._")

        if not shown:
            st.caption(f"No metric matches “{query}”.")


def render_custom_columns_manager():
    """Sidebar 'Custom Columns' expander, right below 'Columns to show /
    reorder' -- add/edit/delete a formula-based computed column (e.g. '52W
    Distance %' = (week52_high - last_close) / week52_high * 100). Once
    saved here, a custom column is picked up automatically everywhere else
    in the app: build_column_defs (so it's selectable in the picker above),
    get_all_filterable_metrics (so it's usable in the custom filter builder
    and alert conditions), and stock_data.fetch_all_markets (so its value
    is actually computed for every row, including for the headless
    alert_check.py/refresh_data.py scripts -- see custom_columns.py)."""
    custom_columns = load_custom_columns()
    if not os.path.exists(CUSTOM_COLUMNS_FILE):
        # Eagerly create the file (empty list) on first render, same reason
        # column_prefs.json does this -- push_all_config's atomic push fails
        # the WHOLE push if any targeted file doesn't exist yet, and
        # custom_columns.json is now in SYNCABLE_FILES.
        save_custom_columns(custom_columns)
    metrics_by_label = get_filterable_metrics(load_settings())
    metric_labels_sorted = sorted(metrics_by_label.keys())
    valid_names = set(metrics_by_label.values())

    def _insert_metric_row(formula_key, widget_suffix):
        """Renders a 'pick a metric -> Insert' row that appends the chosen
        metric's exact key to the formula text at `formula_key` (in
        st.session_state) and reruns -- lets the user build a formula by
        choosing columns from a dropdown instead of hand-typing metric
        keys, which is easy to mistype (e.g. week52low vs week52_low)."""
        ins1, ins2 = st.columns([3, 1])
        choice = ins1.selectbox(
            "Insert a metric", metric_labels_sorted,
            key=f"cc_insert_choice_{widget_suffix}", label_visibility="collapsed",
        )
        if ins2.button("+ Insert", key=f"cc_insert_btn_{widget_suffix}", width="stretch"):
            key_to_insert = metrics_by_label[choice]
            current = st.session_state.get(formula_key, "")
            sep = "" if not current or current.endswith((" ", "(")) else " "
            st.session_state[formula_key] = f"{current}{sep}{key_to_insert}"
            st.rerun()

    with st.sidebar.expander("Custom Columns", expanded=False):
        st.caption(
            "Define a computed column as a formula over existing metrics -- "
            "e.g. `(week52_high - last_close) / week52_high * 100` for '% off 52W high'. "
            "Use the 'Insert a metric' picker below to add exact column names without typing "
            "them by hand. Only + - * / ** and parentheses are allowed, no other functions. "
            "Saved to custom_columns.json -- push it via the Alert Rules tab's GitHub button "
            "to make it show up on the deployed app too."
        )
        with st.expander("All available metric keys", expanded=False):
            ref_lines = "\n".join(f"- `{k}` — {lbl}" for lbl, k in sorted(metrics_by_label.items()))
            st.markdown(ref_lines)

        if custom_columns:
            st.caption(f"{len(custom_columns)} custom column(s):")
            for col in list(custom_columns):
                with st.container(border=True):
                    st.markdown(f"**{col['name']}** {'' if col.get('enabled', True) else '_(disabled)_'}")
                    st.code(col.get("formula", ""), language=None)
                    cc1, cc2, cc3 = st.columns(3)
                    new_name = cc1.text_input("Name", value=col["name"], key=f"cc_name_{col['id']}")
                    new_format = cc2.selectbox(
                        "Format", list(FORMAT_CHOICES.keys()),
                        index=list(FORMAT_CHOICES.keys()).index(col.get("format", "number")),
                        format_func=lambda k: FORMAT_CHOICES[k], key=f"cc_format_{col['id']}",
                    )
                    new_enabled = cc3.checkbox("Enabled", value=col.get("enabled", True), key=f"cc_enabled_{col['id']}")
                    formula_key = f"cc_formula_{col['id']}"
                    if formula_key not in st.session_state:
                        # Seed once from the saved formula. Deliberately NOT
                        # passing value=... on the text_input below (every
                        # render) -- Streamlit warns/rejects that combo once
                        # _insert_metric_row() has explicitly written to this
                        # session_state key (the "Insert" button), since a
                        # literal value= and a programmatic session_state
                        # write to the same widget key conflict.
                        st.session_state[formula_key] = col.get("formula", "")
                    _insert_metric_row(formula_key, col["id"])
                    new_formula = st.text_input("Formula", key=formula_key)
                    ok, err = validate_formula(new_formula, valid_names)
                    if not ok:
                        st.error(err)
                    bcol1, bcol2 = st.columns(2)
                    if bcol1.button("Save", key=f"cc_save_{col['id']}", disabled=not ok, width="stretch"):
                        col["name"], col["formula"], col["format"], col["enabled"] = (
                            new_name.strip() or col["name"], new_formula, new_format, new_enabled,
                        )
                        save_custom_columns(custom_columns)
                        st.rerun()
                    if bcol2.button("Delete", key=f"cc_del_{col['id']}", width="stretch"):
                        custom_columns.remove(col)
                        save_custom_columns(custom_columns)
                        st.rerun()

        st.markdown("**Add a new custom column**")
        nc1, nc2 = st.columns(2)
        add_name = nc1.text_input("Name", key="cc_new_name", placeholder="e.g. % off 52W High")
        add_format = nc2.selectbox(
            "Format", list(FORMAT_CHOICES.keys()), format_func=lambda k: FORMAT_CHOICES[k], key="cc_new_format",
        )
        _insert_metric_row("cc_new_formula", "new")
        add_formula = st.text_input(
            "Formula", key="cc_new_formula",
            placeholder="e.g. (week52_high - last_close) / week52_high * 100",
        )
        add_ok, add_err = (False, "") if not add_formula.strip() else validate_formula(add_formula, valid_names)
        if add_formula.strip() and not add_ok:
            st.error(add_err)
        if st.button("＋ Add custom column", disabled=not (add_name.strip() and add_ok), width="stretch"):
            custom_columns.append({
                "id": uuid.uuid4().hex[:8],
                "name": add_name.strip(),
                "formula": add_formula,
                "format": add_format,
                "enabled": True,
            })
            save_custom_columns(custom_columns)
            st.rerun()


def render_ticker_notes_manager():
    """Sidebar 'Ticker Notes' expander -- pick any ticker from either
    market, jot a free-text note and/or pick a flag color, save instantly
    to ticker_notes.json. This IS the editing surface: the main watchlist
    table is a static HTML table (needed for the sticky header/frozen
    ticker column, see sticky_header_html), so real inline cell-editing
    isn't practical there -- the table only ever displays the result (a
    Notes column, plus the flag color prepended to the ticker symbol
    itself, via flag_marker_html)."""
    watchlists = load_watchlists()
    all_tickers = sorted(set(t for tickers in watchlists.values() for t in tickers))
    if not all_tickers:
        return

    notes = load_ticker_notes()
    if not os.path.exists(TICKER_NOTES_FILE):
        # Eager-write, same reason column_prefs.json/custom_columns.json do
        # this -- push_all_config's atomic push fails the WHOLE push if any
        # targeted file doesn't exist yet, and ticker_notes.json is in
        # SYNCABLE_FILES.
        save_ticker_notes(notes)

    with st.sidebar.expander("Ticker Notes", expanded=False):
        st.caption(
            "Jot a note and/or pick a flag color for any ticker -- the note shows up "
            "in the Notes column, and the flag color marks the ticker symbol itself. "
            "Saved to ticker_notes.json -- push it via the Alert Rules tab's GitHub button "
            "to make it show up on the deployed app too."
        )
        picked = st.selectbox("Ticker", all_tickers, key="tn_picked_ticker")
        current_note = get_ticker_note(notes, picked)
        current_flag = get_ticker_flag(notes, picked)

        flag_options = ["(none)"] + FLAG_CHOICES
        current_flag_index = flag_options.index(current_flag) if current_flag in flag_options else 0
        new_flag_choice = st.selectbox(
            "Flag", flag_options, index=current_flag_index, key=f"tn_flag_{picked}",
            format_func=lambda f: f"{FLAG_EMOJI[f]} {f}" if f in FLAG_EMOJI else "(none)",
        )
        new_flag = "" if new_flag_choice == "(none)" else new_flag_choice
        
        note_opts_str = load_settings().get("note_dropdown_options", "").strip()
        if note_opts_str:
            options = [o.strip() for o in note_opts_str.split(",") if o.strip()]
            options = [""] + options + ["Custom..."]
            
            if current_note and current_note not in options and current_note != "Custom...":
                idx = options.index("Custom...")
            else:
                idx = options.index(current_note) if current_note in options else 0
                
            selected_note_opt = st.selectbox("Note Dropdown", options, index=idx, key=f"tn_note_sel_{picked}")
            
            if selected_note_opt == "Custom..." or (current_note and current_note not in options and current_note != "Custom..."):
                new_note = st.text_area("Custom Note", value=current_note, key=f"tn_note_{picked}", height=80)
            else:
                new_note = selected_note_opt
        else:
            new_note = st.text_area("Note", value=current_note, key=f"tn_note_{picked}", height=80)

        scol1, scol2 = st.columns(2)
        if scol1.button("Save", key=f"tn_save_{picked}", width="stretch"):
            set_ticker_note(notes, picked, new_note, new_flag)
            save_ticker_notes(notes)
            st.rerun()
        if scol2.button("Clear", key=f"tn_clear_{picked}", width="stretch"):
            set_ticker_note(notes, picked, "", "")
            save_ticker_notes(notes)
            st.rerun()

        if notes:
            st.caption(f"{len(notes)} ticker(s) with notes/flags:")
            for tk in sorted(notes.keys()):
                entry = notes[tk]
                emoji = FLAG_EMOJI.get(entry.get("flag", ""), "")
                note_text = entry.get("note", "")
                preview = (note_text[:60] + "…") if len(note_text) > 60 else note_text
                line = f"{emoji} **{tk}**" if emoji else f"**{tk}**"
                if preview:
                    line += f" — {preview}"
                st.markdown(line)


def sync_ai_views_to_github(message, filenames=("expert_views.json", "fundamentals.json")):
    """Push the AI result files back to GitHub.

    Covers fundamentals.json as well as expert_views.json: the re-analyze
    buttons now refresh Sentiment too, and a local-only fundamentals.json would
    be silently overwritten by the next pull.
    """
    token, repo, branch = get_github_config(st.secrets)
    if token and repo:
        ok, msg = push_all_config(
            token, repo, branch,
            filenames=list(filenames),
            message=message
        )
        if ok:
            st.toast("✓ Saved & committed updated AI views to GitHub!")
        else:
            st.toast(f"✓ Saved locally! (GitHub sync: {msg})")
    else:
        st.toast("✓ Saved updated AI views!")


def trigger_ai_refresh_workflow(workflow_file, name, label, markets):
    """Kick off ONE background AI refresh workflow, scoped to `markets`.

    `markets` is the list of REAL registry keys behind the tab the button was
    clicked from (a combined tab passes its member list). It's forwarded as the
    workflow's `markets` input, which refresh_expert_views.py /
    refresh_fundamentals.py read back as REFRESH_MARKETS to filter their
    per-watchlist loop -- without it, a "Re-analyze All" clicked from one tab
    re-ran every watchlist in the repo.

    An empty `markets` is refused rather than sent: blank means "all
    watchlists" to those scripts, i.e. exactly the behavior being fixed.
    """
    if not markets:
        st.error("No watchlists configured for this view -- nothing to refresh.")
        return
    try:
        from github_sync import trigger_github_workflow, get_github_config
        token, repo, _ = get_github_config(getattr(st, "secrets", None))
        if not token or not repo:
            st.error("GitHub credentials not found.")
            return
        ok, msg = trigger_github_workflow(
            token, repo, workflow_file, inputs={"markets": ",".join(markets)}
        )
        if ok:
            # "queued", not "running": concurrency group is per-workflow with
            # cancel-in-progress false, so this waits out any nightly run.
            st.success(f"{label}: {name} refresh queued in background for {', '.join(markets)}.")
        else:
            st.error(f"{name} refresh failed: {msg}")
    except Exception as e:
        st.error(f"Failed to start refresh: {e}")


def _reanalyze_tickers_in_dashboard(tickers, results, api_key, sync_message,
                                    scope):
    """Refreshes ONE AI column for `tickers` synchronously, right here in the
    dashboard -- no GitHub Actions workflow involved.

    `scope` is "expert" (Expert Take) or "sentiment" (Sentiment). The two
    columns used to be refreshed together in a single pass, which meant a
    ticker pending in only one of them still paid for both; the controls are
    now split into per-column sections, so each button drives exactly one
    column and syncs only the JSON file it touched.

    Shared by each section's "selected" and "Retry Pending" buttons so both
    scopes refresh identically; kept out of the per-ticker re-analyze button
    (it has its own st.spinner-based version, and deliberately still does both
    columns for its one ticker) since that flow doesn't need a progress bar.
    """
    label = "Expert Take" if scope == "expert" else "Sentiment"
    progress_bar = st.progress(0, text=f"Starting selective {label} re-analysis...")
    from google import genai
    client = genai.Client(api_key=api_key) if scope == "expert" else None
    updated_views = load_expert_views() if scope == "expert" else None
    # Section 2 of the Expert Take prompt. Evaluated once for the whole market,
    # not per ticker -- see alerts.active_alerts_by_ticker. `results` is the
    # full market row set, which is what rule scoping and rule-to-rule
    # references need; passing only the selected tickers would change which
    # rules resolve as true.
    alerts_by_ticker = active_alerts_for_prompt(results) if scope == "expert" else None

    for idx, tk in enumerate(tickers):
        row = next((r for r in results if r["ticker"] == tk), None)
        if not row:
            continue
        company_name = row.get("company_name", tk)
        progress_bar.progress(
            (idx + 1) / len(tickers),
            text=f"{label} for {company_name} ({idx+1}/{len(tickers)})...",
        )
        if scope == "expert":
            try:
                view = generate_expert_view(
                    client, row,
                    active_alerts_text=alerts_text_for(alerts_by_ticker, tk),
                    is_retry=True,
                )
                if _is_valid_view(view):
                    updated_views[tk] = view
                    save_expert_views(updated_views)
                else:
                    progress_bar.progress(
                        (idx + 1) / len(tickers),
                        text=f"⚠️ {tk} expert analysis pending/fallback.",
                    )
            except Exception as e:
                print(f"[re-analyze] expert take failed for {tk}: {e}")
                progress_bar.progress(
                    (idx + 1) / len(tickers),
                    text=f"⚠️ {tk} expert analysis error: {e}",
                )
        else:
            # A failure here keeps the prior view (see
            # analyze_single_ticker_sentiment) and must not abort the loop.
            try:
                analyze_single_ticker_sentiment(tk, row, api_key, is_retry=True)
            except Exception as e:
                print(f"[re-analyze] sentiment failed for {tk}: {e}")
                progress_bar.progress(
                    (idx + 1) / len(tickers),
                    text=f"⚠️ {tk} sentiment error: {e}",
                )
        time.sleep(3)

    # Only the one file this pass could have written -- a narrower commit than
    # the old both-columns default.
    filename = "expert_views.json" if scope == "expert" else "fundamentals.json"
    sync_ai_views_to_github(sync_message, filenames=(filename,))
    st.rerun()


def render_expert_analysis_control_bar(market, results, combined_markets=None):
    expert_views = load_expert_views()
    api_key = get_gemini_api_key(st.secrets)

    all_tickers = [r["ticker"] for r in results]

    # --- Auto-cleanup: remove stale tickers no longer in watchlist ---
    from stock_data import load_watchlists
    global_watchlists = load_watchlists()
    global_all_tickers = [tk for mkt_tks in global_watchlists.values() for tk in mkt_tks]

    stale_keys = [tk for tk in expert_views if tk not in global_all_tickers]
    if stale_keys:
        for tk in stale_keys:
            del expert_views[tk]
        save_expert_views(expert_views)
        sync_ai_views_to_github(
            f"Auto-cleanup: removed {len(stale_keys)} deleted ticker(s) from expert_views",
            filenames=("expert_views.json",),  # cleanup touches only this file
        )

    # --- Detect pending tickers per AI column (includes newly added ones with
    # no entry at all) ---
    # Tracked separately, one list per column: the controls below are split
    # into an Expert Take section and a Sentiment section, and each section's
    # "Retry Pending" must only re-run the column it owns. A ticker whose
    # Expert Take is fine but whose Sentiment is pending shows up in exactly
    # one of these -- previously a single merged list forced both columns to
    # be regenerated for it.
    fundamentals_now = load_fundamentals()
    expert_pending = [tk for tk in all_tickers if not _is_valid_view(expert_views.get(tk))]
    sentiment_pending = [
        tk for tk in all_tickers if not _is_valid_sentiment_view(fundamentals_now.get(tk))
    ]

    # The REAL registry keys behind this tab, for scoping the background
    # workflow. A combined tab spans its configured member watchlists; a real
    # tab is just itself. Empty only for a combined tab with zero members --
    # see the disabled "Re-analyze All" below, since an empty `markets` input
    # would mean "every watchlist" to the refresh scripts.
    scope_markets = list(combined_markets) if combined_markets is not None else [market]

    # Folded by default: these are set-once controls, and leaving them open
    # pushed the watchlist table below the fold on every visit. The loads and
    # the stale-ticker cleanup above stay outside -- they are side effects that
    # must run whether or not the panel is open.
    from stock_data import load_settings, save_settings
    settings_now = load_settings()

    REASON_CHOICES = ["models/gemini-3.5-flash-lite", "models/gemma-4-31b-it", "models/gemma-4-26b-a4b-it"]
    DEFAULT_REASON = "models/gemini-3.5-flash-lite"

    def _normalize_reason(val):
        """Legacy settings stored the bare id; every choice is 'models/'-prefixed now."""
        return DEFAULT_REASON if val == "gemini-3.5-flash-lite" else val

    def _render_ai_section(scope, heading, pending, model_key, budget_key,
                           workflow_file, workflow_name):
        """One column's controls: model + thinking budget, a ticker picker, and
        the three run buttons. Both sections are built from this so Expert Take
        and Sentiment can never drift apart in layout or behavior -- only in
        which settings keys, pending list and workflow they drive.

        Widget keys are namespaced by BOTH scope and market: two sections on
        every one of several tabs, all live in the same session_state.
        """
        st.markdown(f"##### {heading}")
        col1, col2, col3 = st.columns([4, 3, 3])
        with col1:
            st.caption("Search is powered by gemma-4-26b-a4b-it")
        with col2:
            saved_reason = _normalize_reason(settings_now.get(model_key, DEFAULT_REASON))
            new_reason = st.selectbox(
                "Reasoning Model", REASON_CHOICES,
                index=REASON_CHOICES.index(saved_reason) if saved_reason in REASON_CHOICES else 0,
                key=f"{scope}_reasoning_model_select_{market}",
            )
            if new_reason != saved_reason:
                settings_now[model_key] = new_reason
                save_settings(settings_now)
                st.rerun()

        with col3:
            # Gemma exposes a named reasoning LEVEL; Gemini a numeric token
            # budget. Swapping the model swaps the whole choice list.
            is_gemma = "gemma" in settings_now.get(model_key, "")
            if is_gemma:
                budget_choices = ["LOW", "MEDIUM", "HIGH"]
                default_val = "HIGH"
            else:
                budget_choices = [1024, 2048, 4096, 8192]
                default_val = 8192

            current_val = settings_now.get(budget_key, default_val)

            if is_gemma and current_val not in budget_choices:
                current_val = default_val
            elif not is_gemma:
                try:
                    current_val = int(current_val)
                except (ValueError, TypeError):
                    current_val = default_val
                if current_val not in budget_choices:
                    current_val = default_val

            new_budget = st.selectbox(
                "Thinking Budget / Level", budget_choices,
                index=budget_choices.index(current_val),
                key=f"{scope}_reasoning_budget_select_{market}",
            )
            if new_budget != current_val:
                settings_now[budget_key] = new_budget
                save_settings(settings_now)
                st.rerun()

        c1, c2, c3, c4 = st.columns([3.5, 1.3, 1.8, 1.4])

        selected_to_reanalyze = c1.multiselect(
            "Select tickers to re-analyze",
            options=all_tickers,
            key=f"ev_multisel_{scope}_{market}",
            placeholder=f"Search tickers to re-analyze ({heading})...",
            label_visibility="collapsed",
        )

        if c2.button(
            f"⚡ Re-analyze ({len(selected_to_reanalyze)})",
            key=f"btn_re_sel_{scope}_{market}",
            disabled=not selected_to_reanalyze or not api_key,
            width="stretch",
        ):
            _reanalyze_tickers_in_dashboard(
                selected_to_reanalyze, results, api_key,
                f"Re-analyze selected {scope} ({len(selected_to_reanalyze)} tickers) via UI",
                scope=scope,
            )

        pending_count = len(pending)
        if c3.button(
            f"⚠️ Retry Pending ({pending_count})",
            key=f"btn_re_failed_{scope}_{market}",
            disabled=pending_count == 0 or not api_key,
            type="primary" if pending_count > 0 else "secondary",
            width="stretch",
        ):
            _reanalyze_tickers_in_dashboard(
                pending, results, api_key,
                f"Retry pending {scope} ({pending_count} tickers) via UI",
                scope=scope,
            )

        if c4.button(
            f"🔄 Re-analyze All ({len(all_tickers)})",
            key=f"btn_re_all_{scope}_{market}",
            disabled=not scope_markets,
            help=(
                f"Runs {workflow_file} in the background for this view's watchlist(s) only: "
                f"{', '.join(scope_markets)}." if scope_markets
                else "No watchlists are configured for this view."
            ),
            width="stretch",
        ):
            trigger_ai_refresh_workflow(
                workflow_file, workflow_name,
                f"Re-analyze All ({len(all_tickers)})", scope_markets,
            )

    with st.expander("🤖 AI Analysis Controls", expanded=False):
        _render_ai_section(
            "expert", "🤖 Expert Take", expert_pending,
            "expert_reasoning_model", "expert_thinking_budget",
            "expert-views.yml", "Expert Views",
        )
        st.markdown("---")
        _render_ai_section(
            "sentiment", "🧠 Sentiment", sentiment_pending,
            "sentiment_reasoning_model", "sentiment_thinking_budget",
            "fundamentals.yml", "Sentiment",
        )


def render_expert_view_expander(market, filtered_rows, settings, results=None):
    expert_views = load_expert_views()
    tickers = [r["ticker"] for r in filtered_rows]
    if not tickers:
        return

    v_colors = {"ACCUMULATE": "#1a7a3a", "HOLD": "#7a6a00", "CAUTION": "#7a1a1a"}
    v_badges = {"ACCUMULATE": "🟢 ACCUMULATE / ADD", "HOLD": "🟡 HOLD / WATCH", "CAUTION": "🔴 CAUTION / EXIT"}
    api_key = get_gemini_api_key(st.secrets)
    # Was missing entirely, so the per-ticker re-analyze button below raised
    # NameError the moment it was clicked.

    with st.expander(f"🤖 AI Stock Expert Views ({market} — {len(tickers)} tickers)", expanded=False):
        # Scrollable container for all ticker cards
        scroll_html_open = (
            "<div style='max-height:680px; overflow-y:auto; padding-right:6px; "
            "border:1px solid rgba(128,128,128,0.2); border-radius:8px; padding:12px;'>"
        )
        st.markdown(scroll_html_open, unsafe_allow_html=True)

        def _reanalyze_button(ticker, row):
            """Per-ticker re-analyze. Refreshes Expert Take AND Sentiment so one
            click doesn't leave the two AI columns describing different runs."""
            if not api_key:
                return
            if not st.button(f"⚡ Re-analyze {ticker}", key=f"re_ev_{market}_{ticker}",
                             width="content"):
                return
            with st.spinner(f"Re-analyzing {ticker} (Expert Take + Sentiment)..."):
                ev_ok = True
                try:
                    alerts_by_ticker = active_alerts_for_prompt(results or filtered_rows)
                    analyze_single_ticker(
                        ticker, row, api_key,
                        active_alerts_text=alerts_text_for(alerts_by_ticker, ticker),
                        is_retry=True,
                    )
                except Exception as e:
                    ev_ok = False
                    st.error(f"Expert Take refresh failed: {e}")
                try:
                    analyze_single_ticker_sentiment(ticker, row, api_key, is_retry=True)
                except Exception as e:
                    if ev_ok:
                        st.warning(f"Expert Take updated; Sentiment refresh failed: {e}")
                    else:
                        st.error(f"Sentiment refresh failed: {e}")
                sync_ai_views_to_github(f"Re-analyze single ticker ({ticker}) via UI")
                st.rerun()

        for ticker in tickers:
            view = expert_views.get(ticker)
            row = next(r for r in filtered_rows if r["ticker"] == ticker)

            if not view or not _is_valid_view(view):
                # Compact pending card. It gets a re-analyze button too -- these
                # are precisely the tickers worth re-running, and the early
                # `continue` used to deny them one.
                st.markdown(
                    f"<div style='padding:10px 14px; margin-bottom:8px; border-radius:8px; "
                    f"border:1px solid rgba(128,128,128,0.25); background:rgba(128,128,128,0.05);'>"
                    f"<b>{ticker}</b> &nbsp;⚪ Pending — no AI analysis yet.</div>",
                    unsafe_allow_html=True,
                )
                _reanalyze_button(ticker, row)
                continue

            verdict = view.get("verdict", "")
            badge = v_badges.get(verdict, "⚪ PENDING")
            color = v_colors.get(verdict, "#555")
            news_source = view.get("news_source", "")
            as_of = view.get("as_of", "")

            card_html = (
                f"<div style='padding:14px 16px; margin-bottom:10px; border-radius:10px; "
                f"border-left:4px solid {color}; border:1px solid rgba(128,128,128,0.2); "
                f"background:rgba(0,0,0,0.03);'>"
                f"<div style='font-size:1.05em; font-weight:700; margin-bottom:4px;'>"
                f"{ticker} &nbsp; <span style='color:{color}'>{badge}</span></div>"
                f"<div style='font-size:0.9em; margin-bottom:6px; opacity:0.85;'>"
                f"<b>Headline:</b> {view.get('headline', '')}</div>"
                f"<div style='font-size:0.85em; margin-bottom:4px;'>"
                f"<b>📊 Technical:</b> {view.get('technical_summary', '')}</div>"
                f"<div style='font-size:0.85em; margin-bottom:4px;'>"
                f"<b>📰 Catalyst:</b> {view.get('catalyst_summary', '')}</div>"
                f"<div style='font-size:0.85em; background:rgba(0,100,255,0.06); "
                f"border-radius:6px; padding:6px 10px; margin-bottom:6px;'>"
                f"<b>💡 Action:</b> {view.get('actionable_take', '')}</div>"
                f"<div style='font-size:0.75em; opacity:0.55;'>"
                f"Generated: {as_of} · News: {news_source} · Model: {view.get('model_used', 'gemini-3.5-flash-lite')}</div>"
                f"</div>"
            )
            st.markdown(card_html, unsafe_allow_html=True)

            # Per-ticker re-analyze button
            _reanalyze_button(ticker, row)

        st.markdown("</div>", unsafe_allow_html=True)


def render_market_tab(market, results, settings, visible_keys, label_by_key, sort_levels=None,
                       combined_markets=None, combined_label=None):
    """Renders one watchlist table tab. `market` is either a real registry
    key (single-market tab) or a synthetic key like "all_invested" used only
    for widget-key namespacing / CSV filenames when `combined_markets` is
    set (a combined tab spanning several real markets' concatenated
    `results`) -- see the module-level combined-tab wiring below for how
    the two are told apart. A combined tab has no single benchmark, no
    watchlist editor (ticker membership is edited from the real per-market
    tabs, unaffected by this), and a friendlier empty-state message."""
    benchmarks = get_benchmarks(settings)
    labels = ema_col_labels(settings)
    custom_columns = load_custom_columns()
    filterable_metrics = get_all_filterable_metrics(settings, custom_columns)

    if combined_markets is not None:
        registry_now = load_markets_registry()
        # Used both in the caption below and in the RS-formula footer glossary
        # further down -- a combined tab has no single benchmark, so this is
        # a legend of each underlying market's, not one ticker symbol. Empty
        # when zero members are configured -- " · ".join of nothing is "".
        bench = " · ".join(
            f"{registry_now.get(m, {}).get('label', m)} vs {benchmarks.get(m, 'SPY')}" for m in combined_markets
        ) or "no watchlists configured yet"
        rs_caption = f"Mansfield RS vs each row's own index benchmark ({bench}) -- see the Index column"
    else:
        bench = benchmarks[market]
        rs_caption = f"Mansfield RS vs {bench}"
    st.caption(
        f"{labels['w_fast']}/{labels['w_mid']}/{labels['w_slow']} · "
        f"{labels['d_fast']}/{labels['d_mid']}/{labels['d_slow']} · "
        f"RSI({settings['rsi_period']}) D/W/M · {rs_caption} "
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

    # combined_markets is a LIST for a combined tab (possibly empty, if every
    # member was unchecked) and None for a real tab -- checked by identity,
    # not truthiness, so an empty-but-configured combined tab doesn't fall
    # through to the real-tab branch below (which would try to edit a
    # watchlist named "all_invested"/"all_watchlist" and break).
    is_combined = combined_markets is not None
    if is_combined:
        market_display_label = combined_label or market
        registry_all = load_markets_registry()
        # Every REAL market is a candidate member, regardless of whether it's
        # currently in this group -- membership across the two combined tabs
        # is independent (a watchlist can feed both, one, or neither).
        member_label_to_key = {info["label"]: k for k, info in registry_all.items()}
        member_key_to_label = {k: lbl for lbl, k in member_label_to_key.items()}
        current_labels = [member_key_to_label[k] for k in combined_markets if k in member_key_to_label]
        with st.expander(f"⚙️ Configure {market_display_label}", expanded=False):
            st.caption(
                "This view is a read-only, derived merge of the watchlists selected below -- "
                "ticker lists are edited from each watchlist's own tab, not here."
            )
            selected_labels = st.multiselect(
                "Watchlists feeding this view", options=list(member_label_to_key.keys()),
                default=current_labels, key=f"group_members_{market}",
            )
            if st.button("Save", key=f"save_group_{market}"):
                new_members = [member_label_to_key[lbl] for lbl in selected_labels]
                groups = load_watchlist_groups()
                groups[market] = new_members
                save_watchlist_groups(groups)
                gh_token, gh_repo, gh_branch = get_github_config(getattr(st, "secrets", None))
                if gh_token and gh_repo:
                    ok, msg = push_all_config(
                        gh_token, gh_repo, gh_branch, filenames=["watchlist_groups.json"],
                        message=f"Update {market_display_label} membership",
                    )
                    if not ok:
                        st.error(f"Failed to push to GitHub: {msg}")
                st.rerun()
    else:
        watchlists = load_watchlists()
        market_display_label = load_markets_registry().get(market, {}).get("label", market)
        _wl_empty = not watchlists.get(market)
        with st.expander(f"Edit {market_display_label}", expanded=_wl_empty):
            render_watchlist_editor(market, watchlists)

    if not results:
        empty_msg = f"No {market_display_label} tickers with enough data yet."
        if not is_combined:
            empty_msg += " Add tickers above."
        elif not combined_markets:
            empty_msg += " No watchlists are feeding this view yet -- configure it above."
        else:
            empty_msg += " Add tickers from the individual tabs above."
        st.info(empty_msg)
        return

    # These get set once and rarely touched, so they live folded away -- only
    # the ticker search and the saved-scan filter below stay on screen. The
    # label carries a count of what's active, because a filter you forgot you
    # set would otherwise silently narrow the table from inside a closed panel.
    # Counted from session_state, not the widget return values: the label has to
    # be decided when the expander is built, which is before its contents run.
    _filter_defaults = {
        **{f"f_{k}_{market}": "Any" for k in
           ("ema10", "ema20", "ema40", "ema10d", "ema50", "ema200",
            "trend", "voltrend", "expert")},
        **{f"f_{k}_{market}": (0, 100) for k in ("rsid", "rsiw", "rsim")},
        **{f"f_{k}_{market}": (-150, 150) for k in ("rsd", "rsw", "rsm")},
        f"f_tech_{market}": False,
    }
    _active = 0
    for _key, _default in _filter_defaults.items():
        if _key not in st.session_state:
            continue  # first render -- widget hasn't been created yet
        _current = st.session_state[_key]
        # Sliders hand back a list or a tuple depending on Streamlit version.
        if isinstance(_default, tuple):
            _current = tuple(_current)
        if _current != _default:
            _active += 1

    with st.expander(f"Filters ({_active} active)" if _active else "Filters", expanded=False):
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

        fc1, fc2, fc3, fc4 = st.columns([1.2, 1.2, 1.3, 0.8])
        f_trend = fc1.selectbox(
            "Trend", ["Any", "Strong Uptrend", "Uptrend", "Downtrend", "Strong Downtrend"],
            key=f"f_trend_{market}",
        )
        f_vol_trend = fc2.selectbox(
            "Vol Trend", ["Any", "Exploding", "In-line", "Declining"], key=f"f_voltrend_{market}",
        )
        f_expert_take = fc3.selectbox(
            "Expert Take", list(EXPERT_FILTER_VALUES),
            key=f"f_expert_{market}",
        )
        f_tech_only = fc4.checkbox("Tech Uptrend only", key=f"f_tech_{market}")

    search = st.text_input("Ticker search", "", key=f"search_{market}").strip().upper()

    with st.expander("Custom filters (metric vs metric, or metric vs fixed value; chain with AND/OR)", expanded=False):
        active_custom_filters = render_custom_filter_builder(
            market, filterable_metrics, definitions=metric_definitions(settings, labels, custom_columns)
        )

    # Filter this watchlist by any saved rule from the Alert Rules tab --
    # reuses the exact same condition-chain engine (filters.passes_filter_chain)
    # that alerts and custom filters use, so results match what that rule
    # would flag. Rules are listed regardless of the market tab they were
    # created for or their scope (US/INDIA/ALL) -- a rule is just a reusable
    # bundle of conditions here, applicable from any tab.
    market_rules_all = load_rules()
    scan_rules_all = [r for r in market_rules_all if r.get("enabled", True) and r.get("conditions")]
    scan_rule_labels = [f"{r.get('name') or '(unnamed)'} [{r['id']}]" for r in scan_rules_all]
    scan_rule_by_label = dict(zip(scan_rule_labels, scan_rules_all))
    # Mode chosen first, then the alerts: selected rules combine per this mode.
    scan_mode = st.segmented_control(
        "Match mode",
        options=["OR", "AND"],
        default="OR",
        key=f"f_scan_mode_{market}",
        help="OR: keep tickers matching any selected alert. AND: keep tickers matching every selected alert.",
    )
    selected_scan_labels = st.multiselect(
        "Filter by Saved Scans / Alerts",
        options=scan_rule_labels,
        key=f"f_scans_{market}",
        help="Applies the metric conditions from selected alert/scan rules to this watchlist, "
             f"regardless of which tab or scope the rule was originally set up under. "
             f"Selected rules combine with {scan_mode} logic.",
    )
    selected_scans = [scan_rule_by_label[lbl] for lbl in selected_scan_labels]

    # Resolve rule->rule references once for this market so the scan filter
    # and custom-filter chains work even when a selected rule references
    # another alert (passes_filter_chain needs the shared rule_truth map).
    market_rule_truth, _ = compute_rule_truth(market_rules_all, results)

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
        # Selected alert rules combine per scan_mode: OR (any match) or AND (all match).
        if selected_scans:
            rule_passes = [passes_filter_chain(row, sr.get("conditions", []), market_rule_truth) for sr in selected_scans]
            if scan_mode == "OR":
                if not any(rule_passes):
                    continue
            else:
                if not all(rule_passes):
                    continue
        # Expert Take filter -- resolved against row["expert_take"], the
        # guarded, pending-aware verdict attached in the enrichment loop
        # (module level, before any tab renders), so the dropdown, the badge
        # and the sortable column cannot disagree.
        #
        # This used to re-derive the answer here from the RAW stored verdict
        # plus some headline sniffing ("429"/"analysis pending"), which was
        # wrong in both directions: a failed-generation placeholder stores
        # verdict "HOLD", so it matched BOTH "🟡 Hold" and "⚪ Pending", and it
        # cost an uncached load_expert_views() per tab to do it.
        if f_expert_take != "Any":
            if EXPERT_FILTER_VALUES.get(f_expert_take) != row.get("expert_take"):
                continue
        filtered.append(row)

    filtered = apply_filters(filtered, active_custom_filters, market_rule_truth)
    # Each tab carries its own sort now (see saved_sort_levels), so the
    # All Watchlist-only Index-then-%Chg default that used to be hardcoded
    # here is gone -- DEFAULT_SORT covers every tab, this one included.
    filtered = apply_sort_levels(filtered, sort_levels or [])

    render_expert_analysis_control_bar(market, results, combined_markets=combined_markets)
    st.write(f"**Showing {len(filtered)} of {len(results)} tickers**")

    if filtered:
        def vstop_change_str(row):
            if row["vstop_weekly_last_change"] is None:
                return "—"
            return str(row["vstop_weekly_weeks_since_change"])

        raw_df = pd.DataFrame(filtered)
        raw_df["vstop_change"] = [vstop_change_str(r) for r in filtered]
        # Trend / Vol Trend / Tech Uptrend cells carry a hover tooltip
        # (native <span title=...>, see with_tooltip's docstring for why)
        # spelling out which of the underlying conditions passed/failed --
        # so you can see at a glance which one criterion is blocking a
        # better (or worse) read, instead of just the final label.
        raw_df["trend"] = [
            with_tooltip(r["trend"] if r["trend"] else "—", trend_tooltip(r, labels))
            for r in filtered
        ]
        raw_df["volume_trend"] = [
            with_tooltip(r["volume_trend"] if r["volume_trend"] else "—", vol_trend_tooltip(r, settings))
            for r in filtered
        ]
        raw_df["tech_uptrend_label"] = [
            with_tooltip("Yes" if r["tech_uptrend"] else "No", tech_uptrend_tooltip(r, settings, labels))
            for r in filtered
        ]
        raw_df["interested_label"] = ["Yes" if r.get("interested") else "No" for r in filtered]
        # Note text: short preview in the cell, full text on hover/tap (same
        # with_tooltip pattern as Trend/Vol Trend above) so a long note
        # doesn't blow out the column width.
        raw_df["note"] = [
            with_tooltip(_note_preview(r["note"]), r["note"] if len(r["note"]) > 40 else "")
            for r in filtered
        ]
        raw_df["flag"] = [
            with_tooltip(
                f"{FLAG_EMOJI[r['flag']]} {r['flag']}" if r["flag"] in FLAG_EMOJI else "—",
                r.get("flag_reason", "") if r["flag"] else "",
            )
            for r in filtered
        ]
        raw_df["net_volume_10d_dir"] = [
            with_tooltip(r.get("net_volume_10d_dir", "—") or "—",
                         f"Ratio: {r.get('net_volume_10d_ratio', 0)}% of total 10d vol" if r.get("net_volume_10d_dir") else "")
            for r in filtered
        ]
        
        for ccol in custom_columns:
            if ccol["id"] == "w52dist":
                col_key = f"custom_{ccol['id']}"
                raw_df[col_key] = [
                    with_tooltip(f"{r[col_key]:+.1f}%" if pd.notna(r.get(col_key)) else "—",
                                 f"52W High: {r.get('week52_high', 'N/A')}")
                    for r in filtered
                ]
            elif ccol["id"] == "w52lowdist":
                col_key = f"custom_{ccol['id']}"
                raw_df[col_key] = [
                    with_tooltip(f"{r[col_key]:+.1f}%" if pd.notna(r.get(col_key)) else "—",
                                 f"52W Low: {r.get('week52_low', 'N/A')}")
                    for r in filtered
                ]

        # Two markers ride on the ticker symbol itself, so both are readable
        # while scanning without their own columns being shown or scrolled
        # into view (each also has a plain column of its own):
        #   - Flag color, carrying a tooltip with its reason.
        #   - Interested, a ★ -- deliberately not a colored ⭐, since the cell
        #     already spends color on the flag dot and a second colored glyph
        #     would read as another status.
        # Both sit TOGETHER in a leading marker group, inside one nowrap span.
        # The ticker column is narrow enough to wrap, and a star trailing the
        # symbol got pushed onto a third line of the cell -- and only on some
        # rows, depending on how long the symbol was, so the column looked
        # ragged. Keeping the two markers glued to each other means the cell
        # wraps at most once, in the same place, on every row.
        STAR_HTML = '<span title="Interested" style="font-size:15px;">★</span>'

        def _ticker_cell(r):
            link = (f'<a href="{tradingview_url(r["ticker"])}" '
                    f'target="_blank" rel="noopener noreferrer">{r["ticker"]}</a>')
            flag = r.get("flag", "")
            reason = html.escape(r.get("flag_reason", ""))
            emoji = FLAG_EMOJI.get(flag)
            markers = []
            if emoji and reason:
                markers.append(f'<span title="{reason}">{emoji}</span>')
            elif emoji:
                markers.append(emoji)
            if r.get("interested"):
                markers.append(STAR_HTML)
            if not markers:
                return link
            return f'<span style="white-space:nowrap;">{" ".join(markers)}</span> {link}'
        raw_df["ticker_link"] = [_ticker_cell(r) for r in filtered]
        raw_df["company_name"] = [r.get("company_name", "—") for r in filtered]

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
        # Rule color -> number color, so the cell/legend builders below can
        # go straight from a bare int to a color without re-walking rules.
        number_color = {rule_number[r["id"]]: r.get("color") for r in numbered_rules}
        alert_matches = {}
        # alert_hits keeps the full preview item (rule name, conditions, and the
        # row the rule was evaluated against) so the AI-review payload can render
        # each firing rule with its live values, without re-running the engine.
        alert_hits = {}
        if alert_rules_all:
            for p in preview_rules(alert_rules_all, results)[0]:
                if p["is_true_now"]:
                    num = rule_number.get(p["rule_id"])
                    if num is not None:
                        alert_matches.setdefault(p["ticker"], []).append(num)
                        alert_hits.setdefault(p["ticker"], []).append(p)

        def _color_sort_key(n):
            # Green first, unset in the middle, red last -- within a bucket,
            # ascending by number (today's order).
            c = number_color.get(n)
            bucket = 0 if c == "green" else (2 if c == "red" else 1)
            return (bucket, n)

        def _colored_alert_cell(ticker):
            nums = alert_matches.get(ticker)
            if not nums:
                return "—"
            parts = []
            for n in sorted(nums, key=_color_sort_key):
                hex_color = RULE_COLOR_HEX.get(number_color.get(n))
                parts.append(f'<span style="color:{hex_color}">{n}</span>' if hex_color else str(n))
            return ", ".join(parts)

        raw_df["matched_alerts"] = [_colored_alert_cell(r["ticker"]) for r in filtered]

        expert_views = load_expert_views()
        _row_by_ticker = {r["ticker"]: r for r in filtered}

        def _expert_take_cell(ticker):
            v = expert_views.get(ticker, {})
            # Guarded verdict, matching the filterable/sortable expert_take
            # field attached in the enrichment loop -- the badge must not
            # disagree with what you can filter on.
            verdict, vflag = validate_verdict(v, _row_by_ticker.get(ticker))
            headline = v.get("headline", "")
            actionable = v.get("actionable_take", "")
            as_of = v.get("as_of", "unknown")
            news_source = v.get("news_source", "⚪ Unknown")
            model_used = v.get("model_used", "⚪ Unknown")
            # Pending FIRST. A failed/stale placeholder stores verdict "HOLD"
            # with an "Analysis pending -- ..." headline, so the verdict ladder
            # caught it in its HOLD branch and rendered a broken pipeline as a
            # confident 🟡 Hold -- making this "Failed (Retry)" badge
            # unreachable for exactly the records it was written for.
            if is_pending_view(v):
                badge = "⚠️ Failed (Retry)"
            elif verdict == "ACCUMULATE":
                badge = "🟢 Accumulate"
            elif verdict == "HOLD":
                badge = "🟡 Hold"
            elif verdict == "CAUTION":
                badge = "🔴 Caution"
            else:
                # Never analysed, or aged past EXPERT_STALE_DAYS -- validate_verdict
                # returns the non-verdict sentinel "PENDING" for the latter.
                badge = "⚪ Pending"
            if headline:
                parts = [headline, actionable]
                note = verdict_flag_note(vflag, as_of)
                if note:
                    parts.append(note)
                # The news the verdict actually rests on. This was never shown,
                # so a technicals-only verdict looked identical to a
                # news-informed one -- about 30% of them are the former.
                if _expert_view_has_news(v):
                    parts.append(f"News used:\n{str(v.get('news_used') or '').strip()}")
                else:
                    parts.append("News used: none found -- this verdict is a technicals-only read.")
                parts.append(f"As of: {as_of}  |  Source: {news_source}  |  Model: {model_used}")
                tooltip = "\n\n".join(p for p in parts if p)
            else:
                tooltip = "Click 'Retry Failed' in controls above to analyze."
            return with_tooltip(badge, tooltip)

        raw_df["expert_take"] = [_expert_take_cell(r["ticker"]) for r in filtered]

        fundamentals = load_fundamentals()
        def _fundamentals_cell(ticker):
            v = fundamentals.get(ticker, {})
            sentiment = v.get("sentiment", "Unknown")
            earnings = v.get("earnings_summary", "N/A")
            guidance = v.get("future_guidance", "N/A")
            analyst = v.get("analyst_coverage", "N/A")
            reasoning = v.get("reasoning", "No evaluation available yet.")
            as_of = v.get("as_of", "unknown")
            news_source = v.get("news_source", "⚪ Unknown")
            model_used = v.get("model_used", "⚪ Unknown")

            # Deterministic guard: never let a stale or data-less entry show a
            # confident directional verdict, regardless of what the model wrote.
            sentiment, flag = _validate_sentiment(v)
            note = sentiment_flag_note(flag, as_of)
            if note:
                reasoning = f"{reasoning}\n\n{note}"

            if sentiment == "Positive":
                color = "#00C853"
                label = "🐂 Bullish"
            elif sentiment == "Negative":
                color = "#FF5252"
                label = "🐻 Bearish"
            elif sentiment == "Neutral":
                color = None
                label = "⚖️ Neutral"
            else:
                color = None
                label = "⚪ Unknown (STALE)" if flag == "STALE" else (
                    "⚪ Unknown (NO DATA)" if flag == "NO_DATA" else (
                        "⚪ Unknown (QUARTER UNCONFIRMED)" if flag == "STALE_QUARTER" else "⚪ Unknown"
                    )
                )

            tooltip_esc = html.escape(
                f"Earnings: {earnings}\n\nGuidance: {guidance}\n\n"
                f"Analyst Coverage: {analyst}\n\nReasoning: {reasoning}\n\n"
                f"As of: {as_of}  |  Source: {news_source}  |  Model: {model_used}"
            )
            title_attr = tooltip_esc.replace("\n", "&#10;")
            body_html = tooltip_esc.replace("\n", "<br>")
            label_html = (
                f'<span style="color:{color};font-weight:500">{html.escape(label)}</span>'
                if color else html.escape(label)
            )
            return (
                f'<details style="display:inline-block" title="{title_attr}">'
                f'<summary style="cursor:help">{label_html}</summary>'
                f'<div style="font-size:11px;font-weight:400;line-height:1.5;'
                f'white-space:normal;margin-top:4px;">{body_html}</div>'
                f'</details>'
            )

        raw_df["fundamentals"] = [_fundamentals_cell(r["ticker"]) for r in filtered]

        # Column visibility/order is chosen ONCE via the shared sidebar
        # picker (render_shared_column_picker) and passed in, so US and
        # India always show identical columns in identical order.
        # Guard: only select columns that actually exist in raw_df. New columns
        # won't be present in a snapshot built before this code was deployed;
        # selecting a missing column raises KeyError rather than showing blanks.
        safe_keys = [k for k in visible_keys if k in raw_df.columns and k not in ("ticker_link", "last_close")]
        df = raw_df[["ticker_link", "last_close"] + safe_keys].copy()

        # Deduplicate column names (append space) to prevent pandas Styler
        # crashing in to_html() when non-unique columns are present.
        raw_cols = ["Ticker", "Last"] + [label_by_key[k] for k in safe_keys]
        seen = set()
        dedup_cols = []
        for c in raw_cols:
            while c in seen:
                c = c + " "
            seen.add(c)
            dedup_cols.append(c)
        df.columns = dedup_cols
        if "Company Name" in df.columns:
            df["Company Name"] = df["Company Name"].fillna("—")
        if "Trend" in df.columns:
            df["Trend"] = df["Trend"].fillna("—")
        if "Index" in df.columns:
            df["Index"] = df["Index"].fillna("—")

        # CSV export built straight from `filtered` (plain values, pre-HTML)
        # rather than scraping raw_df's with_tooltip()-wrapped cells -- those
        # mix the visible label and the tooltip explanation in one HTML
        # string, so reversing that reliably would mean re-deriving each
        # column's plain value anyway. Every key here is already a plain
        # field on the row dict (flag/note/expert_take are attached earlier
        # in this function; trend/tech_uptrend/etc. come straight from
        # stock_data.py) except Sentiment and Alerts, which get the exact
        # same one-line lookups _fundamentals_cell/the alert-matching loop
        # above already use, minus the badge/tooltip formatting.
        def _export_value(r, key):
            if key == "company_name":
                return r.get("company_name", "")
            if key == "index_name":
                return r.get("index_name", "")
            if key == "tech_uptrend_label":
                return "Yes" if r.get("tech_uptrend") else "No"
            if key == "interested_label":
                return "Yes" if r.get("interested") else "No"
            if key == "vstop_change":
                return vstop_change_str(r)
            if key == "fundamentals":
                # Guarded value, not the raw model one -- _validate_sentiment
                # downgrades a stale/evidence-free view, and the table shows the
                # downgraded result, so returning v["sentiment"] disagreed with
                # the screen. Bare sentiment with no flag suffix: keeps this
                # column's domain to Positive/Negative/Neutral/Unknown so
                # downstream exact-match filters keep working.
                return _validate_sentiment(fundamentals.get(r["ticker"], {}))[0]
            if key == "matched_alerts":
                return ", ".join(str(n) for n in sorted(alert_matches.get(r["ticker"], [])))
            return r.get(key)

        export_df = pd.DataFrame([
            {
                "Ticker": r["ticker"],
                "Last": r.get("last_close"),
                **{label_by_key[k]: _export_value(r, k) for k in visible_keys if k in label_by_key},
            }
            for r in filtered
        ])
        st.download_button(
            "⬇ Download table (CSV)",
            data=export_df.to_csv(index=False),
            file_name=f"{market}_watchlist_{date.today()}.csv",
            mime="text/csv",
            key=f"download_csv_{market}",
        )

        price_cs = [c for c in price_cols(labels) if c in df.columns]
        ratio_cs = [c for c in ratio_cols() if c in df.columns]
        pct_cols = [c for c in PCT_COLS if c in df.columns]
        vol_cols = [c for c in VOLUME_COLS if c in df.columns]

        # Custom columns pick their own display format (see FORMAT_CHOICES)
        # at creation time -- bucket their labels the same way the built-in
        # columns above are bucketed, so they get consistent formatting
        # rather than pandas' raw float repr.
        custom_price_cs, custom_pct_cs, custom_number_cs = [], [], []
        for col in custom_columns:
            if not col.get("enabled", True):
                continue
            label = col["name"]
            if label not in df.columns:
                continue
            fmt = col.get("format", "number")
            if fmt == "price":
                custom_price_cs.append(label)
            elif fmt == "percent":
                if col["id"] not in ("w52dist", "w52lowdist"):
                    custom_pct_cs.append(label)
            else:
                custom_number_cs.append(label)

        styled = (
            df.style
            .hide(axis="index")
            .apply(lambda row: style_row(row, labels), axis=1)
            .format("{:,.0f}", subset=price_cs + custom_price_cs, na_rep="—")
            .format("{:.1f}", subset=ratio_cs, na_rep="—")
            .format("{:,.0f}", subset=vol_cols, na_rep="—")
        )
        if custom_number_cs:
            styled = styled.format("{:.2f}", subset=custom_number_cs, na_rep="—")
        all_pct_cols = pct_cols + custom_pct_cs
        if all_pct_cols:
            styled = styled.format(lambda v: f"{v:+.1f}%" if pd.notna(v) else "—", subset=all_pct_cols)
        perf_pct_cols = [c for c in PERF_PCT_COLS if c in df.columns]
        if perf_pct_cols:
            styled = styled.format(lambda v: f"{v:+.0f}%" if pd.notna(v) else "—", subset=perf_pct_cols)
        rel_pct_cs = [c for c in REL_PCT_COLS if c in df.columns]
        if rel_pct_cs:
            styled = styled.format(lambda v: f"{v:+.1f}%" if pd.notna(v) else "—", subset=rel_pct_cs)
        dist_pct_cs = [c for c in DIST_PCT_COLS if c in df.columns]
        if dist_pct_cs:
            styled = styled.format(lambda v: f"{v:.1f}%" if pd.notna(v) else "—", subset=dist_pct_cs)
        ratio_pct_cs = [c for c in RATIO_PCT_COLS if c in df.columns]
        if ratio_pct_cs:
            styled = styled.format(lambda v: f"{v:+.1%}" if pd.notna(v) else "—", subset=ratio_pct_cs)
        count_cs = [c for c in COUNT_COLS if c in df.columns]
        if count_cs:
            styled = styled.format("{:,.0f}", subset=count_cs, na_rep="—")
        header_tooltips = {**column_definitions(settings, labels), **custom_column_tooltips(custom_columns)}
        table_html = add_header_tooltips(sticky_header_html(styled), header_tooltips)
        st.markdown(table_html, unsafe_allow_html=True)

        # Footer legend: spell out what each number in the Alerts column
        # actually means, so you don't have to jump to the Alert Rules tab
        # to remember what e.g. "2" refers to.
        if "Alerts" in df.columns and alert_matches:
            used_numbers = {n for nums in alert_matches.values() for n in nums}
            metric_labels_market = {v: k for k, v in filterable_metrics.items()}
            # All rules, so a condition referencing a disabled alert still
            # resolves to its name instead of a bare id (see describe_filter).
            rule_by_id_market = {r["id"]: r for r in alert_rules_all}
            legend_bits = []
            legend_dot = {"green": "🟢 ", "red": "🔴 "}
            for r in numbered_rules:
                num = rule_number[r["id"]]
                if num in used_numbers:
                    name = r.get("name") or "(unnamed)"
                    dot = legend_dot.get(r.get("color"), "")
                    legend_bits.append(f"{dot}**{num}** = {name} — {describe_chain(r['conditions'], metric_labels_market, rule_by_id_market)}")
            if legend_bits:
                st.caption("Alert legend: " + " · ".join(legend_bits))

        # Pull one or more rows out as markdown to hand to a chat assistant.
        # This lives below the table rather than in the table: the table is a
        # static HTML block (see sticky_header_html), so it can't host a real
        # per-row button. st.code's built-in copy icon does the clipboard work
        # with no JS, and the same string feeds the download button.
        with st.expander("🧠 Copy / download tickers for AI review", expanded=False):
            ai_sel = st.multiselect(
                "Tickers", [r["ticker"] for r in filtered],
                default=[filtered[0]["ticker"]],
                key=f"ai_review_tickers_{market}",
            )
            # Guard on the resolved rows, not the raw selection: Streamlit
            # already drops selections that fall out of `options`, but this way
            # the builder is never handed an empty list whatever the widget does.
            ai_rows = [r for r in filtered if r["ticker"] in ai_sel]
            if not ai_rows:
                st.caption("Pick at least one ticker.")
            else:
                ai_payload = build_ai_review_payload(
                    ai_rows,
                    market=market,
                    settings=settings,
                    labels=labels,
                    visible_keys=safe_keys,
                    label_by_key=label_by_key,
                    definitions=header_tooltips,
                    export_value=_export_value,
                    expert_views=expert_views,
                    fundamentals=fundamentals,
                    alert_hits=alert_hits,
                    metric_labels={v: k for k, v in filterable_metrics.items()},
                    # All rules, not just numbered_rules: a condition can
                    # reference another alert by id, and describe_filter falls
                    # back to printing the raw id when that rule is missing --
                    # which is exactly what happens to a disabled one.
                    rule_by_id={r["id"]: r for r in alert_rules_all},
                )
                st.caption(f"{len(ai_rows)} ticker(s) · ~{max(1, len(ai_payload) // 1024)} KB")
                st.code(ai_payload, language="markdown", wrap_lines=True, height=400)
                # From ai_rows, not ai_sel, so the filename can't disagree with
                # the caption and payload when a selection stops resolving.
                stem = (ai_rows[0]["ticker"].replace(".", "_") if len(ai_rows) == 1
                        else f"{market}_{len(ai_rows)}_tickers")
                st.download_button(
                    "⬇ Download .md",
                    data=ai_payload,
                    file_name=f"{stem}_{date.today()}.md",
                    mime="text/markdown",
                    key=f"ai_review_dl_{market}",
                )
    else:
        st.info("No tickers match the current filters.")

    # `results` (not just the filtered rows) so the alert rules behind the
    # per-ticker re-analyze button resolve over the whole market, exactly as
    # they do in the nightly job.
    render_expert_view_expander(market, filtered, settings, results=results)

    st.caption(
        f"Mansfield RS = ((price/{bench} ratio today ÷ SMA of that ratio, n) − 1) × 100. "
        "Positive = outperforming the benchmark's trend, negative = underperforming. "
        "WEMA = weekly EMA, DSMA = daily EMA. "
        f"VStop-W = weekly Volatility Stop (ATR stop-and-reverse system, "
        f"length={settings['vstop_length']}, factor={settings['vstop_factor']}, "
        f"{'TradingView-exact engine, Source=close' if settings.get('vstop_mode', 'tv') == 'tv' else 'legacy app engine'}"
        f"{', incl. in-progress week (matches TradingView)' if settings.get('vstop_include_incomplete_week', True) else ', completed weeks only'}) — not independently "
        "cross-checked against your chart the way RS/RSI were, so compare a few readings before relying "
        "on it. Trend = a 4-level read (Strong Uptrend / Uptrend / Downtrend / Strong Downtrend). Uptrend "
        "requires ALL of: price above the slow WEMA, that WEMA's "
        f"{settings.get('trend_slope_lookback', 3)}-week slope rising, fast WEMA above slow WEMA (e.g. 10 "
        "WEMA > 40 WEMA), and weekly RS positive (when available) — no partial credit, anything short of "
        "unanimous is Downtrend. 'Strong' additionally requires price within "
        f"{settings.get('trend_near_high_low_pct', 0.10) * 100:.0f}% of its 52-week high/low AND 10D avg "
        f"volume ≥ {settings.get('trend_volume_ratio', 1.0)}× the 100D avg — its own parameters, "
        "editable in Settings, independent of Vol Trend/Tech Uptrend below — a sort/filter aid, not a "
        "precise signal. "
        "52W High/Low = trailing 12-month intraday extremes. Vol 10D/100D = average daily share volume "
        "over the last 10 / 100 trading days. % Chg = 1-day close-to-close change. Vol Trend classifies "
        f"10D-vs-100D average volume as Exploding (≥{settings.get('volume_explode_ratio', 1.4)}×), "
        f"Declining (≤{settings.get('volume_decline_ratio', 0.7)}×), or In-line — thresholds editable in "
        "Settings. Tech Uptrend = close above the weekly VStop (held for more than "
        f"{settings.get('tech_uptrend_min_vstop_weeks', 3)} weeks) AND close above the slow WEMA AND 10D "
        f"volume ≥ {settings.get('tech_uptrend_volume_ratio', 1.4)}× the 100D avg — its own volume ratio, "
        "independent of Vol Trend's Exploding ratio even though they default to the same value. All "
        "values shown to 1 decimal. Use 'Columns to show' above the table to hide/show columns. Edit any "
        "of these parameters via Settings in the sidebar."
    )


# ============================================================
# APP LAYOUT
# ============================================================

if "refresh_token" not in st.session_state:
    st.session_state.refresh_token = 0
    st.session_state.refresh_nonce = uuid.uuid4().hex
watchlists_now = load_watchlists()
settings_now = load_settings()

st.sidebar.title("Stock Watchlist")
sb1, sb2 = st.sidebar.columns(2)
if sb1.button("Refresh Data", type="primary", width="stretch"):
    with st.spinner("Fetching latest prices & updating snapshot..."):
        combined, as_of, per_market = fetch_all_markets(watchlists_now, settings=settings_now)
        # Keep last-known rows for anything Yahoo would not return, rather than
        # persisting the gap as though those tickers no longer exist. A single
        # throttled click once cut India from 30 rows to 4 and pushed it.
        _prev_rows = (load_data_snapshot() or {}).get("per_market") or {}
        per_market, _recovered = fill_snapshot_gaps(per_market, _prev_rows, watchlists_now)
        combined = [r for mkt_rows in per_market.values() for r in mkt_rows]
        if _recovered:
            _n = sum(len(v) for v in _recovered.values())
            st.sidebar.warning(
                f"Yahoo returned no data for {_n} ticker(s) this refresh — kept their "
                "last-known values rather than dropping them. Check the 'Data Thru' "
                "column, and refresh again later for current prices."
            )
        _persist_and_serve(per_market, as_of, settings_now)
        token, repo, branch = get_github_config(st.secrets)
        if token and repo:
            ok, msg = push_all_config(token, repo, branch, filenames=["data_snapshot.json", "ticker_index.json"], message=f"Refresh data snapshot via UI ({as_of})")
            if ok:
                st.toast(f"✓ Refreshed {len(combined)} tickers & updated GitHub snapshot!")
            else:
                st.sidebar.warning(
                    f"Refreshed {len(combined)} tickers locally, but the GitHub snapshot push failed: {msg}. "
                    "Click **Push to GitHub** in the Alert Rules tab to retry, so the next redeploy can't revert to the old snapshot."
                )
        else:
            st.toast(f"✓ Refreshed live prices for {len(combined)} tickers!")
        _bump_refresh()
        st.rerun()

if "last_refresh_summary" in st.session_state:
    s = st.session_state["last_refresh_summary"]
    counts_str = " · ".join(f"{mkt}: {n}" for mkt, n in s["per_market_counts"].items())
    st.sidebar.caption(f"Last refresh: {s['as_of']}  \n{counts_str} ({s['total']} total)")

if sb2.button("⚙️ Settings", width="stretch"):
    settings_dialog()
render_logout_button()

# Resolve this run's per-market data WITHOUT ever hitting yfinance on a plain
# login/reload. A live fetch is allowed ONLY after an explicit trigger
# (the Refresh Data button, or saving a watchlist) -- both of
# which set refresh_token > 0. Saving calc Settings is NOT one of them despite
# what this comment used to claim: the Settings dialog only calls save_settings,
# which leaves refresh_token alone. What a settings change actually does is fail
# snapshot_is_usable's settings comparison, so the app serves the last-known rows
# with the "out of date" warning until you click Refresh Data yourself.
# At refresh_token == 0 we always serve the
# on-disk snapshot (data_snapshot.json, built by the GitHub Actions data
# refresh); if it's missing or stale we still serve its rows and ask the user
# to click Refresh Data instead of silently re-fetching on every page load.
#
# A freshly-persisted result (Refresh button / watchlist save) is stashed in
# session state so the rerun that follows those actions serves that exact
# result -- no duplicate fetch on top of the one the action already did.
using_snapshot = False
snapshot_warning = None
served = st.session_state.get("_served_snapshot")
if served:
    as_of, per_market = served
    using_snapshot = True

if st.session_state.refresh_token == 0 and not using_snapshot:
    snapshot = load_data_snapshot()
    if snapshot and snapshot_is_usable(snapshot, watchlists_now, settings_now):
        as_of = snapshot["as_of"]
        # Filter the snapshot to only include the tickers currently in the watchlist
        filtered_per_market = {}
        for mkt in watchlists_now.keys():
            wl = set(watchlists_now.get(mkt, []))
            filtered_per_market[mkt] = [r for r in snapshot["per_market"].get(mkt, []) if r.get("ticker") in wl]
        per_market = filtered_per_market
        using_snapshot = True
    elif snapshot and snapshot.get("per_market"):
        # Snapshot exists but is stale (settings/code changed or a ticker was
        # added via GitHub since it was built). Still show the last-known data
        # rather than fetching at login; prompt the user to refresh.
        as_of = snapshot["as_of"]
        filtered_per_market = {}
        for mkt in watchlists_now.keys():
            wl = set(watchlists_now.get(mkt, []))
            filtered_per_market[mkt] = [r for r in snapshot["per_market"].get(mkt, []) if r.get("ticker") in wl]
        per_market = filtered_per_market
        using_snapshot = True
        snapshot_warning = (
            "Data snapshot is out of date (settings, code, or watchlist changed since it was built). "
            "Showing last-known data — click **Refresh Data** to recompute."
        )
    else:
        per_market = {}
        using_snapshot = True
        snapshot_warning = "No data snapshot found. Click **Refresh Data** to load the latest prices."

if not using_snapshot:
    # Only reachable when refresh_token > 0 -- i.e. after an explicit trigger.
    # Filter out pipeline/UI settings so changing a news model, sentiment
    # model, or note dropdown labels doesn't bust the cache and trigger a
    # live fetch when refresh_token > 0
    _NON_CALC = ("news_", "expert_", "note_", "sentiment_")
    calc_settings = {k: v for k, v in settings_now.items() if not k.startswith(_NON_CALC)}
    
    as_of, per_market = cached_fetch_all(
        st.session_state.refresh_nonce,
        json.dumps(watchlists_now, sort_keys=True),
        json.dumps(calc_settings, sort_keys=True),
    )

if snapshot_warning:
    st.warning(snapshot_warning)

# fetch_all_markets() already applies custom columns on the live-fetch path,
# but the snapshot path loads raw JSON straight off disk and never touches
# them -- if a custom column was added/edited any time after that morning's
# snapshot was generated (data-refresh.yml only runs once/day), its key is
# simply missing from snapshot rows. That caused a real KeyError (column
# picker/visible_keys referenced a key that didn't exist in the dataframe).
# Re-applying here, unconditionally, is cheap (plain arithmetic) and makes
# custom columns always reflect the CURRENT formula regardless of when the
# snapshot was built or whether this run used it at all.
custom_columns_now = load_custom_columns()
ticker_notes_now = load_ticker_notes()
from stock_data import load_interested as _load_interested_now
interested_now = _load_interested_now()
fundamentals_now_global = load_fundamentals()
expert_views_now_global = load_expert_views()
for _market_rows in per_market.values():
    apply_custom_columns_to_rows(_market_rows, custom_columns_now)
    # Notes/flags change far more often than custom columns (edited
    # ad hoc throughout the day, not just occasionally) -- re-apply live on
    # every run, same reasoning as custom columns above, so a note/flag you
    # just saved shows up immediately even when serving from this morning's
    # snapshot instead of waiting for the next refresh.
    apply_notes_to_rows(_market_rows, ticker_notes_now, min_vstop_weeks=settings_now.get("tech_uptrend_min_vstop_weeks", 3))
    # Interested status lives in interested.json (ticked in the watchlist
    # editor), not the technical snapshot -- attach it here, same reasoning as
    # notes/flags above, so it's sortable and exportable like any other column.
    #
    # Sentiment likewise comes from fundamentals.json rather than the snapshot.
    # It used to be resolved only at table-render time, i.e. AFTER filtering
    # and sorting had already run, which is why it could be displayed but
    # never filtered, alerted or sorted on. Resolving it here -- once per row,
    # before any tab renders -- is what makes it a first-class metric, and it
    # covers the combined tabs too since they reuse these same row dicts.
    # expert_take moved up here from render_market_tab's filter loop for two
    # reasons: it has to exist BEFORE the sidebar renders for the sort control
    # to type it as text rather than numeric, and the old site called the
    # uncached load_expert_views() once PER ROW -- re-reading and re-parsing
    # the whole file ~760 times a render across every row of every tab.
    for _row in _market_rows:
        _row["interested"] = _row["ticker"] in interested_now
        _row["sentiment"] = _validate_sentiment(fundamentals_now_global.get(_row["ticker"], {}))[0]
        _view = expert_views_now_global.get(_row["ticker"], {})
        # Guarded verdict, not the raw model one -- validate_verdict demotes an
        # ACCUMULATE whose trend/VStop/RS preconditions aren't actually met and
        # ages out a view the pipeline has stopped refreshing, the same way
        # _validate_sentiment guards the Sentiment column.
        #
        # is_pending_view comes first for the same reason it does in the badge:
        # a failed-generation placeholder stores verdict "HOLD", so keying off
        # the verdict alone made a broken analysis filterable and sortable as a
        # genuine Hold.
        if is_pending_view(_view):
            _row["expert_take"] = "Pending"
        else:
            _verdict, _vflag = validate_verdict(_view, _row)
            _row["expert_take"] = _verdict.title() if _verdict in ("ACCUMULATE", "HOLD", "CAUTION") else "Pending"
        # Whether the verdict had any news behind it. 30% of stored verdicts
        # rest on "No recent news found." -- which is a legitimate
        # technicals-only read (VERDICT_RULES says absent news leans HOLD, and
        # the data bears that out), but nothing in the UI distinguished the two.
        # Attached HERE, in the enrichment loop, rather than in the render path,
        # so it is filterable and sortable -- see AGENTS.md's row-dict contract.
        _row["expert_news_backed"] = "Yes" if _expert_view_has_news(_view) else "No"

source_label = "daily snapshot" if using_snapshot else "live fetch"
st.sidebar.caption(f"Data as of: {as_of} ({source_label})")
markets_registry_now = load_markets_registry()
st.sidebar.caption(
    " · ".join(
        f"{markets_registry_now.get(mkt, {}).get('label', mkt)}: {len(per_market.get(mkt, []))}"
        for mkt in markets_registry_now.keys()
    )
)

market_keys_now = list(markets_registry_now.keys())
market_tab_labels = [markets_registry_now[mkt]["label"] for mkt in market_keys_now]

# Two combined views, sequenced AFTER the real watchlists: "All Invested"
# and "All Watchlist" -- roll-ups read as a summary of the tabs before them.
# Synthetic keys ("all_invested"/"all_watchlist") namespace their own widget
# state -- see render_market_tab's combined_markets docstring -- and are
# reserved names (not real market keys), so they can never collide with a
# registry key. The KEY and LABEL of each group are fixed here (no UI to
# create a 3rd/4th group, by design) -- only MEMBERSHIP is user-editable,
# via the "Configure this view" expander rendered on each combined tab,
# which reads/writes watchlist_groups.json through load_watchlist_groups().
COMBINED_TAB_DEFS = [
    ("all_invested", "All Invested"),
    ("all_watchlist", "All Watchlist"),
]
combined_keys = [k for k, _ in COMBINED_TAB_DEFS]
combined_tab_labels = [lbl for _, lbl in COMBINED_TAB_DEFS]
combined_display_label_by_key = {k: lbl for k, lbl in COMBINED_TAB_DEFS}
watchlist_groups_now = load_watchlist_groups()
combined_markets_by_key = {k: watchlist_groups_now.get(k, []) for k in combined_keys}

# key= + on_change="rerun" is what makes the tab strip TRACK its selection.
# Without them st.tabs re-selects its first tab on every script run, so any
# st.rerun() -- and the Alert Rules tab fires one on nearly every interaction
# -- dumped you back on the first watchlist. The rule expander underneath was
# still open (it keeps its own state); you just weren't on that tab any more,
# which reads exactly like the alert collapsing.
#
# This block is instantiated as early as possible in the script -- BEFORE
# render_shared_column_picker() and the dashboard shortcut controls below,
# both of which have widgets (e.g. "Show fundamental columns") that call
# st.rerun() immediately on change. A keyed widget's session_state entry is
# only kept alive across a rerun if that widget is actually re-instantiated
# during the run that precedes it; an st.rerun() fired from code positioned
# BEFORE this point used to cut the script short before st.tabs() was ever
# reached on that pass, silently orphaning "main_tabs" and dumping the user
# back on the first tab (confirmed empirically -- session_state["main_tabs"]
# read back as None immediately after such a rerun). Instantiating the tab
# strip first closes that gap for every widget below it, not just one.
#
# Tab bodies still all execute: with on_change="rerun" they run unless each is
# individually guarded on `.open`, and skipping hidden tabs would change what
# the visible one shows (the market tabs populate alert_matches). Persistence
# only, deliberately -- laziness is a separate job.
# Real watchlists first (in markets.json order -- the registry's insertion
# order IS the display order), then the combined roll-ups, then the two fixed
# tabs. Reorder watchlists by reordering markets.json, not by hardcoding a
# list here: a watchlist added later appends to the registry and picks up its
# tab automatically.
all_tabs = st.tabs(market_tab_labels + combined_tab_labels + ["News", "Alert Rules"],
                   key="main_tabs", on_change="rerun")
n_markets = len(market_keys_now)
market_tabs = dict(zip(market_keys_now, all_tabs[:n_markets]))
combined_tabs = dict(zip(combined_keys, all_tabs[n_markets:-2]))
tab_news, tab_alerts = all_tabs[-2], all_tabs[-1]

# Which tab is on screen, so the sidebar sort control can edit THAT tab's
# order. st.tabs(key=...) stores the selected tab's LABEL, defaulting to the
# first tab's label on the very first render. Market tabs are inserted before
# the two fixed tabs, so a watchlist labelled "News" or "Alert Rules" wins the
# name and keeps its sort control (those two have no table to sort anyway).
_tab_label_to_market = {"News": None, "Alert Rules": None}
_tab_label_to_market.update(dict(zip(combined_tab_labels, combined_keys)))
_tab_label_to_market.update(dict(zip(market_tab_labels, market_keys_now)))
_active_label = st.session_state.get("main_tabs") or (market_tab_labels + combined_tab_labels)[0]
_active_market = _tab_label_to_market.get(_active_label)

# Field types don't vary by market, so a slice of whatever rows exist is
# enough to tell the sort control whether a column is numeric, text or a date.
_sample_rows = [r for rows in per_market.values() for r in rows[:5]]
# Every OTHER sortable tab, as (label, market_key) -- the source list for
# "Copy sort from". News/Alert Rules map to None and are excluded.
_other_tabs = [(lbl, mkt) for lbl, mkt in _tab_label_to_market.items()
               if mkt and mkt != _active_market]
shared_visible_keys, shared_label_by_key, shared_key_by_label = render_shared_column_picker(
    ema_col_labels(settings_now),
    active_market=_active_market,
    active_market_label=_active_label if _active_market else None,
    sample_rows=_sample_rows,
    other_tabs=_other_tabs,
)

# Prominent, upfront dashboard controls -- shortcuts to settings that
# otherwise require opening a sidebar expander or the Settings dialog.
# These read/write the exact same settings.json / markets.json as those
# other locations, so all access points always stay in sync.
dash1, dash2 = st.columns([1, 3])
with dash1:
    dash_show_fundamentals = st.checkbox(
        "Show fundamental columns",
        value=settings_now.get("show_fundamental_columns", True),
        key="dash_show_fundamental_columns_toggle",
        help="Sentiment, Qtr Profit/Revenue Growth %. Same setting as the toggle in the column picker.",
    )
    if dash_show_fundamentals != settings_now.get("show_fundamental_columns", True):
        settings_now["show_fundamental_columns"] = dash_show_fundamentals
        save_settings(settings_now)
        st.rerun()
with dash2:
    with st.popover("➕ Add Watchlist"):
        dash_wl_label = st.text_input("Label", key="dash_new_watchlist_label", placeholder="e.g. UK Watchlist")
        dash_wl_bench = st.text_input("Benchmark ticker", key="dash_new_watchlist_bench", placeholder="e.g. ^FTSE")
        if st.button("Add", key="dash_add_watchlist_btn"):
            if not dash_wl_label.strip() or not dash_wl_bench.strip():
                st.error("Both a label and a benchmark ticker are required.")
            else:
                from stock_data import add_watchlist as _dash_add_watchlist
                _dash_add_watchlist(dash_wl_label.strip(), dash_wl_bench.strip())
                gh_token, gh_repo, gh_branch = get_github_config(getattr(st, "secrets", None))
                if gh_token and gh_repo:
                    with st.spinner("Pushing new watchlist to GitHub..."):
                        ok, msg = push_all_config(gh_token, gh_repo, gh_branch, filenames=["markets.json", "watchlist.json", "custom_filters.json"], message=f"Add watchlist {dash_wl_label.strip()}")
                        if not ok:
                            st.error(f"Failed to push to GitHub: {msg}")
                st.success(f"Added \"{dash_wl_label.strip()}\".")
                time.sleep(1)
                st.rerun()

for ck in combined_keys:
    with combined_tabs[ck]:
        combined_markets_here = combined_markets_by_key[ck]
        # De-duplicated by ticker, first member wins. A ticker can sit in more
        # than one member watchlist (AMKR is in both US Watchlist and
        # Substack-OutperformingMarket), and a raw concatenation rendered it
        # twice -- which crashed the tab outright, because the per-ticker
        # widgets downstream are keyed on {market}_{ticker} and the second copy
        # collided (StreamlitDuplicateElementKey on re_ev_all_watchlist_AMKR).
        # Which copy survives doesn't change any displayed number: the rows are
        # identical apart from their `market` tag, since every metric including
        # Mansfield RS is computed against the ticker's own index (see
        # ticker_index.json), not the member watchlist's benchmark.
        seen_tickers = set()
        combined_results = []
        for m in combined_markets_here:
            for r in per_market.get(m, []):
                if r["ticker"] in seen_tickers:
                    continue
                seen_tickers.add(r["ticker"])
                combined_results.append(r)
        render_market_tab(
            ck, combined_results, settings_now, shared_visible_keys, shared_label_by_key,
            saved_sort_levels(ck, shared_key_by_label), combined_markets=combined_markets_here,
            combined_label=combined_display_label_by_key[ck],
        )

for mkt in market_keys_now:
    with market_tabs[mkt]:
        render_market_tab(
            mkt, per_market.get(mkt, []), settings_now, shared_visible_keys, shared_label_by_key,
            saved_sort_levels(mkt, shared_key_by_label),
        )

with tab_news:
    st.subheader("Market Breadth & Performance")
    col_filter, col_refresh = st.columns([3, 1])
    with col_filter:
        time_filter = st.radio("Time Horizon", ["3 Years", "5 Years"], horizontal=True, key="time_horizon_filter")
        years = 3 if time_filter == "3 Years" else 5
        
    with col_refresh:
        if st.button("🔄 Refresh Charts", width="stretch"):
            st.cache_data.clear()
            st.rerun()

    # Helpers
    import plotly.graph_objects as go
    import pandas as pd

    def plot_performance_plotly(df, portfolio_name, benchmark_name):
        if df is None or df.empty:
            return
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df.index, y=df['Portfolio'], name=portfolio_name, line=dict(color='#3498db', width=2)))
        fig.add_trace(go.Scatter(x=df.index, y=df['Benchmark'], name=benchmark_name, line=dict(color='#95a5a6', width=1.5, dash='dot')))
        fig.update_layout(
            height=250, margin=dict(l=0, r=0, t=10, b=0),
            hovermode="x unified",
            xaxis=dict(showspikes=True, spikemode="across", spikesnap="cursor", showline=True, showgrid=False),
            yaxis=dict(showgrid=True, title="Return (Base 100)"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig, width="stretch")

    def plot_breadth_plotly(df, title, y_label, show_50=False):
        if df is None or df.empty:
            return
        fig = go.Figure()
        colors = ['#2ecc71', '#e74c3c', '#f39c12']
        for i, col in enumerate([c for c in df.columns if c != 'Date']):
            fig.add_trace(go.Scatter(x=df['Date'], y=df[col], name=col, line=dict(color=colors[i % len(colors)], width=1.5)))
            
        if show_50:
            fig.add_hline(y=50, line_dash="dash", line_color="rgba(0,0,0,0.3)", annotation_text="50%")
            
        fig.update_layout(
            height=250, margin=dict(l=0, r=0, t=10, b=0),
            hovermode="x unified",
            xaxis=dict(showspikes=True, spikemode="across", spikesnap="cursor", showline=True, showgrid=False),
            yaxis=dict(showgrid=True, title=y_label, range=[0, 100] if show_50 else None),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1) if len(df.columns) > 2 else dict(visible=False)
        )
        st.plotly_chart(fig, width="stretch")

    def calculate_portfolio_returns(closes, tickers, benchmark_ticker, weights=None, filter_years=3):
        if closes is None or closes.empty: return None
        if closes.index.tzinfo is not None:
            cutoff = pd.Timestamp.now(tz=closes.index.tz) - pd.DateOffset(years=filter_years)
        else:
            cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(years=filter_years)
            
        closes = closes[closes.index >= cutoff]
        if closes.empty: return None
        
        valid_tickers = [t for t in tickers if t in closes.columns]
        if not valid_tickers: return None
        
        returns = closes[valid_tickers].pct_change()
        
        w_dict = {}
        for t in valid_tickers:
            w_dict[t] = weights.get(t, 1.0) if weights else 1.0
        w_series = pd.Series(w_dict)
        
        valid_weights = returns.notna() * w_series
        weighted_returns = (returns * valid_weights).sum(axis=1) / valid_weights.sum(axis=1)
        
        portfolio = 100 * (1 + weighted_returns).cumprod()
        portfolio.iloc[0] = 100
            
        bench = None
        if benchmark_ticker in closes.columns and closes[benchmark_ticker].first_valid_index() is not None:
            first_val = closes[benchmark_ticker].bfill().iloc[0]
            if first_val > 0:
                bench = (closes[benchmark_ticker] / first_val) * 100
                
        if portfolio is not None and bench is not None:
            return pd.DataFrame({"Portfolio": portfolio, "Benchmark": bench})
        return None

    def format_json_breadth(market_data, filter_years):
        if not market_data: return None, None
        cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(years=filter_years)
        
        # SMA
        hist = market_data.get("history", {})
        df_ema = pd.DataFrame(list(hist.items()), columns=["Date", "% Above 200d SMA"]) if hist else pd.DataFrame()
        if not df_ema.empty:
            df_ema["Date"] = pd.to_datetime(df_ema["Date"])
            df_ema = df_ema[df_ema["Date"] >= cutoff]
            
        # HL
        h_hist = market_data.get("highs_history", {})
        l_hist = market_data.get("lows_history", {})
        df_hl = pd.DataFrame()
        if h_hist and l_hist:
            df_hl = pd.DataFrame({
                "Date": pd.to_datetime(list(h_hist.keys())),
                "% New Highs": list(h_hist.values()),
                "% New Lows": list(l_hist.values())
            })
            df_hl = df_hl[df_hl["Date"] >= cutoff]
            
        return df_ema, df_hl

    # Load Data
    breadth_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_breadth.json")
    breadth_data = {}
    if os.path.exists(breadth_file):
        with open(breadth_file) as f:
            breadth_data = json.load(f)

    # Dashboard portfolio-vs-benchmark curves come from dashboard_perf.json,
    # built by the same GitHub Actions market-breadth workflow -- NO live
    # yfinance at render. Each market stores {ticker: {date: close}}, and the
    # benchmark ticker is included as its own column so calculate_portfolio_returns
    # works exactly as when the data was fetched live.
    perf_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_perf.json")
    perf_data = {}
    perf_as_of = None
    if os.path.exists(perf_file):
        try:
            with open(perf_file) as f:
                perf_data = json.load(f)
            perf_as_of = perf_data.get("as_of")
        except Exception:
            perf_data = {}

    def _closes_from_perf(market):
        series_map = perf_data.get("markets", {}).get(market, {})
        if not series_map:
            return None
        df = pd.DataFrame({t: dict(s) for t, s in series_map.items()})
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
            
    # Each curve is the whole watchlist, equal-weighted, against its own
    # benchmark: India Invested vs Nifty 500 (^CRSLDX), US Invested vs SPY.
    # The India side used to be filtered down to whichever of its tickers were
    # flagged in invested.json and weighted by that file; both are gone with
    # the flag (every stored weight was 1.0, i.e. equal-weight already, and the
    # filter matched all 29 tickers -- so the curves are unchanged).
    #
    # These are the "US Invested"/"India Invested" WATCHLISTS keyed by their
    # markets.json registry keys -- not to be confused with market_breadth.json's
    # "US"/"INDIA" keys below, which represent national S&P 500/Nifty 500
    # breadth and are unrelated to the watchlist registry (see that lookup's
    # own comment).
    us_tickers = watchlists_now.get("us_invested", [])
    ind_tickers = watchlists_now.get("india_invested", [])

    st.divider()

    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    # We will build a single 3x2 subplot figure so hover spikes sync perfectly across all rows.
    with st.spinner("Loading Dashboards..."):
        closes_ind = None
        df_perf_ind = None
        if ind_tickers:
            closes_ind = _closes_from_perf("india_invested")
            if closes_ind is not None and "^CRSLDX" in closes_ind.columns:
                df_perf_ind = calculate_portfolio_returns(closes_ind, ind_tickers, "^CRSLDX", weights=None, filter_years=years)
            
        closes_us = None
        df_perf_us = None
        if us_tickers:
            closes_us = _closes_from_perf("us_invested")
            if closes_us is not None and "SPY" in closes_us.columns:
                df_perf_us = calculate_portfolio_returns(closes_us, us_tickers, "SPY", weights=None, filter_years=years)
            
        if perf_as_of:
            st.caption(f"Portfolio & breadth data as of {perf_as_of} — refreshed by the scheduled GitHub Action.")
            
        # market_breadth.json's "INDIA"/"US" keys are fixed national-index
        # breadth (Nifty 500 / S&P 500), independent of the watchlist
        # registry -- NOT renamed alongside markets.json/watchlist.json, so
        # these stay as the literal keys refresh_market_breadth.py writes.
        df_ema_ind, df_hl_ind = format_json_breadth(breadth_data.get("markets", {}).get("INDIA"), years)
        df_ema_us, df_hl_us = format_json_breadth(breadth_data.get("markets", {}).get("US"), years)

        def get_val(df, col):
            if df is not None and not df.empty and col in df.columns:
                return f"{df[col].iloc[-1]:.1f}"
            return "--"
            
        # `or {}` is load-bearing: refresh_market_breadth.py's calculate_breadth
        # returns None when every download batch fails, and a stored None made
        # .get("US", {}) return None -- .get("total") on it raised AttributeError
        # and took down this whole tab, not just one chart. The refresh script no
        # longer writes None, but an older snapshot may still carry one.
        def _breadth_block(market_key):
            return (breadth_data.get("markets") or {}).get(market_key) or {}

        # A market whose own as_of differs from the file's is preserved data from
        # an earlier run -- its leg failed and main() kept the previous block
        # rather than wiping it. Label it instead of passing it off as current.
        _breadth_as_of = breadth_data.get("as_of")

        def _stale_suffix(market_key):
            block_as_of = _breadth_block(market_key).get("as_of")
            if block_as_of and _breadth_as_of and block_as_of != _breadth_as_of:
                return f" [stale: {block_as_of}]"
            return ""

        total_ind = _breadth_block("INDIA").get("total", "--")
        total_us = _breadth_block("US").get("total", "--")
        stale_ind = _stale_suffix("INDIA")
        stale_us = _stale_suffix("US")
            
        title_ema_ind = f"Nifty 500: % Above 200-Day SMA (Current: {get_val(df_ema_ind, '% Above 200d SMA')}%, Captured: {total_ind}){stale_ind}"
        title_ema_us = f"S&P 500: % Above 200-Day SMA (Current: {get_val(df_ema_us, '% Above 200d SMA')}%, Captured: {total_us}){stale_us}"
        
        title_hl_ind = f"Nifty 500: 52-Week Highs vs Lows (Highs: {get_val(df_hl_ind, '% New Highs')}%, Lows: {get_val(df_hl_ind, '% New Lows')}%){stale_ind}"
        title_hl_us = f"S&P 500: 52-Week Highs vs Lows (Highs: {get_val(df_hl_us, '% New Highs')}%, Lows: {get_val(df_hl_us, '% New Lows')}%){stale_us}"
        
        def get_perf(df):
            if df is not None and not df.empty and 'Portfolio' in df.columns and 'Benchmark' in df.columns:
                ret_port = (df['Portfolio'].iloc[-1] / 100 - 1) * 100
                bench_valid = df['Benchmark'].dropna()
                ret_bench = (bench_valid.iloc[-1] / 100 - 1) * 100 if not bench_valid.empty else float('nan')
                return f"Watchlist: {ret_port:+.1f}%, Bench: {ret_bench:+.1f}%"
            return "--"
            
        _ind_display_label = markets_registry_now.get("india_invested", {}).get("label", "India Invested")
        _us_display_label = markets_registry_now.get("us_invested", {}).get("label", "US Invested")
        _ind_bench_label = markets_registry_now.get("india_invested", {}).get("benchmark", "Nifty 500")
        _us_bench_label = markets_registry_now.get("us_invested", {}).get("benchmark", "S&P 500")
        title_perf_ind = f"{_ind_display_label} vs {_ind_bench_label} ({get_perf(df_perf_ind)})"
        title_perf_us = f"{_us_display_label} vs {_us_bench_label} ({get_perf(df_perf_us)})"

        fig = make_subplots(
            rows=3, cols=2,
            shared_xaxes="all",
            vertical_spacing=0.08,
            horizontal_spacing=0.05,
            subplot_titles=(
                title_ema_ind, title_ema_us,
                title_hl_ind, title_hl_us,
                title_perf_ind, title_perf_us
            )
        )
        
        colors = ['#2ecc71', '#e74c3c', '#f39c12']

        # An empty panel is indistinguishable from a panel whose data is all
        # zero. Say why it is blank -- the S&P breadth charts sat empty for 8
        # days because a refresh leg was failing silently, and nothing on screen
        # pointed at the refresh job.
        def _no_data(row, col):
            fig.add_annotation(
                text="No data - last refresh failed",
                showarrow=False,
                xref="x domain", yref="y domain", x=0.5, y=0.5,
                row=row, col=col,
                font=dict(color="#95a5a6", size=13),
            )

        # Row 1: SMA Breadth
        if df_ema_ind is not None and not df_ema_ind.empty:
            for i, col in enumerate([c for c in df_ema_ind.columns if c != 'Date']):
                fig.add_trace(go.Scatter(x=df_ema_ind['Date'], y=df_ema_ind[col], name=col, line=dict(color=colors[i % len(colors)], width=1.5), showlegend=False), row=1, col=1)
            fig.add_hline(y=50, line_dash="dash", line_color="rgba(0,0,0,0.3)", row=1, col=1)
        else:
            _no_data(1, 1)

        if df_ema_us is not None and not df_ema_us.empty:
            for i, col in enumerate([c for c in df_ema_us.columns if c != 'Date']):
                fig.add_trace(go.Scatter(x=df_ema_us['Date'], y=df_ema_us[col], name=col, line=dict(color=colors[i % len(colors)], width=1.5), showlegend=False), row=1, col=2)
            fig.add_hline(y=50, line_dash="dash", line_color="rgba(0,0,0,0.3)", row=1, col=2)
        else:
            _no_data(1, 2)

        # Row 2: High/Low Extremes
        if df_hl_ind is not None and not df_hl_ind.empty:
            for i, col in enumerate([c for c in df_hl_ind.columns if c != 'Date']):
                fig.add_trace(go.Scatter(x=df_hl_ind['Date'], y=df_hl_ind[col], name=col, line=dict(color=colors[i % len(colors)], width=1.5), showlegend=False), row=2, col=1)
        else:
            _no_data(2, 1)

        if df_hl_us is not None and not df_hl_us.empty:
            for i, col in enumerate([c for c in df_hl_us.columns if c != 'Date']):
                fig.add_trace(go.Scatter(x=df_hl_us['Date'], y=df_hl_us[col], name=col, line=dict(color=colors[i % len(colors)], width=1.5), showlegend=False), row=2, col=2)
        else:
            _no_data(2, 2)

        # Row 3: Portfolio Performance
        _ind_label = markets_registry_now.get("india_invested", {}).get("label", "India Invested")
        _us_label = markets_registry_now.get("us_invested", {}).get("label", "US Invested")
        if df_perf_ind is not None and not df_perf_ind.empty:
            fig.add_trace(go.Scatter(x=df_perf_ind.index, y=df_perf_ind['Portfolio'], name=_ind_label, line=dict(color='#3498db', width=2), showlegend=False), row=3, col=1)
            fig.add_trace(go.Scatter(x=df_perf_ind.index, y=df_perf_ind['Benchmark'], name="Nifty 500", line=dict(color='#95a5a6', width=1.5, dash='dot'), showlegend=False), row=3, col=1)
        else:
            _no_data(3, 1)

        if df_perf_us is not None and not df_perf_us.empty:
            fig.add_trace(go.Scatter(x=df_perf_us.index, y=df_perf_us['Portfolio'], name=_us_label, line=dict(color='#3498db', width=2), showlegend=False), row=3, col=2)
            fig.add_trace(go.Scatter(x=df_perf_us.index, y=df_perf_us['Benchmark'], name="S&P 500", line=dict(color='#95a5a6', width=1.5, dash='dot'), showlegend=False), row=3, col=2)
        else:
            _no_data(3, 2)

        fig.update_layout(
            height=900,
            hovermode="x unified",
            # t=30 left the modebar sitting directly on the right column's
            # subplot title. A vertical modebar parks it against the right edge
            # and the extra headroom keeps it clear of the titles entirely.
            margin=dict(l=0, r=0, t=55, b=0),
            modebar=dict(orientation="v", bgcolor="rgba(0,0,0,0)"),
            showlegend=False
        )
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", showline=True, showgrid=False)
        fig.update_yaxes(showgrid=True)
        
        st.plotly_chart(
            fig,
            width="stretch",
            config={
                "displaylogo": False,
                "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
            },
        )

    st.divider()
    st.subheader("Watchlist news digest")
    st.caption(
        "Major announcements, developments, and stock moves for your watchlist tickers in the "
        "last 24-48 hours, summarized via Gemini (Google Search grounding). Runs once a day at "
        "8:00 PM ET via GitHub Actions and is also sent to Discord — this tab just shows the "
        "same result."
    )
    news_data = load_news_summary()

    # ── News scope selector ─────────────────────────────────────────────────
    # Controls which watchlists are included when the news pipeline runs.
    # The selection is persisted in settings.json so GitHub Actions picks it
    # up on the next scheduled run (after pushing settings to GitHub).
    _all_news_market_opts = {
        markets_registry_now.get(mkt, {}).get("label", mkt): mkt
        for mkt in market_keys_now
    }
    _saved_scope_keys = settings_now.get("news_watchlist_scope", [])
    _saved_scope_labels = [
        lbl for lbl, key in _all_news_market_opts.items() if key in _saved_scope_keys
    ]
    _selected_scope_labels = st.multiselect(
        "📋 News scope — generate for watchlists (empty = all)",
        options=list(_all_news_market_opts.keys()),
        default=_saved_scope_labels,
        key="news_scope_select",
        help=(
            "Choose which watchlists to include in the news digest. "
            "Leave empty to include all. Push settings to GitHub (Alert Rules tab) "
            "so the scheduled GitHub Actions workflow respects this selection."
        ),
    )
    _new_scope_keys = [_all_news_market_opts[lbl] for lbl in _selected_scope_labels]
    if _new_scope_keys != _saved_scope_keys:
        settings_now["news_watchlist_scope"] = _new_scope_keys
        save_settings(settings_now)
        st.rerun()

    col1, col2, col3, col4 = st.columns([2, 3, 3, 3])
    with col1:
        if st.button("🔄 Refresh News", width="stretch"):
            token, repo, _ = get_github_config(st.secrets)
            if token and repo:
                ok, msg = trigger_github_workflow(token, repo, "news-summary.yml")
                if ok:
                    st.success(f"News refresh started in background! [View live logs on GitHub](https://github.com/{repo}/actions/workflows/news-summary.yml) (takes ~15 mins)")
                else:
                    st.error(f"Failed to start refresh: {msg}")
            else:
                st.error("Missing GITHUB_TOKEN or GITHUB_REPO in secrets.")
                
    with col2:
        search_choices = ["models/gemma-4-31b-it", "models/gemma-4-26b-a4b-it"]
        new_search = st.selectbox(
            "Search Model", search_choices,
            index=search_choices.index(settings_now.get("news_search_model", search_choices[0])) if settings_now.get("news_search_model") in search_choices else 0,
            key="news_search_model_select"
        )
        if new_search != settings_now.get("news_search_model"):
            settings_now["news_search_model"] = new_search
            save_settings(settings_now)
            st.rerun()

    with col3:
        reason_choices = ["models/gemini-3.5-flash-lite", "models/gemma-4-31b-it", "models/gemma-4-26b-a4b-it"]
        current_reason = settings_now.get("news_reasoning_model", "models/gemini-3.5-flash-lite")
        if current_reason == "gemini-3.5-flash-lite":
            current_reason = "models/gemini-3.5-flash-lite"
        new_reason = st.selectbox(
            "Reasoning Model", reason_choices,
            index=reason_choices.index(current_reason) if current_reason in reason_choices else 0,
            key="news_reasoning_model_select"
        )
        # Normalize comparison
        saved_reason = settings_now.get("news_reasoning_model", "models/gemini-3.5-flash-lite")
        if saved_reason == "gemini-3.5-flash-lite":
            saved_reason = "models/gemini-3.5-flash-lite"
        if new_reason != saved_reason:
            settings_now["news_reasoning_model"] = new_reason
            save_settings(settings_now)
            st.rerun()
            
    with col4:
        is_gemma = "gemma" in settings_now.get("news_reasoning_model", "")
        if is_gemma:
            budget_choices = ["LOW", "MEDIUM", "HIGH"]
            default_val = "HIGH"
        else:
            budget_choices = [1024, 2048, 4096, 8192]
            default_val = 8192

        current_val = settings_now.get("news_reasoning_budget", default_val)
        
        # Type safety for transitioning between models
        if is_gemma and current_val not in budget_choices:
            current_val = default_val
        elif not is_gemma:
            try:
                current_val = int(current_val)
            except (ValueError, TypeError):
                current_val = default_val
            if current_val not in budget_choices:
                current_val = default_val
                
        new_budget = st.selectbox(
            "Thinking Budget / Level", budget_choices,
            index=budget_choices.index(current_val),
            key="news_reasoning_budget_select"
        )
        if new_budget != current_val:
            settings_now["news_reasoning_budget"] = new_budget
            save_settings(settings_now)
            st.rerun()

    if not news_data:
        st.info(
            "No news summary yet. It's generated once a day by the scheduled GitHub Actions "
            "workflow (`news-summary.yml`) — nothing to do here until the first scheduled run, "
            "or trigger it manually using the button above."
        )
    else:
        st.caption(f"As of {news_data.get('as_of', '—')}")
        for market in market_keys_now:
            entry = news_data.get("markets", {}).get(market)
            if not entry:
                continue
            st.markdown(f"### {markets_registry_now.get(market, {}).get('label', MARKET_LABELS.get(market, market))}")
            st.markdown(entry.get("summary", "_No summary available._"))

            # Per-ticker run health. Without this a quiet news day and a run
            # where every search errored look identical -- both render as one
            # short "no major news" line. Absent on digests generated before
            # the counters existed, hence the `if counts`.
            counts = entry.get("counts") or {}
            if counts:
                bits = [f"{counts.get('material', 0)} with news",
                        f"{counts.get('quiet', 0)} quiet"]
                if counts.get("degraded"):
                    bits.append(f"⚠️ {counts['degraded']} unfiltered (AI filter failed)")
                if counts.get("failed"):
                    bits.append(f"⚠️ {counts['failed']} search failed")
                # Tickers Stage 2 judged material but Stage 3 left out of the
                # digest. Surfaced because "8 with news" over a one-bullet
                # summary is exactly the discrepancy that hid this bug.
                _dropped = entry.get("collation_dropped") or []
                if _dropped:
                    bits.append(f"⚠️ {len(_dropped)} dropped in collation ({', '.join(_dropped[:4])}"
                                + ("…" if len(_dropped) > 4 else "") + ")")
                st.caption(" · ".join(bits))

            sources = entry.get("sources") or []
            if sources:
                with st.expander(f"Sources ({len(sources)})"):
                    for s in sources:
                        title = s.get("title") or s.get("url")
                        st.markdown(f"- [{title}]({s.get('url')})")
            st.divider()

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

    combined_tickers = [t for mkt in market_keys_now for t in watchlists_now.get(mkt, [])]
    combined_results = [r for mkt in market_keys_now for r in per_market.get(mkt, [])]

    # Helper maps used by the per-section playlist pickers below.
    # Computed once here so both the Preview and Wrap-up sections share the same
    # label↔key translation without duplicating the registry call.
    _alert_mkt_label_to_key = {markets_registry_now[mkt]["label"]: mkt for mkt in market_keys_now}
    _alert_mkt_all_labels = list(_alert_mkt_label_to_key.keys())

    filterable_metrics_alert = get_all_filterable_metrics(settings_now)
    metric_names_alert = list(filterable_metrics_alert.keys())
    metric_labels_alert = {v: k for k, v in filterable_metrics_alert.items()}
    metric_defs_alert = metric_definitions(settings_now, ema_col_labels(settings_now))

    rules = load_rules()
    rule_by_id_alert = {r["id"]: r for r in rules}

    # ── Rule builder ────────────────────────────────────────────────────────
    st.markdown("**Add a rule**")

    if "draft_rule_conditions" not in st.session_state:
        st.session_state.draft_rule_conditions = []

    top1, top2, top3 = st.columns([2, 3, 1.3])
    market_scope_label_to_key = {f"{markets_registry_now[mkt]['label']} watchlist": mkt for mkt in market_keys_now}
    market_scope_key_to_label = {v: k for k, v in market_scope_label_to_key.items()}
    scope_options = ["All watchlist"] + list(market_scope_label_to_key.keys()) + combined_tickers
    scope_choice = top1.selectbox("Scope", scope_options, key="rule_scope")
    rule_name = top2.text_input("Name (optional)", key="rule_name", placeholder="e.g. Stage 2 breakout")
    rule_color_ui = top3.selectbox(
        "Color", RULE_COLOR_UI_OPTIONS, key="rule_color",
        help="Colors the rule's number in every watchlist's Alerts column, and orders it "
             "green-first/red-last within a ticker's cell.",
    )

    if st.session_state.draft_rule_conditions:
        st.caption("Conditions in this rule so far:")
        st.session_state.draft_rule_conditions, dr_changed = render_condition_list(
            st.session_state.draft_rule_conditions, "dr", metric_labels_alert,
            filterable_metrics_alert, rule_by_id=rule_by_id_alert,
            available_rules=rules, definitions=metric_defs_alert,
        )
        if dr_changed:
            # Draft lives in session_state only -- nothing to persist until
            # "Save rule".
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

    new_cond = render_condition_builder("dr", metric_names_alert, filterable_metrics_alert, dr_logic,
                                        available_rules=rules, definitions=metric_defs_alert)
    if new_cond:
        st.session_state.draft_rule_conditions.append(new_cond)
        st.rerun()

    st.markdown("**Alert mode & schedule**")
    day_options = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_code_map = dict(zip(day_options, DAY_CODES))
    inv_day_map = {v: k for k, v in day_code_map.items()}
    hours_list = [f"{h:02d}" for h in ALLOWED_HOURS]
    default_day_labels = [inv_day_map[d] for d in DEFAULT_DAYS]

    dr_alert_mode = st.radio(
        "Alert mode", ["Scheduled Discord alert", "Scan only (no alert)"],
        key="rule_mode", horizontal=True,
        help="Scheduled Discord alert sends Discord pings when due. Scan only never sends "
             "Discord pings but stays available to filter watchlists by, in the market tabs above.",
    )
    if dr_alert_mode == "Scheduled Discord alert":
        sc1, sc2 = st.columns([3, 1.2])
        dr_days_labels = sc1.multiselect("Days (ET)", options=day_options, default=default_day_labels, key="rule_sched_days")
        dr_hour = sc2.selectbox(
            "Time (ET)", options=hours_list, index=hours_list.index("21"),
            format_func=lambda h: HOUR_LABELS.get(int(h), h), key="rule_sched_hour",
        )
        st.caption(
            "The alert check runs once a day at 9:00 PM ET (kept to 1x/day to save on GitHub "
            "Actions minutes) — pick which day(s) it should check on."
        )
    else:
        dr_days_labels, dr_hour = [], "21"

    save_col, clear_col = st.columns([1, 1])
    if save_col.button("Save rule", type="primary"):
        if not st.session_state.draft_rule_conditions:
            st.error("Add at least one condition first.")
        else:
            if scope_choice == "All watchlist":
                scope_val = "ALL"
            elif scope_choice in market_scope_label_to_key:
                scope_val = market_scope_label_to_key[scope_choice]
            else:
                scope_val = scope_choice
            if dr_alert_mode == "Scheduled Discord alert":
                sched_days = [day_code_map[d] for d in dr_days_labels] if dr_days_labels else list(DEFAULT_DAYS)
                new_schedule = {"type": "scheduled", "days": sched_days, "time_et": f"{dr_hour}:00"}
            else:
                new_schedule = {"type": "none", "days": list(DEFAULT_DAYS), "time_et": "21:00"}
            new_rule = {
                "id": uuid.uuid4().hex[:8],
                "name": st.session_state.get("rule_name", "").strip(),
                "scope": scope_val,
                "conditions": list(st.session_state.draft_rule_conditions),
                "enabled": True,
                "schedule": new_schedule,
                "color": RULE_COLOR_UI_TO_VALUE[rule_color_ui],
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
        search_col, count_col = st.columns([3, 1])
        rule_search = search_col.text_input(
            "Search rules", key="rule_search", placeholder="name, scope, or a metric it uses",
            help="Matches the rule name, its scope, and the labels of every metric its "
                 "conditions reference — so searching 'adx' finds any rule built on ADX.",
        )

        def _rule_haystack(r):
            """Name + scope + every metric label the rule references, so a
            search can find a rule by what it TESTS and not just what it was
            named -- most of these names don't mention their metrics."""
            bits = [r.get("name") or "", str(r.get("scope") or "")]
            for c in r.get("conditions", []):
                for mk in (c.get("metric_a"), c.get("metric_b")):
                    if mk:
                        bits.append(metric_labels_alert.get(mk, mk))
                if c.get("type") == "rule":
                    ref = rule_by_id_alert.get(c.get("rule_id"), {})
                    bits.append(ref.get("name") or "")
            return " ".join(bits).lower()

        visible_rules = ([r for r in rules if rule_search.strip().lower() in _rule_haystack(r)]
                         if rule_search.strip() else rules)
        count_col.metric("Shown", f"{len(visible_rules)}/{len(rules)}")

        def _arrange_bucket(r):
            if not r.get("enabled", True):
                return 3
            return {"green": 0, "red": 2}.get(r.get("color"), 1)

        if st.button(
            "↕️ Auto-arrange: green → red → disabled", key="auto_arrange_rules",
            help="One-click default order (each group keeps its current relative order). "
                 "Use ▲/▼ on individual rules below to fine-tune from there.",
        ):
            rules = sorted(rules, key=_arrange_bucket)
            save_rules(rules)
            st.rerun()

        if not visible_rules:
            st.info(f"No rule matches “{rule_search}”.")

        for rule in visible_rules:
            scope_label = {"ALL": "All watchlist", **market_scope_key_to_label}.get(rule.get("scope"), rule.get("scope"))
            name_label = rule.get("name") or "(unnamed)"
            n_conds = len(rule.get("conditions", []))
            sched_summary = describe_schedule(rule)
            expander_title = f"{name_label} — {scope_label} ({n_conds} condition{'s' if n_conds != 1 else ''}) | {sched_summary}"
            # Status the title never carried: a disabled rule and a scan-only
            # rule used to look identical when collapsed.
            if not rule.get("enabled", True):
                status_icon = "⏸️"
            elif rule.get("schedule", {}).get("type") == "none":
                status_icon = "🔍"
            else:
                status_icon = "✅"

            exp_key = f"rule_exp_{rule['id']}"

            # Re-open a rule that asked to stay open before it reran.
            #
            # Streamlit keeps the expander's open/closed state under `key`,
            # which survives an ordinary st.rerun() -- but NOT a rerun in
            # which `icon` changes value. Changing icon re-creates the
            # element and its tracked state goes with it. Toggling Enabled
            # flips the icon (⏸️ <-> ✅), so the single most common action
            # collapsed the very rule you were working on. Verified in a
            # browser: a rerun that leaves the icon alone (Run preview,
            # editing a condition) keeps the rule open; only the icon change
            # closes it.
            #
            # The flag is a SEPARATE key, and is consumed here BEFORE the
            # expander is created -- Streamlit forbids writing a widget's own
            # session_state key once that widget exists in the current run,
            # so the handlers below can't set exp_key themselves.
            if st.session_state.get("_reopen_rule") == rule["id"]:
                st.session_state[exp_key] = True
                del st.session_state["_reopen_rule"]

            # key= + on_change="rerun" keeps the open/closed state in
            # st.session_state, so it survives the st.rerun() that every
            # action in this body triggers -- previously the expander
            # slammed shut on each one and you lost your place. The .open
            # guard also means the 16 rules you AREN'T looking at skip
            # building a full condition builder each (a 50+ option metric
            # selectbox, two radios and two number inputs apiece).
            exp = st.expander(expander_title, key=exp_key,
                              on_change="rerun", icon=status_icon)
            # Set by each handler that is about to st.rerun(), so the rule
            # comes back open. Deliberately NOT set unconditionally here:
            # collapsing the expander yourself also reruns, and a blanket
            # flag would force it straight back open and make the rule
            # impossible to close.
            def keep_open(_rid=rule["id"]):
                st.session_state["_reopen_rule"] = _rid

            with exp:
                if not exp.open:
                    continue
                # The chain summary used to render here too; it now lives in
                # render_condition_list below, next to the controls that act
                # on it, instead of appearing twice per rule.
                if not rule.get("conditions"):
                    st.warning("This rule has no conditions (from an older rule format) — delete it and re-add with the current builder.")
                if rule.get("scope") != "ALL" and rule.get("scope") not in market_scope_key_to_label:
                    st.markdown(f"Ticker: [{rule['scope']}]({tradingview_url(rule['scope'])})")

                en_col, up_col, dn_col, dup_col, del_col = st.columns([1, 0.6, 0.6, 1, 1])
                enabled = en_col.checkbox("Enabled", value=rule.get("enabled", True), key=f"en_{rule['id']}")
                if enabled != rule.get("enabled", True):
                    rule["enabled"] = enabled
                    save_rules(rules)
                    keep_open()
                    st.rerun()
                # Reorder acts on the FULL rules list (same rules.index(rule)
                # lookup Duplicate below uses to place a clone), not the
                # filtered visible_rules -- disabled while a search is active
                # so "move up" always means the same thing as what's on
                # screen: the whole, unfiltered list.
                _searching = bool(rule_search.strip())
                _idx = rules.index(rule)
                if up_col.button("▲", key=f"ord_up_{rule['id']}",
                                 disabled=(_idx == 0 or _searching),
                                 help="Move up" if not _searching else "Clear the search box to reorder"):
                    rules[_idx - 1], rules[_idx] = rules[_idx], rules[_idx - 1]
                    save_rules(rules)
                    keep_open()
                    st.rerun()
                if dn_col.button("▼", key=f"ord_dn_{rule['id']}",
                                 disabled=(_idx == len(rules) - 1 or _searching),
                                 help="Move down" if not _searching else "Clear the search box to reorder"):
                    rules[_idx], rules[_idx + 1] = rules[_idx + 1], rules[_idx]
                    save_rules(rules)
                    keep_open()
                    st.rerun()
                if dup_col.button("⧉ Duplicate", key=f"dup_{rule['id']}",
                                  help="Copy this rule and its conditions into a new, disabled rule"):
                    clone = copy.deepcopy(rule)
                    clone["id"] = uuid.uuid4().hex[:8]
                    clone["name"] = f"{rule.get('name') or '(unnamed)'} (copy)"
                    # Disabled, and out of the weekly wrap-up: a copy exists
                    # to be edited, and shouldn't start pinging Discord (or
                    # double-counting a stock in the Sunday roll-up) with
                    # whatever conditions it was cloned from.
                    clone["enabled"] = False
                    clone["weekly_wrapup"] = False
                    rules.insert(rules.index(rule) + 1, clone)
                    save_rules(rules)
                    st.session_state[f"rule_exp_{clone['id']}"] = True
                    st.rerun()
                if del_col.button("🗑 Delete rule", key=f"del_{rule['id']}"):
                    rules = [r for r in rules if r["id"] != rule["id"]]
                    save_rules(rules)
                    st.rerun()

                st.markdown("**Name & scope**")
                nm_col, sc_col, cl_col = st.columns([2, 2, 1.3])
                edit_name = nm_col.text_input("Name", value=rule.get("name", ""), key=f"nm_{rule['id']}")
                scope_label_map = {"ALL": "All watchlist", **market_scope_key_to_label}
                scope_edit_options = ["All watchlist"] + list(market_scope_label_to_key.keys()) + combined_tickers
                current_scope_label = scope_label_map.get(rule.get("scope"), rule.get("scope"))
                if current_scope_label not in scope_edit_options:
                    scope_edit_options = [current_scope_label] + scope_edit_options
                edit_scope_label = sc_col.selectbox(
                    "Scope", scope_edit_options, index=scope_edit_options.index(current_scope_label),
                    key=f"sc_{rule['id']}",
                )
                edit_color_ui = cl_col.selectbox(
                    "Color", RULE_COLOR_UI_OPTIONS,
                    index=RULE_COLOR_UI_OPTIONS.index(RULE_COLOR_VALUE_TO_UI.get(rule.get("color"), "None")),
                    key=f"cl_{rule['id']}",
                )

                st.markdown("**Conditions**")
                edit_conds = rule.get("conditions", [])
                if edit_conds:
                    edit_conds, ec_changed = render_condition_list(
                        edit_conds, f"ec_{rule['id']}", metric_labels_alert,
                        filterable_metrics_alert, rule_by_id=rule_by_id_alert,
                        available_rules=rules, exclude_rule_id=rule["id"],
                        definitions=metric_defs_alert,
                    )
                    if ec_changed:
                        rule["conditions"] = edit_conds
                        save_rules(rules)
                        keep_open()
                        st.rerun()

                st.caption("Add a condition to this rule:" if not edit_conds else "Add another condition:")
                if edit_conds:
                    ec_logic = st.radio(
                        "Combine with the condition(s) above using", ["AND", "OR"],
                        key=f"ec_logic_{rule['id']}", horizontal=True,
                    )
                else:
                    ec_logic = "AND"

                new_edit_cond = render_condition_builder(
                    f"ec_{rule['id']}", metric_names_alert, filterable_metrics_alert, ec_logic,
                    available_rules=rules, exclude_rule_id=rule["id"], definitions=metric_defs_alert,
                )
                if new_edit_cond:
                    edit_conds.append(new_edit_cond)
                    rule["conditions"] = edit_conds
                    save_rules(rules)
                    keep_open()
                    st.rerun()

                st.markdown("**Alert mode & schedule**")
                curr_sched = rule.get("schedule", {"type": "scheduled", "days": DEFAULT_DAYS, "time_et": "21:00"})
                es_mode_options = ["Scheduled Discord alert", "Scan only (no alert)"]
                es_mode = st.radio(
                    "Alert mode", es_mode_options,
                    index=0 if curr_sched.get("type", "scheduled") == "scheduled" else 1,
                    key=f"es_mode_{rule['id']}", horizontal=True,
                )
                curr_days_codes = curr_sched.get("days", DEFAULT_DAYS)
                curr_days_labels = [inv_day_map[d] for d in curr_days_codes if d in inv_day_map]
                curr_time = curr_sched.get("time_et", "21:00")
                h_str = curr_time.split(":")[0] if ":" in curr_time else "21"
                h_norm = f"{int(h_str):02d}" if h_str.isdigit() and int(h_str) in ALLOWED_HOURS else "21"
                h_idx = hours_list.index(h_norm)

                if es_mode == "Scheduled Discord alert":
                    es_c1, es_c2 = st.columns([3, 1.2])
                    es_days_labels = es_c1.multiselect(
                        "Days (ET)", options=day_options, default=curr_days_labels, key=f"es_days_{rule['id']}"
                    )
                    es_hour = es_c2.selectbox(
                        "Time (ET)", options=hours_list, index=h_idx,
                        format_func=lambda h: HOUR_LABELS.get(int(h), h), key=f"es_h_{rule['id']}",
                    )
                else:
                    es_days_labels, es_hour = curr_days_labels, h_norm

                # ONE save for every field that isn't already immediate.
                # Enabled and the conditions save on click; name, scope and
                # schedule used to need two SEPARATE buttons, so it was easy
                # to rename a rule, hit "Save schedule", and lose the rename.
                if st.button("💾 Save rule", key=f"rule_save_{rule['id']}", type="primary"):
                    if edit_scope_label == "All watchlist":
                        edit_scope_val = "ALL"
                    elif edit_scope_label in market_scope_label_to_key:
                        edit_scope_val = market_scope_label_to_key[edit_scope_label]
                    else:
                        edit_scope_val = edit_scope_label
                    rule["name"] = edit_name.strip()
                    rule["scope"] = edit_scope_val
                    rule["color"] = RULE_COLOR_UI_TO_VALUE[edit_color_ui]
                    if es_mode == "Scheduled Discord alert":
                        new_sched_days = [day_code_map[d] for d in es_days_labels] if es_days_labels else list(DEFAULT_DAYS)
                        rule["schedule"] = {
                            "type": "scheduled",
                            "days": new_sched_days,
                            "time_et": f"{es_hour}:00",
                        }
                    else:
                        rule["schedule"] = {"type": "none", "days": curr_days_codes, "time_et": curr_time}
                    save_rules(rules)
                    keep_open()
                    st.success("Saved name, scope and schedule.")
                    st.rerun()

    # ── Preview ─────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Preview: what would fire right now**")
    preview_cycle_ids = st.session_state.get("preview_cycle_ids", set())
    if preview_cycle_ids:
        cycle_names = [f"{rule_by_id_alert.get(rid, {}).get('name') or 'alert'} [{rid}]" for rid in sorted(preview_cycle_ids)]
        st.warning(
            f"⚠ Circular alert references detected: {', '.join(cycle_names)}. "
            "Those rule references are treated as not matching — break the cycle to use them."
        )
    # Playlist filter: placed right above the button so it's contextually clear
    _preview_mkt_filter = st.multiselect(
        "🎵 Playlist",
        options=_alert_mkt_all_labels,
        default=_alert_mkt_all_labels,
        key="preview_market_filter",
        help="Select which market(s) to scan. Defaults to all markets.",
    )
    _preview_mkt_keys = (
        [_alert_mkt_label_to_key[lbl] for lbl in _preview_mkt_filter]
        if _preview_mkt_filter else market_keys_now
    )
    filtered_results_preview = [r for mkt in _preview_mkt_keys for r in per_market.get(mkt, [])]
    if not _preview_mkt_filter:
        st.caption("⚠ No markets selected — all markets will be used.")
    if st.button("Run preview"):
        preview, preview_cycle_ids = preview_rules(rules, filtered_results_preview)
        st.session_state.preview_cycle_ids = preview_cycle_ids
        st.session_state.preview_active = [p for p in preview if p["is_true_now"]]
        st.session_state.preview_as_of = as_of
        # Remember which markets were used so we can warn on stale results
        st.session_state.preview_market_keys = list(_preview_mkt_keys)

    active = st.session_state.get("preview_active")
    if active is not None:
        # Warn if the market filter changed since the last run
        _prev_mkt_keys = st.session_state.get("preview_market_keys")
        if _prev_mkt_keys is not None and sorted(_prev_mkt_keys) != sorted(_preview_mkt_keys):
            _prev_labels = ", ".join(
                markets_registry_now.get(k, {}).get("label", k) for k in _prev_mkt_keys
            )
            st.info(
                f"ℹ Results below are from the previous filter: **{_prev_labels}**. "
                "Click **Run preview** again to refresh with the current selection."
            )
        if not active:
            st.write("No rule conditions are currently true.")
        else:
            rows_preview = []
            for p in active:
                market_key = p["row"].get("market", "")
                watchlist_label = markets_registry_now.get(market_key, {}).get("label", market_key)
                rows_preview.append({
                    "Ticker": tradingview_url(p["ticker"]),
                    "Watchlist": watchlist_label,
                    "Rule": p.get("rule_name") or "(unnamed)",
                    "Conditions": describe_chain_with_values(p["row"], p["conditions"], metric_labels_alert, rule_by_id_alert),
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

                    ok, detail = send_discord_batch(webhook, all_msgs)
                    if ok:
                        st.success(
                            f"Sent {len(active)} match(es) across {len(tickers_by_rule)} rule(s) to Discord "
                            f"({len(all_msgs)} message{'s' if len(all_msgs) != 1 else ''})."
                        )
                    else:
                        st.error(f"Failed to send — {detail}")

    # ── Weekly wrap-up ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("**📅 Weekly wrap-up**")
    st.caption(
        "A Sunday digest over **all enabled alerts** (same set and numbers as the dashboard Alerts "
        "column): one table per alert, then a roll-up counting how many alerts each stock matches "
        "— the most active names first. Sent to Discord every Sunday 9:00 PM ET. "
        "Running it here is **read-only** — it never advances the Wk counters."
    )

    wrapup_eligible = eligible_rules(rules)
    if not wrapup_eligible:
        st.info("No enabled rules with conditions yet — add one above to use the weekly wrap-up.")
    else:
        # Playlist filter: scoped to this section, placed right above the button
        _wrapup_mkt_filter = st.multiselect(
            "🎵 Playlist",
            options=_alert_mkt_all_labels,
            default=_alert_mkt_all_labels,
            key="wrapup_market_filter",
            help="Select which market(s) to include in the wrap-up. Defaults to all markets.",
        )
        _wrapup_mkt_keys = (
            [_alert_mkt_label_to_key[lbl] for lbl in _wrapup_mkt_filter]
            if _wrapup_mkt_filter else market_keys_now
        )
        filtered_results_wrapup = [r for mkt in _wrapup_mkt_keys for r in per_market.get(mkt, [])]
        if not _wrapup_mkt_filter:
            st.caption("⚠ No markets selected — all markets will be used.")
        if st.button("Build wrap-up now"):
            st.session_state.wrapup_report = build_wrapup(
                rules, filtered_results_wrapup, load_wrapup_state(),
                metric_labels=metric_labels_alert,
                registry=markets_registry_now,
                as_of=as_of,
            )
            # Remember which markets were used so we can warn on stale results
            st.session_state.wrapup_market_keys = list(_wrapup_mkt_keys)

            report = st.session_state.get("wrapup_report")
            if report is not None:
                # Warn if the market filter changed since the last build
                _prev_wrapup_mkt_keys = st.session_state.get("wrapup_market_keys")
                if (_prev_wrapup_mkt_keys is not None
                        and sorted(_prev_wrapup_mkt_keys) != sorted(_wrapup_mkt_keys)):
                    _prev_wrapup_labels = ", ".join(
                        markets_registry_now.get(k, {}).get("label", k)
                        for k in _prev_wrapup_mkt_keys
                    )
                    st.info(
                        f"ℹ Wrap-up below was built with: **{_prev_wrapup_labels}**. "
                        "Click **Build wrap-up now** to refresh with the current selection."
                    )
                anchor = report.get("state_last_run")
                st.caption(
                    f"**{_pretty_date(report['run_date'])}** · {len(report['alerts'])} alert(s) · "
                    f"{report['total_stocks']} stock(s) · data as of {report.get('as_of') or as_of} · "
                    + (f"Wk measured from the {_pretty_date(anchor)} run."
                       if anchor else
                       "no previous run recorded yet, so every stock reads Wk 0.")
                )
                if report["cycle_ids"]:
                    cyc = [f"{rule_by_id_alert.get(rid, {}).get('name') or 'alert'} [{rid}]"
                           for rid in sorted(report["cycle_ids"])]
                    st.warning(
                        f"⚠ Circular alert references detected: {', '.join(cyc)}. "
                        "Those rule references are treated as not matching."
                    )

                st.markdown("  ·  ".join(
                    f"**{a['num']}.** {a['name']} ({len(a['rows'])})" for a in report["alerts"]
                ))

                for a in report["alerts"]:
                    st.markdown(
                        f"**{a['num']}. {a['name']}** — {a['scope_label']} · "
                        f"{len(a['rows'])} stock{'s' if len(a['rows']) != 1 else ''}"
                        + ("  🔍 scan only" if a["scan_only"] else "")
                    )
                    if not a["rows"]:
                        st.caption("No matches right now.")
                        continue
                    # Ticker column carries a TradingView link, so the rendered
                    # cell differs from the raw symbol used in the Discord table.
                    adf = pd.DataFrame(
                        [[tradingview_url(r[0])] + r[1:] for r in a["rows"]],
                        columns=a["headers"],
                    )
                    st.dataframe(adf, width="stretch", hide_index=True,
                                 column_config=LINK_COLUMN_CONFIG)

                st.markdown("**🔥 Most active stocks** — across the alerts above")
                if not report["rollup"]:
                    st.caption("No stock matched any of the selected alerts.")
                else:
                    rdf = pd.DataFrame([
                        {
                            "Ticker": tradingview_url(r["ticker"]),
                            "Watchlist": r["market"],
                            "# Alerts": r["count"],
                            "Alerts#": ", ".join(str(n) for n in r["alert_nums"]),
                            "Oldest Wk": r["oldest_weeks"],
                        }
                        for r in report["rollup"]
                    ])
                    st.dataframe(rdf, width="stretch", hide_index=True,
                                 column_config=LINK_COLUMN_CONFIG)

                if st.button("📤 Send this wrap-up to Discord"):
                    webhook = get_discord_webhook()
                    if not webhook:
                        st.error("No Discord webhook configured yet — set one in the Discord section below.")
                    else:
                        msgs = build_wrapup_messages(report)
                        ok, detail = send_discord_batch(webhook, msgs)
                        if ok:
                            st.success(f"Sent {len(msgs)} message(s) to Discord. "
                                       "Wk counters were not advanced — only the Sunday run does that.")
                        else:
                            st.error(f"Failed to send — {detail}")

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
            ok, detail = send_discord_batch(webhook_input, ["✅ Test alert from your Stock Watchlist app."])
            st.success("Sent!") if ok else st.error(f"Failed to send — {detail}")

    st.divider()
    with st.expander("☁️ Push config to GitHub", expanded=False):
        gh_token, gh_repo, gh_branch = get_github_config(getattr(st, "secrets", None))
        if not gh_token or not gh_repo:
            st.caption(
                "Edits made here (watchlist, custom filters, settings, alert rules) only live on "
                "this instance's disk -- they won't reach GitHub Actions (or survive a redeploy) "
                "until pushed. Set **GITHUB_TOKEN** (a fine-grained PAT with Contents: read/write "
                "on this repo) and **GITHUB_REPO** (`owner/repo-name`) as secrets to enable this -- "
                "see DEPLOYMENT.md."
            )
        else:
            st.caption(f"Target: `{gh_repo}` @ `{gh_branch}`")
            st.caption(
                "Pushes all configuration files (watchlists, filters, settings, alerts) "
                "plus the current data snapshot as one combined commit -- important on "
                "Streamlit Cloud, which auto-redeploys the instant any commit lands."
            )
            if st.button("Push to GitHub", width="stretch"):
                targets = [fname for fname, label in SYNCABLE_FILES]
                ok, msg = push_all_config(gh_token, gh_repo, gh_branch, filenames=targets)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.caption("Your app may restart shortly since Streamlit Cloud watches this repo.")
