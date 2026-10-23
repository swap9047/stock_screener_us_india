"""A daily bar Yahoo has not published yet is rebuilt from its 5-minute bars.

On 2026-10-22 most NSE/BSE stocks' 22 Oct daily row still held only Volume at 01:40 UTC the next
day, while the quote and the 5-minute bars had the session. Every consumer drops a row without a
close, so the dashboard and the research library read 21 Oct. fill_unpublished_daily_bars rebuilds
such a trailing bar once its session has settled, and official_close takes the close from the quote
(NSE/BSE's official close is a half-hour average, not the last trade).

Offline: invented tickers and frames, the downloads and yf.Ticker replaced, no network.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stock_data as sd  # noqa: E402

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def daily(tickers, days, blank_last=(), start=100.0):
    """A group_by="ticker" daily frame (naive dates), the last row Volume-only for `blank_last`."""
    idx = pd.bdate_range(end=days, periods=300)
    cols = pd.MultiIndex.from_product([tickers, FIELDS])
    frame = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for i, t in enumerate(tickers):
        close = start + i * 10 + np.arange(len(idx)) * 0.1
        frame[(t, "Open")], frame[(t, "High")], frame[(t, "Low")] = close - 0.5, close + 1, close - 1
        frame[(t, "Close")], frame[(t, "Volume")] = close, 1000.0
        if t in blank_last:
            for f in ("Open", "High", "Low", "Close"):
                frame.loc[idx[-1], (t, f)] = np.nan
            frame.loc[idx[-1], (t, "Volume")] = 6840.0
    return frame


def intraday(tickers, day, tz="Asia/Kolkata", bars=None):
    """5-minute bars for `day` in the exchange's timezone: closes 200 .. 205, highs up to 207."""
    idx = pd.date_range(f"{day} 09:15", f"{day} 15:25", freq="5min", tz=tz)
    n = len(idx)
    closes = np.linspace(200, 205, n)
    cols = pd.MultiIndex.from_product([tickers, FIELDS])
    frame = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for t in tickers:
        frame[(t, "Open")], frame[(t, "Close")] = closes - 0.2, closes
        frame[(t, "High")], frame[(t, "Low")], frame[(t, "Volume")] = closes + 2, closes - 3, 10.0
    return frame if bars is None else bars


NOW = datetime(2026, 10, 23, 1, 40, tzinfo=timezone.utc)   # 07:10 IST on the 23rd: the 22nd has settled

# --- the fill ---------------------------------------------------------------------------------------
raw = daily(["ZED.NS", "ACME.NS"], "2026-10-22", blank_last=["ZED.NS"])
asked = []
raw, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS", "ACME.NS"], now=NOW,
                                              download=lambda t: (asked.append(list(t)), intraday(t, "2026-10-22"))[1])
last = raw.index[-1]
bar = raw.loc[last, "ZED.NS"]
check(rebuilt == ["ZED.NS"] and asked == [["ZED.NS"]], "only the ticker with a blank trailing close is fetched and rebuilt")
check((bar["Open"], bar["High"], bar["Low"], bar["Close"]) == (199.8, 207.0, 197.0, 205.0),
      f"the bar is the session's 5-minute bars: first open, highest high, lowest low, last close ({dict(bar)})")
check(bar["Volume"] == 6840.0, "Yahoo's own daily volume is kept")
check(raw.loc[last, ("ACME.NS", "Close")] == 100 + 10 + 299 * 0.1, "a complete row is untouched")

raw = daily(["ZED.NS"], "2026-10-22", blank_last=["ZED.NS"])
raw.loc[raw.index[-1], ("ZED.NS", "Volume")] = np.nan
raw.loc[raw.index[-1], ("ZED.NS", "Open")] = np.nan
raw.loc[raw.index[-1], ("ZED.NS", "Close")] = np.nan
raw.loc[raw.index[-1], ("ZED.NS", "High")] = 1.0     # something to keep the row: only High
raw, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=NOW, download=lambda t: intraday(t, "2026-10-22"))
check(rebuilt == ["ZED.NS"] and raw.loc[raw.index[-1], ("ZED.NS", "Volume")] == 750.0,
      "without a daily volume, the session's 5-minute volumes are summed")

during = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)   # 13:30 IST: the session is still open
raw = daily(["ZED.NS"], "2026-10-22", blank_last=["ZED.NS"])
called = []
_, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=during, download=lambda t: called.append(t))
check(rebuilt == [] and called == [], "a session still forming is never filled (nothing is even fetched)")

raw = daily(["ZED.NS"], "2026-10-12", blank_last=["ZED.NS"])
_, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=NOW, download=lambda t: intraday(t, "2026-10-12"))
check(rebuilt == [], f"a blank more than {sd.UNPUBLISHED_BAR_MAX_DAYS} days old is not a publishing lag and is left alone")

raw = daily(["ZED.NS"], "2026-10-22", blank_last=["ZED.NS"])
_, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=NOW, download=lambda t: intraday(t, "2026-10-21"))
check(rebuilt == [] and pd.isna(raw.loc[raw.index[-1], ("ZED.NS", "Close")]),
      "no 5-minute bars for that date (a holiday row): the row stays as it was")


def boom(t):
    raise RuntimeError("Yahoo refused")


_, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=NOW, download=boom)
check(rebuilt == [], "a failed intraday download fills nothing and does not raise")
one = intraday(["ZED.NS"], "2026-10-22")["ZED.NS"]   # a plain frame, as an older yfinance returns for one ticker
raw = daily(["ZED.NS"], "2026-10-22", blank_last=["ZED.NS"])
_, rebuilt = sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=NOW, download=lambda t: one)
check(rebuilt == ["ZED.NS"], "a single ticker's flat frame is read too")

# --- the official close from the quote ----------------------------------------------------------------
raw = daily(["ZED.NS"], "2026-10-22", blank_last=["ZED.NS"])
sd.fill_unpublished_daily_bars(raw, ["ZED.NS"], now=NOW, download=lambda t: intraday(t, "2026-10-22"))
at_close = int(datetime(2026, 10, 22, 9, 59, 22, tzinfo=timezone.utc).timestamp())   # 15:29:22 IST
check(sd.official_close(raw, "ZED.NS", {"regularMarketPrice": 208.5, "regularMarketTime": at_close})
      and raw.loc[raw.index[-1], ("ZED.NS", "Close")] == 208.5 and raw.loc[raw.index[-1], ("ZED.NS", "High")] == 208.5,
      "the quote's price for that session becomes the close, and the high widens to include it")
day_before = int(datetime(2026, 10, 21, 9, 59, tzinfo=timezone.utc).timestamp())
check(not sd.official_close(raw, "ZED.NS", {"regularMarketPrice": 1.0, "regularMarketTime": day_before})
      and not sd.official_close(raw, "ZED.NS", {}), "a quote from another session, or none, changes nothing")

# --- through fetch_snapshot ---------------------------------------------------------------------------
class FakeTicker:
    def __init__(self, t):
        self.info = {"longName": "Zed Industries Limited", "regularMarketPrice": 208.5, "regularMarketTime": at_close}
        self.cash_flow = self.income_stmt = self.balance_sheet = self.quarterly_income_stmt = pd.DataFrame()


real = (sd._download_with_retries, sd._download_intraday, sd.yf.Ticker, sd.datetime)


class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


sd._download_with_retries = lambda tickers, period: daily(["ZED.NS", "^CRSLDX"], "2026-10-22", blank_last=["ZED.NS", "^CRSLDX"])
sd._download_intraday = lambda t: intraday(t, "2026-10-22")
sd.yf.Ticker = FakeTicker
sd.datetime = FixedNow
try:
    rows, _ = sd.fetch_snapshot(["ZED.NS"], benchmark="^CRSLDX", settings=dict(sd.DEFAULT_SETTINGS))
finally:
    sd._download_with_retries, sd._download_intraday, sd.yf.Ticker, sd.datetime = real
row = rows[0] if rows else {}
prev = 100 + 298 * 0.1
check(row.get("data_end") == "2026-10-22" and row.get("last_close") == 208.5,
      f"fetch_snapshot: the rebuilt day is the row's latest, closing at the quote's price ({row.get('data_end')}, {row.get('last_close')})")
check(row.get("pct_change_1d") == round((208.5 / prev - 1) * 100, 2) or row.get("pct_change_1d") == round((208.5 / prev - 1) * 100, 1),
      f"...and the day's change is against the previous close ({row.get('pct_change_1d')})")

print(f"\n{'FAILED' if fails else 'OK'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
