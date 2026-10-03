"""Adding a ticker while Yahoo throttles must not drop it as a typo (finding N4).

Yahoo usually throttles by returning an EMPTY frame, not by raising, and an
empty frame used to read exactly like a misspelt symbol. validate_ticker now
retries, then asks a control symbol: empty for both = Yahoo is down (None,
accepted with a warning); data for the control = the ticker has none (False).

Offline: the yfinance call is replaced; no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pandas as pd

import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


DATA = pd.DataFrame({"Close": [1.0, 2.0]})
EMPTY = pd.DataFrame()


def yahoo(have=(), down=False, raises=False, flaky=None):
    calls = []

    def fetch(t):
        calls.append(t)
        if raises:
            raise ConnectionError("boom")
        if flaky and calls.count(t) < flaky:
            return EMPTY
        return EMPTY if down or t not in have else DATA
    return fetch, calls


f, _ = yahoo(have={"ACME"})
check(sd.validate_ticker("ACME", base_delay=0, _history=f) is True, "a ticker with prices is valid")

f, calls = yahoo(have={"SPY"})
check(sd.validate_ticker("ACMEE", base_delay=0, _history=f) is False, "a typo is rejected while Yahoo answers for the control")
check(calls.count("ACMEE") == 3 and "SPY" in calls, "...after 3 attempts and one control lookup")

f, _ = yahoo(down=True)
check(sd.validate_ticker("ACME", base_delay=0, _history=f) is None,
      "Yahoo returning nothing for anyone -> None (could not tell), not False")

f, _ = yahoo(have={"ACME"}, flaky=2)
check(sd.validate_ticker("ACME", base_delay=0, _history=f) is True, "a first empty answer that recovers on retry -> valid")

f, _ = yahoo(raises=True)
check(sd.validate_ticker("ACME", base_delay=0, _history=f) is None, "an exception still fails open (None)")

src = (REPO / "app.py").read_text()
check("elif ok is None:" in src and "Added anyway" in src, "the app keeps a ticker it could not check, with a warning")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
