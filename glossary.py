"""The screener's own words for its columns: the glossary (each column's definition, the header
tooltip) and the "why" behind the computed signals (Trend, TA Rules, Vol Trend, Tech Uptrend: which
condition passed or failed, with the numbers).

Kept out of app.py so the dashboard and market_api.py (what the research library's Q&A and Claude
chat read) explain a number in the same words -- one copy, nothing to drift. Pure functions of a
row and the settings: no Streamlit, no data files. app.py imports everything here under the same
names it always had.
"""


def ema_col_labels(settings):
    """Column-header labels for the 6 SMA slots, reflecting the currently
    configured periods (e.g. {'w_fast': '10 WEMA', ...})."""
    wf, wm, ws = settings["ema_weekly"]
    df_, dm, ds = settings["ema_daily"]
    return {
        "w_fast": f"{wf} WEMA", "w_mid": f"{wm} WEMA", "w_slow": f"{ws} WEMA",
        "d_fast": f"{df_} DSMA", "d_mid": f"{dm} DSMA", "d_slow": f"{ds} DSMA",
    }


def column_definitions(settings, labels):
    """label -> plain-language definition, for the header info-icon hover
    tooltip. Bench/period/threshold numbers are pulled from `settings` so
    the tooltip always reflects your current configuration, not defaults."""
    from expert_views import EXPERT_NEWS_WINDOW_DAYS
    bench_note = "the ticker's own index (S&P 500 → SPY, Nifty 500 → ^CRSLDX; see the Index column)"
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
            "Strong Uptrend / Uptrend / Mixed / Downtrend / Strong Downtrend. Uptrend requires ALL of: price "
            f"above slow WEMA, slow WEMA slope rising over {settings.get('trend_slope_lookback', 3)} weeks, fast "
            "WEMA above slow WEMA, and weekly RS positive (when available); Downtrend requires all four bearish; "
            "anything in between is Mixed (never Strong). A condition too close to call doesn't vote (RS "
            f"within ±{settings.get('trend_rs_neutral', 1.0):g}, 10W within "
            f"{settings.get('trend_ma_neutral_pct', 0.5):g}% of 40W, 40W slope within "
            f"{settings.get('trend_slope_neutral_pct', 0.05):g}%/week); Uptrend/Downtrend still need 3 votes. "
            "Strong additionally needs price within "
            f"{settings.get('trend_near_high_low_pct', 0.10) * 100:.0f}% of the 52W high/low AND the 10D median "
            f"volume ≥ {settings.get('trend_volume_ratio', 1.0)}× the 100D median. Hover a cell for the "
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
            "one stock can show a 190-day window but ~30% supply because ten more levels sit above, "
            "while another has twelve levels above yet ~0% because all of them predate the year."
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
        "Data Thru": "Most recent date with price data for this ticker. Shown in red once 2+ weekday sessions have passed with no newer bar.",
        "Net Vol 10D": (
            "Sums each of the last 10 trading days' volume as UP-volume (close higher than the "
            "prior day) or DOWN-volume (close lower). Positive means more volume traded on up "
            "days than down days over that window, Negative the reverse. Hover a cell for the "
            "exact net/total ratio."
        ),
        "Vol Trend": (
            f"Exploding: the 10D median daily volume ≥ {settings.get('volume_explode_ratio', 1.4)}× the 100D "
            f"median. Declining: ≤ {settings.get('volume_decline_ratio', 0.7)}×. Otherwise In-line. Medians, so "
            "a results-day spike doesn't skew the baseline for months. Hover a cell for the actual ratio."
        ),
        "TA Rules": (
            "TheWrap flowchart, on the last completed weekly close. If the three WEMAs are within "
            f"{settings.get('ta_converge_pct', 3.0):g}% of each other (converging): broken support → Exit, "
            "broken resistance → Bullish Signal, else Wait/Watch. Otherwise: broken slow WEMA → Exit, mid → "
            "Be Cautious, fast → Momentum Fading, none → Maintain/Add. Broken = the close is more than "
            f"{settings.get('ta_break_pct', 3.0):g}% past the line. Support/resistance = price zones the stock "
            f"turned at {settings.get('ta_sr_min_touches', 2)}+ times (weekly wicks, "
            f"{settings.get('ta_sr_reaction_pct', 8.0):g}%+ move away). Hover a cell for the path taken; "
            "the flowchart itself is under \"TA Rules flowchart\" in the sidebar."
        ),
        "Tech Uptrend": (
            "Yes only if ALL of: close > weekly VStop, VStop pointing Up for more than "
            f"{settings.get('tech_uptrend_min_vstop_weeks', 3)} weeks, close > slow WEMA, and the 10D median "
            f"volume > {settings.get('tech_uptrend_volume_ratio', 0.3)}× the 100D median. Hover a cell for the "
            "per-condition breakdown."
        ),
        "Vol 10D": "Average daily share volume over the last 10 trading days.",
        "Vol 100D": "Average daily share volume over the last 100 trading days.",
        "Flag": (
            "Your own marker, set via the sidebar 'Ticker Notes' panel (Red/Yellow/Green/Blue), also shown "
            "next to the ticker symbol, where it takes the place of the Expert Take dot. Never set automatically."
        ),
        "Expert Take": (
            "ACCUMULATE, HOLD or CAUTION, decided live from the other columns. Accumulate: Trend up or Tech "
            "Uptrend Yes, plus TA Rules Maintain/Add or Bullish Signal and Sentiment not Bearish -- or a TA Bullish Signal "
            "(a breakout from a converging base) with Trend not down and Sentiment not Bearish. Caution: 2+ "
            "points, where TA Exit counts 2 and TA Be Cautious, a Downtrend and a Bearish Sentiment 1 each. "
            "Otherwise Hold. The nightly AI explains it and writes a trade plan, and reads the last "
            f"{EXPERT_NEWS_WINDOW_DAYS} days of material news: a dated negative event (fraud, a regulator's "
            "action, a lost contract, dilution...) lowers the verdict one step, marked ⚑. News never raises it. "
            "Its colour is the dot next to the ticker (🟢 Accumulate, 🟡 Hold, 🔴 Caution, ⚪ Pending) when you "
            "haven't set a Flag. Hover a cell for what decided it and the trade plan."
        ),
        "Expert News?": (
            f"Whether the Expert Take write-up had any news behind it: the last {EXPERT_NEWS_WINDOW_DAYS} days' "
            "material news, or this quarter's checked results, guidance and analyst actions from the Sentiment "
            "job. \"No\" means neither was found: the verdict is the columns' alone."
        ),
        "10/30 W Golden Cross (weeks ago)": "Weeks since the 10-week EMA crossed above the 30-week EMA. Lower numbers mean a more recent bullish cross.",
        "Notes": "Your free-text note for this ticker, set via the sidebar 'Ticker Notes' panel. Hover/tap a truncated note to see the full text.",
        "Interested": "Whether you ticked this ticker as Interested in the watchlist editor.",
        "Sentiment": (
            "Forward-looking fundamental sentiment (Positive / Neutral / Negative), decided by a fixed rule from "
            "facts the AI extracts. Mainly, by weight: guidance raised/lowered (15, outweighs everything), "
            "management's quoted outlook improving/cautious (6), guidance above/below analysts' consensus as the "
            "news states it (3), and upgrades/downgrades by named firms (3) -- each decides on its own. "
            "The quarter just reported weighs much less: with forward signals present it only breaks a tie "
            "(profit up/down more than 15% YoY, a beat/miss) and never outvotes them -- strong growth with a "
            "cautious outlook reads Negative. With no forward signal, profit up more than 25% YoY reads Positive "
            "and down more than 20% Negative. The search reaches back to each company's last results. The tag shows the evidence, forward signals first (e.g. Upgrade · Outlook ↑ · Profit +22%); "
            "hover for what decided it. "
            "'Unknown' means the view is stale, predates a confirmed earnings report, or had no hard evidence to "
            "stand on -- not that sentiment is neutral."
        ),
        "Stage": (
            "TheWrap Stage Analysis: the current market stage -- S1 Accumulation (sideways, often at the bottom of "
            "a downtrend), S2 Advancing (higher highs and higher lows), S3 Distribution (momentum slows, sideways "
            "at the top), S4 Decline (lower highs and lower lows). Read from the indicator's CSV export, uploaded in "
            "the sidebar's 'Stage Analysis upload': the screener does not compute it. 'Unknown' means the indicator "
            "had too little history. Hover a cell for the previous stage, how long the stage has run and how "
            "well-formed it is."
        ),
        "Stage Mom": (
            "The indicator's momentum read for the stage (Strong ↑, Recovering ↑, Weakening ↓, Weak ↓), as exported. "
            "Not computed here."
        ),
        "CE": "The indicator's Cheat Entry flag (Yes / No), as exported. Not computed here.",
        "PEAD": (
            "The indicator's post-earnings announcement drift read (Strong / Mod = moderate / Weak / No), as "
            "exported. Not computed here."
        ),
        "Prev Stage": "The stage before the current one, from the same upload as Stage.",
        "Stage Conf": (
            "How well-formed the current stage is: New (just forming), Developing (gaining definition), Established "
            "(has persisted long enough to be reliable), Mature (long-standing). Rises with the periods in Stage Periods."
        ),
        "Stage Periods": (
            "How many periods (bars of the timeframe the indicator was exported on) the current stage has lasted. "
            "Blank when the stage is Unknown."
        ),
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
        "ROCE %": "Return on Capital Employed: Operating Income / (Stockholders Equity + Long-term Debt) as a percentage, all from the same (most recent) fiscal year in yfinance's annual statements. Long-term debt Yahoo leaves blank that year counts as 0. Blank when equity or that year's operating income is missing, or capital employed is zero or negative.",
        "Reported Qtr": "The most recent quarter for which the company reported earnings and revenue growth, mapped to the local financial year (Yahoo Finance).",
        "Perf 1W %": "Price return over the past week (5 trading days).",
        "Perf YTD %": "Price return so far this calendar year: the latest close against the last close of the previous year.",
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
        "1M Ret vs Index": "Stock's 1-month return minus its own index benchmark's, in percentage points — Nifty 500 (^CRSLDX) for Indian listings, S&P 500 (SPY) for US listings; see the Index column. Positive = outperforming. Measured over a date-aligned calendar, so both sides span exactly the same sessions.",
        "6M Ret vs Index": "Stock's 6-month return minus its own index benchmark's (Nifty 500 for Indian listings, S&P 500 for US), in percentage points. Positive = outperforming.",
        "1D Ret vs Index": "Stock's 1-day return minus its own index benchmark's (Nifty 500 for Indian listings, S&P 500 for US), in percentage points, over the latest session both traded. Positive = outperforming.",
        "1W Ret vs Index": "Stock's 1-week return (last ~5 sessions) minus its own index benchmark's (Nifty 500 for Indian listings, S&P 500 for US), in percentage points. Positive = outperforming.",
        "PAT Growth TTM %": "Net income over the trailing four quarters versus the four before that — needs eight quarters of income-statement history. CURRENTLY BLANK FOR EVERY TICKER: yfinance returns only about five quarters, so the comparison cannot be made. Use Qtr Profit Growth % (single quarter, year-on-year) instead; this column populates automatically if a deeper data source is ever wired in.",
        "Revenue Growth TTM %": "Total revenue over the trailing four quarters versus the four before that — needs eight quarters of income-statement history. CURRENTLY BLANK FOR EVERY TICKER: yfinance returns only about five quarters. Use Qtr Revenue Growth % instead; this column populates automatically if a deeper data source is ever wired in.",
    }
    return defs


