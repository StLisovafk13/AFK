import importlib
import sys

import asyncio
from pathlib import Path

import pytest
from aiogram.types import MessageEntity


def test_text_link_message_inserts_profile(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TESTTOKEN")
    monkeypatch.setenv("BOT_DB_PATH", str(tmp_path / "vsco_links.db"))
    monkeypatch.setenv("BOT_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("BOT_LOGDIR", str(tmp_path / "logs"))

    sys.modules.pop("vsco_bot18", None)
    vsco_bot18 = importlib.import_module("vsco_bot18")
    vsco_bot18.init_db()

    text = "anast2010, hi"
    entity = MessageEntity(type="text_link", offset=0, length=9, url="https://vsco.co/anast2010")
    pairs = vsco_bot18.parse_vsco_pairs_from_message(text, [entity])
    assert pairs == [{"url": "https://vsco.co/anast2010", "comment": "hi"}]

    normalized = asyncio.run(vsco_bot18.normalize_vsco_pairs(pairs))
    items_added, comments_added = vsco_bot18.upsert_items_with_comments(
        chat_id=100,
        pairs=normalized,
        source="text",
        source_file="message",
        added_by="",
    )

    assert items_added == 1
    assert comments_added == 1

    conn = vsco_bot18.db_connect()
    try:
        link_row = conn.execute("SELECT username, url FROM links").fetchone()
        item_row = conn.execute("SELECT username, profile_url FROM items").fetchone()
        comment_row = conn.execute("SELECT comment FROM comments").fetchone()
    finally:
        conn.close()

    assert link_row == ("anast2010", "https://vsco.co/anast2010")
    assert item_row == ("anast2010", "https://vsco.co/anast2010")
    assert comment_row == ("hi",)

    asyncio.run(vsco_bot18.bot.session.close())
