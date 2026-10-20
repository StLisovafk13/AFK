"""Inspect a local SQLite database, then stream it into an empty PostgreSQL database."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# Allow `python scripts/migrate_to_postgres.py` from the project directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql

from database import IDENTITY_TABLES, TABLE_COLUMNS, PostgresConnection, database_url, initialize_schema


def inspect_database(path: Path):
    if not path.is_file():
        raise ValueError("SQLite source does not exist")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        # One read transaction gives counts and subsequent COPY the same snapshot.
        conn.execute("BEGIN")
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("SQLite integrity check failed")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"links", "items", "comments"}.issubset(tables):
            raise ValueError("Source is not the main VSCO bot database")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        counts = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for table in TABLE_COLUMNS if table in tables}
        orphan_comments = conn.execute("""SELECT COUNT(*) FROM comments c
            LEFT JOIN items i ON i.id=c.item_id WHERE i.id IS NULL""").fetchone()[0]
        report = {
            "file_mib": round(path.stat().st_size / 1024**2, 2),
            "wal_mib": round(Path(str(path) + "-wal").stat().st_size / 1024**2, 2)
                if Path(str(path) + "-wal").is_file() else 0,
            "sqlite_used_pages_mib": round((page_count - free_count) * page_size / 1024**2, 2),
            "sqlite_free_pages_mib": round(free_count * page_size / 1024**2, 2),
            "rows": counts,
            "ignored_tables": sorted(tables - TABLE_COLUMNS.keys() - {"sqlite_sequence"}),
            "orphan_comments": orphan_comments,
            "postgres_size_note": "SQLite page usage is not a PostgreSQL size estimate. Allow room for indexes and growth.",
        }
        return conn, report
    except Exception:
        conn.close()
        raise


def migrate(path: Path, url: str, batch_size=1000):
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("Configure DATABASE_URL with a PostgreSQL URL")
    if batch_size < 1 or batch_size > 10000:
        raise ValueError("Batch size must be 1–10000")
    source, report = inspect_database(path)
    with closing(source):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if report["orphan_comments"]:
            raise ValueError("Orphan comments found; resolve them before migration. No rows were imported.")
        with closing(PostgresConnection(url)) as target:
            initialize_schema(target)
            # Import is atomic. Failures rollback all rows; existing data is never replaced.
            with target.raw.transaction():
                target.raw.execute("LOCK TABLE " + ", ".join(TABLE_COLUMNS) + " IN ACCESS EXCLUSIVE MODE")
                for table in TABLE_COLUMNS:
                    if target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
                        raise ValueError("Destination contains bot data. Use an empty database; import was cancelled.")
                for table, columns in TABLE_COLUMNS.items():
                    expected = report["rows"].get(table)
                    if expected is None:
                        continue
                    present = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
                    selected = [column for column in columns if column in present]
                    fields = ", ".join(f'"{column}"' for column in selected)
                    rows = source.execute(f'SELECT {fields} FROM "{table}"')
                    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, selected)))
                    copied = 0
                    with target.raw.cursor() as cursor:
                        with cursor.copy(statement) as writer:
                            while batch := rows.fetchmany(batch_size):
                                for row in batch:
                                    writer.write_row(row)
                                copied += len(batch)
                                if copied % 10000 < len(batch):
                                    print(f"{table}: {copied}/{expected}", flush=True)
                    actual = target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    if actual != expected:
                        raise ValueError(f"Row count mismatch in {table}; import rolled back")
                    print(f"{table}: verified {actual} rows", flush=True)
                # Rebuild runtime chat registry for older databases without this table.
                target.execute("""INSERT OR IGNORE INTO bot_private_chats (chat_id)
                    SELECT chat_id FROM links WHERE chat_id>0
                    UNION SELECT chat_id FROM items WHERE chat_id>0
                    UNION SELECT chat_id FROM comments WHERE chat_id>0""")
                for table in sorted(IDENTITY_TABLES):
                    target.raw.execute(sql.SQL("""SELECT setval(pg_get_serial_sequence(%s, 'id'),
                        COALESCE(MAX(id), 1), COUNT(*) > 0) FROM {}""").format(sql.Identifier(table)), (table,))
            size = target.raw.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
            print(f"Import committed. PostgreSQL database size: {size}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "migrate"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            conn, report = inspect_database(args.source)
            conn.close()
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            migrate(args.source, database_url(), args.batch_size)
    except (ValueError, OSError, sqlite3.Error, psycopg.Error) as error:
        # Avoid driver messages containing connection credentials or row contents.
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            state = getattr(error, "sqlstate", None) or "unavailable"
            print(f"Migration failed ({type(error).__name__}, SQLSTATE={state}). "
                  "Check quota, connectivity and source data.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
