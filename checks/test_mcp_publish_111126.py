"""The sidebar's "Send to Claude chat" control (owner, 2026-11-11).

Refresh Data only updates the app and the data repo; the research library's database -- what Claude chat's connector
reads -- is written by data-refresh.yml (the tickers) and market-breadth.yml (the tabs, breadth, closes), which the
Worker starts on a schedule. A stock added in the app took up to an hour, and a weekend, to appear there. The control
is a dropdown of those two plus a Send button, manual only.

P1 start_mcp_publish dispatches the right workflow with no inputs; no config, an unknown choice or a failed request
   is reported, never raised.
P2 Both workflows can be dispatched, and still publish what the choice promises.
P3 The wiring: dropdown + button sit under Refresh Data / Settings; pending config edits are pushed to the data repo
   FIRST (the workflows read it), and a failed save blocks the dispatch; nothing starts unless clicked.

Offline: the GitHub calls are replaced.
"""
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import github_sync as gs

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


calls = []
real = (gs.get_github_config, gs.trigger_github_workflow)
gs.get_github_config = lambda s=None: ("tok", "o/r", "main")
gs.trigger_github_workflow = lambda token, repo, workflow_file="x", ref="main", inputs=None: (
    calls.append((token, repo, workflow_file, inputs)) or (True, "Workflow triggered successfully."))
t, w = gs.start_mcp_publish("tickers"), gs.start_mcp_publish("watchlists")
check(t[0] and w[0] and calls == [("tok", "o/r", "data-refresh.yml", None), ("tok", "o/r", "market-breadth.yml", None)],
      "P1: tickers -> data-refresh.yml, watchlists -> market-breadth.yml, no inputs (a full run)")
check(gs.start_mcp_publish("nope")[0] is False and len(calls) == 2, "P1: an unknown choice starts nothing")
gs.trigger_github_workflow = lambda *a, **k: (False, "Failed to trigger workflow (422): x")
check(gs.start_mcp_publish("tickers") == (False, "Failed to trigger workflow (422): x"), "P1: GitHub's refusal is passed on")


def boom(*a, **k):
    raise OSError("network down")


gs.trigger_github_workflow = boom
r = gs.start_mcp_publish("tickers")
check(r[0] is False and "network down" in r[1], "P1: a network error is reported, not raised")
gs.get_github_config = lambda s=None: (None, None, "main")
check(gs.start_mcp_publish("tickers")[0] is False, "P1: no GITHUB_TOKEN / GITHUB_REPO -> not started")
gs.get_github_config, gs.trigger_github_workflow = real

check(set(gs.MCP_PUBLISH_WORKFLOWS) == {"tickers", "watchlists"}, "P1: the dropdown offers exactly the two choices")

wf_d = (REPO / ".github/workflows/data-refresh.yml").read_text()
wf_b = (REPO / ".github/workflows/market-breadth.yml").read_text()
check("workflow_dispatch" in wf_d and "publish_market.py screener" in wf_d, "P2: data-refresh.yml is dispatchable and publishes the rows")
check("workflow_dispatch" in wf_b and "publish_market.py watchlists" in wf_b, "P2: market-breadth.yml is dispatchable and publishes the tabs")
check("cancel-in-progress: false" in wf_d and "cancel-in-progress: false" in wf_b,
      "P2: both queue behind a run in progress instead of cancelling it (a click is never lost)")

APP = (REPO / "app.py").read_text()
i_sel = APP.find('"Send to Claude chat", list(MCP_PUBLISH_WORKFLOWS)')
i_btn = APP.find('st.sidebar.button("Send now", key="mcp_publish_go"')
i_settings = APP.find('if sb2.button("⚙️ Settings"')
i_push = APP.find("unpushed_config_files()", i_btn)
i_start = APP.find("start_mcp_publish(_mcp_kind, st.secrets)", i_btn)
check(0 < i_settings < i_sel < i_btn < i_push < i_start, "P3: the control sits under Settings; config is pushed BEFORE the workflow starts")
block = APP[i_btn:i_start + 400]
check("if not _mcp_ok" in block and "_mcp_ok, _mcp_msg = start_mcp_publish" in block and "if _mcp_ok:" in block,
      "P3: a failed save of pending config stops the dispatch")
check(APP[i_btn - 3:i_btn] == "if " and APP.count("start_mcp_publish(") == 1,
      "P3: the only caller is the button's click branch (nothing starts on a plain rerun)")
ast.parse(APP)
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
