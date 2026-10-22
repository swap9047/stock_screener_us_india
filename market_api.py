"""The screener's numbers for other apps: a catalog of the metrics another app may read, the
public part of a row, and a live computation for any ticker.

Two consumers, one set of rules:
  - publish_market.py writes rows into the knowledge graph's database (Supabase), where the
    research library's Q&A and Claude chat read them;
  - api/market.py (deployed on Vercel) computes a ticker the database doesn't hold yet.

Only fields named in METRIC_GROUPS ever leave this repo -- a whitelist, so a field added to the
row dict later stays private until someone adds it here on purpose. Never add the watchlist a
ticker sits in (`market`), notes, flags, `interested` or custom columns: those say what is held.

Computation is stock_data.fetch_snapshot itself, the same function the dashboard's snapshot comes
from, so a number here can never drift from the one on screen. Nothing in this module imports
Streamlit or the AI pipelines, so the Vercel function stays small.
"""

import time
from datetime import datetime, timezone

import stock_data

# group -> (title, [(field, label, unit, description)]). Labels follow stock_data.
# get_filterable_metrics (the moving-average ones are filled in from the settings by catalog()).
# Units: "price" is in the listing's currency, "%" a percent, "pp" percentage points, "x" a
# multiple, "" a label or count.
METRIC_GROUPS = {
    "price": ("Price", [
        ("last_close", "Last Close", "price", "Most recent daily close."),
        ("pct_change_1d", "% Chg", "%", "1-day close-to-close change."),
        ("data_end", "Data Thru", "", "Date of the latest daily bar."),
    ]),
    "returns": ("Returns", [
        ("perf_1m", "Perf 1M %", "%", "Price return over ~1 month (22 sessions)."),
        ("perf_3m", "Perf 3M %", "%", "Price return over ~3 months (63 sessions)."),
        ("perf_6m", "Perf 6M %", "%", "Price return over ~6 months (126 sessions)."),
        ("perf_1y", "Perf 1Y %", "%", "Price return over ~1 year (252 sessions)."),
        ("perf_3y", "Perf 3Y %", "%", "Price return over ~3 years (756 sessions)."),
        ("rel_ret_1w_index", "1W Ret vs Index", "pp", "1-week return minus its index's (Nifty 500 for India, S&P 500 for US)."),
        ("rel_ret_1m_index", "1M Ret vs Index", "pp", "1-month return minus its index's."),
        ("rel_ret_6m_index", "6M Ret vs Index", "pp", "6-month return minus its index's."),
    ]),
    "moving_averages": ("Moving averages", [
        ("ema10", "{w_fast} WEMA", "price", "Weekly EMA, fast period."),
        ("ema20", "{w_mid} WEMA", "price", "Weekly EMA, medium period."),
        ("ema40", "{w_slow} WEMA", "price", "Weekly EMA, slow period (the one Trend and Tech Uptrend use)."),
        ("ema10_daily", "{d_fast} DSMA", "price", "Daily SMA, fast period."),
        ("ema50", "{d_mid} DSMA", "price", "Daily SMA, medium period."),
        ("ema200", "{d_slow} DSMA", "price", "Daily SMA, slow period."),
        ("below_10", "Below {w_fast} WEMA", "", "True when the close is below the fast weekly EMA."),
        ("below_20", "Below {w_mid} WEMA", "", "True when the close is below the medium weekly EMA."),
        ("below_40", "Below {w_slow} WEMA", "", "True when the close is below the slow weekly EMA."),
        ("below_50", "Below {d_mid} DSMA", "", "True when the close is below the medium daily SMA."),
        ("below_200", "Below {d_slow} DSMA", "", "True when the close is below the slow daily SMA."),
        ("crossed_above_40", "Crossed above {w_slow} WEMA", "", "Crossed above the slow weekly EMA on the latest bar."),
        ("crossed_below_40", "Crossed below {w_slow} WEMA", "", "Crossed below the slow weekly EMA on the latest bar."),
        ("crossed_above_200", "Crossed above {d_slow} DSMA", "", "Crossed above the slow daily SMA on the latest bar."),
        ("crossed_below_200", "Crossed below {d_slow} DSMA", "", "Crossed below the slow daily SMA on the latest bar."),
        ("gc_weeks_10_30", "10/30 W Golden Cross (weeks ago)", "", "Weeks since the 10-week EMA crossed above the 30-week EMA."),
    ]),
    "momentum": ("Momentum", [
        ("rsi14_daily", "RSI-D", "", "Daily RSI (14). 30 or less oversold, 70 or more overbought."),
        ("rsi14_weekly", "RSI-W", "", "Weekly RSI (14)."),
        ("rsi14_monthly", "RSI-M", "", "Monthly RSI (14)."),
        ("rsi12_monthly", "RSI-M (12)", "", "Monthly RSI at period 12."),
        ("adx_weekly_14", "ADX-W", "", "Weekly ADX (14): trend strength, not direction. 25+ trending, under 20 chop."),
        ("adx_monthly_12", "ADX-M", "", "Monthly ADX (12)."),
    ]),
    "relative_strength": ("Relative strength", [
        ("rs_daily", "RS-D", "", "Mansfield relative strength vs its index, daily. Positive = outperforming."),
        ("rs_weekly", "RS-W", "", "Mansfield relative strength vs its index, weekly."),
        ("rs_monthly", "RS-M", "", "Mansfield relative strength vs its index, monthly."),
        ("index_name", "Index", "", "The index it is measured against."),
    ]),
    "trend": ("Trend signals", [
        ("trend", "Trend", "", "Strong Uptrend / Uptrend / Mixed / Downtrend / Strong Downtrend, from price vs the slow WEMA, its slope, fast vs slow WEMA and weekly RS."),
        ("tech_uptrend", "Tech Uptrend", "", "1 when the close is above an upward weekly VStop (up for 3+ weeks) and the slow WEMA, on reasonable volume."),
        ("ta_rules", "TA Rules", "", "The weekly EMA flowchart on the last completed week: Maintain/Add, Momentum Fading, Be Cautious, Exit, Bullish Signal or Wait/Watch."),
        ("expert_take", "Expert Take", "", "Accumulate, Hold or Caution, decided in code from Trend, Tech Uptrend, TA Rules and Sentiment."),
        ("vstop_weekly", "VStop-W", "price", "Weekly volatility stop (ATR stop-and-reverse)."),
        ("vstop_weekly_direction", "VStop Dir", "", "Up or Down."),
        ("vstop_weekly_weeks_since_change", "VStop Weeks Ago", "", "Weeks since the weekly VStop last flipped."),
    ]),
    "volume": ("Volume", [
        ("avg_volume_10d", "Vol 10D", "", "Average daily volume, last 10 sessions."),
        ("avg_volume_20d", "Vol 20D", "", "Average daily volume, last 20 sessions."),
        ("avg_volume_100d", "Vol 100D", "", "Average daily volume, last 100 sessions."),
        ("volume_trend", "Vol Trend", "", "Exploding / In-line / Declining: 10-day vs 100-day median volume."),
        ("net_volume_10d_dir", "Net Vol 10D", "", "Positive when more volume traded on up days than down days over 10 sessions."),
        ("net_volume_10d_ratio", "Net Vol 10D ratio", "%", "Net up-minus-down volume as a percent of the 10-day total."),
    ]),
    "range": ("Range and breakouts", [
        ("week52_high", "52W High", "price", "Trailing 52-week intraday high."),
        ("week52_low", "52W Low", "price", "Trailing 52-week intraday low."),
        ("week52_distance", "52WH Distance", "%", "Percent below the 52-week high (0 = at the high)."),
        ("week26_distance", "26WH Distance", "%", "Percent below the 26-week high."),
        ("week52_high_age", "52W High Age", "", "Sessions since the 52-week high was set."),
        ("high_5y", "5Y High", "price", "Highest intraday high in 5 years."),
        ("high_5y_distance", "5Y High Distance", "%", "Percent below the 5-year high."),
        ("breakout_window", "Breakout Window", "", "Sessions back to the level price is now testing (0 = nothing above, an all-time high)."),
        ("overhead_supply", "Overhead Supply", "%", "Share of the last year's volume traded above today's price."),
    ]),
    "valuation": ("Valuation", [
        ("trailing_pe", "P/E (TTM)", "x", "Trailing 12-month price to earnings (Yahoo Finance)."),
        ("forward_pe", "P/E (Fwd)", "x", "Forward price to earnings on analyst estimates (Yahoo Finance)."),
        ("pb_ratio", "P/B", "x", "Price to book (Yahoo Finance)."),
        ("ev_ebitda", "EV/EBITDA", "x", "Enterprise value to EBITDA (Yahoo Finance)."),
        ("p_cashflow", "P/Cashflow", "x", "Market cap / operating cash flow."),
    ]),
    "quality_growth": ("Quality and growth", [
        ("roe", "ROE %", "%", "Return on equity, TTM (Yahoo Finance)."),
        ("roce", "ROCE %", "%", "Return on capital employed, latest fiscal year."),
        ("cfo_op_5yr", "CFO/OP 5Y", "x", "Operating cash flow / operating income over up to 5 years (above 1 = good cash conversion)."),
        ("qtr_profit_growth", "Qtr Profit Growth %", "%", "Latest quarter's net income vs the same quarter a year ago."),
        ("qtr_eps_growth", "Qtr EPS Growth %", "%", "Latest quarter's diluted EPS vs a year ago (lower than profit growth = dilution)."),
        ("qtr_revenue_growth", "Qtr Revenue Growth %", "%", "Latest quarter's revenue vs a year ago."),
        ("reported_qtr", "Reported Qtr", "", "The quarter those growth figures are for."),
    ]),
    # Not row fields: the dashboard's own hover text for each signal (glossary.py), computed by
    # explanations() when a record is built, so another app can say WHY a signal reads as it does.
    "explain": ("Why the signals read as they do", [
        ("trend_why", "Trend: why", "", "Each condition behind Trend -- passed (✓), failed (✗) or too close to call (–) -- with the numbers, then the test for Strong."),
        ("tech_uptrend_why", "Tech Uptrend: why", "", "Each requirement of Tech Uptrend, passed or failed, with the numbers."),
        ("ta_rules_why", "TA Rules: why", "", "The path taken through the weekly EMA flowchart on the last completed week, with the numbers each step was decided on."),
        ("volume_trend_why", "Vol Trend: why", "", "The 10-day vs 100-day median volume ratio against the Exploding and Declining thresholds."),
        ("expert_take_why", "Expert Take: why", "", "What decided the Expert Take verdict (Trend, Tech Uptrend, TA Rules, Sentiment), and a news event that lowered it, if any."),
    ]),
}
# Catalog fields whose glossary entry (glossary.column_definitions, keyed by column label) is under
# another label than the metric's.
GLOSSARY_LABELS = {"last_close": "Last"}

