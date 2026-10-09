"""Shared Playwright settings for the bot and standalone workers."""

import logging
import os
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
