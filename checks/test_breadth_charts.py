"""News-tab charts (2026-10-15): the shared look, the phone behaviour, and the
Refresh Charts / data-snapshot wiring.

C1 is_mobile: phone and tablet User-Agents, not desktop ones.
C2 on a phone: no drag, no zoom, no modebar; a tap only shows values.
C3 the reference shapes really land: plotly skips a shape on a subplot with no
   trace yet, and the zones / 50% line / 100 baseline are drawn before the lines.
C4 an empty panel's note sits in THAT panel, centred, and an empty panel does
   not squash the shared date axis (its default range read as 1970).
C5 highs up, lows down, both labelled as positives.
C6 Refresh Charts starts the market-breadth workflow, which also refreshes the
   watchlist snapshot.

Offline: invented data, no network, no Streamlit.
"""

import sys
from pathlib import Path

import pandas as pd
from plotly.subplots import make_subplots

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import breadth_charts as bc

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
ANDROID = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"
IPAD = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"

# --- C1 ------------------------------------------------------------------------------
check(bc.is_mobile(IPHONE) and bc.is_mobile(ANDROID) and bc.is_mobile(IPAD), "C1: phones and tablets are mobile")
check(not bc.is_mobile(MAC) and not bc.is_mobile("") and not bc.is_mobile(None), "C1: desktop / no header is not")

# --- fixtures --------------------------------------------------------------------------
days = pd.bdate_range("2026-01-05", periods=60)
ema = pd.DataFrame({"Date": days, "% Above 200d SMA": [40.0 + i % 20 for i in range(60)]})
hl = pd.DataFrame({"Date": days, "% New Highs": [3.0] * 60, "% New Lows": [1.0] * 59 + [12.0]})
perf = pd.DataFrame({"Portfolio": [100.0 + i for i in range(60)], "Benchmark": [100.0] * 60}, index=days)


def figure(mobile, empty_second=False):
    fig = make_subplots(rows=3, cols=2, shared_xaxes="all")
    bc.add_breadth(fig, ema, 1, 1)
    bc.add_highs_lows(fig, hl, 2, 1)
    bc.add_performance(fig, perf, 3, 1, "List", "Index")
    if empty_second:
        for r in (1, 2, 3):
            bc.no_data(fig, r, 2, f"none {r}")
    else:
        bc.add_breadth(fig, ema, 1, 2)
        bc.add_highs_lows(fig, hl, 2, 2)
        bc.add_performance(fig, perf, 3, 2, "List", "Index")
    return bc.style(fig, height=900, mobile=mobile)


# --- C2 ------------------------------------------------------------------------------
m = figure(mobile=True)
axes = [a for k, a in m.layout.to_plotly_json().items() if k.startswith(("xaxis", "yaxis"))]
check(m.layout.dragmode is False and axes and all(a.get("fixedrange") is True for a in axes),
      "C2: on a phone, dragging does nothing and no axis can be zoomed")
cfg = bc.plotly_config(True)
check(cfg.get("displayModeBar") is False and cfg.get("scrollZoom") is False and cfg.get("doubleClick") is False,
      "C2: ...no modebar, scroll zoom or double-tap zoom")
check(m.layout.hovermode == "x unified", "C2: a tap still shows the values")
d = figure(mobile=False)
check(d.layout.dragmode == "zoom" and not any(a.get("fixedrange") for k, a in d.layout.to_plotly_json().items()
                                              if k.startswith(("xaxis", "yaxis"))),
      "C2: on a desktop, zoom still works")
check(bc.plotly_config(False).get("displaylogo") is False and "displayModeBar" not in bc.plotly_config(False),
      "C2: the desktop keeps its modebar")

# --- C3 ------------------------------------------------------------------------------
shapes = d.layout.shapes
check(len(shapes) == 10, f"C3: zones + 50% line per breadth panel, zero lines, 100 baselines all drawn ({len(shapes)})")
check(sum(1 for s in shapes if s.type == "rect") == 4, "C3: the 0-20% and 80-100% zones on both breadth panels")

# --- C4 ------------------------------------------------------------------------------
e = figure(mobile=False, empty_second=True)
notes = [a for a in e.layout.annotations if str(a.text).startswith("none")]
right = e.get_subplot(1, 2).xaxis.domain
check(len(notes) == 3 and all(n.xref == "paper" and n.xanchor == "center" for n in notes)
      and all(abs(n.x - (right[0] + right[1]) / 2) < 1e-9 for n in notes),
      "C4: an empty panel's note is centred on that panel, not the first one")
rng = e.layout.xaxis.range
check(rng is not None and pd.Timestamp(rng[0]) == days[0] and pd.Timestamp(rng[1]) > days[-1],
      f"C4: the shared date axis spans the data, not 1970 ({rng})")

