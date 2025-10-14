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
    urls = [
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media1.jpg",
        "https://images.example.com/media2.jpg",
    ]

    result = store_profile_media(db_path, 123, "example", profile_url, urls)
    assert result.added_items == 2
    assert result.link_added is True

    second = store_profile_media(
        db_path,
        123,
        "example",
        profile_url,
        [urls[0], "https://images.example.com/media3.jpg"],
    )
    assert second.added_items == 1
    assert second.link_added is True

    conn = sqlite3.connect(db_path)
    try:
        count_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        assert count_items == 3

        count_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        assert count_links == 1
    finally:
        conn.close()


def test_store_profile_media_stores_gps(tmp_path: Path):
    db_path = tmp_path / "gps.db"
    profile_url = "https://vsco.co/example/gallery"
    url_with_gps = "https://images.example.com/geo1.jpg"
    url_without_gps = "https://images.example.com/geo2.jpg"

    def fake_fetcher(url: str):
        if url == url_with_gps:
            return 55.751244, 37.618423
        return None, None

    result = store_profile_media(
        db_path,
        321,
        "example",
        profile_url,
        [url_with_gps, url_without_gps],
        gps_fetcher=fake_fetcher,
    )
    assert result.added_items == 2

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT image_url, latitude, longitude FROM items"
        ).fetchall()
        mapped = {url: (lat, lon) for url, lat, lon in rows}
        assert mapped[url_with_gps] == (55.751244, 37.618423)
        assert mapped[url_without_gps] == (None, None)
    finally:
        conn.close()


def test_store_profile_media_updates_missing_gps(tmp_path: Path):
    db_path = tmp_path / "gps_update.db"
    profile_url = "https://vsco.co/example/gallery"
    url = "https://images.example.com/geo1.jpg"

    store_profile_media(
        db_path,
        111,
        "example",
        profile_url,
        [url],
        gps_fetcher=lambda _url: (None, None),
    )

    store_profile_media(
        db_path,
        111,
        "example",
        profile_url,
        [url],
        gps_fetcher=lambda _url: (12.34, 56.78),
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT latitude, longitude FROM items WHERE image_url=?", (url,)
        ).fetchone()
        assert row == (12.34, 56.78)
    finally:
        conn.close()
