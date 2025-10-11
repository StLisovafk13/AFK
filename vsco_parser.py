# -*- coding: utf-8 -*-
"""
VSCO Parser: HTML → Excel (+ optional HTML gallery/map)
Supports:
  1) .image-card cards with .username, .coordinates, a.view-profile, img[src]
  2) Fallback lists of .username/.coordinates/a.view-profile/img[src] merged by index
  3) Simple list of a[href^="https://vsco.co/"]
  4) Visual Search HTML with `users = [ {...} ];` in a <script>
"""

import argparse, json, re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:  # pragma: no cover - optional dependency for HTML parsing
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - executed when dependency is absent
    BeautifulSoup = None  # type: ignore

try:  # pragma: no cover - optional dependency for Excel helpers
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover - executed when dependency is absent
    pd = None  # type: ignore


def split_coords(s: str) -> Tuple[Optional[float], Optional[float]]:
    if not s:
        return None, None
    s = s.strip().replace("°", "").replace(",", " ").replace("  ", " ")
    parts = [p for p in s.split() if p]
    if len(parts) >= 2:
        try:
            return float(parts[0]), float(parts[1])
        except Exception:
            return None, None
    return None, None


def _extract_users_from_script(html: str) -> List[Dict[str, object]]:
    m = re.search(r'users\s*=\s*(\[[\s\S]*?\])\s*;', html)
    if not m:
        return []
    raw = m.group(1)
    try:
        data = json.loads(raw)
    except Exception:
        cleaned = re.sub(r",\s*]", "]", raw)
        cleaned = re.sub(r",\s*}", "}", cleaned)
        try:
            data = json.loads(cleaned)
        except Exception:
            return []
    out: List[Dict[str, object]] = []
    for obj in data:
        try:
            username = (obj.get("username") or "").strip()
            if username.startswith("@"): username = username[1:]
            lat = obj.get("lat"); lng = obj.get("lng")
            try: lat = float(lat) if lat is not None else None
            except Exception: lat = None
            try: lng = float(lng) if lng is not None else None
            except Exception: lng = None
            out.append({
                "username": username,
                "latitude": lat,
                "longitude": lng,
                "profile_url": obj.get("profile_url") or None,
                "image_url": obj.get("image_url") or None,
            })
        except Exception:
            continue
    return out


def parse_html_file(path: Path) -> List[Dict[str, object]]:
    if BeautifulSoup is None:
        raise RuntimeError("BeautifulSoup is required to parse HTML. Install bs4 to enable this feature.")
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, object]] = []

    # Visual Search users=[...]
    extracted = _extract_users_from_script(html)
    if extracted:
        return extracted

    # .image-card flow
    cards = soup.select(".image-card")
    if cards:
        for card in cards:
            u = ""
            u_node = card.select_one(".username")
            if u_node:
                u = u_node.get_text(strip=True)
                if u.startswith("@"): u = u[1:]
            coord_text = ""
            c_node = card.select_one(".coordinates")
            if c_node:
                coord_text = c_node.get_text(strip=True).replace("📍","").strip()
            lat, lon = split_coords(coord_text)
            link = None
            a = card.select_one("a.view-profile[href]")
            if a: link = a.get("href")
            img = None
            img_node = card.select_one("img[src]")
            if img_node: img = img_node.get("src")
            rows.append({"username": u, "latitude": lat, "longitude": lon, "profile_url": link, "image_url": img})
        return rows

    # Fallback lists
    user_nodes = [n.get_text(strip=True) for n in soup.select(".username")]
    coord_nodes = [n.get_text(strip=True).replace("📍","").strip() for n in soup.select(".coordinates")]
    link_nodes = [a.get("href") for a in soup.select("a.view-profile[href]")]
    img_nodes = [img.get("src") for img in soup.select("img[src]")]
    n = max(len(user_nodes), len(coord_nodes), len(link_nodes), len(img_nodes)) if (user_nodes or coord_nodes or link_nodes or img_nodes) else 0
    for i in range(n):
        u = user_nodes[i] if i < len(user_nodes) else ""
        if u.startswith("@"): u = u[1:]
        c = coord_nodes[i] if i < len(coord_nodes) else ""
        link = link_nodes[i] if i < len(link_nodes) else None
        img = img_nodes[i] if i < len(img_nodes) else None
        lat, lon = split_coords(c)
        rows.append({"username": u, "latitude": lat, "longitude": lon, "profile_url": link, "image_url": img})

    # Simple anchors
    if not rows:
        for a in soup.select("a[href^='https://vsco.co/']"):
            href = a.get("href")
            text = (a.get_text(strip=True) or "")
            username = text or (href.rstrip("/").split("/")[-1] if href else "")
            if username.startswith("@"): username = username[1:]
            rows.append({"username": username, "latitude": None, "longitude": None, "profile_url": href, "image_url": None})

    return rows