# --- C5 ------------------------------------------------------------------------------
bars = [t for t in d.data if t.type == "bar" and t.xaxis == "x3"]
highs, lows = bars
check(min(highs.y) >= 0 and max(lows.y) <= 0 and list(lows.customdata)[-1] == 12.0,
      "C5: highs up, lows down, the lows' real values kept for hover")
y3 = d.layout.yaxis3
check(all(not str(t).startswith("-") for t in y3.ticktext) and y3.range[0] == -y3.range[1],
      f"C5: a symmetric axis labelled with positives ({list(y3.ticktext)})")

# --- C7 the 50-day overlay (2026-10-15) ----------------------------------------------
e50 = ema.assign(**{"% Above 50d SMA": [55.0] * 60})
f = make_subplots(rows=1, cols=1)
bc.add_breadth(f, e50, 1, 1, show_50=True)
names = [t.name for t in f.data if t.name]
check(names == ["%50d", "%200d"] and f.data[0].line.color == bc.AMBER,
      f"C7: the 50-day line is drawn in amber, under the 200-day ({names})")
f2 = make_subplots(rows=1, cols=1)
bc.add_breadth(f2, e50, 1, 1)
check([t.name for t in f2.data if t.name] == ["%200d"], "C7: only the 200-day unless asked")
f3 = make_subplots(rows=1, cols=1)
bc.add_breadth(f3, ema.assign(**{"% Above 50d SMA": [None] * 60}), 1, 1, show_50=True)
check([t.name for t in f3.data if t.name] == ["%200d"], "C7: a block stored before the 50-day series draws no empty line")

# --- C8 labels and legends (2026-10-15) ------------------------------------------------
lines = {t.name: t for t in f.data if t.name}
check(lines["%200d"].hovertemplate.startswith("% stocks above 200d SMA")
      and lines["%50d"].hovertemplate.startswith("% stocks above 50d SMA")
      and all(t.hovertemplate.endswith("<extra></extra>") for t in lines.values()),
      "C8: the hover says '% stocks above 200d / 50d SMA'; the legend's short name stays out of it")
fo = make_subplots(rows=1, cols=1)
bc.add_breadth(fo, e50, 1, 1, overlay=ema, overlay_name="Nifty 500", show_50=True)
check([t.name for t in fo.data if t.name] == ["Nifty 500 %200d", "%50d", "%200d"]
      and "Nifty 500: % stocks above 200d SMA" in fo.data[0].hovertemplate,
      "C8: the watchlist view's index line is named for its index, in the legend and the hover")
legs = {t.legend for t in d.data if t.showlegend is not False}
check(legs == {"legend", "legend2"},
      f"C8: each breadth panel has its own legend; nothing else is in one ({sorted(legs)})")
lay = d.layout.to_plotly_json()
dom2 = d.get_subplot(1, 2).xaxis.domain
ydom2 = d.get_subplot(1, 2).yaxis.domain
check(lay["legend2"]["x"] == dom2[0] and lay["legend2"]["yanchor"] == "top" and lay["legend2"]["y"] < ydom2[0]
      and all(lay[k]["traceorder"] == "normal" for k in ("legend", "legend2")),
      "C8: ...just under its own panel, left-aligned, in the same order in every panel")
check(all(lay[k].get("itemclick") is False for k in lay if k.startswith("legend")) is False
      and all(m.layout.to_plotly_json()[k].get("itemclick") is False
              for k in m.layout.to_plotly_json() if k.startswith("legend")),
      "C8: a tap on a legend item hides nothing on a phone; a desktop click still toggles")

# --- C6 ------------------------------------------------------------------------------
APP = (REPO / "app.py").read_text()
check('key="breadth_refresh_charts"' in APP and 'trigger_github_workflow(token, repo, "market-breadth.yml")' in APP,
      "C6: Refresh Charts starts the market-breadth workflow")
check('st.toggle("Show % above 50-day SMA", value=True, key="breadth_show_50d")' in APP
      and APP.count("show_50=show_50") == 2 and '"breadth_show_50d"' in APP,
      "C7: one toggle, on by default, shows the 50-day on both views and survives leaving the tab")
check("breadth_charts.is_mobile(st.context.headers" in APP and APP.count("breadth_charts.plotly_config(_mobile)") == 2,
      "C6: both charts get the phone behaviour")
WF = (REPO / ".github/workflows/market-breadth.yml").read_text()
step = WF[WF.index("- name: Refresh data snapshot"):WF.index("- name: Commit and push changes")]
check("continue-on-error: true" in step and "refresh_data.py" in step and "log_redact.py" in step,
      "C6: a breadth run refreshes the watchlist snapshot too, and a failure there does not cost the breadth commit")
check("files: market_breadth.json dashboard_perf.json data_snapshot.json" in WF,
      "C6: ...and commits it with the breadth files")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
