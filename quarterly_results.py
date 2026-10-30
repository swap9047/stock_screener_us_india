"""Quarterly results for a group of companies (2026-10-28): the News tab's fourth
chart row, for an index and for a watchlist.

Each quarter, the MEDIAN company's revenue growth %, profit growth % (reported
PAT: Yahoo's Net Income, one-offs included), EPS growth % and OPM % (its
operating margin, Yahoo's Operating Income over revenue: after depreciation) --
the median of the companies' own figures (owner, 2026-10-28), so one giant does
not decide an index's number and a small list is read the way its typical stock
did. YoY (the default) is the quarter against the same quarter a year earlier;
QoQ against the one before.

EPS growth skips a company whose diluted share count moved more than SHARE_JUMP
times between the two quarters: Yahoo often leaves the older quarter's EPS
unadjusted for a bonus issue or split (common in India), so a 1:1 bonus read as
EPS halving.

Why the history is stored, not fetched: Yahoo gives about 5 quarters of
statements per company (stock_data's TTM growth columns are blank for the same
reason), enough for 4 QoQ readings and ONE YoY. quarterly_results.json keeps
every quarter a fetch has ever returned, so YoY fills in one quarter per
results season (six after about a year).

Quarters are calendar quarters: a statement period is filed under the nearest
calendar quarter end, so an Indian company's Q2 FY27 and a US company's Q3 2026
(both ending 30 Sep) are one bucket, and a retailer's quarter ending 1 Nov
lands with September's.

Stdlib only: the app imports it, and refresh_quarterly_results.py does the
Yahoo part.

File (quarterly_results.json, written by refresh_quarterly_results.py):
    {"as_of": "YYYY-MM-DD HH:MM UTC",
     "tickers": {ticker: {"fetched": "YYYY-MM-DD",
                          "q": {"YYYY-MM-DD" (quarter end): {"rev", "op", "ni", "eps", "sh"}}}},
     "members": {index_registry key: [tickers]},
     "status": {...counts...}}
"""

from datetime import date, timedelta
from statistics import median

QUARTERLY_RESULTS_FILE = "quarterly_results.json"

# Quarters kept per company: three years (owner, 2026-10-29: Yahoo's quarters
# are stored from now on, three years of them, so YoY has two years of bars --
# each of the last 8 quarters against the one 4 before it).
MAX_QUARTERS = 12
# Quarters a chart shows by default: two years.
SHOW_QUARTERS = 8
# Days after a quarter ends before its results are looked for, and how long a
# company still missing them keeps being looked for daily. Indian companies
# have 45 days (60 for Q4), US ones ~40; past the wait a company that never
# reports (delisted, a fund) drops back to REFETCH_DAYS.
RESULTS_START_DAYS = 20
RESULTS_WAIT_DAYS = 75
# Even with nothing due, a company is fetched again this often (restatements).
REFETCH_DAYS = 14
# A quarter fewer companies have reported than this share of the quarter
# before it is still in its results season: drawn faded, and said so.
SEASON_SHARE = 0.5
# A figure needs at least this many companies, and this share of those that
# reported the quarter, or it is left out (owner, 2026-10-28). A small list's
# medians came from 1-6 companies: US Invested's Mar '26 profit growth was one
# company, India Invested's Sep '26 +435% was one company and stretched the
# axis to 400%. Yahoo's ~5 quarters also leave most companies without a
# year-ago quarter in the oldest YoY bar (S&P 500 Mar '26: 27 of 502).
MIN_COMPANIES = 5
MIN_SHARE = 0.3
# A company whose stored history spans the base quarter but lacks it is a hole
# in Yahoo's data, not a company too new to compare: Yahoo serves most Indian
# companies' Jun '25 and Dec '25 but not Sep '25 (236 of 501 Nifty 500
# companies on 2026-10-30), so only 40 of the 188 that had reported Sep '26
# had a year-ago quarter and the bar fell under MIN_SHARE. Such companies are
# left out of MIN_SHARE's count (owner, 2026-10-30: show the bar, marked);
# the figure is drawn light and its hover names how many were missing.
# A diluted share count this many times larger or smaller than the base
# quarter's is a bonus issue or split, not dilution: that EPS growth is skipped.
SHARE_JUMP = 1.5

