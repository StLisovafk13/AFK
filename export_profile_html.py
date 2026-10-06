#!/usr/bin/env python3
"""Generate a standalone HTML export for a VSCO profile."""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from html_utils import json_for_html

DEFAULT_DB_PATH = os.environ.get("BOT_DB_PATH", "vsco_links.db")


@dataclass
class PhotoEntry:
    """A single photo row with optional metadata."""

    id: int
    url: str
    created_at: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    comments: List[str]
    size_bytes: Optional[int]
    camera: Optional[str]
    city: Optional[str]


@dataclass
class ProfilePayload:
    """Aggregated data for a profile export."""

    username: str
    profile_url: str
    photos: List[PhotoEntry]

    @property
    def photos_with_coords(self) -> List[PhotoEntry]:
        return [p for p in self.photos if p.lat is not None and p.lon is not None]


def _normalize_username(raw: str) -> str:
    trimmed = (raw or "").strip()
    if trimmed.startswith("@"):  # Telegram-style mention
        trimmed = trimmed[1:]
    return trimmed


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_coord_pair(lat_raw: Any, lon_raw: Any) -> Optional[Tuple[float, float]]:
    lat = _safe_float(lat_raw)
    lon = _safe_float(lon_raw)
    if lat is None or lon is None:
        return None
    return lat, lon


def _apply_gps_ref(value: float, ref: Any, positive_refs: Tuple[str, ...]) -> float:
    ref_str = str(ref or "").strip().upper()
    if not ref_str:
        return value
    if ref_str in {item.upper() for item in positive_refs}:
        return abs(value)
    return -abs(value)


