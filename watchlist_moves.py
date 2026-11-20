"""The "Move stocks" grid's logic (owner, 2026-11-20): every stock in one table, a tick box per watchlist.

Its own stdlib-only module, not app.py or stock_data.py: the plan has to be testable offline, and
stock_data._code_fingerprint hashes that file's code, so any edit there marks the stored data snapshot
as computed by old code (every visitor then gets a live Yahoo fetch until the next refresh).

The grid only ever covers the REGISTERED watchlists (markets.json). The combined tabs (All Invested, All
Watchlist) are views over those lists, not lists of their own, so they are never a column.

A move is a change of membership: ticking a box puts the stock in that list, unticking takes it out. The
rules `plan_changes` applies:

  - a list keeps its order; a stock newly ticked into it goes to the end, in the grid's order;
  - a list nothing changed in is returned as it was (and is not named in `changed`), so saving touches only
    what moved, and `watchlist.json`'s other keys are passed through untouched;
  - an EXISTING stock ticked nowhere is in no list any more: it is reported in `orphans`, because that
    removes it from the app (the caller asks first);
  - a NEW stock (typed into the grid's add box) ticked nowhere is simply not added (`skipped_new`).
"""

INDIA_SUFFIXES = (".NS", ".BO")


def is_india(ticker):
    return str(ticker).upper().endswith(INDIA_SUFFIXES)


def clean_ticker(text):
    return str(text or "").strip().upper()


def parse_tickers(text):
    """The tickers in a typed box: comma, semicolon, space or line separated; upper-cased, no repeats."""
    out = []
    for raw in str(text or "").replace(";", ",").replace("\n", ",").replace(" ", ",").split(","):
        t = clean_ticker(raw)
        if t and t not in out:
            out.append(t)
    return out


def current_ticks(watchlists, markets):
    """{ticker: set of the `markets` it is in} for every stock in any of them."""
    ticks = {}
    for m in markets:
        for t in watchlists.get(m) or []:
            ticks.setdefault(clean_ticker(t), set()).add(m)
    return ticks


def row_order(tickers, new=()):
    """The grid's rows: the stocks added in this session first (so they cannot be missed), then every Indian
    stock (.NS / .BO), then every other (US), each alphabetical by ticker."""
    new = [t for t in new if t in set(tickers)]
    rest = sorted((t for t in set(tickers) if t not in set(new)), key=lambda t: (not is_india(t), t))
    return new + rest


def plan_changes(watchlists, markets, ticks, new=()):
    """What saving the grid does.

    `ticks` is {ticker: iterable of the markets ticked for it} for every row of the grid; `new` the tickers the
    user added in this session (they have no list yet). Returns a dict:

      lists          the full {market: [tickers]} to save -- `watchlists` with the changed lists replaced
      changed        the markets whose list is different, in `markets` order
      moves          {ticker: {"add": [markets], "remove": [markets]}} for every stock whose membership changed
      orphans        existing stocks that end up in no list (sorted, India first)
      skipped_new    new stocks ticked nowhere, which are not added
      added          new stocks that are added (ticked somewhere)

    Never modifies its arguments."""
    markets = list(markets)
    new = [clean_ticker(t) for t in new]
    old_ticks = current_ticks(watchlists, markets)
    want = {clean_ticker(t): {m for m in ms if m in markets} for t, ms in (ticks or {}).items()}

    lists = {m: list(v) if isinstance(v, list) else v for m, v in watchlists.items()}
    changed, moves = [], {}
    for m in markets:
        old = [clean_ticker(t) for t in watchlists.get(m) or []]
        keep = [t for t in old if t not in want or m in want[t]]
        have = set(keep)
        joined = [t for t in row_order(list(want), new) if m in want[t] and t not in have]
        updated = keep + joined
        if updated != old:
            lists[m] = [t for t in (watchlists.get(m) or []) if clean_ticker(t) in set(keep)] + joined
            changed.append(m)
    for t in want:
        before = old_ticks.get(t, set())
        after = want[t]
        if before != after:
            moves[t] = {"add": [m for m in markets if m in after - before],
                        "remove": [m for m in markets if m in before - after]}
    orphans = sorted((t for t in want if t in old_ticks and not want[t]), key=lambda t: (not is_india(t), t))
    skipped_new = [t for t in new if t in want and not want[t] and t not in old_ticks]
    added = [t for t in new if t in want and want[t] and t not in old_ticks]
    return {"lists": lists, "changed": changed, "moves": moves, "orphans": orphans,
            "skipped_new": skipped_new, "added": added}


def describe_move(move, labels):
    """One line for the review list: "+ Sold  − India Watchlist"."""
    parts = [f"+ {labels.get(m, m)}" for m in move["add"]] + [f"− {labels.get(m, m)}" for m in move["remove"]]
    return "  ".join(parts)
