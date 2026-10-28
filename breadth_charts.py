"""Plotly building blocks for the News tab's breadth and performance charts.

Shared by the index view (Nifty 500 / S&P 500 or Nasdaq 100, side by side) and
the per-watchlist view, so both look the same. Pure plotly + pandas, no
Streamlit: the app passes in DataFrames and a `mobile` flag.

Design (2026-10-15, the charts "looked basic"):
- breadth: a line on a fixed 0-100% axis, the 0-20% / 80-100% extremes
  shaded, the 50% line dashed, and a dot on the last point -- so where the line
  ends is visible (at 3 years the last few days share a pixel, and the line read
  as stopping days early);
- 52-week highs vs lows as diverging bars, highs up and lows down, instead of
  two tangled lines;
- returns: the watchlist filled against its 100 start, the index dotted;
- transparent backgrounds and faint grids, so Streamlit's light and dark themes
  both carry it.

On a phone (owner, 2026-10-15) a tap only shows values: dragging, pinching and
double-tapping no longer zoom or filter the chart, and the panels stack in one
column instead of being squeezed side by side.
"""

import pandas as pd
import plotly.graph_objects as go

GREEN = "#2ecc71"
RED = "#e74c3c"
BLUE = "#3498db"
GREY = "#95a5a6"
AMBER = "#f39c12"
FAINT = "rgba(128,128,128,0.18)"

_MOBILE_MARKERS = ("mobi", "android", "iphone", "ipad", "ipod")


def is_mobile(user_agent):
    """True for a phone or tablet browser's User-Agent string."""
    ua = (user_agent or "").lower()
    return any(m in ua for m in _MOBILE_MARKERS)


def _last_point(fig, x, y, color, row, col):
    """A dot on the series' last point, so the end of the line is visible."""
    fig.add_trace(go.Scatter(x=[x.iloc[-1]], y=[y.iloc[-1]], mode="markers", showlegend=False,
                             marker=dict(color=color, size=8, line=dict(color="white", width=1.5)),
                             hoverinfo="skip"), row=row, col=col)


def _panel_legend(fig, row, col):
    """The id of a legend just under this panel's bottom-left corner, created on
    first use. One per breadth panel (plotly 5.15+ multiple legends): a single
    figure legend could not say which panel's lines it names. Under the panel,
    not in it: a watchlist's line often runs through the top corners, and the
    gap below is free -- the shared date axis labels only the bottom row.
    traceorder is set outright: plotly reverses a legend by default when the
    figure has stacked bars (the highs/lows panels), so the first one read
    backwards."""
    sp = fig.get_subplot(row, col)
    n = sp.xaxis.plotly_name[len("xaxis"):]
    legend_id = f"legend{n}" if n not in ("", "1") else "legend"
    (x0, _), (y0, _) = sp.xaxis.domain, sp.yaxis.domain
    fig.update_layout({legend_id: dict(x=x0, y=y0 - 0.005, xanchor="left", yanchor="top", orientation="h",
                                       traceorder="normal", bgcolor="rgba(0,0,0,0)", font=dict(size=11))})
    return legend_id


# Every add_hline/add_hrect here passes exclude_empty_subplots=False: plotly skips
# a shape on a subplot with no trace yet, and these are drawn before the lines --
# so the zones, the 50% line and the 100 baseline silently vanished (2026-10-15).


