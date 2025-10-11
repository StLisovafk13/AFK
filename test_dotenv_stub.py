from __future__ import annotations

from importlib import reload
from io import StringIO
import os


def _reload_stub():
    import dotenv  # local import to ensure module exists

    return reload(dotenv)


def test_load_dotenv_reads_current_directory(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\n# comment\nexport BAZ='qux'\nEMPTY=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAZ", raising=False)
    monkeypatch.delenv("EMPTY", raising=False)

    dotenv = _reload_stub()
    assert dotenv.load_dotenv() is True

    assert os.getenv("FOO") == "bar"
    assert os.getenv("BAZ") == "qux"
    assert os.getenv("EMPTY") == ""


def test_load_dotenv_respects_override(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("FOO=new\n", encoding="utf-8")
    monkeypatch.setenv("FOO", "old")

    dotenv = _reload_stub()
    assert dotenv.load_dotenv(env_file, override=False) is True
    assert os.getenv("FOO") == "old"

    assert dotenv.load_dotenv(env_file, override=True) is True
    assert os.getenv("FOO") == "new"


def test_load_dotenv_supports_stream(monkeypatch):
    monkeypatch.delenv("STREAM_VALUE", raising=False)

    dotenv = _reload_stub()
    assert dotenv.load_dotenv(stream=StringIO("STREAM_VALUE=123")) is True
    assert os.getenv("STREAM_VALUE") == "123"