# The screener's AI write-ups, from expert_views.json and fundamentals.json. Published for the
# screener's own tickers only (the library-only ones have no write-up).
AI_EXPERT_FIELDS = ("headline", "technical_summary", "catalyst_summary", "actionable_take", "verdict", "as_of")
AI_SENTIMENT_FIELDS = ("sentiment", "sentiment_drivers", "reasoning", "earnings_summary", "future_guidance",
                       "outlook_tone", "outlook_quote", "guidance_change", "analyst_action", "analyst_firm",
                       "profit_yoy_pct", "revenue_yoy_pct", "results_vs_estimate", "earnings_report_date", "as_of")
AI_GROUP = "ai_views"

PUBLIC_FIELDS = tuple(f for _, fields in METRIC_GROUPS.values() for f, *_ in fields)
GROUP_NAMES = tuple(METRIC_GROUPS) + (AI_GROUP,)


def catalog(settings=None):
    """[{field, group, label, unit, description, ord}] -- the menu a model picks from, with the
    moving-average periods taken from the screener's settings. A metric's description is the
    dashboard's own glossary entry (the column's header tooltip) where it has one, so a number
    means the same thing here as on screen; the short text in METRIC_GROUPS covers the rest."""
    import glossary
    settings = settings or stock_data.load_settings()
    w_fast, w_mid, w_slow = settings["ema_weekly"]
    d_fast, d_mid, d_slow = settings["ema_daily"]
    names = {"w_fast": w_fast, "w_mid": w_mid, "w_slow": w_slow, "d_fast": d_fast, "d_mid": d_mid, "d_slow": d_slow}
    defs = glossary.column_definitions(settings, glossary.ema_col_labels(settings))
    out = []
    for group, (_, fields) in METRIC_GROUPS.items():
        for field, label, unit, description in fields:
            label = label.format(**names)
            out.append({"field": field, "group": group, "label": label, "unit": unit,
                        "description": defs.get(GLOSSARY_LABELS.get(field, label)) or description, "ord": len(out)})
    out.append({"field": AI_GROUP, "group": AI_GROUP, "label": "Expert Take and Sentiment write-ups", "unit": "",
                "description": "The screener's nightly AI write-ups: Expert Take (headline, technical and catalyst "
                               "summaries, trade plan) and Sentiment (earnings, guidance, outlook, analyst actions). "
                               "Screener tickers only.", "ord": len(out)})
    return out


