"""The Sentiment pipeline's news window and its recency preference.

The search window decides what "the current quarter" can even mean. US sat at
25 days against a ~91-day reporting cycle, so for most of a quarter a US ticker
had no earnings news in range: on 2026-09-17, 18 of the 24 Unknown views were
tickers whose last report was 35-66 days old, i.e. outside the window. Both
markets are 50 days now, and the two prompts are asked to prefer the newest
item -- a wider window holds more, so which item wins has to be stated rather
than left to the model.

Offline: invented tickers, the ladder intercepted, no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from datetime import datetime, timedelta, timezone

import fundamentals_eval as fe
import llm_util

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- the window is one number for both markets -------------------------------
check(fe.US_SEARCH_WINDOW_DAYS == 50, f"US window is 50 (got {fe.US_SEARCH_WINDOW_DAYS})")
check(fe.INDIA_SEARCH_WINDOW_DAYS == 50, f"India window is 50 (got {fe.INDIA_SEARCH_WINDOW_DAYS})")
# Equal by assertion, not by coincidence: they were 25/45 and drifted apart
# once already, when a new watchlist's .NS tickers silently got the US number.
check(fe.US_SEARCH_WINDOW_DAYS == fe.INDIA_SEARCH_WINDOW_DAYS,
      "both markets share one window, so neither can drift")

for tk in ("ACME", "ZED.NS", "ZED.BO"):
    check(fe.search_window_days(ticker=tk) == 50, f"search_window_days({tk}) == 50")
for mkt in ("india_invested", "us_invested", "newsletter_picks", None):
    check(fe.search_window_days(market=mkt) == 50,
          f"search_window_days(market={mkt}) == 50 -- the market-only fallback agrees")


# --- both prompts prefer the newest item -------------------------------------
captured = {}


def _fake_ladder(client, prompt, tiers, config_for, label="llm", subject="", timeout=None, on_success=None):
    captured[label] = prompt
    captured["tiers"] = tiers
    return None, None          # exhausted: is_retry=True returns, never raises


_real_ladder = llm_util.run_model_ladder
llm_util.run_model_ladder = _fake_ladder
try:
    fe.fetch_fundamental_news(object(), "ACME", "us_invested", "Acme Corp", is_retry=True)
    search_prompt = captured.get("fundamental-search", "")
    check("NEWEST FIRST" in search_prompt,
          "the search prompt asks for the items newest first")
    check("exact date" in search_prompt,
          "...with each item's exact date, so the reasoning stage can order them")

    # The cutoff really is the window, not a hardcoded number: this is the
    # plumbing the constants above travel through.
    today = datetime.now(timezone.utc).date()
    expect = (today - timedelta(days=50)).strftime("%Y-%m-%d")
    check(expect in search_prompt, f"the search prompt's cutoff is 50 days back ({expect})")
    check(str(today) in search_prompt, "...through today")
finally:
    llm_util.run_model_ladder = _real_ladder

reasoning_prompt = fe.build_sentiment_prompt("Acme Corp", "ACME", "some news")
check("never overrides a newer one" in reasoning_prompt,
      "the reasoning prompt says an older item cannot override a newer one")
# The guard docstrings cite "the prompt's own rules 2-3" by number, so a new
# rule goes on the END. If this fails, something was inserted mid-list.
_rule2 = reasoning_prompt.find("2. If the current quarter's EPS")
_recency = reasoning_prompt.find("never overrides a newer one")
check(0 <= _rule2 < _recency,
      "...added after the existing numbered rules, which _validate_sentiment cites by number")

# --- both grounded searches lead with the same model -------------------------
# The targeted pass used to LEAD with 31b on the theory that a second attempt
# wants "a different reader of the same web". The 2026-09-17 runs measured that
# reader: models/gemma-4-31b-it answered 0 of 40 calls across two independent
# runs (overwhelmingly 500 INTERNAL) while 26b failed ~17%. Leading with it
# burned two rungs of every targeted search -- up to 120s each plus the backoff
# -- before reaching the model that answers.
captured2 = {}


def _fake_ladder2(client, prompt, tiers, config_for, label="llm", subject="", timeout=None, on_success=None):
    captured2[label] = list(tiers)
    return None, None


_real2 = llm_util.run_model_ladder
llm_util.run_model_ladder = _fake_ladder2
try:
    out = fe.fetch_targeted_earnings_numbers(object(), "ZED.NS", "Zed Ltd", "india_invested", "2026-07-24")
    check(out == "", "an exhausted targeted ladder returns '' and never raises")
    tiers = captured2.get("targeted-earnings", [])
    models = [m for m, _ in tiers]
    # Every rung is SEARCH_MODEL now: the 31b fallback answered 0 of ~63 calls
    # across four runs, so the model stops varying and the KEY varies instead
    # (llm_util.same_model_tiers + _pick's `avoid`).
    check(models == [fe.SEARCH_MODEL] * 3,
          f"targeted search is three attempts on SEARCH_MODEL (got {models})")
    check(models == [m for m, _ in llm_util.same_model_tiers(fe.SEARCH_MODEL)],
          f"...via same_model_tiers, so the retries and their pacing are shared: {models}")

    fe.fetch_fundamental_news(object(), "ACME", "us_invested", "Acme Corp", is_retry=True)
    broad = [m for m, _ in captured2.get("fundamental-search", [])]
    check(broad == models,
          f"both passes agree on the order, so neither can drift: broad={broad} targeted={models}")
finally:
    llm_util.run_model_ladder = _real2

# --- the reasoning ladder's last resort must answer too ----------------------
# Both Sentiment reasoning ladders ended on gemma-4-31b-it with nothing behind
# it, so a double failure of the reasoning model landed on the model that
# answered 0 of 40 calls on 2026-09-17. news_summary already uses 26b as its
# REASONING_FALLBACK_MODEL; this matches it.
_reason_fb = getattr(fe, "REASONING_FALLBACK_MODEL", None)
check(_reason_fb == "models/gemma-4-26b-a4b-it",
      f"the reasoning fallback is 26b (got {_reason_fb})")

captured3 = {}


def _fake_ladder3(client, prompt, tiers, config_for, label="llm", subject="", timeout=None, on_success=None):
    captured3[label] = [m for m, _ in tiers]
    return None, None


_real3 = llm_util.run_model_ladder
llm_util.run_model_ladder = _fake_ladder3
try:
    # news_text supplied, so the search stage is skipped and only the reasoning
    # ladder runs. An exhausted ladder yields the pending placeholder, not a raise.
    view = fe.generate_fundamental_view(
        object(), {"ticker": "ACME", "market": "us_invested", "company_name": "Acme Corp"},
        news_text="Q2 EPS $1.10 vs $1.00 est, reported 2026-09-10.", news_source="test")
    check(view.get("sentiment") == "Unknown",
          "an exhausted reasoning ladder returns the pending placeholder")
    rungs = captured3.get("sentiment", [])
    check(rungs[-1:] == [_reason_fb] and _reason_fb is not None,
          f"the sentiment ladder's last rung is the reasoning fallback (got {rungs[-1:]})")
    check("models/gemma-4-31b-it" not in rungs,
          f"...and 31b is not in the reasoning ladder at all: {rungs}")
finally:
    llm_util.run_model_ladder = _real3

# Catches the sentiment-retry ladder too, which needs a targeted follow-up to
# reach and so is not exercised above.
_fe_src = (Path(REPO) / "fundamentals_eval.py").read_text()
check('standard_tiers(model, "models/gemma-4-31b-it")' not in _fe_src,
      "no reasoning ladder in fundamentals_eval still hardcodes 31b as its fallback")
check(_fe_src.count("standard_tiers(model, REASONING_FALLBACK_MODEL)") == 2,
      "both reasoning ladders (sentiment, sentiment-retry) use the constant")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
