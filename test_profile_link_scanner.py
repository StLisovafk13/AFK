import json
import sqlite3
import asyncio
from pathlib import Path

import pytest


import profile_link_scanner
from profile_link_scanner import (
    resolve_profile_inputs,
    store_profile_media,
    collect_profile_media,
    ProfileMediaCollection,
    PARTIAL_LOAD_WARNING,
)


@pytest.mark.parametrize(
    "username, profile_url, expected",
    [
        ("example", None, ("example", "https://vsco.co/example/gallery")),
        (None, "https://vsco.co/jane/gallery", ("jane", "https://vsco.co/jane/gallery")),
        ("john", "https://vsco.co/john", ("john", "https://vsco.co/john/gallery")),
    ],
)
def test_resolve_profile_inputs(username, profile_url, expected):
    assert resolve_profile_inputs(username, profile_url) == expected


def test_store_profile_media_deduplicates(tmp_path: Path):
    db_path = tmp_path / "vsco.db"
    profile_url = "https://vsco.co/example/gallery"
    stub_meta = lambda url: {"size_bytes": 123, "exiftool": {"Model": "TestCam"}}

    tabs = [
        {"href": "https://vsco.co/example/collection/1", "label": "REPOSTS"},
        {"href": "https://vsco.co/example/gallery", "label": "RECENT"},
    ]

    media_urls = [
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media2.jpg",
        "https://images.example.com/media3.jpg",
    ]
    media_by_tab = {
        "https://vsco.co/example/gallery": [
            "https://images.example.com/media1.jpg",
            "https://images.example.com/media1.jpg",
            "https://images.example.com/media2.jpg",
        ],
        "https://vsco.co/example/collection/1": [
            "https://images.example.com/media3.jpg",
        ],
    }

    result = store_profile_media(
        db_path,
        123,
        "example",
        profile_url,
        media_urls,
        profile_tabs=tabs,
        media_by_tab=media_by_tab,
        meta_fetcher=stub_meta,
        warnings=["partial"],
    )
    assert result.added_items == 3
    assert result.link_added is True
    assert result.profile_tabs and result.profile_tabs[0]["href"].endswith("/collection/1")
    assert result.media_by_tab["https://vsco.co/example/gallery"] == [
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media2.jpg",
    ]
    assert len(result.metadata_targets) == 3
    assert result.warnings == ["partial"]

    second = store_profile_media(
        db_path,
        123,
        "example",
        profile_url,
        [
            "https://images.example.com/media2.jpg",
            "https://images.example.com/media4.jpg",
            "https://images.example.com/media5.jpg",
        ],
        media_by_tab={
            "https://vsco.co/example/gallery": [
                "https://images.example.com/media2.jpg",
                "https://images.example.com/media4.jpg",
            ],
            "https://vsco.co/example/spaces": [
                "https://images.example.com/media5.jpg",
            ],
        },
        meta_fetcher=stub_meta,
        warnings=["partial"],
    )
    assert second.added_items == 2
    assert second.link_added is True
    assert len(second.metadata_targets) == 2
    assert second.warnings == ["partial"]

    conn = sqlite3.connect(db_path)
    try:
        count_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count_items == 5

        count_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        assert count_links == 1

        stored = conn.execute(
            "SELECT profile_url, image_url FROM items ORDER BY image_url"
        ).fetchall()
        assert stored == [
            ("https://vsco.co/example/gallery", "https://images.example.com/media1.jpg"),
            ("https://vsco.co/example/gallery", "https://images.example.com/media2.jpg"),
            ("https://vsco.co/example/collection/1", "https://images.example.com/media3.jpg"),
            ("https://vsco.co/example/gallery", "https://images.example.com/media4.jpg"),
            ("https://vsco.co/example/spaces", "https://images.example.com/media5.jpg"),
        ]

        meta_row = conn.execute(
            "SELECT meta_json FROM items WHERE image_url=?",
            ("https://images.example.com/media1.jpg",),
        ).fetchone()
        assert meta_row is not None and meta_row[0]
        payload = json.loads(meta_row[0])
        assert payload.get("size_bytes") == 123
        assert payload.get("exiftool", {}).get("Model") == "TestCam"

        extra_row = conn.execute(
            "SELECT extra_json FROM links WHERE username=?",
            ("example",),
        ).fetchone()
        assert extra_row is not None and extra_row[0]
        tabs_payload = json.loads(extra_row[0])
        stored_tabs = tabs_payload.get("profile_tabs") or []
        assert stored_tabs and stored_tabs[0]["href"].endswith("/collection/1")

        notified_row = conn.execute(
            "SELECT notified_at FROM links WHERE username=?",
            ("example",),
        ).fetchone()
        assert notified_row is not None
        assert (notified_row[0] or "") == ""
    finally:
        conn.close()


