"""Mask held tickers and company names in GitHub Actions log output.

The code repo is public, and so are its Actions logs: every run printed symbols
from the private watchlists ("Too little price history: ...", per-ticker AI
progress). Workflows pipe each Python step through this filter:

    python -u refresh_data.py 2>&1 | python -u log_redact.py

It reads the data files from the current directory (the workflow copies them in
from the data repo first) and never raises: with no readable data it passes
lines through, since losing a log is worse than an unmasked one on a run that
had no data to leak. Run steps under `shell: bash` so pipefail keeps the
script's exit status.
"""
import json
import os
import re
import sys

# `_` counts as part of a token so a watchlist key inside a longer identifier
# isn't half-masked; `&` so "M&M.NS" is one symbol.
BOUNDARY_BEFORE = r"(?<![A-Za-z0-9&_])"
BOUNDARY_AFTER = r"(?![A-Za-z0-9&_])"


def _load(folder, name):
    try:
        with open(os.path.join(folder, name)) as f:
            return json.load(f)
    except Exception:
        return None


def _tickers(folder):
    found = set()
    watchlist = _load(folder, "watchlist.json")
    if isinstance(watchlist, dict):
        for tickers in watchlist.values():
            if isinstance(tickers, list):
                found.update(t for t in tickers if isinstance(t, str))
    interested = _load(folder, "interested.json")
    if isinstance(interested, list):
        found.update(t for t in interested if isinstance(t, str))
    for name in ("ticker_notes.json", "ticker_index.json"):
        data = _load(folder, name)
        if isinstance(data, dict):
            found.update(k for k in data if isinstance(k, str))
    return {t.strip() for t in found if t and t.strip()}


def _companies(folder):
    snapshot = _load(folder, "data_snapshot.json")
    names = set()
    per_market = (snapshot or {}).get("per_market") if isinstance(snapshot, dict) else None
    for rows in (per_market or {}).values():
        for row in rows if isinstance(rows, list) else []:
            name = row.get("company_name") if isinstance(row, dict) else None
            if isinstance(name, str) and len(name.strip()) >= 4:
                names.add(name.strip())
    return names


def _watchlists(folder):
    """Watchlist keys and labels -- names like a newsletter's picks list are
    personal too, and runs print them ("31 india_invested + 25 ...")."""
    registry = _load(folder, "markets.json")
    names = set()
    if isinstance(registry, dict):
        for key, meta in registry.items():
            names.add(key)
            if isinstance(meta, dict) and isinstance(meta.get("label"), str):
                names.add(meta["label"])
    return {n.strip() for n in names if isinstance(n, str) and len(n.strip()) >= 4}


def build_patterns(folder="."):
    """[(regex, placeholder)], longest literal first so "[ticker]" wins over "[ticker]"."""
    literals = {}
    for name in _watchlists(folder):
        literals[name] = "[watchlist]"
    for name in _companies(folder):
        literals[name] = "[company]"
    for ticker in _tickers(folder):
        literals.setdefault(ticker, "[ticker]")
        bare = re.split(r"\.[A-Z]{1,3}$", ticker)[0]
        if bare != ticker and len(bare) >= 3:
            literals.setdefault(bare, "[ticker]")
    ordered = sorted(literals, key=len, reverse=True)
    return [(re.compile(BOUNDARY_BEFORE + re.escape(lit) + BOUNDARY_AFTER), literals[lit]) for lit in ordered]


def redact(line, patterns):
    for pattern, placeholder in patterns:
        line = pattern.sub(placeholder, line)
    return line


def main():
    try:
        patterns = build_patterns(".")
    except Exception:
        patterns = []
    if not patterns:
        print("[log_redact] no data files found -- output is NOT masked", flush=True)
    for line in sys.stdin:
        try:
            line = redact(line, patterns)
        except Exception:
            pass
        sys.stdout.write(line)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
