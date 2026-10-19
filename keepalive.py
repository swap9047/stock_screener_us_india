"""Open the app in a real browser so Streamlit Community Cloud does not put it to sleep.

Run by .github/workflows/keep-alive.yml. The app sleeps after about 12 hours with no
browser session; a plain HTTP request does not count because the timer follows the
websocket a real page opens. This visits the page, clicks the wake-up button when the
app is already asleep, waits until the app itself has rendered, then stays a moment so
the script run and the connection register.

No logic worth duplicating lives anywhere else. The app URL arrives in APP_URL (a
secret): this repo and its Actions logs are public, so it is never printed.
"""
import os
import sys
import time

from playwright.sync_api import Error as PlaywrightError, sync_playwright

WAKE_TEXT = "get this app back up"
APP_SELECTOR = '[data-testid="stApp"]'
# Community Cloud serves the app inside an iframe of this title; a self-hosted
# Streamlit has no iframe, so the page itself is checked as well.
APP_FRAME = 'iframe[title="streamlitApp"]'
LOAD_TIMEOUT_S = 150    # a sleeping app needs a minute or so to boot
HOLD_S = 20             # stay connected after the app shows up


def app_visible(page):
    # While the app boots the iframe may not exist yet, and counting inside a frame
    # that is missing raises instead of returning 0 -- that is "not yet", not a failure.
    try:
        if page.locator(APP_SELECTOR).count():
            return True
        return page.frame_locator(APP_FRAME).locator(APP_SELECTOR).count() > 0
    except PlaywrightError:
        return False


def visit(url, load_timeout_s=LOAD_TIMEOUT_S, hold_s=HOLD_S):
    """Return "awake" or "woken" when the app rendered, raise RuntimeError otherwise."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            woken = False
            deadline = time.time() + load_timeout_s
            while time.time() < deadline:
                wake = page.get_by_text(WAKE_TEXT, exact=False)
                if wake.count() and wake.first.is_visible():
                    wake.first.click()
                    woken = True
                    time.sleep(5)
                    continue
                if app_visible(page):
                    time.sleep(hold_s)
                    return "woken" if woken else "awake"
                time.sleep(2)
            raise RuntimeError("the app never rendered")
        finally:
            browser.close()


if __name__ == "__main__":
    app_url = os.environ.get("APP_URL", "").strip()
    if not app_url:
        print("::error::APP_URL secret is not set")
        sys.exit(1)
    print("app was", visit(app_url))
