"""Shared SQLite/PostgreSQL access for the bot, scanners and exports.

Application queries use qmark parameters. The PostgreSQL adapter translates this
small project's existing SQL dialect; values always remain bound parameters.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path

import psycopg
from dotenv import dotenv_values


DatabaseError = (sqlite3.Error, psycopg.Error)
OperationalError = (sqlite3.OperationalError, psycopg.OperationalError, psycopg.ProgrammingError)
ENV_PATH = Path(__file__).with_name(".env")

# Keep the established names and text formats so existing exports stay valid.
TABLE_COLUMNS = {
    "links": ("id", "chat_id", "username", "url", "extra_json", "created_at", "notified_at"),
    "items": ("id", "chat_id", "username", "latitude", "longitude", "profile_url", "image_url", "source", "source_file", "added_by", "meta_json", "created_at"),
    "comments": ("id", "item_id", "chat_id", "comment", "created_at"),
    "user_access_links": ("user_id", "profile_url", "created_at"),
    "user_access_state": ("user_id", "invite_sent_at"),
    "bot_private_chats": ("chat_id",),
}
IDENTITY_TABLES = {"links", "items", "comments"}
_schema_ready = set()
_schema_lock = threading.Lock()


def database_url() -> str:
    raw = os.getenv("DATABASE_URL")
    if raw is None:
        raw = dotenv_values(ENV_PATH, interpolate=False).get("DATABASE_URL", "")
    return (raw or "").strip()


def _postgres_query(query: str, parameters: bool) -> str:
    """Translate placeholders, preserving quoted '?' and percent literals."""
    output = []
    quote = None
    i = 0
    while i < len(query):
        char = query[i]
        if char in {"'", '"'}:
            if quote == char:
                if i + 1 < len(query) and query[i + 1] == char:
                    output.extend((char, char))
                    i += 2
                    continue
                quote = None
            elif quote is None:
                quote = char
        if char == "?" and quote is None:
            output.append("%s")
        elif char == "%" and parameters:
            output.append("%%")
        else:
            output.append(char)
        i += 1
    query = "".join(output).strip().rstrip(";")
    query = re.sub(r"\bdatetime\(created_at\)", "vsco_datetime(created_at)", query, flags=re.I)
    query = re.sub(r"\busername COLLATE NOCASE\b", "LOWER(username)", query, flags=re.I)
    if re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", query, re.I):
        query = re.sub(r"^INSERT\s+OR\s+IGNORE", "INSERT", query, flags=re.I)
        query += " ON CONFLICT DO NOTHING"
    return query


class PostgresCursor:
    def __init__(self, connection):
        self.connection = connection
        self.raw = connection.raw.cursor()
        self.lastrowid = None

    @property
    def rowcount(self):
        return self.raw.rowcount

    def execute(self, query, params=None):
        translated = _postgres_query(query, params is not None)
        match = re.match(r"INSERT\s+INTO\s+(links|items|comments)\s*\(", translated, re.I)
        if match:
            translated += " RETURNING id"
        self.raw.execute(translated, params)
        self.lastrowid = None
        if match:
            row = self.raw.fetchone()
            self.lastrowid = row[0] if row else None
        return self

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()

    def fetchmany(self, size=1):
        return self.raw.fetchmany(size)

    def close(self):
        self.raw.close()

    def __iter__(self):
        return iter(self.raw)


class PostgresConnection:
    def __init__(self, url):
        self.identity = url
        # No fallback to SQLite on configuration or connection failures.
        # Transaction-pooling endpoints (including Neon pooled URLs) must not
        # depend on session-bound prepared statement state.
        self.raw = psycopg.connect(url, connect_timeout=20, prepare_threshold=None)

    def cursor(self):
        return PostgresCursor(self)

    def execute(self, query, params=None):
        return self.cursor().execute(query, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        # Match sqlite3: transactions commit/rollback, connection stays open.
        if kind is None:
            self.commit()
        else:
            self.rollback()


Connection = sqlite3.Connection | PostgresConnection


def connect_database(path="vsco_links.db"):
    url = database_url()
    if url:
        if not url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        return PostgresConnection(url)
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def stream_query(conn, query, params=None):
    """Use a server cursor for large PostgreSQL galleries instead of buffering rows."""
    if isinstance(conn, PostgresConnection):
        cursor = conn.raw.cursor(name="vsco_" + uuid.uuid4().hex)
        cursor.execute(_postgres_query(query, params is not None), params)
        return cursor
    return conn.execute(query, params) if params is not None else conn.execute(query)


def initialize_schema(conn):
    postgres = isinstance(conn, PostgresConnection)
    with _schema_lock:
        if postgres and conn.identity in _schema_ready:
            return
        identity = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        integer = "BIGINT" if postgres else "INTEGER"
        real = "DOUBLE PRECISION" if postgres else "REAL"
        statements = [
            f"""CREATE TABLE IF NOT EXISTS links (
                id {identity}, chat_id {integer} NOT NULL, username TEXT NOT NULL,
                url TEXT NOT NULL, extra_json TEXT DEFAULT '', created_at TEXT NOT NULL,
                notified_at TEXT DEFAULT '', UNIQUE(chat_id, username))""",
            f"""CREATE TABLE IF NOT EXISTS items (
                id {identity}, chat_id {integer} NOT NULL, username TEXT DEFAULT '',
                latitude {real}, longitude {real}, profile_url TEXT DEFAULT '',
                image_url TEXT DEFAULT '', source TEXT DEFAULT '', source_file TEXT DEFAULT '',
                added_by TEXT DEFAULT '', meta_json TEXT DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(chat_id, username, image_url, profile_url))""",
            f"""CREATE TABLE IF NOT EXISTS comments (
                id {identity}, item_id {integer} NOT NULL, chat_id {integer} NOT NULL,
                comment TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(item_id, comment),
                FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE)""",
            f"""CREATE TABLE IF NOT EXISTS user_access_links (
                user_id {integer} NOT NULL, profile_url TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, profile_url))""",
            f"""CREATE TABLE IF NOT EXISTS user_access_state (
                user_id {integer} PRIMARY KEY, invite_sent_at TEXT DEFAULT '')""",
            f"CREATE TABLE IF NOT EXISTS bot_private_chats (chat_id {integer} PRIMARY KEY)",
        ]
        try:
            for query in statements:
                conn.execute(query)
            if postgres:
                conn.raw.execute("""
                    CREATE OR REPLACE FUNCTION vsco_datetime(value text) RETURNS timestamptz
                    LANGUAGE plpgsql STABLE AS $$ BEGIN RETURN value::timestamptz;
                    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL;
                    END $$
                """)
            # Preserve the small additive migrations for older local SQLite files.
            additions = {"items": ("added_by", "meta_json"), "links": ("extra_json", "notified_at")}
            for table, columns in additions.items():
                if postgres:
                    for column in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT DEFAULT ''")
                else:
                    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    for column in columns:
                        if column not in existing:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if postgres:
            _schema_ready.add(conn.identity)
