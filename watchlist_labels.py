"""Tab labels a watchlist may not take, and the check that enforces it.

Its own module, not stock_data.py, on purpose: stock_data._code_fingerprint
hashes that whole file, and any edit to it marks the stored data snapshot as
computed by old code -- every visitor then gets a live Yahoo fetch until the
next scheduled refresh restamps it. None of this is calculation code, so it
lives where it cannot trigger that.
"""

# The combined tabs' labels, and the two fixed tabs after them. app.py builds
# its tab strip from these, and watchlist_label_error refuses a watchlist label
# equal to any of them -- one list, so the two cannot drift.
COMBINED_TAB_LABELS = {
    "all_invested": "All Invested",
    "all_watchlist": "All Watchlist",
}
FIXED_TAB_LABELS = ("News", "Alert Rules")


def _label_norm(label):
    return " ".join(str(label or "").split()).casefold()


def watchlist_label_error(label, registry, own_key=None):
    """Why `label` cannot name a watchlist, or "" if it can.

    Watchlist KEYS were already deduplicated (stock_data.add_watchlist), labels
    were not. The tab strip is keyed by label (st.tabs(key="main_tabs") stores
    the selected LABEL), so two tabs sharing one could not be told apart:
    picking the second selected the first on the next rerun, and the sidebar
    sort control edited whichever market the label mapped to last. Compared
    case- and whitespace-insensitively, since "us  picks" beside "US Picks"
    reads as the same tab. `own_key` lets a watchlist keep its own label.

    Every UI path that adds or renames a watchlist calls this first (app.py's
    Settings dialog add + rename, and the dashboard's Add popover)."""
    norm = _label_norm(label)
    if not norm:
        return "A label is required."
    reserved = list(COMBINED_TAB_LABELS.values()) + list(FIXED_TAB_LABELS)
    if norm in {_label_norm(r) for r in reserved}:
        return f'"{label.strip()}" is the name of a built-in tab. Pick another label.'
    for key, info in (registry or {}).items():
        if key != own_key and _label_norm((info or {}).get("label")) == norm:
            return f'Another watchlist is already labelled "{info.get("label")}". Pick another label.'
    return ""
