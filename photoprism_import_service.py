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
import asyncio
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
from exif_fetcher import DEFAULT_HTTP_HEADERS, build_curl_command

LOGGER = logging.getLogger("photoprism_importer")


def ensure_import_columns(conn: sqlite3.Connection) -> None:
    """Ensure auxiliary progress columns are present on the ``items`` table."""

    has_items = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    if not has_items:
        raise RuntimeError(
            "items table not found – initialise the VSCO bot database before running the "
            "PhotoPrism import service"
        )

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
        SELECT id, image_url, profile_url
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


class DownloadTask:
    """Normalized unit of work for HTTP/Playwright downloaders."""

    __slots__ = ("item_id", "url", "dest", "referer")

    def __init__(self, item_id: int, url: str, dest: Path, referer: str | None):
        self.item_id = item_id
        self.url = url
        self.dest = dest
        self.referer = referer


BASE_HEADERS = dict(DEFAULT_HTTP_HEADERS)
BASE_HEADERS.setdefault(
    "Accept", "video/*;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8"
)
BASE_HEADERS.setdefault("Accept-Language", "en-US,en;q=0.9,ru;q=0.8")


def _download_with_requests(
    session: requests.Session,
    task: DownloadTask,
    *,
    timeout: int,
    chunk_size: int = 65536,
    user_agent: str | None = None,
) -> None:
    headers = dict(BASE_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent
    if task.referer:
        headers["Referer"] = task.referer

    with session.get(task.url, stream=True, timeout=timeout, headers=headers) as resp:
        resp.raise_for_status()
        dest_tmp = task.dest.with_suffix(task.dest.suffix + ".part")
        with dest_tmp.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                handle.write(chunk)
        dest_tmp.replace(task.dest)


def _download_with_curl(
    task: DownloadTask,
    *,
    curl_bin: str,
    timeout: int,
    user_agent: str | None,
) -> None:
    dest_tmp = task.dest.with_suffix(task.dest.suffix + ".part")
    headers = dict(BASE_HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent
    cmd = build_curl_command(
        task.url,
        referer=task.referer,
        timeout=timeout,
        headers=headers,
        curl_bin=curl_bin,
    )
    cmd.extend(["-o", str(dest_tmp)])

    LOGGER.debug("Running curl: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    dest_tmp.replace(task.dest)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _build_tasks(
    items: Sequence[sqlite3.Row],
    *,
    dest_dir: Path,
    default_referer: str | None,
) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for row in items:
        item_id = int(row["id"])
        url = row["image_url"]
        filename = generate_media_filename(url, item_id)
        dest_path = dest_dir / filename
        _ensure_parent(dest_path)

        referer = (row["profile_url"] or "").strip() or (default_referer or None)
        if referer and not referer.startswith("http"):
            referer = default_referer or None

        tasks.append(DownloadTask(item_id, url, dest_path, referer))

    return tasks


def _process_batch_http(
    conn: sqlite3.Connection,
    tasks: Sequence[DownloadTask],
    *,
    use_curl: bool,
    curl_bin: str,
    timeout: int,
    user_agent: str | None,
) -> None:
    session: requests.Session | None = None
    if not use_curl:
        session = requests.Session()

    try:
        for idx, task in enumerate(tasks, start=1):
            LOGGER.info(
                "[%s/%s] Downloading %s -> %s",
                idx,
                len(tasks),
                task.url,
                task.dest,
            )
            try:
                if use_curl:
                    _download_with_curl(
                        task,
                        curl_bin=curl_bin,
                        timeout=timeout,
                        user_agent=user_agent,
                    )
                else:
                    assert session is not None
                    _download_with_requests(
                        session,
                        task,
                        timeout=timeout,
                        user_agent=user_agent,
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Failed to download item %s: %s", task.item_id, exc)
                _mark_failure(conn, task.item_id, error=str(exc))
                continue

            _mark_success(conn, task.item_id, path=task.dest)
    finally:
        if session is not None:
            session.close()


async def _download_batch_playwright(
    tasks: Sequence[DownloadTask],
    *,
    timeout: int,
    user_agent: str | None,
    concurrency: int,
) -> list[tuple[DownloadTask, Exception | None]]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "Playwright is not available. Install it or run without --use-playwright"
        ) from exc

    results: list[tuple[DownloadTask, Exception | None]] = []

    total = len(tasks)

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context_kwargs = {}
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        context = await browser.new_context(**context_kwargs)

        headers_base = dict(BASE_HEADERS)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def fetch(idx: int, task: DownloadTask) -> None:
            async with sem:
                LOGGER.info(
                    "[%s/%s] Downloading %s -> %s (Playwright)",
                    idx,
                    total,
                    task.url,
                    task.dest,
                )
                headers = dict(headers_base)
                if task.referer:
                    headers["Referer"] = task.referer
                try:
                    response = await context.request.get(
                        task.url,
                        timeout=timeout * 1000,
                        headers=headers,
                    )
                    status = response.status
                    if status >= 400:
                        raise RuntimeError(f"HTTP {status}")
                    body = await response.body()
                    if not body:
                        raise RuntimeError("empty body")
                    dest_tmp = task.dest.with_suffix(task.dest.suffix + ".part")
                    dest_tmp.write_bytes(body)
                    dest_tmp.replace(task.dest)
                    results.append((task, None))
                except Exception as exc:  # noqa: BLE001
                    results.append((task, exc))

        await asyncio.gather(*(fetch(idx, task) for idx, task in enumerate(tasks, 1)))

        await context.close()
        await browser.close()

    return results


def process_batch(
    conn: sqlite3.Connection,
    items: Sequence[sqlite3.Row],
    *,
    dest_dir: Path,
    use_curl: bool,
    curl_bin: str,
    timeout: int,
    user_agent: str | None,
    default_referer: str | None,
    use_playwright: bool,
    playwright_concurrency: int,
) -> None:
    tasks = _build_tasks(
        items,
        dest_dir=dest_dir,
        default_referer=default_referer,
    )

    if not tasks:
        return

    if use_playwright:
        LOGGER.debug(
            "Downloading %d item(s) via Playwright request context", len(tasks)
        )
        results = asyncio.run(
            _download_batch_playwright(
                tasks,
                timeout=timeout,
                user_agent=user_agent,
                concurrency=playwright_concurrency,
            )
        )
        for task, error in results:
            if error is None:
                _mark_success(conn, task.item_id, path=task.dest)
            else:
                LOGGER.warning(
                    "Playwright download failed for %s (item %s): %s",
                    task.url,
                    task.item_id,
                    error,
                )
                _mark_failure(conn, task.item_id, error=str(error))
        return

    _process_batch_http(
        conn,
        tasks,
        use_curl=use_curl,
        curl_bin=curl_bin,
        timeout=timeout,
        user_agent=user_agent,
    )


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
        help="Optional custom User-Agent for HTTP/Playwright downloads",
    )
    parser.add_argument(
        "--default-referer",
        default="https://vsco.co/",
        help="Fallback Referer header when an item lacks profile_url",
    )
    parser.add_argument(
        "--use-playwright",
        action="store_true",
        help="Download files via Playwright request context (best VSCO compatibility)",
    )
    parser.add_argument(
        "--playwright-concurrency",
        type=int,
        default=3,
        help="Maximum simultaneous Playwright downloads when --use-playwright is set",
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
        try:
            ensure_import_columns(conn)
        except RuntimeError as exc:
            LOGGER.error("%s", exc)
            return 1

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
                default_referer=args.default_referer or None,
                use_playwright=args.use_playwright,
                playwright_concurrency=max(1, args.playwright_concurrency),
            )

            if args.once:
                break

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
