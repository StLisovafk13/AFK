import io

import pytest
from PIL import Image

from vsco_exif import (
    DEFAULT_USER_AGENT,
    _build_parser,
    extract_exif_from_bytes,
    fetch_image_bytes,
    get_exif_from_url,
    main,
)


class _BytesResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def test_fetch_image_bytes_downloads_with_custom_user_agent(monkeypatch):
    url = "https://example.com/test.jpg"
    payload = b"image-bytes"

    def fake_urlopen(request, timeout):
        assert request.full_url == url
        assert request.get_header("User-agent") == DEFAULT_USER_AGENT
        assert timeout == pytest.approx(5.0)
        return _BytesResponse(payload)

    monkeypatch.setattr("vsco_exif.urlopen", fake_urlopen)

    result = fetch_image_bytes(url, timeout=5.0)
    assert result == payload


def test_fetch_image_bytes_rejects_empty_url():
    with pytest.raises(ValueError):
        fetch_image_bytes("")


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (5, 5), color="red")
    exif = Image.Exif()
    exif[0x010F] = "ExampleCam"  # Make
    exif[0x0110] = "ExampleCam X"  # Model
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_extract_exif_from_bytes_returns_named_tags():
    payload = _jpeg_with_exif()

    result = extract_exif_from_bytes(payload)

    assert result["Make"] == "ExampleCam"
    assert result["Model"] == "ExampleCam X"


def test_extract_exif_from_bytes_returns_empty_dict_for_missing_data():
    assert extract_exif_from_bytes(b"") == {}


def test_get_exif_from_url_chains_download_and_parsing(monkeypatch):
    payload = _jpeg_with_exif()

    def fake_fetch(url, timeout):
        assert url == "https://images.example.com/photo.jpg"
        assert timeout == pytest.approx(7.5)
        return payload

    monkeypatch.setattr("vsco_exif.fetch_image_bytes", fake_fetch)

    result = get_exif_from_url("https://images.example.com/photo.jpg", timeout=7.5)

    assert result["Make"] == "ExampleCam"


def test_cli_parser_accepts_arguments():
    parser = _build_parser()
    args = parser.parse_args(["https://cdn.example.com/photo.jpg", "--timeout", "2", "--pretty"])
    assert args.url == "https://cdn.example.com/photo.jpg"
    assert args.timeout == pytest.approx(2.0)
    assert args.pretty is True


def test_cli_main_prints_json(monkeypatch, capsys):
    fake_exif = {"Make": "ExampleCam", "Model": "ExampleCam X"}

    def fake_get(url, timeout):
        assert url == "https://cdn.example.com/photo.jpg"
        assert timeout == pytest.approx(3.0)
        return fake_exif

    monkeypatch.setattr("vsco_exif.get_exif_from_url", fake_get)

    exit_code = main(["https://cdn.example.com/photo.jpg", "--timeout", "3", "--pretty"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ExampleCam" in captured.out
    assert captured.err == ""
