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


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _sqlite_storage(conn):
    """Optional SQLite dbstat extension; report physical pages, not a PG estimate."""
    try:
        objects = {name: (table, kind) for name, table, kind in conn.execute(
            "SELECT name, tbl_name, type FROM sqlite_master WHERE type IN ('table','index')")}
        sizes = {}
        for name, size, unused in conn.execute("SELECT name, SUM(pgsize), SUM(unused) FROM dbstat GROUP BY name"):
            table, kind = objects.get(name, (name, "table"))
            entry = sizes.setdefault(table, {"table_mib": 0, "indexes_mib": 0, "unused_mib": 0})
            entry["indexes_mib" if kind == "index" else "table_mib"] += size / 1024**2
            entry["unused_mib"] += unused / 1024**2
        return {name: {kind: round(size, 2) for kind, size in sizes.items()}
                for name, sizes in sorted(sizes.items())}
    except sqlite3.OperationalError:
        # Some Windows SQLite distributions do not include SQLITE_ENABLE_DBSTAT_VTAB.
        return None


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
        extra = sorted(tables - TABLE_COLUMNS.keys() - {"sqlite_sequence"})
        extra_details = {}
        for table in extra:
            quoted = _identifier(table)
            extra_details[table] = {
                "rows": conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0],
                "columns": [{"name": row[1], "type": row[2], "primary_key": row[5]}
                            for row in conn.execute(f"PRAGMA table_info({quoted})")],
                "foreign_keys": [{"table": row[2], "from": row[3], "to": row[4]}
                                 for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")],
            }
        item_columns = {row[1] for row in conn.execute('PRAGMA table_info("items")')}
        measured = [column for column in ("meta_json", "image_url", "profile_url", "source_file")
                    if column in item_columns]
        expressions = [f"COALESCE(SUM(LENGTH(CAST({_identifier(column)} AS BLOB))), 0)" for column in measured]
        payload = conn.execute("SELECT " + ", ".join(expressions) + " FROM items").fetchone() if measured else ()
        orphan_comments = conn.execute("""SELECT COUNT(*) FROM comments c
            LEFT JOIN items i ON i.id=c.item_id WHERE i.id IS NULL""").fetchone()[0]
        report = {
            "file_mib": round(path.stat().st_size / 1024**2, 2),
            "wal_mib": round(Path(str(path) + "-wal").stat().st_size / 1024**2, 2)
                if Path(str(path) + "-wal").is_file() else 0,
            "sqlite_used_pages_mib": round((page_count - free_count) * page_size / 1024**2, 2),
            "sqlite_free_pages_mib": round(free_count * page_size / 1024**2, 2),
            "rows": counts,
            "ignored_tables": extra,
            "ignored_table_details": extra_details,
            "sqlite_storage_mib": _sqlite_storage(conn),
            "items_text_payload_mib": {name: round(size / 1024**2, 2) for name, size in zip(measured, payload)},
            "orphan_comments": orphan_comments,
            "postgres_size_note": "SQLite page usage is not a PostgreSQL size estimate. Allow room for indexes and growth.",
        }
        return conn, report
    except Exception:
        conn.close()
        raise


def migrate(path: Path, url: str, batch_size=1000, allow_skipped_tables=False):
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError("Configure DATABASE_URL with a PostgreSQL URL")
    if batch_size < 1 or batch_size > 10000:
        raise ValueError("Batch size must be 1–10000")
    source, report = inspect_database(path)
    with closing(source):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if report["orphan_comments"]:
            raise ValueError("Orphan comments found; resolve them before migration. No rows were imported.")
        if report["ignored_tables"] and not allow_skipped_tables:
            raise ValueError("Additional tables found. Review ignored_table_details before import. "
                             "Use --allow-skipped-tables only if their omission is intentional.")
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
    parser.add_argument("--allow-skipped-tables", action="store_true",
                        help="Explicitly allow omission of additional SQLite tables after review")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            conn, report = inspect_database(args.source)
            conn.close()
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            migrate(args.source, database_url(), args.batch_size, args.allow_skipped_tables)
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
