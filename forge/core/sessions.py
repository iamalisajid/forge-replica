"""Operator login: storage state saved encrypted under ~/.forge/sessions, never in run dirs or repos."""
from __future__ import annotations

import json
import os
import re

from cryptography.fernet import Fernet

from forge.core.config import KEYS_DIR, SESSIONS_DIR


def _fernet() -> Fernet:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    path = KEYS_DIR / "session.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        os.chmod(path, 0o600)
    return Fernet(path.read_bytes())


def _path(origin: str):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / (re.sub(r"[^a-z0-9]+", "-", origin.lower()).strip("-") + ".enc")


def save(origin: str, state: dict) -> None:
    path = _path(origin)
    path.write_bytes(_fernet().encrypt(json.dumps(state).encode()))
    os.chmod(path, 0o600)


def load(origin: str) -> dict | None:
    path = _path(origin)
    if not path.exists():
        return None
    return json.loads(_fernet().decrypt(path.read_bytes()))


CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
]


def find_chrome() -> str | None:
    return next((p for p in CHROME_PATHS if os.path.exists(p)), None)


async def login_with_chrome(url: str, *, port: int = 9223) -> dict:
    """Sign in with the operator's real Google Chrome, started normally (no automation flags), so CAPTCHAs and
    SSO behave exactly as in everyday browsing and the human solves them. Afterwards forge attaches over the
    DevTools port of that dedicated profile only to read the resulting session; it never drives the login."""
    import subprocess
    import time
    import urllib.request

    from playwright.async_api import async_playwright

    from forge.core.config import FORGE_HOME
    from forge.core.store import origin_of

    chrome = find_chrome()
    if chrome is None:
        raise RuntimeError("Google Chrome not found; install it or use --playwright")
    profile = FORGE_HOME / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                             "--no-first-run", "--no-default-browser-check", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        input("Sign in in the Chrome window (solve any CAPTCHA yourself), then press Enter here... ")
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            try:
                state = await context.storage_state(indexed_db=True)
            except TypeError:
                state = await context.storage_state()
            await browser.close()
    finally:
        proc.terminate()
    save(origin_of(url), state)
    return state


async def login(url: str, *, email: str | None = None, password: str | None = None, headless: bool = False) -> dict:
    """Open a real browser for the operator to sign in (SSO, 2FA, CAPTCHA are theirs to complete).

    With email/password, a standard form is filled automatically (useful for local test apps)."""
    from playwright.async_api import async_playwright

    from forge.core.store import origin_of

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url)
        if email and password:
            await page.locator("input[type=email], input[name=email], input[name=username]").first.fill(email)
            await page.locator("input[type=password]").first.fill(password)
            await page.locator("button[type=submit], input[type=submit]").first.click()
            await page.wait_for_load_state("networkidle")
        else:
            input("Sign in in the browser window, then press Enter here... ")
        try:
            state = await context.storage_state(indexed_db=True)
        except TypeError:
            state = await context.storage_state()
        await browser.close()
    save(origin_of(url), state)
    return state
