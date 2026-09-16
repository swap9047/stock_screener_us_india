"""push_json_entry_changes / read_remote_json against an in-memory Git Data API."""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_sync as gs
from fake_github import FakeGitHub, use

fails=0
def check(ok,label):
    global fails; fails+= not ok; print("PASS" if ok else "FAIL", label)

remote={"expert_views.json":{"AAA":{"verdict":"ACCUMULATE","as_of":"2026-09-14 20:00"},"BBB":{"verdict":"HOLD","as_of":"2026-09-13 10:00"}},
        "fundamentals.json":{"AAA":{"sentiment":"Positive","as_of":"2026-09-14 20:00"}},"watchlist.json":{"x":["AAA"]}}

fk=FakeGitHub(remote); use(fk)
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"CCC":{"v":1,"as_of":"2026-09-14 21:00"}}}},"m")
check(ok and fk.file("expert_views.json")["AAA"]["as_of"]=="2026-09-14 20:00" and "CCC" in fk.file("expert_views.json") and fk.file("watchlist.json")=={"x":["AAA"]}, f"new ticker added, others untouched: {msg}")

fk=FakeGitHub(remote); use(fk)
fk.on_patch=lambda: fk.commit_file("expert_views.json",{**fk.file("expert_views.json"),"DDD":{"by":"workflow"}})
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"CCC":{"v":1}},"delete":["BBB"]}},"m")
ev=fk.file("expert_views.json"); check(ok and "DDD" in ev and "CCC" in ev and "BBB" not in ev and fk.patches==2, "mid-push workflow commit retried and kept")

fk=FakeGitHub(remote); use(fk); start=fk.head
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"CCC":{"v":1}}},"fundamentals.json":{"set":{"CCC":{"s":1}}}},"m")
check(ok and fk.commits[fk.head]["parents"]==[start] and fk.commits_made==1, "two files, one commit")

fk=FakeGitHub({"expert_views.json":[1]}); use(fk)
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"CCC":{}}}},"m")
check(not ok and fk.patches==0, "non-object remote refused")

# --- newer_than_field (review fix) ---
fk=FakeGitHub(remote); use(fk); head0=fk.head
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"AAA":{"verdict":"CAUTION","as_of":"2026-09-10 08:00"}}}},"m",newer_than_field="as_of")
check(ok and fk.head==head0 and fk.commits_made==0 and m["expert_views.json"]["AAA"]["verdict"]=="ACCUMULATE", f"failed ticker's OLDER local entry not pushed, no commit, merged returns remote's newer value: {msg}")

fk=FakeGitHub(remote); use(fk)
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"AAA":{"verdict":"HOLD","as_of":"2026-09-14 22:00"},"BBB":{"verdict":"HOLD","as_of":"2026-09-01 00:00"}}}},"m",newer_than_field="as_of")
ev=fk.file("expert_views.json")
check(ok and ev["AAA"]["verdict"]=="HOLD" and ev["BBB"]["as_of"]=="2026-09-13 10:00" and "Pushed 1 entry" in msg, f"only the newer of two entries pushed: {msg}")

fk=FakeGitHub(remote); use(fk)
ok,msg,m=gs.push_json_entry_changes("t","o/r","main",{"expert_views.json":{"set":{"ZZZ":{"verdict":"HOLD","as_of":"2026-01-01 00:00"}}}},"m",newer_than_field="as_of")
check(ok and "ZZZ" in fk.file("expert_views.json"), "key absent on branch is pushed even if old")

fk=FakeGitHub(remote); use(fk)
check(gs.read_remote_json("t","o/r","main","fundamentals.json")==remote["fundamentals.json"] and gs.read_remote_json("t","o/r","main","nope.json") is None, "read_remote_json")
print("FAILURES:", fails); sys.exit(fails)
