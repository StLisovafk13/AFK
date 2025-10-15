import importlib
import sys

import asyncio
import json
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

    sys.modules.pop("vsco_bot", None)
    sys.modules.pop("zip_profile", None)
    vsco_bot = importlib.import_module("vsco_bot")
    vsco_bot.init_db()
    async def _empty_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        return []

    async def _empty_scan(session, profile_url, **kwargs):  # type: ignore[override]
        return []

    monkeypatch.setattr(vsco_bot, "playwright_scan_profile", _empty_playwright, raising=False)
    monkeypatch.setattr(vsco_bot, "scan_profile_media", _empty_scan, raising=False)
    yield vsco_bot
    asyncio.run(vsco_bot.bot.session.close())


def test_text_link_message_inserts_profile(vsco_module):
    text = "anast2010, hi"
    entity = MessageEntity(type="text_link", offset=0, length=9, url="https://vsco.co/anast2010")
    pairs = vsco_module.parse_vsco_pairs_from_message(text, [entity])
    assert pairs == [{"url": "https://vsco.co/anast2010", "comment": "hi"}]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))
    items_added, comments_added, new_links = vsco_module.upsert_items_with_comments(
        chat_id=100,
        pairs=normalized,
        source="text",
        source_file="message",
        added_by="",
    )

    assert items_added == 1
    assert comments_added == 1
    assert new_links == ["https://vsco.co/anast2010"]

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


def test_profile_link_scans_direct_media(monkeypatch, vsco_module):
    assets = [
        "https://cdn.example.com/photo1.jpg?w=800",
        "https://cdn.example.com/photo2.jpg",
        "https://cdn.example.com/video1.mp4",
    ]

    async def fake_scan(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        return assets

    monkeypatch.setattr(vsco_module, "playwright_scan_profile", fake_scan, raising=False)

    pairs = [{"url": "https://vsco.co/sampleuser", "comment": "wow"}]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))

    assert len(normalized) == len(assets)
    assert {entry["image_url"] for entry in normalized} == set(assets)

    items_added, comments_added, new_links = vsco_module.upsert_items_with_comments(
        chat_id=200,
        pairs=normalized,
        source="text",
        source_file="message",
        added_by="tester",
    )

    assert items_added == len(assets)
    assert comments_added == len(assets)
    assert new_links == ["https://vsco.co/sampleuser"]

    gallery_users = vsco_module.fetch_gallery_users("chat", 200)
    assert gallery_users and gallery_users[0]["username"] == "sampleuser"
    assert set(gallery_users[0]["images"]) == set(assets)


def test_on_text_creates_profile_urls_file(monkeypatch, vsco_module):
    assets = [
        "https://cdn.example.com/photo1.jpg",
        "https://cdn.example.com/photo1.jpg",
        "https://cdn.example.com/photo2.jpg",
    ]

    async def fake_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        return assets

    monkeypatch.setattr(vsco_module, "playwright_scan_profile", fake_playwright, raising=False)

    msg = DummyMessage(chat_id=321, user_id=999)
    msg.text = "https://vsco.co/sampleuser"
    msg.entities = None
    msg.from_user.username = "tester"

    asyncio.run(vsco_module.on_text(msg))

    ses = vsco_module.get_session(321)
    expected_dir = ses.dir / "profiles" / "sampleuser"
    urls_file = expected_dir / "urls_extracted.txt"
    assert urls_file.exists()
    lines = urls_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["https://cdn.example.com/photo1.jpg", "https://cdn.example.com/photo2.jpg"]

    conn = vsco_module.db_connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM items WHERE chat_id=?", (321,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 2

    assert msg.answer_calls, "bot should respond"
    text, _ = msg.answer_calls[-1]
    assert "profiles/sampleuser/urls_extracted.txt" in text


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


def test_text_link_comment_after_space(vsco_module):
    text = "anast2010 привет"
    entity = MessageEntity(type="text_link", offset=0, length=9, url="https://vsco.co/anast2010")

    pairs = vsco_module.parse_vsco_pairs_from_message(text, [entity])

    assert pairs == [{"url": "https://vsco.co/anast2010", "comment": "привет"}]


def test_media_link_scans_media_assets(monkeypatch, vsco_module):
    html = """
    <html>
      <body>
        <img src="https://cdn.example.com/media1.jpg?w=640" />
        <picture>
          <source srcset="https://cdn.example.com/media2_small.jpg 320w, https://cdn.example.com/media2_large.jpg 1280w" />
        </picture>
        <video>
          <source src="https://cdn.example.com/video.mp4" />
        </video>
        <img src="https://static.vsco.co/assets/images/VSCO-logo-white.png" />
      </body>
    </html>
    """

    class DummyResponse:
        def __init__(self, text: str):
            self._text = text
            self.headers = {}
            self.status = 200
            self.url = "https://vsco.co/user/media/abc"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def text(self, errors: str = "ignore") -> str:
            return self._text

    def fake_get(self, url, **kwargs):  # type: ignore[override]
        return DummyResponse(html)

    monkeypatch.setattr(vsco_module.aiohttp.ClientSession, "get", fake_get, raising=False)

    pairs = [{"url": "https://vsco.co/testuser/media/abc", "comment": "wow"}]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))

    image_urls = [entry["image_url"] for entry in normalized]
    assert image_urls == [
        "https://cdn.example.com/media1.jpg?w=2048",
        "https://cdn.example.com/media2_large.jpg",
        "https://cdn.example.com/video.mp4",
    ]
    assert all(entry["username"] == "testuser" for entry in normalized)
    assert all(entry["url"] == "https://vsco.co/testuser" for entry in normalized)
    assert all(entry["comment"] == "wow" for entry in normalized)


