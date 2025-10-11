"""Map feature HTML builders."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from core.added_by import added_by_display_and_link, added_by_html

def _normalize_created_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return ""
    return text.strip()


def _format_created_range(first: Any, last: Any) -> str:
    first_text = _normalize_created_value(first)
    last_text = _normalize_created_value(last)
    if first_text and last_text:
        return first_text if first_text == last_text else f"{first_text} → {last_text}"
    return first_text or last_text


def build_map_users(users: List[Dict[str, Any]], title="VSCO Profiles (Users)"):
    meta: List[Tuple[Dict[str, Any], Optional[float], Optional[float], str, bool]] = []
    list_rows: List[str] = []
    for idx, u in enumerate(users):
        raw_lat = u.get("lat"); raw_lon = u.get("lon")
        try:
            lat = float(raw_lat) if raw_lat is not None else None
            lon = float(raw_lon) if raw_lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        has_coord = lat is not None and lon is not None
        key = f"u{idx}" if has_coord else ""
        meta.append((u, lat, lon, key, has_coord))

        uname = escape(u.get("username", ""))
        link = escape(u.get("profile_url", ""))
        cm = u.get("comments") or []
        comment_count = len(cm)
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:3])
            more = f"<div class='c'>и ещё {len(cm)-3}…</div>" if len(cm) > 3 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"

        added_raw = u.get("added_by_raw", "")
        added_display, _ = added_by_display_and_link(added_raw)
        added_html = added_by_html(added_raw)
        added_block = f"<div class='ab'>Добавил: {added_html or '—'}</div>"
        added_key = (added_display or "").strip().lower()

        raw_datasets = u.get("datasets") or []
        dataset_payload: List[Dict[str, str]] = []
        dataset_labels: List[str] = []
        seen_dataset: set[str] = set()
        for entry in raw_datasets:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                label = str(entry.get("label") or value)
            else:
                value = str(entry or "")
                label = value
            if not value or value in seen_dataset:
                continue
            seen_dataset.add(value)
            dataset_labels.append(label)
            dataset_payload.append({"value": value, "label": label})

        raw_cities = [str(city) for city in (u.get("cities") or []) if city]

        search_parts = [str(u.get("username") or "")]
        search_parts.extend(str(x or "") for x in cm)
        if added_display:
            search_parts.append(added_display)
        search_parts.extend(dataset_labels)
        search_parts.extend(raw_cities)
        search_text = " ".join(p.strip() for p in search_parts if p).lower()

        comment_filter_text = " ".join(str(x or "") for x in cm).lower()
        added_filter_text = (added_display or "").lower()

        first_created = _normalize_created_value(u.get("first_created"))
        last_created = _normalize_created_value(u.get("last_created"))
        created_label = _format_created_range(u.get("first_created"), u.get("last_created"))
        created_block = (
            f"<div class='created-range'>Добавлено: {escape(created_label)}</div>"
            if created_label
            else ""
        )

        attrs = [
            f"data-has-coords=\"{1 if has_coord else 0}\"",
            f"data-search=\"{escape(search_text, quote=True)}\"",
            f"data-comments=\"{escape(comment_filter_text, quote=True)}\"",
            f"data-added=\"{escape(added_filter_text, quote=True)}\"",
            f"data-cities=\"{escape(json.dumps(raw_cities, ensure_ascii=False), quote=True)}\"",
            f"data-datasets=\"{escape(json.dumps(dataset_payload, ensure_ascii=False), quote=True)}\"",
            f"data-comments-count=\"{comment_count}\"",
            f"data-added-key=\"{escape(added_key, quote=True)}\"",
            f"data-added-label=\"{escape(added_display or '', quote=True)}\"",
        ]
        if first_created:
            attrs.append(f"data-first-created=\"{escape(first_created, quote=True)}\"")
        if last_created:
            attrs.append(f"data-last-created=\"{escape(last_created, quote=True)}\"")
        if has_coord:
            attrs.extend(
                [
                    f"data-key=\"{key}\"",
                    f"data-lat=\"{lat:.6f}\"",
                    f"data-lon=\"{lon:.6f}\"",
                ]
            )
        meta_chips: List[str] = []
        for city in raw_cities[:3]:
            meta_chips.append(f"<span class='chip chip-city'>{escape(city)}</span>")
        for label in dataset_labels[:3]:
            meta_chips.append(f"<span class='chip chip-data'>{escape(label)}</span>")
        meta_block = f"<div class='meta'>{''.join(meta_chips)}</div>" if meta_chips else ""

        attr_html = " " + " ".join(attrs)
        list_rows.append(
            f"""
          <div class=\"row row-user\"{attr_html}>
            <div class=\"u\"><a href=\"{link}\" target=\"_blank\">@{uname}</a></div>
            {meta_block}
            {cm_txt}
            {created_block}
            {added_block}
          </div>"""
        )

    list_html = "".join(list_rows)
    total = len(users)
    with_coords = sum(1 for _, _, _, _, hc in meta if hc)
    summary_text = (
        "Нет данных"
        if total == 0
        else f"Пользователи: {total} • С координатами: {with_coords} • Без координат: {total - with_coords}"
    )

    marker_js: List[str] = []
    if with_coords:
        marker_js += [
            f"if(loadingController && loadingController.start){{ loadingController.start({with_coords}); }}",
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup({chunkedLoading:true,chunkDelay:20,chunkInterval:200,removeOutsideVisibleBounds:true,spiderfyDistanceMultiplier:1.1,chunkProgress:function(processed,total){ if(loadingController && loadingController.update){ loadingController.update(processed,total); } }});",
            "var markerByKey={};",
            "if(markers.on){ markers.on('chunkedLoadingEnd', function(){ if(loadingController && loadingController.finish){ loadingController.finish(); }}); } else if(loadingController && loadingController.finish){ loadingController.finish(); }",
        ]
        for u, lat, lon, key, has_coord in meta:
            if not has_coord:
                continue
            uname = escape(str(u.get("username") or ""))
            prof = escape(str(u.get("profile_url") or ""))
            added_html = added_by_html(u.get("added_by_raw", ""))
            city_values: List[str] = []
            for city in u.get("cities") or []:
                text = str(city)
                if text:
                    city_values.append(escape(text))
            dataset_labels_marker: List[str] = []
            seen_dataset_labels: set[str] = set()
            for entry in u.get("datasets") or []:
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("value") or "")
                else:
                    label = str(entry or "")
                if not label or label in seen_dataset_labels:
                    continue
                seen_dataset_labels.add(label)
                dataset_labels_marker.append(escape(label))
            parts = [f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a>"]
            if city_values:
                parts.append("<br/>📍 " + ", ".join(city_values[:3]))
            if dataset_labels_marker:
                parts.append("<br/>💾 " + ", ".join(dataset_labels_marker[:3]))
            if added_html:
                parts.append(f"<br/>Добавил: {added_html}")
            parts.append("</div>")
            popup = "".join(parts)
            marker_js.append(
                f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); "
                f"markers.addLayer(m); bounds.extend([{lat},{lon}]); markerByKey[{key!r}]=m;",
            )
        marker_js += [
            "map.addLayer(markers);",
            "if(bounds.isValid()){map.fitBounds(bounds.pad(0.1));}else{map.setView([20,0],2);}",
            "withFiltering(function(filteringState){",
            "  setupListInteractions(map, markerByKey, markers, filteringState);",
            "  bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
            "});",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("if(loadingController && loadingController.finish){ loadingController.finish(); }")
        marker_js.append("withFiltering(function(filteringState){")
        marker_js.append("  setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("  bindFilteringToMarkers(filteringState, map, null, {});")
        marker_js.append("});")

    return _map_html(
        title,
        list_html,
        marker_js,
        stats={
            "summary_text": summary_text,
            "total": total,
            "with_coords": with_coords,
            "without_coords": total - with_coords,
        },
    )

def build_map_images(items: List[Dict[str, Any]], title="VSCO Profiles (Images)"):
    meta: List[Tuple[Dict[str, Any], Optional[float], Optional[float], str, bool]] = []
    rows: List[str] = []
    for idx, r in enumerate(items):
        raw_lat = r.get("lat"); raw_lon = r.get("lon")
        try:
            lat = float(raw_lat) if raw_lat is not None else None
            lon = float(raw_lon) if raw_lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        has_coord = lat is not None and lon is not None
        key = f"i{idx}" if has_coord else ""
        meta.append((r, lat, lon, key, has_coord))

        uname = escape(r.get("username", ""))
        link = escape(r.get("profile_url", ""))
        img = r.get("image_url") or ""
        thumb_html = (
            f"<img src='{escape(img)}' loading='lazy' style='width:68px;height:68px;object-fit:cover;border-radius:8px;border:1px solid #eee;'/>"
            if img
            else ""
        )
        cm = r.get("comments") or []
        comment_count = len(cm)
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:2])
            more = f"<div class='c'>и ещё {len(cm)-2}…</div>" if len(cm) > 2 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"

        added_raw = r.get("added_by_raw", "")
        added_display, _ = added_by_display_and_link(added_raw)
        added_html = added_by_html(added_raw)
        added_block = f"<div class='ab'>Добавил: {added_html or '—'}</div>"
        added_key = (added_display or "").strip().lower()

        raw_datasets = r.get("datasets") or []
        dataset_payload: List[Dict[str, str]] = []
        dataset_labels: List[str] = []
        seen_dataset: set[str] = set()
        for entry in raw_datasets:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                label = str(entry.get("label") or value)
            else:
                value = str(entry or "")
                label = value
            if not value or value in seen_dataset:
                continue
            seen_dataset.add(value)
            dataset_labels.append(label)
            dataset_payload.append({"value": value, "label": label})

        raw_cities = [str(city) for city in (r.get("cities") or []) if city]

        search_parts = [str(r.get("username") or ""), str(img or "")]
        search_parts.extend(str(x or "") for x in cm)
        if added_display:
            search_parts.append(added_display)
        search_parts.extend(dataset_labels)
        search_parts.extend(raw_cities)
        search_text = " ".join(p.strip() for p in search_parts if p).lower()

        comment_filter_text = " ".join(str(x or "") for x in cm).lower()
        added_filter_text = (added_display or "").lower()
        created_single = _normalize_created_value(r.get("created_at"))
        created_block = (
            f"<div class='created-range'>Добавлено: {escape(created_single)}</div>"
            if created_single
            else ""
        )

        attrs = [
            f"data-has-coords=\"{1 if has_coord else 0}\"",
            f"data-search=\"{escape(search_text, quote=True)}\"",
            f"data-comments=\"{escape(comment_filter_text, quote=True)}\"",
            f"data-added=\"{escape(added_filter_text, quote=True)}\"",
            f"data-cities=\"{escape(json.dumps(raw_cities, ensure_ascii=False), quote=True)}\"",
            f"data-datasets=\"{escape(json.dumps(dataset_payload, ensure_ascii=False), quote=True)}\"",
            f"data-comments-count=\"{comment_count}\"",
            f"data-added-key=\"{escape(added_key, quote=True)}\"",
            f"data-added-label=\"{escape(added_display or '', quote=True)}\"",
        ]
        if created_single:
            attrs.append(f"data-first-created=\"{escape(created_single, quote=True)}\"")
            attrs.append(f"data-last-created=\"{escape(created_single, quote=True)}\"")
        if has_coord:
            attrs.extend(
                [
                    f"data-key=\"{key}\"",
                    f"data-lat=\"{lat:.6f}\"",
                    f"data-lon=\"{lon:.6f}\"",
                ]
            )
        meta_chips: List[str] = []
        for city in raw_cities[:3]:
            meta_chips.append(f"<span class='chip chip-city'>{escape(city)}</span>")
        for label in dataset_labels[:3]:
            meta_chips.append(f"<span class='chip chip-data'>{escape(label)}</span>")
        meta_block = f"<div class='meta'>{''.join(meta_chips)}</div>" if meta_chips else ""

        attr_html = " " + " ".join(attrs)
        rows.append(
            f"""
          <div class=\"row row-image\"{attr_html}>
            <div class=\"u\"><a href=\"{link}\" target=\"_blank\">@{uname}</a></div>
            <div class=\"t\">{thumb_html}</div>
            {meta_block}
            {cm_txt}
            {created_block}
            {added_block}
          </div>"""
        )

    list_html = "".join(rows)
    total = len(items)
    with_coords = sum(1 for _, _, _, _, hc in meta if hc)
    unique_users = len({str(r.get("username") or "") for r in items if r.get("username")})
    summary_text = (
        "Нет данных"
        if total == 0
        else f"Фотографии: {total} • Пользователи: {unique_users} • С координатами: {with_coords}"
    )

    marker_js: List[str] = []
    if with_coords:
        marker_js += [
            f"if(loadingController && loadingController.start){{ loadingController.start({with_coords}); }}",
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup({chunkedLoading:true,chunkDelay:20,chunkInterval:200,removeOutsideVisibleBounds:true,spiderfyDistanceMultiplier:1.1,chunkProgress:function(processed,total){ if(loadingController && loadingController.update){ loadingController.update(processed,total); } }});",
            "var markerByKey={};",
            "if(markers.on){ markers.on('chunkedLoadingEnd', function(){ if(loadingController && loadingController.finish){ loadingController.finish(); }}); } else if(loadingController && loadingController.finish){ loadingController.finish(); }",
        ]
        for r, lat, lon, key, has_coord in meta:
            if not has_coord:
                continue
            uname = escape(str(r.get("username") or ""))
            prof = escape(str(r.get("profile_url") or ""))
            img = r.get("image_url") or ""
            img_html = (
                f"<img src='{escape(img)}' loading='lazy' style='width:140px;height:140px;object-fit:cover;border-radius:10px;border:1px solid #eee;'/>"
                if img
                else ""
            )
            added_html = added_by_html(r.get("added_by_raw", ""))
            city_values: List[str] = []
            for city in r.get("cities") or []:
                text = str(city)
                if text:
                    city_values.append(escape(text))
            dataset_labels_marker: List[str] = []
            seen_dataset_labels: set[str] = set()
            for entry in r.get("datasets") or []:
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("value") or "")
                else:
                    label = str(entry or "")
                if not label or label in seen_dataset_labels:
                    continue
                seen_dataset_labels.add(label)
                dataset_labels_marker.append(escape(label))
            parts = [f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a>"]
            if city_values:
                parts.append("<br/>📍 " + ", ".join(city_values[:3]))
            if dataset_labels_marker:
                parts.append("<br/>💾 " + ", ".join(dataset_labels_marker[:3]))
            if img_html:
                parts.append("<br/>" + img_html)
            if added_html:
                parts.append(f"<br/>Добавил: {added_html}")
            parts.append("</div>")
            popup = "".join(parts)
            marker_js.append(
                f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); "
                f"markers.addLayer(m); bounds.extend([{lat},{lon}]); markerByKey[{key!r}]=m;",
            )
        marker_js += [
            "map.addLayer(markers);",
            "if(bounds.isValid()){map.fitBounds(bounds.pad(0.1));}else{map.setView([20,0],2);}",
            "withFiltering(function(filteringState){",
            "  setupListInteractions(map, markerByKey, markers, filteringState);",
            "  bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
            "});",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("if(loadingController && loadingController.finish){ loadingController.finish(); }")
        marker_js.append("withFiltering(function(filteringState){")
        marker_js.append("  setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("  bindFilteringToMarkers(filteringState, map, null, {});")
        marker_js.append("});")

    return _map_html(
        title,
        list_html,
        marker_js,
        stats={
            "summary_text": summary_text,
            "total": total,
            "with_coords": with_coords,
            "without_coords": total - with_coords,
        },
    )


def _map_html(
    title: str,
    list_html: str,
    marker_js: List[str],
    stats: Optional[Dict[str, Any]] = None,
) -> str:
    # Надёжная загрузка Leaflet + MarkerCluster с fallback и инициализацией после DOMContentLoaded
    stats = stats or {}
    summary_text = stats.get("summary_text", "")
    total = int(stats.get("total", 0) or 0)
    with_coords = int(stats.get("with_coords", 0) or 0)
    without_coords = int(stats.get("without_coords", max(total - with_coords, 0)))
    summary_attrs = (
        f" data-total=\"{total}\" data-withcoords=\"{with_coords}\""
        f" data-withoutcoords=\"{without_coords}\" data-text=\"{escape(summary_text)}\""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <title>{escape(title)}</title>
  <link rel="dns-prefetch" href="https://unpkg.com"/>
  <link rel="preconnect" href="https://unpkg.com" crossorigin/>
  <link rel="dns-prefetch" href="https://a.tile.openstreetmap.org"/>
  <link rel="dns-prefetch" href="https://b.tile.openstreetmap.org"/>
  <link rel="dns-prefetch" href="https://c.tile.openstreetmap.org"/>
  <link rel="preconnect" href="https://a.tile.openstreetmap.org" crossorigin/>
  <link rel="preconnect" href="https://b.tile.openstreetmap.org" crossorigin/>
  <link rel="preconnect" href="https://c.tile.openstreetmap.org" crossorigin/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
  <style>
    html, body {{ height:100%; margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
    .layout {{ display:flex; height:100vh; }}
    #map {{ flex: 1 1 auto; min-height: 320px; }}
    .panel {{ width: 420px; max-width: 48vw; border-left:1px solid #e5e7eb; background:#fafafa; overflow:auto; display:flex; flex-direction:column; }}
    .panel .head {{ position: sticky; top:0; background:#fff; border-bottom:1px solid #e5e7eb; padding:12px 14px 10px; z-index:1; }}
    .panel .title {{ font-weight:600; margin-bottom:6px; }}
    .panel .summary {{ font-size:12px; color:#4b5563; margin-bottom:10px; }}
    .panel .loading {{ font-size:12px; color:#1f2937; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; padding:10px 12px; margin-bottom:12px; box-shadow:0 1px 2px rgba(15,23,42,0.08); display:flex; flex-direction:column; gap:6px; }}
    .panel .loading[hidden] {{ display:none !important; }}
    .panel .loading__label {{ font-weight:600; display:flex; align-items:center; gap:6px; }}
    .panel .loading__label::before {{ content:"⏳"; }}
    .panel .loading__bar {{ display:flex; align-items:center; gap:8px; }}
    .panel .loading progress {{ flex:1 1 auto; width:100%; height:8px; accent-color:#2563eb; }}
    .panel .loading__details {{ min-width:80px; text-align:right; font-variant-numeric:tabular-nums; color:#4b5563; font-size:11px; }}
    .panel .search label {{ display:block; font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px; }}
    .panel .search input {{ width:100%; padding:6px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:14px; }}
    .panel .filters {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap:8px; margin-top:12px; }}
    .panel .filters .field {{ display:flex; flex-direction:column; gap:4px; font-size:12px; }}
    .panel .filters .field label {{ color:#6b7280; text-transform:uppercase; letter-spacing:0.05em; font-size:11px; }}
    .panel .filters .field input,
    .panel .filters .field select {{ padding:6px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:13px; background:#fff; }}
    .panel .filters .field--button {{ align-self:end; }}
    .panel .filters .field--button button {{ padding:6px 8px; border:1px solid #bfdbfe; background:#e0f2fe; color:#1d4ed8; border-radius:6px; font-size:13px; cursor:pointer; }}
    .panel .filters .field--button button:hover {{ background:#bfdbfe; }}
    .panel .rows {{ flex:1 1 auto; }}
    .panel .row {{ padding:10px 14px; border-bottom:1px dashed #e5e7eb; display:grid; grid-template-columns:auto 80px 1fr; gap:8px; align-items:center; transition:background 0.2s ease; }}
    .panel .row.row-user {{ grid-template-columns: 1fr; }}
    .panel .row[data-key] {{ cursor:pointer; }}
    .panel .row:hover {{ background:#f3f4f6; }}
    .panel .row.active {{ background:#e0f2fe; box-shadow:inset 0 0 0 1px #bae6fd; }}
    .panel .row .u a {{ font-weight:600; color:#111; text-decoration:none; }}
    .panel .row .c {{ font-size: 13px; color:#111; grid-column:1 / -1; }}
    .panel .row .meta {{ grid-column:1 / -1; font-size:11px; color:#4b5563; display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; }}
    .panel .row .meta .chip {{ display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:999px; background:#e5e7eb; color:#374151; font-size:11px; font-weight:500; }}
    .panel .row .meta .chip-city {{ background:#dbeafe; color:#1d4ed8; }}
    .panel .row .meta .chip-city::before {{ content:"📍"; }}
    .panel .row .meta .chip-data {{ background:#fef3c7; color:#92400e; }}
    .panel .row .meta .chip-data::before {{ content:"💾"; }}
    .panel .row .created-range {{ grid-column:1 / -1; font-size:11px; color:#4b5563; }}
    .panel .row .ab {{ grid-column:1 / -1; font-size:12px; color:#4b5563; }}
    .panel .row.row-user .u {{ grid-column:1 / -1; }}
    @media (max-width: 900px) {{
      .layout {{ flex-direction: column; }}
      .panel {{ width: 100%; max-width: 100%; height: 46vh; }}
      #map {{ height: 54vh; }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <div id="map"></div>
    <div class="panel">
      <div class="head">
        <div class="title">Список / превью</div>
        <div class="summary" id="summary"{summary_attrs}>{escape(summary_text)}</div>
        <div class="loading" id="loadingIndicator" hidden>
          <div class="loading__label">Загрузка карты…</div>
          <div class="loading__bar">
            <progress id="loadingProgress" max="100" value="0"></progress>
            <span class="loading__details" id="loadingDetails">0%</span>
          </div>
        </div>
        <div class="search">
          <label for="filter">Поиск</label>
          <input id="filter" type="search" placeholder="Поиск по нику, комментариям или добавившему" autocomplete="off"/>
        </div>
        <div class="filters">
          <div class="field">
            <label for="filterData">Данные</label>
            <select id="filterData">
              <option value="">Все данные</option>
            </select>
          </div>
          <div class="field">
            <label for="filterCity">Город</label>
            <input id="filterCity" type="search" list="filterCityOptions" placeholder="Начните вводить город или выберите из списка" autocomplete="off"/>
            <datalist id="filterCityOptions"></datalist>
          </div>
          <div class="field">
            <label for="filterComment">Комментарий</label>
            <input id="filterComment" type="search" placeholder="Фильтр по тексту комментария" autocomplete="off"/>
          </div>
          <div class="field">
            <label for="filterHasComments">Наличие комментариев</label>
            <select id="filterHasComments">
              <option value="">Все</option>
              <option value="with">Только с комментариями</option>
              <option value="without">Без комментариев</option>
            </select>
          </div>
          <div class="field">
            <label for="filterAdded">Добавивший</label>
            <select id="filterAdded">
              <option value="">Все добавившие</option>
            </select>
          </div>
          <div class="field">
            <label for="filterDateFrom">Дата от</label>
            <input id="filterDateFrom" type="date" />
          </div>
          <div class="field">
            <label for="filterDateTo">Дата до</label>
            <input id="filterDateTo" type="date" />
          </div>
          <div class="field field--button">
            <label>&nbsp;</label>
            <button id="filtersReset" type="button">Сбросить</button>
          </div>
        </div>
      </div>
      <div class="rows" id="list">{list_html}</div>
    </div>
  </div>
  <script>
    function loadScript(src, onload) {{
      var s=document.createElement('script'); s.src=src; s.onload=onload; s.async=true; document.head.appendChild(s);
    }}
    function ensureLeaflet(next) {{
      if (window.L) return next();
      loadScript("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", function() {{
        if (window.L) return next();
        loadScript("https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js", next);
      }});
    }}
    function ensureCluster(next) {{
      if (window.L && L.MarkerClusterGroup) return next();
      loadScript("https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js", function() {{
        if (window.L && L.MarkerClusterGroup) return next();
        loadScript("https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js", next);
      }});
    }}
    function debounce(fn, delay) {{
      var timer; return function() {{
        var ctx=this, args=arguments; clearTimeout(timer);
        timer=setTimeout(function() {{ fn.apply(ctx,args); }}, delay);
      }};
    }}
    function parseJsonArray(raw) {{
      if (!raw) return [];
      try {{
        var parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
      }} catch (e) {{
        return [];
      }}
    }}
    function parseDatasetList(raw) {{
      var result = [];
      var arr = parseJsonArray(raw);
      arr.forEach(function(entry) {{
        if (!entry) return;
        if (typeof entry === 'string') {{
          result.push({{ value: entry, label: entry }});
        }} else if (typeof entry === 'object') {{
          var value = (entry.value || entry.label || '').toString();
          if (!value) return;
          result.push({{ value: value, label: (entry.label || value).toString() }});
        }}
      }});
      return result;
    }}
    function fillSelectOptions(select, options, placeholder) {{
      if (!select) return;
      var frag=document.createDocumentFragment();
      var optAll=document.createElement('option');
      optAll.value='';
      optAll.textContent=placeholder || 'Все';
      frag.appendChild(optAll);
      Object.keys(options).sort(function(a,b) {{
        var labelA=(options[a]||'').toString();
        var labelB=(options[b]||'').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity:'accent' }});
      }}).forEach(function(value) {{
        var opt=document.createElement('option');
        opt.value=value;
        opt.textContent=options[value];
        frag.appendChild(opt);
      }});
      select.innerHTML='';
      select.appendChild(frag);
    }}
    function fillDatalistOptions(datalist, options) {{
      if (!datalist) return;
      var frag=document.createDocumentFragment();
      Object.keys(options).sort(function(a,b) {{
        var labelA=(options[a]||'').toString();
        var labelB=(options[b]||'').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity:'accent' }});
      }}).forEach(function(value) {{
        var opt=document.createElement('option');
        opt.value=options[value] || value;
        frag.appendChild(opt);
      }});
      datalist.innerHTML='';
      datalist.appendChild(frag);
    }}
    function parseDateValue(raw) {{
      if (!raw && raw !== 0) return null;
      var str=('' + raw).trim();
      if (!str) return null;
      var normalized = str.indexOf('T') !== -1 ? str : str.replace(' ', 'T');
      var ts = Date.parse(normalized);
      if (!Number.isFinite(ts)) {{
        ts = Date.parse(normalized + 'Z');
      }}
      return Number.isFinite(ts) ? ts : null;
    }}
    function formatDateLabel(ts) {{
      if (ts == null || !Number.isFinite(ts)) return '';
      var d = new Date(ts);
      if (Number.isNaN(d.getTime())) return '';
      var y = d.getFullYear();
      var m = String(d.getMonth()+1).padStart(2,'0');
      var day = String(d.getDate()).padStart(2,'0');
      return y + '-' + m + '-' + day;
    }}
    function formatDateInput(ts) {{
      return formatDateLabel(ts);
    }}
    function toDateRangeValue(value, endOfDay) {{
      if (!value) return null;
      var str=('' + value).trim();
      if (!str) return null;
      var base = str.length > 10 ? str : str + (endOfDay ? 'T23:59:59.999' : 'T00:00:00');
      return parseDateValue(base);
    }}
    var filteringReady=false;
    var filteringStateValue=null;
    var filteringCallbacks=[];
    function withFiltering(callback) {{
      if (typeof callback !== 'function') return;
      if (filteringReady) {{
        try {{ callback(filteringStateValue); }} catch (err) {{ if (typeof console !== 'undefined' && console.error) console.error(err); }}
      }} else {{
        filteringCallbacks.push(callback);
      }}
    }}
    function resolveFilteringState(state) {{
      if (filteringReady) return;
      filteringReady=true;
      filteringStateValue=state;
      while (filteringCallbacks.length) {{
        var cb=filteringCallbacks.shift();
        try {{ cb && cb(state); }} catch (err) {{ if (typeof console !== 'undefined' && console.error) console.error(err); }}
      }}
    }}
    function initFilteringAsync() {{
      var run=function() {{
        try {{
          resolveFilteringState(setupFiltering());
        }} catch (err) {{
          if (typeof console !== 'undefined' && console.error) console.error(err);
          resolveFilteringState({{ onChange:function() {{}}, refresh:function() {{}} }});
        }}
      }};
      if (typeof window !== 'undefined' && window.requestIdleCallback) {{
        window.requestIdleCallback(run, {{ timeout: 500 }});
      }} else {{
        setTimeout(run, 0);
      }}
    }}
    function createLoadingController() {{
      var indicator=document.getElementById('loadingIndicator');
      var bar=document.getElementById('loadingProgress');
      var details=document.getElementById('loadingDetails');
      var summary=document.getElementById('summary');
      var totalMarkers=summary ? parseInt(summary.dataset.withcoords || '0', 10) || 0 : 0;
      var displayThreshold=150;
      var active=false;
      var lastUpdate=0;
      function hideIndicator() {{
        if (!indicator) return;
        indicator.hidden=true;
        indicator.setAttribute('aria-hidden','true');
      }}
      function showIndicator() {{
        if (!indicator) return;
        indicator.hidden=false;
        indicator.setAttribute('aria-hidden','false');
      }}
      function formatDetails(processed, total) {{
        if (!details) return;
        if (!total) {{
          details.textContent=processed ? processed.toString() : '…';
          return;
        }}
        var percent=Math.min(100, Math.max(0, Math.round((processed/total)*100)));
        details.textContent=processed + ' / ' + total + ' (' + percent + '%)';
      }}
      if (!indicator || totalMarkers <= displayThreshold) {{
        hideIndicator();
        return {{
          start:function() {{}},
          update:function() {{}},
          finish:function() {{ hideIndicator(); }}
        }};
      }}
      hideIndicator();
      return {{
        start:function(total) {{
          var target=total && total>0 ? total : totalMarkers;
          active=true;
          showIndicator();
          if (bar) {{
            bar.max=100;
            bar.value=0;
          }}
          formatDetails(0, target);
          lastUpdate=Date.now();
        }},
        update:function(processed, total) {{
          if (!active) this.start(total);
          var target=total && total>0 ? total : totalMarkers;
          var percent=target ? Math.min(100, Math.max(0, Math.round((processed/target)*100))) : 0;
          if (bar) {{
            bar.max=100;
            bar.value=percent;
          }}
          formatDetails(processed, target || 0);
          lastUpdate=Date.now();
        }},
        finish:function() {{
          if (!indicator) return;
          if (bar) {{
            bar.max=100;
            bar.value=100;
          }}
          if (totalMarkers) {{
            formatDetails(totalMarkers, totalMarkers);
          }} else if (details) {{
            details.textContent='Готово';
          }}
          var delay=Math.max(0, 400 - (Date.now()-lastUpdate));
          setTimeout(hideIndicator, delay);
        }}
      }};
    }}
    function setupFiltering() {{
      var rows=Array.prototype.slice.call(document.querySelectorAll('.panel .row'));
      var summary=document.getElementById('summary');
      var input=document.getElementById('filter');
      var datasetSelect=document.getElementById('filterData');
      var cityInput=document.getElementById('filterCity');
      var cityDatalist=document.getElementById('filterCityOptions');
      var commentInput=document.getElementById('filterComment');
      var hasCommentsSelect=document.getElementById('filterHasComments');
      var addedSelect=document.getElementById('filterAdded');
      var dateFromInput=document.getElementById('filterDateFrom');
      var dateToInput=document.getElementById('filterDateTo');
      var resetBtn=document.getElementById('filtersReset');
      var baseText = summary ? (summary.dataset.text || summary.textContent || '') : '';
      var totals = summary ? {{
        total: parseInt(summary.dataset.total || rows.length, 10) || rows.length,
        withCoords: parseInt(summary.dataset.withcoords || 0, 10) || 0
      }} : {{ total: rows.length, withCoords: rows.filter(function(r) {{ return r.dataset.hasCoords==='1'; }}).length }};
      var datasetOptions={{}};
      var cityOptions={{}};
      var cityLookup={{}};
      var addedOptions={{}};
      var globalMinTs=null;
      var globalMaxTs=null;
      var changeHandlers=[];
      var lastKeys=[];
      rows.forEach(function(row) {{
        row._searchText=(row.dataset.search || '').toString();
        row._hasCoords=row.dataset.hasCoords==='1';
        row._commentsText=(row.dataset.comments || '').toString();
        row._addedText=(row.dataset.added || '').toString();
        row._addedKey=(row.dataset.addedKey || '').toString();
        row._addedLabel=(row.dataset.addedLabel || '').toString();
        row._commentsCount=parseInt(row.dataset.commentsCount || '0', 10) || 0;
        row._datasets=parseDatasetList(row.dataset.datasets);
        row._cities=parseJsonArray(row.dataset.cities).map(function(city) {{ return city ? city.toString() : ''; }}).filter(function(city) {{ return !!city; }});
        row._firstTs=parseDateValue(row.dataset.firstCreated);
        row._lastTs=parseDateValue(row.dataset.lastCreated);
        row._minTs=row._firstTs!=null ? row._firstTs : (row._lastTs!=null ? row._lastTs : null);
        row._maxTs=row._lastTs!=null ? row._lastTs : (row._firstTs!=null ? row._firstTs : null);
        var candidateMin=row._minTs!=null ? row._minTs : (row._maxTs!=null ? row._maxTs : null);
        var candidateMax=row._maxTs!=null ? row._maxTs : (row._minTs!=null ? row._minTs : null);
        if (candidateMin!=null) {{
          if (globalMinTs==null || candidateMin<globalMinTs) globalMinTs=candidateMin;
        }}
        if (candidateMax!=null) {{
          if (globalMaxTs==null || candidateMax>globalMaxTs) globalMaxTs=candidateMax;
        }}
        row._datasets.forEach(function(ds) {{
          var value=(ds.value || '').toString();
          if (!value) return;
          var label=(ds.label || value).toString();
          if (!datasetOptions[value]) datasetOptions[value]=label;
        }});
        row._cities.forEach(function(city) {{
          if (!cityOptions[city]) cityOptions[city]=city;
          var lowerCity=city.toLowerCase();
          if (lowerCity && !cityLookup[lowerCity]) cityLookup[lowerCity]=city;
        }});
        if (row._addedKey && !addedOptions[row._addedKey]) {{
          addedOptions[row._addedKey]=row._addedLabel || row._addedKey;
        }}
      }});
      fillSelectOptions(datasetSelect, datasetOptions, 'Все данные');
      fillDatalistOptions(cityDatalist, cityOptions);
      fillSelectOptions(addedSelect, addedOptions, 'Все добавившие');
      var minDateLabel=formatDateInput(globalMinTs);
      var maxDateLabel=formatDateInput(globalMaxTs);
      if (dateFromInput) {{
        dateFromInput.min=minDateLabel || '';
        dateFromInput.max=maxDateLabel || '';
      }}
      if (dateToInput) {{
        dateToInput.min=minDateLabel || '';
        dateToInput.max=maxDateLabel || '';
      }}
      function notify(keys) {{
        lastKeys=keys.slice();
        changeHandlers.forEach(function(fn) {{
          try {{ fn(keys.slice()); }} catch (e) {{}}
        }});
      }}
      function applyFilter() {{
        var q=(input && input.value ? input.value : '').trim().toLowerCase();
        var dsValue=datasetSelect ? datasetSelect.value : '';
        var dsValueLower=dsValue ? dsValue.toLowerCase() : '';
        var cityRaw=cityInput && cityInput.value ? cityInput.value.trim() : '';
        var cityValueLower=cityRaw.toLowerCase();
        var hasExactCity = cityValueLower && Object.prototype.hasOwnProperty.call(cityLookup, cityValueLower);
        var commentValue=(commentInput && commentInput.value ? commentInput.value : '').trim().toLowerCase();
        var hasCommentsValue=hasCommentsSelect ? hasCommentsSelect.value : '';
        var addedKey=(addedSelect && addedSelect.value ? addedSelect.value : '');
        var dateFromValue=(dateFromInput && dateFromInput.value ? dateFromInput.value : '').trim();
        var dateToValue=(dateToInput && dateToInput.value ? dateToInput.value : '').trim();
        var fromTs=dateFromValue ? toDateRangeValue(dateFromValue, false) : null;
        var toTs=dateToValue ? toDateRangeValue(dateToValue, true) : null;
        var visible=[];
        var visibleKeys=[];
        rows.forEach(function(row) {{
          var match=true;
          if (match && q && row._searchText.indexOf(q)===-1) match=false;
          if (match && dsValue) {{
            match=row._datasets && row._datasets.some(function(ds) {{
              var val=(ds.value || '').toString().toLowerCase();
              var label=(ds.label || '').toString().toLowerCase();
              return val===dsValueLower || label===dsValueLower;
            }});
          }}
          if (match && cityValueLower) {{
            if (hasExactCity) {{
              match=row._cities && row._cities.some(function(city) {{
                return city.toLowerCase()===cityValueLower;
              }});
            }} else {{
              match=row._cities && row._cities.some(function(city) {{
                return city.toLowerCase().indexOf(cityValueLower)!==-1;
              }});
            }}
          }}
          if (match && commentValue) {{
            match=row._commentsText.indexOf(commentValue)!==-1;
          }}
          if (match && hasCommentsValue==='with') {{
            match=row._commentsCount>0;
          }} else if (match && hasCommentsValue==='without') {{
            match=row._commentsCount===0;
          }}
          if (match && addedKey) {{
            match=row._addedKey===addedKey;
          }}
          if (match && fromTs!=null) {{
            var newest=row._maxTs!=null ? row._maxTs : (row._minTs!=null ? row._minTs : null);
            if (newest==null || newest<fromTs) match=false;
          }}
          if (match && toTs!=null) {{
            var oldest=row._minTs!=null ? row._minTs : (row._maxTs!=null ? row._maxTs : null);
            if (oldest==null || oldest>toTs) match=false;
          }}
          row.style.display = match ? '' : 'none';
          if (match) {{
            visible.push(row);
            if (row.dataset.key) visibleKeys.push(row.dataset.key);
          }}
        }});
        if (summary) {{
          if (!q && !dsValue && !cityValueLower && !commentValue && !addedKey && !hasCommentsValue && !dateFromValue && !dateToValue) {{
            summary.textContent = baseText;
          }} else {{
            var coordsShown = visible.filter(function(row) {{ return row._hasCoords; }}).length;
            summary.textContent = visible.length + ' из ' + totals.total + ' записей' + ' • С координатами: ' + coordsShown;
          }}
        }}
        notify(visibleKeys);
      }}
      var debouncedApply=debounce(applyFilter, 150);
      if (input) input.addEventListener('input', debouncedApply);
      if (cityInput) cityInput.addEventListener('input', debouncedApply);
      if (commentInput) commentInput.addEventListener('input', debouncedApply);
      if (datasetSelect) datasetSelect.addEventListener('change', applyFilter);
      if (cityInput) cityInput.addEventListener('change', applyFilter);
      if (addedSelect) addedSelect.addEventListener('change', applyFilter);
      if (hasCommentsSelect) hasCommentsSelect.addEventListener('change', applyFilter);
      if (dateFromInput) {{
        dateFromInput.addEventListener('input', debouncedApply);
        dateFromInput.addEventListener('change', applyFilter);
      }}
      if (dateToInput) {{
        dateToInput.addEventListener('input', debouncedApply);
        dateToInput.addEventListener('change', applyFilter);
      }}
      if (resetBtn) resetBtn.addEventListener('click', function() {{
        if (input) input.value='';
        if (datasetSelect) datasetSelect.value='';
        if (cityInput) cityInput.value='';
        if (commentInput) commentInput.value='';
        if (addedSelect) addedSelect.value='';
        if (hasCommentsSelect) hasCommentsSelect.value='';
        if (dateFromInput) dateFromInput.value='';
        if (dateToInput) dateToInput.value='';
        applyFilter();
      }});
      applyFilter();
      return {{
        onChange: function(handler) {{
          if (typeof handler === 'function') {{
            changeHandlers.push(handler);
            handler(lastKeys.slice());
          }}
        }},
        refresh: applyFilter
      }};
    }}
    function setupListInteractions(map, markerByKey, clusterGroup, filteringState) {{
      var rows=Array.prototype.slice.call(document.querySelectorAll('.panel .row'));
      var activeRow=null;
      var TARGET_ZOOM=15;
      function activate(row) {{
        if (activeRow && activeRow!==row) activeRow.classList.remove('active');
        if (row) {{
          row.classList.add('active');
          activeRow = row;
          try {{ row.scrollIntoView({{ behavior:'smooth', block:'center', inline:'nearest' }}); }} catch (e) {{ row.scrollIntoView({{ block:'center' }}); }}
        }}
      }}
      function focusMarker(marker) {{
        if (!map || !marker) return;
        var finalize=function() {{
          var latlng = marker.getLatLng && marker.getLatLng();
          if (latlng) {{
            var zoom = map.getZoom ? map.getZoom() : TARGET_ZOOM;
            if (typeof zoom !== 'number' || zoom < TARGET_ZOOM) zoom = TARGET_ZOOM;
            if (map.flyTo) map.flyTo(latlng, zoom); else map.setView(latlng, zoom);
          }}
          if (marker.openPopup) marker.openPopup();
        }};
        if (clusterGroup && clusterGroup.hasLayer && !clusterGroup.hasLayer(marker)) {{
          clusterGroup.addLayer(marker);
        }}
        if (clusterGroup && clusterGroup.zoomToShowLayer) {{
          clusterGroup.zoomToShowLayer(marker, finalize);
        }} else {{
          finalize();
        }}
      }}
      rows.forEach(function(row) {{
        var key=row.dataset.key;
        if (!key || !markerByKey || !markerByKey[key]) return;
        row.addEventListener('click', function() {{
          focusMarker(markerByKey[key]);
          activate(row);
        }});
      }});
      if (markerByKey) {{
        Object.keys(markerByKey).forEach(function(key) {{
          var marker=markerByKey[key];
          if (!marker || !marker.on) return;
          marker.on('click', function() {{
            focusMarker(marker);
            var row=document.querySelector('.panel .row[data-key="'+key+'"]');
            if (row) activate(row);
          }});
        }});
      }}
      if (filteringState && filteringState.onChange) {{
        filteringState.onChange(function() {{
          if (activeRow && activeRow.style.display==='none') {{
            activeRow.classList.remove('active');
            activeRow=null;
          }}
        }});
      }}
    }}
    function bindFilteringToMarkers(filteringState, map, clusterGroup, markerByKey) {{
      if (!filteringState || !filteringState.onChange) return;
      filteringState.onChange(function(visibleKeys) {{
        if (!markerByKey) return;
        var visibleSet={{}};
        (visibleKeys || []).forEach(function(key) {{
          if (key) visibleSet[key]=true;
        }});
        if (clusterGroup && clusterGroup.hasLayer) {{
          Object.keys(markerByKey).forEach(function(key) {{
            var marker=markerByKey[key];
            if (!marker) return;
            var shouldShow=!!visibleSet[key];
            var hasLayer=clusterGroup.hasLayer(marker);
            if (shouldShow && !hasLayer) {{
              clusterGroup.addLayer(marker);
            }} else if (!shouldShow && hasLayer) {{
              clusterGroup.removeLayer(marker);
            }}
          }});
        }} else if (map && map.addLayer && map.removeLayer) {{
          Object.keys(markerByKey).forEach(function(key) {{
            var marker=markerByKey[key];
            if (!marker) return;
            var shouldShow=!!visibleSet[key];
            var onMap=map.hasLayer ? map.hasLayer(marker) : false;
            if (shouldShow && !onMap) {{
              map.addLayer(marker);
            }} else if (!shouldShow && onMap) {{
              map.removeLayer(marker);
            }}
          }});
        }}
      }});
    }}
    document.addEventListener('DOMContentLoaded', function() {{
      initFilteringAsync();
      var loadingController=createLoadingController();
      ensureLeaflet(function() {{
        ensureCluster(function() {{
          var map=L.map('map', {{ preferCanvas:true }});
          L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{attribution:'&copy; OpenStreetMap contributors', maxZoom:19, minZoom:1, updateWhenIdle:true, keepBuffer:4, reuseTiles:true, detectRetina:true}}).addTo(map);
          {chr(10).join(marker_js)}
        }});
      }});
    }});
  </script>
</body>
</html>"""
def build_map_users(users: List[Dict[str, Any]], title="VSCO Profiles (Users)"):
    meta: List[Tuple[Dict[str, Any], Optional[float], Optional[float], str, bool]] = []
    list_rows: List[str] = []
    for idx, u in enumerate(users):
        raw_lat = u.get("lat"); raw_lon = u.get("lon")
        try:
            lat = float(raw_lat) if raw_lat is not None else None
            lon = float(raw_lon) if raw_lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        has_coord = lat is not None and lon is not None
        key = f"u{idx}" if has_coord else ""
        meta.append((u, lat, lon, key, has_coord))

        uname = escape(u.get("username", ""))
        link = escape(u.get("profile_url", ""))
        cm = u.get("comments") or []
        comment_count = len(cm)
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:3])
            more = f"<div class='c'>и ещё {len(cm)-3}…</div>" if len(cm) > 3 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"

        added_raw = u.get("added_by_raw", "")
        added_display, _ = added_by_display_and_link(added_raw)
        added_html = added_by_html(added_raw)
        added_block = f"<div class='ab'>Добавил: {added_html or '—'}</div>"
        added_key = (added_display or "").strip().lower()

        raw_datasets = u.get("datasets") or []
        dataset_payload: List[Dict[str, str]] = []
        dataset_labels: List[str] = []
        seen_dataset: set[str] = set()
        for entry in raw_datasets:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                label = str(entry.get("label") or value)
            else:
                value = str(entry or "")
                label = value
            if not value or value in seen_dataset:
                continue
            seen_dataset.add(value)
            dataset_labels.append(label)
            dataset_payload.append({"value": value, "label": label})

        raw_cities = [str(city) for city in (u.get("cities") or []) if city]

        search_parts = [str(u.get("username") or "")]
        search_parts.extend(str(x or "") for x in cm)
        if added_display:
            search_parts.append(added_display)
        search_parts.extend(dataset_labels)
        search_parts.extend(raw_cities)
        search_text = " ".join(p.strip() for p in search_parts if p).lower()

        comment_filter_text = " ".join(str(x or "") for x in cm).lower()
        added_filter_text = (added_display or "").lower()

        first_created = _normalize_created_value(u.get("first_created"))
        last_created = _normalize_created_value(u.get("last_created"))
        created_label = _format_created_range(u.get("first_created"), u.get("last_created"))
        created_block = (
            f"<div class='created-range'>Добавлено: {escape(created_label)}</div>"
            if created_label
            else ""
        )

        attrs = [
            f"data-has-coords=\"{1 if has_coord else 0}\"",
            f"data-search=\"{escape(search_text, quote=True)}\"",
            f"data-comments=\"{escape(comment_filter_text, quote=True)}\"",
            f"data-added=\"{escape(added_filter_text, quote=True)}\"",
            f"data-cities=\"{escape(json.dumps(raw_cities, ensure_ascii=False), quote=True)}\"",
            f"data-datasets=\"{escape(json.dumps(dataset_payload, ensure_ascii=False), quote=True)}\"",
            f"data-comments-count=\"{comment_count}\"",
            f"data-added-key=\"{escape(added_key, quote=True)}\"",
            f"data-added-label=\"{escape(added_display or '', quote=True)}\"",
        ]
        if first_created:
            attrs.append(f"data-first-created=\"{escape(first_created, quote=True)}\"")
        if last_created:
            attrs.append(f"data-last-created=\"{escape(last_created, quote=True)}\"")
        if has_coord:
            attrs.extend(
                [
                    f"data-key=\"{key}\"",
                    f"data-lat=\"{lat:.6f}\"",
                    f"data-lon=\"{lon:.6f}\"",
                ]
            )
        meta_chips: List[str] = []
        for city in raw_cities[:3]:
            meta_chips.append(f"<span class='chip chip-city'>{escape(city)}</span>")
        for label in dataset_labels[:3]:
            meta_chips.append(f"<span class='chip chip-data'>{escape(label)}</span>")
        meta_block = f"<div class='meta'>{''.join(meta_chips)}</div>" if meta_chips else ""

        attr_html = " " + " ".join(attrs)
        list_rows.append(
            f"""
          <div class=\"row row-user\"{attr_html}>
            <div class=\"u\"><a href=\"{link}\" target=\"_blank\">@{uname}</a></div>
            {meta_block}
            {cm_txt}
            {created_block}
            {added_block}
          </div>"""
        )

    list_html = "".join(list_rows)
    total = len(users)
    with_coords = sum(1 for _, _, _, _, hc in meta if hc)
    summary_text = (
        "Нет данных"
        if total == 0
        else f"Пользователи: {total} • С координатами: {with_coords} • Без координат: {total - with_coords}"
    )

    marker_js: List[str] = []
    if with_coords:
        marker_js += [
            f"if(loadingController && loadingController.start){{ loadingController.start({with_coords}); }}",
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup({chunkedLoading:true,chunkDelay:20,chunkInterval:200,removeOutsideVisibleBounds:true,spiderfyDistanceMultiplier:1.1,chunkProgress:function(processed,total){ if(loadingController && loadingController.update){ loadingController.update(processed,total); } }});",
            "var markerByKey={};",
            "if(markers.on){ markers.on('chunkedLoadingEnd', function(){ if(loadingController && loadingController.finish){ loadingController.finish(); }}); } else if(loadingController && loadingController.finish){ loadingController.finish(); }",
        ]
        for u, lat, lon, key, has_coord in meta:
            if not has_coord:
                continue
            uname = escape(str(u.get("username") or ""))
            prof = escape(str(u.get("profile_url") or ""))
            added_html = added_by_html(u.get("added_by_raw", ""))
            city_values: List[str] = []
            for city in u.get("cities") or []:
                text = str(city)
                if text:
                    city_values.append(escape(text))
            dataset_labels_marker: List[str] = []
            seen_dataset_labels: set[str] = set()
            for entry in u.get("datasets") or []:
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("value") or "")
                else:
                    label = str(entry or "")
                if not label or label in seen_dataset_labels:
                    continue
                seen_dataset_labels.add(label)
                dataset_labels_marker.append(escape(label))
            parts = [f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a>"]
            if city_values:
                parts.append("<br/>📍 " + ", ".join(city_values[:3]))
            if dataset_labels_marker:
                parts.append("<br/>💾 " + ", ".join(dataset_labels_marker[:3]))
            if added_html:
                parts.append(f"<br/>Добавил: {added_html}")
            parts.append("</div>")
            popup = "".join(parts)
            marker_js.append(
                f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); "
                f"markers.addLayer(m); bounds.extend([{lat},{lon}]); markerByKey[{key!r}]=m;",
            )
        marker_js += [
            "map.addLayer(markers);",
            "if(bounds.isValid()){map.fitBounds(bounds.pad(0.1));}else{map.setView([20,0],2);}",
            "withFiltering(function(filteringState){",
            "  setupListInteractions(map, markerByKey, markers, filteringState);",
            "  bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
            "});",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("if(loadingController && loadingController.finish){ loadingController.finish(); }")
        marker_js.append("withFiltering(function(filteringState){")
        marker_js.append("  setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("  bindFilteringToMarkers(filteringState, map, null, {});")
        marker_js.append("});")

    return _map_html(
        title,
        list_html,
        marker_js,
        stats={
            "summary_text": summary_text,
            "total": total,
            "with_coords": with_coords,
            "without_coords": total - with_coords,
        },
    )

def build_map_images(items: List[Dict[str, Any]], title="VSCO Profiles (Images)"):
    meta: List[Tuple[Dict[str, Any], Optional[float], Optional[float], str, bool]] = []
    rows: List[str] = []
    for idx, r in enumerate(items):
        raw_lat = r.get("lat"); raw_lon = r.get("lon")
        try:
            lat = float(raw_lat) if raw_lat is not None else None
            lon = float(raw_lon) if raw_lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        has_coord = lat is not None and lon is not None
        key = f"i{idx}" if has_coord else ""
        meta.append((r, lat, lon, key, has_coord))

        uname = escape(r.get("username", ""))
        link = escape(r.get("profile_url", ""))
        img = r.get("image_url") or ""
        thumb_html = (
            f"<img src='{escape(img)}' loading='lazy' style='width:68px;height:68px;object-fit:cover;border-radius:8px;border:1px solid #eee;'/>"
            if img
            else ""
        )
        cm = r.get("comments") or []
        comment_count = len(cm)
        cm_txt = ""
        if not cm:
            cm_txt = "<div class='c'>нет комментариев</div>"
        else:
            head = "".join(f"<li>{escape(x) if x else ''}</li>" for x in cm[:2])
            more = f"<div class='c'>и ещё {len(cm)-2}…</div>" if len(cm) > 2 else ""
            cm_txt = f"<div class='c'><ul>{head}</ul>{more}</div>"

        added_raw = r.get("added_by_raw", "")
        added_display, _ = added_by_display_and_link(added_raw)
        added_html = added_by_html(added_raw)
        added_block = f"<div class='ab'>Добавил: {added_html or '—'}</div>"
        added_key = (added_display or "").strip().lower()

        raw_datasets = r.get("datasets") or []
        dataset_payload: List[Dict[str, str]] = []
        dataset_labels: List[str] = []
        seen_dataset: set[str] = set()
        for entry in raw_datasets:
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                label = str(entry.get("label") or value)
            else:
                value = str(entry or "")
                label = value
            if not value or value in seen_dataset:
                continue
            seen_dataset.add(value)
            dataset_labels.append(label)
            dataset_payload.append({"value": value, "label": label})

        raw_cities = [str(city) for city in (r.get("cities") or []) if city]

        search_parts = [str(r.get("username") or ""), str(img or "")]
        search_parts.extend(str(x or "") for x in cm)
        if added_display:
            search_parts.append(added_display)
        search_parts.extend(dataset_labels)
        search_parts.extend(raw_cities)
        search_text = " ".join(p.strip() for p in search_parts if p).lower()

        comment_filter_text = " ".join(str(x or "") for x in cm).lower()
        added_filter_text = (added_display or "").lower()
        created_single = _normalize_created_value(r.get("created_at"))
        created_block = (
            f"<div class='created-range'>Добавлено: {escape(created_single)}</div>"
            if created_single
            else ""
        )

        attrs = [
            f"data-has-coords=\"{1 if has_coord else 0}\"",
            f"data-search=\"{escape(search_text, quote=True)}\"",
            f"data-comments=\"{escape(comment_filter_text, quote=True)}\"",
            f"data-added=\"{escape(added_filter_text, quote=True)}\"",
            f"data-cities=\"{escape(json.dumps(raw_cities, ensure_ascii=False), quote=True)}\"",
            f"data-datasets=\"{escape(json.dumps(dataset_payload, ensure_ascii=False), quote=True)}\"",
            f"data-comments-count=\"{comment_count}\"",
            f"data-added-key=\"{escape(added_key, quote=True)}\"",
            f"data-added-label=\"{escape(added_display or '', quote=True)}\"",
        ]
        if created_single:
            attrs.append(f"data-first-created=\"{escape(created_single, quote=True)}\"")
            attrs.append(f"data-last-created=\"{escape(created_single, quote=True)}\"")
        if has_coord:
            attrs.extend(
                [
                    f"data-key=\"{key}\"",
                    f"data-lat=\"{lat:.6f}\"",
                    f"data-lon=\"{lon:.6f}\"",
                ]
            )
        meta_chips: List[str] = []
        for city in raw_cities[:3]:
            meta_chips.append(f"<span class='chip chip-city'>{escape(city)}</span>")
        for label in dataset_labels[:3]:
            meta_chips.append(f"<span class='chip chip-data'>{escape(label)}</span>")
        meta_block = f"<div class='meta'>{''.join(meta_chips)}</div>" if meta_chips else ""

        attr_html = " " + " ".join(attrs)
        rows.append(
            f"""
          <div class=\"row row-image\"{attr_html}>
            <div class=\"u\"><a href=\"{link}\" target=\"_blank\">@{uname}</a></div>
            <div class=\"t\">{thumb_html}</div>
            {meta_block}
            {cm_txt}
            {created_block}
            {added_block}
          </div>"""
        )

    list_html = "".join(rows)
    total = len(items)
    with_coords = sum(1 for _, _, _, _, hc in meta if hc)
    unique_users = len({str(r.get("username") or "") for r in items if r.get("username")})
    summary_text = (
        "Нет данных"
        if total == 0
        else f"Фотографии: {total} • Пользователи: {unique_users} • С координатами: {with_coords}"
    )

    marker_js: List[str] = []
    if with_coords:
        marker_js += [
            f"if(loadingController && loadingController.start){{ loadingController.start({with_coords}); }}",
            "var bounds=L.latLngBounds();",
            "var markers=L.markerClusterGroup({chunkedLoading:true,chunkDelay:20,chunkInterval:200,removeOutsideVisibleBounds:true,spiderfyDistanceMultiplier:1.1,chunkProgress:function(processed,total){ if(loadingController && loadingController.update){ loadingController.update(processed,total); } }});",
            "var markerByKey={};",
            "if(markers.on){ markers.on('chunkedLoadingEnd', function(){ if(loadingController && loadingController.finish){ loadingController.finish(); }}); } else if(loadingController && loadingController.finish){ loadingController.finish(); }",
        ]
        for r, lat, lon, key, has_coord in meta:
            if not has_coord:
                continue
            uname = escape(str(r.get("username") or ""))
            prof = escape(str(r.get("profile_url") or ""))
            img = r.get("image_url") or ""
            img_html = (
                f"<img src='{escape(img)}' loading='lazy' style='width:140px;height:140px;object-fit:cover;border-radius:10px;border:1px solid #eee;'/>"
                if img
                else ""
            )
            added_html = added_by_html(r.get("added_by_raw", ""))
            city_values: List[str] = []
            for city in r.get("cities") or []:
                text = str(city)
                if text:
                    city_values.append(escape(text))
            dataset_labels_marker: List[str] = []
            seen_dataset_labels: set[str] = set()
            for entry in r.get("datasets") or []:
                if isinstance(entry, dict):
                    label = str(entry.get("label") or entry.get("value") or "")
                else:
                    label = str(entry or "")
                if not label or label in seen_dataset_labels:
                    continue
                seen_dataset_labels.add(label)
                dataset_labels_marker.append(escape(label))
            parts = [f"<div><b>@{uname}</b><br/><a href='{prof}' target='_blank'>{prof}</a>"]
            if city_values:
                parts.append("<br/>📍 " + ", ".join(city_values[:3]))
            if dataset_labels_marker:
                parts.append("<br/>💾 " + ", ".join(dataset_labels_marker[:3]))
            if img_html:
                parts.append("<br/>" + img_html)
            if added_html:
                parts.append(f"<br/>Добавил: {added_html}")
            parts.append("</div>")
            popup = "".join(parts)
            marker_js.append(
                f"var m=L.marker([{lat},{lon}]).bindPopup({popup!r}); "
                f"markers.addLayer(m); bounds.extend([{lat},{lon}]); markerByKey[{key!r}]=m;",
            )
        marker_js += [
            "map.addLayer(markers);",
            "if(bounds.isValid()){map.fitBounds(bounds.pad(0.1));}else{map.setView([20,0],2);}",
            "withFiltering(function(filteringState){",
            "  setupListInteractions(map, markerByKey, markers, filteringState);",
            "  bindFilteringToMarkers(filteringState, map, markers, markerByKey);",
            "});",
        ]
    else:
        marker_js.append("map.setView([20,0],2);")
        marker_js.append("if(loadingController && loadingController.finish){ loadingController.finish(); }")
        marker_js.append("withFiltering(function(filteringState){")
        marker_js.append("  setupListInteractions(map, {}, null, filteringState);")
        marker_js.append("  bindFilteringToMarkers(filteringState, map, null, {});")
        marker_js.append("});")

    return _map_html(
        title,
        list_html,
        marker_js,
        stats={
            "summary_text": summary_text,
            "total": total,
            "with_coords": with_coords,
            "without_coords": total - with_coords,
        },
    )
