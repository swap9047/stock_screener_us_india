"""
LLM-generated news/announcements summary for the watchlists -- a
Perplexity-Finance-style digest built with a 3-stage per-ticker architecture:

  Stage 1 (Web Search): a Gemma model with Google Search Grounding.
     ONE grounded search per unique ticker. Ladder: the configured search model
     (default gemma-4-26b-a4b-it) -> gemma-4-31b-it -> retry queue -> failed.

  Stage 2 (Significance filter): gemini-3.5-flash-lite, no search.
     Filters one ticker's raw notes against strict recency (24h, plus scheduled
     events 3-4 days out) and material-catalyst rules. Ladder:
     gemini-3.5-flash-lite -> retry it once -> gemma-4-26b-a4b-it -> degraded.

  Stage 3 (Collation): gemini-3.7-flash, falling back to gemini-3.6-flash,
     both with a 4k-8k thinking budget; if both fail the pair is retried once
     after a backoff. Once per market. EDITS and formats that market's notes
     into a scannable brief -- it deliberately does NOT re-filter them, since
     recency and materiality were already decided in Stage 2 where the raw
     dates still exist.

Roughly 105 search + 105 reasoning + 5 collation calls per run at today's
watchlist sizes. Stages 1 and 2 are memoised per (ticker, window date), so a
ticker in three watchlists (AMKR today) costs one search, not three, while still
appearing in all three digests -- only Stage 3 is genuinely per-market.

News is generated once/day at 8:00 PM ET via GitHub Actions (news-summary.yml).
The watchlist scope (which markets to include) is controlled by the
``news_watchlist_scope`` key in settings.json (empty list = all markets).

There is deliberately NO scraped-web fallback. A DuckDuckGo/yfinance tier used
to sit under Stage 1; it was removed because it was contributing noise rather
than coverage -- the yfinance tier silently returned nothing at all on yfinance
1.5.x (the schema moved to item["content"], so the old title/providerPublishTime
reads yielded None for every item and the guard skipped them), the DuckDuckGo
tier queried bare tickers with no company name ("Q stock news"), and the tier
above both grepped the previous run's news_summary.json with naive substring
matching, so ticker "Q" matched every line containing the letter q. A ticker
whose search ladder is exhausted is now recorded as `failed` instead, which the
output schema actually reports.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.genai import types

import llm_util

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NEWS_SUMMARY_FILE = os.path.join(SCRIPT_DIR, "news_summary.json")

# Stage 1 ladder. The default matches settings.json's news_search_model so the
# code default and the saved setting can't silently disagree.
SEARCH_MODEL = "models/gemma-4-26b-a4b-it"
SEARCH_FALLBACK_MODEL = "models/gemma-4-31b-it"

# Stage 2 ladder.
REASONING_MODEL = "models/gemini-3.5-flash-lite"
REASONING_FALLBACK_MODEL = "models/gemma-4-26b-a4b-it"

# Stage 3 gets its own, stronger ladder. Collation is the one stage that reads
# every surviving note for a watchlist at once and has to keep all of them, so
# it is worth more capable models and a real thinking budget -- and unlike
# Stage 2 there is no weak tier to concede to: both are trusted, so the ladder
# tries each, then tries BOTH again after a backoff rather than degrading.
# Overridable from settings.json via news_collation_model /
# news_collation_fallback_model, so a model id can be corrected without a code
# change; an unavailable id makes the ladder step to the next tier (see
# llm_util.is_model_unavailable) instead of failing the stage.
COLLATION_MODEL = "models/gemini-3.7-flash"
COLLATION_FALLBACK_MODEL = "models/gemini-3.6-flash"

# Thinking budget for Stage 3, clamped to the 4k-8k band. Below 4k the model
# starts dropping items from a long note list, which is the failure this stage
# was just fixed for; above 8k buys nothing for what is an editing task.
COLLATION_MIN_THINKING = 4096
COLLATION_MAX_THINKING = 8192

SECONDS_BETWEEN_CALLS = 2
# Longer backoff between retry-queue attempts, to give a transient
# rate-limit/network issue more time to clear before hitting the same API again
RETRY_SECONDS_BETWEEN_CALLS = 30
# The pause before re-trying the GOOD model now comes from
# llm_util.RETRY_BACKOFF_SECONDS, since both stages build their tiers with
# llm_util.standard_tiers. A local REASONING_RETRY_BACKOFF_SECONDS lived here
# and went dead in that change -- tune the shared one instead.

CALL_TIMEOUT_SECONDS = 120

# Per-ticker outcome recorded in news_summary.json. The point of these is that
# a quiet news day and a total pipeline outage used to be byte-identical in the
# output -- ticker_count was just len(tickers) no matter what happened.
STATUS_MATERIAL = "material"   # Stage 2 kept something
STATUS_QUIET = "quiet"         # searched cleanly, nothing cleared the bar
STATUS_DEGRADED = "degraded"   # Stage 2 ladder exhausted; raw text forwarded
STATUS_FAILED = "failed"       # Stage 1 ladder exhausted; no data at all

NO_NEWS_SENTENCE = "No major news for this watchlist's tickers in the last 24 hours."

MARKET_LABELS = {"US": "US Watchlist", "INDIA": "India Watchlist"}

_INDIA_SUFFIXES = (".NS", ".BO")


# Which watchlists a news run covers when settings.news_watchlist_scope is
# empty. It used to mean "every watchlist" -- 142 slots a night across 7 lists,
# most of them research/tracking lists whose news nobody reads daily. The
# invested lists are the ones worth a digest by default; everything else is
# opt-in.
DEFAULT_NEWS_SCOPE_GROUP = "all_invested"


def resolve_news_scope(scope, watchlists, groups=None):
    """The market keys a news run should cover, given settings' scope list.

    `scope` may hold market keys, combined GROUP keys ("all_invested",
    "all_watchlist"), or a mix. Storing the group key rather than its members
    is deliberate: editing a group's membership on its combined tab then
    re-scopes the news digest automatically, instead of leaving a stale copy of
    the old membership in settings.json.

    An empty scope resolves to DEFAULT_NEWS_SCOPE_GROUP, not to everything.

    Returns keys in `watchlists` order (registry order), de-duplicated -- a
    ticker list can be reachable both directly and through a group, and the
    caller uses this to filter a dict, so order and uniqueness both matter.

    Falls back to every watchlist if resolution yields nothing, which can only
    happen if the group is missing or its members were renamed. A digest that
    silently covers nothing is worse than one that covers too much.
    """
    if groups is None:
        from stock_data import load_watchlist_groups
        groups = load_watchlist_groups()

    keys = list(scope or [])
    if not keys:
        keys = [DEFAULT_NEWS_SCOPE_GROUP]

    wanted = set()
    for key in keys:
        if key in groups:
            wanted.update(groups.get(key) or [])
        else:
            wanted.add(key)

    resolved = [m for m in watchlists if m in wanted]
    if not resolved:
        print(f"[news] scope {scope!r} resolved to no known watchlist -- falling back to all.")
        return list(watchlists)
    return resolved


def _market_label(market):
    """Display label for a market: the registry's label if registered,
    falling back to the legacy MARKET_LABELS dict, then the raw key."""
    from stock_data import load_markets_registry
    registry_label = load_markets_registry().get(market, {}).get("label")
    return registry_label or MARKET_LABELS.get(market, market)


def get_gemini_api_key(st_secrets=None):
    """Returns ONE Gemini API key -- the primary if configured, else whichever
    is. Kept because callers use it as an "is Gemini configured at all?" test
    and to pass a key down into the analysis helpers.

    Discovery itself lives in llm_util.gemini_api_keys now, because there are
    two keys (GEMINI_API_KEY and GEMINI_API_KEY_BACKUP) and every actual CALL
    should rotate between them -- see llm_util.make_client. Returning a single
    key from here is only about configuration checks; it is not the thing that
    picks which key a request uses."""
    keys = llm_util.gemini_api_keys(st_secrets=st_secrets)
    return keys[0][1] if keys else None


def get_gemini_api_keys(st_secrets=None):
    """Every configured key, for callers that pass keys down to a helper which
    will build a rotating client (the dashboard does this)."""
    return [k for _, k in llm_util.gemini_api_keys(st_secrets=st_secrets)]


def _bare_ticker(ticker):
    """Strips the .NS/.BO exchange suffix so the search prompt reads
    naturally (e.g. "RELIANCE" instead of "RELIANCE.NS")."""
    if ticker.endswith(_INDIA_SUFFIXES):
        return ticker.rsplit(".", 1)[0]
    return ticker


def _display_name(ticker, ticker_names):
    """"Company Name (BARETICKER)" when the snapshot knows the company,
    else just the bare ticker."""
    bare = _bare_ticker(ticker)
    company = (ticker_names or {}).get(ticker)
    return f"{company} ({bare})" if company and company != ticker else bare


def ticker_window_date(ticker):
    """The exchange-local calendar date anchoring ONE ticker's search window.

    market_window_date below resolves this per MARKET, from whether ANY ticker
    in the list is Indian -- which is right for today's watchlists (each is
    single-region) and wrong the moment one mixes them: at the 8 PM ET run,
    Kolkata is already on the next calendar day, so every US ticker in a mixed
    list would be asked for a New York date that has not happened yet. That is
    the same bug the market_window_date docstring describes fixing for UTC.

    Resolved off the TICKER SUFFIX, matching what fundamentals_eval's
    search_window_days had to be changed to for exactly this reason: watchlists
    are user-creatable from the dashboard, so nothing stops a mixed one.
    """
    tz = ZoneInfo("Asia/Kolkata") if ticker.endswith(_INDIA_SUFFIXES) else ZoneInfo("America/New_York")
    return datetime.now(tz).strftime("%Y-%m-%d")


def market_window_date(market, tickers):
    """The exchange-local calendar date that anchors the search window.

    Was datetime.now(timezone.utc).strftime(...), which is wrong for US
    markets: the workflow fires at 00:00 UTC, which is 8 PM ET the PREVIOUS
    calendar day. The shipped 2026-08-16 file is the proof -- generated_at
    2026-08-16T01:21Z (Aug 15, 9:21 PM ET) but labelled as_of 2026-08-16, so a
    digest covering Aug 15's US session was dated Aug 16 and the model was told
    to search a window whose second half hadn't happened yet.

    A single UTC date cannot be right for both markets at that hour, so resolve
    it per market off the tickers' listing venue."""
    is_india = any(t.endswith(_INDIA_SUFFIXES) for t in (tickers or []))
    tz = ZoneInfo("Asia/Kolkata") if is_india else ZoneInfo("America/New_York")
    return datetime.now(tz).strftime("%Y-%m-%d")


