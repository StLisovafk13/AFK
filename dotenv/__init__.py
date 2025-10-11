"""Lightweight subset of the :mod:`python-dotenv` package used in tests.

The real dependency is fairly feature rich, however the project only relies on
its :func:`load_dotenv` helper to populate environment variables from a
``.env`` file.  The previous test stub was a no-op which meant the configuration
file was silently ignored and critical settings – for example the
``TELEGRAM_BOT_TOKEN`` – never became available at runtime.  This module
implements a small but functional loader that honours the most common
behaviour: reading key/value pairs from a file (optionally provided via the
``dotenv_path`` argument) and exporting them into :mod:`os.environ` with an
``override`` toggle.
"""

from pathlib import Path
from typing import Iterable, Optional, TextIO, Tuple, Union
import os

__all__ = ["load_dotenv"]


def _iter_env_lines(stream: Iterable[str]) -> Iterable[Tuple[str, str]]:
    for raw_line in stream:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value and ((value[0] == value[-1]) and value[0] in {'"', "'"}):
            value = value[1:-1]
        yield key, value


def _load_stream(stream: Iterable[str], *, override: bool) -> bool:
    loaded = False
    for key, value in _iter_env_lines(stream):
        if override or key not in os.environ:
            os.environ[key] = value
        loaded = True
    return loaded


def _resolve_path(
    dotenv_path: Optional[Union[os.PathLike[str], str]]
) -> Optional[Path]:
    if dotenv_path is None:
        candidate = Path.cwd() / ".env"
        return candidate if candidate.exists() else None
    path = Path(dotenv_path)
    if path.is_dir():
        path = path / ".env"
    return path if path.exists() else None


def load_dotenv(
    dotenv_path: Optional[Union[os.PathLike[str], str]] = None,
    stream: Optional[TextIO] = None,
    override: bool = False,
    encoding: str = "utf-8",
) -> bool:
    """Parse a ``.env`` file and store values in :mod:`os.environ`.

    Only the arguments used within the project are implemented.  The function
    mirrors the behaviour of :func:`python-dotenv.load_dotenv` by returning
    ``True`` when at least one variable has been loaded.
    """

    if stream is not None:
        return _load_stream(stream, override=override)

    path = _resolve_path(dotenv_path)
    if path is None:
        return False

    with path.open("r", encoding=encoding) as handle:
        return _load_stream(handle, override=override)