def listing_of(ticker):
    """(exchange, currency) from the ticker's suffix: .NS NSE, .BO BSE, otherwise a US listing."""
    t = (ticker or "").upper()
    if t.endswith(".NS"):
        return "NSE", "INR"
    if t.endswith(".BO"):
        return "BSE", "INR"
    return "US", "USD"


def _clean(value):
    """JSON-safe: NaN/inf become None, numpy scalars plain Python, floats rounded."""
    if hasattr(value, "item") and not isinstance(value, (list, dict, str)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, 4)
    return value


def public_metrics(row):
    """{field: value} for the catalog's fields only."""
    return {f: _clean(row.get(f)) for f in PUBLIC_FIELDS if f in row}


def ai_views(expert_view, fundamental):
    """{"expert": {...}, "sentiment": {...}} from the two write-up files, only the listed fields,
    or {} when the ticker has neither."""
    out = {}
    if isinstance(expert_view, dict) and expert_view:
        out["expert"] = {f: expert_view.get(f) for f in AI_EXPERT_FIELDS if expert_view.get(f) not in (None, "")}
    if isinstance(fundamental, dict) and fundamental:
        out["sentiment"] = {f: fundamental.get(f) for f in AI_SENTIMENT_FIELDS if fundamental.get(f) not in (None, "")}
    return {k: v for k, v in out.items() if v}


