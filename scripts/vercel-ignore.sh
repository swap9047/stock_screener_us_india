#!/bin/sh
# Vercel's "ignored build step" (vercel.json: ignoreCommand). Exit 0 = skip this deployment, exit 1 = build it.
#
# Vercel's Git integration deployed every commit on every branch (27 PRs in 11 days), and each deployment keeps
# its own copy of the pandas/numpy/yfinance function bundle: the free team reached 75% of its 10 GB Function
# Storage, past which Vercel deletes unprotected old deployments itself. So a deployment is made only for
# `main`, and only when a file the two functions (api/market.py, api/mcp.py) need changed. The live data never
# goes through Vercel (it is committed to the data repo and written to the research library's database), so a
# skipped deployment holds nothing back.
#
# WATCHED lists those files: api/, the Vercel config, the dependency list and every module the functions
# import, directly or through another. checks/test_vercel_deploys.py computes that import closure and fails
# when a module is missing here -- add it when api/*.py starts importing a new one.
#
# The comparison is with the last deployed commit (VERCEL_GIT_PREVIOUS_SHA), so a push of several commits still
# deploys when an earlier one changed the functions; without it, with the parent. Any failure to compare (a
# commit the clone doesn't have, no parent) builds: a needless deployment is cheaper than a missing one.
WATCHED="api vercel.json .vercelignore requirements.txt alerts.py custom_columns.py expert_views.py filters.py fundamentals_eval.py glossary.py index_registry.py json_store.py llm_util.py market_api.py market_http.py market_lookup.py market_lists.py news_summary.py stage_analysis.py stock_data.py ticker_notes.py"

[ "$VERCEL_GIT_COMMIT_REF" != "main" ] && exit 0
cd "$(git rev-parse --show-toplevel)" || exit 1
# shellcheck disable=SC2086  # WATCHED is a list of paths without spaces
git diff --quiet "${VERCEL_GIT_PREVIOUS_SHA:-HEAD^}" HEAD -- $WATCHED && exit 0
exit 1
