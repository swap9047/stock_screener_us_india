# Codebase Review — 2026-07-31

General-health review of the full app (~8,000 lines of Python). Covers the working tree as of the uncommitted "sentiment hallucination fix" diff (`app.py`, `expert_views.py`, `fundamentals_eval.py`, `refresh_fundamentals.py`) on top of commit `5ff992e`.

## Systemic issues (recur across 3+ files)

1. **No atomic writes anywhere — High.** Every JSON cache (`alert_state.json`, `fundamentals.json`, `expert_views.json`, `data_snapshot.json`, `custom_columns.json`, `ticker_notes.json`, `custom_filters.json`, `market_breadth.json`) is written with plain `open(path,"w") + json.dump` — no temp-file-then-`os.replace`, no locking. A crash mid-write (GitHub Actions timeout, OOM, overlapping cron runs — confirmed to happen, not hypothetical) truncates the file, and most loaders silently swallow the resulting `JSONDecodeError` and treat it as "first run" rather than surfacing the corruption. One shared `atomic_write_json()` helper would fix this everywhere at once.

2. **The sentiment-hallucination guard checked *presence* of evidence, not *truth* of evidence — High.** `fundamentals_eval.py`'s `_has_hard_evidence` did a free-text substring check (`"eps" in earnings`), so a model asserting a fabricated EPS number, or a vague phrase like `"EPS: N/A (pending)"`, could pass as "hard evidence." **Addressed 2026-07-31**: reasoning-step schema now requires structured `eps_value` / `guidance_change` / `analyst_action` fields the model must explicitly commit to (real value or explicit null), closing the substring bypass for new results. See "Sentiment fix" below.

3. **The two AI-verdict pipelines have diverged — Medium-High.** `fundamentals_eval.py` has the post-hoc validation guard (`_validate_sentiment`); `expert_views.py` (which drives the higher-stakes ACCUMULATE/HOLD/CAUTION verdict) has no equivalent — only prompt-text rules with no code-level check, and no staleness downgrade at all, so a stale/wrong verdict can persist in `expert_views.json` indefinitely. **Still open** — out of scope for the 2026-07-31 sentiment fix by explicit decision; flagged for a follow-up pass.

4. **Reasoning-generation calls had no timeout, only the search step did — Medium-High.** In both `expert_views.py` and `fundamentals_eval.py`, `_generate_with_timeout` wraps the news-search call but the verdict/sentiment `generate_content` call is unwrapped, so a hang there blocks the batch job indefinitely without hitting the retry-queue path built for exactly this. **Still open.**

## Correctness bugs

- `app.py:2338` — `snapshot["as_of"]` accessed without a key check; `snapshot_is_usable()` doesn't verify it exists. A malformed/older-schema `data_snapshot.json` throws an uncaught `KeyError` that breaks the whole app, not just one widget. **Still open.**
- Delisted/failed tickers silently exit the pipeline (`stock_data.py` catches per-ticker fetch errors and just `print`s) and are then skipped entirely by `refresh_fundamentals.py`'s `if not row: SKIP` — so a delisted ticker's old, possibly-hallucinated fundamentals entry is never re-evaluated or downgraded. **Still open.**
- Only `TimeoutError` routed into the retry queue (`refresh_fundamentals.py`); a plain rate-limit exception (e.g. Gemini 429) fell into the generic handler, and if the prior view was already stale, one transient rate-limit blip could permanently overwrite good data with "Unknown." **Still open.**
- Missing/unparseable `as_of` was treated as "not stale" rather than "unknown, be cautious" (`_view_age_days` returns `None` → the age check never fires) — exactly the corrupted-timestamp case the staleness feature should catch. **Still open.**
- Dead UI control: `app.py`'s "Reasoning Model" selectbox still offers `gemini-3.5-flash-lite` for Expert Views and persists the selection, but `expert_views.py` now hardcodes the Gemma cascade and never reads that setting — the dropdown does nothing. **Still open.**
- `app.py`'s `_fundamentals_cell` re-derived sentiment by hand from the validation flag instead of using the value `_validate_sentiment` already returns — two copies of the same STALE/NO_DATA→sentiment mapping in two files, prone to drift. **Fixed 2026-07-31** (now uses the returned `sentiment` directly).
- Concurrency / last-write-wins on shared state: `alert_state.json` could double-fire or silently drop Discord alerts under overlapping cron runs (code comments confirm this is observed); `custom_columns.json` / `ticker_notes.json` / `custom_filters.json` can silently clobber each other under concurrent edits in the single-instance Streamlit app, with no error shown to either user. **Still open** — root cause is systemic issue #1.

## Lower-priority / hygiene

