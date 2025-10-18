import io
import json

import pytest
from PIL import Image
from urllib.error import HTTPError

from vsco_exif import (
    DEFAULT_HEADERS,
    PLAYWRIGHT_DEFAULT_HEADERS,
    _build_parser,
    _normalise_cookie,
    _parse_header,
    configure_logging,
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
        headers = {key.lower(): value for key, value in request.header_items()}
        for header, value in DEFAULT_HEADERS.items():
            assert headers.get(header.lower()) == value
        assert timeout == pytest.approx(5.0)
        return _BytesResponse(payload)

    monkeypatch.setattr("vsco_exif.urlopen", fake_urlopen)

    result = fetch_image_bytes(url, timeout=5.0)
    assert result == payload


def test_fetch_image_bytes_rejects_empty_url():
    with pytest.raises(ValueError):
        fetch_image_bytes("")


def test_fetch_image_bytes_translates_403_to_permission_error(monkeypatch):
    url = "https://example.com/forbidden.jpg"

    def fake_urlopen(request, timeout):
        raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr("vsco_exif.urlopen", fake_urlopen)

    with pytest.raises(PermissionError):
        fetch_image_bytes(url)


def test_fetch_image_bytes_merges_extra_headers(monkeypatch):
    url = "https://example.com/with-cookie.jpg"

    def fake_urlopen(request, timeout):
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["cookie"] == "session=abc"
        assert headers["user-agent"] == "Override-Agent"
        return _BytesResponse(b"data")

    monkeypatch.setattr("vsco_exif.urlopen", fake_urlopen)

    result = fetch_image_bytes(
        url,
        headers={"Cookie": "session=abc", "User-Agent": "Override-Agent"},
    )
    assert result == b"data"


def test_fetch_image_bytes_uses_playwright_fallback(monkeypatch):
    url = "https://example.com/protected.jpg"

    def fake_urlopen(request, timeout):
        raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

    def fake_playwright_fetch(url_arg, *, timeout, headers):
        assert url_arg == url
        assert timeout == pytest.approx(7.0)
        merged = {key.lower(): value for key, value in headers.items()}
        for key in DEFAULT_HEADERS:
            assert key.lower() in merged
        for key in PLAYWRIGHT_DEFAULT_HEADERS:
            assert key.lower() in merged
        assert merged["x-extra"] == "value"
        return b"fallback"

    monkeypatch.setattr("vsco_exif.urlopen", fake_urlopen)
    monkeypatch.setattr("vsco_exif._playwright_fetch", fake_playwright_fetch)

    result = fetch_image_bytes(
        url,
        timeout=5.0,
        headers={"X-Extra": "value"},
        use_playwright=True,
        playwright_timeout=7.0,
    )
    assert result == b"fallback"


def test_fetch_image_bytes_playwright_failure_reports(monkeypatch):
    url = "https://example.com/protected.jpg"

    def fake_urlopen(request, timeout):
        raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

    def fake_playwright_fetch(url_arg, *, timeout, headers):
        raise RuntimeError("Playwright missing")

    monkeypatch.setattr("vsco_exif.urlopen", fake_urlopen)
    monkeypatch.setattr("vsco_exif._playwright_fetch", fake_playwright_fetch)

    with pytest.raises(PermissionError) as exc:
        fetch_image_bytes(url, use_playwright=True)
    assert "Playwright fallback failed" in str(exc.value)


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

    def fake_fetch(url, timeout, headers=None, use_playwright=False, playwright_timeout=None):
        assert url == "https://images.example.com/photo.jpg"
        assert timeout == pytest.approx(7.5)
        assert headers is None
        assert use_playwright is True
        assert playwright_timeout == pytest.approx(12.0)
        return payload

    monkeypatch.setattr("vsco_exif.fetch_image_bytes", fake_fetch)

    result = get_exif_from_url(
        "https://images.example.com/photo.jpg",
        timeout=7.5,
        use_playwright=True,
        playwright_timeout=12.0,
    )

    assert result["Make"] == "ExampleCam"


def test_cli_parser_accepts_arguments():
    parser = _build_parser()
    args = parser.parse_args([
        "https://cdn.example.com/photo.jpg",
        "--timeout",
        "2",
        "--pretty",
        "--output",
        "result.json",
        "--log-level",
        "DEBUG",
        "--log-file",
        "exif.log",
        "--header",
        "Authorization: Bearer token",
        "--cookie",
        "session=abc",
        "--playwright",
        "--playwright-timeout",
        "15",
    ])
    assert args.url == "https://cdn.example.com/photo.jpg"
    assert args.timeout == pytest.approx(2.0)
    assert args.pretty is True
    assert args.output == "result.json"
    assert args.log_level == "DEBUG"
    assert args.log_file == "exif.log"
    assert ("Authorization", "Bearer token") in args.header
    assert args.cookie == "session=abc"
    assert args.playwright is True
    assert args.playwright_timeout == pytest.approx(15.0)


def test_normalise_cookie_strips_prefix_and_whitespace():
    raw = "  Cookie:  session=abc; other=def  "
    assert _normalise_cookie(raw) == "session=abc; other=def"


def test_normalise_cookie_collapses_newlines():
    raw = "cookie\nvs_app=1;\n other=2"
    assert _normalise_cookie(raw) == "vs_app=1; other=2"


def test_normalise_cookie_handles_cyrillic_prefix():
    raw = "\u0421ookie: session=abc"  # first letter is Cyrillic capital Es
    assert _normalise_cookie(raw) == "session=abc"


def test_parse_header_normalises_confusable_name():
    name, value = _parse_header("\u0421ookie: value")
    assert name == "Cookie"
    assert value == "value"


def test_cli_main_prints_json(monkeypatch, capsys):
    fake_exif = {"Make": "ExampleCam", "Model": "ExampleCam X"}

    def fake_get(url, timeout, headers=None, use_playwright=False, playwright_timeout=None):
        assert url == "https://cdn.example.com/photo.jpg"
        assert timeout == pytest.approx(3.0)
        assert headers == {"X-Test": "value"}
        assert use_playwright is False
        assert playwright_timeout is None
        return fake_exif

    monkeypatch.setattr("vsco_exif.get_exif_from_url", fake_get)

    exit_code = main([
        "https://cdn.example.com/photo.jpg",
        "--timeout",
        "3",
        "--pretty",
        "--header",
        "X-Test: value",
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ExampleCam" in captured.out
    assert "Starting EXIF extraction" in captured.err


def test_cli_main_rejects_invalid_cookie(monkeypatch, capsys):
    def fake_get(url, timeout, headers=None, use_playwright=False, playwright_timeout=None):
        raise AssertionError("Should not be called when cookie invalid")

    monkeypatch.setattr("vsco_exif.get_exif_from_url", fake_get)

    exit_code = main([
        "https://cdn.example.com/photo.jpg",
        "--cookie",
        "   \n  ",
    ])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error parsing cookie value" in captured.err


def test_cli_main_writes_output_file(monkeypatch, tmp_path):
    fake_exif = {"Make": "ExampleCam", "Model": "ExampleCam X"}
    output_path = tmp_path / "result.json"
    log_path = tmp_path / "exif.log"

    def fake_get(url, timeout, headers=None, use_playwright=False, playwright_timeout=None):
        assert url == "https://cdn.example.com/photo.jpg"
        assert timeout == pytest.approx(4.0)
        assert headers == {"Cookie": "session=abc"}
        assert use_playwright is True
        assert playwright_timeout == pytest.approx(9.0)
        return fake_exif

    monkeypatch.setattr("vsco_exif.get_exif_from_url", fake_get)

    exit_code = main(
        [
            "https://cdn.example.com/photo.jpg",
            "--timeout",
            "4",
            "--output",
            str(output_path),
            "--log-file",
            str(log_path),
            "--log-level",
            "DEBUG",
            "--cookie",
            "session=abc",
            "--playwright",
            "--playwright-timeout",
            "9",
        ]
    )

    assert exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8")) == fake_exif
    log_contents = log_path.read_text(encoding="utf-8")
    assert "EXIF metadata saved" in log_contents


def test_cli_rejects_invalid_header():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["https://example.com", "--header", "MissingColon"])


def test_configure_logging_rejects_invalid_level():
    with pytest.raises(ValueError):
        configure_logging("NOTALEVEL")