def explanations(row, settings=None, expert_view=None):
    """{field: text} for the explain group: the dashboard's hover text for Trend, Tech Uptrend, TA Rules
    and Vol Trend (glossary.py -- the same words as on screen), and what decided Expert Take
    (expert_views.expert_take_for_row: its reasons, and the news step when it lowered the verdict)."""
    import glossary
    from expert_views import expert_take_for_row
    settings = settings or stock_data.load_settings()
    labels = glossary.ema_col_labels(settings)
    out = {}
    if "trend" in row:
        out["trend_why"] = glossary.trend_tooltip(row, labels)
    if "tech_uptrend" in row:
        out["tech_uptrend_why"] = glossary.tech_uptrend_tooltip(row, settings, labels)
    if "ta_rules" in row:
        out["ta_rules_why"] = glossary.ta_rules_tooltip(row)
    if "volume_trend" in row:
        out["volume_trend_why"] = glossary.vol_trend_tooltip(row, settings)
    if row.get("trend"):
        take = expert_take_for_row(row, expert_view or {})
        why = f"{take['verdict'].title()}: " + "; ".join(take["reasons"])
        risk = take.get("news_risk") or {}
        if take.get("news_lowered") or take.get("news_noted"):
            why += (f". {'Lowered one step' if take.get('news_lowered') else 'Noted (already Caution)'} for news: "
                    f"{risk.get('category') or 'a material negative event'} ({risk.get('date') or 'undated'})"
                    + (f' - "{risk["quote"]}"' if risk.get("quote") else ""))
        out["expert_take_why"] = why
    return out