- `README.md` documents the Gemini reasoning tier that was removed from `expert_views.py` — stale as soon as that diff lands.
- Prompt injection surface: news text (from open web search) is interpolated unsanitized into both LLM prompts. Mitigated by JSON-schema-constrained output and `html.escape()` on all rendered LLM text (confirmed — no XSS path), but no defense against instruction-injection steering the verdict itself.
- No retry/backoff on rate limits — falls straight to the next model tier rather than backing off.
- Duplicated, slightly-inconsistent JSON load/save boilerplate across `stock_data.py`, `fundamentals_eval.py`, `expert_views.py`, `refresh_market_breadth.py` — same root cause as issue #1, one shared helper would consolidate both.
- `github_sync.py` correctly sources secrets from `st.secrets`/env (never hardcoded) and fails closed on partial errors, but has no conflict handling on concurrent pushes (loses the losing session's edit silently) and `trigger_github_workflow`'s `requests.post` has no timeout.

## What's solid (confirmed, not just absence of findings)

- `filters.py` / `custom_columns.py` deliberately avoid `eval()`/`exec()` in favor of a whitelisted operator set / hand-rolled AST evaluator.
- Total-failure paths in both AI pipelines are honest: they mark output as `"Analysis pending"` / `"Unknown"` rather than fabricating a verdict, and the UI correctly renders that as "Failed (Retry)."
- All LLM-derived text is `html.escape()`'d before rendering — no injection-to-XSS path.
- `github_sync.py`'s multi-step commit sequence fails closed before the final ref-move, so a mid-sync API failure can't produce a corrupt commit.

## Sentiment fix (2026-07-31 follow-up)

Separately investigated: the Sentiment column was showing a confirmed skew for Indian tickers (60% Positive / 8% Negative vs US 46% Positive / 23% Negative, n=50/26 from `fundamentals.json`). Root causes and fixes:

1. **No deterministic earnings-quarter check** — the 30-day news-search window had no real earnings-calendar awareness, and whether the current quarter had actually reported was left entirely to model self-report. **Fixed**: `fundamentals_eval.py` now fetches the ticker's actual last-reported earnings date via yfinance and cross-checks it against the model's own `earnings_report_date` field; a mismatch forces `sentiment=Unknown` with a new `STALE_QUARTER` flag (rendered in `app.py` as "⚪ Unknown (QUARTER UNCONFIRMED)"). The search window is now market-aware (45 days for India, 25 for US) to match India's longer reporting lag.
2. **Hard-evidence guard checked presence, not substance** — fixed as described in systemic issue #2 above, via structured `eps_value`/`guidance_change`/`analyst_action` fields.
3. Also fixed the `NO_DATA` check's exact-string-match bug (`view.get(f) != "N/A"` missed `"n/a"`, `"None"`, etc.) — replaced with a proper whole-field placeholder check (`_field_is_placeholder`), taking care not to reuse the substring-based `_field_has_data` helper for this (an earlier attempt at the fix regressed composite fields like `"Revenue Rs 582 cr, EPS N/A"` being wrongly flagged as no-data — caught by the test suite before landing).
4. Reasoning-step `thinking_budget` raised 4096 → 8192 (Gemma models don't support `thinking_config` at all, confirmed via `test_gemma_thinking.py`, so the thinking-capable `gemini-3.5-flash-lite` stays primary).

Test coverage: `test_refresh_logic_sim.py` extended with cases for the evidence-structure fix and the new `STALE_QUARTER` path (33/33 passing).

**Live-verified 2026-07-31** (`test_sentiment_fix_live.py`, real Gemini + yfinance calls against `NH.NS`, `SANSERA.NS`, `TWST`; `fundamentals.json` untouched):
- First run surfaced a real false positive: `NH.NS` had genuinely fresh, well-evidenced data (EPS, revenue, analyst upgrade for a quarter that reported that same day) but the model left `earnings_report_date` null, so `quarter_verified` wrongly came back `False` and a legitimately good Positive call got discarded as `STALE_QUARTER`.
- **Fixed**: added CRITICAL RULE 5 to the reasoning prompt making `earnings_report_date` mandatory whenever `earnings_summary`/`eps_value`/`analyst_coverage` contain real data. Re-ran against the same ticker — `earnings_report_date` now populated (`2026-07-26`, within tolerance of the real `2026-07-31` report date), `quarter_verified=True`, sentiment correctly stayed Positive.
- `SANSERA.NS` and `TWST` behaved correctly on both runs (evidence-based Neutral/Positive calls driven by real cited evidence, no exceptions on the new yfinance/quarter-check path).
- Not yet done: a full-batch re-tally of the India/US Positive/Negative split (this test only covered 3 tickers) to confirm the fix narrows the skew across the whole watchlist. The user will trigger this via GitHub Actions rather than a local run.

**GitHub Actions readiness (2026-07-31)**: audited `.github/workflows/fundamentals.yml` before the user triggers it there. The workflow itself needed no changes — Python 3.11 matches the codebase, `requirements.txt` already covers every import (`yfinance`, `google-genai`) including the new yfinance usage, `GEMINI_API_KEY` is correctly wired via `${{ secrets.GEMINI_API_KEY }}`, persistence is a direct `git commit`+`push` of `fundamentals.json`, and the default 360-minute job timeout comfortably covers the 77-ticker watchlist (~25-40 min estimated). One real gap was found and fixed: the new `_fetch_last_reported_earnings_date` yfinance call had no timeout (unlike the Gemini calls, which all use `_generate_with_timeout`) — on GitHub-hosted runners, which share IP ranges Yahoo Finance sometimes rate-limits/slows, a hang there could have stalled the whole sequential batch job. Wrapped it in the same `ThreadPoolExecutor`-based 15s timeout pattern already used elsewhere in this file, failing open (`return None`) on timeout exactly like the existing exception path. Re-verified: 33/33 unit tests still pass, and a live re-run against the same 3 tickers shows the timeout wrapper is transparent on the normal path (no behavior change, no exceptions).
