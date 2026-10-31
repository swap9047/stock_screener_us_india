"""The News tab's "market read": a few sentences on what the index charts say.

For each index that gets a read (Nifty 500 and the S&P 500: READ_INDEX_KEYS) and each of
the four charts the tab draws for it -- % of stocks above the 200/50-day SMA,
52-week highs vs lows, % of stocks 10/20/30/40% below their 52-week high, and
your invested list against the index -- code works out
the facts (where today's reading sits against its own history, how it moved over
the last sessions, when it last crossed a line) and a Gemini ladder writes the
reading from those facts and the last few reads, so it can say what CHANGED.
Written twice a day by market-read.yml (7:15 AM ET after NSE's close, 5:15 PM ET
after the US close) into market_read.json, which the tab shows behind an (i)
button above the charts.

The model never sees the charts or the market, only the facts below, and a reply
is rejected -- the ladder moves to the next rung -- when it quotes a number the
facts do not hold, gives advice ("buy", "sell"), or is malformed. A chart with
nothing worth saying comes back as null and is simply not shown.

PRIVACY: the return chart compares the invested list, so the text describes
holdings. market_read.json lives only in the private data repo; nothing here
prints a ticker, a company name or a read's text (the repo's Actions logs are
public). Pandas only; no Streamlit.
"""

import json
import os
import re
from datetime import datetime, timezone

import pandas as pd
from google.genai import types

import index_registry
import llm_util
import model_settings
from breadth_math import assemble_closes

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MARKET_READ_FILE = os.path.join(SCRIPT_DIR, "market_read.json")

# The ladder (owner, 2026-10-31): 3.8-flash four times, each on a different key
# (run_model_ladder passes the failed rung's key as the next one's `avoid_key`),
# then 3.6-flash, then 3.5-flash-lite, every rung at a 4k thinking budget.
PRIMARY_MODEL = "models/gemini-3.8-flash"
PRIMARY_ATTEMPTS = 4
FALLBACK_MODELS = ("models/gemini-3.6-flash", "models/gemini-3.5-flash-lite")
THINKING_BUDGET = 4096
CALL_TIMEOUT_S = 150      # a call not answered by then fails and the ladder moves on (owner, 2026-10-31)

# Reads kept per index, and how many of the latest go back into the prompt.
KEEP_ENTRIES = 10
PRIOR_READS = 3

# The indexes that get a read (owner, 2026-10-31: Nifty 500 and the S&P 500 only). Their keys are
# index_registry's "INDIA" and "US"; the other four indexes the News tab can show are not written.
READ_INDEX_KEYS = ("INDIA", "US")

# The invested lists that get a read (owner, 2026-10-31): the two panels the News tab's Watchlist Breadth shows by
# default, each against its home index, whose own read it takes as the market context (list_read.py).
LIST_READ_KEYS = {"india_invested": "INDIA", "us_invested": "US"}

# In the order the charts are drawn.
SECTIONS = ("breadth", "highs_lows", "drawdown", "return")
SECTION_TITLES = {"breadth": "Breadth", "highs_lows": "52-week highs vs lows",
                  "drawdown": "Stocks far below their highs", "return": "Your list vs the index"}
# An invested list's read adds the market it sits in, its quarterly results and its holdings, and ends with takeaways.
LIST_SECTIONS = ("market", "breadth", "highs_lows", "drawdown", "return", "results", "stocks")
LIST_SECTION_TITLES = {**SECTION_TITLES, "market": "The market", "return": "Against the index",
                       "results": "Quarterly results", "stocks": "Holdings"}
MAX_TEXT_CHARS = 700

# Trading-day windows for the return chart's facts.
RETURN_WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}

