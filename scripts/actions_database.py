"""Restore and snapshot SQLite for a bounded GitHub Actions trial."""

import argparse
import os
import sqlite3
import tempfile
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


MAX_DATABASE_BYTES = 512 * 1024 * 1024


def snapshot_database(source: Path, destination: Path) -> None:
    """SQLite backup includes committed WAL data in a standalone database file."""
    if not source.is_file():
        raise ValueError("Database file does not exist")
    with source.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise ValueError("Source is not a SQLite database")
    if source.resolve() == destination.resolve():
        raise ValueError("Snapshot must use a different path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        temporary = Path(directory) / "snapshot.db"
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("Database integrity check failed")
        os.replace(temporary, destination)


def _copy_limited(source, destination: Path) -> None:
    size = 0
    with destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_DATABASE_BYTES:
                raise ValueError("Database exceeds the 512 MiB trial import limit")
            target.write(chunk)


def restore_database(database: Path, archive: Path, seed_url: str = "") -> str:
    if database.exists():
        raise ValueError("Refusing to overwrite an existing runtime database")
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=database.parent) as directory:
        temporary = Path(directory) / "import.db"
        if archive.is_file():
            with zipfile.ZipFile(archive) as saved:
                entry = saved.getinfo("vsco_links.db")
                if entry.file_size > MAX_DATABASE_BYTES:
                    raise ValueError("Database exceeds the 512 MiB trial import limit")
                with saved.open(entry) as stream:
                    _copy_limited(stream, temporary)
            source = "artifact"
        elif seed_url:
            if urlsplit(seed_url).scheme != "https":
                raise ValueError("BOT_DB_SEED_URL must use HTTPS")
            try:
                with urllib.request.urlopen(seed_url, timeout=60) as stream:
                    if urlsplit(stream.geturl()).scheme != "https":
                        raise ValueError("Database download must remain on HTTPS")
                    _copy_limited(stream, temporary)
            except Exception:
                # Signed URLs may contain credentials: do not include them in logs.
                raise ValueError("Cannot import BOT_DB_SEED_URL; check access and URL expiry") from None
            source = "seed"
        else:
            with closing(sqlite3.connect(temporary)) as empty:
                empty.execute("PRAGMA user_version=0")
            source = "empty"
        snapshot_database(temporary, database)
        return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "snapshot"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "restore":
            if args.archive is None:
                parser.error("restore requires --archive")
            source = restore_database(args.database, args.archive, os.getenv("BOT_DB_SEED_URL", "").strip())
            print(f"Database ready (source: {source})")
        else:
            if args.output is None:
                parser.error("snapshot requires --output")
            snapshot_database(args.database, args.output)
            print("Consistent SQLite snapshot saved")
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, KeyError):
        raise SystemExit("Database operation failed; existing artifacts were not replaced") from None


if __name__ == "__main__":
    main()