def add_breadth(fig, df, row, col, overlay=None, overlay_name=None, show_50=False):
    """% above the 200-day SMA: `df` has Date and "% Above 200d SMA". `overlay`
    (same shape) is drawn dotted underneath, e.g. the index under a watchlist.
    `show_50` adds df's "% Above 50d SMA" in amber (the chart's 50-day toggle),
    under the 200-day line, which stays the main reading.

    Labels (owner, 2026-10-15): the panel's own legend says "%200d" / "%50d", the
    hover says it in full -- "% stocks above 200d SMA" -- via a hovertemplate whose
    empty <extra> drops the short name from the hover."""
    # No fill under the line: a fill to zero means nothing for a % that swings
    # around 50, and it hid these zones.
    # Faint on purpose (owner, 2026-10-15): a tint behind the line, not a block
    # competing with it. Translucent, so it sits on Streamlit's light or dark theme.
    for y0, y1, colour in ((0, 20, "rgba(231,76,60,0.045)"), (80, 100, "rgba(46,204,113,0.05)")):
        fig.add_hrect(y0=y0, y1=y1, fillcolor=colour, line_width=0, layer="below", row=row, col=col,
                      exclude_empty_subplots=False)
    fig.add_hline(y=50, line_dash="dash", line_color="rgba(128,128,128,0.55)", line_width=1, row=row, col=col,
                      exclude_empty_subplots=False)
    legend = _panel_legend(fig, row, col)
    if overlay is not None and not overlay.empty:
        fig.add_trace(go.Scatter(x=overlay["Date"], y=overlay["% Above 200d SMA"], name=f"{overlay_name} %200d",
                                 line=dict(color=GREY, width=1.5, dash="dot"), legend=legend, legendrank=3,
                                 hovertemplate=f"{overlay_name}: % stocks above 200d SMA %{{y:.1f}}%<extra></extra>"),
                      row=row, col=col)
    if show_50 and "% Above 50d SMA" in df.columns and df["% Above 50d SMA"].notna().any():
        d50 = df[["Date", "% Above 50d SMA"]].dropna()
        fig.add_trace(go.Scatter(x=d50["Date"], y=d50["% Above 50d SMA"], name="%50d",
                                 line=dict(color=AMBER, width=1.4), opacity=0.85, legend=legend, legendrank=2,
                                 hovertemplate="% stocks above 50d SMA %{y:.1f}%<extra></extra>"),
                      row=row, col=col)
        _last_point(fig, d50["Date"], d50["% Above 50d SMA"], AMBER, row, col)
    fig.add_trace(go.Scatter(x=df["Date"], y=df["% Above 200d SMA"], name="%200d",
                             line=dict(color=GREEN, width=2), legend=legend, legendrank=1,
                             hovertemplate="% stocks above 200d SMA %{y:.1f}%<extra></extra>"),
                  row=row, col=col)
    _last_point(fig, df["Date"], df["% Above 200d SMA"], GREEN, row, col)
    ticks = [0, 20, 50, 80, 100]
    fig.update_yaxes(range=[0, 100], tickvals=ticks, ticktext=[f"{t}%" for t in ticks], row=row, col=col)