def _cutoff_date(as_of_date):
    return (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


# Both of these now live in llm_util so expert_views and fundamentals_eval get
# the same behavior -- they had their own drifted copies with no retry tier and
# no terminal-error gate. Kept as module-level aliases so this file's existing
# call sites and tests are untouched.
_generate_with_timeout = llm_util.generate_with_timeout
_is_retryable = llm_util.is_retryable


def _extract_sources(resp):
    """Grounding chunks -> [{"title", "url"}]. Note these are Vertex redirect
    URLs that expire (roughly 30 days), so archived digests will have dead
    links -- that's upstream behaviour, not something we can persist around."""
    sources = []
    gm = resp.candidates[0].grounding_metadata if resp.candidates else None
    if gm and gm.grounding_chunks:
        for chunk in gm.grounding_chunks:
            web = getattr(chunk, "web", None)
            if web and web.uri:
                sources.append({"title": web.title, "url": web.uri})
    return sources


def fetch_single_raw_news(client, ticker, market, as_of_date, ticker_names=None, model=SEARCH_MODEL):
    """Stage 1: grounded search for ONE ticker.

    Returns (text, sources) where `text` is the model's BARE output -- callers
    add the "**Name (TICKER)**:" header themselves. It used to return that
    header pre-attached, which made the return value unconditionally truthy:
    the caller's `if raw_text:` check could never be false, the "Stage1 FAILED"
    branch was dead code, and a search that found nothing was counted as a
    success and forwarded to Stage 2 as if it were news.

    Raises the last exception if the whole model ladder is exhausted."""
    from stock_data import get_exchange_label

    cutoff_date = _cutoff_date(as_of_date)
    exchange = get_exchange_label(market, ticker)
    name = _display_name(ticker, ticker_names)

    prompt = (
        f"You are a financial news researcher. For the {exchange} stock {name} -- "
        f"search for news, announcements, press releases, analyst notes, and stock moves between "
        f"{cutoff_date} and {as_of_date} (the last 24 hours), AND any major upcoming scheduled events in the next 3-4 days (e.g. earnings (latest quarter only), launches). Report any news items you find, "
        "specifying the exact date of each item. Be extremely concise. If there is no news, output nothing."
    )

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(tools=[grounding_tool])

    # standard_tiers = the good model, the good model again after a short
    # backoff, then the fallback. The hand-rolled [model, fallback] list this
    # replaces had NO same-model retry, so a single transient 429 -- the
    # likeliest failure on a ~115-call serial run -- permanently demoted that
    # ticker to the weaker search model for the night.
    #
    # This stage keeps its own loop rather than calling run_model_ladder
    # because the caller inspects the RAISED exception to choose between the
    # retry queue and a terminal failure, and the ladder helper does not
    # surface it.
    last_exc = None
    for attempt_model, backoff in llm_util.standard_tiers(model, SEARCH_FALLBACK_MODEL):
        if backoff:
            time.sleep(backoff)
        try:
            resp = _generate_with_timeout(client, attempt_model, prompt, config,
                                          timeout=CALL_TIMEOUT_SECONDS)
            return (resp.text or "").strip(), _extract_sources(resp)
        except Exception as e:
            last_exc = e
            print(f"  [stage1 {attempt_model} failed] {ticker}: {e}")
            if llm_util.is_model_unavailable(e):
                # A wrong/retired/unavailable model id says nothing about the
                # next tier. is_retryable() returns False for these, so the old
                # `break` meant a typo'd news_search_model -- a settings value
                # editable straight from the dashboard -- failed EVERY ticker
                # without ever trying the fallback that would have worked.
                continue
            if not _is_retryable(e):
                break
    raise last_exc


def _run_reasoning(client, prompt, model, budget, label, subject=""):
    """Shared Stage 2 / Stage 3 model ladder:
    `model` -> `model` again after a short backoff -> REASONING_FALLBACK_MODEL.

    Returns (text, ok). `text` is the model's stripped output -- an EMPTY
    string is a legitimate, expected result (both prompts explicitly instruct
    the model to output nothing when nothing clears the bar), which is exactly
    what the old `resp.text or raw_text` idiom could not express."""
    def _config_for(m):
        kwargs = {}
        if "gemma" not in m:
            if isinstance(budget, str):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=budget)
            else:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        return types.GenerateContentConfig(**kwargs)

    # run_model_ladder is the same shape this loop was hand-rolling (good model,
    # good model after a backoff, fallback) plus the one behaviour it lacked:
    # stepping PAST an unavailable model id instead of treating it as terminal.
    # news_reasoning_model is settings-driven, so a bad value used to take out
    # Stage 2 for every ticker and silently forward unfiltered search text.
    text, used = llm_util.run_model_ladder(
        client, prompt,
        llm_util.standard_tiers(model, REASONING_FALLBACK_MODEL),
        _config_for, label=label, subject=subject,
        timeout=CALL_TIMEOUT_SECONDS,
        on_success=lambda resp: (resp.text or "").strip(),
    )
    # `used`, not `text` -- an empty string is a legitimate answer here (both
    # prompts say to output nothing when nothing qualifies), so success cannot
    # be inferred from the text being non-empty.
    return (text or ""), used is not None


