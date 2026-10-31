"""The News tab's invested-list reads: what the Watchlist Breadth panels say, for the owner of the list.

market_read.py reads an INDEX's charts. This reads the panels below them -- India Invested and US Invested, each
with its breadth (% above the 200/50-day SMA, against its home index), 52-week highs vs lows, stocks 10-40% below
their 52-week high, the list's return against the index, and the quarterly results medians -- and sets them in the
market the index read describes. It ends with a few key takeaways for the investor, and may name up to three
holdings per point (owner, 2026-10-31).

Same discipline as market_read: code works out the facts (the panels' own maths: breadth_math over the list's closes,
the equal-weight curve, quarterly_results.summarize, the snapshot's per-stock figures), the model only words them,
and a reply that quotes a number the facts lack, gives advice, or is malformed is refused so the ladder moves on.
Names are used as the facts give them.

PRIVACY: this text names holdings. It lives only in the private data repo, the research library's database behind the
token-protected connector, and the app; nothing here prints a ticker, a company name or a read's text (public logs).
Pandas only; no Streamlit.
"""

import json
from collections import Counter
from datetime import datetime, timezone

import pandas as pd

import index_registry
import market_read as mr
import quarterly_results as qr
from breadth_math import assemble_closes, compute_breadth

LIST_READ_KEYS = mr.LIST_READ_KEYS
# Months of the list's own breadth computed for the facts (the panel's default horizon is 18).
BREADTH_MONTHS = 24
NEAR_LOW_PCT = 5.0
CALLOUTS = 3
MAX_TAKEAWAYS = 5
MAX_TAKEAWAY_CHARS = 320
RESULT_QUARTERS = 4

PARTS = ("market", "breadth", "highs_lows", "drawdown", "return", "results", "stocks")


# --- facts ------------------------------------------------------------------------------------

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _name(row):
    return str(row.get("company_name") or row.get("ticker") or "").strip()[:40]


def stock_callouts(rows):
    """Per-stock facts from the snapshot rows of the list, or None: the trend mix, what is below its 200-day,
    what is near its 52-week low, and the best and worst of the last month and quarter."""
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("ticker")]
    if not rows:
        return None
    out = {"stocks_in_list": len(rows)}
    trend = Counter(r["trend"] for r in rows if r.get("trend"))
    if trend:
        out["trend_counts"] = dict(trend)
    expert = Counter(r["expert_take"] for r in rows if r.get("expert_take"))
    if expert:
        out["expert_take_counts"] = dict(expert)
    below = [r for r in rows if r.get("below_200")]
    out["below_200_day_average"] = {"count": len(below), "examples": [_name(r) for r in below[:CALLOUTS + 2]]}
    near = []
    for r in rows:
        last, low = _num(r.get("last_close")), _num(r.get("week52_low"))
        if last and low and low > 0 and (last / low - 1) * 100 <= NEAR_LOW_PCT:
            near.append((round((last / low - 1) * 100, 1), r))
    near.sort(key=lambda t: t[0])
    out["near_52_week_low"] = [{"name": _name(r), "pct_above_low": pct} for pct, r in near[:CALLOUTS]]
    for field, label in (("perf_1m", "1m"), ("perf_3m", "3m")):
        have = sorted(((_num(r.get(field)), r) for r in rows if _num(r.get(field)) is not None), key=lambda t: t[0])
        if have:
            out[f"laggards_{label}"] = [{"name": _name(r), "return_pct": round(v, 1)} for v, r in have[:CALLOUTS]]
            out[f"leaders_{label}"] = [{"name": _name(r), "return_pct": round(v, 1)} for v, r in reversed(have[-CALLOUTS:])]
    return out