def _mark(passed):
    """✓/✗/n·a marker for a tri-state condition (True/False/None -- None
    means the condition wasn't evaluable, e.g. RS unavailable)."""
    if passed is None:
        return "n/a"
    return "✓" if passed else "✗"


def trend_tooltip(row, labels):
    """Builds the hover-tooltip text for a Trend cell: which of the 4 hard
    requirements passed/failed, plus the Strong criteria -- so you can see
    at a glance which condition is blocking a better/worse read."""
    detail = row.get("trend_detail")
    if not detail:
        return "Not enough weekly history yet to compute Trend."
    w_slow = labels["w_slow"]
    w_fast = labels["w_fast"]
    # "–" marks a condition inside its neutral band: too close to call, so it
    # does not vote (stock_data.compute_trend). Older snapshots lack the keys.
    def _vote(ok, neutral):
        return "–" if neutral else _mark(ok)

    lines = ["All voting ✓ → Uptrend · all ✗ → Downtrend · otherwise Mixed",
             "(– = too close to call, doesn't vote; needs 3 votes):"]
    lines.append(f"{_mark(detail['price_above_ma'])} Price > {w_slow} ({detail['last_close']:.1f} vs {detail['last_ma']:.1f})")
    lines.append(f"{_vote(detail['slope_rising'], detail.get('slope_neutral'))} {w_slow} slope rising "
                 f"({detail['slope']:+.3f}/wk)")
    if detail.get("ema_aligned") is not None:
        lines.append(f"{_vote(detail['ema_aligned'], detail.get('ma_neutral'))} {w_fast} > {w_slow} "
                     f"({detail['ema_fast']:.1f} vs {detail['last_ma']:.1f})")
    if detail.get("rs_positive") is not None:
        lines.append(f"{_vote(detail['rs_positive'], detail.get('rs_neutral'))} Weekly RS positive "
                     f"({detail['rs_weekly']:+.1f})")
    lines.append(f"→ {detail['direction']}")
    if detail["direction"] == "Mixed":
        lines.append("")
        lines.append("(Mixed is never Strong: the conditions above disagree.)")
        return "\n".join(lines)
    lines.append("")
    lines.append("Strong also needs BOTH:")
    ref_price = detail["week52_high"] if detail["direction"] == "Uptrend" else detail["week52_low"]
    ref_label = "52W high" if detail["direction"] == "Uptrend" else "52W low"
    pct_label = f"{detail['near_high_low_pct'] * 100:.0f}%"
    if ref_price is not None:
        lines.append(f"{_mark(detail['near_high_low_pass'])} Within {pct_label} of {ref_label} ({detail['last_close']:.1f} vs {ref_price:.1f})")
    else:
        lines.append(f"n/a Within {pct_label} of {ref_label} (no 52W data)")
    v10 = detail.get("volume_10d", detail.get("avg_volume_10d"))
    v100 = detail.get("volume_100d", detail.get("avg_volume_100d"))
    basis = "median" if detail.get("volume_basis") == "median" else "avg"
    if v10 is not None and v100 is not None:
        ratio = v10 / v100 if v100 else None
        ratio_str = f"{ratio:.2f}×" if ratio is not None else "n/a"
        lines.append(f"{_mark(detail['volume_rising'])} 10D {basis} volume ≥ {detail['volume_ratio']}× 100D ({ratio_str})")
    else:
        lines.append(f"n/a 10D {basis} volume ≥ {detail['volume_ratio']}× 100D (no volume data)")
    lines.append(f"→ {'Strong' if detail['strong'] else 'Not Strong'}")
    return "\n".join(lines)


