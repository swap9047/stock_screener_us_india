"""Every Discord batch ends with the disclaimer, and the app shows it.

All Discord output (alerts, weekly wrap-up, news digest, the app's buttons) goes
through alerts.send_discord_batch, so the footer is added there once. It rides
on the last message when that stays under the 1900-character budget the
message builders keep to, and is sent on its own otherwise.

Offline: the Discord post is replaced; no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import alerts

fails = 0


def check(ok, label):
    global fails
    fails += not ok
    print("PASS" if ok else "FAIL", label)


F = alerts.DISCORD_FOOTER
check(alerts.with_disclaimer(["a", "b"]) == ["a", f"b\n{F}"], "the footer rides on the last message")
long = "x" * (alerts._DISCORD_SAFE_LEN - 5)
check(alerts.with_disclaimer([long]) == [long, F], "a full last message gets the footer as its own message")
check(alerts.with_disclaimer([]) == [], "no messages -> no footer-only post")
check(all(len(m) <= alerts._DISCORD_SAFE_LEN for m in alerts.with_disclaimer(["y" * 1800, long])),
      "no message goes over the length budget")

posted = []
_real = alerts._post_discord
alerts._post_discord = lambda url, content: (posted.append(content), (True, ""))[1]
alerts.DISCORD_BATCH_PACING_SECONDS = 0
try:
    alerts.send_discord_batch("https://example.invalid/webhook", ["alert table"])
finally:
    alerts._post_discord = _real
check(posted == [f"alert table\n{F}"], "send_discord_batch posts the footer")

src = (REPO / "app.py").read_text()
check("st.sidebar.caption(DISCLAIMER)" in src, "the app shows the disclaimer in the sidebar")
check("not as personal investment advice" in (REPO / "expert_views.py").read_text(),
      "the Expert Take prompt asks for analysis, not personal advice")

print("TOTAL", "all passed" if not fails else "")
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