def results_facts(quarterly, tickers, index_key):
    """The list's quarterly results medians (the panel's results row), latest quarters last, with the home index's
    median for the newest quarter -- or None when no figure survives quarterly_results' thresholds."""
    data = (quarterly or {}).get("tickers") or {}
    if not data or not tickers:
        return None
    rows, companies = qr.summarize(data, tickers, basis="yoy", quarters=RESULT_QUARTERS)
    rows = [r for r in rows if any(r.get(f) is not None for f in qr.FIELDS)]
    if not rows:
        return None

    def shape(r):
        return {"quarter": r["label"], "revenue_growth_pct": r.get("rev"), "profit_growth_pct": r.get("ni"),
                "eps_growth_pct": r.get("eps"), "opm_pct": r.get("margin"), "companies_reported": r.get("reported"),
                "still_being_reported": bool(r.get("season"))}

    out = {"basis": "year on year, median company", "companies_with_results": companies, "quarters": [shape(r) for r in rows]}
    members = ((quarterly or {}).get("members") or {}).get(index_key) or []
    if members:
        idx_rows, _ = qr.summarize(data, members, basis="yoy", quarters=RESULT_QUARTERS)
        match = next((r for r in idx_rows if r["label"] == rows[-1]["label"]), None)
        if match:
            out["index_median_same_quarter"] = {k: v for k, v in shape(match).items() if k != "quarter"}
    return out


def market_context(data, index_key, asof):
    """The index's own latest read up to `asof` (what the market looks like), trimmed for the prompt, or None."""
    entries = [e for e in mr.entries_for(data, index_key) if (e.get("session") or "") <= asof]
    if not entries:
        return None
    e = max(entries, key=lambda x: x.get("session") or "")
    return {"index": index_registry.BY_KEY[index_key]["label"], "session": e.get("session"), "headline": e.get("headline"),
            **{k: e.get(k) for k in mr.SECTIONS if e.get(k)}}


def list_breadth_block(closes, asof):
    """The list's own breadth as the panel computes it (breadth_math over its stocks' closes), in the shape of an
    index's stored block so market_read's breadth, highs/lows and drawdown facts read it unchanged."""
    wb = compute_breadth(closes, since=pd.Timestamp(asof) - pd.DateOffset(months=BREADTH_MONTHS))
    if not wb or not wb.get("history"):
        return None
    return {"total": wb["total"], "history": wb["history"], "history_50": wb.get("history_50") or {},
            "highs_history": wb["highs_history"], "lows_history": wb["lows_history"],
            "drawdown_history": wb.get("drawdown_history") or {}}


def build_list_facts(list_key, label, perf, tickers, rows, quarterly, index_block, data, asof):
    """Everything the model may say about one invested list as of `asof`, or None when the list has no stored
    closes. `rows` are the list's snapshot rows (today's figures; only used for the latest session)."""
    index_key = LIST_READ_KEYS[list_key]
    ix = index_registry.BY_KEY[index_key]
    closes, _missing = assemble_closes((perf or {}).get("markets") or {}, tickers)
    if closes.empty:
        return None
    closes = closes[closes.index <= pd.Timestamp(asof)]
    if closes.empty:
        return None
    block = list_breadth_block(closes, asof)
    bench = mr.benchmark_series(ix, perf)
    breadth = mr.breadth_facts(block, asof) if block else None
    if breadth and index_block:
        idx200 = mr._series(index_block.get("history"), asof)
        idx50 = mr._series(index_block.get("history_50"), asof)
        if len(idx200):
            breadth["index_pct_above_200d_now"] = mr.r1(idx200.iloc[-1])
        if len(idx50):
            breadth["index_pct_above_50d_now"] = mr.r1(idx50.iloc[-1])
    latest = bool(index_block) and asof == max((index_block.get("history") or {"": 0}))
    facts = {
        "list": label, "benchmark": ix["label"], "stocks_tracked": len(set(tickers)),
        "breadth": breadth,
        "highs_lows": mr.highs_lows_facts(block, asof) if block else None,
        "drawdown": mr.drawdown_facts(block, asof) if block else None,
        "return": mr.return_facts(mr.portfolio_curve(closes, tickers), bench, len(closes.columns), asof),
        "index_price": mr.index_price_facts(bench, asof),
        "results": results_facts(quarterly, tickers, index_key) if latest else None,
        "stocks": stock_callouts(rows) if latest else None,
        "market_read": market_context(data, index_key, asof),
    }
    facts["session"] = next((facts[k]["session"] for k in ("breadth", "highs_lows", "drawdown", "return") if facts.get(k)), None)
    return facts if facts["session"] else None


# --- the prompt and the check on its answer ----------------------------------------------------