def write_excel(rows: List[Dict[str, object]], out_path: Path, sheet: str = "data"):
    if pd is None:
        raise RuntimeError("pandas is required to export Excel files. Install pandas to enable this feature.")
    df = pd.DataFrame(rows, columns=["username", "latitude", "longitude", "profile_url", "image_url"])
    with pd.ExcelWriter(Path(out_path), engine="openpyxl") as w:
        df.to_excel(w, sheet_name=sheet, index=False)


def read_excel(xlsx_path: Path, sheet: str = "data") -> List[Dict[str, object]]:
    if pd is None:
        raise RuntimeError("pandas is required to read Excel files. Install pandas to enable this feature.")
    df = pd.read_excel(xlsx_path, sheet_name=sheet)
    for col in ["username", "latitude", "longitude"]:
        if col not in df.columns:
            df[col] = None
    for col in ["profile_url", "image_url"]:
        if col not in df.columns:
            df[col] = None
    out: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        lat = row.get("latitude"); lon = row.get("longitude")
        try: lat = float(lat) if pd.notna(lat) else None
        except Exception: lat = None
        try: lon = float(lon) if pd.notna(lon) else None
        except Exception: lon = None
        out.append({
            "username": (str(row.get("username")) if pd.notna(row.get("username")) else "") or "",
            "latitude": lat,
            "longitude": lon,
            "profile_url": (str(row.get("profile_url")) if pd.notna(row.get("profile_url")) else None) or None,
            "image_url": (str(row.get("image_url")) if pd.notna(row.get("image_url")) else None) or None,
        })
    return out




def _norm_username(u: Optional[str]) -> str:
    u = (u or "").strip()
    return u[1:].lower() if u.startswith("@") else u.lower()

def _norm_url(u: Optional[str]) -> str:
    if not u:
        return ""
    u = u.strip()
    u = u.replace("http://", "https://")
    while u.endswith("/") and len(u) > len("https://"):
        u = u[:-1]
    return u

def _round(v: Optional[float], nd=6) -> Optional[float]:
    try:
        return round(float(v), nd) if v is not None else None
    except Exception:
        return None