# A read is analysis, not advice. Phrases that tell the reader what to do or promise an
# outcome -- not the words alone: "selling pressure", "short-term" and "buyers" are
# ordinary market language the model rightly uses (its first real run tripped over them).
_ACTION = r"(?:buy|sell|add|trim|exit|accumulate|short|go long|go short)"
_DOING = r"(?:buy|buying|sell|selling|add|adding|trim|trimming|exit|exiting|accumulate|accumulating|short|shorting)"
ADVICE_WORDS = re.compile(
    rf"\b(?:you|investors?|traders?)\s+(?:should|could|can|may|might|must|need to)\s+(?:now\s+|consider\s+)?{_DOING}\b"
    rf"|\b(?:time|good time|good place|good moment|opportunity|chance|moment)\s+to\s+{_ACTION}\b"
    rf"|\b{_ACTION}\s+(?:the\s+|into\s+|on\s+)?(?:dip|dips|rally|rallies|strength|weakness|stocks?|index|market|this|now)\b"
    r"|\brecommend|\bstop[- ]?loss|\bprice target|\bguarantee[sd]?\b"
    r"|\bwill\s+(?:definitely\s+|surely\s+)?(?:rally|crash|bounce|fall|rise|recover|collapse)\b", re.I)

# Numbers a read may use without the facts holding them: the chart's own lines and
# windows ("50% line", "200-day", "52-week", "20 sessions").
FIXED_NUMBERS = {20, 50, 80, 100, 200, 52, 1, 2, 3, 4, 5, 10}