SYSTEM_PROMPT = """You write the "invested list read" an investor sees under the News tab's Watchlist Breadth charts: what
the charts of ONE of their own lists say, set in the market around it, and the key takeaways for them as the owner
of that list. The reader holds Indian and US stocks and wants the most useful insight, not a description of every line.

You get FACTS (computed from the charts' data), the screener's own read of the market (market_read: the home index's
charts, written earlier), and your EARLIER READS of this list.

The list's charts:
- breadth: % of the list's stocks above their 200-day SMA (main line) and 50-day SMA, with the home index's own
  share beside it. Above 80 is stretched, below 20 washed out, 50 the divide; the 50-day line leads the 200-day.
- highs_lows: % of the list's stocks at a 52-week high vs a 52-week low (it is a small list, so one stock moves it
  several points: say so when a few stocks explain a reading).
- drawdown: % of the list's stocks at least 10/20/30/40% below their highest close of the past 52 weeks.
- return: the list (equal-weight, rebalanced daily, price only) against its home index over 1, 3, 6 and 12 months,
  and each one's distance below its 1-year peak.
- results: the median company's year-on-year revenue, profit and EPS growth and OPM for the last quarters, and
  the home index's median for the newest quarter. A figure missing means too few companies reported.
- stocks: the holdings behind the charts: trend mix, what is below its 200-day average, what is near a 52-week
  low, and the leaders and laggards of the last month and quarter.

Rules:
- Use ONLY the facts. Every number you write must appear in the facts or in the market read, as given. Use
  company names exactly as the facts give them, and name at most 3 holdings in any one sentence. Do not invent
  levels, dates, causes, news or forecasts.
- Name the condition (for example "holding up better than the market", "narrow", "washed out", "late-cycle
  leadership", "broad weakness") and add hedged context -- what such a reading has tended to mean and its caveat
  ("often", "can", "tends to"). Never say buy, sell, trim, add or short, never promise an outcome, no price targets.
- market: one sentence setting the list in the market read (does the list confirm or diverge from the index?).
  Null when there is no market read.
- Put a reading in context before calling it extreme: set it against the chart's own range in the facts (the
  1-year high and low and when they happened, how many sessions it has been in this zone) and, for breadth,
  against the home index's share. Say "at its 12-month high/low" only when at_1y_high / at_1y_low (highs_at_1y_most
  / lows_at_1y_most) is true; never "matching" or "at" a high or low otherwise.
- Do not build a trend on one day's move. If last_5_sessions show the move reverses the previous day's, call it a
  one-day move and use the 5- or 20-session change. Earlier peaks only as the 12-month ranges the facts give.
- Compare with the earlier reads of this list: say what changed (a line crossed, a zone changed, leaders swapped)
  or that nothing has. Do not repeat an earlier read's wording when nothing moved.
- Say something about a chart only when it adds insight; otherwise use null for it. Most days four or five parts
  are enough; an ordinary or unchanged one is null. A fact being absent means null. Do not repeat a figure in two
  sentences, and do not repeat what the market read already says beyond one line.
- headline: one sentence, at most 30 words: the single most important takeaway.
- Each part's text: at most two sentences, at most 60 words, plain text, no markdown.
- takeaways: 3 to 5 short items (each at most 40 words) for the investor as the owner of this list: where it is
  strong, where it is weak or exposed, and what is worth watching next (a level the list is near, a stock
  deteriorating, a result still to come). Observations and "watch" items, not instructions.

Reply with one JSON object and nothing else:
{"headline": "...", "market": "..." or null, "breadth": "..." or null, "highs_lows": "..." or null,
 "drawdown": "..." or null, "return": "..." or null, "results": "..." or null, "stocks": "..." or null,
 "takeaways": ["...", "...", "..."]}"""


def build_list_prompt(label, facts, prior):
    """The prompt: the rules, the facts (the market read inside them), and the last few earlier reads, oldest first."""
    earlier = [{"session": e.get("session"), "headline": e.get("headline"), "takeaways": e.get("takeaways"),
                **{k: e.get(k) for k in PARTS}} for e in prior[-mr.PRIOR_READS:]]
    return (SYSTEM_PROMPT + f"\n\nLIST: {label}\nSESSION: {facts.get('session')}\n\nFACTS:\n"
            + json.dumps(facts, indent=1, default=str)
            + "\n\nEARLIER READS OF THIS LIST (oldest first"
            + ("" if earlier else "; none yet, so this is the first read") + "):\n"
            + (json.dumps(earlier, indent=1, default=str) if earlier else "[]"))


