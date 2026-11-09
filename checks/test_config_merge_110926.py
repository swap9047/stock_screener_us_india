"""A config push merges with a newer copy in the data repo instead of overwriting it (owner, 2026-11-09).

push_all_config pushed an edited config file WHOLE. The app pulls the data repo at most every 5 minutes, so a
commit that landed there in between (made by hand, or by another app instance) was silently undone by the
app's older copy plus its edit. Now a file whose data-repo copy moved since this container last pulled or
pushed it is merged three ways (json_merge.merge); where both sides changed the same item the app's edit
wins (owner's choice) and the sidebar says so.

M1 json_merge: one-sided changes, a watchlist edited on both sides, keys and rules added / edited / deleted,
   tab and column order, conflicts (app wins, named), unmergeable shapes, and a second merge changes nothing.
M2 push_all_config against a fake GitHub: a moved copy is merged, written back locally and recorded as the
   baseline (no re-push next run); a conflict is reported; an unmoved copy, a generated file and a file with
   no baseline are pushed as before; an older copy that cannot be read blocks the push and leaves it pending.
M3 The merge note: kept in the sync state (a pull does not wipe it), shown for MERGE_NOTE_SECONDS, drawn in the
   sidebar.

Offline: invented tickers and files in a temp folder.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "checks"))

import json_merge as jm

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- M1 json_merge ------------------------------------------------------------------------------------
base = {"india_watchlist": ["AAA.NS", "BBB.NS", "CCC.NS"], "us_watchlist": ["ZED", "QUX"]}
check(jm.merge(base, base, {**base, "us_watchlist": ["ZED"]}) == ({**base, "us_watchlist": ["ZED"]}, []),
      "M1: only the data repo changed -> its copy")
check(jm.merge(base, {**base, "us_watchlist": ["ZED"]}, base) == ({**base, "us_watchlist": ["ZED"]}, []),
      "M1: only the app changed -> its copy")
local = {**base, "india_watchlist": ["AAA.NS", "CCC.NS"]}                                   # app removed BBB
remote = {**base, "india_watchlist": ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS", "EEE.NS"]}     # repo added two
merged, conflicts = jm.merge(base, local, remote)
check(merged["india_watchlist"] == ["AAA.NS", "CCC.NS", "DDD.NS", "EEE.NS"] and conflicts == [],
      f"M1: one watchlist edited on both sides keeps the removal AND the additions ({merged['india_watchlist']})")
merged, _ = jm.merge(base, {**base, "us_watchlist": ["ZED", "QUX", "NEW"]}, {**base, "india_watchlist": base["india_watchlist"] + ["DDD.NS"]})
check(merged == {"india_watchlist": ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS"], "us_watchlist": ["ZED", "QUX", "NEW"]},
      "M1: different watchlists edited on each side -> both edits")
merged, _ = jm.merge(base, base, {**base, "new_tab": ["X"]})
check(list(merged) == ["india_watchlist", "us_watchlist", "new_tab"], "M1: a tab added in the data repo is kept")

notes_b = {"AAA.NS": {"note": "a", "flag": "Red"}, "BBB.NS": {"note": "b", "flag": None}}
notes_l = {"AAA.NS": {"note": "a2", "flag": "Red"}}                                          # app edited A, deleted B
notes_r = {"AAA.NS": {"note": "a", "flag": "Green"}, "BBB.NS": {"note": "b", "flag": None}, "CCC.NS": {"note": "c"}}
merged, conflicts = jm.merge(notes_b, notes_l, notes_r)
check(merged == {"AAA.NS": {"note": "a2", "flag": "Green"}, "CCC.NS": {"note": "c"}} and conflicts == [],
      f"M1: notes: two fields of one note edited on each side, a deletion and an addition all kept ({merged})")
merged, conflicts = jm.merge(notes_b, {"AAA.NS": notes_b["AAA.NS"]}, {**notes_b, "BBB.NS": {"note": "b2", "flag": None}})
check("BBB.NS" not in merged and conflicts == ["BBB.NS"],
      "M1: deleted in the app, edited in the data repo -> the app's deletion wins, named as a conflict")
merged, conflicts = jm.merge(notes_b, {**notes_b, "BBB.NS": {"note": "b3", "flag": None}}, {"AAA.NS": notes_b["AAA.NS"]})
check(merged["BBB.NS"]["note"] == "b3" and conflicts == ["BBB.NS"],
      "M1: edited in the app, deleted in the data repo -> the app's edit wins, named")
merged, conflicts = jm.merge({"rsi_period": 14, "x": 1}, {"rsi_period": 21, "x": 1}, {"rsi_period": 9, "x": 2})
check(merged == {"rsi_period": 21, "x": 2} and conflicts == ["rsi_period"],
      "M1: one setting set to two values -> the app's wins, named; the other setting is the data repo's")

rules_b = [{"id": "r1", "name": "One", "enabled": True}, {"id": "r2", "name": "Two", "enabled": True}]
rules_l = [{"id": "r1", "name": "One", "enabled": False}, {"id": "r3", "name": "Three"}]      # app: r1 off, r2 gone, r3 new
rules_r = [{"id": "r1", "name": "One!", "enabled": True}, {"id": "r2", "name": "Two", "enabled": True},
           {"id": "r4", "name": "Four"}]                                                     # repo: r1 renamed, r4 new
merged, conflicts = jm.merge(rules_b, rules_l, rules_r)
check(merged == [{"id": "r1", "name": "One!", "enabled": False}, {"id": "r3", "name": "Three"}, {"id": "r4", "name": "Four"}]
      and conflicts == [], f"M1: alert rules merge rule by rule ({[r['id'] for r in merged]})")

markets_b = {"a": {"label": "A"}, "b": {"label": "B"}, "c": {"label": "C"}}
markets_l = {"c": {"label": "C"}, "a": {"label": "A"}, "b": {"label": "B"}}                  # tabs reordered in the app
markets_r = {"a": {"label": "A"}, "b": {"label": "Bee"}, "c": {"label": "C"}}
merged, _ = jm.merge(markets_b, markets_l, markets_r)
check(list(merged) == ["c", "a", "b"] and merged["b"]["label"] == "Bee",
      "M1: the app's tab order and the data repo's rename are both kept")
merged, _ = jm.merge(["x", "y", "z"], ["x", "y", "z", "w"], ["z", "y", "x"])
check(merged == ["z", "y", "x", "w"], "M1: a list the data repo reordered keeps its order, the app's addition goes last")
merged, conflicts = jm.merge({"k": [[1, 2]]}, {"k": [[1, 3]]}, {"k": [[1, 4]]})
check(merged == {"k": [[1, 3]]} and conflicts == ["k"], "M1: a shape it cannot merge -> the app's value, named")
b2, l2, r2 = base, local, remote
m2, _ = jm.merge(b2, l2, r2)
check(jm.merge(b2, m2, r2) == (m2, []) and jm.merge(r2, m2, m2) == (m2, []),
      "M1: merging again changes nothing (a retried push is safe)")

# --- M2 push_all_config against a fake GitHub ------------------------------------------------------------
import github_sync as gs
from fake_github import FakeGitHub, use

saved_attrs = (gs.SCRIPT_DIR, gs.SYNC_STATE_FILE)
saved_requests, saved_sleep = gs.requests, gs.time.sleep
tmp = tempfile.mkdtemp()
gs.SCRIPT_DIR, gs.SYNC_STATE_FILE = tmp, os.path.join(tmp, ".data_sync_state.json")


def write(name, data):
    Path(tmp, name).write_text(json.dumps(data, indent=2))


def baseline(*names):
    """As if just pulled: the sync state knows each file's blob."""
    state = gs._read_sync_state()
    gs._write_sync_state({**state, "blobs": {**(state.get("blobs") or {}),
                                             **{n: gs._git_blob_sha(os.path.join(tmp, n)) for n in names}}})