def r1(x):
    """A float rounded to one decimal, or None when it is missing."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        return None
    return round(float(x), 1)


def _iso(ts):
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _series(mapping, asof=None):
    """{"YYYY-MM-DD": value} -> a float Series by date, oldest first, up to `asof`."""
    if not mapping:
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(k): v for k, v in mapping.items()}, dtype=float).sort_index().dropna()
    if asof is not None:
        s = s[s.index <= pd.Timestamp(asof)]
    return s


def _zone(v):
    return "above 80" if v > 80 else "50 to 80" if v >= 50 else "20 to 50" if v >= 20 else "below 20"


def _level_facts(s):
    """Where a 0-100 breadth reading stands: change, range, rank, zone, crossings."""
    now = s.iloc[-1]
    out = {"now": r1(now), "last_5_sessions": [r1(v) for v in s.iloc[-5:]]}
    for n in (1, 5, 20):
        if len(s) > n:
            out[f"change_{n}d_points"] = r1(now - s.iloc[-1 - n])
    year = s.iloc[-252:]
    out["1y_low"] = {"value": r1(year.min()), "date": _iso(year.idxmin())}
    out["1y_high"] = {"value": r1(year.max()), "date": _iso(year.idxmax())}
    out["share_of_last_year_at_or_below_now_pct"] = r1((year <= now).mean() * 100)
    earlier = s.iloc[:-1]
    for name, mask in (("lowest_since", earlier <= now), ("highest_since", earlier >= now)):
        hit = earlier[mask]
        if hit.empty:
            out[name] = "the whole stored history"
        else:
            sessions = len(s) - 1 - s.index.get_loc(hit.index[-1])
            out[name] = _iso(hit.index[-1]) if sessions >= 10 else None
    out["zone"] = _zone(now)
    run = 0
    for v in reversed(s.tolist()):
        if _zone(v) != _zone(now):
            break
        run += 1
    out["sessions_in_zone"] = run
    side = (s >= 50)
    flips = side[side != side.shift()].iloc[1:]
    if not flips.empty:
        last = flips.index[-1]
        out["last_cross_of_50"] = {"date": _iso(last), "direction": "up" if bool(side.loc[last]) else "down",
                                   "sessions_ago": len(s) - 1 - s.index.get_loc(last)}
    return out


def breadth_facts(block, asof=None):
    """Facts for the % above the 200-day (and 50-day) SMA chart, or None."""
    s200 = _series((block or {}).get("history"), asof)
    if len(s200) < 30:
        return None
    out = {"session": _iso(s200.index[-1]), "stocks_counted": (block or {}).get("total"),
           "pct_above_200d": _level_facts(s200)}
    s50 = _series((block or {}).get("history_50"), asof)
    if len(s50) >= 30:
        out["pct_above_50d"] = _level_facts(s50)
        out["gap_50d_minus_200d_points"] = r1(s50.iloc[-1] - s200.reindex(s50.index).iloc[-1])
    return out


def highs_lows_facts(block, asof=None):
    """Facts for the 52-week highs vs lows chart, or None."""
    hi = _series((block or {}).get("highs_history"), asof)
    lo = _series((block or {}).get("lows_history"), asof)
    both = pd.concat([hi.rename("h"), lo.rename("l")], axis=1).dropna()
    if len(both) < 30:
        return None
    h, l = both["h"], both["l"]
    year_h, year_l = h.iloc[-252:], l.iloc[-252:]
    out = {"session": _iso(both.index[-1]),
           "highs_pct": r1(h.iloc[-1]), "lows_pct": r1(l.iloc[-1]),
           "net_highs_minus_lows_points": r1(h.iloc[-1] - l.iloc[-1]),
           "highs_last_5_sessions": [r1(v) for v in h.iloc[-5:]],
           "lows_last_5_sessions": [r1(v) for v in l.iloc[-5:]],
           "highs_avg_20d": r1(h.iloc[-20:].mean()), "lows_avg_20d": r1(l.iloc[-20:].mean()),
           "1y_most_highs": {"value": r1(year_h.max()), "date": _iso(year_h.idxmax())},
           "1y_most_lows": {"value": r1(year_l.max()), "date": _iso(year_l.idxmax())},
           "share_of_last_year_lows_at_or_below_now_pct": r1((year_l <= l.iloc[-1]).mean() * 100),
           "share_of_last_year_highs_at_or_below_now_pct": r1((year_h <= h.iloc[-1]).mean() * 100)}
    for name, lead, other in (("sessions_in_a_row_lows_exceed_highs", l, h), ("sessions_in_a_row_highs_exceed_lows", h, l)):
        run = 0
        for a, b in zip(reversed(lead.tolist()), reversed(other.tolist())):
            if a <= b:
                break
            run += 1
        out[name] = run
    return out


def drawdown_facts(block, asof=None):
    """Facts for the "% down from the 52-week high" chart (the share of stocks at
    least 10 / 20 / 30 / 40% below their highest close of the year), or None."""
    history = (block or {}).get("drawdown_history") or {}
    levels = {}
    for lvl, mapping in sorted(history.items(), key=lambda kv: int(kv[0])):
        s = _series(mapping, asof)
        if len(s) < 30:
            continue
        year = s.iloc[-252:]
        facts = {"now": r1(s.iloc[-1]), "last_5_sessions": [r1(v) for v in s.iloc[-5:]],
                 "1y_high": {"value": r1(year.max()), "date": _iso(year.idxmax())},
                 "share_of_last_year_at_or_below_now_pct": r1((year <= s.iloc[-1]).mean() * 100)}
        for n in (5, 20):
            if len(s) > n:
                facts[f"change_{n}d_points"] = r1(s.iloc[-1] - s.iloc[-1 - n])
        levels[f"down_{lvl}pct_or_more"] = facts
        last = s.index[-1]
    if not levels:
        return None
    return {"session": _iso(last), "share_of_stocks": levels}


def portfolio_curve(closes, tickers):
    """The equal-weight, daily-rebalanced curve (base 100) the News tab draws for a
    list: the same maths as app.py's calculate_portfolio_returns -- interior gaps
    filled inside each ticker's own history, a late listing not padded."""
    cols = [t for t in dict.fromkeys(tickers) if t in closes.columns]
    if not cols:
        return None
    raw = closes[cols]
    filled = raw.ffill().where(raw.bfill().notna())
    returns = filled.pct_change(fill_method=None)
    valid = returns.notna().astype(float)
    weighted = (returns.fillna(0) * valid).sum(axis=1) / valid.sum(axis=1)
    curve = 100 * (1 + weighted.fillna(0)).cumprod()
    return curve.dropna()


