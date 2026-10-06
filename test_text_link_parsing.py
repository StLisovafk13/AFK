import importlib
import sys

import asyncio
import json
from pathlib import Path

import pytest

aiogram = pytest.importorskip("aiogram")
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
    monkeypatch.setenv("BOT_PROFILE_NORMALIZE_CONCURRENCY", "2")
    monkeypatch.setenv("BOT_INLINE_PLAYWRIGHT", "0")

    sys.modules.pop("vsco_bot", None)
    sys.modules.pop("zip_profile", None)
    sys.modules.pop("profile_link_scanner", None)

    import profile_link_scanner as pls

    monkeypatch.setattr(pls, "extract_exif_from_url", lambda url: {"size_bytes": 1}, raising=False)

    vsco_bot = importlib.import_module("vsco_bot")
    vsco_bot.init_db()
    async def _empty_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        return []

    async def _empty_scan(session, profile_url, **kwargs):  # type: ignore[override]
        return [], ""

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

    call_order: list[tuple[str, str]] = []

    async def fake_scan(session, profile_url, *, max_width=2048, logger=None, **kwargs):  # type: ignore[override]
        assert profile_url.endswith("sampleuser/gallery")
        call_order.append(("http", profile_url))
        return assets, "<html></html>"

    async def fake_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        call_order.append(("playwright", profile_url))
        raise AssertionError("playwright should not be invoked when HTTP succeeded")

    monkeypatch.setattr(vsco_module, "scan_profile_media", fake_scan, raising=False)
    monkeypatch.setattr(vsco_module, "playwright_scan_profile", fake_playwright, raising=False)

    pairs = [{"url": "https://vsco.co/sampleuser", "comment": "wow"}]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))

    assert len(normalized) == len(assets)
    assert {entry["image_url"] for entry in normalized} == set(assets)
    assert call_order == [("http", "https://vsco.co/sampleuser/gallery")]

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

    gallery_users = list(vsco_module.fetch_gallery_users("chat", 200))
    assert gallery_users and gallery_users[0]["username"] == "sampleuser"
    assert set(gallery_users[0]["images"]) == set(assets)


def test_user_unique_links_progress(vsco_module):
    user_id = 777

    count, invite = vsco_module.record_user_unique_links(user_id, [])
    assert count == 0
    assert invite is False

    initial_links = [f"https://vsco.co/testuser{i}" for i in range(5)]
    count, invite = vsco_module.record_user_unique_links(user_id, initial_links + initial_links[:2])
    assert count == 5
    assert invite is False

    more_links = [f"https://vsco.co/testuser{i}" for i in range(5, 10)]
    count, invite = vsco_module.record_user_unique_links(user_id, more_links)
    assert count == 10
    assert invite is True

    count, invite = vsco_module.record_user_unique_links(user_id, ["https://vsco.co/testuser9"])
    assert count == 10
    assert invite is False


def test_profile_link_scans_http_failure_returns_placeholder(monkeypatch, vsco_module):
    assets = [
        "https://cdn.example.com/photo1.jpg?w=800",
        "https://cdn.example.com/photo2.jpg",
    ]

    call_order: list[tuple[str, str]] = []

    async def fake_scan(session, profile_url, *, max_width=2048, logger=None, **kwargs):  # type: ignore[override]
        assert profile_url.endswith("sampleuser/gallery")
        call_order.append(("http", profile_url))
        return [], ""

    async def fake_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        raise AssertionError("playwright should not be invoked when inline disabled")

    monkeypatch.setattr(vsco_module, "scan_profile_media", fake_scan, raising=False)
    monkeypatch.setattr(vsco_module, "playwright_scan_profile", fake_playwright, raising=False)

    pairs = [{"url": "https://vsco.co/sampleuser", "comment": "wow"}]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))

    assert len(normalized) == 1
    assert normalized[0]["username"] == "sampleuser"
    assert normalized[0]["url"] == "https://vsco.co/sampleuser"
    assert normalized[0]["image_url"] == ""
    assert normalized[0]["comment"] == "wow"
    assert call_order == [
        ("http", "https://vsco.co/sampleuser/gallery"),
    ]


