"""Keep the test suite independent of live bot credentials and storage."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def pytest_configure(config):
    workspace = TemporaryDirectory(prefix="afk-pytest-")
    config.add_cleanup(workspace.cleanup)
    env = pytest.MonkeyPatch()
    config.add_cleanup(env.undo)
    for key in list(os.environ):
        if key.startswith(("BOT_", "VSCO_", "TELEGRAM_")):
            env.delenv(key)
    root = Path(workspace.name)
    for key, value in {
        "PYTHON_DOTENV_DISABLED": "1",
        "TELEGRAM_BOT_TOKEN": "123:TESTTOKEN",
        "BOT_DB_PATH": str(root / "test.db"),
        "BOT_WORKDIR": str(root / "work"),
        "BOT_LOGDIR": str(root / "logs"),
    }.items():
        env.setenv(key, value)
