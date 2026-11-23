"""Stocks the news digest could not search are searched again within the slot (owner, 2026-11-15).

Google's search model returned 500 for most calls on some days (138 failed calls and 16 of 38 stocks unsearched
on 2026-11-10, 9 on 11-13). With fewer than half failed the digest went out and those stocks were never searched
that day. Now such a run is marked retry_pending and exits 1, so the Worker's next hourly dispatch runs again: it
reuses every stock already searched, searches only the leftovers and, the digest being out, posts a short
follow-up for those with news instead of a second digest.

L1 First run, a few failed: the digest is posted, saved retry_pending + posted, exit 1.
L2 The retry: the leftover now has news -> a follow-up with only that stock is posted, no digest; exit 0 when
   nothing is left.
L3 The retry finds the leftover quiet -> nothing posted; one still failing -> exit 1 again.
L4 Half or more failed: nothing posted (as before); the retry that gets most through posts the FULL digest.
L5 A partial (smoke-test) run keeps the slot's flags; a new slot's run does not inherit them.
L6 reusable_outcomes reuses a retry_pending slot's searched stocks; build_followup_messages names only the late
   stocks with news.

Offline: invented tickers, the pipeline and Discord replaced.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import news_check as nc
import news_summary as ns

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def digest(statuses, as_of="2026-11-15"):
    """A news_summary.json-shaped dict for {ticker: status}; a material stock carries a note."""
    tickers = {t: ({"status": st, "note": f"**{t}** - news about {t}"} if st == "material" else {"status": st})
               for t, st in statuses.items()}
    counts = {s: sum(1 for x in statuses.values() if x == s) for s in ("material", "quiet", "degraded", "failed")}
    searched = sum(counts.values())
    return {"as_of": as_of, "totals": {**counts, "searched": searched},
            "markets": {"us_invested": {"summary": "* digest", "counts": counts, "tickers": tickers}}}


state = {"file": None}
sent = []
nc.get_gemini_api_key = lambda: "k"
nc.load_watchlists = lambda: {"us_invested": ["ACME", "ZED", "QUX", "ZORP"]}
nc.load_news_summary = lambda: state["file"]
nc.save_news_summary = lambda d: state.__setitem__("file", d)
nc.load_discord_webhook = lambda: "https://discord.example/hook"
nc.send_discord_batch = lambda hook, msgs, **kw: (sent.append(list(msgs)), (True, ""))[1]
for k in ("REFRESH_MARKETS", "REFRESH_LIMIT"):
    os.environ.pop(k, None)


def run(statuses, as_of="2026-11-15", env=None):
    nc.build_news_summary = lambda w, k, reuse_failed_run=True: digest(statuses, as_of)
    sent.clear()
    old = {k: os.environ.get(k) for k in ("REFRESH_MARKETS", "REFRESH_LIMIT")}
    os.environ.update(env or {"REFRESH_MARKETS": "", "REFRESH_LIMIT": ""})
    try:
        nc.main()
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return code, [m for batch in sent for m in batch]


# --- L1 ---------------------------------------------------------------------------------------------
code, msgs = run({"ACME": "material", "ZED": "quiet", "QUX": "quiet", "ZORP": "failed"})
f = state["file"]
check(code == 1 and any("digest" in m for m in msgs) and f["retry_pending"] and f["posted"] and not f["run_failed"],
      f"L1: 1 of 4 failed -> digest posted, saved retry_pending + posted, exit 1 ({code}, {f.get('retry_pending')}, {f.get('posted')})")
check(any("1 stock could not be checked yet" in m for m in msgs), "L1: ...and the digest says one is still to be checked")

# --- L2 ---------------------------------------------------------------------------------------------
code, msgs = run({"ACME": "material", "ZED": "quiet", "QUX": "quiet", "ZORP": "material"})
check(code == 0 and len(msgs) == 1 and "follow-up" in msgs[0] and "news about ZORP" in msgs[0]
      and "news about ACME" not in msgs[0] and "* digest" not in msgs[0],
      f"L2: the retry posts a follow-up with only the late stock, no second digest; exit 0 ({code}, {msgs})")
check(state["file"]["retry_pending"] is False and state["file"]["posted"] is True, "L2: ...and the slot is complete")

# --- L3 ---------------------------------------------------------------------------------------------
state["file"] = None
X = {"ONE": "quiet", "TWO": "quiet"}          # 2 of 6 failed: under half, so the digest goes out
run({"ACME": "quiet", "ZED": "failed", "QUX": "quiet", "ZORP": "failed", **X})
code, msgs = run({"ACME": "quiet", "ZED": "quiet", "QUX": "quiet", "ZORP": "failed", **X})
check(code == 1 and msgs == [] and state["file"]["retry_pending"] and state["file"]["posted"],
      f"L3: one leftover quiet, one still failing -> nothing posted, exit 1 to try again ({code}, {msgs})")
code, msgs = run({"ACME": "quiet", "ZED": "quiet", "QUX": "quiet", "ZORP": "material", **X})
check(code == 0 and len(msgs) == 1 and "news about ZORP" in msgs[0] and "ZED" not in msgs[0],
      "L3: the next retry gets the last one: only it is posted (the one found quiet earlier is not repeated)")

# --- L4 ---------------------------------------------------------------------------------------------
state["file"] = None
code, msgs = run({"ACME": "failed", "ZED": "failed", "QUX": "quiet", "ZORP": "material"})
check(code == 1 and msgs == [] and state["file"]["run_failed"] and not state["file"]["posted"],
      "L4: half failed -> nothing posted (as before), exit 1")
code, msgs = run({"ACME": "material", "ZED": "failed", "QUX": "quiet", "ZORP": "material"})
check(code == 1 and any("* digest" in m for m in msgs) and state["file"]["posted"] and state["file"]["retry_pending"],
      "L4: the retry that gets most through posts the FULL digest, and still leaves the last one pending")

# --- L5 ---------------------------------------------------------------------------------------------
before = dict(state["file"])
code, msgs = run({"ACME": "quiet"}, env={"REFRESH_LIMIT": "1", "REFRESH_MARKETS": ""})
check(msgs == [] and state["file"]["retry_pending"] == before["retry_pending"] and state["file"]["posted"] == before["posted"],
      "L5: a partial run posts nothing and keeps the slot's flags")
code, msgs = run({"ACME": "quiet", "ZED": "quiet", "QUX": "quiet", "ZORP": "quiet"}, as_of="2026-11-16")
check(code == 0 and any("* digest" in m for m in msgs) and state["file"]["posted"] and not state["file"]["retry_pending"],
      "L5: the next slot's run is a fresh digest, not a follow-up")

# --- L6 ---------------------------------------------------------------------------------------------
from datetime import date

prev = {**digest({"ACME": "material", "ZED": "quiet", "ZORP": "failed"}), "retry_pending": True, "posted": True}
reused = ns.reusable_outcomes(prev, date(2026, 11, 15))
check(set(reused) == {"ACME", "ZED"}, f"L6: a retry_pending slot's searched stocks are reused, the failed one is not ({sorted(reused)})")
check(ns.reusable_outcomes({**prev, "retry_pending": False}, date(2026, 11, 15)) == {}
      and ns.reusable_outcomes(prev, date(2026, 11, 16)) == {},
      "L6: nothing is reused from a complete digest or from another slot")
fu = ns.build_followup_messages(digest({"ACME": "material", "ZORP": "material", "ZED": "quiet"}), {"ZORP", "ZED"})
check(len(fu) == 1 and "news about ZORP" in fu[0] and "ACME" not in fu[0] and "follow-up" in fu[0],
      "L6: the follow-up names only the late stocks with news")
check(ns.build_followup_messages(digest({"ZED": "quiet"}), {"ZED"}) == [], "L6: late stocks with no news -> no message")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