def test_normalize_pairs_preserves_order_and_dedupes(monkeypatch, vsco_module):
    assets_map = {
        "https://vsco.co/user1/gallery": ["https://cdn.example.com/user1_1.jpg"],
        "https://vsco.co/user2/gallery": ["https://cdn.example.com/user2_1.jpg"],
    }

    call_log: list[str] = []

    async def fake_scan(session, profile_url, *, max_width=2048, logger=None, **kwargs):  # type: ignore[override]
        call_log.append(profile_url)
        await asyncio.sleep(0)
        return assets_map.get(profile_url, []), "<html></html>"

    async def fake_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        raise AssertionError("playwright should not be invoked when HTTP succeeded")

    monkeypatch.setattr(vsco_module, "scan_profile_media", fake_scan, raising=False)
    monkeypatch.setattr(vsco_module, "playwright_scan_profile", fake_playwright, raising=False)

    pairs = [
        {"url": " https://vsco.co/user1 ", "comment": "first"},
        {"url": "https://vsco.co/user2", "comment": "second"},
        {"url": "https://vsco.co/user1", "comment": "third"},
    ]

    normalized = asyncio.run(vsco_module.normalize_vsco_pairs(pairs))

    assert [entry["comment"] for entry in normalized] == ["first", "second", "third"]
    assert [entry["url"] for entry in normalized] == [
        "https://vsco.co/user1",
        "https://vsco.co/user2",
        "https://vsco.co/user1",
    ]
    assert [entry["image_url"] for entry in normalized] == [
        "https://cdn.example.com/user1_1.jpg",
        "https://cdn.example.com/user2_1.jpg",
        "https://cdn.example.com/user1_1.jpg",
    ]

    assert call_log.count("https://vsco.co/user1/gallery") == 1
    assert call_log.count("https://vsco.co/user2/gallery") == 1


def test_on_text_creates_profile_urls_file(monkeypatch, vsco_module):
    assets = [
        "https://cdn.example.com/photo1.jpg",
        "https://cdn.example.com/photo1.jpg",
        "https://cdn.example.com/photo2.jpg",
    ]

    async def fake_scan(session, profile_url, **kwargs):  # type: ignore[override]
        return assets, ""

    async def fake_playwright(profile_url, *, max_width=2048, session=None, logger=None, delay=0.4, target_count=0):  # type: ignore[override]
        raise AssertionError("playwright should not be invoked when HTTP succeeds")

    monkeypatch.setattr(vsco_module, "scan_profile_media", fake_scan, raising=False)
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


