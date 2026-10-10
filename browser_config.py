"""Shared Playwright settings for the bot and standalone workers."""

import logging
import os
import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from dotenv import dotenv_values


ENV_PATH = Path(__file__).with_name(".env")


def browser_headless() -> bool:
    """Process environment takes precedence over the project's .env file."""
    raw = os.getenv("BOT_BROWSER_HEADLESS")
    if raw is None:
        raw = dotenv_values(ENV_PATH, interpolate=False).get("BOT_BROWSER_HEADLESS")
    value = (raw or "1").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value not in {"", "1", "true", "yes", "on"}:
        logging.getLogger(__name__).warning(
            "Invalid BOT_BROWSER_HEADLESS value; using headless mode"
        )
    return True


def browser_keep_open() -> bool:
    raw = os.getenv("BOT_BROWSER_KEEP_OPEN")
    if raw is None:
        raw = dotenv_values(ENV_PATH, interpolate=False).get("BOT_BROWSER_KEEP_OPEN")
    return (raw or "0").strip().lower() in {"1", "true", "yes", "on"}


def _open_pages(browser):
    return [page for context in browser.contexts for page in context.pages if not page.is_closed()]


def _keep_open_enabled(headless: bool) -> bool:
    enabled = browser_keep_open()
    if enabled and headless:
        logging.getLogger(__name__).warning(
            "BOT_BROWSER_KEEP_OPEN ignored: set BOT_BROWSER_HEADLESS=0 to keep a visible window"
        )
    return enabled and not headless


@asynccontextmanager
async def managed_async_browser():
    """Retain the driver and browser until the user closes all windows."""
    from playwright.async_api import async_playwright

    headless = browser_headless()
    keep_open = _keep_open_enabled(headless)
    async with async_playwright() as playwright:
        browser = await playwright.firefox.launch(headless=headless)
        interrupted = False
        try:
            yield browser
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            interrupted = True
            raise
        except Exception:
            if keep_open:
                logging.getLogger(__name__).exception("Browser task failed; keeping window for inspection")
            raise
        finally:
            try:
                if keep_open and not interrupted and browser.is_connected() and _open_pages(browser):
                    logging.getLogger(__name__).info(
                        "Browser kept open after task/error; close its windows to continue"
                    )
                    while browser.is_connected() and _open_pages(browser):
                        await asyncio.sleep(0.25)
            finally:
                with suppress(Exception):
                    await browser.close()


@contextmanager
def managed_sync_browser():
    from playwright.sync_api import Error, sync_playwright

    headless = browser_headless()
    keep_open = _keep_open_enabled(headless)
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch(headless=headless)
        interrupted = False
        try:
            yield browser
        except (KeyboardInterrupt, SystemExit):
            interrupted = True
            raise
        except Exception:
            if keep_open:
                logging.getLogger(__name__).exception("Browser task failed; keeping window for inspection")
            raise
        finally:
            try:
                if keep_open and not interrupted and browser.is_connected() and _open_pages(browser):
                    logging.getLogger(__name__).info(
                        "Browser kept open after task/error; close its windows to continue"
                    )
                    while browser.is_connected():
                        pages = _open_pages(browser)
                        if not pages:
                            break
                        try:
                            # Playwright must pump events to observe manual closure.
                            pages[0].wait_for_timeout(250)
                        except Error:
                            if browser.is_connected() and not pages[0].is_closed():
                                raise
            finally:
                with suppress(Exception):
                    browser.close()
