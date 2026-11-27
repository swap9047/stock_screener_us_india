"""The daily Gemini calls per key, shared with the transcription app (owner, 2026-11-26).

Every request sent to Google is counted against the key that carried it and the model, per Google quota day (Pacific), and
added to ONE table in the research library's database (kg_key_usage) by this repo's jobs, its dashboard and the transcription
app, so a table of "calls today" per key shows all of them. A key is only ever a short fingerprint.

U1  the quota day (Pacific, both clock changes) and the key fingerprint give the same answers as the transcription app's copies
U2  a key picked for a call is counted under (day, fingerprint, model without "models/"): every attempt, failures included
U3  the flush: by the database when MARKET_DB_URL is set, else through the Vercel function, else dropped; a failed send keeps the
    counts for the next try; a recent send is skipped when asked; the exit hook is registered once; the log line has counts only
U4  MarketDB.add_key_usage ADDS to the stored count in key order and skips bad rows; key_usage reads one day
U5  api/market.py, over real HTTP: GET ?usage= and POST {"key_usage": ...} need the token, validate every row and stay within limits
U6  the table both apps show is built the same way
U7  the four AI workflows install psycopg and give the AI step (only) the database address
"""

import contextlib
import datetime
import http.server
import importlib.util
import io
import json
import os
import re
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


for k in [k for k in os.environ if k.startswith(("GEMINI", "MARKET_"))]:
    del os.environ[k]

import llm_util  # noqa: E402
import market_lookup  # noqa: E402

# --- U1 -----------------------------------------------------------------------------------------------------------------
U = datetime.timezone.utc
check([llm_util.quota_day(datetime.datetime(*t, tzinfo=U)) for t in ((2026, 11, 26, 7, 59), (2026, 11, 26, 8, 0), (2026, 7, 1, 6, 59),
                                                                     (2026, 7, 1, 7, 0))] == ["2026-11-25", "2026-11-26", "2026-06-30", "2026-07-01"],
      "U1: the quota day is Pacific: it turns at 08:00 UTC in winter and 07:00 UTC in summer")
check(llm_util.key_fingerprint("AIzaSyDUMMY0000") == "b1264487" == llm_util.key_fingerprint(" AIzaSyDUMMY0000\n")
      and "AIza" not in llm_util.key_fingerprint("AIzaSyDUMMY0000"),
      "U1: a key's fingerprint is its SHA-256's first 8 hex digits (the transcription app's test holds its copy to the same value)")


def reset(atexit_registered=True):
    llm_util._USAGE.clear()
    llm_util._USAGE_STATE.update(atexit=atexit_registered, flushed_at=None)


# --- U2 -----------------------------------------------------------------------------------------------------------------
reset()
os.environ.update({"GEMINI_API_KEY": "AIzaSyDUMMY0000", "GEMINI_API_KEYS_X": "", "GEMINI_API_KEY_BACKUP": "AIzaSyDUMMY0001"})
client = llm_util.make_client()
served = []


class Stub:
    def __init__(self, key):
        self.key = key
        self.models = types.SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        served.append(self.key)
        if self.key == "AIzaSyDUMMY0001":
            raise RuntimeError("500 INTERNAL")
        return types.SimpleNamespace(text="ok", usage_metadata=None)


client._client_for = lambda key: Stub(key)
day = llm_util.quota_day()
for _ in range(60):
    try:
        client.models.generate_content(model="models/gemma-9-test", contents="c")
    except RuntimeError:
        pass
counts = dict(llm_util._USAGE)
fp0, fp1 = llm_util.key_fingerprint("AIzaSyDUMMY0000"), llm_util.key_fingerprint("AIzaSyDUMMY0001")
check(set(counts) == {(day, fp0, "gemma-9-test"), (day, fp1, "gemma-9-test")} and sum(counts.values()) == len(served) == 60
      and served.count("AIzaSyDUMMY0000") == counts[(day, fp0, "gemma-9-test")],
      f"U2: every attempt is counted under the day, the key's fingerprint and the model without 'models/' (failed ones too): {counts}")
reset()
llm_util.count_key_call("", "m"); llm_util.count_key_call(None, "m"); llm_util.count_key_call("k", "models/a/b", 2)
check(dict(llm_util._USAGE) == {(day, llm_util.key_fingerprint("k"), "a/b"): 2},
      "U2: no key counts nothing; only a leading 'models/' is stripped")
registered = []
_real_register = llm_util.atexit.register
llm_util.atexit.register = lambda fn, *a, **k: registered.append(fn)
reset(atexit_registered=False)
llm_util.count_key_call("k", "m"); llm_util.count_key_call("k", "m")
llm_util.atexit.register = _real_register
check(registered == [llm_util.flush_key_usage], "U2: the first count registers the flush at exit, once")


