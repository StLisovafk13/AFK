"""Local auto-updating HTTP host for VSCO exports.

This module spins up a simple HTTP server that reuses the existing export
helpers from :mod:`vsco_bot` to build gallery, map and CSV exports on demand.
Every request pulls fresh data from the SQLite database, so the pages always
reflect the current state without any manual export command.

Usage::

    python local_export_server.py --port 8765 --refresh 60

Then open http://127.0.0.1:8765/ in your browser. The index page lists links
for the whole database and for each chat available in the DB. Gallery and map
pages include an auto-refresh meta tag (configurable via ``--refresh``).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from vsco_bot import (
    DB_PATH,
    build_map_images,
    build_map_users,
    build_rich_gallery,
    db_connect,
    fetch_gallery_users,
    fetch_items_for_map,
)


log = logging.getLogger(__name__)


def _inject_auto_refresh(html: str, interval: int) -> str:
    """Insert a ``<meta refresh>`` tag if *interval* is positive."""

    if interval <= 0:
        return html

    marker = "</head>"
    refresh_tag = f"  <meta http-equiv=\"refresh\" content=\"{interval}\" />\n"
    if marker in html:
        return html.replace(marker, refresh_tag + marker, 1)
    return refresh_tag + html


def _list_chat_stats() -> List[Tuple[int, int]]:
    """Return ``(chat_id, items_count)`` pairs sorted by chat id."""

    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT chat_id, COUNT(*) FROM items GROUP BY chat_id ORDER BY chat_id"
        ).fetchall()
    finally:
        conn.close()
    return [(int(chat_id), int(count)) for chat_id, count in rows]


def _render_index(refresh_interval: int) -> str:
    """Build the HTML for the index page."""

    chats = _list_chat_stats()
    index = {
        "title": "VSCO Export Host",
        "subtitle": "Автообновляемые выгрузки",
        "refresh_interval": refresh_interval,
        "chats": [
            {"chat_id": chat_id, "count": count}
            for chat_id, count in chats
        ],
    }
    html = """<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>VSCO Export Host</title>
  <style>
    body { font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; background:#f4f6fb; color:#0f172a; }
    .wrap { max-width: 960px; margin: 40px auto 80px; padding: 0 20px; }
    h1 { margin: 0 0 12px; font-size: 36px; }
    p { margin: 0 0 32px; color:#475569; }
    section { background:#fff; border-radius: 20px; padding: 24px; box-shadow:0 16px 32px rgba(15,23,42,0.12); }
    section + section { margin-top: 28px; }
    ul { list-style:none; padding:0; margin:0; display:grid; gap:12px; }
    li { display:flex; flex-direction:column; gap:6px; padding:16px; border:1px solid #e2e8f0; border-radius:16px; }
    a { color:#2563eb; font-weight:600; text-decoration:none; }
    a:hover { text-decoration:underline; }
    .count { font-size:13px; color:#64748b; }
    code { background:#e2e8f0; padding:3px 6px; border-radius:8px; font-size:12px; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>VSCO Export Host</h1>
    <p>Автообновляемые выгрузки из локальной базы. Страницы генерируются на лету при каждом запросе.</p>
    <section>
      <h2>Вся база</h2>
      <ul>
        <li>
          <a href=\"/gallery?scope=all\">📷 Галерея</a>
          <div class=\"count\">Обновляется автоматически</div>
        </li>
        <li>
          <a href=\"/map/users?scope=all\">🗺️ Карта пользователей</a>
          <div class=\"count\">Все профили с координатами</div>
        </li>
        <li>
          <a href=\"/map/images?scope=all\">🗺️ Карта фото</a>
          <div class=\"count\">Каждое фото с координатами</div>
        </li>
        <li>
          <a href=\"/csv?scope=all\">📄 CSV</a>
          <div class=\"count\">Полный список профилей</div>
        </li>
      </ul>
    </section>
    <section>
      <h2>Чаты</h2>
      <ul>
"""
    if not index["chats"]:
        html += "        <li>Пока нет данных в таблице <code>items</code>.</li>\n"
    else:
        for info in index["chats"]:
            chat_id = info["chat_id"]
            count = info["count"]
            html += (
                "        <li>\n"
                f"          <strong>Чат {chat_id}</strong>\n"
                f'          <div class="count">{count} записей</div>\n'
                f'          <div><a href="/gallery?scope=chat&chat_id={chat_id}">📷 Галерея</a></div>\n'
                f'          <div><a href="/map/users?scope=chat&chat_id={chat_id}">🗺️ Карта пользователей</a></div>\n'
                f'          <div><a href="/map/images?scope=chat&chat_id={chat_id}">🗺️ Карта фото</a></div>\n'
                f'          <div><a href="/csv?scope=chat&chat_id={chat_id}">📄 CSV</a></div>\n'
                "        </li>\n"
            )
    html += """      </ul>
    </section>
  </div>
</body>
</html>
"""
    return _inject_auto_refresh(html, refresh_interval)


def _export_scope_from_query(params: Dict[str, List[str]]) -> Tuple[str, Optional[int], Optional[str]]:
    """Parse scope/chat_id from query params."""

    scope = (params.get("scope") or ["all"])[0]
    scope = scope.lower()
    chat_id: Optional[int] = None
    error: Optional[str] = None
    if scope not in {"all", "chat"}:
        error = "Недопустимая область"
    elif scope == "chat":
        raw_chat = (params.get("chat_id") or [""])[0].strip()
        if not raw_chat:
            error = "Для области chat требуется параметр chat_id"
        else:
            try:
                chat_id = int(raw_chat)
            except ValueError:
                error = "chat_id должен быть числом"
    return scope, chat_id, error


def _generate_csv(scope: str, chat_id: Optional[int]) -> str:
    """Generate CSV data for the requested scope."""

    users = fetch_gallery_users(scope, chat_id or 0)
    if not users:
        return ""

    flat_rows = [
        {
            "username": u.get("username", ""),
            "profile_url": u.get("profile_url", ""),
            "lat": u.get("lat"),
            "lon": u.get("lon"),
            "images_count": u.get("images_count"),
            "comments_count": u.get("comments_count"),
            "comments": " | ".join(u.get("comments", [])),
            "added_by": u.get("added_by_raw", ""),
            "added_by_display": u.get("added_by", ""),
            "added_by_link": u.get("added_by_link", ""),
        }
        for u in users
    ]

    output = io.StringIO()
    if flat_rows:
        fieldnames = list(flat_rows[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in flat_rows:
            writer.writerow(row)
    return output.getvalue()


class ExportRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that exposes gallery/map/CSV exports."""

    refresh_interval: int = 0

    server_version = "VSCOExportHost/1.0"
    sys_version = ""

    def _send_bytes(
        self,
        data: bytes,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "text/plain; charset=utf-8",
        filename: Optional[str] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
        self.end_headers()
        self.wfile.write(data)

    def _send_text(
        self,
        text: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "text/html; charset=utf-8",
        filename: Optional[str] = None,
    ) -> None:
        self._send_bytes(text.encode("utf-8"), status=status, content_type=content_type, filename=filename)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        log.debug("GET %s %s", path, params)

        if path == "/":
            html = _render_index(self.refresh_interval)
            self._send_text(html)
            return

        scope, chat_id, error = _export_scope_from_query(params)
        if error:
            self._send_text(
                json.dumps({"error": error}, ensure_ascii=False),
                status=HTTPStatus.BAD_REQUEST,
                content_type="application/json; charset=utf-8",
            )
            return

        try:
            if path == "/gallery":
                html = build_rich_gallery(
                    fetch_gallery_users(scope, chat_id or 0),
                    title="VSCOLeak",
                    subtitle=("All DB" if scope == "all" else f"Chat {chat_id}"),
                )
                html = _inject_auto_refresh(html, self.refresh_interval)
                self._send_text(html)
                return

            if path == "/map/users":
                users = fetch_gallery_users(scope, chat_id or 0)
                if not users:
                    self._send_text("Нет данных для отображения", status=HTTPStatus.NO_CONTENT)
                    return
                html = build_map_users(users, title="VSCO Profiles — Users")
                html = _inject_auto_refresh(html, self.refresh_interval)
                self._send_text(html)
                return

            if path == "/map/images":
                items = fetch_items_for_map(scope, chat_id or 0)
                if not items:
                    self._send_text("Нет данных для отображения", status=HTTPStatus.NO_CONTENT)
                    return
                html = build_map_images(items, title="VSCO Profiles — Images")
                html = _inject_auto_refresh(html, self.refresh_interval)
                self._send_text(html)
                return

            if path == "/csv":
                csv_payload = _generate_csv(scope, chat_id)
                if not csv_payload:
                    self._send_text("Нет данных для экспорта", status=HTTPStatus.NO_CONTENT)
                    return
                scope_label = "all" if scope == "all" else f"chat_{chat_id}"
                filename = f"export_{scope_label}.csv"
                self._send_bytes(
                    csv_payload.encode("utf-8"),
                    content_type="text/csv; charset=utf-8",
                    filename=filename,
                )
                return

        except sqlite3.Error as err:
            log.exception("Database error during %s", path)
            self._send_text(
                json.dumps({"error": "DB error", "details": str(err)}, ensure_ascii=False),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                content_type="application/json; charset=utf-8",
            )
            return

        self._send_text(
            json.dumps({"error": "Not found"}, ensure_ascii=False),
            status=HTTPStatus.NOT_FOUND,
            content_type="application/json; charset=utf-8",
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: D401, A003 - standard hook
        """Route HTTP server logs through the module logger."""

        log.info("%s - %s", self.address_string(), format % args)


def serve(host: str, port: int, refresh: int) -> None:
    """Start the threaded HTTP server."""

    ExportRequestHandler.refresh_interval = max(0, refresh)
    server = ThreadingHTTPServer((host, port), ExportRequestHandler)
    log.info("Serving VSCO exports on http://%s:%s/ (refresh=%ss)", host, port, refresh)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping server…")
    finally:
        server.server_close()


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Auto-updating local host for VSCO exports")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument(
        "--refresh",
        type=int,
        default=60,
        help="Meta refresh interval in seconds for HTML pages (0 disables)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(DB_PATH),
        help="Path to the SQLite database (default: value from BOT_DB_PATH)",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")

    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    # Ensure DB path is set before we start serving.
    if args.db != Path(DB_PATH):
        # Override the global DB_PATH used by helper functions.
        import vsco_bot

        vsco_bot.DB_PATH = str(args.db)
        log.info("Using custom DB path: %s", args.db)

    serve(args.host, args.port, args.refresh)


if __name__ == "__main__":
    main()