def dedupe_rows(rows: List[Dict[str, object]], mode: str = "safe") -> List[Dict[str, object]]:
    if mode == "none":
        return list(rows)
    best_profile_for = {}
    for r in rows:
        uname = _norm_username(r.get("username"))
        pu = _norm_url(r.get("profile_url"))
        if pu:
            best_profile_for.setdefault(uname, pu)
    ordered = {}
    for r in rows:
        uname = _norm_username(r.get("username"))
        img = _norm_url(r.get("image_url"))
        prof = _norm_url(r.get("profile_url"))
        lat = _round(r.get("latitude"))
        lon = _round(r.get("longitude"))
        if img:
            key = ("img", uname, img)
        elif lat is not None and lon is not None:
            key = ("geo", uname, lat, lon)
        elif prof:
            key = ("profile", uname, prof)
        else:
            key = ("user", uname)
        if key not in ordered:
            ordered[key] = {
                "username": r.get("username") or "",
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "profile_url": r.get("profile_url"),
                "image_url": r.get("image_url"),
            }
        else:
            ex = ordered[key]
            if not ex.get("username") and r.get("username"):
                ex["username"] = r.get("username")
            if ex.get("latitude") in (None, "", float("nan")) and r.get("latitude") not in (None, "", float("nan")):
                ex["latitude"] = r.get("latitude")
            if ex.get("longitude") in (None, "", float("nan")) and r.get("longitude") not in (None, "", float("nan")):
                ex["longitude"] = r.get("longitude")
            if not ex.get("profile_url") and r.get("profile_url"):
                ex["profile_url"] = r.get("profile_url")
            if not ex.get("image_url") and r.get("image_url"):
                ex["image_url"] = r.get("image_url")
    merged = list(ordered.values())
    for r in merged:
        uname = _norm_username(r.get("username"))
        if not r.get("profile_url") and uname in best_profile_for:
            r["profile_url"] = best_profile_for[uname]
    if mode != "aggressive":
        return merged
    info = {}
    for r in merged:
        uname = _norm_username(r.get("username"))
        has_payload = bool(_norm_url(r.get("image_url"))) or (r.get("latitude") is not None and r.get("longitude") is not None)
        st = info.setdefault(uname, {"has_payload": False})
        if has_payload:
            st["has_payload"] = True
    out = []
    for r in merged:
        uname = _norm_username(r.get("username"))
        is_stub = (not _norm_url(r.get("image_url"))) and (r.get("latitude") is None or r.get("longitude") is None)
        if is_stub and info.get(uname, {}).get("has_payload", False):
            continue
        out.append(r)
    return out
def build_gallery_html(items, title="VSCO Gallery", subtitle="Merged"):
    import html as pyhtml, json as pyjson
    data = []
    for it in items:
        data.append({
            "username": it.get("username") or "",
            "latitude": it.get("latitude"),
            "longitude": it.get("longitude"),
            "profile_url": it.get("profile_url") or "",
            "image_url": it.get("image_url") or "",
        })
    data_json = pyjson.dumps(data, ensure_ascii=False)

    # Используем .format и экранируем фигурные скобки удвоением {{ }}
    tpl = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>{TITLE}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