def return_facts(portfolio, bench, stocks, asof=None):
    """Facts for the list-vs-index chart, or None. `portfolio` and `bench` are
    Series by date; only dates both have are used."""
    if portfolio is None or bench is None:
        return None
    both = pd.concat([portfolio.rename("p"), bench.rename("b")], axis=1).dropna()
    if asof is not None:
        both = both[both.index <= pd.Timestamp(asof)]
    if len(both) < 30:
        return None
    p, b = both["p"], both["b"]
    out = {"session": _iso(both.index[-1]), "stocks_in_list": stocks, "windows": {}}
    for name, n in RETURN_WINDOWS.items():
        if len(both) > n:
            lp, lb = (p.iloc[-1] / p.iloc[-1 - n] - 1) * 100, (b.iloc[-1] / b.iloc[-1 - n] - 1) * 100
            out["windows"][name] = {"list_pct": r1(lp), "index_pct": r1(lb), "list_minus_index_points": r1(lp - lb)}
    year_p, year_b = p.iloc[-252:], b.iloc[-252:]
    out["list_below_1y_peak_pct"] = r1((year_p.iloc[-1] / year_p.max() - 1) * 100)
    out["index_below_1y_peak_pct"] = r1((year_b.iloc[-1] / year_b.max() - 1) * 100)
    spread = p / b
    for n in (5, 20):
        if len(spread) > n:
            out[f"relative_change_{n}d_pct"] = r1((spread.iloc[-1] / spread.iloc[-1 - n] - 1) * 100)
    return out