def _extract_coords_from_dict(data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    direct_pairs = [("lat", "lon"), ("latitude", "longitude"), ("Latitude", "Longitude")]
    for lat_key, lon_key in direct_pairs:
        lat = _safe_float(data.get(lat_key))
        lon = _safe_float(data.get(lon_key))
        if lat is not None and lon is not None:
            return lat, lon

    lat = _safe_float(data.get("GPSLatitude"))
    lon = _safe_float(data.get("GPSLongitude"))
    if lat is not None and lon is not None:
        lat = _apply_gps_ref(lat, data.get("GPSLatitudeRef") or data.get("LatitudeRef"), ("N",))
        lon = _apply_gps_ref(lon, data.get("GPSLongitudeRef") or data.get("LongitudeRef"), ("E",))
        return lat, lon

    lat = _safe_float(data.get("GeoLatitude"))
    lon = _safe_float(data.get("GeoLongitude"))
    if lat is not None and lon is not None:
        return lat, lon

    gps_position = data.get("GPSPosition")
    if isinstance(gps_position, str):
        parts = [chunk for chunk in gps_position.replace(",", " ").split() if chunk]
        if len(parts) >= 2:
            lat = _safe_float(parts[0])
            lon = _safe_float(parts[1])
            if lat is not None and lon is not None:
                return lat, lon

    return None


def extract_coordinates_from_meta(meta: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    to_visit: List[Any] = [meta]
    seen: set[int] = set()
    while to_visit:
        current = to_visit.pop()
        if isinstance(current, dict):
            obj_id = id(current)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            coords = _extract_coords_from_dict(current)
            if coords:
                return coords
            for value in current.values():
                if isinstance(value, dict):
                    to_visit.append(value)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, dict):
                            to_visit.append(item)
        elif isinstance(current, (list, tuple)):
            for item in current:
                if isinstance(item, dict):
                    to_visit.append(item)
    return None


def _load_comments(conn: sqlite3.Connection, item_ids: List[int]) -> Dict[int, List[str]]:
    if not item_ids:
        return {}
    try:
        placeholders = ",".join("?" for _ in item_ids)
        query = (
            "SELECT c.item_id, c.comment FROM comments AS c"
            " JOIN items AS i ON i.id=c.item_id AND i.chat_id=c.chat_id"
            f" WHERE c.item_id IN ({placeholders}) ORDER BY c.id"
        )
        rows = conn.execute(query, item_ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    comments: Dict[int, List[str]] = {}
    for item_id, comment in rows:
        comments.setdefault(int(item_id), []).append(comment or "")
    return comments


def _pick_camera_label(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(meta, dict):
        return None
    for key in ("Model", "DeviceModel", "CameraModelName"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def _decode_meta(meta_json: Any) -> Optional[Dict[str, Any]]:
    if not meta_json:
        return None
    if isinstance(meta_json, dict):
        return meta_json
    try:
        decoded = json.loads(meta_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def load_profile(conn: sqlite3.Connection, username: str, *, chat_id: Optional[int] = None) -> ProfilePayload:
    normalized = _normalize_username(username)
    if not normalized:
        raise ValueError("Username must be provided")

    where_clause = "LOWER(COALESCE(username,'')) = ?"
    params: List[Any] = [normalized.lower()]
    if chat_id is not None:
        where_clause += " AND chat_id = ?"
        params.append(int(chat_id))

    query = (
        "SELECT id, username, profile_url, image_url, latitude, longitude, meta_json, created_at, added_by "
        "FROM items WHERE " + where_clause + " ORDER BY created_at ASC, id ASC"
    )
    rows = conn.execute(query, params).fetchall()
    if not rows:
        raise LookupError(f"Profile '{normalized}' not found in database")

    item_ids = [int(row[0]) for row in rows]
    comments_map = _load_comments(conn, item_ids)

    photos: List[PhotoEntry] = []
    detected_username = None
    profile_url = ""

    for row in rows:
        (
            item_id,
            raw_username,
            raw_profile_url,
            image_url,
            lat_raw,
            lon_raw,
            meta_json,
            created_at,
            _added_by,
        ) = row

        detected_username = detected_username or (raw_username or normalized)
        if not profile_url and raw_profile_url:
            profile_url = raw_profile_url

        decoded_meta = _decode_meta(meta_json)
        coords = extract_coordinates_from_meta(decoded_meta) if decoded_meta else None
        if coords is None:
            coords = _safe_coord_pair(lat_raw, lon_raw)

        lat = coords[0] if coords else None
        lon = coords[1] if coords else None
        camera = _pick_camera_label(decoded_meta)
        size_bytes = None
        if isinstance(decoded_meta, dict):
            size_value = decoded_meta.get("size_bytes")
            if isinstance(size_value, (int, float)):
                size_bytes = int(size_value)
            elif isinstance(size_value, str) and size_value.isdigit():
                size_bytes = int(size_value)
        city = None
        if isinstance(decoded_meta, dict):
            city_value = decoded_meta.get("city") or decoded_meta.get("City")
            if isinstance(city_value, str) and city_value.strip():
                city = " ".join(city_value.split())

        photos.append(
            PhotoEntry(
                id=int(item_id),
                url=image_url or "",
                created_at=created_at,
                lat=lat,
                lon=lon,
                comments=comments_map.get(int(item_id), []),
                size_bytes=size_bytes,
                camera=camera,
                city=city,
            )
        )

    final_username = detected_username or normalized
    profile_url = profile_url or f"https://vsco.co/{final_username}/gallery"
    return ProfilePayload(username=final_username, profile_url=profile_url, photos=photos)


def _human_size(num: Optional[int]) -> str:
    if not isinstance(num, int) or num <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{num} B"
        num /= 1024
    return f"{num:.2f} TB"


def build_profile_html(profile: ProfilePayload) -> str:
    photos = profile.photos
    total = len(photos)
    with_coords = len(profile.photos_with_coords)
    without_coords = total - with_coords

    map_points = [
        {
            "url": p.url,
            "lat": p.lat,
            "lon": p.lon,
            "id": p.id,
        }
        for p in profile.photos_with_coords
    ]
    map_data_json = json_for_html(map_points)

    gallery_cards: List[str] = []
    for photo in photos:
        url = photo.url
        esc_url = html.escape(url)
        coords_parts: List[str] = []
        if photo.lat is not None and photo.lon is not None:
            coords_parts.append(f"{photo.lat:.6f}, {photo.lon:.6f}")
        if photo.city:
            coords_parts.append(html.escape(photo.city))
        coords_html = (
            "<div class=\"meta\"><span>📍 " + " · ".join(coords_parts) + "</span></div>"
            if coords_parts
            else ""
        )
        size_html = (
            f"<div class=\"meta\"><span>📦 {_human_size(photo.size_bytes)}</span></div>"
            if photo.size_bytes is not None
            else ""
        )
        camera_html = (
            f"<div class=\"meta\"><span>📷 {html.escape(photo.camera)}</span></div>"
            if photo.camera
            else ""
        )
        comments_html = ""
        if photo.comments:
            comment_items = "".join(
                f"<li>{html.escape(comment)}</li>" for comment in photo.comments[:3]
            )
            more = ""
            if len(photo.comments) > 3:
                more = f"<div class=\"meta\">…и ещё {len(photo.comments) - 3}</div>"
            comments_html = f"<div class=\"comments\"><ul>{comment_items}</ul>{more}</div>"
        created_html = (
            f"<div class=\"meta\"><span>🕒 {html.escape(photo.created_at or '—')}</span></div>"
        )
        gallery_cards.append(
            """
        <article class="card">
          <a class="preview" href="{url}" target="_blank" rel="noopener">
            <img src="{url}" alt="Photo" loading="lazy"/>
          </a>
          <div class="info">
            {created}
            {coords}
            {size}
            {camera}
            <div class="meta"><a href="{url}" target="_blank" rel="noopener">Открыть оригинал</a></div>
            {comments}
          </div>
        </article>
        """.format(
                url=esc_url,
                created=created_html,
                coords=coords_html,
                size=size_html,
                camera=camera_html,
                comments=comments_html,
            )
        )

    gallery_html = "\n".join(gallery_cards) if gallery_cards else "<p>Нет фотографий для отображения.</p>"

    map_section = ""
    if map_points:
        map_section = f"""
      <section class="map-block">
        <h2>Карта снимков</h2>
        <div id="photo-map"></div>
      </section>
      <script>
        const MAP_DATA = {map_data_json};
        const PROFILE_USERNAME = {json_for_html(profile.username)};
        const map = L.map('photo-map');
        const bounds = L.latLngBounds();
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
          attribution: '&copy; OpenStreetMap contributors'
        }}).addTo(map);
        if (MAP_DATA.length) {{
          MAP_DATA.forEach(item => {{
            const marker = L.marker([item.lat, item.lon]).addTo(map);
            const popup = document.createElement('div');
            popup.className = 'popup';
            const popupTitle = document.createElement('div');
            popupTitle.className = 'popup-title';
            popupTitle.textContent = '@' + PROFILE_USERNAME;
            const photoLink = document.createElement('a');
            photoLink.href = item.url;
            photoLink.target = '_blank';
            photoLink.rel = 'noopener';
            photoLink.textContent = 'Открыть фото';
            popup.append(popupTitle, photoLink);
            marker.bindPopup(popup);
            bounds.extend([item.lat, item.lon]);
          }});
          if (bounds.isValid()) {{
            map.fitBounds(bounds.pad(0.25));
          }} else {{
            map.setView([MAP_DATA[0].lat, MAP_DATA[0].lon], 8);
          }}
        }} else {{
          map.setView([0, 0], 2);
        }}
      </script>
    """

    summary_html = (
        f"<div class=\"stats\">"
        f"<div class=\"stat\"><span class=\"value\">{total}</span><span class=\"label\">всего фото</span></div>"
        f"<div class=\"stat\"><span class=\"value\">{with_coords}</span><span class=\"label\">с координатами</span></div>"
        f"<div class=\"stat\"><span class=\"value\">{without_coords}</span><span class=\"label\">без координат</span></div>"
        f"</div>"
    )

    title = f"Экспорт VSCO — @{profile.username}" if profile.username else "Экспорт VSCO"
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    :root {{ color-scheme: light; }}
    body {{ font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f4f5f8; color: #0f172a; }}
    header {{ padding: 32px 16px 16px; text-align: center; }}
    header h1 {{ margin: 0; font-size: 32px; }}
    header a {{ color: #2563eb; text-decoration: none; }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 0 16px 48px; display: flex; flex-direction: column; gap: 32px; }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 16px; justify-content: center; }}
    .stat {{ background: #fff; padding: 16px 24px; border-radius: 16px; box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08); min-width: 160px; text-align: center; }}
    .stat .value {{ display: block; font-size: 24px; font-weight: 700; }}
    .stat .label {{ font-size: 12px; text-transform: uppercase; color: #64748b; letter-spacing: 0.08em; }}
    .map-block h2 {{ margin-bottom: 12px; }}
    #photo-map {{ width: 100%; height: 420px; border-radius: 16px; overflow: hidden; box-shadow: 0 16px 32px rgba(15, 23, 42, 0.12); }}
    .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 20px; }}
    .card {{ background: #fff; border-radius: 20px; overflow: hidden; display: flex; flex-direction: column; box-shadow: 0 18px 36px rgba(15, 23, 42, 0.1); }}
    .card .preview {{ display: block; background: #0f172a; aspect-ratio: 3/4; position: relative; }}
    .card img {{ width: 100%; height: 100%; object-fit: cover; }}
    .card .info {{ padding: 16px 18px 20px; display: flex; flex-direction: column; gap: 8px; font-size: 14px; color: #475569; }}
    .card .meta {{ font-size: 13px; color: #1f2937; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .card .meta a {{ color: #2563eb; text-decoration: none; font-weight: 600; }}
    .card .meta a:hover {{ text-decoration: underline; }}
    .card .comments ul {{ margin: 0 0 4px 18px; padding: 0; list-style: disc; }}
    .card .comments li {{ margin-bottom: 4px; }}
    footer {{ text-align: center; padding: 24px; font-size: 12px; color: #94a3b8; }}
    @media (max-width: 640px) {{
      header h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Экспорт профиля VSCO — @{html.escape(profile.username)}</h1>
    <div><a href="{html.escape(profile.profile_url)}" target="_blank" rel="noopener">Открыть профиль VSCO</a></div>
  </header>
  <main>
    {summary_html}
    {map_section}
    <section>
      <h2>Галерея</h2>
      <div class="gallery">
        {gallery_html}
      </div>
    </section>
  </main>
  <footer>Сгенерировано скриптом export_profile_html.py</footer>
</body>
</html>
"""


def run_cli(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Export VSCO profile gallery/map to a single HTML file")
    parser.add_argument("username", help="VSCO username (with or without @)")
    parser.add_argument("--db", dest="db_path", default=DEFAULT_DB_PATH, help="Path to vsco_links SQLite database")
    parser.add_argument("--chat-id", type=int, help="Limit export to a specific chat_id")
    parser.add_argument("-o", "--out", dest="output", help="Output HTML path")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        profile = load_profile(conn, args.username, chat_id=args.chat_id)
    finally:
        conn.close()

    output_path = Path(args.output) if args.output else Path(f"export_{_normalize_username(args.username) or 'profile'}.html")
    html_payload = build_profile_html(profile)
    output_path.write_text(html_payload, encoding="utf-8")
    print(f"HTML export saved to {output_path}")


if __name__ == "__main__":
    run_cli()