.header {{ text-align:center; background:#fff; padding:16px; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,.1); }}
.controls, .stats {{ background:#fff; padding:12px; border-radius:10px; box-shadow:0 2px 10px rgba(0,0,0,.1); margin: 16px 0; }}
.controls {{ display:grid; grid-template-columns: repeat(7, minmax(120px,1fr)); gap:10px; }}
.controls input, .controls select {{ padding:8px 10px; border:1px solid #ddd; border-radius:6px; }}
.controls button {{ padding:10px 14px; border:none; border-radius:6px; cursor:pointer; }}
.btn-apply {{ background:#007bff; color:#fff; }}
.btn-reset {{ background:#6c757d; color:#fff; }}
.gallery {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:16px; }}
.user-block {{ background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 2px 10px rgba(0,0,0,.1); }}
.user-header {{ display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border-bottom:1px solid #f0f0f0; }}
.user-header .username {{ font-weight:700; font-size:16px; }}
.user-header a {{ background:#28a745; color:#fff; padding:8px 12px; border-radius:6px; text-decoration:none; font-size:13px; }}
.user-header a:hover {{ background:#1e7e34; }}
.user-sub {{ color:#777; font-size:12px; margin-left:8px; }}
.user-media-grid {{ display:grid; grid-template-columns: 1fr; gap:10px; padding:12px; }}
.image-card {{ background:#fff; border-radius:10px; border:1px solid #f0f0f0; overflow:hidden; }}
.image-card img {{ width:100%; height:280px; object-fit:cover; display:block; }}
.image-info {{ padding:10px; }}
.coordinates {{ color:#666; font-size:14px; margin-bottom:8px; }}
.links a {{ display:inline-block; background:#007bff; color:#fff; padding:8px 12px; border-radius:6px; text-decoration:none; font-size:14px; }}
.links a:hover {{ background:#0056b3; }}
@media (min-width: 768px) {{ .user-media-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (min-width: 1200px) {{ .user-media-grid {{ grid-template-columns: repeat(3, 1fr); }} }}
</style>
</head>
<body>
<div class="header">
  <h1>🎯 VSCO Gallery</h1>
  <h2>{SUBTITLE}</h2>
</div>

<div class="controls">
  <input id="f-user" placeholder="Filter by username">
  <input id="f-lat-min" type="number" step="any" placeholder="Lat min">
  <input id="f-lat-max" type="number" step="any" placeholder="Lat max">
  <input id="f-lon-min" type="number" step="any" placeholder="Lon min">
  <input id="f-lon-max" type="number" step="any" placeholder="Lon max">
  <select id="sort-by">
    <option value="username-asc">Username A → Z</option>
    <option value="username-desc">Username Z → A</option>
    <option value="count-desc" selected>More images first</option>
    <option value="count-asc">Fewer images first</option>
  </select>
  <div style="display:flex; gap:10px;">
    <button class="btn-apply" id="btn-apply">Apply</button>
    <button class="btn-reset" id="btn-reset">Reset</button>
  </div>
</div>

<div class="stats" id="stats"></div>
<div class="gallery" id="gallery"></div>

<script>
const data = {DATA_JSON};

function groupByUsername(items) {{
  const groups = new Map();
  for (const it of items) {{
    const key = (it.username || '').toLowerCase();
    if (!groups.has(key)) groups.set(key, {{ username: it.username || '', profile_url: it.profile_url || '', entries: [] }});
    const ent = {{ latitude: it.latitude, longitude: it.longitude, image_url: it.image_url || '' }};
    groups.get(key).entries.push(ent);
    if (!groups.get(key).profile_url && it.profile_url) groups.get(key).profile_url = it.profile_url;
  }}
  return Array.from(groups.values());
}}

function avg(xs) {{
  const a = xs.filter(x => typeof x === 'number');
  if (!a.length) return null;
  return a.reduce((s,x)=>s+x,0)/a.length;
}}

function render(groups) {{
  const g = document.getElementById('gallery');
  g.innerHTML = '';
  for (const grp of groups) {{
    const avgLat = avg(grp.entries.map(e=>e.latitude));
    const avgLon = avg(grp.entries.map(e=>e.longitude));
    const count = grp.entries.length;
    const block = document.createElement('div');
    block.className = 'user-block';
    block.dataset.username = (grp.username || '').toLowerCase();
    block.dataset.count = count;
    block.dataset.avgLat = avgLat ?? '';
    block.dataset.avgLon = avgLon ?? '';
    block.innerHTML = `
      <div class="user-header">
        <div><span class="username">@${{grp.username || '(no username)'}} </span>
          <span class="user-sub">${{count}} item(s)${{avgLat!=null&&avgLon!=null?` • avg: ${{avgLat.toFixed(6)}}, ${{avgLon.toFixed(6)}}`:''}}</span>
        </div>
        <a href="${{grp.profile_url || '#'}}" target="_blank">View Profile</a>
      </div>
      <div class="user-media-grid"></div>`;
    const grid = block.querySelector('.user-media-grid');
    for (const e of grp.entries) {{
      const latStr = (typeof e.latitude==='number')? e.latitude.toFixed(6) : (e.latitude || '');
      const lonStr = (typeof e.longitude==='number')? e.longitude.toFixed(6) : (e.longitude || '');
      const card = document.createElement('div');
      card.className = 'image-card';
      card.dataset.lat = typeof e.latitude==='number' ? e.latitude : '';
      card.dataset.lon = typeof e.longitude==='number' ? e.longitude : '';
      card.innerHTML = `
        ${{e.image_url?`<img src="${{e.image_url}}" alt="VSCO">`:''}}
        <div class="image-info">
          <div class="coordinates">${{(latStr||lonStr)?`📍 ${{latStr}}, ${{lonStr}}`:''}}</div>
          <div class="links">${{e.image_url?`<a href="${{e.image_url}}" target="_blank">View Image</a>`:''}}</div>
        </div>`;
      grid.appendChild(card);
    }}
    g.appendChild(block);
  }}
  document.getElementById('stats').textContent = `Users: ${{groups.length}} • Entries: ${{groups.reduce((s,g)=>s+g.entries.length,0)}}`;
}}

function applyFilters() {{
  const name = (document.getElementById('f-user').value || '').toLowerCase();
  const latMin = parseFloat(document.getElementById('f-lat-min').value);
  const latMax = parseFloat(document.getElementById('f-lat-max').value);
  const lonMin = parseFloat(document.getElementById('f-lon-min').value);
  const lonMax = parseFloat(document.getElementById('f-lon-max').value);
  const key = document.getElementById('sort-by').value;

  const blocks = Array.from(document.querySelectorAll('.user-block'));
  blocks.forEach(b => {{
    const uname = (b.dataset.username || '');
    const nameOk = !name || uname.includes(name);
    let anyVisible = false;
    const cards = b.querySelectorAll('.image-card');
    cards.forEach(c => {{
      const lat = parseFloat(c.dataset.lat);
      const lon = parseFloat(c.dataset.lon);
      let ok = true;
      if (name && !nameOk) ok = false;
      if (!isNaN(latMin) && !(typeof lat==='number' && !isNaN(lat) && lat >= latMin)) ok = false;
      if (!isNaN(latMax) && !(typeof lat==='number' && !isNaN(lat) && lat <= latMax)) ok = false;
      if (!isNaN(lonMin) && !(typeof lon==='number' && !isNaN(lon) && lon >= lonMin)) ok = false;
      if (!isNaN(lonMax) && !(typeof lon==='number' && !isNaN(lon) && lon <= lonMax)) ok = false;
      c.style.display = ok ? '' : 'none';
      if (ok) anyVisible = true;
    }});
    b.style.display = anyVisible ? '' : 'none';
  }});

  const gallery = document.getElementById('gallery');
  const sorted = Array.from(document.querySelectorAll('.user-block')).sort((a,b) => {{
    if (key === 'username-asc' || key === 'username-desc') {{
      const A = (a.dataset.username || '').localeCompare(b.dataset.username || '');
      return key === 'username-asc' ? A : -A;
    }}
    if (key === 'count-asc' || key === 'count-desc') {{
      const A = parseInt(a.dataset.count || '0', 10);
      const B = parseInt(b.dataset.count || '0', 10);
      return key === 'count-asc' ? (A-B) : (B-A);
    }}
    return 0;
  }});
  sorted.forEach(el => gallery.appendChild(el));
}}

document.getElementById('btn-apply').addEventListener('click', applyFilters);
document.getElementById('btn-reset').addEventListener('click', () => {{
  document.getElementById('f-user').value='';
  document.getElementById('f-lat-min').value='';
  document.getElementById('f-lat-max').value='';
  document.getElementById('f-lon-min').value='';
  document.getElementById('f-lon-max').value='';
  document.getElementById('sort-by').value='count-desc';
  applyFilters();
}});

const initialGroups = groupByUsername(data);
render(initialGroups);
applyFilters();
</script>
</body>
</html>"""
    return tpl.format(TITLE=pyhtml.escape(title), SUBTITLE=pyhtml.escape(subtitle), DATA_JSON=data_json)


def build_map_html(items, title="Visual Search Results", center_lat=0.0, center_lng=0.0, radius_km=1.0, zoom=12):
    import html as pyhtml, json as pyjson
    users = []
    for it in items:
        if it.get("latitude") is None or it.get("longitude") is None:
            continue
        users.append({
            "username": it.get("username") or "",
            "lat": float(it.get("latitude")),
            "lng": float(it.get("longitude")),
            "image_url": it.get("image_url") or "",
            "profile_url": it.get("profile_url") or "",
        })
    users_json = pyjson.dumps(users, ensure_ascii=False)
    tpl = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{TITLE}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>html, body, #map {{ height: 100%; margin: 0; }}</style>
</head>
<body>
<div id="map"></div>
<script>
  const users = {USERS_JSON};
  const map = L.map('map').setView([{CLAT}, {CLNG}], {ZOOM});
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ attribution: '© OpenStreetMap' }}).addTo(map);
  if ({RADIUS} > 0) {{
    L.circle([{CLAT}, {CLNG}], {{ radius: {RADIUS} * 1000, color: 'red', fillOpacity: 0.1 }}).addTo(map);
  }}
  users.forEach(u => {{
    const m = L.marker([u.lat, u.lng]).addTo(map);
    const img = u.image_url ? `<img src="${{u.image_url}}" style="max-width:180px;max-height:180px;border-radius:8px;margin-bottom:8px;">` : '';
    m.bindPopup(`<div style="min-width:240px"><h4>@${{u.username}}</h4>${{img}}<p>${{u.lat.toFixed(6)}}, ${{u.lng.toFixed(6)}}</p><p><a href="${{u.profile_url}}" target="_blank">View Profile</a></p></div>`);
  }});
</script>
</body>
</html>"""
    return tpl.format(TITLE=pyhtml.escape(title), USERS_JSON=users_json, CLAT=center_lat, CLNG=center_lng, ZOOM=int(zoom), RADIUS=float(radius_km))


def main():
    ap = argparse.ArgumentParser(description="Parse VSCO HTML → Excel/HTML")
    ap.add_argument("--dedupe", choices=["none","safe","aggressive"], default="safe", help="Duplicate cleanup mode")
    ap.add_argument("inputs", nargs="*", help="HTML files/dirs/globs (ignored if --from-xlsx)")
    ap.add_argument("-o", "--output", default="extracted.xlsx", help="Output XLSX file")
    ap.add_argument("--sheet", default="data", help="XLSX sheet name")
    ap.add_argument("--from-xlsx", help="Build HTML from given XLSX (skip parsing HTML)")
    ap.add_argument("--html-out", help="Output HTML path")
    ap.add_argument("--html-type", choices=["gallery","map"], default="gallery")
    ap.add_argument("--html-title", default="VSCO Gallery")
    ap.add_argument("--html-subtitle", default="Merged")
    ap.add_argument("--center-lat", type=float, default=0.0)
    ap.add_argument("--center-lng", type=float, default=0.0)
    ap.add_argument("--radius-km", type=float, default=1.0)
    ap.add_argument("--zoom", type=int, default=12)
    args = ap.parse_args()

    if args.from_xlsx:
        items = read_excel(Path(args.from_xlsx), sheet=args.sheet)
        items = dedupe_rows(items, mode=args.dedupe)
        if not args.html_out:
            print("Loaded", len(items), "rows from XLSX")
            return
        if args.html_type == "gallery":
            Path(args.html_out).write_text(build_gallery_html(items, title=args.html_title, subtitle=args.html_subtitle), encoding="utf-8")
        else:
            Path(args.html_out).write_text(build_map_html(items, title=args.html_title, center_lat=args.center_lat, center_lng=args.center_lng, radius_km=args.radius_km, zoom=args.zoom), encoding="utf-8")
        print("Wrote HTML ->", args.html_out)
        return

    # Parse HTML -> Excel (and optionally HTML)
    files: List[Path] = []
    for p in args.inputs:
        pth = Path(p)
        if pth.is_dir():
            files += list(pth.rglob("*.html"))
        else:
            files.append(pth)
    rows: List[Dict[str, object]] = []
    for f in files:
        if f.exists():
            rows.extend(parse_html_file(f))

    rows = dedupe_rows(rows, mode=args.dedupe)
    write_excel(rows, Path(args.output), sheet=args.sheet)
    print("Wrote Excel rows:", len(rows), "->", args.output)

    if args.html_out:
        items = read_excel(Path(args.output), sheet=args.sheet)
        if args.html_type == "gallery":
            Path(args.html_out).write_text(build_gallery_html(items, title=args.html_title, subtitle=args.html_subtitle), encoding="utf-8")
        else:
            Path(args.html_out).write_text(build_map_html(items, title=args.html_title, center_lat=args.center_lat, center_lng=args.center_lng, radius_km=args.radius_km, zoom=args.zoom), encoding="utf-8")
        print("Wrote HTML ->", args.html_out)


if __name__ == "__main__":
    main()
