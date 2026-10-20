"""Backend contract and migration tests; PostgreSQL uses a fresh isolated schema."""
import contextlib
import io
import importlib
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, quote, urlsplit, urlunsplit
from unittest.mock import patch

import psycopg
from psycopg import sql

from database import connect_database, initialize_schema, stream_query
from export_profile_html import load_profile
from profile_link_scanner import store_profile_media
from scripts.migrate_to_postgres import inspect_database, migrate
from startup_broadcast import StartupBroadcast


class BackendContract:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bot.db"
        self.env = patch.dict(os.environ, {"DATABASE_URL": self.url()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = connect_database(self.path)
        self.addCleanup(self.conn.close)
        initialize_schema(self.conn)

    def insert_item(self, metadata="{}"):
        cursor = self.conn.execute("""INSERT INTO items
            (chat_id,username,profile_url,image_url,meta_json,created_at)
            VALUES(?,?,?,?,?,?)""", (5027274716, "alice", "https://vsco.co/alice/gallery",
                "https://im.vsco.co/a.jpg", metadata, "2026-10-20T12:00:00+03:00"))
        self.conn.commit()
        return cursor.lastrowid

    def test_insert_ignore_large_telegram_id_and_generated_id(self):
        item_id = self.insert_item()
        self.assertGreater(item_id, 0)
        cursor = self.conn.execute("""INSERT OR IGNORE INTO items
            (chat_id,username,profile_url,image_url,created_at) VALUES(?,?,?,?,?)""",
            (5027274716, "alice", "https://vsco.co/alice/gallery", "https://im.vsco.co/a.jpg", "2026-10-20"))
        self.assertEqual(cursor.rowcount, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_bound_values_quotes_question_marks_and_percent_literals(self):
        row = self.conn.execute("SELECT '?' AS literal, ? AS value, '100%' AS pct", ("x'; DROP TABLE items; -- ? %",)).fetchone()
        self.assertEqual(row, ("?", "x'; DROP TABLE items; -- ? %", "100%"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_transaction_rollback_matches_sqlite(self):
        with self.assertRaises(ValueError):
            with self.conn:
                self.conn.execute("INSERT INTO user_access_state(user_id) VALUES(?)", (99,))
                raise ValueError("rollback")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM user_access_state").fetchone()[0], 0)

    def test_profile_export_preserves_comments_metadata_and_ids(self):
        item_id = self.insert_item('{"camera": "Test"}')
        self.conn.execute("INSERT INTO comments(item_id,chat_id,comment,created_at) VALUES(?,?,?,?)",
                          (item_id, 5027274716, "Комментарий", "2026-10-20"))
        self.conn.commit()
        profile = load_profile(self.conn, "alice", chat_id=5027274716)
        self.assertEqual(profile.username, "alice")
        self.assertEqual(len(profile.photos), 1)
        self.assertIn("Комментарий", profile.photos[0].comments)

    def test_scanner_uses_same_backend_and_deduplicates(self):
        # The public scanner writes via its own connect_db() helper.
        result = store_profile_media(
            self.path, username="alice", profile_url="https://vsco.co/alice/gallery",
            media_urls=["https://im.vsco.co/a.jpg"], chat_id=5027274716)
        self.assertEqual(result.added_items, 1)
        self.assertTrue(result.link_added)
        again = store_profile_media(
            self.path, username="alice", profile_url="https://vsco.co/alice/gallery",
            media_urls=["https://im.vsco.co/a.jpg"], chat_id=5027274716)
        self.assertEqual(again.added_items, 0)

    def test_streaming_gallery_cursor_and_startup_registry(self):
        self.insert_item()
        cursor = stream_query(self.conn, "SELECT username FROM items WHERE chat_id=?", (5027274716,))
        self.assertEqual(cursor.fetchmany(1), [("alice",)])
        cursor.close()
        self.conn.commit()
        broadcast = StartupBroadcast(lambda: connect_database(self.path), enabled=False, text="hello")
        broadcast.initialize()
        self.assertEqual(broadcast._recipients(), [5027274716])

    def test_bot_stats_links_map_and_streamed_gallery_use_backend(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456789:TEST_TOKEN_NOT_USED_FOR_REQUESTS",
            "BOT_WORKDIR": str(Path(self.directory.name) / "work"),
            "BOT_LOGDIR": str(Path(self.directory.name) / "logs"),
            "BOT_SEND_TIMEOUT": "600", "BOT_REQUIRED_UNIQUE_LINKS": "10",
            "BOT_MEDIA_SCAN_MAX_WIDTH": "2048", "BOT_PROFILE_SCAN_WORKERS": "1",
            "BOT_META_FETCH_WORKERS": "1", "BOT_PROFILE_NORMALIZE_CONCURRENCY": "1",
            "BOT_REQUIRED_ACCESS_CACHE_TTL": "30", "BOT_REQUIRED_CHANNEL_ID": "",
            "BOT_REQUIRED_GROUP_ID": "", "BOT_ARCHIVE_ADMIN_CHANNEL_ID": "",
            "BOT_ARCHIVE_SUMMARY_CHANNEL_ID": "", "BOT_LINKS_NOTIFY_CHAT_ID": "",
        }):
            bot_module = importlib.import_module("vsco_bot")
        with patch.object(bot_module, "DB_PATH", str(self.path)):
            bot_module.init_db()
            bot_module.upsert_items_with_comments(5027274716, [{
                "username": "alice", "url": "https://vsco.co/alice/gallery",
                "image_url": "https://im.vsco.co/a.jpg", "comment": "A comment"
            }], "test", None, "tester")
            stats = bot_module.get_stats(5027274716, "chat")
            self.assertEqual(stats["total"]["media_total"], 1)
            groups = list(bot_module.fetch_gallery_users("chat", 5027274716, chunk_size=1))
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]["images"]), 1)
            bot_module.fetch_items_for_map("chat", 5027274716)
            bot_module._links_since_query(5027274716, "chat", "2000-01-01", 10, 0)
            bot_module._collect_pending_link_notifications("2000-01-01")


class SQLiteTests(BackendContract, unittest.TestCase):
    def url(self):
        return ""

    def test_invalid_database_url_never_creates_local_fallback(self):
        other_path = Path(self.directory.name) / "unexpected.db"
        with patch.dict(os.environ, {"DATABASE_URL": "https://not-postgres.example"}):
            with self.assertRaises(ValueError):
                connect_database(other_path)
        self.assertFalse(other_path.exists())

    def test_failed_remote_connection_never_creates_local_fallback(self):
        other_path = Path(self.directory.name) / "unexpected.db"
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example.invalid/db"}):
            with patch("database.psycopg.connect", side_effect=psycopg.OperationalError("offline")):
                with self.assertRaises(psycopg.OperationalError):
                    connect_database(other_path)
        self.assertFalse(other_path.exists())


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for a PostgreSQL test instance")
class PostgreSQLTests(BackendContract, unittest.TestCase):
    def url(self):
        base = os.environ["TEST_DATABASE_URL"]
        schema = "test_" + uuid.uuid4().hex
        admin = psycopg.connect(base, autocommit=True)
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        def cleanup():
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()
        self.addCleanup(cleanup)
        parts = urlsplit(base)
        query = parse_qsl(parts.query) + [("options", f"-c search_path={schema}")]
        return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))

    def source(self):
        source_path = Path(self.directory.name) / "source.db"
        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            conn = connect_database(source_path)
            initialize_schema(conn)
            conn.execute("INSERT INTO links(id,chat_id,username,url,created_at) VALUES(?,?,?,?,?)",
                         (41, 5027274716, "alice", "https://vsco.co/alice", "2026-10-20"))
            conn.execute("INSERT INTO items(id,chat_id,username,image_url,created_at) VALUES(?,?,?,?,?)",
                         (73, 5027274716, "alice", "https://im.vsco.co/a.jpg", "2026-10-20"))
            conn.execute("INSERT INTO comments(id,item_id,chat_id,comment,created_at) VALUES(?,?,?,?,?)",
                         (101, 73, 5027274716, "Imported", "2026-10-20"))
            conn.commit()
            conn.close()
        return source_path

    def test_streaming_import_preserves_ids_and_reseeds_sequences(self):
        source = self.source()
        with contextlib.redirect_stdout(io.StringIO()):
            migrate(source, os.environ["DATABASE_URL"], batch_size=1)
        self.assertEqual(self.conn.execute("SELECT item_id,comment FROM comments").fetchone(), (73, "Imported"))
        self.assertEqual(self.conn.execute("SELECT id FROM links").fetchone(), (41,))
        self.conn.commit()
        self.assertGreater(self.insert_item(), 73)

    def test_copy_failure_rolls_back_rows_from_earlier_tables(self):
        source = self.source()
        with sqlite3.connect(source) as local:
            local.execute("UPDATE items SET meta_json=?", ("invalid\x00text",))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(psycopg.Error):
            migrate(source, os.environ["DATABASE_URL"], batch_size=1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0)

    def test_nonempty_destination_is_never_overwritten(self):
        source = self.source()
        self.insert_item()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Destination contains"):
            migrate(source, os.environ["DATABASE_URL"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

    def test_timestamp_sorting_accepts_invalid_legacy_values_as_null(self):
        row = self.conn.execute("SELECT datetime(created_at) FROM (SELECT ? AS created_at) t", ("not a date",)).fetchone()
        self.assertEqual(row, (None,))

    def test_orphans_detected_before_import(self):
        source = self.source()
        with sqlite3.connect(source) as local:
            local.execute("UPDATE comments SET item_id=999")
        conn, report = inspect_database(source)
        conn.close()
        self.assertEqual(report["orphan_comments"], 1)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Orphan comments"):
            migrate(source, os.environ["DATABASE_URL"])


if __name__ == "__main__":
    unittest.main()
