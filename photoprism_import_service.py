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
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

import aiohttp
import requests

from profile_link_scanner import DEFAULT_USER_AGENT
from vsco_utils import generate_media_filename
from zip_profile import _http_simple as zip_profile_http_simple

LOGGER = logging.getLogger("photoprism_importer")

ACCEPT_HEADER = "video/*;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8"
ACCEPT_LANGUAGE_HEADER = "en-US,en;q=0.9,ru;q=0.8"


@dataclass(slots=True)
class _FallbackJob:
    item_id: int
    url: str
    dest: Path
    referer: str | None


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


def _build_headers(*, user_agent: str | None, referer: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": ACCEPT_HEADER,
        "Accept-Language": ACCEPT_LANGUAGE_HEADER,
    }
    if user_agent:
        headers["User-Agent"] = user_agent
    if referer:
        headers["Referer"] = referer
    return headers


def _download_with_requests(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    timeout: int,
    chunk_size: int = 65536,
    user_agent: str | None = None,
    referer: str | None = None,
) -> None:
    headers = _build_headers(user_agent=user_agent, referer=referer)
    with session.get(url, stream=True, timeout=timeout, headers=headers) as resp:
        resp.raise_for_status()
        dest_tmp = dest.with_suffix(dest.suffix + ".part")
        with dest_tmp.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                handle.write(chunk)
        dest_tmp.replace(dest)


def _download_with_curl(
    url: str,
    dest: Path,
    *,
    curl_bin: str,
    timeout: int,
    referer: str | None,
    user_agent: str | None,
) -> None:
    dest_tmp = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        curl_bin,
        "-fL",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout * 2),
        "--http1.1",
        url,
        "-o",
        str(dest_tmp),
    ]
    if referer:
        cmd.extend(["-H", f"Referer: {referer}"])
    if user_agent:
        cmd.extend(["-H", f"User-Agent: {user_agent}"])
    cmd.extend([
        "-H",
        f"Accept: {ACCEPT_HEADER}",
        "-H",
        f"Accept-Language: {ACCEPT_LANGUAGE_HEADER}",
    ])
    LOGGER.debug("Running curl: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    dest_tmp.replace(dest)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


async def _download_batch_with_zip_helper(
    jobs: Sequence[_FallbackJob],
    *,
    timeout: int,
    user_agent: str | None,
) -> dict[int, tuple[bool, str | None]]:
    """Download items via :mod:`zip_profile` session helper to reuse cookies."""

    if not jobs:
        return {}

    ua = user_agent or DEFAULT_USER_AGENT
    results: dict[int, tuple[bool, str | None]] = {}
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    primed: set[str] = set()

    async with zip_profile_http_simple(headers={"User-Agent": ua}) as session:
        for job in jobs:
            headers = {
                "Accept": ACCEPT_HEADER,
                "Accept-Language": ACCEPT_LANGUAGE_HEADER,
            }
            referer = (job.referer or "").strip()
            if referer:
                headers["Referer"] = referer
                if referer not in primed:
                    try:
                        await session.get(
                            referer,
                            allow_redirects=True,
                            timeout=timeout_cfg,
                        )
                    except Exception as exc:  # pragma: no cover - best effort
                        LOGGER.debug("Failed to warm referer %s: %s", referer, exc)
                    primed.add(referer)

            try:
                async with session.get(
                    job.url,
                    allow_redirects=True,
                    timeout=timeout_cfg,
                    headers=headers,
                ) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.read()
            except Exception as exc:  # pragma: no cover - network failures
                results[job.item_id] = (False, str(exc))
                continue

            dest_tmp = job.dest.with_suffix(job.dest.suffix + ".part")
            try:
                job.dest.parent.mkdir(parents=True, exist_ok=True)
                dest_tmp.write_bytes(data)
                dest_tmp.replace(job.dest)
                results[job.item_id] = (True, None)
            except Exception as exc:  # pragma: no cover - filesystem errors
                results[job.item_id] = (False, str(exc))

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
) -> None:
    session: requests.Session | None = None
    if not use_curl:
        session = requests.Session()
        session.headers.update({"Accept-Language": ACCEPT_LANGUAGE_HEADER})

    try:
        primed_referers: set[str] = set()
        fallback_jobs: list[_FallbackJob] = []
        for idx, row in enumerate(items, start=1):
            item_id = int(row["id"])
            url = row["image_url"]
            filename = generate_media_filename(url, item_id)
            dest_path = dest_dir / filename
            _ensure_parent(dest_path)

            referer = (row["profile_url"] or "").strip() or (default_referer or None)
            if referer and not referer.startswith("http"):
                referer = default_referer or None

            LOGGER.info("[%s/%s] Downloading %s -> %s", idx, len(items), url, dest_path)
            try:
                if use_curl:
                    _download_with_curl(
                        url,
                        dest_path,
                        curl_bin=curl_bin,
                        timeout=timeout,
                        referer=referer,
                        user_agent=user_agent,
                    )
                else:
                    assert session is not None
                    if referer and referer not in primed_referers:
                        try:
                            resp = session.get(
                                referer,
                                timeout=timeout,
                                headers=_build_headers(
                                    user_agent=user_agent,
                                    referer=referer,
                                ),
                            )
                            resp.close()
                        except Exception as exc:  # pragma: no cover - soft failure
                            LOGGER.debug("Failed to warm referer %s: %s", referer, exc)
                        primed_referers.add(referer)
                    _download_with_requests(
                        session,
                        url,
                        dest_path,
                        timeout=timeout,
                        user_agent=user_agent,
                        referer=referer,
                    )
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else None
                if status == 403:
                    LOGGER.warning(
                        "Item %s hit HTTP 403, scheduling zip_profile fallback", item_id
                    )
                    fallback_jobs.append(
                        _FallbackJob(
                            item_id=item_id,
                            url=url,
                            dest=dest_path,
                            referer=referer,
                        )
                    )
                    continue
                LOGGER.exception("Failed to download item %s: %s", item_id, exc)
                _mark_failure(conn, item_id, error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Failed to download item %s: %s", item_id, exc)
                _mark_failure(conn, item_id, error=str(exc))
                continue

            _mark_success(conn, item_id, path=dest_path)

        if fallback_jobs:
            LOGGER.info(
                "Running zip_profile fallback for %s item(s)", len(fallback_jobs)
            )
            fallback_results = asyncio.run(
                _download_batch_with_zip_helper(
                    fallback_jobs,
                    timeout=timeout,
                    user_agent=user_agent,
                )
            )
            for job in fallback_jobs:
                ok, error = fallback_results.get(job.item_id, (False, "no result"))
                if ok:
                    _mark_success(conn, job.item_id, path=job.dest)
                else:
                    LOGGER.error(
                        "Fallback downloader failed for item %s: %s",
                        job.item_id,
                        error,
                    )
                    _mark_failure(conn, job.item_id, error=error or "fallback failed")
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
        default=DEFAULT_USER_AGENT,
        help="Optional custom User-Agent for HTTP requests",
    )
    parser.add_argument(
        "--default-referer",
        default="https://vsco.co/",
        help="Fallback Referer header when an item lacks profile_url",
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
            )

            if args.once:
                break

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
