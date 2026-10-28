"""The indexes the screener tracks breadth for, in one place.

Before 2026-10-28 this list was copied into six places -- the News tab's two
pickers, the breadth job's legs, the performance job's benchmark series, the
breadth workflow's verify step, the research library's publish and Claude chat's
name lookup -- and adding Nasdaq 100 meant finding every copy. Each now reads
INDEXES, so an index is added here once.

Stdlib only: the Vercel functions (market_lists via api/mcp.py) and the
breadth workflow's inline verify step import it.

Fields of each entry:
    key        market_breadth.json's block key (also the research library's
               breadth series name). "INDIA" and "US" predate the others and
               mean Nifty 500 and S&P 500 -- never rename them, the stored
               history and the published series are keyed by them.
    label      what people see and type.
    benchmark  Yahoo's ticker for the index's price (the return curves), a
               series dashboard_perf.json stores under "benchmarks".
    region     "INDIA" or "US": the exchange clock its breadth runs on, and
               which invested watchlist its return curve compares.
    aliases    loose names Claude chat may use for it (exact matches only, so
               "nifty 50" never reads as "nifty 500").
    nse_list   NSE's member-list CSV (Indian indexes), under
               nsearchives.nseindia.com/content/indices/.
    size       its nominal member count: a list far shorter than this is a
               broken download, not a membership change.
"""

INDEXES = [
    {"key": "INDIA", "label": "Nifty 500", "benchmark": "^CRSLDX", "region": "INDIA",
     "aliases": ("nifty", "nifty 500", "nifty500", "india", "nse", "indian market"),
     "nse_list": "ind_nifty500list.csv", "size": 500},
    {"key": "NIFTY50", "label": "Nifty 50", "benchmark": "^NSEI", "region": "INDIA",
     "aliases": ("nifty 50", "nifty50", "nifty fifty"),
     "nse_list": "ind_nifty50list.csv", "size": 50},
    {"key": "NIFTYMID150", "label": "Nifty Midcap 150", "benchmark": "NIFTYMIDCAP150.NS", "region": "INDIA",
     "aliases": ("nifty midcap 150", "nifty midcap150", "midcap 150", "midcap150", "nifty midcap", "midcap",
                 "mid cap", "nifty mid cap 150", "mid cap 150"),
     "nse_list": "ind_niftymidcap150list.csv", "size": 150},
    {"key": "NIFTYSMALL250", "label": "Nifty Smallcap 250", "benchmark": "NIFTYSMLCAP250.NS", "region": "INDIA",
     "aliases": ("nifty smallcap 250", "nifty smallcap250", "smallcap 250", "smallcap250", "nifty smallcap",
                 "smallcap", "small cap", "nifty small cap 250", "small cap 250"),
     "nse_list": "ind_niftysmallcap250list.csv", "size": 250},
    {"key": "US", "label": "S&P 500", "benchmark": "SPY", "region": "US",
     "aliases": ("s&p", "s&p 500", "sp500", "spx", "s and p 500", "us", "us market")},
    {"key": "NASDAQ100", "label": "Nasdaq 100", "benchmark": "QQQ", "region": "US",
     "aliases": ("nasdaq", "nasdaq 100", "nasdaq100", "ndx")},
]

# Each region's exchange: timezone, regular-session open and close (hh, mm).
SESSIONS = {
    "INDIA": ("Asia/Kolkata", (9, 15), (15, 30)),
    "US": ("America/New_York", (9, 30), (16, 0)),
}

BY_KEY = {ix["key"]: ix for ix in INDEXES}
BY_LABEL = {ix["label"]: ix for ix in INDEXES}
KEYS = [ix["key"] for ix in INDEXES]
LABELS = [ix["label"] for ix in INDEXES]
# Every index's price series, once (dashboard_perf.json's "benchmarks").
BENCHMARK_TICKERS = [ix["benchmark"] for ix in INDEXES]


def label_list():
    """'Nifty 500, Nifty 50, ..., Nasdaq 100' -- for the connectors' tool descriptions."""
    return ", ".join(LABELS)