def ta_rules_tooltip(row):
    """Hover text for a TA Rules cell: the path taken through TheWrap's
    flowchart, with the numbers each node was decided on."""
    d = row.get("ta_rules_detail")
    if not d:
        return "Not enough completed weekly history yet (needs the slow WEMA)."
    fast, mid, slow = d["periods"]
    buf = d["break_pct"]
    lines = [f"Week ending {d.get('week', '?')} · weekly close {d['close']:,.2f}"]
    lines.append(
        f"EMAs converging? spread {d['spread_pct']:.1f}% of the {slow}W "
        f"({'≤' if d['converging'] else '>'} {d['converge_pct']:g}%) → {'Yes' if d['converging'] else 'No'}"
    )
    decided = d.get("decided_by") or {}

    def _zone(z):
        span = f"{z['low']:,.2f}" if z["low"] == z["high"] else f"{z['low']:,.2f}–{z['high']:,.2f}"
        return f"{span} ({z['touches']} touches)"

    if d["converging"]:
        # stock_data picks `support` as the highest zone starting at or below
        # the close and `resistance` as the lowest zone ending at or above it,
        # so a close INSIDE a zone made both the same zone and the tooltip
        # listed it twice, once as each. And the zone that decided the verdict
        # usually sits on the far side of the close by then (a broken ceiling
        # is below price), so it was listed again as "Support" right above
        # "Broken resistance". Both are fixed here rather than in stock_data,
        # whose code fingerprint would mark the stored snapshot stale.
        sup, res = d.get("support"), d.get("resistance")
        decided_zone = decided.get("zone")
        close = d["close"]
        if not d.get("zone_count"):
            lines.append("No support/resistance zone found in the lookback.")
        if sup and sup == res:
            lines.append(
                f"Close is inside zone {_zone(sup)}: broken below {sup['low'] * (1 - buf / 100):,.2f} "
                f"or above {sup['high'] * (1 + buf / 100):,.2f}"
            )
        else:
            if sup and sup != decided_zone:
                lines.append(f"Support {_zone(sup)}: broken below {sup['low'] * (1 - buf / 100):,.2f}")
            if res and res != decided_zone:
                lines.append(f"Resistance {_zone(res)}: broken above {res['high'] * (1 + buf / 100):,.2f}")
        if decided.get("test") == "support":
            z = decided_zone
            lines.append(
                f"Broken support {_zone(z)}? close {close:,.2f} < {z['low'] * (1 - buf / 100):,.2f} → Yes"
            )
        else:
            lines.append("Broken support? → No")
            if decided.get("test") == "resistance":
                z = decided_zone
                lines.append(
                    f"Broken resistance {_zone(z)}? close {close:,.2f} > "
                    f"{z['high'] * (1 + buf / 100):,.2f} → Yes"
                )
            else:
                lines.append("Broken resistance? → No")
    else:
        for name, period, key in (("slow", slow, "ema_slow"), ("mid", mid, "ema_mid"), ("fast", fast, "ema_fast")):
            ema = d[key]
            broken = decided.get("test") == name
            lines.append(
                f"Broken {period}W EMA? close {d['close']:,.2f} vs {ema:,.2f} "
                f"(line {ema * (1 - buf / 100):,.2f}) → {'Yes' if broken else 'No'}"
            )
            if broken:
                break
    lines.append(f"→ {row.get('ta_rules') or '—'}")
    return "\n".join(lines)