def index_price_facts(bench, asof=None):
    """The index's own price context (it is the dotted line on the return chart)."""
    s = bench.dropna() if bench is not None else None
    if s is None:
        return None
    if asof is not None:
        s = s[s.index <= pd.Timestamp(asof)]
    if len(s) < 30:
        return None
    out = {"session": _iso(s.index[-1])}
    for n in (1, 5, 20, 63):
        if len(s) > n:
            out[f"change_{n}d_pct"] = r1((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100)
    year = s.iloc[-252:]
    out["below_1y_high_pct"] = r1((s.iloc[-1] / year.max() - 1) * 100)
    out["above_1y_low_pct"] = r1((s.iloc[-1] / year.min() - 1) * 100)
    return out


def benchmark_series(ix, perf):
    """An index's price series from dashboard_perf.json (its "benchmarks", else a market block that holds it), or None."""
    benchmarks = (perf or {}).get("benchmarks") or {}
    bench_map = benchmarks.get(ix["benchmark"])
    if not bench_map:
        for market_block in ((perf or {}).get("markets") or {}).values():
            if isinstance(market_block, dict) and market_block.get(ix["benchmark"]):
                bench_map = market_block[ix["benchmark"]]
                break
    return _series(bench_map) if bench_map else None


def build_facts(ix, block, perf, tickers, asof=None):
    """Everything the model may say about one index, as of `asof` (default: the
    block's latest day). `perf` is dashboard_perf.json, `tickers` the invested
    list of the index's region."""
    bench = benchmark_series(ix, perf)
    breadth = breadth_facts(block, asof)
    highs = highs_lows_facts(block, asof)
    drawdown = drawdown_facts(block, asof)
    listing = None
    closes, _ = assemble_closes((perf or {}).get("markets") or {}, tickers)
    if not closes.empty:
        listing = return_facts(portfolio_curve(closes, tickers), bench, len(closes.columns), asof)
    facts = {"index": ix["label"], "breadth": breadth, "highs_lows": highs, "drawdown": drawdown,
             "return": listing, "index_price": index_price_facts(bench, asof)}
    facts["session"] = (breadth or highs or drawdown or listing or {}).get("session")
    return facts


# --- the model -------------------------------------------------------------------

SYSTEM_PROMPT = """You write the "market read" an investor sees above the News tab's index charts: a few
plain sentences on what each chart says about how the market is positioned, and whether anything
significant changed. The reader holds Indian and US stocks and wants the most useful insight, not a
description of every line.

You get FACTS (computed from the charts' data) and your EARLIER READS of this index.

Charts:
- breadth: % of the index's stocks above their 200-day SMA (main line) and 50-day SMA. Above 80 is
  stretched/overbought territory, below 20 washed out/oversold, 50 the divide between broad strength
  and weakness. The 50-day line leads the 200-day.
- highs_lows: % of stocks at a 52-week high vs a 52-week low. Many lows with few highs is broad
  selling; a spike in lows is often a capitulation-style washout near a low (not a timing call);
  highs expanding with breadth rising is a healthy advance, highs narrowing while the index holds up
  a narrow, fragile rally.
- drawdown: % of the index's stocks at least 10/20/30/40% below their highest close of the past 52
  weeks (correction, bear-market and deeper damage; each level includes the ones above it). A high
  share at 20%+ with the index near its high means a narrow market; the share falling from a peak
  means the damage is healing.
- return: the investor's invested list (equal-weight, rebalanced daily, price only) against the
  index.

Rules:
- Use ONLY the facts. Every number you write must appear in the facts (rounded as given). Do not
  invent levels, dates, causes, news or forecasts.
- Name the condition (for example "washed out", "oversold bounce attempt", "overbought", "broad
  strength", "narrow rally", "deteriorating", "recovering") and add hedged context -- what such a
  reading has tended to mean and its caveat ("often", "can", "tends to"). Never say buy, sell or
  short, never promise an outcome, no price targets.
- Compare with the earlier reads: say what changed since them (a cross of a line, a reading moving
  from one zone to another, a trend continuing for several sessions) or that nothing has. Do not
  repeat an earlier read's wording when nothing moved.
- Say something about a chart only when it adds insight; otherwise use null for it. A fact being
  absent means that chart gets null.
- headline: one sentence (at most 28 words) with the single most important takeaway.
- Each chart's text: at most two sentences, at most 55 words, plain text, no markdown.

Reply with one JSON object and nothing else:
{"headline": "...", "breadth": "..." or null, "highs_lows": "..." or null, "drawdown": "..." or null,
 "return": "..." or null}"""


def build_prompt(label, facts, prior):
    """The model's prompt: the rules, the facts, and the last few reads (oldest first)."""
    earlier = [{"session": e.get("session"), "headline": e.get("headline"),
                **{k: e.get(k) for k in SECTIONS}} for e in prior[-PRIOR_READS:]]
    return (SYSTEM_PROMPT + f"\n\nINDEX: {label}\nSESSION: {facts.get('session')}\n\nFACTS:\n"
            + json.dumps(facts, indent=1, default=str)
            + "\n\nEARLIER READS (oldest first"
            + ("" if earlier else "; none yet, so this is the first read") + "):\n"
            + (json.dumps(earlier, indent=1, default=str) if earlier else "[]"))


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(value, found):
    """Every number held in the facts: JSON numbers, and the digits of date strings."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        found.add(abs(float(value)))
    elif isinstance(value, str):
        for m in _NUMBER.findall(value):
            found.add(float(m))
    elif isinstance(value, dict):
        for v in value.values():
            _numbers_in(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers_in(v, found)


def unknown_numbers(text, facts):
    """Numbers `text` writes that the facts do not hold: one written with d decimals
    matches a fact that rounds to it at that precision."""
    known = set()
    _numbers_in(facts, known)
    bad = []
    for m in _NUMBER.findall(text):
        n = float(m)
        if n in FIXED_NUMBERS:
            continue
        d = len(m.split(".")[1]) if "." in m else 0
        if not any(abs(k - n) <= 0.5 * 10 ** -d + 1e-9 for k in known):
            bad.append(m)
    return bad


def validate_read(data, facts):
    """The model's reply as a clean read, or ValueError -- which run_model_ladder
    treats as a failed rung, so the next one tries."""
    if not isinstance(data, dict):
        raise ValueError("the read is not a JSON object")
    read = {}
    for key in ("headline", *SECTIONS):
        text = data.get(key)
        if text is None or (isinstance(text, str) and not text.strip()):
            text = None
        elif not isinstance(text, str):
            raise ValueError(f"{key} is not text")
        else:
            text = " ".join(text.split())
            if len(text) > MAX_TEXT_CHARS:
                raise ValueError(f"{key} is too long")
            if ADVICE_WORDS.search(text):
                raise ValueError(f"{key} gives advice")
            bad = unknown_numbers(text, facts)
            if bad:
                raise ValueError(f"{key} quotes numbers the facts do not hold: {', '.join(bad)}")
        if key in SECTIONS and text and not facts.get(key):
            raise ValueError(f"{key} has no facts to read")
        read[key] = text
    if not read["headline"] and not any(read[k] for k in SECTIONS):
        raise ValueError("the read is empty")
    return read


def generate_read(client, label, facts, prior, tiers=None, budget=None, prompt=None, validate=None):
    """(read dict, model used), or (None, None) once every rung failed. `tiers` and `budget` come from
    the sidebar's Settings · Models ("R1", model_settings); without them, the defaults below. `prompt` and
    `validate` replace build_prompt / validate_read (an invested list's read, list_read.py)."""
    thinking = THINKING_BUDGET if budget is None else budget

    def config_for(model):
        kwargs = {"response_mime_type": "application/json"}
        if "gemma" not in model and thinking:
            if isinstance(thinking, str):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking)
            else:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=int(thinking))
        return types.GenerateContentConfig(**kwargs)

    tiers = tiers or (llm_util.same_model_tiers(PRIMARY_MODEL, attempts=PRIMARY_ATTEMPTS)
                      + [(m, 0) for m in FALLBACK_MODELS])
    return llm_util.run_model_ladder(
        client, (prompt or build_prompt)(label, facts, prior), tiers, config_for, label="market read", subject=label,
        timeout=CALL_TIMEOUT_S,
        on_success=lambda resp: (validate or validate_read)(llm_util.json_object(resp.text or ""), facts))


