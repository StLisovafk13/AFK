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
import hashlib
import hmac
import html
import itertools
import io
import json
import logging
import os
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

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


def _render_login_page(next_url: str, error: Optional[str] = None) -> str:
    """Render a minimalistic login form."""

    if not next_url.startswith("/"):
        next_url = "/"
    escaped_next = html.escape(next_url, quote=True)
    error_block = ""
    if error:
        error_block = (
            "      <div class=\"error\">"
            f"{html.escape(error, quote=False)}"
            "</div>\n"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>VSCO Export Host — Вход</title>
  <style>
    body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; background:#0f172a; color:#0f172a; }}
    .wrap {{ width: min(420px, 100%); margin: 80px auto; background:#fff; border-radius:20px; padding:32px; box-shadow:0 18px 40px rgba(15,23,42,0.3); }}
    h1 {{ margin:0 0 16px; font-size: 28px; }}
    p {{ margin:0 0 24px; color:#475569; }}
    label {{ display:block; font-weight:600; margin-bottom:6px; }}
    input[type=text], input[type=password] {{ width:100%; padding:12px 14px; border-radius:12px; border:1px solid #cbd5f5; font-size:16px; box-sizing:border-box; }}
    input[type=text]:focus, input[type=password]:focus {{ border-color:#6366f1; outline:none; box-shadow:0 0 0 3px rgba(99,102,241,0.25); }}
    button {{ width:100%; margin-top:24px; padding:14px; font-size:16px; border-radius:14px; border:0; background:#4f46e5; color:#fff; font-weight:600; cursor:pointer; }}
    button:hover {{ background:#4338ca; }}
    .error {{ margin-top:16px; padding:12px; background:#fee2e2; color:#b91c1c; border-radius:12px; font-size:14px; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>VSCO Export Host</h1>
    <p>Пожалуйста, войдите, чтобы продолжить.</p>
    <form method=\"post\" action=\"/login\" autocomplete=\"off\">
      <input type=\"hidden\" name=\"next\" value=\"{escaped_next}\" />
      <label for=\"username\">Логин</label>
      <input id=\"username\" name=\"username\" type=\"text\" required autofocus />
      <label for=\"password\">Пароль</label>
      <input id=\"password\" name=\"password\" type=\"password\" required />
      <button type=\"submit\">Войти</button>
{error_block}    </form>
  </div>
</body>
</html>
"""


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

    users_iter = iter(fetch_gallery_users(scope, chat_id or 0))
    try:
        first_user = next(users_iter)
    except StopIteration:
        return ""

    output = io.StringIO()
    writer: Optional[csv.DictWriter[str]] = None
    for user in itertools.chain([first_user], users_iter):
        row = {
            "username": user.get("username", ""),
            "profile_url": user.get("profile_url", ""),
            "lat": user.get("lat"),
            "lon": user.get("lon"),
            "images_count": user.get("images_count"),
            "comments_count": user.get("comments_count"),
            "comments": " | ".join(user.get("comments", [])),
            "added_by": user.get("added_by_raw", ""),
            "added_by_display": user.get("added_by", ""),
            "added_by_link": user.get("added_by_link", ""),
        }
        if writer is None:
            fieldnames = list(row.keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
        writer.writerow(row)
    return output.getvalue()


class ExportRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that exposes gallery/map/CSV exports."""

    refresh_interval: int = 0
    auth_manager: Optional["AuthManager"] = None

    server_version = "VSCOExportHost/1.0"
    sys_version = ""

    _session_cookie_name = "vsco_session"

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

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    def _get_cookie_token(self) -> Optional[str]:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except (TypeError, ValueError):
            return None
        morsel = cookie.get(self._session_cookie_name)
        if morsel:
            return morsel.value
        return None

    def _expire_session_cookie(self) -> SimpleCookie:
        cookie = SimpleCookie()
        cookie[self._session_cookie_name] = ""
        cookie[self._session_cookie_name]["path"] = "/"
        cookie[self._session_cookie_name]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
        cookie[self._session_cookie_name]["httponly"] = True
        return cookie

    def _redirect(
        self,
        location: str,
        *,
        cookie: Optional[SimpleCookie] = None,
        status: HTTPStatus = HTTPStatus.SEE_OTHER,
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        if cookie is not None:
            for morsel in cookie.values():
                self.send_header("Set-Cookie", morsel.OutputString())
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_to_login(self, *, next_path: Optional[str] = None, clear_cookie: bool = False) -> None:
        next_path = next_path or "/"
        if not next_path.startswith("/"):
            next_path = "/"
        params = ""
        if next_path not in {"/", ""}:
            params = "?" + urlencode({"next": next_path})
        cookie = self._expire_session_cookie() if clear_cookie else None
        self._redirect(f"/login{params}", cookie=cookie)

    def _ensure_authenticated(self) -> bool:
        auth = self.auth_manager
        if auth is None:
            return True
        token = self._get_cookie_token()
        if token and auth.validate(token):
            return True
        if token:
            auth.invalidate(token)
        self._redirect_to_login(next_path=self.path, clear_cookie=True)
        return False

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        log.debug("GET %s %s", path, params)

        if path == "/login":
            if self.auth_manager is None:
                self._send_text("Авторизация отключена", status=HTTPStatus.NOT_FOUND)
                return
            next_target = (params.get("next") or ["/"])[0] or "/"
            if not next_target.startswith("/"):
                next_target = "/"
            token = self._get_cookie_token()
            if token:
                if self.auth_manager.validate(token):
                    self._redirect(next_target)
                    return
                self.auth_manager.invalidate(token)
                suffix = ""
                if next_target not in {"/", ""}:
                    suffix = "?" + urlencode({"next": next_target})
                self._redirect("/login" + suffix, cookie=self._expire_session_cookie())
                return
            html = _render_login_page(next_target)
            self._send_text(html)
            return

        if path == "/logout":
            if self.auth_manager is None:
                self._redirect("/")
                return
            token = self._get_cookie_token()
            if token:
                self.auth_manager.invalidate(token)
            self._redirect("/login", cookie=self._expire_session_cookie())
            return

        if not self._ensure_authenticated():
            return

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
                iterator = iter(fetch_gallery_users(scope, chat_id or 0))
                try:
                    first_user = next(iterator)
                except StopIteration:
                    self._send_text("Нет данных для отображения", status=HTTPStatus.NO_CONTENT)
                    return
                users = [first_user]
                users.extend(iterator)
                html = build_rich_gallery(
                    users,
                    title="VSCOLeak",
                    subtitle=("All DB" if scope == "all" else f"Chat {chat_id}"),
                )
                html = _inject_auto_refresh(html, self.refresh_interval)
                self._send_text(html)
                return

            if path == "/map/users":
                iterator = iter(fetch_gallery_users(scope, chat_id or 0))
                try:
                    first_user = next(iterator)
                except StopIteration:
                    self._send_text("Нет данных для отображения", status=HTTPStatus.NO_CONTENT)
                    return
                users = [first_user]
                users.extend(iterator)
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

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path != "/login" or self.auth_manager is None:
            self._send_text(
                json.dumps({"error": "Not found"}, ensure_ascii=False),
                status=HTTPStatus.NOT_FOUND,
                content_type="application/json; charset=utf-8",
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        data = parse_qs(body)
        username = (data.get("username") or [""])[0]
        password = (data.get("password") or [""])[0]
        next_target = (data.get("next") or ["/"])[0] or "/"
        if not next_target.startswith("/"):
            next_target = "/"

        if self.auth_manager.check_credentials(username, password):
            token = self.auth_manager.create_session()
            cookie = SimpleCookie()
            cookie[self._session_cookie_name] = token
            cookie[self._session_cookie_name]["path"] = "/"
            cookie[self._session_cookie_name]["httponly"] = True
            if self.auth_manager.session_ttl:
                cookie[self._session_cookie_name]["max-age"] = str(self.auth_manager.session_ttl)
            self._redirect(next_target, cookie=cookie)
            return

        html = _render_login_page(next_target, error="Неверный логин или пароль")
        self._send_text(html, status=HTTPStatus.UNAUTHORIZED)

    def log_message(self, format: str, *args: object) -> None:  # noqa: D401, A003 - standard hook
        """Route HTTP server logs through the module logger."""

        log.info("%s - %s", self.address_string(), format % args)


class AuthManager:
    """In-memory session manager for simple username/password auth."""

    def __init__(self, username: str, password: str, session_ttl: int = 3600) -> None:
        self.username = username
        self.session_ttl = max(0, session_ttl)
        self._salt = secrets.token_bytes(16)
        self._password_hash = self._hash_password(password)
        self._sessions: Dict[str, float] = {}
        self._lock = RLock()

    def _hash_password(self, password: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            self._salt,
            390000,
        )

    def check_credentials(self, username: str, password: str) -> bool:
        if username != self.username:
            return False
        candidate = self._hash_password(password)
        return hmac.compare_digest(candidate, self._password_hash)

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + self.session_ttl if self.session_ttl else now
        with self._lock:
            self._purge_expired_locked(now)
            self._sessions[token] = expires
        return token

    def validate(self, token: Optional[str]) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if self.session_ttl and expires < now:
                self._sessions.pop(token, None)
                return False
            if self.session_ttl:
                self._sessions[token] = now + self.session_ttl
            return True

    def invalidate(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_expired_locked(self, now: float) -> None:
        if not self.session_ttl:
            return
        to_remove = [tok for tok, expires in self._sessions.items() if expires < now]
        for tok in to_remove:
            self._sessions.pop(tok, None)


def serve(host: str, port: int, refresh: int, auth: Optional[AuthManager]) -> None:
    """Start the threaded HTTP server."""

    ExportRequestHandler.refresh_interval = max(0, refresh)
    ExportRequestHandler.auth_manager = auth
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
    parser.add_argument(
        "--auth-username",
        help=(
            "Username required to access the server. "
            "Can also be provided via VSCO_HOST_USERNAME."
        ),
    )
    parser.add_argument(
        "--auth-password",
        help=(
            "Password required to access the server. "
            "Can also be provided via VSCO_HOST_PASSWORD."
        ),
    )
    parser.add_argument(
        "--session-ttl",
        type=int,
        default=3600,
        help="Session lifetime in seconds (default: 3600). 0 disables expiry.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    # Ensure DB path is set before we start serving.
    if args.db != Path(DB_PATH):
        # Override the global DB_PATH used by helper functions.
        import vsco_bot

        vsco_bot.DB_PATH = str(args.db)
        log.info("Using custom DB path: %s", args.db)

    username = args.auth_username or os.environ.get("VSCO_HOST_USERNAME")
    password = args.auth_password or os.environ.get("VSCO_HOST_PASSWORD")

    auth_manager: Optional[AuthManager]
    if username and password:
        auth_manager = AuthManager(username=username, password=password, session_ttl=args.session_ttl)
    else:
        log.warning(
            "Authentication credentials not fully provided; the server will run without authorization"
        )
        auth_manager = None

    serve(args.host, args.port, args.refresh, auth_manager)


if __name__ == "__main__":
    main()