def stage_tooltip(row):
    """Hover text for a Stage cell, from the fields stage_analysis attached: the
    stage before, how long this one has run and how well-formed it is, and the day
    the CSV carrying it was uploaded (the indicator is not live here, so an old
    upload should read as old)."""
    if not row.get("stage"):
        return "No Stage Analysis upload covers this ticker yet."
    if row["stage"] == "Unknown":
        return "The indicator had too little history to place this ticker in a stage."
    lines = [f"Stage: {row['stage']}"]
    if row.get("stage_prev"):
        lines.append(f"Previous stage: {row['stage_prev']}")
    if row.get("stage_periods") is not None:
        conf = f" ({row['stage_conf']})" if row.get("stage_conf") else ""
        lines.append(f"In this stage for {row['stage_periods']} periods{conf}")
    if row.get("stage_uploaded"):
        lines.append(f"Uploaded: {row['stage_uploaded']}")
    return "\n".join(lines)


def vol_trend_tooltip(row, settings):
    """Builds the hover-tooltip text for a Vol Trend cell: the actual 10D/100D
    MEDIAN-volume ratio against both thresholds (stock_data.volume_stats)."""
    # A row from a snapshot made before the medians existed (the app shows the
    # old snapshot until the next refresh) was classified on the averages.
    basis = "Median" if "median_volume_10d" in row else "Average"
    if basis == "Median":
        v10, v100 = row.get("median_volume_10d"), row.get("median_volume_100d")
    else:
        v10, v100 = row.get("avg_volume_10d"), row.get("avg_volume_100d")
    explode = settings.get("volume_explode_ratio", 1.4)
    decline = settings.get("volume_decline_ratio", 0.7)
    if v10 is None or v100 is None or not v100:
        return "Not enough volume history yet to compute Vol Trend."
    ratio = v10 / v100
    lines = [
        f"{basis} day, 10D ÷ 100D = {v10:,.0f} ÷ {v100:,.0f} = {ratio:.2f}×",
        f"Exploding needs ≥ {explode}×",
        f"Declining needs ≤ {decline}×",
        f"→ {row.get('volume_trend') or '—'}",
    ]
    return "\n".join(lines)


