"""Trend, Tech Uptrend and Vol Trend: the 2026-10-04 review.

V1  The volume tests compare the MEDIAN day, not the mean. A results-day spike
    inflated the 100-day mean for months: one ticker's last 10 sessions were
    normal (~30k a day) against a 100-day mean of 288k and a median of 93k, so
    15 of 31 Uptrends failed Tech Uptrend on volume alone.
V2  Tech Uptrend's volume factor is 0.3 (owner's call).
B1  Neutral bands: a Trend condition too close to call (RS within +-1, 10W
    within 0.5% of 40W, 40W slope within 0.05%/week) does not vote, and
    Uptrend/Downtrend still need 3 voting conditions that all agree. Price vs
    the 40W always votes. RS -0.1 used to turn an otherwise clear Uptrend Mixed.
E1  Tech Uptrend compares unrounded prices (the stored VStop and 40W are rounded
    to 0.1, up to 1% on a 5-unit stock), and requires the VStop to point Up.

Offline: invented tickers and series, no network, no data files.
"""

import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def attr(name):
    f = getattr(sd, name, None)
    if f is None:
        check(False, f"stock_data.{name} exists")
    return f


# --- V1 the median day ---------------------------------------------------------
volume_stats = attr("volume_stats")
if volume_stats:
    spike = pd.Series([100.0] * 120)
    spike.iloc[-50] = 10_000.0                       # one results day, 50 sessions ago
    v = volume_stats(spike)
    check(v["avg_volume_100d"] == 199 and v["median_volume_100d"] == 100,
          f"V1: one spike lifts the 100D mean to 199, the median stays 100 ({v})")
    check(v["median_volume_10d"] == 100 and v["avg_volume_10d"] == 100, "V1: the last 10 sessions are a normal 100")
    check(v["avg_volume_20d"] == 100, "V1: the 20D mean (Volume Rocketing's baseline) is unchanged")
    short = volume_stats(pd.Series([100.0] * 50))
    check(short["median_volume_10d"] == 100 and short["median_volume_100d"] is None
          and short["avg_volume_100d"] is None, "V1: under 100 sessions, the 100D figures are None")

classify = attr("classify_volume_trend")
if classify:
    check(classify(100, 100, 1.4, 0.7) == "In-line", "V1: Vol Trend on equal medians is In-line")
    check(classify(150, 100, 1.4, 0.7) == "Exploding" and classify(70, 100, 1.4, 0.7) == "Declining",
          "V1: Exploding at >= 1.4x, Declining at <= 0.7x")
    check(classify(None, 100, 1.4, 0.7) is None and classify(10, 0, 1.4, 0.7) is None, "V1: no data -> None")

src = (REPO / "stock_data.py").read_text()
call = src[src.index("trend, trend_rank, trend_detail = compute_trend("):][:400]
check("median_volume_10d" in call and "median_volume_100d" in call, "V1: Trend's Strong test is given the medians")
check('"median_volume_10d": median_volume_10d' in src and '"median_volume_100d": median_volume_100d' in src,
      "V1: the medians are stored on the row (for the hover and Expert Take)")
check("classify_volume_trend(median_volume_10d, median_volume_100d" in src, "V1: Vol Trend is given the medians")

# --- V2 / E1 Tech Uptrend --------------------------------------------------------
tech = attr("compute_tech_uptrend")
if tech:
    base = dict(close=110.0, vstop=100.0, vstop_direction="Up", weeks_since=10, ema_slow=105.0,
                vol_10d=40.0, vol_100d=100.0, min_weeks=3, vol_ratio=0.3)

    def tu(**kw):
        return tech(**{**base, **kw})

    got, d = tu()
    check(got == 1 and d["passed"] == {"above_vstop": True, "vstop_up": True, "held": True,
                                        "above_slow": True, "volume": True},
          f"E1: all conditions -> 1, with each one in the detail ({got}, {d.get('passed')})")
    check(tu(close=10.92, vstop=10.94, ema_slow=10.0)[0] == 0,
          "E1: a close of 10.92 under a 10.94 stop fails (both read 10.9 rounded, which passed)")
    check(tu(close=10.92, vstop=10.91, ema_slow=10.0)[0] == 1, "E1: ...and 10.92 over 10.91 passes")
    check(tu(vstop_direction="Down")[0] == 0, "E1: a VStop pointing Down fails even with the close above it")
    check(tu(weeks_since=3)[0] == 0 and tu(weeks_since=4)[0] == 1, "held MORE than 3 weeks")
    check(tu(vol_10d=30.0)[0] == 0 and tu(vol_10d=31.0)[0] == 1, "V2: volume more than 0.3x the 100D median")
    check(tu(close=104.0)[0] == 0, "close above the slow WEMA")
    check(tu(vstop=None)[0] == 0 and tu(vol_100d=None)[0] == 0 and tu(weeks_since=None)[0] == 0,
          "missing data -> 0")