def filter_batch_with_reasoning(client, raw_text, tickers, market, as_of_date,
                                ticker_names=None, model=REASONING_MODEL, budget=4096):
    """Stage 2: strict recency + materiality filtering for one ticker.

    Returns (text, status): status is "ok" when a model answered (an empty
    `text` then means "nothing material here", the common case), or "degraded"
    when the whole ladder failed and the caller should forward the unfiltered
    Stage 1 text instead.

    This used to `return resp.text or raw_text`, which made the correct empty
    answer indistinguishable from a crash and substituted the UNFILTERED web
    text back in -- so the filter was bypassed for precisely the tickers it had
    worked on. That is why the shipped digests carried items this prompt
    explicitly drops, e.g. "Disclosed newspaper advertisement regarding the
    notice of interim dividend" and "Submitted the Q1 FY27 earnings conference
    call transcript"."""
    if not raw_text or not raw_text.strip():
        return "", "ok"

    cutoff_date = _cutoff_date(as_of_date)
    names = ", ".join(_display_name(t, ticker_names) for t in (tickers or []))
    subject = names or "the ticker below"

    prompt = (
        f"You are a senior financial analyst. Below is raw news text gathered for: {subject}.\n\n"
        f"Today is {as_of_date}. STRICT RECENCY RULE: Evaluate each news item. Keep ONLY items dated "
        f"{cutoff_date} or {as_of_date} (the last 24 hours), OR major upcoming scheduled events in the "
        f"next 3-4 days. Drop anything older or undated.\n\n"
        "STRICT MATERIALITY RULE:\n"
        "- KEEP ONLY: earnings released in the last 2 days (latest quarter only), upcoming earnings/events in the next 3-4 days (latest quarter only), M&A/acquisitions, FDA/regulatory approvals, "
        "important board announcements (EXCLUDING dividend and generic day-to-day announcements), analyst coverage and important stock targets, "
        "major contract wins/losses, big institutional and promoter activity, and significant stock movements (+-3%).\n"
        "- DROP ENTIRELY: routine scheduled board meetings/AGMs/EGMs with no outcome yet, ordinary "
        "insider option exercises, routine block trades, minor price fluctuations, dividend announcements, and generic no-news filler.\n\n"
        "For items that pass both rules, write short, clear bullet points under bold ticker headers. "
        "If a ticker has no qualifying items, omit it completely. DO NOT write 'no significant news', just skip the ticker entirely.\n\n"
        f"RAW TEXT:\n{raw_text}"
    )

    text, ok = _run_reasoning(client, prompt, model, budget, "stage2", subject)
    if not ok:
        return raw_text, STATUS_DEGRADED
    return text, "ok"


