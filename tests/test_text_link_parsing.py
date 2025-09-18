import importlib
import sys

import asyncio
from pathlib import Path

import pytest
from aiogram.types import MessageEntity


@pytest.fixture()
def vsco_module(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TESTTOKEN")
    monkeypatch.setenv("BOT_DB_PATH", str(tmp_path / "vsco_links.db"))
    monkeypatch.setenv("BOT_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("BOT_LOGDIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOT_ADMIN_IDS", "42, 99")

    sys.modules.pop("vsco_bot18", None)
    sys.modules.pop("zip_profile", None)
    vsco_bot18 = importlib.import_module("vsco_bot18")
    vsco_bot18.init_db()
    yield vsco_bot18
    asyncio.run(vsco_bot18.bot.session.close())


def test_text_link_message_inserts_profile(vsco_module):
    text = "anast2010, hi"
    entity = MessageEntity(type="text_link", offset=0, length=9, url="https://vsco.co/anast2010")
    pairs = vsco_module.parse_vsco_pairs_from_message(text, [entity])
    assert pairs == [{"url": "https://vsco.co/anast2010", "comment": "hi"}]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))
    items_added, comments_added = vsco_module.upsert_items_with_comments(
        chat_id=100,
        pairs=normalized,
        source="text",
        source_file="message",
        added_by="",
    )

    assert items_added == 1
    assert comments_added == 1

    conn = vsco_module.db_connect()
    try:
        link_row = conn.execute("SELECT username, url FROM links").fetchone()
        item_row = conn.execute("SELECT username, profile_url FROM items").fetchone()
        comment_row = conn.execute("SELECT comment FROM comments").fetchone()
    finally:
        conn.close()

    assert link_row == ("anast2010", "https://vsco.co/anast2010")
    assert item_row == ("anast2010", "https://vsco.co/anast2010")
    assert comment_row == ("hi",)


def test_text_link_with_emoji_preserves_comment(vsco_module):
    text = "🔥anast2010, hi"
    entity = MessageEntity(
        type="text_link",
        offset=0,
        length=11,
        url="https://vsco.co/anast2010",
    )

    pairs = vsco_module.parse_vsco_pairs_from_message(text, [entity])
    assert pairs == [{"url": "https://vsco.co/anast2010", "comment": "hi"}]


class DummyChat:
    def __init__(self, chat_id: int, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type


class DummyUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class DummyMessage:
    def __init__(self, chat_id: int, user_id: int) -> None:
        self.chat = DummyChat(chat_id)
        self.from_user = DummyUser(user_id)
        self.answer_calls: list[tuple[str, dict]] = []
        self.edit_text_calls: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answer_calls.append((text, kwargs))
        return None

    async def edit_text(self, text: str, **kwargs):
        self.edit_text_calls.append((text, kwargs))
        return None


class DummyCallback:
    def __init__(self, data: str, message: DummyMessage, user_id: int) -> None:
        self.data = data
        self.message = message
        self.from_user = DummyUser(user_id)
        self.answer_calls: list[dict] = []

    async def answer(self, text: str = "", *, show_alert: bool = False):
        self.answer_calls.append({"text": text, "show_alert": show_alert})
        return None


def test_is_admin_id_helper(vsco_module):
    assert vsco_module.is_admin_id(42) is True
    assert vsco_module.is_admin_id(12345) is False
    assert vsco_module.is_admin_id(None) is False


def test_has_daily_data_access_admin_override(vsco_module):
    allowed, message = vsco_module.has_daily_data_access(chat_id=100, user_id=42)
    assert allowed is True
    assert "Администратор" in message
    assert "с координатами" in message


def test_admin_command_displays_menu(vsco_module):
    msg = DummyMessage(chat_id=500, user_id=42)
    asyncio.run(vsco_module.cmd_admin(msg))

    assert msg.answer_calls, "admin command should respond"
    text, kwargs = msg.answer_calls[-1]
    assert "Панель администратора" in text
    keyboard = kwargs.get("reply_markup")
    assert keyboard is not None
    datas = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert {"admin:queue", "admin:limits", "admin:stats"}.issubset(set(datas))


def test_admin_queue_callback_reports_jobs(vsco_module, tmp_path):
    admin_id = 42
    prev_queue = getattr(vsco_module, "_DL_QUEUE", None)
    prev_current = getattr(vsco_module, "_CURRENT_JOB", None)
    out_base = tmp_path / "out"
    out_base.mkdir()
    try:
        q = asyncio.Queue()
        vsco_module._DL_QUEUE = q
        current_job = vsco_module.DLJob(
            id=1,
            chat_id=777,
            target="alpha",
            extra_flags=["--max", "10"],
            out_base=out_base,
        )
        vsco_module._CURRENT_JOB = {"job": current_job}
        pending_job = vsco_module.DLJob(
            id=2,
            chat_id=888,
            target="beta",
            extra_flags=[],
            out_base=out_base,
        )
        q.put_nowait(pending_job)

        message = DummyMessage(chat_id=500, user_id=admin_id)
        callback = DummyCallback("admin:queue", message, admin_id)
        asyncio.run(vsco_module.on_admin_click(callback))

        assert message.answer_calls, "queue handler should send a report"
        text, _ = message.answer_calls[-1]
        assert "Очередь" in text
        assert "#1" in text and "#2" in text
        assert callback.answer_calls and callback.answer_calls[-1]["text"] == "Готово"
    finally:
        vsco_module._DL_QUEUE = prev_queue
        vsco_module._CURRENT_JOB = prev_current


def test_admin_limits_callback_uses_admin_message(vsco_module):
    admin_id = 42
    message = DummyMessage(chat_id=500, user_id=admin_id)
    callback = DummyCallback("admin:limits", message, admin_id)
    asyncio.run(vsco_module.on_admin_click(callback))

    assert message.answer_calls, "limits handler should respond"
    text, _ = message.answer_calls[-1]
    assert "Администратор" in text
    assert callback.answer_calls and callback.answer_calls[-1]["text"] == "Готово"