# --- storage -----------------------------------------------------------------------

def load_market_read(path=None):
    """market_read.json as a dict ({"indexes": {key: {"label", "entries": [...]}}}); {} when absent or torn."""
    try:
        with open(path or MARKET_READ_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_market_read(data, path=None, now=None):
    """Atomic write, stamped `generated_at` -- the field github_sync's freshness guard
    reads so a pull or a commit never moves this file backwards."""
    from stock_data import atomic_write_json
    data["generated_at"] = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    atomic_write_json(path or MARKET_READ_FILE, data)


def entries_for(data, key, group="indexes"):
    """An index's reads (group "indexes") or an invested list's (group "lists"), oldest first."""
    entries = ((data.get(group) or {}).get(key) or {}).get("entries")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def latest_entry(data, key, group="indexes"):
    entries = entries_for(data, key, group)
    return max(entries, key=lambda e: e.get("session") or "") if entries else None


def add_entry(data, key, label, entry, group="indexes"):
    """Store `entry`, replacing the same session's and keeping the newest KEEP_ENTRIES."""
    slot = data.setdefault(group, {}).setdefault(key, {})
    slot["label"] = label
    kept = [e for e in entries_for(data, key, group) if e.get("session") != entry["session"]] + [entry]
    kept.sort(key=lambda e: e.get("session") or "")
    slot["entries"] = kept[-KEEP_ENTRIES:]


def box_markdown(entry, label, when_text):
    """What the (i) box shows for an index's or an invested list's latest read, as Markdown (Streamlit
    would read $...$ as LaTeX, so a dollar sign is escaped)."""
    def clean(text):
        return str(text).replace("$", "\\$")

    if not entry:
        return f"No read for {label} yet -- it is written after the {when_text} run."
    parts = []
    if entry.get("headline"):
        parts.append(f"**{clean(entry['headline'])}**")
    is_list = any(entry.get(k) for k in ("market", "results", "stocks", "takeaways"))
    for key in (LIST_SECTIONS if is_list else SECTIONS):
        if entry.get(key):
            title = (LIST_SECTION_TITLES if is_list else SECTION_TITLES)[key]
            parts.append(f"**{title}.** {clean(entry[key])}")
    if isinstance(entry.get("takeaways"), list) and entry["takeaways"]:
        parts.append("**Key takeaways**\n" + "\n".join(f"- {clean(t)}" for t in entry["takeaways"] if t))
    if not parts:
        parts.append("Nothing stood out on these charts.")
    foot = f"{label} · session of {entry.get('session', '?')}"
    if entry.get("benchmark"):
        foot += f" · against {entry['benchmark']}"
    if entry.get("written"):
        foot += f" · written {str(entry['written'])[:10]}"
    if entry.get("model"):
        foot += f" · {str(entry['model']).replace('models/', '')}"
    return "\n\n".join(parts) + f"\n\n*{foot}*"


# --- the job -----------------------------------------------------------------------

REGION_LIST = {"INDIA": "india_invested", "US": "us_invested"}


def sessions_to_write(block, existing, backfill=0, force=False):
    """The block's days to write a read for, oldest first: its latest day if there is
    none yet (or `force`), and with `backfill` n also the n days before it that have
    none (seeding the 'earlier reads' a new index starts with)."""
    days = sorted((block or {}).get("history") or {})
    if not days:
        return []
    have = {e.get("session") for e in existing}
    wanted = days[-(backfill + 1):]
    return [d for d in wanted if d not in have or (force and d == days[-1])]


def run(client, breadth, perf, watchlists, data, now=None, backfill=0, force=False, log=print,
        tiers=None, budget=None, on_write=None):
    """Write the missing reads into `data`. Returns (written, awaiting, failed): counts
    of reads written, indexes whose reading is still forming or part-published, and
    reads every rung failed on. Only READ_INDEX_KEYS are read. `on_write()` is called after each read is
    added, so a caller can save as it goes (a run cut short keeps what it wrote)."""
    now = now or datetime.now(timezone.utc)
    markets = (breadth or {}).get("markets") or {}
    status = (breadth or {}).get("status") or {}
    written = awaiting = failed = 0
    for ix in (i for i in index_registry.INDEXES if i["key"] in READ_INDEX_KEYS):
        block = markets.get(ix["key"]) or {}
        if not block.get("history"):
            log(f"[market read] {ix['label']}: no breadth stored yet")
            continue
        if block.get("live") or block.get("partial") or status.get(ix["key"]) in ("live", "partial", "awaiting"):
            log(f"[market read] {ix['label']}: the latest reading is not final yet")
            awaiting += 1
            continue
        todo = sessions_to_write(block, entries_for(data, ix["key"]), backfill, force)
        tickers = (watchlists or {}).get(REGION_LIST[ix["region"]], []) or []
        for session in todo:
            facts = build_facts(ix, block, perf, tickers, asof=session)
            prior = [e for e in entries_for(data, ix["key"]) if (e.get("session") or "") < session]
            read, model = generate_read(client, ix["label"], facts, prior, tiers=tiers, budget=budget)
            if not read:
                log(f"[market read] {ix['label']} {session}: every model failed")
                failed += 1
                break
            add_entry(data, ix["key"], ix["label"], {"session": session, "written": now.strftime("%Y-%m-%d %H:%M UTC"),
                                                     "model": model, **read})
            log(f"[market read] {ix['label']} {session}: written by {model}")
            written += 1
            if on_write:
                on_write()
    return written, awaiting, failed
