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
    check(models[:1] == [fe.SEARCH_MODEL],
          f"targeted search LEADS with SEARCH_MODEL (got {models[:1]})")
    check(models[-1:] == [fe.SEARCH_FALLBACK_MODEL],
          f"...and falls back to SEARCH_FALLBACK_MODEL last (got {models[-1:]})")
    check(models == [m for m, _ in llm_util.standard_tiers(fe.SEARCH_MODEL, fe.SEARCH_FALLBACK_MODEL)],
          f"...via standard_tiers, so the same-model retry is kept: {models}")

    fe.fetch_fundamental_news(object(), "ACME", "us_invested", "Acme Corp", is_retry=True)
    broad = [m for m, _ in captured2.get("fundamental-search", [])]
    check(broad == models,
          f"both passes agree on the order, so neither can drift: broad={broad} targeted={models}")
finally:
    llm_util.run_model_ladder = _real2

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