@pytest.mark.parametrize(
    "import_kind,image_url",
    [
        ("text", "https://cdn.example.com/shared.jpg"),
        ("text", ""),
        ("html", "https://cdn.example.com/shared.jpg"),
        ("html", ""),
        ("download", "https://cdn.example.com/shared.jpg"),
    ],
)
def test_imports_keep_identical_items_in_separate_chats(vsco_module, tmp_path, import_kind, image_url):
    username = "shareduser"
    profile_url = "https://vsco.co/shareduser"
    user_dir = tmp_path / username
    user_dir.mkdir()
    (user_dir / "manifest.json").write_text(json.dumps({
        "username": username,
        "profile_url": profile_url,
        "items": [{"url": image_url, "ok": True}],
    }), encoding="utf-8")

    def ingest(chat_id, comment):
        if import_kind == "text":
            return vsco_module.upsert_items_with_comments(
                chat_id, [{"username": username, "url": profile_url,
                           "image_url": image_url, "comment": comment}],
                "text", "message", "tester",
            )
        if import_kind == "html":
            return vsco_module.insert_full_rows_from_html(
                chat_id, [{"username": username, "profile_url": profile_url,
                           "image_url": image_url}], "sample.html", "tester",
            )
        job = vsco_module.DLJob(
            id=chat_id, chat_id=chat_id, target=username, extra_flags=[],
            out_base=tmp_path, requested_by="tester",
        )
        return vsco_module.ingest_download_results(job, user_dir)

    for chat_id in (101, 202):
        assert ingest(chat_id, f"private-{chat_id}")[0] == 1
        assert ingest(chat_id, f"private-{chat_id}")[0] == 0
        if import_kind == "text":
            # The same comment may legitimately exist once in each chat.
            assert ingest(chat_id, "shared comment")[:2] == (0, 1)
            assert ingest(chat_id, "shared comment")[:2] == (0, 0)

    conn = vsco_module.db_connect()
    try:
        assert conn.execute(
            "SELECT chat_id, username, profile_url, image_url FROM items ORDER BY chat_id"
        ).fetchall() == [(chat, username, profile_url, image_url) for chat in (101, 202)]
        assert conn.execute(
            "SELECT COUNT(*) FROM comments AS c JOIN items AS i ON i.id=c.item_id"
            " WHERE c.chat_id != i.chat_id"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    for chat_id in (101, 202):
        gallery = list(vsco_module.fetch_gallery_users("chat", chat_id))
        assert len(gallery) == 1
        assert gallery[0]["images"] == ([image_url] if image_url else [])
        expected = [f"private-{chat_id}", "shared comment"] if import_kind == "text" else []
        assert gallery[0]["comments"] == expected
        assert vsco_module.fetch_items_for_map("chat", chat_id)[0]["comments"] == expected

    combined = list(vsco_module.fetch_gallery_users("all", 101))
    assert len(combined) == 1
    expected_all = {"private-101", "private-202", "shared comment"} if import_kind == "text" else set()
    assert set(combined[0]["comments"]) == expected_all


@pytest.mark.parametrize("scope", ["chat", "all"])
def test_exports_hide_legacy_comments_linked_to_another_chat(vsco_module, scope):
    vsco_module.upsert_items_with_comments(
        101, [{"username": "shareduser", "url": "https://vsco.co/shareduser",
               "image_url": "https://cdn.example.com/shared.jpg", "comment": "own comment"}],
        "text", "message", "tester",
    )
    conn = vsco_module.db_connect()
    try:
        item_id = conn.execute("SELECT id FROM items WHERE chat_id=101").fetchone()[0]
        # Reproduce a persisted association created by the old unscoped lookup.
        conn.execute(
            "INSERT INTO comments(item_id,chat_id,comment,created_at) VALUES(?,?,?,?)",
            (item_id, 202, "other chat's comment", vsco_module.utc_now_iso()),
        )
        conn.commit()
        before = conn.execute("SELECT * FROM comments ORDER BY id").fetchall()

        gallery = list(vsco_module.fetch_gallery_users(scope, 101))
        assert gallery[0]["comments"] == ["own comment"]
        assert vsco_module.fetch_items_for_map(scope, 101)[0]["comments"] == ["own comment"]
        profile = vsco_module.load_profile(
            conn, "shareduser", chat_id=101 if scope == "chat" else None,
        )
        assert profile.photos[0].comments == ["own comment"]
        archive = vsco_module.fetch_profile_archive_info("shareduser")
        assert archive.comments == ["own comment"]
        assert conn.execute("SELECT * FROM comments ORDER BY id").fetchall() == before
    finally:
        conn.close()


@pytest.mark.parametrize("comment", [
    '</script><script>window.__xss=1</script>',
    '</ScRiPt ><img src=x onerror="window.__xss=1">',
    '<!--<script>Обычный текст & "кавычки" 😀',
])
def test_rich_gallery_comment_cannot_escape_inline_script(vsco_module, comment):
    from bs4 import BeautifulSoup
    from test_html_export_security import read_inline_json

    pairs = vsco_module.parse_vsco_pairs_from_cell("https://vsco.co/example, " + comment)
    assert pairs[0]["comment"] == comment
    vsco_module.upsert_items_with_comments(
        101, [{"username": "example", "url": pairs[0]["url"], "comment": comment}],
        "text", "message", "tester",
    )
    users = list(vsco_module.fetch_gallery_users("chat", 101))
    document = vsco_module.build_rich_gallery(users)
    soup = BeautifulSoup(document, "html.parser")
    assert len(soup.find_all("script")) == 1
    assert not soup.select("[onerror]")
    assert read_inline_json(document, "DATA")[0]["comments"] == [comment]


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


@pytest.fixture()
def membership_module(vsco_module, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(vsco_module, "REQUIRED_CHATS", [
        vsco_module.RequiredChat(-101, "channel", ""),
        vsco_module.RequiredChat(-202, "group", ""),
    ])
    monkeypatch.setattr(vsco_module, "_ACCESS_CACHE", {})
    monkeypatch.setattr(vsco_module, "_ACCESS_PENDING", {})
    request = AsyncMock()
    monkeypatch.setattr(vsco_module.bot, "get_chat_member", request)
    return vsco_module, request


@pytest.mark.parametrize("error_name", [
    "TelegramForbiddenError", "TelegramRetryAfter", "TelegramBadRequest", "TelegramNetworkError",
])
def test_membership_errors_deny_access_without_caching(membership_module, error_name):
    from aiogram import exceptions
    from types import SimpleNamespace

    module, request = membership_module
    kwargs = {"retry_after": 5} if error_name == "TelegramRetryAfter" else {}
    request.side_effect = getattr(exceptions, error_name)(method=None, message="test failure", **kwargs)

    async def check():
        assert await module._is_user_allowed(777) is False
        assert 777 not in module._ACCESS_CACHE
        assert not module._ACCESS_PENDING
        # A repeat must actually check membership, never reuse a successful result.
        previous_calls = request.await_count
        request.side_effect = None
        request.return_value = SimpleNamespace(status="left")
        assert await module._is_user_allowed(777) is False
        assert request.await_count > previous_calls
        assert module._ACCESS_CACHE[777][1] is False

    asyncio.run(check())


def test_membership_waits_for_all_chats_and_shares_pending_check(membership_module):
    from types import SimpleNamespace

    module, request = membership_module

    async def check():
        started = asyncio.Event()
        release = asyncio.Event()

        async def member(chat_id, user_id):
            if chat_id == -202:
                started.set()
                await release.wait()
            return SimpleNamespace(status="member")

        request.side_effect = member
        first = asyncio.create_task(module._is_user_allowed(777))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(module._is_user_allowed(777))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert 777 not in module._ACCESS_CACHE
        release.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert request.await_count == 2
        assert await module._is_user_allowed(777) is True
        assert request.await_count == 2
        assert not module._ACCESS_PENDING

    asyncio.run(check())


def test_membership_one_denied_chat_prevents_access_and_cache_expires(membership_module):
    from types import SimpleNamespace

    module, request = membership_module
    request.side_effect = [SimpleNamespace(status="member"), SimpleNamespace(status="left")]

    async def check():
        assert await module._is_user_allowed(777) is False
        assert module._ACCESS_CACHE[777][1] is False
        calls = request.await_count
        assert await module._is_user_allowed(777) is False
        assert request.await_count == calls
        module._ACCESS_CACHE[777] = (module.time.time() - module.ACCESS_CACHE_TTL - 1, False)
        request.side_effect = None
        request.return_value = SimpleNamespace(status="member")
        assert await module._is_user_allowed(777) is True
        assert request.await_count == calls + 2

    asyncio.run(check())


def test_membership_cancellation_leaves_no_cached_permission(membership_module):
    from types import SimpleNamespace

    module, request = membership_module

    async def check():
        started = asyncio.Event()
        stopped = set()

        async def blocked(chat_id, user_id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(chat_id)

        request.side_effect = blocked
        task = asyncio.create_task(module._is_user_allowed(777))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == {-101, -202}
        assert 777 not in module._ACCESS_CACHE
        assert not module._ACCESS_PENDING
        request.side_effect = None
        request.return_value = SimpleNamespace(status="left")
        assert await module._is_user_allowed(777) is False

    asyncio.run(check())


def test_membership_unexpected_exception_does_not_cache_permission(membership_module):
    module, request = membership_module
    request.side_effect = RuntimeError("unexpected failure")

    async def check():
        with pytest.raises(RuntimeError, match="unexpected failure"):
            await module._is_user_allowed(777)
        assert 777 not in module._ACCESS_CACHE
        assert not module._ACCESS_PENDING

    asyncio.run(check())


@pytest.mark.parametrize("flags", [
    ["--max", "100", "--timeout", "60", "--concurrency", "2"],
    ["--max=100", "--zip-name", "album a.zip"],
    ["--skip-video-thumbs", "--no-zip"],
])
def test_download_command_preserves_flag_values(vsco_module, monkeypatch, flags):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from vsco_downloader import parse_args

    monkeypatch.setattr(vsco_module, "has_daily_data_access", lambda *args: (True, ""))
    message = SimpleNamespace(chat=SimpleNamespace(type="private", id=101), from_user=None, answer=AsyncMock())
    assert asyncio.run(vsco_module._enqueue_download_request(message, "alice", flags, 777))
    job = vsco_module._DL_QUEUE.get_nowait()
    assert job.extra_flags == flags
    monkeypatch.setattr(sys, "argv", ["vsco_downloader.py", "--username", "alice", *job.extra_flags])
    args = parse_args()
    assert args.username == "alice"
    if any(flag.startswith("--max") for flag in flags):
        assert args.max == 100


@pytest.mark.parametrize("flags", [
    ["--max"], ["--max", "-1"], ["--timeout", "nan"], ["--concurrency", "0"],
    ["--out", "/tmp/other"], ["--username=other"], ["--zip-name", "../other.zip"],
    ["--unknown"], ["--timeout", "0"],
])
def test_invalid_download_flags_are_rejected_before_queueing(vsco_module, flags):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    message = SimpleNamespace(chat=SimpleNamespace(type="private", id=101), from_user=None, answer=AsyncMock())
    assert asyncio.run(vsco_module._enqueue_download_request(message, "alice", flags, 777)) is False
    assert vsco_module._DL_QUEUE is None or vsco_module._DL_QUEUE.empty()
    assert "параметры" in message.answer.call_args.args[0]


@pytest.mark.parametrize("explicit_id,expected_id", [(42, 42), (None, 999)])
def test_export_menu_checks_effective_user(vsco_module, explicit_id, expected_id):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from vsco_export_impl import ExportManager

    deps = SimpleNamespace(ensure_user_has_access=AsyncMock(return_value=True),
                           has_daily_data_access=Mock(return_value=(False, "denied")))
    message = SimpleNamespace(chat=SimpleNamespace(type="private", id=42),
                              from_user=SimpleNamespace(id=999), answer=AsyncMock())
    asyncio.run(ExportManager(deps).open_menu(message, user_id=explicit_id))
    deps.has_daily_data_access.assert_called_once_with(42, expected_id)


def test_download_timeout_stops_process_and_continues_queue(vsco_module, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    module = vsco_module
    stopped = []
    created = []
    monkeypatch.setattr(module, "DOWNLOAD_JOB_TIMEOUT", 0.02)
    monkeypatch.setattr(module.bot, "send_message", AsyncMock(return_value=SimpleNamespace(message_id=1)))
    monkeypatch.setattr(module.bot, "edit_message_text", AsyncMock())

    async def create_process(*args, **kwargs):
        reader = asyncio.StreamReader()
        if created:
            reader.feed_eof()
        proc = SimpleNamespace(stdout=reader, returncode=None, wait=AsyncMock(return_value=5))
        created.append(proc)
        return proc

    async def stop(proc):
        stopped.append(proc)
        proc.returncode = -9
        proc.stdout.feed_eof()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(module, "_terminate_download_process", stop)

    async def check():
        queue = asyncio.Queue()
        monkeypatch.setattr(module, "_DL_QUEUE", queue)
        for job_id in (1, 2):
            await queue.put(module.DLJob(job_id, 101, "example", [], tmp_path))
        task = asyncio.create_task(module._dl_worker())
        try:
            await asyncio.wait_for(queue.join(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(check())
    assert len(created) == 2 and stopped == [created[0]]
    assert any("истёк лимит" in call.kwargs["text"] for call in module.bot.edit_message_text.call_args_list)


def test_summary_channel_uses_parts_when_combined_archive_is_too_large(vsco_module, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    module = vsco_module
    parts = [tmp_path / "part1.zip", tmp_path / "part2.zip"]
    for path in parts:
        path.write_bytes(b"archive")
    combined = tmp_path / "combined.zip"
    with combined.open("wb") as stream:
        stream.truncate(51 * 1024 * 1024)
    monkeypatch.setattr(module, "ARCHIVE_ADMIN_CHANNEL_ID", None)
    monkeypatch.setattr(module, "ARCHIVE_SUMMARY_CHANNEL_ID", -100)
    monkeypatch.setattr(module, "_create_single_archive", lambda *args: combined)
    async def run_in_test(func, *args):
        return func(*args)
    monkeypatch.setattr(module.asyncio, "to_thread", run_in_test)
    monkeypatch.setattr(module.bot, "send_message", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(module.bot, "send_document", send)
    job = module.DLJob(1, 101, "example", [], tmp_path)
    asyncio.run(module._send_archives_to_channels(job, parts, tmp_path, 2))
    assert [call.args[1].filename for call in send.call_args_list] == [p.name for p in parts]
    assert not combined.exists()


def test_download_process_group_is_terminated(vsco_module):
    import os
    if os.name != "posix":
        pytest.skip("POSIX process-group cleanup")

    async def check():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True,
        )
        try:
            await vsco_module._terminate_download_process(proc)
            assert proc.returncode is not None and proc.returncode < 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    asyncio.run(check())