def add_highs_lows(fig, df, row, col):
    """52-week highs (up, green) and lows (down, red) as diverging bars: `df`
    has Date, "% New Highs" and "% New Lows". Hover shows both as positives."""
    highs, lows = df["% New Highs"], df["% New Lows"]
    fig.add_trace(go.Bar(x=df["Date"], y=highs, name="New highs", marker_color=GREEN, marker_line_width=0,
                         showlegend=False,
                         hovertemplate="%{y:.1f}%"), row=row, col=col)
    fig.add_trace(go.Bar(x=df["Date"], y=-lows, name="New lows", marker_color=RED, marker_line_width=0,
                         showlegend=False,
                         customdata=lows, hovertemplate="%{customdata:.1f}%"), row=row, col=col)
    fig.add_hline(y=0, line_color="rgba(128,128,128,0.55)", line_width=1, row=row, col=col,
                      exclude_empty_subplots=False)
    # Symmetric axis with positive labels both ways: the lows are drawn negative.
    top = max(float(highs.max() or 0), float(lows.max() or 0), 5.0)
    step = 5 if top <= 20 else 10 if top <= 50 else 25
    edge = step * -(-top // step)
    ticks = [v for v in range(-int(edge), int(edge) + 1, step)]
    fig.update_yaxes(range=[-edge, edge], tickvals=ticks, ticktext=[f"{abs(v)}%" for v in ticks],
                     row=row, col=col)


def add_performance(fig, df, row, col, name, bench_name):
    """Equal-weight return vs the index: `df` indexed by date with Portfolio and
    Benchmark (both start at 100)."""
    fig.add_hline(y=100, line_color="rgba(128,128,128,0.55)", line_width=1, row=row, col=col,
                      exclude_empty_subplots=False)
    # An invisible 100 line for the watchlist's fill to sit against.
    fig.add_trace(go.Scatter(x=df.index, y=[100] * len(df), line=dict(width=0), showlegend=False,
                             hoverinfo="skip"), row=row, col=col)
    fig.add_trace(go.Scatter(x=df.index, y=df["Portfolio"], name=name, line=dict(color=BLUE, width=2),
                             showlegend=False,
                             fill="tonexty", fillcolor="rgba(52,152,219,0.10)", hovertemplate="%{y:.1f}"),
                  row=row, col=col)
    fig.add_trace(go.Scatter(x=df.index, y=df["Benchmark"], name=bench_name, showlegend=False,
                             line=dict(color=GREY, width=1.5, dash="dot"), hovertemplate="%{y:.1f}"),
                  row=row, col=col)
    _last_point(fig, pd.Series(df.index), df["Portfolio"].reset_index(drop=True), BLUE, row, col)


def no_data(fig, row, col, text):
    """Why a panel is blank: an empty panel reads the same as all-zero data.

    Placed on the page at the centre of the panel's area, not on its axes: an
    axis with no trace is never drawn, so a note anchored to it was dropped --
    and xref="x domain" with row/col put it in the FIRST panel anyway. Only the
    first panel's note ever showed (2026-10-15)."""
    sp = fig.get_subplot(row, col)
    (x0, x1), (y0, y1) = sp.xaxis.domain, sp.yaxis.domain
    fig.add_annotation(text=text, showarrow=False, xref="paper", yref="paper",
                       x=(x0 + x1) / 2, y=(y0 + y1) / 2, xanchor="center", yanchor="middle",
                       font=dict(color=GREY, size=13))


def _date_span(fig):
    """(first, last) date over every trace, or None."""
    lo = hi = None
    for tr in fig.data:
        if tr.x is None or len(tr.x) == 0:
            continue
        xs = pd.to_datetime(pd.Series(list(tr.x)), errors="coerce").dropna()
        if xs.empty:
            continue
        lo = xs.min() if lo is None else min(lo, xs.min())
        hi = xs.max() if hi is None else max(hi, xs.max())
    return (lo, hi) if lo is not None else None


def style(fig, height, mobile):
    """Shared look, and the phone behaviour: no zoom/drag, values on tap.

    The date axis is set outright from the data: the panels share one axis, and
    an EMPTY panel's default numeric range read as 1970 and squeezed every other
    panel into a sliver at the right (2026-10-15, a benchmark not stored yet).
    A 2% pad on the right keeps the last-point dot clear of the edge."""
    fig.update_layout(
        height=height,
        hovermode="x unified",
        showlegend=True,
        barmode="relative",
        bargap=0,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        # t=55: the right column's titles cleared the modebar only with this
        # headroom (a vertical modebar parks against the right edge).
        margin=dict(l=0, r=0, t=55, b=0),
        modebar=dict(orientation="v", bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(font_size=12),
        dragmode=False if mobile else "zoom",
    )
    fig.update_annotations(font_size=13)
    if mobile:
        # A tap on a legend item would hide that line -- the "filtering by tap"
        # phones should not do (2026-10-15).
        for key in [k for k in fig.layout.to_plotly_json() if k.startswith("legend")]:
            fig.layout[key].update(itemclick=False, itemdoubleclick=False)
    span = _date_span(fig)
    if span:
        lo, hi = span
        fig.update_xaxes(range=[lo, hi + (hi - lo) * 0.02])
    fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikethickness=1,
                     spikecolor="rgba(128,128,128,0.6)", spikedash="dot", showline=False, showgrid=False,
                     fixedrange=mobile)
    fig.update_yaxes(showgrid=True, gridcolor=FAINT, zeroline=False, fixedrange=mobile)
    return fig


def plotly_config(mobile):
    """st.plotly_chart's config: on a phone, no modebar, scroll or double-tap zoom."""
    if mobile:
        return {"displayModeBar": False, "scrollZoom": False, "doubleClick": False, "displaylogo": False}
    return {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}


# --- quarterly results (2026-10-28) ---------------------------------------------------
# The fourth row: per quarter, the median company's revenue, profit and EPS
# growth as grouped bars (StockScans' blue / teal / purple), and its OPM
# (operating margin) as an amber line on a second axis -- each one shown or
# hidden by the News tab's field picker. A quarter still being reported
# (quarterly_results' "season") is drawn light with its colour as an outline,
# so half a season is not read as the final number. It used to be hatched: a
# white hatch over a faded bar rendered as near-white in the live app, so those
# bars all but vanished (2026-10-28).
TEAL = "#1abc9c"
PURPLE = "#9b59b6"
RESULT_SERIES = (("rev", "Revenue growth", BLUE), ("ni", "Profit growth", TEAL), ("eps", "EPS growth", PURPLE))


def _signed(v):
    return "--" if v is None else f"{v:+.1f}%"


def _level(v):
    return "--" if v is None else f"{v:.1f}%"


def add_results(fig, rows, row, col, basis_label, compare=None, compare_name=None, legend=True,
                show=("rev", "ni", "eps", "margin")):
    """`rows` from quarterly_results.summarize (oldest first). `compare` (the
    same shape, e.g. the benchmark index's) is named in each bar's hover.
    `legend`: draw this panel's series in the figure legend (once per figure;
    the legend groups toggle every panel's matching series). `show`: which of
    the four fields to draw ("rev", "ni", "eps", "margin")."""
    x = [r["label"] for r in rows]
    cmp = {r["q"]: r for r in (compare or [])}
    season = [r["season"] for r in rows]
    for key, name, colour in RESULT_SERIES:
        if key not in show:
            continue
        hover = []
        for r in rows:
            text = f"{name} {basis_label}: {_signed(r[key])} (median of {r['n'][key]})"
            if compare_name is not None:
                text += f" -- {compare_name} {_signed((cmp.get(r['q']) or {}).get(key))}"
            if r["season"]:
                text += " -- still being reported"
            hover.append(text)
        fig.add_trace(go.Bar(x=x, y=[r[key] for r in rows], name=name, legendgroup=key, showlegend=legend,
                             marker=dict(color=colour, opacity=[0.4 if s else 1 for s in season],
                                         line=dict(color=colour, width=[1.5 if s else 0 for s in season])),
                             hovertext=hover, hovertemplate="%{hovertext}<extra></extra>"),
                      row=row, col=col, secondary_y=False)
    mg = [r["margin"] for r in rows]
    if "margin" in show and any(v is not None for v in mg):
        hover = []
        for r in rows:
            text = f"OPM: {_level(r['margin'])} (median of {r['n']['margin']})"
            if compare_name is not None:
                text += f" -- {compare_name} {_level((cmp.get(r['q']) or {}).get('margin'))}"
            hover.append(text)
        fig.add_trace(go.Scatter(x=x, y=mg, name="OPM", legendgroup="margin", showlegend=legend,
                                 mode="lines+markers", line=dict(color=AMBER, width=2),
                                 marker=dict(size=7, color=AMBER, line=dict(color="white", width=1.5)),
                                 connectgaps=True, hovertext=hover, hovertemplate="%{hovertext}<extra></extra>"),
                      row=row, col=col, secondary_y=True)
    fig.add_hline(y=0, line_color="rgba(128,128,128,0.55)", line_width=1, row=row, col=col,
                  exclude_empty_subplots=False)
    fig.update_yaxes(ticksuffix="%", row=row, col=col, secondary_y=False)
    # OPM's axis from 0 to a round step above its highest point, so the line sits
    # in the upper part of the panel, clear of the bars' labels; plotly's default
    # put ticks like 0.03% and 17.58% there.
    top = max([v for v in mg if v is not None] or [0])
    step = 5 if top <= 30 else 10
    edge = step * (int(top * 1.25 // step) + 1)
    fig.update_yaxes(ticksuffix="%", showgrid=False, range=[0, edge], dtick=step * (2 if edge > 4 * step else 1),
                     tickformat=".0f", row=row, col=col, secondary_y=True)


def style_results(fig, height, mobile):
    """style()'s look for the quarterly results figure: category quarters on
    the x axis (style() sets a date range), grouped bars, and one legend under
    the panels, below the quarter labels."""
    fig.update_layout(
        height=height,
        hovermode="x unified",
        barmode="group",
        bargap=0.28,
        bargroupgap=0.06,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=55, b=10),
        modebar=dict(orientation="v", bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(font_size=12),
        dragmode=False if mobile else "zoom",
        showlegend=True,
        legend=dict(orientation="h", x=0, y=-0.12, xanchor="left", yanchor="top", traceorder="normal",
                    bgcolor="rgba(0,0,0,0)", font=dict(size=11),
                    **({"itemclick": False, "itemdoubleclick": False} if mobile else {})),
    )
    fig.update_annotations(font_size=13)
    fig.update_xaxes(type="category", showgrid=False, showline=False, fixedrange=mobile)
    fig.update_yaxes(gridcolor=FAINT, zeroline=False, fixedrange=mobile)
    return fig
