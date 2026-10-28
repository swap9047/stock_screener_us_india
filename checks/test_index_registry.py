"""Nifty 50, Nifty Midcap 150 and Nifty Smallcap 250 (2026-10-28), and the one
index list everything reads (index_registry).

  X1  the registry: unique keys, labels and aliases; every region has a clock;
      the two keys that predate it (INDIA, US) keep their meaning;
  X2  the breadth job runs one leg per index, Nifty 500 ahead of the three
      Nifty indexes inside it and the S&P 500 ahead of Nasdaq 100, and a later
      leg takes the closes an earlier one already downloaded -- the Nifty legs
      download nothing new, a ticker that failed is not retried by the next leg,
      and a direct call outside main() is never served from that cache;
  X3  NSE's member lists: Yahoo symbols, and a list far shorter than the index
      is a broken download (raises), not a membership change;
  X4  every consumer reads the registry: the performance job's benchmark series,
      the research library's breadth series, Claude chat's names (a "nifty 50"
      is not "nifty 500") and its tool descriptions;

The News tab with these indexes is rendered by test_index_render.py.

Offline: invented tickers, stubbed downloads, no data files.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import zlib
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import index_registry as ir
import refresh_market_breadth as rmb

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- X1 ------------------------------------------------------------------------------
check(len(set(ir.KEYS)) == len(ir.KEYS) and len(set(ir.LABELS)) == len(ir.LABELS),
      f"X1: keys and labels are unique ({ir.KEYS})")
check(ir.BY_KEY["INDIA"]["label"] == "Nifty 500" and ir.BY_KEY["US"]["label"] == "S&P 500"
      and ir.BY_KEY["INDIA"]["benchmark"] == "^CRSLDX" and ir.BY_KEY["US"]["benchmark"] == "SPY",
      "X1: INDIA and US still mean Nifty 500 and S&P 500 (the stored history is keyed by them)")
check({"NIFTY50", "NIFTYMID150", "NIFTYSMALL250"} <= set(ir.KEYS)
      and ir.BY_KEY["NIFTY50"]["benchmark"] == "^NSEI"
      and ir.BY_KEY["NIFTYMID150"]["benchmark"] == "NIFTYMIDCAP150.NS"
      and ir.BY_KEY["NIFTYSMALL250"]["benchmark"] == "NIFTYSMLCAP250.NS",
      "X1: the three Nifty indexes, with the Yahoo tickers their prices come from")
check(all(ix["region"] in ir.SESSIONS for ix in ir.INDEXES), "X1: every index runs on a known exchange clock")
check(all(ix.get("nse_list") and ix.get("size") for ix in ir.INDEXES if ix["region"] == "INDIA"),
      "X1: every Indian index names its NSE member list and size")
_aliases = [a for ix in ir.INDEXES for a in ix["aliases"]]
check(len(_aliases) == len(set(_aliases)), "X1: no alias names two indexes")
check(len(set(ir.BENCHMARK_TICKERS)) == len(ir.BENCHMARK_TICKERS), "X1: one price series per index")

# --- X2 ------------------------------------------------------------------------------
IN_ALL = [f"IN{i}.NS" for i in range(12)]          # "Nifty 500"
IN_50 = IN_ALL[:4]
IN_MID = IN_ALL[4:8] + ["IN11.NS"]                 # IN11 fails to download
IN_SMALL = IN_ALL[8:11]
US_ALL = [f"U{i}" for i in range(6)]
US_NDX = US_ALL[:3] + ["NEWNDX"]                   # one Nasdaq name not in the S&P list
DAYS = pd.bdate_range(end="2026-10-23", periods=320)
BROKEN = {"IN11.NS"}


def _close(sym):
    rng = np.random.default_rng(zlib.crc32(sym.encode()))
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(DAYS)))), index=DAYS)


downloads = []


def fake_download(tickers, **kw):
    tickers = tickers if isinstance(tickers, list) else [tickers]
    downloads.extend(tickers)
    got = {t: _close(t) for t in tickers if t not in BROKEN}
    if not got:
        return pd.DataFrame()
    return pd.concat({"Close": pd.DataFrame(got)}, axis=1)


saved = (rmb.BREADTH_FILE, rmb.yf.download, rmb.time.sleep, rmb.get_sp500_tickers, rmb.get_nifty500_tickers,
         rmb.get_nasdaq100_tickers, rmb.get_nse_index_tickers, rmb.expected_session, rmb.is_live)
lists = {"ind_nifty50list.csv": IN_50, "ind_niftymidcap150list.csv": IN_MID, "ind_niftysmallcap250list.csv": IN_SMALL}
d = tempfile.mkdtemp()
try:
    rmb.BREADTH_FILE = os.path.join(d, "market_breadth.json")
    rmb.yf.download = fake_download
    rmb.time.sleep = lambda s: None
    rmb.get_sp500_tickers = lambda: list(US_ALL)
    rmb.get_nifty500_tickers = lambda: list(IN_ALL)
    rmb.get_nasdaq100_tickers = lambda: list(US_NDX)
    rmb.get_nse_index_tickers = lambda name, size=None: list(lists[name])
    rmb.expected_session = lambda tz, hhmm, **k: "2026-10-23"
    rmb.is_live = lambda day, tz, hhmm, **k: False
    order = []
    _real_calc = rmb.calculate_breadth

    def _spy(tickers, label, tz, hhmm):
        order.append((label, len(downloads)))
        return _real_calc(tickers, label, tz, hhmm)
    rmb.calculate_breadth = _spy
    rmb.main()
    rmb.calculate_breadth = _real_calc
    out = json.loads(Path(rmb.BREADTH_FILE).read_text())
    labels = [o[0] for o in order]
    check(set(out["status"]) == set(ir.KEYS) and all(v == "ok" for v in out["status"].values()),
          f"X2: one leg per registry index, each stored ({out['status']})")
    check(labels.index("Nifty 500") < min(labels.index(x) for x in ("Nifty 50", "Nifty Midcap 150", "Nifty Smallcap 250"))
          and labels.index("S&P 500") < labels.index("Nasdaq 100"),
          f"X2: the parent index runs before the indexes inside it ({labels})")
    per_leg = {}
    marks = [n for _, n in order] + [len(downloads)]
    for i, (label, n) in enumerate(order):
        per_leg[label] = downloads[n:marks[i + 1]]
    check(per_leg["Nifty 50"] == [] and per_leg["Nifty Smallcap 250"] == [],
          f"X2: Nifty 50 and Smallcap 250 download nothing -- Nifty 500's closes are reused ({per_leg['Nifty 50']})")
    check(per_leg["Nifty Midcap 150"] == [],
          f"X2: ...and a ticker that failed in the Nifty 500 leg is not retried by Midcap 150 ({per_leg['Nifty Midcap 150']})")
    check(per_leg["Nasdaq 100"] == ["NEWNDX"], f"X2: Nasdaq 100 downloads only the name the S&P 500 lacks ({per_leg['Nasdaq 100']})")
    check(out["markets"]["NIFTY50"]["total"] == 4 and out["markets"]["NIFTYMID150"]["total"] == 4
          and out["markets"]["INDIA"]["total"] == 11,
          f"X2: each index counts its own members ({out['markets']['NIFTY50']['total']}, "
          f"{out['markets']['NIFTYMID150']['total']}, {out['markets']['INDIA']['total']})")
    sub = pd.DataFrame({t: _close(t) for t in IN_50})
    from breadth_math import compute_breadth
    want = compute_breadth(sub, since=pd.Timestamp.now(tz=sub.index.tz) - pd.DateOffset(years=5))
    check(out["markets"]["NIFTY50"]["history"] == want["history"],
          "X2: a reused leg's breadth equals the same members computed on their own")
    check(rmb._RUN_CACHE is None, "X2: the cache ends with main()")
    downloads.clear()
    rmb.calculate_breadth(IN_50, "Direct", "Asia/Kolkata", (15, 30))
    check(sorted(downloads) == sorted(IN_50), "X2: a direct call downloads (never a stale cache)")
finally:
    (rmb.BREADTH_FILE, rmb.yf.download, rmb.time.sleep, rmb.get_sp500_tickers, rmb.get_nifty500_tickers,
     rmb.get_nasdaq100_tickers, rmb.get_nse_index_tickers, rmb.expected_session, rmb.is_live) = saved
    shutil.rmtree(d, ignore_errors=True)

# --- X3 ------------------------------------------------------------------------------
class _Resp:
    def __init__(self, text):
        self.text = text


saved_req = rmb.requests
try:
    seen = []
    rmb.requests = types.SimpleNamespace(get=lambda url, **k: seen.append(url) or _Resp(
        "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"Co {i},X,SYM{i} ,EQ,IN{i}\n" for i in range(50))))
    got = rmb.get_nse_index_tickers("ind_nifty50list.csv", 50)
    check(len(got) == 50 and got[0] == "SYM0.NS" and seen[0].endswith("/content/indices/ind_nifty50list.csv"),
          f"X3: NSE's list becomes Yahoo symbols, trimmed ({got[:2]})")
    try:
        rmb.get_nse_index_tickers("ind_niftysmallcap250list.csv", 250)
        check(False, "X3: a 50-name list for a 250-name index raises")
    except ValueError:
        check(True, "X3: a 50-name list for a 250-name index raises (the leg keeps its stored block)")
finally:
    rmb.requests = saved_req

# --- X4 ------------------------------------------------------------------------------
import publish_market
import refresh_dashboard_perf as rdp
check(rdp.BENCHMARK_SERIES == ir.BENCHMARK_TICKERS, "X4: the performance job stores every index's price series")
check(publish_market.INDEX_SERIES == {ix["key"]: ix["label"] for ix in ir.INDEXES},
      "X4: the research library gets every index's breadth")
_brec = publish_market.breadth_records(
    {"as_of": "x", "markets": {"NIFTYSMALL250": {"as_of": "x", "total": 240, "pct_above": 30.0,
                                                 "history": {"2026-10-23": 30.0}}}}, {}, [])
check([r["series"] for r in _brec] == ["NIFTYSMALL250"] and _brec[0]["label"] == "Nifty Smallcap 250",
      "X4: ...a new index's block is published under its key and label")
import market_lists
check(market_lists._index_series("Nifty 50") == "NIFTY50" and market_lists._index_series("nifty 500") == "INDIA"
      and market_lists._index_series("nifty") == "INDIA" and market_lists._index_series("smallcap 250") == "NIFTYSMALL250"
      and market_lists._index_series("Midcap") == "NIFTYMID150",
      "X4: Claude chat reads 'Nifty 50' as Nifty 50, 'nifty'/'nifty 500' as Nifty 500, 'midcap' as Midcap 150")
check(market_lists.INDEX_BENCHMARKS["NIFTYMID150"] == "NIFTYMIDCAP150.NS", "X4: an index name prices as its benchmark")
_mcp = (REPO / "api" / "mcp.py").read_text()
check(_mcp.count("index_registry.label_list()") >= 3 and "import index_registry" in _mcp
      and all(label in ir.label_list() for label in ir.LABELS),
      "X4: the connector's tool descriptions list every index")
_vi = (REPO / ".vercelignore").read_text()
check("index_registry" in _vi and "index_registry.py" not in _vi.split("\n", 3)[-1],
      "X4: index_registry ships with the Vercel functions")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
