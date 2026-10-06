import json
import re

import pytest
from bs4 import BeautifulSoup

from export_profile_html import PhotoEntry, ProfilePayload, build_profile_html
from html_utils import json_for_html
from vsco_parser import build_gallery_html, build_map_html


PAYLOADS = [
    '</script><script>window.__xss = 1</script>',
    '</ScRiPt ><img src=x onerror="window.__xss=1">',
    '<!--<script>Комментарий & кавычки " \' \\ 😀\u2028\u2029',
    '${window.__xss=1}` <img src=x onerror="window.__xss=1">',
]


def read_inline_json(document, variable):
    soup = BeautifulSoup(document, "html.parser")
    for script in soup.find_all("script"):
        match = re.search(r"\bconst " + re.escape(variable) + r"\s*=\s*", script.get_text())
        if match:
            return json.JSONDecoder().raw_decode(script.get_text()[match.end():])[0]
    raise AssertionError(f"Missing inline data: {variable}")


@pytest.mark.parametrize("payload", PAYLOADS)
def test_inline_json_preserves_text_without_html_delimiters(payload):
    value = {"comments": [payload], "nested": {payload: "Обычный текст"}}
    encoded = json_for_html(value)
    assert json.loads(encoded) == value
    assert not any(char in encoded for char in "<>&\u2028\u2029")


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("kind", ["gallery", "map", "profile"])
def test_exports_keep_untrusted_values_inside_script_data(payload, kind):
    item = {"username": payload, "profile_url": payload, "image_url": payload,
            "latitude": 1.0, "longitude": 2.0}
    if kind == "gallery":
        document = build_gallery_html([item])
        assert read_inline_json(document, "data") == [item]
        script_count = 1
    elif kind == "map":
        document = build_map_html([item])
        assert read_inline_json(document, "users")[0]["username"] == payload
        assert read_inline_json(document, "users")[0]["image_url"] == payload
        script_count = 2
    else:
        photo = PhotoEntry(id=1, url=payload, created_at=None, lat=1.0, lon=2.0,
                           comments=[payload], size_bytes=None, camera=None, city=None)
        document = build_profile_html(ProfilePayload(payload, payload, [photo]))
        assert read_inline_json(document, "MAP_DATA")[0]["url"] == payload
        assert read_inline_json(document, "PROFILE_USERNAME") == payload
        assert BeautifulSoup(document, "html.parser").select_one(".comments li").get_text() == payload
        script_count = 2
    soup = BeautifulSoup(document, "html.parser")
    assert len(soup.find_all("script")) == script_count
    assert not soup.select("[onerror]")
