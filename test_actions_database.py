import io
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts.actions_database import restore_database, snapshot_database


class ActionsDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "original.db"
        self.runtime = self.root / "runtime" / "vsco_links.db"
        self.archive = self.root / "saved.zip"
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("CREATE TABLE records (value TEXT)")
            conn.execute("INSERT INTO records VALUES ('existing data')")
            conn.commit()

    def rows(self, database):
        with closing(sqlite3.connect(database)) as conn:
            return conn.execute("SELECT value FROM records").fetchall()

    def test_first_run_creates_valid_empty_database(self):
        self.assertEqual(restore_database(self.runtime, self.archive), "empty")
        with closing(sqlite3.connect(self.runtime)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_snapshot_includes_committed_wal_without_copying_uncommitted_rows(self):
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO records VALUES ('committed in WAL')")
            writer.commit()
            writer.execute("INSERT INTO records VALUES ('not committed')")
            snapshot_database(self.source, self.runtime)
            writer.rollback()
        self.assertEqual(self.rows(self.runtime), [("existing data",), ("committed in WAL",)])
        self.assertFalse(Path(str(self.runtime) + "-wal").exists())

    def test_next_run_restores_snapshot_and_ignores_seed(self):
        with zipfile.ZipFile(self.archive, "w") as saved:
            saved.write(self.source, "vsco_links.db")
        with patch("scripts.actions_database.urllib.request.urlopen") as download:
            result = restore_database(self.runtime, self.archive, "https://example.com/seed")
        self.assertEqual(result, "artifact")
        download.assert_not_called()
        self.assertEqual(self.rows(self.runtime), [("existing data",)])

    def test_corrupt_archive_does_not_create_database(self):
        with zipfile.ZipFile(self.archive, "w") as saved:
            saved.writestr("vsco_links.db", "this is not SQLite")
        with self.assertRaises(ValueError):
            restore_database(self.runtime, self.archive)
        self.assertFalse(self.runtime.exists())

    def test_archive_cannot_extract_arbitrary_paths(self):
        with zipfile.ZipFile(self.archive, "w") as saved:
            saved.writestr("../escape.db", "unexpected")
        with self.assertRaises(KeyError):
            restore_database(self.runtime, self.archive)
        self.assertFalse((self.root / "escape.db").exists())

    def test_existing_runtime_database_is_never_overwritten(self):
        self.runtime.parent.mkdir()
        self.runtime.write_bytes(b"preserve me")
        with self.assertRaises(ValueError):
            restore_database(self.runtime, self.archive)
        self.assertEqual(self.runtime.read_bytes(), b"preserve me")

    def test_bad_snapshot_source_preserves_previous_backup(self):
        self.runtime.parent.mkdir()
        self.runtime.write_bytes(b"previous backup")
        self.source.write_bytes(b"broken source")
        with self.assertRaises(ValueError):
            snapshot_database(self.source, self.runtime)
        self.assertEqual(self.runtime.read_bytes(), b"previous backup")

    def test_seed_download_is_validated_as_sqlite(self):
        stream = io.BytesIO(self.source.read_bytes())
        stream.geturl = lambda: "https://example.com/backup"
        with patch("scripts.actions_database.urllib.request.urlopen", return_value=stream):
            self.assertEqual(restore_database(self.runtime, self.archive, "https://example.com/backup"), "seed")
        self.assertEqual(self.rows(self.runtime), [("existing data",)])

    def test_failed_seed_does_not_log_signed_url_or_create_database(self):
        secret_url = "https://example.com/private?signature=secret"
        with patch("scripts.actions_database.urllib.request.urlopen", side_effect=OSError(secret_url)):
            with self.assertRaises(ValueError) as error:
                restore_database(self.runtime, self.archive, secret_url)
        self.assertNotIn("signature", str(error.exception))
        self.assertFalse(self.runtime.exists())

    def test_oversized_archive_is_rejected(self):
        with zipfile.ZipFile(self.archive, "w") as saved:
            saved.write(self.source, "vsco_links.db")
        with patch("scripts.actions_database.MAX_DATABASE_BYTES", 10):
            with self.assertRaises(ValueError):
                restore_database(self.runtime, self.archive)
        self.assertFalse(self.runtime.exists())


if __name__ == "__main__":
    unittest.main()
