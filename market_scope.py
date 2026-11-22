"""The `markets` input of the AI workflows, as watchlist POSITIONS (owner's review, 2026-11-22).

expert-views.yml, fundamentals.yml and news-summary.yml put their `markets`
dispatch input into a step's env (REFRESH_MARKETS), and GitHub prints a step's
env in this public repo's Actions log before log_redact.py sees any output, so
the watchlist keys the app sent ("Re-analyze All" on a tab) were published as
typed. The app now sends 1-based positions in markets.json key order ("2,5"),
which say nothing about what the lists are, and the scripts map them back here.

A key name is still accepted: a run dispatched by an older copy of the app, or
by hand from the Actions tab, sends names. Anything else -- junk, a position out
of range -- is ignored with a count-only warning. A scope that asked for
something and matched nothing stays a scope (NO_MATCH): falling back to blank
would turn one tab's run into a run over every watchlist.

Stdlib only, no file reads: the caller passes the keys in registry order
(stock_data.load_watchlists() lists the markets.json keys first, in its order).
"""

ENV_VAR = "REFRESH_MARKETS"

# Stands in for "a scope was asked for and none of it matched". Not a possible
# key (keys are slugs: letters, digits, `_`), so it matches no watchlist, and it
# reads sensibly where a script prints its scope.
NO_MATCH = "(no watchlist matched)"


def encode(markets, keys):
    """The positions to send for `markets` (registry keys), as "2,5", in the
    order given. A key not in `keys` is left out: the app only ever passes
    registry keys, and sending its name would put it back in the log."""
    order = list(keys)
    return ",".join(str(order.index(m) + 1) for m in markets if m in order)


def resolve(raw, keys):
    """(selected keys in registry order, number of entries ignored).

    Each comma-separated entry is a 1-based position in `keys` or a key itself.
    Blank or absent -> ([], 0), meaning every watchlist."""
    order = list(keys)
    chosen, ignored = set(), 0
    for entry in str(raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.isdigit():
            pos = int(entry)
            if 1 <= pos <= len(order):
                chosen.add(order[pos - 1])
            else:
                ignored += 1
        elif entry in order:
            chosen.add(entry)
        else:
            ignored += 1
    return [k for k in order if k in chosen], ignored


def only_markets(raw, keys, log=print):
    """The set the refresh scripts filter by: empty for every watchlist, else the
    selected keys -- or {NO_MATCH} when the input asked for something and none of
    it matched (the old behaviour for an unknown key: the run does nothing)."""
    selected, ignored = resolve(raw, keys)
    if ignored:
        # A count only: the entry may be a watchlist name typed by hand.
        log(f"::warning::{ENV_VAR}: {ignored} entr{'y' if ignored == 1 else 'ies'} ignored "
            f"(not a position 1-{len(list(keys))} or a watchlist key)")
    if selected:
        return set(selected)
    return {NO_MATCH} if ignored else set()


def from_env(keys, environ=None, log=print):
    """only_markets() over the REFRESH_MARKETS environment variable."""
    import os
    env = os.environ if environ is None else environ
    return only_markets(env.get(ENV_VAR, ""), keys, log=log)