def public_record(row, source, expert_view=None, fundamental=None, as_of=None, settings=None):
    """The record another app gets for one row: identity, provenance, metrics (with the explain
    group) and AI views."""
    ticker = row.get("ticker")
    exchange, currency = listing_of(ticker)
    return {"ticker": ticker, "company_name": row.get("company_name") or ticker, "exchange": exchange,
            "currency": currency, "source": source, "data_end": row.get("data_end"),
            "as_of": as_of or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": public_metrics(row) | explanations(row, settings, expert_view),
            "ai": ai_views(expert_view, fundamental)}


def select_groups(record, groups=None):
    """The record with only the asked groups' metrics: {group: {label: value}}, plus ai_views when
    asked. Unknown group names are ignored; none asked means every group."""
    wanted = [g for g in (groups or GROUP_NAMES) if g in GROUP_NAMES] or list(GROUP_NAMES)
    metrics = record.get("metrics") or {}
    out = {}
    for group in wanted:
        if group == AI_GROUP:
            if record.get("ai"):
                out[AI_GROUP] = record["ai"]
            continue
        values = {f: metrics.get(f) for f, *_ in METRIC_GROUPS[group][1] if metrics.get(f) is not None}
        if values:
            out[group] = values
    return out


def benchmark_for(ticker, ticker_index=None):
    """The benchmark RS and relative returns are measured against: the ticker's recorded index
    when the screener knows it, else Nifty 500 for an Indian listing and the S&P 500 otherwise --
    what detect_ticker_index would decide, without its Yahoo lookup.

    ticker_index is load_ticker_index()'s {ticker: {"index", "benchmark"}}. The first library run
    crashed (TypeError) because this read each entry as a bare index name."""
    entry = (ticker_index or {}).get(ticker)
    index_name = entry.get("index") if isinstance(entry, dict) else entry
    if isinstance(index_name, str) and index_name in stock_data.INDEX_DEFINITIONS:
        return index_name, stock_data.INDEX_DEFINITIONS[index_name]
    name = "Nifty 500" if listing_of(ticker)[1] == "INR" else "S&P 500"
    return name, stock_data.INDEX_DEFINITIONS[name]


# A ticker missing from a bulk download is asked for once more, after this pause. Yahoo leaves single
# symbols out of a batch now and then (a rate-limited or slow symbol: on Vercel, an NSE ticker came back
# empty while the S&P one beside it, the same minute, was complete), and fetch_snapshot skips an empty
# ticker without a word.
RETRY_MISSING_WAIT_S = 3


def compute_rows(tickers, settings=None, ticker_index=None, retry_wait=RETRY_MISSING_WAIT_S):
    """{ticker: row} computed live by fetch_snapshot, one call per benchmark (it cannot mix
    benchmarks in one call). A ticker Yahoo returned nothing for is tried once more (retry_wait
    seconds later, None = no retry), then simply absent; a benchmark group whose download fails
    raises, as fetch_snapshot does."""
    settings = settings or stock_data.load_settings()
    wanted = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
    rows = _compute_once(wanted, settings, ticker_index)
    missing = [t for t in wanted if t not in rows]
    if missing and retry_wait is not None:
        time.sleep(retry_wait)
        rows.update(_compute_once(missing, settings, ticker_index))
    return rows


def _compute_once(tickers, settings, ticker_index):
    groups = {}
    for t in tickers:
        groups.setdefault(benchmark_for(t, ticker_index), []).append(t)
    rows = {}
    for (index_name, benchmark), members in groups.items():
        results, _as_of = stock_data.fetch_snapshot(members, benchmark=benchmark, settings=settings)
        for row in results:
            row.setdefault("index_name", index_name)
            row["expert_take"] = columns_verdict(row)
            rows[row["ticker"]] = row
    return rows


def columns_verdict(row):
    """Expert Take as the dashboard decides it from the columns (Trend, Tech Uptrend, TA Rules), for a row
    that has no Sentiment or nightly write-up -- a library ticker the screener doesn't track. Same
    function as the dashboard's (expert_views.expert_take_for_row), with no news step to apply."""
    from expert_views import expert_take_for_row
    return expert_take_for_row(row, {})["verdict"].title()