try:
    write("watchlist.json", base)
    write("settings.json", {"rsi_period": 14, "x": 1})
    baseline("watchlist.json", "settings.json")
    fk = FakeGitHub({"watchlist.json": base, "settings.json": {"rsi_period": 14, "x": 1}})
    use(fk)
    fk.commit_file("watchlist.json", remote)            # a commit made in the data repo since the app pulled
    write("watchlist.json", local)                      # ...and the app's edit, made on its older copy
    pending = gs.unpushed_config_files()
    ok, msg = gs.push_all_config("t", "o/r", "main", filenames=pending, message="m")
    want = jm.merge(base, local, remote)[0]
    check(ok and fk.file("watchlist.json") == want, f"M2: the data repo gets the merge, not the app's copy ({msg})")
    check(json.loads(Path(tmp, "watchlist.json").read_text()) == want, "M2: the merged copy is written back locally")
    check(gs.unpushed_config_files() == [], "M2: ...and recorded as the baseline, so the next run pushes nothing")
    check("watchlist.json" in gs.recent_merge_note() and "Merged" in msg, "M2: the push says it merged")

    fk.commit_file("settings.json", {"rsi_period": 9, "x": 2})
    write("settings.json", {"rsi_period": 21, "x": 1})
    ok, msg = gs.push_all_config("t", "o/r", "main", filenames=gs.unpushed_config_files(), message="m")
    check(ok and fk.file("settings.json") == {"rsi_period": 21, "x": 2} and "settings.json (rsi_period)" in msg,
          f"M2: a setting changed on both sides -> the app's value, named in the note ({msg})")

    write("settings.json", {"rsi_period": 21, "x": 3})  # the data repo did not move this time
    ok, msg = gs.push_all_config("t", "o/r", "main", filenames=gs.unpushed_config_files(), message="m")
    check(ok and fk.file("settings.json") == {"rsi_period": 21, "x": 3} and "Merged" not in msg,
          "M2: an unmoved copy is pushed as it is, no merge")

    write("data_snapshot.json", {"generated_at": "2", "per_market": {"a": [1]}})
    baseline("data_snapshot.json")
    fk.commit_file("data_snapshot.json", {"generated_at": "1", "per_market": {"b": [2]}})
    ok, msg = gs.push_all_config("t", "o/r", "main", filenames=["data_snapshot.json"], message="m")
    check(ok and fk.file("data_snapshot.json") == {"generated_at": "2", "per_market": {"a": [1]}},
          "M2: a generated file (the snapshot) is never merged: pushed as before")

    write("custom_columns.json", [{"id": "c1"}])        # never pulled here: no baseline
    fk.commit_file("custom_columns.json", [{"id": "c9"}])
    ok, _ = gs.push_all_config("t", "o/r", "main", filenames=["custom_columns.json"], message="m")
    check(ok and fk.file("custom_columns.json") == [{"id": "c1"}],
          "M2: a file with no baseline is pushed whole (the manual button's job, as before)")

    write("ticker_notes.json", {"AAA.NS": {"note": "z"}})
    state = gs._read_sync_state()
    gs._write_sync_state({**state, "blobs": {**state["blobs"], "ticker_notes.json": "0" * 40}})   # an older copy GitHub lacks
    fk.commit_file("ticker_notes.json", {"BBB.NS": {"note": "y"}})
    ok, msg = gs.push_all_config("t", "o/r", "main", filenames=["ticker_notes.json"], message="m")
    check(not ok and fk.file("ticker_notes.json") == {"BBB.NS": {"note": "y"}}
          and "ticker_notes.json" in gs.unpushed_config_files() and "could not be merged" in msg,
          f"M2: an older copy that cannot be read -> nothing pushed over the data repo's, still pending ({msg[:70]}...)")

    # --- M3 the note ------------------------------------------------------------------------------------
    note = gs.recent_merge_note()
    gs.pull_generated_files("t", "o/r", "main", force=True)
    check(note and gs.recent_merge_note() == note, "M3: a pull keeps the merge note in the sync state")
    check(gs.recent_merge_note(now=gs._read_sync_state()["merge_note"]["at"] + gs.MERGE_NOTE_SECONDS + 1) == "",
          "M3: ...and it is shown for MERGE_NOTE_SECONDS only")
finally:
    gs.SCRIPT_DIR, gs.SYNC_STATE_FILE = saved_attrs
    gs.requests, gs.time.sleep = saved_requests, saved_sleep

APP = (REPO / "app.py").read_text()
i_note = APP.find("_merge_note = recent_merge_note()")
check(0 < i_note < APP.find('if st.session_state.get("_config_push_error"):') and "st.sidebar.info" in APP[i_note:i_note + 200],
      "M3: the sidebar shows the note, next to the failed-push warning")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
