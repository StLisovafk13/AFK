import json
import sqlite3
from pathlib import Path

import pytest

from profile_link_scanner import (
    ProfileDetails,
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
    urls = [
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media2.jpg",
    ]

    stub_meta = lambda url: {"size_bytes": 123, "exiftool": {"Model": "TestCam"}}

    result = store_profile_media(
        db_path,
        123,
        "example",
        profile_url,
        urls,
        meta_fetcher=stub_meta,
    )
    assert result.added_items == 2
    assert result.link_added is True
    assert result.sections.get("gallery") == result.media_urls

    second = store_profile_media(
        db_path,
        123,
        "example",
        profile_url,
        [urls[0], "https://images.example.com/media3.jpg"],
        meta_fetcher=stub_meta,
    )
    assert second.added_items == 1
    assert second.link_added is True

    conn = sqlite3.connect(db_path)
    try:
        count_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count_items == 3

        count_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        assert count_links == 1

        meta_row = conn.execute(
            "SELECT meta_json FROM items WHERE image_url=?",
            ("https://images.example.com/media1.jpg",),
        ).fetchone()
        assert meta_row is not None and meta_row[0]
        payload = json.loads(meta_row[0])
        assert payload.get("size_bytes") == 123
        assert payload.get("exiftool", {}).get("Model") == "TestCam"
    finally:
        conn.close()


def test_store_profile_media_updates_profile_details(tmp_path: Path):
    db_path = tmp_path / "vsco.db"
    profile_url = "https://vsco.co/example/gallery"
    details = ProfileDetails(
        bio="About this profile",
        collection_url="https://vsco.co/example/collection/1",
        journal_url="https://vsco.co/example/journal/p/1",
    )

    store_profile_media(
        db_path,
        321,
        "example",
        profile_url,
        ["https://images.example.com/media1.jpg"],
        profile_details=details,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT profile_bio, collection_url, journal_url FROM links WHERE username=?",
            ("example",),
        ).fetchone()
    finally:
        conn.close()

    assert row == (
        "About this profile",
        "https://vsco.co/example/collection/1",
        "https://vsco.co/example/journal/p/1",
    )
