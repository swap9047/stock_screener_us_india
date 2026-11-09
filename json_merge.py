"""Three-way merge of a user config file (owner, 2026-11-09): what this app changed, laid over a newer copy
in the data repo, so neither side's edits are lost.

WHY: the app pushes an edited config file (watchlists, notes, alert rules, settings, ...) WHOLE at the end
of the run that changed it (github_sync.push_all_config). It pulls the data repo at most every 5 minutes, so
a change that landed there in between -- a commit made by hand, or by another app instance -- was overwritten
by the app's older copy plus its own edit, silently.

merge(base, local, remote): `base` is the copy this app last pulled or pushed, `local` the app's edited copy,
`remote` the data repo's newer one. Anything only one side changed takes that side's value. Where both
changed:
  - a dict merges key by key (a key one side deleted stays deleted unless the other side changed it);
  - a list of plain values (a watchlist's tickers) keeps both sides' additions and removals;
  - a list of dicts that each carry a unique "id" (alert rules, custom columns) merges item by item;
  - anything else (the same setting set to two different values, the same note edited twice) is a
    CONFLICT and the app's value wins (owner's choice), listed in the conflicts returned.
Order: a dict's keys and a list's items follow the app's order unless the data repo reordered them (tab
order, column order), and what only the other side added goes at the end.

Stdlib only, no I/O.
"""

import json

_MISSING = object()


def _is_scalar(x):
    return x is None or isinstance(x, (str, int, float, bool))


def _scalar_list(items):
    """True for a list of plain values with no repeats (a set kept in order)."""
    return all(_is_scalar(x) for x in items) and len(set(map(repr, items))) == len(items)


def _id_list(items):
    """True for a list of dicts that each carry a unique "id"."""
    ids = [x.get("id") for x in items if isinstance(x, dict)]
    return (len(ids) == len(items) and all(_is_scalar(i) and i is not None for i in ids)
            and len(set(map(repr, ids))) == len(ids))


def _order(base_seq, local_seq, remote_seq):
    """The candidate order of keys / items: the app's, unless the data repo reordered what base had."""
    in_both = [k for k in remote_seq if k in base_seq]
    remote_reordered = in_both != [k for k in base_seq if k in remote_seq]
    first, second = (remote_seq, local_seq) if remote_reordered else (local_seq, remote_seq)
    out, seen = [], set()
    for k in list(first) + list(second) + list(base_seq):
        if repr(k) not in seen:
            seen.add(repr(k))
            out.append(k)
    return out


def _same(a, b):
    """Equal as JSON, key ORDER included: Python's == calls two dicts equal in any order, so a tab order changed in
    the app (markets.json's key order) read as no change, and the data repo's copy -- old order -- won."""
    if a is _MISSING or b is _MISSING:
        return a is b
    return json.dumps(a) == json.dumps(b)


def _merge_value(b, l, r, path, conflicts):
    """The merged value, or _MISSING when the key / item is gone."""
    if _same(l, r):
        return l
    if _same(l, b):
        return r
    if _same(r, b):
        return l
    # Both sides changed it, differently.
    if b is _MISSING:   # both added it: merge as if from an empty container of the same kind
        if isinstance(l, dict) and isinstance(r, dict):
            return _merge_dict({}, l, r, path, conflicts)
        if isinstance(l, list) and isinstance(r, list):
            return _merge_list([], l, r, path, conflicts)
    elif l is not _MISSING and r is not _MISSING:
        if isinstance(b, dict) and isinstance(l, dict) and isinstance(r, dict):
            return _merge_dict(b, l, r, path, conflicts)
        if isinstance(b, list) and isinstance(l, list) and isinstance(r, list):
            return _merge_list(b, l, r, path, conflicts)
    conflicts.append("/".join(str(p) for p in path) or "(whole file)")
    return l   # the app's edit wins (owner, 2026-11-09), a deletion included


def _merge_dict(b, l, r, path, conflicts):
    out = {}
    for k in _order(list(b), list(l), list(r)):
        v = _merge_value(b.get(k, _MISSING), l.get(k, _MISSING), r.get(k, _MISSING), path + [k], conflicts)
        if v is not _MISSING:
            out[k] = v
    return out


def _merge_list(b, l, r, path, conflicts):
    if all(_scalar_list(x) for x in (b, l, r)):
        keyed = [{repr(x): x for x in seq} for seq in (b, l, r)]
        order = _order([repr(x) for x in b], [repr(x) for x in l], [repr(x) for x in r])
        out = []
        for k in order:
            v = _merge_value(*(seq.get(k, _MISSING) for seq in keyed), path + [keyed[1].get(k, keyed[2].get(k))],
                             conflicts)
            if v is not _MISSING:
                out.append(v)
        return out
    if all(_id_list(x) for x in (b, l, r)):
        keyed = [{repr(x["id"]): x for x in seq} for seq in (b, l, r)]
        order = _order([repr(x["id"]) for x in b], [repr(x["id"]) for x in l], [repr(x["id"]) for x in r])
        out = []
        for k in order:
            item_id = next(seq[k]["id"] for seq in keyed if k in seq)
            v = _merge_value(*(seq.get(k, _MISSING) for seq in keyed), path + [f"id={item_id}"], conflicts)
            if v is not _MISSING:
                out.append(v)
        return out
    conflicts.append("/".join(str(p) for p in path) or "(whole file)")
    return l


def merge(base, local, remote):
    """(merged, conflicts): `local`'s changes since `base` laid over `remote` -- see the module docstring.
    `conflicts` names each place both sides changed differently ("india_watchlist", "rules/id=r7/name"),
    where the app's value was kept."""
    conflicts = []
    merged = _merge_value(base, local, remote, [], conflicts)
    return merged, conflicts