# --- U3 -----------------------------------------------------------------------------------------------------------------
class FakeDB:
    instances = []

    def __init__(self):
        self.added = []
        FakeDB.instances.append(self)

    def add_key_usage(self, rows):
        self.added.append(list(rows))
        return len(rows)

    def key_usage(self, day):
        return [{"key_id": "aaaa1111", "model": "m", "calls": 3}] if day == "2026-11-20" else []

    def close(self):
        pass


market_lookup.MarketDB.from_env = classmethod(lambda cls: (FakeDB(), ""))


def flushed(**kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = llm_util.flush_key_usage(**kw)
    return ok, buf.getvalue()


os.environ["MARKET_DB_URL"] = "postgresql://writer:pw@db.example/postgres"
reset(); llm_util.count_key_call("AIzaSyDUMMY0000", "models/gemma-9-test", 5); llm_util.count_key_call("AIzaSyDUMMY0001", "gemma-9-test", 2)
FakeDB.instances.clear()
ok, out = flushed()
check(ok and FakeDB.instances[0].added == [sorted([(day, fp0, "gemma-9-test", 5), (day, fp1, "gemma-9-test", 2)])] and not llm_util._USAGE
      and "7 call(s) added" in out, "U3: with MARKET_DB_URL the counts go to the database in one add and the pending ones are emptied")
check("AIza" not in out and fp0 not in out and "pw" not in out and "db.example" not in out,
      "U3: the log line (this repo's logs are public) has a count only: no key, fingerprint or address")
reset(); llm_util.count_key_call("k", "m")
llm_util._USAGE_STATE["flushed_at"] = __import__("time").monotonic()
ok_skip, _ = flushed(min_interval=300)
check(not ok_skip and llm_util._USAGE, "U3: a send made within min_interval is skipped and the counts wait")
check(flushed()[0] and not llm_util._USAGE, "U3: ...and without min_interval it sends")
check(flushed() == (False, ""), "U3: nothing pending, nothing sent, nothing printed")

posts = []


class Resp:
    def __init__(self, status): self.status_code = status


def fake_post(url, json=None, timeout=None, headers=None):
    posts.append((url, json, headers))
    return Resp(fake_post.status)


fake_post.status = 200
import requests  # noqa: E402
_real_post = requests.post
requests.post = fake_post
del os.environ["MARKET_DB_URL"]
os.environ.update({"MARKET_API_URL": "https://usage.example/api/market/", "MARKET_API_TOKEN": "tok-test"})
reset(); llm_util.count_key_call("AIzaSyDUMMY0000", "gemma-9-test", 3)
ok, out = flushed()
check(ok and posts[-1][0] == "https://usage.example/api/market" and posts[-1][1] == {"key_usage": [[day, fp0, "gemma-9-test", 3]]}
      and posts[-1][2] == {"Authorization": "Bearer tok-test"} and "tok-test" not in out,
      f"U3: without a database address the counts are POSTed to the Vercel function with the token, which the log never shows: {posts[-1:]}")
fake_post.status = 500
reset(); llm_util.count_key_call("AIzaSyDUMMY0000", "gemma-9-test", 3)
ok, out = flushed()
check(not ok and "500" in out and dict(llm_util._USAGE) == {(day, fp0, "gemma-9-test"): 3},
      "U3: a send that fails is said so (the status only) and the counts are kept for the next try")
requests.post = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no route to host"))
ok, out = flushed()
check(not ok and "ConnectionError" in out and "no route" not in out and dict(llm_util._USAGE) == {(day, fp0, "gemma-9-test"): 3},
      "U3: an endpoint that doesn't answer keeps them too, naming the error's class only")
reset()
for i in range(llm_util.KEY_USAGE_PENDING_MAX + 50):
    llm_util._USAGE[(day, f"{i:08x}", "m")] = 1
before = len(llm_util._USAGE)
flushed()
check(len(llm_util._USAGE) == llm_util.KEY_USAGE_PENDING_MAX and before == llm_util.KEY_USAGE_PENDING_MAX + 50,
      "U3: while nothing can be reached at most KEY_USAGE_PENDING_MAX entries are kept")
requests.post = _real_post
for k in ("MARKET_API_URL", "MARKET_API_TOKEN"):
    os.environ.pop(k, None)
reset(); llm_util.count_key_call("k", "m")
ok, out = flushed()
check(not ok and not llm_util._USAGE and "no MARKET_DB_URL" in out,
      "U3: with nowhere to send them the counts are dropped (and the log says what is missing), never piled up")
os.environ["MARKET_DB_URL"] = "postgresql://writer:pw@db.example/postgres"
market_lookup.MarketDB.from_env = classmethod(lambda cls: (None, "database unavailable (OperationalError)"))
reset(); llm_util.count_key_call("k", "m")
ok, out = flushed()
check(not ok and "database unavailable (OperationalError)" in out and llm_util._USAGE,
      "U3: a database that doesn't answer keeps the counts (it said what failed, by class)")
del os.environ["MARKET_DB_URL"]


# --- U4 -----------------------------------------------------------------------------------------------------------------
class Cur:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def executemany(self, sql, batch): self.log.append((sql, list(batch)))
    def execute(self, sql, params=()):
        self.log.append((sql, params))
        self.rows = [("aaaa1111", "m", 3)]
        self.description = [("key_id",), ("model",), ("calls",)]
    def fetchall(self): return self.rows


class Conn:
    def __init__(self): self.log = []; self.in_tx = False
    def transaction(self):
        conn = self
        class T:
            def __enter__(s): conn.in_tx = True
            def __exit__(s, *a): conn.in_tx = False; return False
        return T()
    def cursor(self): return Cur(self.log)


conn = Conn()
db = market_lookup.MarketDB(conn)
added = db.add_key_usage([("2026-11-26", "bbbb2222", "m2", 1), ("2026-11-26", "aaaa1111", "m1", 3), ("2026-11-26", "aaaa1111", "m1", 2),
                          ("2026-11-26", "x", "m", 0), ("2026-11-26", "x", "m", "oops"), ("2026-11-26", "x", "m", -1)])
sql, batch = conn.log[0]
check(added == 2 and [b[:4] for b in batch] == [("2026-11-26", "aaaa1111", "m1", 5), ("2026-11-26", "bbbb2222", "m2", 1)]
      and "ON CONFLICT (day, key_id, model) DO UPDATE SET calls = kg_key_usage.calls + EXCLUDED.calls" in sql
      and re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", batch[0][4]),
      "U4: counts ADD to the stored row (the SQL increments), in key order, with zero, negative and non-numeric counts skipped")
check(db.add_key_usage([("d", "k", "m", 0)]) == 0 and len(conn.log) == 1, "U4: nothing to add runs no statement")
check(db.key_usage("2026-11-26") == [{"key_id": "aaaa1111", "model": "m", "calls": 3}] and "WHERE day = %s" in conn.log[-1][0],
      "U4: key_usage reads one quota day")

# --- U5 -----------------------------------------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("api_market_usage", REPO / "api" / "market.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
os.environ["MARKET_API_TOKEN"] = "tok-test"
FakeDB.instances.clear()
api.market_lookup.MarketDB.from_env = classmethod(lambda cls: (FakeDB(), ""))
server = http.server.HTTPServer(("127.0.0.1", 0), api.handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/api/market"


def call(method, query="", body=None, token="tok-test", raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + query, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


check(call("GET", "?usage=today", token=None)[0] == 401 and call("POST", body={"key_usage": [["2026-11-26", "aaaa1111", "m", 1]]}, token=None)[0] == 401
      and call("GET", "?usage=today", token="wrong")[0] == 401, "U5: both the read and the add need the token")
code, body = call("GET", "?usage=2026-11-20")
check(code == 200 and body == {"day": "2026-11-20", "quota_tz": "America/Los_Angeles", "rows": [{"key_id": "aaaa1111", "model": "m", "calls": 3}]},
      f"U5: GET ?usage=<day> returns that quota day's rows: {body}")
code, body = call("GET", "?usage=today")
check(code == 200 and body["day"] == llm_util.quota_day() and body["rows"] == [], "U5: ?usage=today is today's Pacific quota day")
check(call("GET", "?usage=garbage")[0] == 400 and call("GET", "?usage=" + urllib.parse.quote("2026-11-26'; DROP TABLE x"))[0] == 400, "U5: a day that is not a date is refused")
FakeDB.instances.clear()
code, body = call("POST", body={"key_usage": [["2026-11-26", "aaaa1111", "gemma-9", 4], ["2026-11-26", "bbbb2222", "gemma-9", 1]]})
check(code == 200 and body == {"added": 2} and FakeDB.instances[0].added == [[("2026-11-26", "aaaa1111", "gemma-9", 4), ("2026-11-26", "bbbb2222", "gemma-9", 1)]],
      f"U5: a valid POST adds its rows: {body}")
good = ["2026-11-26", "aaaa1111", "gemma-9", 1]
bad_rows = {"a key id that is not 8 hex": ["2026-11-26", "AAAA1111", "m", 1], "a key id of the wrong length": ["2026-11-26", "aaaa", "m", 1],
            "a real key where the id goes": ["2026-11-26", "AIzaSyDUMMY0000", "m", 1], "a date that is not a date": ["yesterday", "aaaa1111", "m", 1],
            "a count of zero": ["2026-11-26", "aaaa1111", "m", 0], "a negative count": ["2026-11-26", "aaaa1111", "m", -3],
            "a count as text": ["2026-11-26", "aaaa1111", "m", "5"], "a count that is true": ["2026-11-26", "aaaa1111", "m", True],
            "a huge count": ["2026-11-26", "aaaa1111", "m", 10 ** 9], "an empty model": ["2026-11-26", "aaaa1111", "", 1],
            "a model name of 81 characters": ["2026-11-26", "aaaa1111", "m" * 81, 1], "three fields": ["2026-11-26", "aaaa1111", "m"]}
refused = {name: call("POST", body={"key_usage": [good, row]})[0] for name, row in bad_rows.items()}
check(all(c == 400 for c in refused.values()), f"U5: every kind of bad row refuses the WHOLE post: {refused}")
FakeDB.instances.clear()
check(not FakeDB.instances or all(not i.added for i in FakeDB.instances), "U5: ...and writes nothing")
check(call("POST", body={"key_usage": []})[0] == 400 and call("POST", body={"key_usage": "x"})[0] == 400 and call("POST", body=[1])[0] == 400
      and call("POST", body={"other": 1})[0] == 400, "U5: an empty list, a non-list or another shape is refused")
check(call("POST", body={"key_usage": [good] * (api.KEY_USAGE_MAX_ROWS + 1)})[0] == 400 and call("POST", body={"key_usage": [good] * api.KEY_USAGE_MAX_ROWS})[0] == 200,
      "U5: at most KEY_USAGE_MAX_ROWS rows a post")
check(call("POST", raw=b"{not json")[0] == 400 and call("POST", raw=b"")[0] == 400 and call("POST", raw=b" " * (api.POST_MAX_BYTES + 1))[0] == 413,
      "U5: a body that is not JSON, is empty, or is over the size limit is refused")
api.market_lookup.MarketDB.from_env = classmethod(lambda cls: (None, "database unavailable (OperationalError)"))
check(call("POST", body={"key_usage": [good]})[0] == 503 and call("GET", "?usage=today")[0] == 503, "U5: no database answers 503, with its error class")
server.shutdown()
del os.environ["MARKET_API_TOKEN"]

# --- U6 -----------------------------------------------------------------------------------------------------------------
pool = ["AIzaSyDUMMY0000", "AIzaSyDUMMY0001"]
fp = llm_util.key_fingerprint
table = llm_util.key_usage_table([{"key_id": fp(pool[1]), "model": "m-small", "calls": 2}, {"key_id": fp(pool[0]), "model": "m-big", "calls": 7},
                                  {"key_id": fp(pool[0]), "model": "m-small", "calls": 1}, {"key_id": "deadbeef", "model": "m-big", "calls": 4}], pool)
check(table == [{"Key": f"key 1 · {fp(pool[0])}", "m-big": 7, "m-small": 1, "Total": 8}, {"Key": f"key 2 · {fp(pool[1])}", "m-big": 0, "m-small": 2, "Total": 2},
                {"Key": "not in this app's pool · deadbeef", "m-big": 4, "m-small": 0, "Total": 4}, {"Key": "All keys", "m-big": 11, "m-small": 3, "Total": 14}]
      and llm_util.key_usage_table([], pool) == [], "U6: the same table the transcription app shows (its test holds this exact result)")

# --- U7 -----------------------------------------------------------------------------------------------------------------
AI = {"news-summary.yml": "news_check.py", "fundamentals.yml": "refresh_fundamentals.py", "expert-views.yml": "refresh_expert_views.py",
      "market-read.yml": "refresh_market_read.py"}
for name, script in AI.items():
    wf = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    ai = [s for s in steps if script in s.get("run", "")]
    install = next(s for s in steps if s.get("name") == "Install dependencies")
    with_db = [s.get("name") for s in steps if "MARKET_DB_URL" in (s.get("env") or {})]
    check(len(ai) == 1 and ai[0]["env"].get("MARKET_DB_URL") == "${{ secrets.MARKET_DB_URL }}" and "psycopg" in install["run"]
          and steps.index(install) < steps.index(ai[0]),
          f"U7: {name}: psycopg is installed before the AI step, and the AI step gets MARKET_DB_URL ({with_db})")

print("FAILURES:", fails)
sys.exit(fails)