def tech_uptrend_tooltip(row, settings, labels):
    """Builds the hover-tooltip text for a Tech Uptrend cell: each requirement
    and whether it passed, read from the detail stock_data stored. It used to
    re-test the row's ROUNDED VStop and slow WEMA, which could disagree with the
    label it explained (stock_data.compute_tech_uptrend compares unrounded)."""
    if "tech_uptrend_detail" not in row:
        # A row from a snapshot made before the detail existed: the app keeps
        # showing it until the next hourly refresh after a code change.
        return "The per-condition breakdown appears after the next data refresh."
    d = row.get("tech_uptrend_detail") or {}
    passed = d.get("passed")
    if not passed:
        return "Not enough data yet to compute Tech Uptrend."
    w_slow = labels["w_slow"]
    ratio = d["vol_10d"] / d["vol_100d"] if d.get("vol_100d") else None
    lines = [
        "Tech Uptrend requires ALL of:",
        f"{_mark(passed['above_vstop'])} Close > Weekly VStop ({d['close']:.2f} vs {d['vstop']:.2f})",
        f"{_mark(passed['vstop_up'])} VStop pointing Up ({d['vstop_direction']})",
        f"{_mark(passed['held'])} Held > {d['min_weeks']} weeks since VStop flip ({d['weeks_since']} weeks)",
        f"{_mark(passed['above_slow'])} Close > {w_slow} ({d['close']:.2f} vs {d['ema_slow']:.2f})",
        f"{_mark(passed['volume'])} 10D median volume > {d['vol_ratio']}× 100D"
        + (f" ({ratio:.2f}×)" if ratio is not None else ""),
        f"→ {'Yes' if row.get('tech_uptrend') else 'No'}",
    ]
    return "\n".join(lines)