METRICS = ("rev", "ni", "eps")
# The four fields a chart can show, in display order: three growth bars and the margin.
FIELDS = ("rev", "ni", "eps", "margin")
FIELD_LABELS = {"rev": "Revenue growth", "ni": "Profit growth", "eps": "EPS growth", "margin": "OPM"}

# Yahoo's statement row names, first found wins.
ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "ni": ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"),
    "eps": ("Diluted EPS", "Basic EPS"),
    # Stored from 2026-10-28 for the EPS guard (SHARE_JUMP); history only builds
    # from when a field is first stored.
    "sh": ("Diluted Average Shares", "Basic Average Shares"),
}
# The figures a period must have one of to be kept (shares alone are not results).
CORE = ("rev", "op", "ni", "eps")

_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def quarter_end(day):
    """The calendar quarter end nearest `day` (a date or "YYYY-MM-DD"), as "YYYY-MM-DD"."""
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    cands = [date(y, m, dd) for y in (d.year - 1, d.year, d.year + 1) for m, dd in _QUARTER_ENDS]
    return min(cands, key=lambda c: (abs((c - d).days), c)).isoformat()


def shift(q, n):
    """The quarter end `n` quarters after `q` (negative: before)."""
    d = date.fromisoformat(q)
    idx = d.year * 4 + (d.month - 1) // 3 + n
    y, i = divmod(idx, 4)
    m, dd = _QUARTER_ENDS[i]
    return date(y, m, dd).isoformat()


def label(q):
    """ "Sep '26" for "2026-09-30"."""
    d = date.fromisoformat(q)
    return f"{d.strftime('%b')} '{d.strftime('%y')}"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def normalize(periods):
    """{quarter end: {"rev","op","ni","eps","sh"}} from {period end: {Yahoo row name: value}}.

    Two periods in one bucket (a 53-week year) keep the later one. A period with
    none of the four figures is dropped."""
    out, chosen = {}, {}
    for end in sorted(periods):
        rows = periods[end] or {}
        rec = {}
        for field, names in ROWS.items():
            rec[field] = next((v for v in (_num(rows.get(n)) for n in names) if v is not None), None)
        if all(rec[k] is None for k in CORE):
            continue
        q = quarter_end(end)
        if q not in chosen or str(end) > chosen[q]:
            chosen[q] = str(end)
            out[q] = rec
    return out


def merge(old, new, keep=MAX_QUARTERS):
    """`old` quarters with `new` laid over them (a newer fetch wins per figure,
    a blank never erases a stored one); the last `keep` quarters, or all of
    them when `keep` is None."""
    out = {q: dict(v) for q, v in (old or {}).items()}
    for q, rec in (new or {}).items():
        cur = out.setdefault(q, {})
        for k, v in rec.items():
            if v is not None or k not in cur:
                cur[k] = v
    qs = sorted(out)
    return {q: out[q] for q in (qs if keep is None else qs[-keep:])}


def is_due(entry, today):
    """Whether a company's statements should be fetched on `today` (a date)."""
    if not entry or not entry.get("fetched"):
        return True
    fetched = date.fromisoformat(entry["fetched"])
    if fetched >= today:
        return False
    if (today - fetched).days >= REFETCH_DAYS:
        return True
    # The latest COMPLETE quarter: one with revenue and profit. Yahoo files a
    # quarter's EPS and share count from the earnings release days before its
    # revenue and net income -- on 2026-10-28 ~60 US companies had a Sep quarter
    # with EPS only -- and counting that as reported left the company unfetched
    # for REFETCH_DAYS, out of revenue and profit growth for two weeks. Such a
    # quarter keeps the company due daily until the rest arrives.
    qs = sorted(q for q, rec in (entry.get("q") or {}).items()
                if (rec or {}).get("rev") is not None and (rec or {}).get("ni") is not None)
    if not qs:
        return False
    nxt = date.fromisoformat(shift(qs[-1], 1))
    return nxt + timedelta(days=RESULTS_START_DAYS) <= today <= nxt + timedelta(days=RESULTS_WAIT_DAYS)


