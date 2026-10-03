"""stock_data's snapshot fingerprint follows the CODE, not comments or docstrings.

It used to hash the raw bytes of stock_data.py, so a comment-only commit marked
the stored snapshot stale and every running app fetched live from Yahoo until
the next refresh (finding R8). It must also give the same answer on every
Python: the snapshot is stamped on Actions (3.11) and compared on Streamlit
Cloud and locally. EXPECTED below is pinned, so CI on 3.11 has to agree with
whatever machine pinned it (3.14 and 3.9 agreed when it was written).

Offline: a fixed sample source, no data files, no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stock_data as sd

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


SAMPLE = '''"""Module docstring."""
import math  # trailing comment

# full-line comment
RATE = 10


def ema(x, span=RATE):
    """Function docstring
    over two lines."""
    label = f"span={span} # not a comment"
    return x * 2 / (span + 1)  # why


class Calc:
    \'\'\'Class docstring.\'\'\'

    def run(self):
        "one-line docstring"
        return ema(3) + math.pi
'''
EXPECTED = "a5356ec71d8a4bd9"

fp = sd.code_text_fingerprint(SAMPLE)
check(fp == EXPECTED, f"the sample's fingerprint is pinned (same on every Python): got {fp}, want {EXPECTED}")

edits_same = {
    "a comment changed": SAMPLE.replace("# why", "# because the span is inclusive"),
    "a comment added": SAMPLE.replace("RATE = 10\n", "RATE = 10  # weeks\n"),
    "a docstring reworded": SAMPLE.replace("Function docstring\n    over two lines.", "Exponential moving average."),
    "blank lines and trailing spaces": SAMPLE.replace("\n\n\ndef ema", "\n\n\n\n\ndef ema").replace("RATE = 10\n", "RATE = 10   \n"),
}
for what, src in edits_same.items():
    check(sd.code_text_fingerprint(src) == fp, f"{what} -> same fingerprint (snapshot stays current)")

edits_differ = {
    "a constant changed": SAMPLE.replace("RATE = 10", "RATE = 11"),
    "a formula changed": SAMPLE.replace("(span + 1)", "(span + 2)"),
    "a non-docstring string changed": SAMPLE.replace("# not a comment", "# still not a comment"),
    "a statement added": SAMPLE.replace("        return ema(3)", "        x = 1\n        return ema(3)"),
}
for what, src in edits_differ.items():
    check(sd.code_text_fingerprint(src) != fp, f"{what} -> new fingerprint (snapshot marked out of date)")

real = (REPO / "stock_data.py").read_text(encoding="utf-8")
check(sd._code_fingerprint() == sd.code_text_fingerprint(real),
      "_code_fingerprint() is code_text_fingerprint of stock_data.py itself")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
