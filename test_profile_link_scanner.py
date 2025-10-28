import json
import sqlite3
from pathlib import Path

import pytest

from profile_link_scanner import (
    resolve_profile_inputs,
    store_profile_media,
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

    profile_description = "🥀russia. ivanovo."

    result = store_profile_media(
        db_path,
        123,
        "example",
        profile_url,
        media_urls,
        profile_tabs=tabs,
        media_by_tab=media_by_tab,
        meta_fetcher=stub_meta,
        profile_description=profile_description,
    )
    assert result.added_items == 3
    assert result.link_added is True
    assert result.profile_tabs and result.profile_tabs[0]["href"].endswith("/collection/1")
    assert result.media_by_tab["https://vsco.co/example/gallery"] == [
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media2.jpg",
    ]
    assert result.profile_description == profile_description

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
        profile_description="",
    )
    assert second.added_items == 2
    assert second.link_added is True

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
        assert tabs_payload.get("profile_description") == profile_description
    finally:
        conn.close()
