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
    MARKETS, load_data_snapshot, snapshot_is_usable, save_data_snapshot,
)
from alerts import (load_rules, save_rules, preview_rules, DISCORD_CONFIG_FILE, send_discord, SCOPE_LABELS,
                     build_discord_messages_for_rule, describe_schedule, DAY_CODES, DAY_LABELS,
                     DEFAULT_DAYS, ALLOWED_HOURS, HOUR_LABELS)
from filters import (get_market_filters, save_market_filters, apply_filters, describe_filter,
                     describe_chain, describe_chain_with_values, passes_filter_chain, CATEGORICAL_METRICS)
from github_sync import get_github_config, push_all_config, SYNCABLE_FILES
from news_summary import load_news_summary, MARKET_LABELS, get_gemini_api_key
from expert_views import load_expert_views, save_expert_views, analyze_single_ticker, generate_expert_view
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


def load_column_prefs():
    """Returns the saved column order (list of data keys, visible ones only,
    in display order) from column_prefs.json, or None if there's no file yet
    or it's malformed -- callers fall back to the built-in default order in
    that case. Unlike discord_config.json/auth_config.json, this file is NOT
    secret -- it's meant to be committed/pushed like watchlist.json etc. so
    a column layout set once (locally or on the deployed app) is shared by
    everyone who opens the app, not just the browser session that set it."""
    if not os.path.exists(COLUMN_PREFS_FILE):
        return None
    try:
        with open(COLUMN_PREFS_FILE) as f:
            data = json.load(f)
        order = data.get("order")
        if isinstance(order, list) and all(isinstance(k, str) for k in order):
            return order
    except Exception:
        pass
    return None


def save_column_prefs(order):
    with open(COLUMN_PREFS_FILE, "w") as f:
        json.dump({"order": order}, f, indent=2)

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


def _is_streamlit_cloud():
    """Returns True when running on Streamlit Community Cloud.
    Detects the cloud environment by checking for the HOSTNAME env var
    pattern used by Streamlit Cloud workers, or by checking that
    st.context.headers contains the cloud-specific host header.
    Avoids any import that could fail on older Streamlit versions."""
    import os
    # Streamlit Cloud always injects this env var
    if os.environ.get("STREAMLIT_SHARING_MODE") == "1":
        return True
    # Fallback: check if running as a Streamlit Cloud container
    # These vars are set on Streamlit Cloud worker machines
    if os.environ.get("HOME") == "/home/appuser":
        return True
    return False


def _query_param_token_key():
    return "_swa_t"


def _set_auth_token(token):
    """Persists the remember-me token. On Streamlit Cloud, JS cookie writes
    are blocked by the cross-origin iframe sandbox so we fall back to storing
    the token in st.session_state and surfacing it to the user as a URL
    query parameter they can bookmark. Locally, we still write a real browser
    cookie as before, with a 100ms-delayed page reload to avoid the race
    condition between the script write and st.rerun()."""
    from urllib.parse import quote
    if _is_streamlit_cloud():
        # Store in session state (survives reruns in the same session)
        st.session_state["_remember_token"] = token
        # Also inject into query params so a hard-reload in the same tab works.
        # The user can bookmark this URL to avoid re-logging in each time.
        st.query_params[_query_param_token_key()] = token
    else:
        # Local: write a real browser cookie
        encoded = quote(token, safe="")
        import streamlit.components.v1 as components
        components.html(
            f'<script>'
            f'try {{ window.parent.document.cookie = "{REMEMBER_COOKIE_NAME}={encoded}; path=/; max-age={REMEMBER_DAYS * 86400}; SameSite=Lax"; }} catch(e) {{}}'
            f'document.cookie = "{REMEMBER_COOKIE_NAME}={encoded}; path=/; max-age={REMEMBER_DAYS * 86400}; SameSite=Lax";'
            f'setTimeout(function() {{ window.parent.location.reload(); }}, 150);'
            f'</script>',
            height=0,
        )


def _clear_auth_token():
    """Clears the remember-me token from wherever it was stored."""
    # Clear from query params
    if _query_param_token_key() in st.query_params:
        del st.query_params[_query_param_token_key()]
    # Clear from session state
    st.session_state.pop("_remember_token", None)
    if not _is_streamlit_cloud():
        # Local: also expire the real browser cookie
        import streamlit.components.v1 as components
        components.html(
            f'<script>'
            f'try {{ window.parent.document.cookie = "{REMEMBER_COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax"; }} catch(e) {{}}'
            f'document.cookie = "{REMEMBER_COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax";'
            f'setTimeout(function() {{ window.parent.location.reload(); }}, 150);'
            f'</script>',
            height=0,
        )