def _collation_thinking_budget(budget):
    """Stage 3's thinking budget, clamped into the 4k-8k band."""
    try:
        value = int(budget)
    except (TypeError, ValueError):
        return COLLATION_MAX_THINKING
    return max(COLLATION_MIN_THINKING, min(COLLATION_MAX_THINKING, value))


def collate_market_summary(client, market, batch_texts, as_of_date=None,
                           model=None, budget=None, fallback_model=None):
    """Stage 3: collate one market's surviving notes into the daily brief.

    Returns (summary, status), same contract as Stage 2. A legitimate empty
    answer means nothing in the whole watchlist cleared the bar, and yields the
    standard no-news sentence -- NOT, as before, the raw concatenation of every
    ticker's unfiltered web text."""
    combined = "\n\n---\n\n".join(t for t in batch_texts if t and t.strip())
    if not combined.strip():
        return NO_NEWS_SENTENCE, "ok"

    as_of_date = as_of_date or datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    cutoff_date = _cutoff_date(as_of_date)

    # Stage 3 EDITS, it does not re-filter.
    #
    # Every note reaching here has already passed Stage 2's recency and
    # materiality rules, against raw text that still carried its dates. Asking
    # this stage to judge those bars a second time made it lossy and
    # inconsistent: on 2026-08-20 Stage 2 marked 60 tickers material and Stage 3
    # emitted only 34 bullets -- india_invested went from 8 down to 1. The
    # giveaway was EBGNG.NS and UFBL.NS, which are in two watchlists and so
    # share one cached Stage 2 result: identical input text, kept in the wrap
    # earnings digest, dropped from india_invested.
    #
    # Two instructions caused it, both of which I added and neither of which the
    # original had. "drop any undated note" is fatal because Stage 2 rewrites
    # items into prose bullets and does not have to repeat the date, so a
    # perfectly recent item arrives here looking undated. And the hard
    # "ONLY items dated X or Y" equality is brittle across the ET/IST window
    # split. Recency is Stage 2's job, done once, where the dates actually live.
    prompt = (
        f"Below are news notes for the {_market_label(market)}. They have ALREADY been "
        "filtered for recency and materiality by an earlier stage. Your job is to EDIT and "
        "FORMAT them -- do NOT re-judge whether an item qualifies, and do NOT drop a ticker "
        "because its note does not restate a date.\n\n"
        f"(For context only, today is {as_of_date}.)\n\n"
        "Write ONE bullet per ticker that has a note, formatted exactly: "
        "**Company Name (Ticker)** - takeaway. Keep each bullet to a single line, and keep it "
        "tight -- a reader should be able to scan the whole list. "
        "Use the EXACT company name and ticker given in the notes; never invent names, figures "
        "or dates. If one ticker has several notes, merge them into that single bullet.\n\n"
        "EVERY ticker present in the notes below must appear exactly once in your output. "
        f"If the notes are empty, output EXACTLY ONE SENTENCE: '{NO_NEWS_SENTENCE}'\n\n"
        f"NOTES:\n{combined}"
    )

    # Stage 3's own ladder: 3.7 -> 3.6 -> 3.7 -> 3.6, both with thinking.
    # _run_reasoning is Stage 2's shape (good model twice, then a weak
    # fallback), which is the wrong ladder here -- there is no weak tier to
    # concede to.
    model = model or COLLATION_MODEL
    fallback_model = fallback_model or COLLATION_FALLBACK_MODEL
    thinking = _collation_thinking_budget(budget if budget is not None else COLLATION_MAX_THINKING)

    def _config_for(m):
        kwargs = {}
        if "gemma" not in m:            # Gemma rejects a thinking config
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking)
        return types.GenerateContentConfig(**kwargs)

    text, used = llm_util.run_model_ladder(
        client, prompt,
        llm_util.retry_pair_tiers(model, fallback_model),
        _config_for, label="stage3", subject=market,
        on_success=lambda resp: (resp.text or "").strip(),
    )
    ok = used is not None
    if ok:
        print(f"  [stage3 {market}] collated by {used} (thinking={thinking})")
    if not ok:
        # Honest degradation: forward what we have rather than silently claim a
        # quiet day, and let the caller mark the market degraded.
        return combined, STATUS_DEGRADED
    return (text or NO_NEWS_SENTENCE), "ok"


