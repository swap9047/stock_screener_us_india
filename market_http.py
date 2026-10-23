"""What the two Vercel functions (api/market.py for the research library's app, api/mcp.py for Claude
chat's connector) share: the runtime set-up, the token check and capturing Yahoo's messages.

Import this before market_api / stock_data: yfinance keeps a timezone cache under the user's cache
dir, which is read-only on Vercel, and /tmp is the one writable place.
"""

import contextlib
import hmac
import io
import logging
import os
from urllib.parse import parse_qs, unquote

os.environ.setdefault("XDG_CACHE_HOME", "/tmp")
try:
    import yfinance as _yf
    _yf.set_tz_cache_location("/tmp/yf-tz-cache")
except Exception:
    pass


def sent_token(headers, query, path=""):
    """The token a request carries, from wherever a client can put it: Authorization: Bearer (the
    app), ?token= , or the path /mcp/<token> (Claude's custom connector takes just a URL). Vercel's
    route rewrite also reports the path's capture in x-now-route-matches."""
    bearer = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if bearer:
        return bearer
    if (query.get("token") or [""])[0]:
        return query["token"][0]
    route = parse_qs(headers.get("x-now-route-matches") or "")
    if (route.get("token") or [""])[0]:
        return route["token"][0]
    parts = [p for p in (path or "").split("?", 1)[0].split("/") if p]
    if len(parts) >= 2 and parts[-2] == "mcp":
        return unquote(parts[-1])
    return ""


def authorized(headers, query, path=""):
    """True only when MARKET_API_TOKEN is set and the request carries it.

    Fails closed (2026-10-23): an unset token used to mean open to anyone with the URL -- meant for
    the first smoke test -- so a deployment or a preview environment that lost the variable would
    have served, and written to the library's database for, every caller."""
    token = os.environ.get("MARKET_API_TOKEN", "")
    if not token:
        return False
    return hmac.compare_digest(sent_token(headers, query, path).encode(), token.encode())


@contextlib.contextmanager
def captured():
    """Collect what Yahoo says while computing -- printed by fetch_snapshot, logged by yfinance (a
    ticker left out of a bulk download, a rate limit) -- into a buffer the caller may return."""
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    yf_logger = logging.getLogger("yfinance")
    yf_logger.addHandler(handler)
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            yield log
    finally:
        yf_logger.removeHandler(handler)