def _get_stored_token(username, password):
    """Retrieves and validates the stored remember-me token from all
    possible locations: query params (Cloud + local after browser reload),
    session state (Cloud, in-session), and browser cookies (local only).
    Returns the token string if valid, or None."""
    from urllib.parse import unquote

    candidates = []

    # 1. Query param (works on both Cloud and local after reload)
    qp = st.query_params.get(_query_param_token_key())
    if qp:
        candidates.append(qp)

    # 2. Session state (Cloud in-session memory)
    ss = st.session_state.get("_remember_token")
    if ss:
        candidates.append(ss)

    # 3. Browser cookie (local only - will be empty on Cloud)
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
                st.session_state["_remember_token"] = token
                if _is_streamlit_cloud():
                    # On Cloud, query params are set above and session state
                    # is in memory -- just rerun normally, no JS reload needed
                    st.rerun()
                else:
                    # Local: JS will trigger a reload after writing the cookie
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
        _clear_auth_token()
        st.rerun()


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
    last = row["Last"]
    ema_cols = set(ema_labels.values())
    for i, col in enumerate(row.index):
        val = row[col]
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
        elif col == "% Chg" and pd.notna(val):
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


def price_cols(ema_labels):
    """Price-denominated columns -- shown as whole numbers (no decimal),
    since sub-dollar/rupee precision isn't meaningful at a glance here."""
    return ["Last", ema_labels["w_fast"], ema_labels["w_mid"], ema_labels["w_slow"],
            ema_labels["d_fast"], ema_labels["d_mid"], ema_labels["d_slow"],
            "VStop-W", "52W High", "52W Low"]


def ratio_cols():
    """Oscillator/ratio columns -- kept at 1 decimal (whole numbers would
    lose meaningful resolution for RSI/RS reads)."""
    return ["RSI-D", "RSI-W", "RSI-M", "RS-D", "RS-W", "RS-M"]


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


