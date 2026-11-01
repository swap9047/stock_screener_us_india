"""A late Stage Analysis upload, and the Stage reaching every Expert Take caller (2026-10-31).

Expert Take is Stage-led since 2026-10-31, and the Stage comes only from the weekly
TheWrap CSV upload. Owner, same day: a missing Stage gets no stand-in (Pending); a
late upload -- more than 10 days old for a stock the watchlists hold -- is flagged by
a red banner at the top of the app and a Discord post from the 9 PM ET alerts job,
whose gate counts it as due so the post goes out once per slot.

Offline: invented tickers, no network, no data files.
"""

import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import stage_analysis as sa

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


def entry(uploaded, stage="S2 Advancing"):
    return {"stage": stage, "stage_mom": "Strong ↑", "cheat_entry": "No", "pead": "No", "uploaded": uploaded}


# --- upload_status / late_upload_notice ------------------------------------------------
DATA = {"meta": {}, "tickers": {"ACME": entry("2026-10-28"), "ZED.NS": entry("2026-10-28"),
                                "GONE": entry("2026-09-01")}}
HELD = ["ACME", "ZED.NS", "ACME", "NEWCO"]          # a duplicate, and one with no entry
check(sa.STAGE_STALE_DAYS == 10, "late means more than 10 days old")
s = sa.upload_status(DATA, HELD, today=date(2026, 11, 7))
check(s["held"] == 3 and s["covered"] == 2 and s["stale"] == [] and s["days"] == 10,
      f"10 days old is not late yet; duplicates count once; GONE (in no watchlist) is ignored ({s})")
check(sa.late_upload_notice(s) == "", "...so there is no notice")
s = sa.upload_status(DATA, HELD, today=date(2026, 11, 8))
check(s["stale"] == ["ACME", "ZED.NS"] and s["days"] == 11 and s["oldest"] == date(2026, 10, 28),
      f"11 days old is late for both held stocks ({s})")
n = sa.late_upload_notice(s)
check(n.startswith("Stage Analysis upload is late: the Stage of every watchlist stock is more than 10 days old")
      and "28 Oct 2026, 11 days ago" in n and "Stage Analysis upload" in n,
      f"the notice says how late and what to do ({n})")
check("ACME" not in n and "ZED" not in n, "the notice names no ticker (the alerts job's log is public)")
part = {"meta": {}, "tickers": {"ACME": entry("2026-11-06"), "ZED.NS": entry("2026-10-20")}}
s = sa.upload_status(part, ["ACME", "ZED.NS"], today=date(2026, 11, 8))
check(s["stale"] == ["ZED.NS"] and "1 of 2 watchlist stocks" in sa.late_upload_notice(s),
      f"a partial upload flags only the stocks it left out ({sa.late_upload_notice(s)})")
s = sa.upload_status({"tickers": {}}, ["ACME"], today=date(2026, 11, 8))
check(s["stale"] == [] and sa.late_upload_notice(s) == "", "no entries at all: nothing is late (every verdict is Pending)")
s = sa.upload_status({"tickers": {"ACME": {"stage": "S2 Advancing", "uploaded": "soon"}}}, ["ACME"],
                     today=date(2026, 11, 8))
check(s["covered"] == 0 and not s["stale"], "an unreadable upload date is skipped, never raises")

import json
import tempfile
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "watchlist.json"
    p.write_text(json.dumps({"us_invested": ["ACME"], "india_watchlist": ["ZED.NS", "ACME"], "bad": "x"}))
    check(sa.watchlist_tickers(str(p)) == ["ACME", "ZED.NS", "ACME"], "watchlist_tickers reads every list")
    check(sa.watchlist_tickers(str(Path(d) / "missing.json")) == [], "...and a missing file holds none")

# --- alert_check posts it, once, through send_discord_batch ----------------------------
import alert_check as ac
posted = []
ac.load_discord_webhook = lambda: "https://discord.invalid/hook"
ac.send_discord_batch = lambda hook, msgs, **k: (posted.append(msgs), (True, ""))[1]
ac.load_watchlists = lambda: {"us_invested": ["ACME"], "india_watchlist": ["ZED.NS"]}
_real_load = sa.load_stage_analysis
sa.load_stage_analysis = lambda: {"meta": {}, "tickers": {"ACME": entry("2026-01-01"), "ZED.NS": entry("2026-01-01")}}
try:
    check(ac.post_late_stage_notice() is True and len(posted) == 1
          and posted[0][0].startswith("🔴 **Stage Analysis upload is late"),
          f"a late upload is posted to Discord through send_discord_batch ({posted})")
    posted.clear()
    sa.load_stage_analysis = lambda: {"meta": {}, "tickers": {"ACME": entry(str(date.today()))}}
    check(ac.post_late_stage_notice() is False and not posted, "a fresh upload posts nothing")
    sa.load_stage_analysis = lambda: (_ for _ in ()).throw(ValueError("torn file"))
    check(ac.post_late_stage_notice() is False, "a broken stage file never raises (the night's alerts still run)")
finally:
    sa.load_stage_analysis = _real_load
src = (REPO / "alert_check.py").read_text()
main_body = src.split("def main():", 1)[1]
check(main_body.lstrip().startswith("post_late_stage_notice()"),
      "alert_check.main posts the notice first, before it returns for no rules due")

# --- the gate counts a late upload as due -------------------------------------------------
wf = (REPO / ".github/workflows/daily-alerts.yml").read_text()
gate = wf.split("- id: check", 1)[1].split("check:\n", 1)[0]
check("late_upload_notice(upload_status(load_stage_analysis(), watchlist_tickers()))" in gate
      and "due or late" in gate, "daily-alerts.yml's gate counts a late upload as due")
check(not re.search(r"send_discord|requests\.post", gate), "...and posts nothing itself (it re-runs every hour)")

# --- the banner, at the top of the app, above the tabs ---------------------------------------
app = (REPO / "app.py").read_text()
i_banner, i_tabs = app.find("if _stage_late:\n    st.error(_stage_late"), app.find("all_tabs = st.tabs(")
check(0 < i_banner < i_tabs, "the app shows the notice in red (st.error) before st.tabs, i.e. at the top of the page")
check(app.find("stage_analysis_now = load_stage_analysis()") < i_banner,
      "...computed from the stage file the enrichment loop already loaded")

# --- the research library's publish recomputes the verdict with the fresh Stage -------------
import publish_market as pm
snap = {"generated_at": "2026-10-31T10:00:00Z", "per_market": {"us_invested": [
    {"ticker": "ACME", "company_name": "Acme", "last_close": 10.0, "ta_rules": "Maintain/Add", "trend": "Uptrend",
     "sentiment": "Positive", "stage": "S4 Decline", "stage_mom": "Weak ↓", "expert_take": "Caution"}]}}
recs = pm.screener_records(snap, {}, {}, settings={}, stage={"meta": {}, "tickers": {"ACME": entry("2026-10-28")}})
check(recs[0]["metrics"]["expert_take"] == "Accumulate" and recs[0]["metrics"]["stage"] == "S2 Advancing",
      f"publish_market takes the upload's Stage and recomputes Expert Take with it, not the snapshot's "
      f"(S4 Caution -> S2 Accumulate; got {recs[0]['metrics'].get('expert_take')})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
