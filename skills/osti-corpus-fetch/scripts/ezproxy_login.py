#!/usr/bin/env python3
"""
EZproxy login bootstrap. Opens a visible Chromium, navigates to UChicago EZproxy
login page, waits for user to complete CNetID+DUO, then saves storage state.

USAGE (run interactively on a Mac with display):
    python3 ezproxy_login.py
Then complete login in browser. When done, switch focus back to terminal and
press ENTER. Storage state is saved to ~/.ezproxy_state.json.

Subsequent worker runs load this state and bypass login until cookies expire (~8h).

Requires: pip install --user playwright && playwright install chromium
Tested on macOS Monterey 12.7.6 with Python 3.9.6.
"""
import asyncio, os, sys
from pathlib import Path
from playwright.async_api import async_playwright

STATE_FILE = Path.home() / ".ezproxy_state.json"
# Use any proxied URL — EZproxy will trigger login flow
START_URL = "https://www-pnas-org.proxy.uchicago.edu/doi/pdf/10.1073/pnas.2024711118"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(accept_downloads=True)
        page = await ctx.new_page()
        print(f"Opening {START_URL}", flush=True)
        await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        print("\n========================================", flush=True)
        print("Complete CNetID + DUO login in the browser.", flush=True)
        print("Once you see the actual PNAS PDF (or article page), come back here.", flush=True)
        print("Press ENTER to save session state and exit...", flush=True)
        print("========================================\n", flush=True)
        input()
        await ctx.storage_state(path=str(STATE_FILE))
        print(f"Saved storage state to {STATE_FILE}", flush=True)
        # Set restrictive perms
        try:
            os.chmod(STATE_FILE, 0o600)
        except Exception:
            pass
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