def column_definitions(settings, labels):
    """label -> plain-language definition, for the header info-icon hover
    tooltip. Bench/period/threshold numbers are pulled from `settings` so
    the tooltip always reflects your current configuration, not defaults."""
    bench_note = "your configured benchmark"
    defs = {
        "Ticker": "Click to open this symbol's chart on TradingView.",
        "Last": "Most recent daily closing price.",
        labels["w_fast"]: f"Weekly EMA, fast period ({settings['ema_weekly'][0]} weeks).",
        labels["w_mid"]: f"Weekly EMA, medium period ({settings['ema_weekly'][1]} weeks).",
        labels["w_slow"]: f"Weekly EMA, slow period ({settings['ema_weekly'][2]} weeks). The 'slow WEMA' referenced by Trend and Tech Uptrend.",
        labels["d_fast"]: f"Daily EMA, fast period ({settings['ema_daily'][0]} days).",
        labels["d_mid"]: f"Daily EMA, medium period ({settings['ema_daily'][1]} days).",
        labels["d_slow"]: f"Daily EMA, slow period ({settings['ema_daily'][2]} days).",
        "RSI-D": f"Daily RSI, {settings['rsi_period']}-period. ≤30 oversold, ≥70 overbought.",
        "RSI-W": f"Weekly RSI, {settings['rsi_period']}-period. ≤30 oversold, ≥70 overbought.",
        "RSI-M": f"Monthly RSI, {settings['rsi_period']}-period. ≤30 oversold, ≥70 overbought.",
        "RS-D": f"Mansfield RS (daily) vs {bench_note}. Positive = outperforming, negative = underperforming.",
        "RS-W": f"Mansfield RS (weekly) vs {bench_note}. Positive = outperforming, negative = underperforming.",
        "RS-M": f"Mansfield RS (monthly) vs {bench_note}. Positive = outperforming, negative = underperforming.",
        "VStop-W": f"Weekly Volatility Stop (Wilder's ATR stop-and-reverse, length={settings['vstop_length']}, factor={settings['vstop_factor']}).",
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
        "Data Thru": "Most recent date with price data for this ticker. Shown in red if 3+ days stale.",
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
        "Flag": "A colored marker you set via the sidebar 'Ticker Notes' panel -- also shown next to the ticker symbol itself.",
        "Notes": "Your free-text note for this ticker, set via the sidebar 'Ticker Notes' panel. Hover/tap a truncated note to see the full text.",
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
        "21. Min 10D ÷ 100D vol ratio", min_value=1.0, step=0.1, format="%.2f",
        value=float(settings.get("tech_uptrend_volume_ratio", 1.4)), key="set_tech_vol_ratio",
        help="Tech Uptrend also requires 10-day average volume ≥ this many times the 100-day average. "
             "This column's own parameter — independent of Vol Trend's 'Exploding' ratio above, even "
             "though both default to the same value.",
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
                "trend_near_high_low_pct": float(trend_near_pct),
                "trend_volume_ratio": float(trend_vol_ratio),
                "volume_explode_ratio": float(volume_explode_ratio),
                "volume_decline_ratio": float(volume_decline_ratio),
                "tech_uptrend_min_vstop_weeks": int(tech_uptrend_min_vstop_weeks),
                "tech_uptrend_volume_ratio": float(tech_uptrend_vol_ratio),
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


def render_condition_builder(key_prefix, metric_names, filterable_metrics, logic_choice):
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

    Returns a finished condition dict (with "logic" set to `logic_choice`,
    ready to append to a conditions list) the moment "Add" is clicked with
    valid inputs, else None."""
    metric_a_label = st.selectbox("Metric A", metric_names, key=f"{key_prefix}_a")
    metric_a_key = filterable_metrics[metric_a_label]
    categorical_options = CATEGORICAL_METRICS.get(metric_a_key)

    if categorical_options:
        c1, c2 = st.columns([4, 1])
        selected = c1.multiselect(
            "Value(s) — matches if Trend/Vol Trend/etc. is ANY of these",
            options=categorical_options, key=f"{key_prefix}_catval",
        )
        if c2.button("＋ Add condition", key=f"{key_prefix}_addbtn"):
            if not selected:
                st.warning("Select at least one value.")
                return None
            return {
                "metric_a": metric_a_key, "operator": "in", "compare_type": "value",
                "value": selected, "logic": logic_choice,
            }
        return None

    c1, c2, c3 = st.columns([1, 1.5, 2])
    operator_choice = c1.selectbox("Op", [">", "<", ">=", "<=", "=="], key=f"{key_prefix}_op")
    compare_type = c2.radio("Compare to", ["Metric", "Fixed value"], key=f"{key_prefix}_ctype", horizontal=True)
    if compare_type == "Metric":
        metric_b_label = c3.selectbox("Metric B", metric_names, key=f"{key_prefix}_b")
        mc1, mc2, mc3 = st.columns([1, 1, 1])
        multiplier = mc1.number_input(
            "× Multiplier (optional)", value=1.0, step=0.1, format="%.2f", key=f"{key_prefix}_mult",
            help="e.g. set to 1.4 for 'Vol 10D Avg >= 1.4 × Vol 100D Avg'.",
        )
        offset = mc2.number_input("+ Offset (optional)", value=0.0, step=0.1, format="%.2f", key=f"{key_prefix}_off")
        if mc3.button("＋ Add condition", key=f"{key_prefix}_addbtn"):
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
        value_text = vc1.text_input(
            "Value", value="0", key=f"{key_prefix}_val",
            help="A number (e.g. 45) or a word for boolean-like metrics, e.g. Yes / No for Tech Uptrend.",
        )
        if vc2.button("＋ Add condition", key=f"{key_prefix}_addbtn"):
            return {
                "metric_a": metric_a_key, "operator": operator_choice, "compare_type": "value",
                "value": parse_filter_value_text(value_text), "logic": logic_choice,
            }
        return None


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

    new_filter = render_condition_builder(f"cf_{market}", metric_names, filterable_metrics, logic_choice)
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
        ("expert_take", "Expert Take"),
        ("trend", "Trend"),
        ("flag", "Flag"),
        ("note", "Notes"),
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
        ("net_volume_10d_dir", "Net Vol 10D"),
        ("tech_uptrend_label", "Tech Uptrend"),
        ("avg_volume_10d", "Vol 10D"),
        ("avg_volume_100d", "Vol 100D"),
    ]
    for col in (custom_columns if custom_columns is not None else load_custom_columns()):
        if col.get("enabled", True):
            optional_defs.append((column_key(col), col["name"]))
    label_by_key = dict(optional_defs)
    key_by_label = {lbl: k for k, lbl in optional_defs}
    all_labels = list(label_by_key.values())
    # Raw 10D/100D volume are hidden by default (Vol Trend already
    # summarizes them); everything else shows by default.
    default_hidden = {"Vol 10D", "Vol 100D"}
    default_visible = [lbl for lbl in all_labels if lbl not in default_hidden]
    return optional_defs, label_by_key, key_by_label, all_labels, default_visible


def apply_sort(rows, sort_field, ascending):
    """Sorts `rows` (list of raw row dicts, BEFORE they become a DataFrame)
    by `sort_field`. Missing values (None, "", or NaN) always sort to the
    end regardless of direction -- the usual spreadsheet convention --
    rather than clustering at the front on a descending sort. This is the
    practical stand-in for 'click a column header to sort' -- see
    render_shared_sort_control's docstring for why a literal header-click
    isn't feasible with this table."""
    if not sort_field:
        return rows

    def is_missing(v):
        return v is None or v == "" or (isinstance(v, float) and pd.isna(v))

    present = [r for r in rows if not is_missing(r.get(sort_field))]
    missing = [r for r in rows if is_missing(r.get(sort_field))]

    def keyfn(r):
        v = r[sort_field]
        return v.lower() if isinstance(v, str) else v

    return sorted(present, key=keyfn, reverse=not ascending) + missing


def render_shared_sort_control(label_by_key, key_by_label):
    """Shared 'Sort by' control, sidebar, always visible (not tucked into
    an expander -- used often enough to want one click, not two). Applied
    identically to both the US and India tables, same reasoning as the
    shared column picker above.

    True click-on-column-header sorting isn't feasible with the current
    table: it's rendered as a static HTML block (via st.markdown, needed
    for the sticky header/frozen ticker column -- see sticky_header_html's
    docstring) and Streamlit's markdown renderer strips <script>/<style>
    tags outright even with unsafe_allow_html=True, so there's no way to
    attach a client-side click handler to a <th>. This dropdown gives the
    same practical outcome -- sort the whole table by any column -- without
    rearchitecting the table onto a JS-executing surface (e.g. st.iframe),
    which would mean giving up the dynamic page-filling height that took
    real effort to get right (see 'Make watchlist tables fill page height
    with sticky header' in the project history) since iframes need a fixed
    height. Returns (sort_field_key_or_None, ascending)."""
    # matched_alerts (Alerts) and vstop_change (VStop Weeks Ago) are
    # computed AFTER filtering, inside render_market_tab -- not present on
    # the raw row dict at sort time -- excluded rather than silently
    # sorting wrong (or crashing on a missing key).
    sort_labels = ["(default order)", "Ticker", "Last"] + [
        lbl for key, lbl in label_by_key.items() if key not in ("matched_alerts", "vstop_change")
    ]
    sc1, sc2 = st.sidebar.columns([3, 1])
    sort_label = sc1.selectbox("Sort by", sort_labels, key="shared_sort_field")
    ascending = sc2.selectbox("Dir", ["↑", "↓"], key="shared_sort_dir") == "↑"
    if sort_label == "(default order)":
        return None, ascending
    sort_field = {"Ticker": "ticker", "Last": "last_close"}.get(sort_label) or key_by_label.get(sort_label)
    return sort_field, ascending


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
    it. Returns (visible_keys, label_by_key, sort_field, sort_ascending)."""
    custom_columns = load_custom_columns()
    optional_defs, label_by_key, key_by_label, all_labels, default_visible = build_column_defs(labels, custom_columns)

    sort_field, sort_ascending = render_shared_sort_control(label_by_key, key_by_label)

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

    with st.sidebar.expander("Columns to show / reorder", expanded=False):
        st.caption(
            "Applies to both the US and India tables. Ticker and Last always show first. "
            "Saved to column_prefs.json -- push it via the Alert Rules tab's GitHub button "
            "to make this layout show up on the deployed app too."
        )
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

    render_custom_columns_manager()
    render_ticker_notes_manager()

    return st.session_state[SHARED_ORDER_KEY], label_by_key, sort_field, sort_ascending


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
    all_tickers = sorted(set(watchlists.get("US", []) + watchlists.get("INDIA", [])))
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


def _is_valid_view(view):
    """Returns True if a view is a real successful analysis (not a 429/error fallback)."""
    if not view:
        return False
    verdict = view.get("verdict")
    if verdict not in ("ACCUMULATE", "HOLD", "CAUTION"):
        return False
    headline = (view.get("headline") or "").lower()
    if "429" in headline or "resource_exhausted" in headline or "analysis pending" in headline or "error" in headline:
        return False
    return True


def sync_expert_views_to_github(message):
    token, repo, branch = get_github_config(st.secrets)
    if token and repo:
        ok, msg = push_all_config(
            token, repo, branch,
            filenames=["expert_views.json"],
            message=message
        )
        if ok:
            st.toast("✓ Saved & committed updated AI Expert Views to GitHub!")
        else:
            st.toast(f"✓ Saved locally! (GitHub sync: {msg})")
    else:
        st.toast("✓ Saved updated AI Expert Views!")


def render_expert_analysis_control_bar(market, results):
    expert_views = load_expert_views()
    api_key = get_gemini_api_key(st.secrets)

    all_tickers = [r["ticker"] for r in results]

    # --- Auto-cleanup: remove stale tickers no longer in watchlist ---
    stale_keys = [tk for tk in expert_views if tk not in all_tickers]
    if stale_keys:
        for tk in stale_keys:
            del expert_views[tk]
        save_expert_views(expert_views)
        sync_expert_views_to_github(f"Auto-cleanup: removed {len(stale_keys)} deleted ticker(s) from expert_views")

    # --- Detect failed/pending tickers (includes newly added ones with no entry) ---
    failed_tickers = [
        tk for tk in all_tickers
        if not _is_valid_view(expert_views.get(tk))
    ]

    st.markdown("##### 🤖 AI Stock Expert Analysis Controls")
    c1, c2, c3, c4 = st.columns([3.5, 1.3, 1.8, 1.4])

    selected_to_reanalyze = c1.multiselect(
        "Select tickers to re-analyze",
        options=all_tickers,
        key=f"ev_multisel_{market}",
        placeholder="Choose tickers to re-analyze...",
        label_visibility="collapsed",
    )

    if c2.button(
        f"⚡ Re-analyze ({len(selected_to_reanalyze)})",
        key=f"btn_re_sel_{market}",
        disabled=not selected_to_reanalyze or not api_key,
        width="stretch",
    ):
        progress_bar = st.progress(0, text="Starting selective AI re-analysis...")
        from google import genai
        client = genai.Client(api_key=api_key)
        updated_views = load_expert_views()

        for idx, tk in enumerate(selected_to_reanalyze):
            row = next(r for r in results if r["ticker"] == tk)
            progress_bar.progress(
                (idx + 1) / len(selected_to_reanalyze),
                text=f"Analyzing {tk} ({idx+1}/{len(selected_to_reanalyze)})...",
            )
            view = generate_expert_view(client, row)
            if _is_valid_view(view):
                updated_views[tk] = view
                save_expert_views(updated_views)
            else:
                progress_bar.progress(
                    (idx + 1) / len(selected_to_reanalyze),
                    text=f"⚠️ {tk} still rate-limited, keeping existing result.",
                )
            time.sleep(15)

        sync_expert_views_to_github(f"Re-analyze selected tickers ({len(selected_to_reanalyze)}) via UI")
        st.rerun()

    failed_count = len(failed_tickers)
    if c3.button(
        f"⚠️ Retry Failed / Pending ({failed_count})",
        key=f"btn_re_failed_{market}",
        disabled=failed_count == 0 or not api_key,
        type="primary" if failed_count > 0 else "secondary",
        width="stretch",
    ):
        progress_bar = st.progress(0, text=f"Retrying {failed_count} failed/pending tickers...")
        from google import genai
        client = genai.Client(api_key=api_key)
        updated_views = load_expert_views()

        for idx, tk in enumerate(failed_tickers):
            row = next(r for r in results if r["ticker"] == tk)
            progress_bar.progress(
                (idx + 1) / failed_count,
                text=f"Retrying {tk} ({idx+1}/{failed_count})...",
            )
            view = generate_expert_view(client, row)
            if _is_valid_view(view):
                updated_views[tk] = view
                save_expert_views(updated_views)
            else:
                progress_bar.progress(
                    (idx + 1) / failed_count,
                    text=f"⚠️ {tk} still rate-limited, skipping overwrite.",
                )
            time.sleep(15)

        sync_expert_views_to_github(f"Retry failed/pending tickers ({failed_count}) via UI")
        st.rerun()

    if c4.button(
        f"🔄 Re-analyze All ({len(all_tickers)})",
        key=f"btn_re_all_{market}",
        disabled=not api_key,
        width="stretch",
    ):
        progress_bar = st.progress(0, text=f"Re-analyzing all {len(all_tickers)} tickers...")
        from google import genai
        client = genai.Client(api_key=api_key)
        updated_views = load_expert_views()

        for idx, tk in enumerate(all_tickers):
            row = next(r for r in results if r["ticker"] == tk)
            progress_bar.progress(
                (idx + 1) / len(all_tickers),
                text=f"Analyzing {tk} ({idx+1}/{len(all_tickers)})...",
            )
            old_view = updated_views.get(tk)
            view = generate_expert_view(client, row)
            if _is_valid_view(view):
                updated_views[tk] = view
                save_expert_views(updated_views)
            elif _is_valid_view(old_view):
                # Keep the pre-existing good result, don't overwrite with 429
                progress_bar.progress(
                    (idx + 1) / len(all_tickers),
                    text=f"⚠️ {tk} rate-limited — keeping previous result.",
                )
            else:
                updated_views[tk] = view  # Still a failure but save it (nothing to preserve)
                save_expert_views(updated_views)
            if idx < len(all_tickers) - 1:
                time.sleep(15)

        # Auto-retry any tickers that failed during the main pass
        still_failed = [tk for tk in all_tickers if not _is_valid_view(updated_views.get(tk))]
        if still_failed:
            progress_bar.progress(1.0, text=f"Main pass done. Auto-retrying {len(still_failed)} failed ticker(s)...")
            time.sleep(15)  # cool-down before retry pass
            for idx, tk in enumerate(still_failed):
                row = next(r for r in results if r["ticker"] == tk)
                progress_bar.progress(
                    (idx + 1) / len(still_failed),
                    text=f"Retrying failed: {tk} ({idx+1}/{len(still_failed)})...",
                )
                view = generate_expert_view(client, row)
                if _is_valid_view(view):
                    updated_views[tk] = view
                    save_expert_views(updated_views)
                if idx < len(still_failed) - 1:
                    time.sleep(15)

        sync_expert_views_to_github(f"Re-analyze all tickers ({len(all_tickers)}) + auto-retry via UI")
        st.rerun()


def render_expert_view_expander(market, filtered_rows, settings):
    expert_views = load_expert_views()
    tickers = [r["ticker"] for r in filtered_rows]
    if not tickers:
        return

    v_colors = {"ACCUMULATE": "#1a7a3a", "HOLD": "#7a6a00", "CAUTION": "#7a1a1a"}
    v_badges = {"ACCUMULATE": "🟢 ACCUMULATE / ADD", "HOLD": "🟡 HOLD / WATCH", "CAUTION": "🔴 CAUTION / EXIT"}
    api_key = get_gemini_api_key(st.secrets)

    with st.expander(f"🤖 AI Stock Expert Views ({market} — {len(tickers)} tickers)", expanded=False):
        # Scrollable container for all ticker cards
        scroll_html_open = (
            "<div style='max-height:680px; overflow-y:auto; padding-right:6px; "
            "border:1px solid rgba(128,128,128,0.2); border-radius:8px; padding:12px;'>"
        )
        st.markdown(scroll_html_open, unsafe_allow_html=True)

        for ticker in tickers:
            view = expert_views.get(ticker)
            row = next(r for r in filtered_rows if r["ticker"] == ticker)

            if not view or not _is_valid_view(view):
                # Compact pending card
                st.markdown(
                    f"<div style='padding:10px 14px; margin-bottom:8px; border-radius:8px; "
                    f"border:1px solid rgba(128,128,128,0.25); background:rgba(128,128,128,0.05);'>"
                    f"<b>{ticker}</b> &nbsp;⚪ Pending — no AI analysis yet.</div>",
                    unsafe_allow_html=True,
                )
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
                f"Generated: {as_of} · News: {news_source}</div>"
                f"</div>"
            )
            st.markdown(card_html, unsafe_allow_html=True)

            # Per-ticker re-analyze button
            if api_key:
                if st.button(f"⚡ Re-analyze {ticker}", key=f"re_ev_{market}_{ticker}", use_container_width=False):
                    with st.spinner(f"Analyzing {ticker} with gemini-3.5-flash-lite..."):
                        analyze_single_ticker(ticker, row, api_key)
                        sync_expert_views_to_github(f"Re-analyze single ticker ({ticker}) via UI")
                        st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)


def render_market_tab(market, results, settings, visible_keys, label_by_key, sort_field=None, sort_ascending=True):
    benchmarks = get_benchmarks(settings)
    bench = benchmarks[market]
    labels = ema_col_labels(settings)
    custom_columns = load_custom_columns()
    filterable_metrics = get_all_filterable_metrics(settings, custom_columns)

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

    src1, src2, src3, src4, src5 = st.columns([2.5, 1.2, 1.2, 1.3, 0.8])
    search = src1.text_input("Ticker search", "", key=f"search_{market}").strip().upper()
    f_trend = src2.selectbox(
        "Trend", ["Any", "Strong Uptrend", "Uptrend", "Downtrend", "Strong Downtrend"],
        key=f"f_trend_{market}",
    )
    f_vol_trend = src3.selectbox(
        "Vol Trend", ["Any", "Exploding", "In-line", "Declining"], key=f"f_voltrend_{market}",
    )
    f_expert_take = src4.selectbox(
        "Expert Take", ["Any", "🟢 Accumulate", "🟡 Hold", "🔴 Caution", "⚪ Pending"],
        key=f"f_expert_{market}",
    )
    f_tech_only = src5.checkbox("Tech Uptrend only", key=f"f_tech_{market}")

    with st.expander("Custom filters (metric vs metric, or metric vs fixed value; chain with AND/OR)", expanded=False):
        active_custom_filters = render_custom_filter_builder(market, filterable_metrics)

    # Filter this watchlist by any saved rule from the Alert Rules tab --
    # reuses the exact same condition-chain engine (filters.passes_filter_chain)
    # that alerts and custom filters use, so results match what that rule
    # would flag. Rules are listed regardless of the market tab they were
    # created for or their scope (US/INDIA/ALL) -- a rule is just a reusable
    # bundle of conditions here, applicable from any tab.
    scan_rules_all = [r for r in load_rules() if r.get("enabled", True) and r.get("conditions")]
    scan_rule_labels = [f"{r.get('name') or '(unnamed)'} [{r['id']}]" for r in scan_rules_all]
    scan_rule_by_label = dict(zip(scan_rule_labels, scan_rules_all))
    selected_scan_labels = st.multiselect(
        "Filter by Saved Scans / Alerts (combines with AND logic)",
        options=scan_rule_labels,
        key=f"f_scans_{market}",
        help="Applies the metric conditions from selected alert/scan rules to this watchlist, "
             "regardless of which tab or scope the rule was originally set up under.",
    )
    selected_scans = [scan_rule_by_label[lbl] for lbl in selected_scan_labels]

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
        if selected_scans and not all(passes_filter_chain(row, sr.get("conditions", [])) for sr in selected_scans):
            continue
        # Expert Take filter — resolved against live expert_views
        if f_expert_take != "Any":
            ev = load_expert_views().get(row["ticker"], {})
            verdict = ev.get("verdict", "")
            headline = (ev.get("headline") or "").lower()
            is_pending = not verdict or verdict in ("PENDING", "FAILED") or "429" in headline or "analysis pending" in headline
            if f_expert_take == "🟢 Accumulate" and verdict != "ACCUMULATE":
                continue
            elif f_expert_take == "🟡 Hold" and verdict != "HOLD":
                continue
            elif f_expert_take == "🔴 Caution" and verdict != "CAUTION":
                continue
            elif f_expert_take == "⚪ Pending" and not is_pending:
                continue
        filtered.append(row)

    filtered = apply_filters(filtered, active_custom_filters)
    filtered = apply_sort(filtered, sort_field, sort_ascending)

    render_expert_analysis_control_bar(market, results)
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

        # Flag color is prepended to the ticker symbol itself (per request:
        # "flag ticker symbol within the ticker column"), in addition to the
        # separate Flag column above -- so it's visible at a glance without
        # needing that column shown/scrolled into view.
        # The flag emoji also carries a tooltip showing the reason.
        def _ticker_with_flag(r):
            link = (f'<a href="{tradingview_url(r["ticker"])}" '
                    f'target="_blank" rel="noopener noreferrer">{r["ticker"]}</a>')
            flag = r.get("flag", "")
            reason = html.escape(r.get("flag_reason", ""))
            emoji = FLAG_EMOJI.get(flag)
            if emoji and reason:
                return f'<span title="{reason}">{emoji}</span> {link}'
            elif emoji:
                return f'{emoji} {link}'
            return link
        raw_df["ticker_link"] = [_ticker_with_flag(r) for r in filtered]

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

        expert_views = load_expert_views()
        def _expert_take_cell(ticker):
            v = expert_views.get(ticker, {})
            verdict = v.get("verdict")
            headline = v.get("headline", "")
            actionable = v.get("actionable_take", "")
            if verdict == "ACCUMULATE":
                badge = "🟢 Accumulate"
            elif verdict == "HOLD":
                badge = "🟡 Hold"
            elif verdict == "CAUTION":
                badge = "🔴 Caution"
            elif verdict in ("FAILED", "PENDING") or "error" in headline.lower() or "pending" in headline.lower():
                badge = "⚠️ Failed (Retry)"
            else:
                badge = "⚪ Pending"
            tooltip = f"{headline}\n\n{actionable}" if headline else "Click 'Retry Failed' in controls above to analyze."
            return with_tooltip(badge, tooltip)

        raw_df["expert_take"] = [_expert_take_cell(r["ticker"]) for r in filtered]

        # Column visibility/order is chosen ONCE via the shared sidebar
        # picker (render_shared_column_picker) and passed in, so US and
        # India always show identical columns in identical order.
        df = raw_df[["ticker_link", "last_close"] + visible_keys].copy()
        df.columns = ["Ticker", "Last"] + [label_by_key[k] for k in visible_keys]
        if "Trend" in df.columns:
            df["Trend"] = df["Trend"].fillna("—")

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
        header_tooltips = {**column_definitions(settings, labels), **custom_column_tooltips(custom_columns)}
        table_html = add_header_tooltips(sticky_header_html(styled), header_tooltips)
        st.markdown(table_html, unsafe_allow_html=True)

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

    render_expert_view_expander(market, filtered, settings)

    st.caption(
        f"Mansfield RS = ((price/{bench} ratio today ÷ SMA of that ratio, n) − 1) × 100. "
        "Positive = outperforming the benchmark's trend, negative = underperforming. "
        "WEMA = weekly EMA, DEMA = daily EMA. "
        f"VStop-W = weekly Volatility Stop (Wilder's ATR stop-and-reverse system, "
        f"length={settings['vstop_length']}, factor={settings['vstop_factor']}) — not independently "
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
watchlists_now = load_watchlists()
settings_now = load_settings()

st.sidebar.title("Stock Watchlist")
sb1, sb2 = st.sidebar.columns(2)
if sb1.button("Refresh Data", type="primary", width="stretch"):
    with st.spinner("Fetching latest prices & updating snapshot..."):
        combined, as_of, per_market = fetch_all_markets(watchlists_now, settings=settings_now)
        save_data_snapshot(as_of, per_market, settings=settings_now)
        token, repo, branch = get_github_config(st.secrets)
        if token and repo:
            ok, msg = push_all_config(token, repo, branch, filenames=["data_snapshot.json"], message=f"Refresh data snapshot via UI ({as_of})")
            if ok:
                st.toast(f"✓ Refreshed {len(combined)} tickers & updated GitHub snapshot!")
            else:
                st.toast(f"✓ Refreshed live prices! (GitHub sync: {msg})")
        else:
            st.toast(f"✓ Refreshed live prices for {len(combined)} tickers!")
        st.session_state.refresh_token += 1
        st.rerun()

if sb2.button("⚙️ Settings", width="stretch"):
    settings_dialog()
render_logout_button()

# Prefer the daily 7 AM ET snapshot (data_snapshot.json, built by
# .github/workflows/data-refresh.yml) over a live yfinance fetch -- much
# faster, and avoids every visitor re-fetching identical data. Only used
# when the user hasn't clicked "Refresh Data" this session (refresh_token
# == 0) and the snapshot actually covers the current watchlist/settings;
# otherwise falls back to the live cached_fetch_all path exactly as before.
using_snapshot = False
if st.session_state.refresh_token == 0:
    snapshot = load_data_snapshot()
    if snapshot_is_usable(snapshot, watchlists_now, settings_now):
        as_of = snapshot["as_of"]
        per_market = snapshot["per_market"]
        using_snapshot = True

if not using_snapshot:
    as_of, per_market = cached_fetch_all(
        st.session_state.refresh_token,
        json.dumps(watchlists_now, sort_keys=True),
        json.dumps(settings_now, sort_keys=True),
    )

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
for _market_rows in per_market.values():
    apply_custom_columns_to_rows(_market_rows, custom_columns_now)
    # Notes/flags change far more often than custom columns (edited
    # ad hoc throughout the day, not just occasionally) -- re-apply live on
    # every run, same reasoning as custom columns above, so a note/flag you
    # just saved shows up immediately even when serving from this morning's
    # snapshot instead of waiting for the next refresh.
    apply_notes_to_rows(_market_rows, ticker_notes_now)

source_label = "daily snapshot" if using_snapshot else "live fetch"
st.sidebar.caption(f"Data as of: {as_of} ({source_label})")
st.sidebar.caption(f"US: {len(per_market.get('US', []))} · India: {len(per_market.get('INDIA', []))}")

shared_visible_keys, shared_label_by_key, shared_sort_field, shared_sort_ascending = render_shared_column_picker(
    ema_col_labels(settings_now)
)

tab_us, tab_india, tab_news, tab_alerts = st.tabs(["US Watchlist", "India Watchlist", "News", "Alert Rules"])

with tab_us:
    render_market_tab(
        "US", per_market.get("US", []), settings_now, shared_visible_keys, shared_label_by_key,
        shared_sort_field, shared_sort_ascending,
    )

with tab_india:
    render_market_tab(
        "INDIA", per_market.get("INDIA", []), settings_now, shared_visible_keys, shared_label_by_key,
        shared_sort_field, shared_sort_ascending,
    )

with tab_news:
    st.subheader("Watchlist news digest")
    st.caption(
        "Major announcements, developments, and stock moves for your watchlist tickers in the "
        "last 24-48 hours, summarized via Gemini (Google Search grounding). Runs once a day at "
        "7:00 AM ET via GitHub Actions and is also sent to Discord — this tab just shows the "
        "same result."
    )
    news_data = load_news_summary()
    if not news_data:
        st.info(
            "No news summary yet. It's generated once a day by the scheduled GitHub Actions "
            "workflow (`news-summary.yml`) — nothing to do here until the first scheduled run, "
            "or trigger it manually from the repo's Actions tab."
        )
    else:
        st.caption(f"As of {news_data.get('as_of', '—')}")
        for market in ("US", "INDIA"):
            entry = news_data.get("markets", {}).get(market)
            if not entry:
                continue
            st.markdown(f"### {MARKET_LABELS.get(market, market)}")
            st.markdown(entry.get("summary", "_No summary available._"))
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

    combined_tickers = watchlists_now.get("US", []) + watchlists_now.get("INDIA", [])
    combined_results = per_market.get("US", []) + per_market.get("INDIA", [])
    filterable_metrics_alert = get_all_filterable_metrics(settings_now)
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

    new_cond = render_condition_builder("dr", metric_names_alert, filterable_metrics_alert, dr_logic)
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
            elif scope_choice == "US watchlist":
                scope_val = "US"
            elif scope_choice == "India watchlist":
                scope_val = "INDIA"
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
            sched_summary = describe_schedule(rule)
            expander_title = f"{name_label} — {scope_label} ({n_conds} condition{'s' if n_conds != 1 else ''}) | {sched_summary}"

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

                st.markdown("**Name & scope**")
                nm_col, sc_col = st.columns([2, 2])
                edit_name = nm_col.text_input("Name", value=rule.get("name", ""), key=f"nm_{rule['id']}")
                scope_label_map = {"ALL": "All watchlist", "US": "US watchlist", "INDIA": "India watchlist"}
                scope_edit_options = ["All watchlist", "US watchlist", "India watchlist"] + combined_tickers
                current_scope_label = scope_label_map.get(rule.get("scope"), rule.get("scope"))
                if current_scope_label not in scope_edit_options:
                    scope_edit_options = [current_scope_label] + scope_edit_options
                edit_scope_label = sc_col.selectbox(
                    "Scope", scope_edit_options, index=scope_edit_options.index(current_scope_label),
                    key=f"sc_{rule['id']}",
                )
                if st.button("Save name/scope", key=f"nmsc_save_{rule['id']}"):
                    if edit_scope_label == "All watchlist":
                        edit_scope_val = "ALL"
                    elif edit_scope_label == "US watchlist":
                        edit_scope_val = "US"
                    elif edit_scope_label == "India watchlist":
                        edit_scope_val = "INDIA"
                    else:
                        edit_scope_val = edit_scope_label
                    rule["name"] = edit_name.strip()
                    rule["scope"] = edit_scope_val
                    save_rules(rules)
                    st.success("Name/scope saved.")
                    st.rerun()

                st.markdown("**Conditions**")
                edit_conds = rule.get("conditions", [])
                if edit_conds:
                    ec_remove_idx = None
                    for i, cond in enumerate(edit_conds):
                        ec1, ec2 = st.columns([5, 1])
                        prefix = "" if i == 0 else f"{cond.get('logic', 'AND')}  "
                        ec1.write(f"{prefix}{describe_filter(cond, metric_labels_alert)}")
                        if ec2.button("Remove", key=f"ec_rm_{rule['id']}_{i}"):
                            ec_remove_idx = i
                    if ec_remove_idx is not None:
                        edit_conds.pop(ec_remove_idx)
                        rule["conditions"] = edit_conds
                        save_rules(rules)
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
                )
                if new_edit_cond:
                    edit_conds.append(new_edit_cond)
                    rule["conditions"] = edit_conds
                    save_rules(rules)
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

                if st.button("Save schedule", key=f"es_save_{rule['id']}"):
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
                    st.success("Schedule saved.")
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
            file_labels = {label: fname for fname, label in SYNCABLE_FILES}
            selected_labels = st.multiselect(
                "Files to push", options=list(file_labels.keys()),
                default=list(file_labels.keys()), key="gh_push_files",
            )
            st.caption(
                "Pushes everything selected as one combined commit -- important on Streamlit Cloud, "
                "which auto-redeploys the instant any commit lands, so separate per-file commits risk "
                "the redeploy interrupting the push before later files go through."
            )
            if st.button("Push selected to GitHub", width="stretch"):
                if not selected_labels:
                    st.warning("Select at least one file.")
                else:
                    targets = [file_labels[lbl] for lbl in selected_labels]
                    ok, msg = push_all_config(gh_token, gh_repo, gh_branch, filenames=targets)
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.caption("Your app may restart shortly since Streamlit Cloud watches this repo.")