def test_collect_profile_media_partial_warning(monkeypatch):
    class DummySession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def close(self) -> None:
            return None

    async def fake_scan(session, url, **kwargs):
        return ["https://images.example.com/media1.jpg"], "<div>Error Loading Content</div>"

    monkeypatch.setattr(profile_link_scanner, "aiohttp", type("A", (), {"ClientSession": DummySession}))
    monkeypatch.setattr(profile_link_scanner, "scan_profile_media", fake_scan)
    monkeypatch.setattr(profile_link_scanner, "extract_profile_tab_links", lambda *a, **k: [])
    async def failed_browser(*args, **kwargs):
        return [], ""
    monkeypatch.setattr(profile_link_scanner, "_collect_with_playwright", failed_browser)

    collected = asyncio.run(
        collect_profile_media(
            "https://vsco.co/example/gallery",
            include_details=True,
        )
    )

    assert isinstance(collected, ProfileMediaCollection)
    assert collected.media_urls == ["https://images.example.com/media1.jpg"]
    assert PARTIAL_LOAD_WARNING in collected.warnings


@pytest.mark.parametrize("browser_fails", [False, True])
def test_http_partial_profile_continues_in_browser(monkeypatch, browser_fails):
    from unittest.mock import AsyncMock
    first = "https://cdn.example/first.jpg"
    second = "https://cdn.example/second.jpg"
    scan = AsyncMock(return_value=([first], '<button id="loadMore-Button">More</button>'))
    browser = AsyncMock(return_value=([], "") if browser_fails else ([second], "<html>Complete</html>"))
    monkeypatch.setattr(profile_link_scanner, "scan_profile_media", scan)
    monkeypatch.setattr(profile_link_scanner, "_collect_with_playwright", browser)
    result = asyncio.run(collect_profile_media("https://vsco.co/example/gallery", target_count=2, include_details=True))
    assert browser.await_count == 1
    assert result.media_urls == ([first] if browser_fails else [first, second])
    assert bool(result.warnings) is browser_fails


@pytest.mark.parametrize("button", ["", '<button id="loadMore-Button" hidden>More</button>',
                                      '<button id="loadMore-Button" disabled>More</button>'])
def test_http_complete_profile_does_not_launch_browser(monkeypatch, button):
    from unittest.mock import AsyncMock
    scan = AsyncMock(return_value=(["https://cdn.example/photo.jpg"], "<html>Complete</html>" + button))
    browser = AsyncMock(side_effect=AssertionError("unexpected browser"))
    monkeypatch.setattr(profile_link_scanner, "scan_profile_media", scan)
    monkeypatch.setattr(profile_link_scanner, "_collect_with_playwright", browser)
    result = asyncio.run(collect_profile_media("https://vsco.co/example/gallery", include_details=True))
    assert len(result.media_urls) == 1 and not result.warnings
    browser.assert_not_awaited()


def test_each_partial_tab_is_completed_without_losing_tab_mapping(monkeypatch):
    from unittest.mock import AsyncMock
    gallery = "https://vsco.co/example/gallery"
    collection = "https://vsco.co/example/collection/1"
    async def scan(session, url, **kwargs):
        if url == gallery:
            return ["https://cdn.example/gallery.jpg"], f'<a id="profileTab-collection" href="{collection}">Collection</a>'
        return ["https://cdn.example/first.jpg"], '<button id="loadMore-Button">More</button>'
    browser = AsyncMock(return_value=(["https://cdn.example/second.jpg"], "<html>Complete</html>"))
    monkeypatch.setattr(profile_link_scanner, "scan_profile_media", scan)
    monkeypatch.setattr(profile_link_scanner, "_collect_with_playwright", browser)
    result = asyncio.run(collect_profile_media(gallery, include_details=True))
    assert browser.call_args.args[0] == collection
    assert result.media_by_tab[gallery] == ["https://cdn.example/gallery.jpg"]
    assert result.media_by_tab[collection] == ["https://cdn.example/first.jpg", "https://cdn.example/second.jpg"]
    assert len(result.media_urls) == 3
