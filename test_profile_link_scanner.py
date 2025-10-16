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


def test_store_profile_media_persists_exif(tmp_path: Path):
    db_path = tmp_path / "vsco_exif.db"
    profile_url = "https://vsco.co/exif/gallery"
    url = "https://images.example.com/photo.jpg"
    exif_payload = {
        "camera_make": "Canon",
        "camera_model": "EOS",
        "aperture": 2.8,
        "iso": 200,
        "lat": 10.123456,
        "lon": 20.654321,
    }

    store_profile_media(
        db_path,
        321,
        "exif_user",
        profile_url,
        [url],
        media_exif={url: exif_payload},
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT latitude, longitude, exif_json FROM items WHERE image_url=?",
            (url,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    lat, lon, exif_json = row
    assert lat == pytest.approx(exif_payload["lat"])
    assert lon == pytest.approx(exif_payload["lon"])
    data = json.loads(exif_json)
    assert data["camera_make"] == "Canon"
    assert data["iso"] == 200