def test_on_text_replies_with_unique_links(vsco_module):
    msg = DummyMessage(chat_id=123, user_id=555)
    msg.text = "https://vsco.co/uniqueuser"
    msg.entities = None
    msg.from_user.username = "tester"

    asyncio.run(vsco_module.on_text(msg))

    assert msg.answer_calls, "bot should respond with a message"
    first_text, _ = msg.answer_calls[-1]
    assert "Новые ссылки" in first_text
    assert '<a href="https://vsco.co/uniqueuser">https://vsco.co/uniqueuser</a>' in first_text

    msg2 = DummyMessage(chat_id=123, user_id=555)
    msg2.text = "https://vsco.co/uniqueuser"
    msg2.entities = None
    msg2.from_user.username = "tester"

    asyncio.run(vsco_module.on_text(msg2))

    assert msg2.answer_calls, "bot should respond again"
    second_text, _ = msg2.answer_calls[-1]
    assert "Новые ссылки" not in second_text


def test_insert_full_rows_from_html_returns_new_links(vsco_module):
    rows = [
        {
            "username": "htmluser",
            "profile_url": "https://vsco.co/htmluser",
            "image_url": "",
        }
    ]

    added, new_links = vsco_module.insert_full_rows_from_html(
        chat_id=200,
        rows=rows,
        source_file="sample.html",
        added_by="tester",
    )

    assert added == 1
    assert new_links == ["https://vsco.co/htmluser"]

    added_again, new_links_again = vsco_module.insert_full_rows_from_html(
        chat_id=200,
        rows=rows,
        source_file="sample.html",
        added_by="tester",
    )

    assert added_again == 0
    assert new_links_again == []


def test_ingest_download_results_adds_media(vsco_module, tmp_path):
    user_dir = tmp_path / "sampleuser"
    user_dir.mkdir()
    manifest = {
        "username": "sampleuser",
        "profile_url": "https://vsco.co/sampleuser/gallery",
        "items": [
            {"url": "https://cdn.example.com/media1.jpg", "ok": True},
            {"url": "https://cdn.example.com/media2.jpg", "ok": False},
            {"image_url": "https://cdn.example.com/static/VSCO-logo-white.png", "ok": True},
            {
                "url": "https://cdn.example.com/media3_small.jpg",
                "responsive_url": "https://cdn.example.com/media3_large.jpg",
                "ok": True,
            },
        ],
    }
    (user_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    job = vsco_module.DLJob(
        id=1,
        chat_id=555,
        target="sampleuser",
        extra_flags=[],
        out_base=tmp_path,
        requested_by="@requester",
    )

    added, link_added = vsco_module.ingest_download_results(job, user_dir)
    assert added == 2
    assert link_added is True

    conn = vsco_module.db_connect()
    try:
        rows = conn.execute(
            "SELECT username, profile_url, image_url, source, source_file, added_by FROM items ORDER BY image_url"
        ).fetchall()
        link_row = conn.execute("SELECT username, url FROM links").fetchone()
    finally:
        conn.close()

    assert rows == [
        ("sampleuser", "https://vsco.co/sampleuser", "https://cdn.example.com/media1.jpg", "download", "sampleuser", "@requester"),
        (
            "sampleuser",
            "https://vsco.co/sampleuser",
            "https://cdn.example.com/media3_large.jpg",
            "download",
            "sampleuser",
            "@requester",
        ),
    ]
    assert link_row == ("sampleuser", "https://vsco.co/sampleuser")

    vsco_module.rebuild_urls_extracted(user_dir)
    rebuilt_urls = (user_dir / "urls_extracted.txt").read_text(encoding="utf-8").splitlines()
    assert rebuilt_urls == [
        "https://cdn.example.com/media1.jpg",
        "https://cdn.example.com/media3_large.jpg",
    ]

    added_again, link_added_again = vsco_module.ingest_download_results(job, user_dir)
    assert added_again == 0
    assert link_added_again is False


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
    assert "новых профилей" in message
    assert str(vsco_module.DAILY_PROFILE_LIMIT) in message


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


def test_polling_retries_after_network_error(monkeypatch, vsco_module):
    attempts: list[str] = []

    async def fake_start_polling(bot, *args, **kwargs):
        attempts.append("call")
        if len(attempts) == 1:
            raise vsco_module.TelegramNetworkError(method=None, message="timeout")

    monkeypatch.setattr(vsco_module.dp, "start_polling", fake_start_polling)

    sleeps: list[float] = []

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr(vsco_module.asyncio, "sleep", fake_sleep)

    asyncio.run(vsco_module._start_polling_with_retries(max_attempts=2))

    assert len(attempts) == 2
    assert sleeps == [1]
