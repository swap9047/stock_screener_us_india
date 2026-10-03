"""Sentiment: guidance outranks the quarter, a quoted outlook fills in where
there is no guidance, and the Sentiment cell shows which one drove it.

  rule 7  forward guidance decides when it and the reported quarter disagree
  rule 8  management's stated outlook (improving/steady/cautious), kept only
          with management's own words, and never directional on its own
  rule 9  an analyst action needs a named brokerage; algorithmic and website
          ratings (StockInvest.us, MarketsMojo, ...) are dropped
  tag     "Guidance ↑" / "Outlook ↓" next to the label

On 2026-09-26 only 12 of 124 views recorded a guidance change; where one was
found the verdict already followed it. The gap was coverage and an unwritten
precedence, not weighting.

Offline: invented tickers, stubbed model calls, no data files, no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import fundamentals_eval as fe
import llm_util
import refresh_fundamentals as rf

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


# --- the prompts --------------------------------------------------------------
p = fe.build_sentiment_prompt("Acme Corp", "ACME", "some news")
r6, r7, r8 = p.find("\n6. "), p.find("\n7. Forward guidance outranks"), p.find("\n8. If there is no explicit guidance")
check(0 <= r6 < r7 < r8, "rules 7 and 8 are appended after rule 6 (the guards cite rules 2-3 by number)")
check("LOWERED guidance is \"Negative\"" in p and "RAISED guidance can be \"Positive\"" in p,
      "rule 7 spells out guidance beating the quarter both ways")
# Changed 2026-10-03 (owner): Sentiment is forward-looking, so a quoted outlook
# now decides on its own; the reported quarter no longer does.
check("decides the sentiment on its own" in p, "rule 8: a quoted outlook decides on its own")
check('"outlook_tone": "improving" | "steady" | "cautious" | null' in p and '"outlook_quote"' in p,
      "the schema asks for outlook_tone and outlook_quote")

captured = {}
_real = llm_util.run_model_ladder
llm_util.run_model_ladder = lambda client, prompt, *a, **k: (captured.setdefault("p", prompt), (None, None))[1]
try:
    fe.fetch_fundamental_news(None, "ACME", "us_invested", "Acme Corp", is_retry=True)
finally:
    llm_util.run_model_ladder = _real
check("management's own stated outlook" in captured.get("p", ""),
      "the news search asks for management's outlook when there is no formal guidance")

# --- the outlook is kept only with management's words -------------------------
def norm(tone, quote):
    return fe.normalize_view({"sentiment": "positive", "outlook_tone": tone, "outlook_quote": quote})


v = norm(" Improving ", "Management on 2026-07-24: 'demand stays strong'")
check(v["outlook_tone"] == "improving" and v["sentiment"] == "Positive", "a quoted tone is kept and tidied")
check(norm("improving", None)["outlook_tone"] is None, "a tone with no quote is dropped")
check(norm("improving", "N/A")["outlook_tone"] is None, "a placeholder quote counts as no quote")
check(norm("bullish", "they said so")["outlook_tone"] is None, "a tone outside improving/steady/cautious is dropped")
check(all(k in norm(None, None) and norm(None, None)[k] is None
          for k in ("outlook_tone", "outlook_quote", "analyst_action", "analyst_firm")),
      "every evidence key is always present")

# --- an outlook alone now makes a directional verdict (owner, 2026-10-03) ------
only_outlook = {"as_of": fe.datetime.now(fe.timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "earnings_summary": "Revenue up 10% YoY", "future_guidance": "N/A", "analyst_coverage": "N/A",
                "eps_value": None, "guidance_change": None, "analyst_action": None,
                "outlook_tone": "improving", "outlook_quote": "Management: 'we see strong demand'",
                "sentiment": "Positive"}
check(fe._validate_sentiment(only_outlook) == ("Positive", ""),
      "Positive resting on a quoted outlook alone stands")
with_eps = {**only_outlook, "eps_value": "$1.10 vs $1.00 est.", "outlook_tone": "cautious", "sentiment": "Neutral"}
check(fe._validate_sentiment(with_eps)[0] == "Neutral", "EPS beat + cautious outlook may stand as Neutral")

# --- the tag next to the label ------------------------------------------------
check(fe.evidence_tag({"guidance_change": "raised"}) == "Guidance ↑", "raised guidance -> Guidance ↑")
check(fe.evidence_tag({"guidance_change": "Lowered "}) == "Guidance ↓", "lowered guidance -> Guidance ↓")
check(fe.evidence_tag({"guidance_change": "raised", "outlook_tone": "cautious", "outlook_quote": "q"}) == "Guidance ↑",
      "explicit guidance wins over the outlook tone")
check(fe.evidence_tag({"outlook_tone": "cautious", "outlook_quote": "q"}) == "Outlook ↓",
      "with no guidance, the quoted outlook shows")
check(fe.evidence_tag({"outlook_tone": "improving"}) == "" and fe.evidence_tag({}) == "",
      "no guidance and no quoted outlook -> no tag")

# --- rule 9: analyst actions come from a named, human analyst ----------------
check("NOT an analyst action" in p and '"analyst_firm"' in p and p.find("\n9. ") > r8,
      "rule 9 and analyst_firm are in the prompt, after rule 8")


def a(action, firm):
    return fe.normalize_view({"sentiment": "Positive", "analyst_action": action, "analyst_firm": firm})


check(a("Upgrade", "Morgan Stanley")["analyst_action"] == "upgrade", "a named brokerage upgrade is kept")
check(a("upgrade", None)["analyst_action"] is None, "an action with no firm is dropped")
for src in ("StockInvest.us", "MarketsMojo", "Zacks Rank", "Seeking Alpha contributor", "TipRanks Smart Score"):
    check(a("upgrade", src)["analyst_action"] is None, f"an algorithmic/website rating is not an analyst action ({src})")

# An upgrade from a rating site used to be enough for Positive on its own.
site_only = {**only_outlook, "outlook_tone": None, "outlook_quote": None,
             "analyst_coverage": "Upgraded to Buy by StockInvest.us",
             "analyst_action": "upgrade", "analyst_firm": "StockInvest.us"}
check(fe._validate_sentiment(fe.normalize_view(dict(site_only))) == ("Neutral", "PARTIAL"),
      "Positive resting on a rating-site upgrade alone is capped at Neutral")

# --- every stored placeholder carries the new keys ----------------------------
fb = rf._unknown_fallback("test")
check(all(fb.get(k, 1) is None for k in ("outlook_tone", "outlook_quote", "analyst_firm")),
      "refresh_fundamentals' Unknown placeholder carries every new key")

src = (REPO / "app.py").read_text()
check('tag = "" if flag in ("STALE", "STALE_QUARTER", "NO_DATA") else evidence_tag(v)' in src,
      "the Sentiment cell shows the tag, but not on a stale or data-less view")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
