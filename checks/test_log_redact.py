"""log_redact.py masks holdings in this public repo's Actions logs.

Every fixture here is invented (ACME, ZED.NS, ...) -- the point of the filter is
that real symbols never reach a log, so they must not reach this file either.
"""

import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import json
import os
import subprocess
import tempfile

import log_redact as lr

fails = 0
def check(ok, label):
    global fails; fails += not ok; print("PASS" if ok else "FAIL", label)

d = tempfile.mkdtemp()
json.dump({"us_picks": ["ACME", "ON"], "in_picks": ["ZED.NS", "A&B.NS"]}, open(f"{d}/watchlist.json", "w"))
json.dump(["KXAB"], open(f"{d}/interested.json", "w"))
json.dump({"YYY.BO": {"note": "x"}}, open(f"{d}/ticker_notes.json", "w"))
json.dump({"WIDG": {"index": "S&P 500", "benchmark": "SPY"}}, open(f"{d}/ticker_index.json", "w"))
json.dump({"per_market": {"us_picks": [{"ticker": "ACME", "company_name": "Acme Industries, Inc."}]}},
          open(f"{d}/data_snapshot.json", "w"))
json.dump({"us_picks": {"label": "US Picks"}, "newsletter_picks": {"label": "Newsletter Picks"}},
          open(f"{d}/markets.json", "w"))

pats = lr.build_patterns(d)
r = lambda s: lr.redact(s, pats)

check(r("Too little history: ZED.NS (1)") == "Too little history: [ticker] (1)", "suffixed ticker")
check(r("fetching ZED now") == "fetching [ticker] now", "bare symbol of 3+ chars")
check(r("ACME, KXAB and YYY.BO") == "[ticker], [ticker] and [ticker]", "watchlist, interested and notes keys")
check(r("A&B.NS failed") == "[ticker] failed", "symbols containing &")
check(r("Analyzing Acme Industries, Inc. today") == "Analyzing [company] today", "company name")
check(r("WIDG") == "[ticker]", "ticker_index keys")
check(r("SPY vs S&P 500 ETF, ACMEX") == "SPY vs S&P 500 ETF, ACMEX", "benchmarks and longer tokens untouched")
check(r("[ON] moved") == "[[ticker]] moved", "short symbol masked as its own token")
check(r("ONE MONTH") == "ONE MONTH", "short symbol not masked inside a word")
check(r("31 newsletter_picks + 2 us tickers; tab Newsletter Picks")
      == "31 [watchlist] + 2 us tickers; tab [watchlist]", "watchlist keys and labels (>=4 chars)")

out = subprocess.run(f"cd {d} && printf 'ZED.NS\\nok\\n' | {sys.executable} -u {REPO}/log_redact.py",
                     shell=True, capture_output=True, text=True)
check(out.stdout == "[ticker]\nok\n", f"pipe mode: {out.stdout!r}")
out = subprocess.run(f"cd {d} && set -o pipefail && (echo ACME; exit 3) | {sys.executable} -u {REPO}/log_redact.py",
                     shell=True, executable="/bin/bash", capture_output=True, text=True)
check(out.returncode == 3 and out.stdout == "[ticker]\n", "exit status survives the pipe with pipefail")
empty = tempfile.mkdtemp()
out = subprocess.run(f"cd {empty} && echo hello | {sys.executable} -u {REPO}/log_redact.py",
                     shell=True, capture_output=True, text=True)
check(out.returncode == 0 and "hello" in out.stdout, "no data files -> passthrough, no crash")

# --- a Gemini key printed on its own (one secret may hold every key joined by commas, GEMINI_API_KEYS)
K1, K2, K3 = "AIzaFAKEKEYONE000000000000000000001", "AIzaFAKEKEYTWO000000000000000000002", "AIzaFAKEKEYTHREE0000000000000000003"
kp = lr.key_patterns({"GEMINI_API_KEYS": f" {K1} ,{K2},\n {K3},,", "GEMINI_API_KEY_BACKUP_B": "AIzaSEPARATE00000000000000000000009",
                      "GROQ_API_KEY": "gsk_notagemini00000000", "GEMINI_MODEL": "gemini-3.5-flash-lite", "GEMINI_API_KEY_B2": "short"})
kr = lambda s: lr.redact(s, kp)
check(kr(f"400 key {K2} is not valid") == "400 key [key] is not valid", "a key of a joined secret printed on its own is masked")
check(all(k not in kr(f"{K1},{K2},{K3}") for k in (K1, K2, K3)) and kr(f"{K1},{K2}") == "[key],[key]",
      "...each key of the list, and the whole joined value")
check(kr("with AIzaSEPARATE00000000000000000000009 too") == "with [key] too", "a separate GEMINI_API_KEY* variable is masked too")
check("gsk_notagemini00000000" not in str(kp) and kr("gemini-3.5-flash-lite short") == "gemini-3.5-flash-lite short",
      "other variables, model names and values under 8 characters are left alone")
check(lr.key_patterns({}) == [] and lr.key_patterns({"GEMINI_API_KEYS": None, "X": 1}) == [] and lr.key_patterns(None) is not None,
      "no keys, or an odd environment, gives no patterns and never raises")
out = subprocess.run(["bash", "-c", f"echo 'err {K2} and ZED.NS' | python3 {REPO}/log_redact.py"], capture_output=True, text=True, cwd=d,
                     env={**os.environ, "GEMINI_API_KEYS": f"{K1},{K2}"})
check(out.stdout == "err [key] and [ticker]\n" and K2 not in out.stdout, f"pipe mode: a key and a ticker masked together: {out.stdout!r}")
empty = tempfile.mkdtemp()
out = subprocess.run(["bash", "-c", f"echo 'err {K2}' | python3 {REPO}/log_redact.py"], capture_output=True, text=True, cwd=empty,
                     env={**os.environ, "GEMINI_API_KEYS": f"{K1},{K2}"})
check("err [key]" in out.stdout and K2 not in out.stdout and "no data files found" in out.stdout,
      "with no data files the keys are still masked, and the notice about the data is still given")
print("FAILURES:", fails); sys.exit(fails)
