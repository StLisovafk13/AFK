import asyncio
import logging
import os
import zipfile

import pytest

from vsco_downloader import build_zip_multi, collect_image_urls


def test_scan_stops_when_visible_load_more_repeatedly_fails():
    class Button:
        first = property(lambda self: self)
        async def count(self): return 1
        async def is_visible(self): return True
        async def get_attribute(self, name): return None
        async def scroll_into_view_if_needed(self): pass
        async def click(self): raise TimeoutError("overlay")

    class Page:
        url = "https://vsco.co/example/gallery"
        reads = 0
        async def content(self):
            self.reads += 1
            assert self.reads <= 7, "Scan did not stop after repeated failure"
            return '<img src="https://cdn.example/photo.jpg">'
        async def evaluate(self, script): return 100
        async def wait_for_timeout(self, timeout): pass
        def locator(self, selector): return Button()

    page = Page()
    result = asyncio.run(collect_image_urls(page, logging.getLogger("test"), 0, 1, 0, 2048))
    assert len(result) == 1 and page.reads == 6


def test_zip_rejects_single_oversized_media_and_keeps_original(tmp_path):
    media = tmp_path / "video.mp4"
    data = os.urandom(2 * 1024 * 1024)
    media.write_bytes(data)
    with pytest.raises(RuntimeError, match="не помещается"):
        build_zip_multi(logging.getLogger("test"), tmp_path, tmp_path / "manifest.json",
                        tmp_path / "urls.txt", [{"ok": True, "file": str(media)}], "profile.zip", 1)
    assert media.read_bytes() == data
    assert list(tmp_path.glob("*.zip")) == []


def test_zip_accounts_for_metadata_and_repartitions_using_actual_size(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(os.urandom(700_000))
    expected = {}
    for name in ("one.jpg", "two.jpg"):
        expected[name] = os.urandom(280_000)
        (tmp_path / name).write_bytes(expected[name])
    paths = build_zip_multi(logging.getLogger("test"), tmp_path, manifest, tmp_path / "urls.txt",
                            [{"ok": True, "file": str(tmp_path / name)} for name in expected], "profile.zip", 1)
    assert len(paths) == 2
    found = {}
    for path in paths:
        assert path.stat().st_size <= 1024 * 1024
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert archive.read("manifest.json") == manifest.read_bytes()
            found.update({name: archive.read(name) for name in archive.namelist() if name.endswith(".jpg")})
    assert found == expected


def test_zip_failure_removes_partial_archives(tmp_path):
    items = []
    for name, size in (("small.jpg", 1000), ("large.mp4", 2 * 1024 * 1024)):
        path = tmp_path / name
        path.write_bytes(os.urandom(size))
        items.append({"ok": True, "file": str(path)})
    with pytest.raises(RuntimeError, match="не помещается"):
        build_zip_multi(logging.getLogger("test"), tmp_path, tmp_path / "manifest.json",
                        tmp_path / "urls.txt", items, "profile.zip", 1)
    assert not list(tmp_path.glob("*.zip"))