def collation_dropped_tickers(summary, material_tickers):
    """Material tickers that Stage 3 failed to carry into the final summary.

    A prompt cannot be trusted to be lossless, so the loss is measured rather
    than assumed -- this is what turns "the digest looks thin" into a number in
    the output. Matches on the bare ticker, case-insensitively, since Stage 3
    renders "RELIANCE" rather than "RELIANCE.NS".
    """
    haystack = (summary or "").upper()
    # Word boundaries, not a bare substring test. "Q" (Qnity) matches inside
    # "QURE" and "Q1", "SE" inside "RAISED", "ARM" inside "NON-FARM" -- so a
    # short ticker that Stage 3 really did drop would be reported as present.
    # Nothing was mis-reported in the 2026-09-02 digest (the colliding symbols
    # were all quiet, and this function only looks at material ones), but the
    # module docstring records this exact class of bug biting the removed
    # DuckDuckGo tier, where ticker "Q" matched every line containing a q.
    return [t for t in material_tickers
            if not re.search(rf"\b{re.escape(_bare_ticker(t).upper())}\b", haystack)]


def build_news_summary(watchlists, api_key):
    """Runs the 3-stage pipeline for every market in `watchlists`, respecting
    the ``news_watchlist_scope`` setting (empty = all markets).

    Returns a dict shaped:
        {"as_of", "generated_at", "totals": {...},
         "markets": {key: {"summary", "sources", "ticker_count", "counts",
                           "collate_status",
                           "tickers": {TICKER: {"status", "sources"}}}}}
    """
    from stock_data import load_settings, load_data_snapshot

    settings = load_settings()

    # Scope filtering. The saved list may name watchlists, combined groups, or
    # both, and an empty list means the default group rather than everything --
    # see resolve_news_scope.
    scope = settings.get("news_watchlist_scope", [])
    selected = resolve_news_scope(scope, watchlists)
    print(f"[news] scope {scope or '(default)'} -> {', '.join(selected)}")
    watchlists = {k: v for k, v in watchlists.items() if k in selected}
    if not watchlists:
        print(f"[news] news_watchlist_scope={scope} matched no watchlist with tickers. Nothing to process.")
        return {
            "as_of": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "totals": {},
            "markets": {},
        }

    search_model = settings.get("news_search_model", SEARCH_MODEL)
    reasoning_model = settings.get("news_reasoning_model", REASONING_MODEL)
    if not reasoning_model.startswith("models/"):
        reasoning_model = f"models/{reasoning_model}"

    collation_model = settings.get("news_collation_model", COLLATION_MODEL)
    collation_fallback = settings.get("news_collation_fallback_model", COLLATION_FALLBACK_MODEL)
    collation_budget = settings.get("news_collation_thinking_budget", COLLATION_MAX_THINKING)

    raw_budget = settings.get("news_reasoning_budget", 4096)
    thinking_budget = int(raw_budget) if isinstance(raw_budget, str) and raw_budget.isdigit() else raw_budget

    client = llm_util.make_client(api_key)
    result = {
        # Top-level as_of is for display and is dated in ET, which is when the
        # scheduled run actually happens (8 PM ET).
        "as_of": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {},
        "markets": {},
    }

    snapshot = load_data_snapshot()
    ticker_names = {}
    if snapshot and "per_market" in snapshot:
        for mkt_data in snapshot["per_market"].values():
            for row in mkt_data:
                if "company_name" in row:
                    ticker_names[row["ticker"]] = row["company_name"]

    # Stage 1 and Stage 2 are both purely per-ticker, so memoise them for the
    # whole run. Today 110 watchlist slots are 105 unique tickers -- AMKR sits
    # in three watchlists and META/ZS/JMFINANCIL.NS in two -- and every repeat
    # used to cost a fresh grounded search plus a fresh reasoning call, and
    # produced independently-worded text so the same news read differently in
    # each digest. Keyed on the WINDOW DATE as well as the ticker so a ticker
    # that ever spans a US and an Indian watchlist can't reuse the wrong day's
    # window; and on the FULL ticker, since _bare_ticker would collide across
    # exchanges.
    search_cache = {}   # (ticker, window_date) -> (text, sources) | Exception
    filter_cache = {}   # (ticker, window_date) -> (text, status)
    counters = {"cache_hits": 0}
    first_call = [True]

    for market in watchlists.keys():
        tickers = watchlists.get(market, [])
        if not tickers:
            result["markets"][market] = {
                "summary": "No tickers in this watchlist.", "sources": [],
                "ticker_count": 0, "counts": {}, "collate_status": "ok", "tickers": {},
            }
            continue

        window_date = market_window_date(market, tickers)
        print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] === {market}: "
              f"{len(tickers)} tickers, window ending {window_date} ===")

        filtered_texts = []
        # Stage 2's note per ticker, so a ticker Stage 3 drops can be recovered
        # verbatim below instead of merely counted.
        note_by_ticker = {}
        all_sources = []
        ticker_records = {}
        retry_queue = []

        def record(ticker, status, sources=None):
            ticker_records[ticker] = {"status": status, "sources": sources or []}

        def run_stage2(ticker, raw_text, sources, window):
            """Stage 2 for one ticker, memoised. Appends to filtered_texts and
            writes the ticker's record. `window` is the TICKER's window date,
            which equals the market's for a single-region watchlist and differs
            for a mixed one -- see ticker_window_date."""
            key = (ticker, window)
            if key in filter_cache:
                counters["cache_hits"] += 1
                clean, status = filter_cache[key]
            elif not raw_text:
                # Search succeeded but found nothing -- don't spend a reasoning
                # call proving that.
                clean, status = "", "ok"
                filter_cache[key] = (clean, status)
            else:
                header = f"**{_display_name(ticker, ticker_names)}**:\n{raw_text}"
                clean, status = filter_batch_with_reasoning(
                    client, header, [ticker], market, window,
                    ticker_names=ticker_names, model=reasoning_model, budget=thinking_budget,
                )
                filter_cache[key] = (clean, status)

            if status == STATUS_DEGRADED:
                filtered_texts.append(clean)
                note_by_ticker[ticker] = clean
                record(ticker, STATUS_DEGRADED, sources)
            elif clean:
                filtered_texts.append(clean)
                note_by_ticker[ticker] = clean
                record(ticker, STATUS_MATERIAL, sources)
            else:
                record(ticker, STATUS_QUIET, sources)

        for i, ticker in enumerate(tickers):
            tw = ticker_window_date(ticker)
            key = (ticker, tw)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            progress = f"[{ts}] [{market}] [{i+1}/{len(tickers)}] {ticker}"

            if key in search_cache:
                cached = search_cache[key]
                counters["cache_hits"] += 1
                if isinstance(cached, Exception):
                    print(f"{progress} - cached FAILURE, skipping")
                    record(ticker, STATUS_FAILED)
                    continue
                raw_text, sources = cached
                print(f"{progress} - Stage1 cache hit")
                all_sources.extend(sources)
                run_stage2(ticker, raw_text, sources, tw)
                continue

            if not first_call[0]:
                time.sleep(SECONDS_BETWEEN_CALLS)
            first_call[0] = False

            print(f"{progress} - Stage1 search starting...")
            t0 = time.time()
            try:
                raw_text, sources = fetch_single_raw_news(
                    client, ticker, market, tw,
                    ticker_names=ticker_names, model=search_model,
                )
            except Exception as e:
                # Timestamp fresh here -- the old handlers reused `ts` captured
                # before the search started, so failures logged up to 4 minutes
                # early.
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                if _is_retryable(e):
                    print(f"[{ts}] [{market}] {ticker} - Stage1 exhausted ({e}). Queued for retry.")
                    retry_queue.append(ticker)
                else:
                    print(f"[{ts}] [{market}] {ticker} - Stage1 TERMINAL ({e}). Not retrying.")
                    search_cache[key] = e
                    record(ticker, STATUS_FAILED)
                continue

            elapsed1 = time.time() - t0
            search_cache[key] = (raw_text, sources)
            all_sources.extend(sources)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            if not raw_text:
                print(f"[{ts}] [{market}] {ticker} - Stage1 found nothing ({elapsed1:.1f}s)")
                run_stage2(ticker, raw_text, sources, tw)
                continue
            print(f"[{ts}] [{market}] {ticker} - Stage1 done ({elapsed1:.1f}s), Stage2 starting...")
            t0_2 = time.time()
            run_stage2(ticker, raw_text, sources, tw)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] [{market}] {ticker} - {ticker_records[ticker]['status']} "
                  f"(search {elapsed1:.1f}s + filter {time.time()-t0_2:.1f}s)")

        if retry_queue:
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] === Retry queue ({len(retry_queue)}) ===")
            for i, ticker in enumerate(retry_queue):
                time.sleep(RETRY_SECONDS_BETWEEN_CALLS)
                tw = ticker_window_date(ticker)
                key = (ticker, tw)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] [RETRY] [{market}] [{i+1}/{len(retry_queue)}] {ticker} - Stage1 starting...")
                try:
                    raw_text, sources = fetch_single_raw_news(
                        client, ticker, market, tw,
                        ticker_names=ticker_names, model=search_model,
                    )
                except Exception as e:
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"[{ts}] [RETRY] [{market}] {ticker} - FAILED: {e}")
                    search_cache[key] = e
                    record(ticker, STATUS_FAILED)
                    continue
                search_cache[key] = (raw_text, sources)
                all_sources.extend(sources)
                run_stage2(ticker, raw_text, sources, tw)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] [RETRY] [{market}] {ticker} - {ticker_records[ticker]['status']}")

        counts = {
            status: sum(1 for r in ticker_records.values() if r["status"] == status)
            for status in (STATUS_MATERIAL, STATUS_QUIET, STATUS_DEGRADED, STATUS_FAILED)
        }
        print(f"\n[{market}] searched={len(ticker_records)} " +
              " ".join(f"{k}={v}" for k, v in counts.items()))

        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [{market}] Stage3 collation "
              f"({len(filtered_texts)} filtered results)...")
        t0 = time.time()
        collated, collate_status = collate_market_summary(
            client, market, filtered_texts, as_of_date=window_date,
            model=collation_model, fallback_model=collation_fallback,
            budget=collation_budget,
        )
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [{market}] Stage3 done "
              f"({time.time()-t0:.1f}s, {collate_status})")

        # Measure what collation lost. Stage 3 is the one stage whose output
        # can't be checked against a schema, so a silent drop here is exactly
        # how "8 with news" ended up rendering a single bullet.
        material_tickers = [t for t, r in ticker_records.items() if r["status"] == STATUS_MATERIAL]
        dropped = collation_dropped_tickers(collated, material_tickers)
        if dropped:
            print(f"WARNING: [{market}] Stage3 dropped {len(dropped)}/{len(material_tickers)} "
                  f"material ticker(s) from the digest: {', '.join(sorted(dropped))}")
            # Append their Stage 2 notes verbatim rather than losing them. The
            # notes are still in hand at this point, so a lossy editing pass
            # costs formatting consistency, not content -- measuring the loss
            # (above) without recovering it left the reader with a warning and
            # no way to see what was missing.
            recovered = "\n\n".join(
                note_by_ticker[t].strip() for t in sorted(dropped) if note_by_ticker.get(t)
            )
            if recovered:
                collated = (f"{collated}\n\n_Not folded in by the editor "
                            f"({len(dropped)}) -- raw notes:_\n\n{recovered}")

        # Dedup the flat source list the app renders -- Stage 1 results are
        # pooled per market and repeated URLs were common (46 entries for 36
        # unique URLs in the substack watchlist). Per-ticker attribution lives
        # in `tickers` below; this list backs the "Sources (N)" expander only.
        seen_urls, deduped = set(), []
        for s in all_sources:
            if s.get("url") and s["url"] not in seen_urls:
                seen_urls.add(s["url"])
                deduped.append(s)

        result["markets"][market] = {
            "summary": collated,
            "sources": deduped,
            "ticker_count": len(tickers),
            "counts": counts,
            "collate_status": collate_status,
            "collation_dropped": sorted(dropped),
            "tickers": ticker_records,
        }

    totals = {s: 0 for s in (STATUS_MATERIAL, STATUS_QUIET, STATUS_DEGRADED, STATUS_FAILED)}
    for entry in result["markets"].values():
        for k, v in (entry.get("counts") or {}).items():
            totals[k] = totals.get(k, 0) + v
    totals["searched"] = sum(totals[s] for s in (STATUS_MATERIAL, STATUS_QUIET, STATUS_DEGRADED, STATUS_FAILED))
    totals["cache_hits"] = counters["cache_hits"]
    result["totals"] = totals
    print(f"\n[news] totals: {totals}")

    return result


