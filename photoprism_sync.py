"""Synchronize bot media with PhotoPrism imports.

This utility reads new entries from the bot SQLite database, downloads the
media files into PhotoPrism's import directory, optionally triggers
``photoprism import`` for the touched folders, and records processed items to
avoid duplicates on subsequent runs.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import sqlite3
import subprocess
import sys
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from exif_fetcher import ExifExtractionError, extract_exif_from_url
from vsco_bot import DB_PATH as DEFAULT_DB_PATH, utc_now_iso
from vsco_utils import generate_media_filename


LOGGER = logging.getLogger("photoprism_sync")


DEFAULT_IMPORT_DIR = Path.home() / "Pictures" / "Import"
SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class PendingItem:
    """Data transfer object for a media item awaiting import."""

    item_id: int
    username: str
    profile_url: str
    image_url: str
    created_at: str
    meta_json: str


def ensure_photoprism_table(conn: sqlite3.Connection) -> None:
    """Create bookkeeping table if it doesn't exist."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photoprism_files (
            item_id INTEGER PRIMARY KEY,
            local_path TEXT NOT NULL,
            size_bytes INTEGER,
            imported_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def fetch_pending_items(conn: sqlite3.Connection, limit: int) -> List[PendingItem]:
    """Return items that have not been imported to PhotoPrism yet."""

    cursor = conn.execute(
        """
        SELECT id, username, COALESCE(profile_url, '') as profile_url,
               image_url, COALESCE(created_at, '') as created_at,
               COALESCE(meta_json, '') as meta_json
        FROM items
        WHERE id NOT IN (SELECT item_id FROM photoprism_files)
        ORDER BY id ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [
        PendingItem(
            item_id=int(row[0]),
            username=str(row[1] or ""),
            profile_url=str(row[2] or ""),
            image_url=str(row[3] or ""),
            created_at=str(row[4] or ""),
            meta_json=str(row[5] or ""),
        )
        for row in cursor.fetchall()
    ]
    return rows


def safe_segment(value: str, fallback: str) -> str:
    cleaned = SAFE_SEGMENT_RE.sub("_", value.strip()) if value else ""
    return cleaned or fallback


def parse_created_date(raw: str) -> datetime:
    if raw:
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(raw)
    return datetime.utcnow()


def target_directory(base: Path, item: PendingItem, *, date_subdirs: bool) -> Path:
    username_segment = safe_segment(item.username, "unknown")
    if date_subdirs:
        dt = parse_created_date(item.created_at)
        return base / username_segment / dt.strftime("%Y-%m-%d")
    return base / username_segment


def load_meta(meta_json: str) -> Dict[str, object]:
    if not meta_json:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        data = json.loads(meta_json)
        if isinstance(data, dict):
            return data
    return {}


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def download_item(item: PendingItem, dest_dir: Path) -> Tuple[Path, Optional[int]]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = generate_media_filename(item.image_url, item.item_id)
    candidate = dest_dir / filename
    if candidate.exists():
        size_bytes = candidate.stat().st_size
        LOGGER.info("Reusing existing file %s for item %s", candidate, item.item_id)
        return candidate, size_bytes
    dest_path = ensure_unique_path(candidate)
    if dest_path != candidate:
        LOGGER.debug("Resolved filename conflict: %s -> %s", candidate, dest_path)

    referer = item.profile_url or None

    LOGGER.info("Downloading %s -> %s", item.image_url, dest_path)
    meta = extract_exif_from_url(
        item.image_url,
        referer=referer,
        download_to=dest_path,
    )
    size_bytes = meta.get("size_bytes")
    return dest_path, int(size_bytes) if isinstance(size_bytes, (int, float)) else None


def run_photoprism_import(
    photoprism_cmd: Sequence[str], folders: Iterable[Path]
) -> None:
    for folder in sorted({folder.resolve() for folder in folders}):
        LOGGER.info("Running photoprism import for %s", folder)
        subprocess.check_call([*photoprism_cmd, "import", "--path", str(folder)])


def resolve_photoprism_command(raw_cmd: Sequence[str]) -> List[str]:
    if not raw_cmd:
        raise FileNotFoundError(
            "PhotoPrism CLI executable was not specified. Use --photoprism to set it explicitly."
        )

    head = raw_cmd[0]
    explicit_path = Path(head).expanduser()
    if explicit_path.is_file():
        return [str(explicit_path), *raw_cmd[1:]]

    resolved = shutil.which(head)
    if resolved:
        return [resolved, *raw_cmd[1:]]

    raise FileNotFoundError(
        f"PhotoPrism CLI executable '{head}' was not found. "
        "Provide the full path via --photoprism or install it in PATH."
    )


def sync_photoprism(
    *,
    db_path: Path,
    import_dir: Path,
    limit: int,
    photoprism_cmd: Sequence[str],
    skip_import: bool,
    date_subdirs: bool,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        ensure_photoprism_table(conn)
        items = fetch_pending_items(conn, limit)
        if not items:
            LOGGER.info("No new items to import")
            return 0

        processed: List[Tuple[int, Path, Optional[int]]] = []
        touched_dirs: List[Path] = []

        for item in items:
            if not item.image_url:
                LOGGER.warning("Item %s has empty image URL, skipping", item.item_id)
                continue
            dest_dir = target_directory(import_dir, item, date_subdirs=date_subdirs)
            try:
                dest_path, download_size = download_item(item, dest_dir)
            except ExifExtractionError as exc:
                LOGGER.error("Failed to download %s: %s", item.image_url, exc)
                continue
            except subprocess.CalledProcessError as exc:  # pragma: no cover - safety net
                LOGGER.error("Download subprocess failed for %s: %s", item.image_url, exc)
                continue

            meta_payload = load_meta(item.meta_json)
            size_value = meta_payload.get("size_bytes")
            size_bytes: Optional[int] = None
            if isinstance(size_value, (int, float)):
                size_bytes = int(size_value)
            elif download_size is not None:
                size_bytes = download_size

            processed.append((item.item_id, dest_path, size_bytes))
            touched_dirs.append(dest_dir)

        if not processed:
            LOGGER.info("No files were downloaded, skipping import phase")
            return 0

        if not skip_import:
            resolved_cmd = resolve_photoprism_command(photoprism_cmd)
            run_photoprism_import(resolved_cmd, touched_dirs)
        else:
            LOGGER.info("Skipping photoprism import step (dry-run)")

        now = utc_now_iso()
        conn.executemany(
            "INSERT OR REPLACE INTO photoprism_files(item_id, local_path, size_bytes, imported_at)"
            " VALUES(?,?,?,?)",
            [
                (item_id, str(path), size_bytes, now)
                for item_id, path, size_bytes in processed
            ],
        )
        conn.commit()
        LOGGER.info("Recorded %s items as imported", len(processed))
        return len(processed)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import bot media into PhotoPrism")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(DEFAULT_DB_PATH),
        help="Path to the bot SQLite database",
    )
    parser.add_argument(
        "--import-dir",
        type=Path,
        default=DEFAULT_IMPORT_DIR,
        help="PhotoPrism import directory",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of items to process per run",
    )
    parser.add_argument(
        "--photoprism",
        nargs="+",
        default=["photoprism"],
        metavar="CMD",
        help=(
            "PhotoPrism CLI command (executable plus optional arguments); "
            "default: photoprism"
        ),
    )
    parser.add_argument(
        "--skip-import",
        action="store_true",
        help="Download files but do not call photoprism import",
    )
    parser.add_argument(
        "--no-date-subdirs",
        action="store_true",
        help="Do not group files by capture date, only by username",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        processed = sync_photoprism(
            db_path=args.db,
            import_dir=args.import_dir,
            limit=args.limit,
            photoprism_cmd=args.photoprism,
            skip_import=args.skip_import,
            date_subdirs=not args.no_date_subdirs,
        )
    except FileNotFoundError as exc:
        LOGGER.error("photoprism import skipped: %s", exc)
        return 3
    except subprocess.CalledProcessError as exc:
        LOGGER.error("photoprism import failed: %s", exc)
        return 2

    LOGGER.info("Finished, imported %s items", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