def growth(cur, base):
    """% growth from `base` to `cur`; None without both, or with a base <= 0
    (a percentage off a loss or zero means nothing, and flips sign)."""
    if cur is None or base is None or base <= 0:
        return None
    return (cur / base - 1) * 100


def share_jump(cur, base):
    """True when the diluted share count changed by a bonus/split-sized step."""
    a, b = (cur or {}).get("sh"), (base or {}).get("sh")
    if not a or not b or a <= 0 or b <= 0:
        return False
    return not (1 / SHARE_JUMP <= a / b <= SHARE_JUMP)


def margin(rec):
    """Operating margin %, or None (no operating income, e.g. most banks on Yahoo)."""
    rev, op = (rec or {}).get("rev"), (rec or {}).get("op")
    if rev is None or op is None or rev <= 0:
        return None
    return op / rev * 100


def summarize(data, tickers, basis="yoy", quarters=SHOW_QUARTERS, min_companies=MIN_COMPANIES,
              min_share=MIN_SHARE):
    """The group's quarters, oldest first: [{"q", "label", "rev", "ni", "eps",
    "margin" (medians, %, or None), "n": {metric: companies counted},
    "thin": [metrics left out for too few companies],
    "reported": companies with the quarter, "season": still being reported,
    "gap": companies whose base quarter is a hole in Yahoo's data, when a
    figure was kept only by leaving them out of the share (else 0)}].

    `data` is quarterly_results.json's "tickers"; `tickers` the group. A figure
    from fewer than `min_companies` companies, or under `min_share` of those
    that reported the quarter (less the "gap" companies, for a growth figure),
    is None (and named in "thin"). A quarter is shown once any growth figure
    survives that; the last `quarters` of those. Also returns how many of
    `tickers` have any stored results.
    """
    lag = 1 if basis == "qoq" else 4
    stored = {t: (data.get(t) or {}).get("q") or {} for t in dict.fromkeys(tickers)}
    stored = {t: q for t, q in stored.items() if q}
    all_q = sorted({q for qs in stored.values() for q in qs})
    reported = {q: sum(1 for qs in stored.values() if q in qs) for q in all_q}
    rows = []
    for q in all_q:
        base_q = shift(q, -lag)
        vals = {m: [] for m in (*METRICS, "margin")}
        holes = 0
        for qs in stored.values():
            cur, base = qs.get(q), qs.get(base_q)
            if cur is None:
                continue
            mg = margin(cur)
            if mg is not None:
                vals["margin"].append(mg)
            if base is None:
                holes += min(qs) < base_q
                continue
            jump = share_jump(cur, base)
            for m in METRICS:
                if m == "eps" and jump:
                    continue
                g = growth(cur.get(m), base.get(m))
                if g is not None:
                    vals[m].append(g)
        need = max(min_companies, min_share * reported[q])
        # Growth needs the base quarter, so a hole there is left out of its share.
        need_growth = max(min_companies, min_share * (reported[q] - holes))
        figures = {m: (round(median(v), 1) if v and len(v) >= (need if m == "margin" else need_growth) else None)
                   for m, v in vals.items()}
        if all(figures[m] is None for m in METRICS):
            continue
        gap_kept = any(figures[m] is not None and len(vals[m]) < need for m in METRICS)
        prev = reported.get(shift(q, -1), 0)
        rows.append({"q": q, "label": label(q), **figures,
                     "n": {m: len(v) for m, v in vals.items()},
                     "thin": [m for m, v in vals.items() if v and figures[m] is None],
                     "reported": reported[q],
                     "season": bool(prev) and reported[q] < SEASON_SHARE * prev,
                     "gap": holes if gap_kept else 0})
    return rows[-quarters:], len(stored)