check(sd.DEFAULT_SETTINGS.get("tech_uptrend_volume_ratio") == 0.3, "V2: the default volume factor is 0.3")
check("tech_uptrend = compute_tech_uptrend(" in src or "tech_uptrend, tech_uptrend_detail = compute_tech_uptrend(" in src,
      "E1: the snapshot uses compute_tech_uptrend")

# --- B1 neutral bands --------------------------------------------------------------
rising = pd.Series([90.0, 92.0, 94.0, 96.0])            # ~2% a week
falling = pd.Series([110.0, 108.0, 106.0, 104.0])
flat_down = pd.Series([96.03, 96.02, 96.01, 96.0])       # -0.01% a week


def trend(close, slow, rs, fast):
    return sd.compute_trend(close, slow, rs, 200.0, 1.0, 1.0, 1.0, 3, near_high_low_pct=0.10,
                            volume_ratio=1.0, ema_fast=fast)


for args, want, why in (
    ((100, rising, -0.1, 98), "Uptrend", "B1: RS -0.1 does not vote; the other three agree -> Uptrend"),
    ((100, rising, -1.5, 98), "Mixed", "B1: RS -1.5 votes bearish -> Mixed"),
    ((100, rising, 2.0, 96.3), "Uptrend", "B1: 10W 0.3% over 40W does not vote; price, slope, RS agree -> Uptrend"),
    ((100, rising, 0.5, 96.3), "Mixed", "B1: 10W and RS both inside their bands -> 2 votes -> Mixed"),
    ((100, flat_down, 2.0, 98), "Uptrend", "B1: a flat 40W (-0.01%/wk) does not vote -> Uptrend"),
    ((96.01, rising, 2.0, 98), "Uptrend", "B1: price vs 40W always votes, however close"),
    ((100, falling, 0.5, 101), "Downtrend", "B1: Downtrend mirrors it: RS +0.5 does not vote"),
    ((100, falling, 2.0, 98), "Mixed", "B1: 2 up / 2 down is still Mixed"),
):
    got = trend(*args)[0]
    check(got == want, f"{why} (got {got})")
_, _, d = trend(100, rising, -0.1, 98)
check(d.get("rs_neutral") is True and d.get("ma_neutral") is False and d.get("slope_neutral") is False
      and d.get("votes") == 3, f"B1: the detail says which conditions sat out ({d.get('votes')})")
for key, val in (("trend_rs_neutral", 1.0), ("trend_ma_neutral_pct", 0.5), ("trend_slope_neutral_pct", 0.05)):
    check(sd.DEFAULT_SETTINGS.get(key) == val, f"B1: setting {key} defaults to {val}")
    check(key in sd.calc_settings(sd.DEFAULT_SETTINGS), f"B1: {key} is a calculation setting (it changes the label)")
call = src[src.index("trend, trend_rank, trend_detail = compute_trend("):][:600]
check("rs_neutral=" in call and "ma_neutral_pct=" in call and "slope_neutral_pct=" in call,
      "B1: the snapshot passes the band settings")

# --- the hover text reads the stored detail, so it cannot disagree with the label ----
import ast
app_src = (REPO / "app.py").read_text()
tree = ast.parse(app_src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tech_uptrend_tooltip")
body = ast.get_source_segment(app_src, fn)
check("tech_uptrend_detail" in body and "last_close > vstop" not in body,
      "E1: the Tech Uptrend hover reads the stored detail instead of re-testing rounded values")
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "vol_trend_tooltip")
check("median_volume_10d" in ast.get_source_segment(app_src, fn), "V1: the Vol Trend hover shows the medians")
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "trend_tooltip")
seg = ast.get_source_segment(app_src, fn)
check("rs_neutral" in seg and "median" in seg, "B1/V1: the Trend hover marks conditions that sat out, and says median")

# --- rows from a snapshot made before this change (the first hour after deploy) ---
# The app keeps showing the old snapshot until the next hourly refresh, and its
# rows carry neither tech_uptrend_detail nor the medians. Found rendering it.
import os
os.environ.setdefault("SKIP_GITHUB_PULL", "1")
ns = {}
for name in ("_mark", "tech_uptrend_tooltip", "vol_trend_tooltip"):
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
old_row = {"tech_uptrend": 1, "vstop_weekly": 100.0, "last_close": 110.0, "ema40": 105.0,
           "avg_volume_10d": 900, "avg_volume_100d": 1000, "volume_trend": "In-line"}
txt = ns["tech_uptrend_tooltip"](old_row, {}, {"w_slow": "40 WEMA"})
check("next data refresh" in txt and "Not enough data" not in txt,
      f"an old row's Tech Uptrend hover says the breakdown comes with the next refresh ({txt!r})")
txt = ns["vol_trend_tooltip"](old_row, {})
check("900" in txt and "Not enough" not in txt and "Average" in txt,
      f"an old row's Vol Trend hover falls back to the averages it was computed from ({txt!r})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
