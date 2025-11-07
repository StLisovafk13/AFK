"""Small PhotoPrism ingest helper for VSCO bot databases.

The script polls the SQLite database used by the bot for records that have
not yet been imported into PhotoPrism (``items.imported_at IS NULL``).  Each
pending item is downloaded into a destination directory that is visible to
PhotoPrism – for example, the rclone-mounted originals folder.  Progress is
tracked in two extra columns: ``items.import_progress`` (``pending`` /
``downloading`` / ``done`` / ``error``) and ``items.import_error``.

Usage example::

    python photoprism_import_service.py \
        --db vsco_links.db \
        --dest /mnt/photoprism-originals/vsco \
        --batch-size 20 \
        --poll-interval 60

The script can run either once (``--once``) or as a long-lived service that
polls the database every ``--poll-interval`` seconds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Sequence

import requests

from vsco_utils import generate_media_filename

LOGGER = logging.getLogger("photoprism_importer")


def ensure_import_columns(conn: sqlite3.Connection) -> None:
    """Ensure auxiliary progress columns are present on the ``items`` table."""

    existing = {row[1] for row in conn.execute("PRAGMA table_info(items)")}

    if "import_progress" not in existing:
        conn.execute(
            "ALTER TABLE items ADD COLUMN import_progress TEXT DEFAULT ''"
        )
    if "import_error" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN import_error TEXT DEFAULT ''")
    if "import_path" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN import_path TEXT DEFAULT ''")
    if "imported_at" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN imported_at TEXT")

    conn.execute(
        """
        UPDATE items
           SET import_progress='pending'
         WHERE imported_at IS NULL
           AND COALESCE(import_progress, '') = ''
        """
    )
    conn.commit()


def _claim_items(
    conn: sqlite3.Connection, *, batch_size: int
) -> List[sqlite3.Row]:
    """Select a batch of pending items and mark them as ``downloading``."""

    rows = conn.execute(
        """
        SELECT id, image_url
        FROM items
        WHERE imported_at IS NULL
          AND COALESCE(import_progress, '') NOT IN ('downloading')
        ORDER BY id
        LIMIT ?
        """,
        (batch_size,),
    ).fetchall()

    for row in rows:
        conn.execute(
            """
            UPDATE items
               SET import_progress='downloading', import_error=''
             WHERE id=? AND imported_at IS NULL
            """,
            (row["id"],),
        )

    conn.commit()
    return rows


def _mark_success(
    conn: sqlite3.Connection, item_id: int, *, path: Path
) -> None:
    conn.execute(
        """
        UPDATE items
           SET import_progress='done',
               import_error='',
               import_path=?,
               imported_at=?
         WHERE id=?
        """,
        (str(path), dt.datetime.utcnow().isoformat(timespec="seconds"), item_id),
    )
    conn.commit()


def _mark_failure(
    conn: sqlite3.Connection, item_id: int, *, error: str
) -> None:
    conn.execute(
        """
        UPDATE items
           SET import_progress='error',
               import_error=?
         WHERE id=?
        """,
        (error[:400], item_id),
    )
    conn.commit()


def _download_with_requests(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    timeout: int,
    chunk_size: int = 65536,
    user_agent: str | None = None,
) -> None:
    headers = {"User-Agent": user_agent} if user_agent else None
    with session.get(url, stream=True, timeout=timeout, headers=headers) as resp:
        resp.raise_for_status()
        dest_tmp = dest.with_suffix(dest.suffix + ".part")
        with dest_tmp.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                handle.write(chunk)
        dest_tmp.replace(dest)


def _download_with_curl(url: str, dest: Path, *, curl_bin: str, timeout: int) -> None:
    dest_tmp = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        curl_bin,
        "-fL",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout * 2),
        url,
        "-o",
        str(dest_tmp),
    ]
    LOGGER.debug("Running curl: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    dest_tmp.replace(dest)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def process_batch(
    conn: sqlite3.Connection,
    items: Sequence[sqlite3.Row],
    *,
    dest_dir: Path,
    use_curl: bool,
    curl_bin: str,
    timeout: int,
    user_agent: str | None,
) -> None:
    session: requests.Session | None = None
    if not use_curl:
        session = requests.Session()

    try:
        for idx, row in enumerate(items, start=1):
            item_id = int(row["id"])
            url = row["image_url"]
            filename = generate_media_filename(url, item_id)
            dest_path = dest_dir / filename
            _ensure_parent(dest_path)

            LOGGER.info("[%s/%s] Downloading %s -> %s", idx, len(items), url, dest_path)
            try:
                if use_curl:
                    _download_with_curl(url, dest_path, curl_bin=curl_bin, timeout=timeout)
                else:
                    assert session is not None
                    _download_with_requests(
                        session, url, dest_path, timeout=timeout, user_agent=user_agent
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Failed to download item %s: %s", item_id, exc)
                _mark_failure(conn, item_id, error=str(exc))
                continue

            _mark_success(conn, item_id, path=dest_path)
    finally:
        if session is not None:
            session.close()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the VSCO bot SQLite database")
    parser.add_argument(
        "--dest",
        required=True,
        help="Destination directory visible to PhotoPrism (mounted originals)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="How many pending items to process per iteration",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=60,
        help="Seconds to sleep between iterations when running continuously",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Network timeout for individual downloads",
    )
    parser.add_argument(
        "--user-agent",
        default="",
        help="Optional custom User-Agent for HTTP requests",
    )
    parser.add_argument(
        "--use-curl",
        action="store_true",
        help="Download files through the curl binary instead of requests",
    )
    parser.add_argument(
        "--curl-bin",
        default="curl",
        help="Path to the curl binary (used when --use-curl is set)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process a single batch and exit instead of running continuously",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_import_columns(conn)

        while True:
            items = _claim_items(conn, batch_size=args.batch_size)
            if not items:
                LOGGER.debug("No pending items found")
                if args.once:
                    break
                time.sleep(args.poll_interval)
                continue

            process_batch(
                conn,
                items,
                dest_dir=dest_dir,
                use_curl=args.use_curl,
                curl_bin=args.curl_bin,
                timeout=args.timeout,
                user_agent=args.user_agent or None,
            )

            if args.once:
                break

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