def build_discord_messages(news_data, limit=1900):
    """Turns a news_summary.json-shaped dict into a list of Discord-ready
    message strings, split so none can exceed Discord's limit.

    Delegates the splitting to alerts.chunked_line_messages, which (unlike the
    loop this replaces) hard-splits a single line too long to fit rather than
    letting it fall through and get POSTed -- the realistic failure here, since
    the summary is raw LLM output and the Stage 3 degraded path forwards
    unbounded text."""
    from alerts import chunked_line_messages, escape_markdown

    messages = []
    as_of = news_data.get("as_of", "")
    for market, entry in (news_data.get("markets") or {}).items():
        if not entry:
            continue
        summary = (entry.get("summary") or "").strip()
        if not summary:
            continue
        label = escape_markdown(_market_label(market))
        title = f"**📰 {label} News — {as_of}**"

        def head_for_part(part, _title=title, _label=label):
            return _title if part == 0 else f"**{_label} News (cont'd, part {part + 1})**"

        messages.extend(chunked_line_messages(summary, limit=limit, head_for_part=head_for_part))
    return messages


def load_news_summary():
    if not os.path.exists(NEWS_SUMMARY_FILE):
        return None
    try:
        with open(NEWS_SUMMARY_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_news_summary(data):
    # Atomic: a torn write here is invisible, because load_news_summary()
    # swallows the parse error and the News tab just renders "nothing
    # generated yet".
    from stock_data import atomic_write_json
    atomic_write_json(NEWS_SUMMARY_FILE, data)
