"""Skip Gemini 3.8 Flash (owner, 2026-11-07): one switch at the top of Settings · Models.

K1 Off (the default): every step's rungs are exactly what they were.
K2 On: no rung calls 3.8 Flash; the chain starts at the next model with no wait, and the waits
   restart from the first try (2, 4, 8 ...), so nothing is spent waiting on a model that is skipped.
K3 The SAVED chain is never edited: resolve() still returns it whole (the step editor shows and
   saves that), and switching off brings 3.8 back exactly.
K4 A chain made of nothing but 3.8 is kept: a step is never left with no model.
K5 Steps without 3.8 are untouched either way.
K6 The switch lives inside `ai_models` (no new settings key, so stock_data.py and its code
   fingerprint are untouched) and survives a step being saved or reset.
K7 save_settings keeps it (round trip through a throwaway settings.json).
K8 The pipelines label a first-model answer as such, not "(Fallback)", while 3.8 is skipped.

Offline: no keys, no network.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import llm_util
import model_settings as ms

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


F38, F36, L = ms.FLASH_38, ms.FLASH_36, ms.FLASH_LITE
CHAIN = [{"model": F38, "attempts": 2, "timeout": 200}, {"model": F36, "attempts": 2, "timeout": 200},
         {"model": L, "attempts": 2, "timeout": 150}]
SAVED = {"R1": {"chain": CHAIN, "budget": 8192}, "R2": {"chain": CHAIN, "budget": 8192},
         "N3": {"chain": CHAIN, "budget": 8192}}
OFF = {"ai_models": json.loads(json.dumps(SAVED))}
ON = {"ai_models": {**json.loads(json.dumps(SAVED)), ms.SKIP_38_KEY: True}}
CLEAN = {}

# --- K1 off --------------------------------------------------------------------------------------
for step in ms.STEPS:
    check(ms.tiers(step, CLEAN) == ms.build_tiers(ms.resolve(step, CLEAN)["chain"]),
          f"K1: {step} unchanged with no switch (defaults)")
check(ms.tiers("R1", OFF) == [(F38, 0, 200), (F38, 2, 200), (F36, 4, 200), (F36, 8, 200), (L, 16, 150), (L, 30, 150)],
      f"K1: a saved R1 chain runs 3.8 x2, 3.6 x2, lite x2 while the switch is off ({ms.tiers('R1', OFF)})")
check(not ms.skipping_38(OFF) and not ms.skipping_38(CLEAN), "K1: the switch reads off when absent")

# --- K2 on ---------------------------------------------------------------------------------------
for step in ("R1", "R2", "N3"):
    t = ms.tiers(step, ON)
    check(all(m != F38 for m, _w, _t in t), f"K2: {step} makes no 3.8 Flash call while skipped")
check(ms.tiers("R1", ON) == [(F36, 0, 200), (F36, 2, 200), (L, 4, 150), (L, 8, 150)],
      f"K2: R1 starts at 3.6 with no wait, waits restart, each model keeps its timeout ({ms.tiers('R1', ON)})")
check(ms.tiers("R1", CLEAN | {"ai_models": {ms.SKIP_38_KEY: True}}) == [(F36, 0, 200), (F36, 2, 200)],
      "K2: the DEFAULT R1 chain (3.8 x2, 3.6 x2) runs 3.6 x2 while skipped")
check(ms.skipped_38_in(ms.resolve("R1", ON)["chain"], ON) and not ms.skipped_38_in(ms.resolve("R1", OFF)["chain"], OFF),
      "K2: the steps list notes the skip only while it is on")

# --- K3 the saved chain is untouched ---------------------------------------------------------------
check(ms.resolve("R1", ON)["chain"] == CHAIN and ms.resolve("R1", ON)["source"] == "saved",
      "K3: resolve() still returns the whole saved chain (what the editor shows and saves)")
back = {"ai_models": ms.with_skip_38(ON, False)}
check(ms.tiers("R1", back) == ms.tiers("R1", OFF) and ms.SKIP_38_KEY not in back["ai_models"],
      "K3: switching off brings 3.8 back exactly, and removes the key")
check(ms.with_skip_38(OFF, True) == ON["ai_models"], "K3: switching on adds only the key, every chain kept")

# --- K4 never empty --------------------------------------------------------------------------------
only38 = {"ai_models": {"R1": {"chain": [{"model": F38, "attempts": 2, "timeout": 200}], "budget": 4096},
                        ms.SKIP_38_KEY: True}}
check([m for m, *_ in ms.tiers("R1", only38)] == [F38, F38] and not ms.skipped_38_in(ms.resolve("R1", only38)["chain"], only38),
      "K4: a chain of only 3.8 is kept as it is (and not shown as skipped)")

# --- K5 steps without 3.8 ------------------------------------------------------------------------------
for step in ("N1", "N2", "E1", "E2", "F1", "F2", "F3"):
    on = {"ai_models": {ms.SKIP_38_KEY: True}}
    check(ms.tiers(step, on) == ms.tiers(step, CLEAN), f"K5: {step} (no 3.8 in it) is the same with the switch on")

# --- K6 stored inside ai_models, survives saves and resets ------------------------------------------------
check(ms.SKIP_38_KEY not in ms.STEPS, "K6: the switch's key is not a step id")
saved = {"ai_models": ms.with_step(ON, "E2", [{"model": L, "attempts": 2}], 8192)}
check(ms.skipping_38(saved) and saved["ai_models"]["R1"]["chain"] == CHAIN, "K6: saving another step keeps the switch")
reset = ms.reset_changes(ON, "R1")
check(ms.skipping_38(reset) and "R1" not in reset["ai_models"], "K6: resetting a step keeps the switch")
src_sd = (REPO / "stock_data.py").read_text()
check("skip_flash_38" not in src_sd, "K6: stock_data.py (and its snapshot fingerprint) is untouched")

# --- K7 save_settings round trip --------------------------------------------------------------------------
import stock_data as sd
tmp = tempfile.mkdtemp()
_orig = sd.SETTINGS_FILE
sd.SETTINGS_FILE = os.path.join(tmp, "settings.json")
try:
    sd.save_settings({"ai_models": ON["ai_models"]})
    again = sd.load_settings()
    check(ms.skipping_38(again) and again["ai_models"]["R1"]["chain"] == CHAIN, "K7: save_settings keeps the switch and the chains")
    sd.save_settings({"ai_models": ms.with_skip_38(again, False)})
    check(not ms.skipping_38(sd.load_settings()), "K7: ...and switching it off saves off")
finally:
    sd.SETTINGS_FILE = _orig

# --- K8 labels -------------------------------------------------------------------------------------------
for f in ("expert_views.py", "fundamentals_eval.py"):
    src = (REPO / f).read_text()
    check('model_settings.effective_chain(model_cfg["chain"], settings)[0]["model"]' in src,
          f"K8: {f} labels its answer against the first model that actually runs")
check(llm_util.retry_wait(0) == 0 and llm_util.retry_wait(1) == 2, "K8: the wait scheme the rungs use is unchanged")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