def _text(key, text, facts, limit=mr.MAX_TEXT_CHARS):
    """One text part, cleaned, or ValueError; None for an empty one."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return None
    if not isinstance(text, str):
        raise ValueError(f"{key} is not text")
    text = " ".join(text.split())
    if len(text) > limit:
        raise ValueError(f"{key} is too long")
    if key == "headline" and len(text.split()) > mr.MAX_HEADLINE_WORDS:
        raise ValueError("the headline is too long")
    if mr.ADVICE_WORDS.search(text):
        raise ValueError(f"{key} gives advice")
    if mr.INTERNAL_NAMES.search(text):
        raise ValueError(f"{key} uses an internal field name")
    bad = mr.unknown_numbers(text, facts)
    if bad:
        raise ValueError(f"{key} quotes numbers the facts do not hold: {', '.join(bad)}")
    return text


def validate_list_read(data, facts):
    """The model's reply as a clean list read, or ValueError (the ladder then tries the next rung)."""
    if not isinstance(data, dict):
        raise ValueError("the read is not a JSON object")
    read = {}
    for key in ("headline", *PARTS):
        read[key] = _text(key, data.get(key), facts)
        needs = "market_read" if key == "market" else key
        if key in PARTS and read[key] and not facts.get(needs):
            raise ValueError(f"{key} has no facts to read")
    raw = data.get("takeaways")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("takeaways is not a list")
    takeaways = [t for t in (_text("a takeaway", x, facts, MAX_TAKEAWAY_CHARS) for x in raw) if t]
    if len(takeaways) > MAX_TAKEAWAYS:
        raise ValueError("too many takeaways")
    read["takeaways"] = takeaways
    if not read["headline"] or len(takeaways) < 2:
        raise ValueError("the read needs a headline and at least two takeaways")
    return read


def generate_list_read(client, label, facts, prior, tiers=None, budget=None):
    return mr.generate_read(client, label, facts, prior, tiers=tiers, budget=budget,
                            prompt=build_list_prompt, validate=validate_list_read)


# --- the job ----------------------------------------------------------------------------------

def run_lists(client, breadth, perf, watchlists, data, snapshot, quarterly, labels=None, now=None, backfill=0,
              force=False, log=print, tiers=None, budget=None, on_write=None):
    """Write the missing invested-list reads into `data["lists"]`, after the index reads (each uses the index's read
    for its session as its market context). Returns (written, failed). A list is read for its home index's sessions
    once that index's reading is final; a list with no stored closes is skipped."""
    now = now or datetime.now(timezone.utc)
    markets = (breadth or {}).get("markets") or {}
    status = (breadth or {}).get("status") or {}
    per_market = (snapshot or {}).get("per_market") or {}
    written = failed = 0
    for list_key, index_key in LIST_READ_KEYS.items():
        label = (labels or {}).get(list_key) or list_key.replace("_", " ").title()
        block = markets.get(index_key) or {}
        tickers = (watchlists or {}).get(list_key) or []
        if not block.get("history") or not tickers:
            continue
        if block.get("live") or block.get("partial") or status.get(index_key) in ("live", "partial", "awaiting"):
            continue                        # already counted by the index's own pass
        todo = mr.sessions_to_write(block, mr.entries_for(data, list_key, "lists"), backfill, force)
        for session in todo:
            facts = build_list_facts(list_key, label, perf, tickers, per_market.get(list_key) or [], quarterly, block, data, session)
            if not facts:
                log(f"[invested read] {label} {session}: no stored closes to read")
                break
            prior = [e for e in mr.entries_for(data, list_key, "lists") if (e.get("session") or "") < session]
            read, model = generate_list_read(client, label, facts, prior, tiers=tiers, budget=budget)
            if not read:
                log(f"[invested read] {label} {session}: every model failed")
                failed += 1
                break
            mr.add_entry(data, list_key, label, {
                "session": session, "written": now.strftime("%Y-%m-%d %H:%M UTC"), "model": model,
                "benchmark": facts["benchmark"], **read}, group="lists")
            log(f"[invested read] {label} {session}: written by {model}")
            written += 1
            if on_write:
                on_write()
    return written, failed
