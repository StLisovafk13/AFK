"""Rich gallery HTML builders."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Dict, List

def build_rich_gallery(users: List[Dict[str, Any]], title="VSCO Gallery", subtitle=""):
    data_json = json.dumps(users, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\"/>
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; margin:0; background:#f5f6f8; color:#111; }}
    .wrap {{ max-width: 1400px; margin: 24px auto; padding: 0 16px; }}
    h1 {{ margin: 0 0 4px 0; }}
    .sub {{ color:#6b7280; margin-bottom: 16px; }}
    .toolbar {{
      display:grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap:12px;
      margin-bottom:16px;
      background:#fff;
      padding:16px;
      border-radius:16px;
      box-shadow:0 1px 4px rgba(15,23,42,0.08);
    }}
    .toolbar .field {{ display:flex; flex-direction:column; gap:6px; font-size:12px; color:#6b7280; }}
    .toolbar .field.inline {{ flex-direction:row; align-items:center; gap:8px; }}
    .toolbar label {{ font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:#6b7280; }}
    .toolbar input,.toolbar select,.toolbar button {{ padding:8px 10px; border:1px solid #e5e7eb; border-radius:8px; background:#fff; font-size:13px; color:#111827; }}
    .toolbar input[type="number"] {{ font-variant-numeric: tabular-nums; }}
    .toolbar .field--button {{ align-self:flex-end; display:flex; flex-direction:column; justify-content:flex-end; }}
    .toolbar .field--button button {{ width:100%; font-weight:600; cursor:pointer; transition:background .15s ease, color .15s ease; }}
    .toolbar .field--button.primary button {{ background:#111827; color:#fff; }}
    .toolbar .field--button.primary button:hover {{ background:#1f2937; }}
    .toolbar .field--button.secondary button {{ background:#f3f4f6; color:#111827; }}
    .toolbar .field--button.secondary button:hover {{ background:#e5e7eb; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }}
    .chip {{ display:inline-flex; align-items:center; padding:4px 8px; border-radius:999px; font-size:11px; background:#f3f4f6; color:#374151; }}
    .chip-city {{ background:#dbeafe; color:#1d4ed8; }}
    .chip-data {{ background:#dcfce7; color:#047857; }}
    .meta.created {{ color:#4b5563; }}
    .stats {{ color:#6b7280; margin: 6px 0 10px 0; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fill,minmax(300px,1fr)); gap:14px; }}
    .card {{ background:#fff; border-radius:14px; padding:12px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
    .card .head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
    .card .head .name a {{ font-weight:700; text-decoration:none; color:#111; }}
    .btn {{ display:inline-block; padding:6px 10px; border-radius:10px; background:#10b981; color:#fff; text-decoration:none; font-weight:600; }}
    .btn:visited {{ color:#fff; }}
    .btn-secondary {{ display:inline-flex; align-items:center; justify-content:center; padding:6px 12px; border-radius:10px; background:#111827; color:#fff; text-decoration:none; font-weight:600; border:none; cursor:pointer; transition:background .15s ease; }}
    .btn-secondary:hover {{ background:#374151; }}
    .card .actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
    .meta {{ font-size:12px; color:#6b7280; margin:4px 0 8px 0; }}
    .meta.added {{ color:#4b5563; margin-top:2px; }}
    .thumbs {{ display:flex; gap:6px; overflow:hidden; }}
    .thumbs img {{ width:72px; height:120px; object-fit:cover; border-radius:8px; border:1px solid #eee; }}
    .cm {{ margin-top:10px; font-size:13px; }}
    .cm ul {{ margin: 0 0 4px 18px; padding:0; }}
    .cm .empty {{ color:#9ca3af; font-size:12px; }}
    .cm .more {{ color:#6b7280; font-size:12px; }}
    .hidden {{ display:none !important; }}
    .profile-view {{ max-width: 1200px; margin: 0 auto; padding: 32px 16px 60px; }}
    .profile-wrap {{ background:#fff; border-radius:20px; padding:36px; box-shadow:0 24px 40px rgba(15,23,42,0.08); }}
    .profile-back {{ background:none; border:none; color:#2563eb; font-size:14px; font-weight:600; cursor:pointer; padding:0; margin-bottom:24px; display:inline-flex; align-items:center; gap:6px; }}
    .profile-back:hover {{ color:#1d4ed8; }}
    .profile-head {{ display:flex; gap:32px; align-items:center; margin-bottom:24px; }}
    .profile-avatar {{ width:144px; height:144px; border-radius:50%; background:linear-gradient(135deg,#e5e7eb,#d1d5db); border:6px solid #f3f4f6; display:flex; align-items:center; justify-content:center; font-size:48px; font-weight:600; color:#9ca3af; overflow:hidden; background-size:cover; background-position:center; }}
    .profile-avatar.has-image {{ border-color:#fff; color:transparent; }}
    .profile-info {{ flex:1 1 auto; }}
    .profile-username {{ font-size:32px; font-weight:300; margin:0 0 14px 0; display:flex; align-items:center; gap:14px; }}
    .profile-actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }}
    .profile-actions .follow {{ background:#111; color:#fff; border-radius:999px; padding:8px 22px; font-size:13px; letter-spacing:0.08em; text-transform:uppercase; text-decoration:none; font-weight:700; }}
    .profile-actions .follow.disabled {{ pointer-events:none; opacity:0.5; }}
    .profile-actions .open {{ font-size:13px; color:#2563eb; text-decoration:none; font-weight:600; }}
    .profile-actions .open:hover {{ text-decoration:underline; }}
    .profile-meta {{ display:flex; gap:16px; flex-wrap:wrap; font-size:13px; color:#4b5563; margin-bottom:10px; }}
    .profile-meta span {{ display:inline-flex; align-items:center; gap:6px; }}
    .profile-tags {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
    .profile-tags .tag {{ background:#f3f4f6; border-radius:999px; padding:4px 10px; font-size:12px; color:#4b5563; }}
    .profile-cities {{ display:flex; gap:8px; flex-wrap:wrap; font-size:12px; color:#1f2937; margin-bottom:18px; }}
    .profile-cities .chip {{ background:#e0f2fe; color:#1d4ed8; border-radius:999px; padding:4px 12px; }}
    .profile-stats {{ display:flex; gap:32px; margin-bottom:28px; }}
    .profile-stats .stat {{ display:flex; flex-direction:column; font-size:14px; color:#6b7280; }}
    .profile-stats .stat .value {{ font-size:20px; font-weight:600; color:#111827; }}
    .profile-grid {{ display:grid; grid-template-columns: repeat(auto-fill,minmax(220px,1fr)); gap:16px; }}
    .profile-grid .cell {{ position:relative; width:100%; padding-bottom:100%; border-radius:18px; overflow:hidden; background:#f3f4f6; }}
    .profile-grid .cell img {{ position:absolute; top:0; left:0; width:100%; height:100%; object-fit:cover; }}
    .profile-empty {{ text-align:center; font-size:15px; color:#6b7280; padding:40px 0; }}
    @media (max-width: 900px) {{
      .profile-wrap {{ padding:24px; }}
      .profile-head {{ flex-direction:column; align-items:flex-start; }}
      .profile-avatar {{ width:120px; height:120px; }}
      .profile-username {{ font-size:26px; }}
      .profile-grid {{ grid-template-columns: repeat(auto-fill,minmax(160px,1fr)); }}
    }}
  </style>
</head>
<body>
  <div class=\"wrap\" id=\"galleryView\">
    <h1>🎯 {escape(title)}</h1>
    <div class=\"sub\">{escape(subtitle)}</div>

    <div class=\"toolbar\">
      <div class=\"field\">
        <label for=\"q\">Поиск</label>
        <input id=\"q\" placeholder=\"Username, города, комментарии\" />
      </div>
      <div class=\"field\">
        <label for=\"datasetSelect\">Данные</label>
        <select id=\"datasetSelect\"></select>
      </div>
      <div class=\"field\">
        <label for=\"cityInput\">Город</label>
        <input id=\"cityInput\" list=\"cityOptionsList\" placeholder=\"Начните вводить город или выберите из списка\" />
        <datalist id=\"cityOptionsList\"></datalist>
      </div>
      <div class=\"field\">
        <label for=\"commentInput\">Комментарий содержит</label>
        <input id=\"commentInput\" placeholder=\"Текст комментария\" />
      </div>
      <div class=\"field\">
        <label for=\"hasComments\">Комментарии</label>
        <select id=\"hasComments\">
          <option value=\"\">Все</option>
          <option value=\"with\">Есть комментарии</option>
          <option value=\"without\">Без комментариев</option>
        </select>
      </div>
      <div class=\"field\">
        <label for=\"addedSelect\">Добавил</label>
        <select id=\"addedSelect\"></select>
      </div>
      <div class=\"field\">
        <label for=\"dateFrom\">Дата с</label>
        <input id=\"dateFrom\" type=\"date\" />
      </div>
      <div class=\"field\">
        <label for=\"dateTo\">Дата по</label>
        <input id=\"dateTo\" type=\"date\" />
      </div>
      <div class=\"field\">
        <label for=\"sort\">Сортировка</label>
        <select id=\"sort\">
          <option value=\"date_desc\" selected>Новые сначала</option>
          <option value=\"date_asc\">Старые сначала</option>
          <option value=\"img_desc\">Больше медиа</option>
          <option value=\"img_asc\">Меньше медиа</option>
          <option value=\"cm_desc\">Больше комментариев</option>
          <option value=\"cm_asc\">Меньше комментариев</option>
          <option value=\"name_asc\">Username A–Z</option>
          <option value=\"name_desc\">Username Z–A</option>
        </select>
      </div>
      <div class=\"field field--button primary\">
        <button id=\"apply\">Применить</button>
      </div>
      <div class=\"field field--button secondary\">
        <button id=\"reset\" type=\"button\">Сбросить</button>
      </div>
    </div>

    <div class=\"stats\" id=\"stats\"></div>
    <div class=\"grid\" id=\"grid\"></div>
  </div>

  <div class=\"profile-view hidden\" id=\"profileView\">
    <div class=\"profile-wrap\">
      <button class=\"profile-back\" id=\"profileBack\" type=\"button\">← Назад к галерее</button>
      <div class=\"profile-head\">
        <div class=\"profile-avatar\" id=\"profileAvatar\">@</div>
        <div class=\"profile-info\">
          <div class=\"profile-username\" id=\"profileUsername\">@username</div>
          <div class=\"profile-actions\">
            <a class=\"follow\" id=\"profileFollow\" href=\"#\" target=\"_blank\" rel=\"noopener\">FOLLOW</a>
            <a class=\"open\" id=\"profileOpen\" href=\"#\" target=\"_blank\" rel=\"noopener\">Открыть оригинал</a>
          </div>
          <div class=\"profile-meta\" id=\"profileMeta\"></div>
          <div class=\"profile-tags\" id=\"profileDatasets\"></div>
          <div class=\"profile-cities\" id=\"profileCities\"></div>
          <div class=\"profile-meta\" id=\"profileInfoExtra\"></div>
        </div>
      </div>
      <div class=\"profile-stats\" id=\"profileStats\"></div>
      <div class=\"profile-grid\" id=\"profileGrid\"></div>
      <div class=\"profile-empty hidden\" id=\"profileEmpty\">Нет сохранённых фотографий для этого профиля.</div>
    </div>
  </div>

  <script>

    const DATA = {data_json};
    const DATA_MAP = new Map();
    DATA.forEach(u => DATA_MAP.set((u.username || '').toLowerCase(), u));

    const galleryView = document.getElementById('galleryView');
    const profileView = document.getElementById('profileView');
    const profileBack = document.getElementById('profileBack');
    const profileAvatar = document.getElementById('profileAvatar');
    const profileUsername = document.getElementById('profileUsername');
    const profileFollow = document.getElementById('profileFollow');
    const profileOpen = document.getElementById('profileOpen');
    const profileMeta = document.getElementById('profileMeta');
    const profileDatasets = document.getElementById('profileDatasets');
    const profileCities = document.getElementById('profileCities');
    const profileInfoExtra = document.getElementById('profileInfoExtra');
    const profileStats = document.getElementById('profileStats');
    const profileGrid = document.getElementById('profileGrid');
    const profileEmpty = document.getElementById('profileEmpty');

    const searchInput = document.getElementById('q');
    const datasetSelect = document.getElementById('datasetSelect');
    const cityInput = document.getElementById('cityInput');
    const cityDatalist = document.getElementById('cityOptionsList');
    const commentInput = document.getElementById('commentInput');
    const hasCommentsSelect = document.getElementById('hasComments');
    const addedSelect = document.getElementById('addedSelect');
    const dateFromInput = document.getElementById('dateFrom');
    const dateToInput = document.getElementById('dateTo');
    const sortSelect = document.getElementById('sort');
    const applyBtn = document.getElementById('apply');
    const resetBtn = document.getElementById('reset');
    const statsEl = document.getElementById('stats');
    const gridEl = document.getElementById('grid');

    function debounce(fn, delay) {{
      let timer;
      return function() {{
        const ctx = this, args = arguments;
        clearTimeout(timer);
        timer = setTimeout(function() {{ fn.apply(ctx, args); }}, delay);
      }};
    }}

    function parseDatasetList(rawList) {{
      const result = [];
      if (!Array.isArray(rawList)) return result;
      rawList.forEach(entry => {{
        if (!entry) return;
        if (typeof entry === 'object') {{
          const value = (entry.value || '').toString();
          const label = (entry.label || value).toString();
          if (!value && !label) return;
          result.push({{ value, label, valueLower: value.toLowerCase(), labelLower: label.toLowerCase() }});
        }} else {{
          const value = entry.toString();
          if (!value) return;
          const lower = value.toLowerCase();
          result.push({{ value, label: value, valueLower: lower, labelLower: lower }});
        }}
      }});
      return result;
    }}

    function fillSelectOptions(select, placeholder, options) {{
      if (!select) return;
      const frag = document.createDocumentFragment();
      const optAll = document.createElement('option');
      optAll.value = '';
      optAll.textContent = placeholder;
      frag.appendChild(optAll);
      Object.keys(options).sort((a,b) => {{
        const labelA = (options[a] || '').toString();
        const labelB = (options[b] || '').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity: 'accent' }});
      }}).forEach(value => {{
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = options[value];
        frag.appendChild(opt);
      }});
      select.innerHTML = '';
      select.appendChild(frag);
    }}

    function fillDatalistOptions(datalist, options) {{
      if (!datalist) return;
      const frag = document.createDocumentFragment();
      Object.keys(options).sort((a,b) => {{
        const labelA = (options[a] || '').toString();
        const labelB = (options[b] || '').toString();
        return labelA.localeCompare(labelB, undefined, {{ sensitivity: 'accent' }});
      }}).forEach(key => {{
        const opt = document.createElement('option');
        opt.value = options[key];
        frag.appendChild(opt);
      }});
      datalist.innerHTML = '';
      datalist.appendChild(frag);
    }}

    function parseDateValue(raw) {{
      if (!raw && raw !== 0) return null;
      const str = ('' + raw).trim();
      if (!str) return null;
      const normalized = str.includes('T') ? str : str.replace(' ', 'T');
      let ts = Date.parse(normalized);
      if (!Number.isFinite(ts)) {{
        ts = Date.parse(normalized + 'Z');
      }}
      return Number.isFinite(ts) ? ts : null;
    }}

    function formatDateLabel(ts) {{
      if (ts == null || !Number.isFinite(ts)) return '';
      const d = new Date(ts);
      if (Number.isNaN(d.getTime())) return '';
      const y = d.getFullYear();
      const m = String(d.getMonth()+1).padStart(2,'0');
      const day = String(d.getDate()).padStart(2,'0');
      return `${{y}}-${{m}}-${{day}}`;
    }}

    function formatDateInput(ts) {{
      return formatDateLabel(ts);
    }}

    function toDateRangeValue(value, endOfDay) {{
      if (!value) return null;
      const str = ('' + value).trim();
      if (!str) return null;
      const base = str.length > 10 ? str : str + (endOfDay ? 'T23:59:59.999' : 'T00:00:00');
      return parseDateValue(base);
    }}

    function getNewestTs(u) {{
      if (u && u._lastTs != null) return u._lastTs;
      if (u && u._firstTs != null) return u._firstTs;
      return -Infinity;
    }}

    function getOldestTs(u) {{
      if (u && u._firstTs != null) return u._firstTs;
      if (u && u._lastTs != null) return u._lastTs;
      return Infinity;
    }}

    function compareByName(a, b) {{
      const nameA = (a && a.username ? a.username : '') || '';
      const nameB = (b && b.username ? b.username : '') || '';
      return nameA.localeCompare(nameB, undefined, {{ sensitivity: 'accent' }});
    }}

    const datasetOptions = {{}};
    const cityOptions = {{}};
    const cityLookup = {{}};
    const addedOptions = {{}};

    let globalFirstTs = null;
    let globalLastTs = null;

    DATA.forEach(u => {{
      const comments = Array.isArray(u.comments) ? u.comments.filter(c => c !== null && c !== undefined && c !== '') : [];
      u._commentText = comments.map(c => ('' + c).toLowerCase()).join(' ');
      const datasetList = parseDatasetList(u.datasets);
      u._datasetList = datasetList;
      datasetList.forEach(ds => {{
        if (ds.value && !datasetOptions[ds.value]) {{
          datasetOptions[ds.value] = ds.label || ds.value;
        }}
      }});
      const citiesRaw = Array.isArray(u.cities) ? u.cities : [];
      const preparedCities = [];
      citiesRaw.forEach(city => {{
        const label = (city || '').toString().trim();
        if (!label) return;
        const lower = label.toLowerCase();
        preparedCities.push({{ label, lower }});
        if (!cityOptions[label]) cityOptions[label] = label;
        if (lower && !cityLookup[lower]) cityLookup[lower] = label;
      }});
      u._cityList = preparedCities;
      u._cityText = preparedCities.map(entry => entry.lower).join(' ');
      const addedLabel = (u.added_by || '').toString();
      const addedKey = (u.added_by_key || addedLabel).toString().trim().toLowerCase();
      u._addedKey = addedKey;
      if (addedKey && !addedOptions[addedKey]) {{
        addedOptions[addedKey] = addedLabel || addedKey;
      }}
      const searchParts = [];
      const username = (u.username || '').toString();
      if (username) searchParts.push(username.toLowerCase());
      if (u._commentText) searchParts.push(u._commentText);
      if (addedLabel) searchParts.push(addedLabel.toLowerCase());
      datasetList.forEach(ds => {{
        if (ds.labelLower) searchParts.push(ds.labelLower);
        if (ds.valueLower) searchParts.push(ds.valueLower);
      }});
      preparedCities.forEach(entry => searchParts.push(entry.lower));
      u._searchText = searchParts.join(' ');
      const firstTs = parseDateValue(u.first_created);
      const lastTs = parseDateValue(u.last_created);
      u._firstTs = firstTs;
      u._lastTs = lastTs;
      if (firstTs != null && (globalFirstTs == null || firstTs < globalFirstTs)) globalFirstTs = firstTs;
      if (lastTs != null && (globalLastTs == null || lastTs > globalLastTs)) globalLastTs = lastTs;
      const firstLabel = formatDateLabel(firstTs);
      const lastLabel = formatDateLabel(lastTs);
      let rangeLabel = '';
      if (firstLabel && lastLabel) {{
        rangeLabel = firstLabel === lastLabel ? firstLabel : firstLabel + ' → ' + lastLabel;
      }} else {{
        rangeLabel = firstLabel || lastLabel || '';
      }}
      u._createdRangeLabel = rangeLabel;
    }});

    fillSelectOptions(datasetSelect, 'Все данные', datasetOptions);
    fillDatalistOptions(cityDatalist, cityOptions);
    fillSelectOptions(addedSelect, 'Все добавившие', addedOptions);

    const minDateLabel = formatDateInput(globalFirstTs);
    const maxDateLabel = formatDateInput(globalLastTs);
    if (dateFromInput) {{
      if (minDateLabel) dateFromInput.min = minDateLabel;
      if (maxDateLabel) dateFromInput.max = maxDateLabel;
    }}
    if (dateToInput) {{
      if (minDateLabel) dateToInput.min = minDateLabel;
      if (maxDateLabel) dateToInput.max = maxDateLabel;
    }}

    function sortData(arr, mode) {{
      switch(mode) {{
        case 'img_desc':
          return arr.sort((a,b) => {{
            const diff = (b.images_count || 0) - (a.images_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'img_asc':
          return arr.sort((a,b) => {{
            const diff = (a.images_count || 0) - (b.images_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'cm_desc':
          return arr.sort((a,b) => {{
            const diff = (b.comments_count || 0) - (a.comments_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'cm_asc':
          return arr.sort((a,b) => {{
            const diff = (a.comments_count || 0) - (b.comments_count || 0);
            if (diff !== 0) return diff;
            return compareByName(a,b);
          }});
        case 'name_desc':
          return arr.sort((a,b) => compareByName(b,a));
        case 'date_desc':
          return arr.sort((a,b) => {{
            const aTs = getNewestTs(a);
            const bTs = getNewestTs(b);
            if (bTs === aTs) return compareByName(a,b);
            return bTs - aTs;
          }});
        case 'date_asc':
          return arr.sort((a,b) => {{
            const aTs = getOldestTs(a);
            const bTs = getOldestTs(b);
            if (aTs === bTs) return compareByName(a,b);
            return aTs - bTs;
          }});
        case 'name_asc':
        default:
          return arr.sort((a,b) => compareByName(a,b));
      }}
    }}

    function escapeHtml(s) {{
      return (''+s).replace(/[&<>"']/g, function(m) {{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]; }});
    }}

    function renderProfile(user, updateHash=true) {{
      if (!user) {{
        return;
      }}
      const username = user.username || '';
      const safeUsername = username ? '@' + username : 'Без username';
      profileUsername.textContent = safeUsername;

      const profileUrl = user.profile_url || '';
      if (profileUrl) {{
        profileFollow.href = profileUrl;
        profileOpen.href = profileUrl;
        profileFollow.classList.remove('disabled');
      }} else {{
        profileFollow.href = '#';
        profileOpen.href = '#';
        profileFollow.classList.add('disabled');
      }}

      const images = Array.isArray(user.images) ? user.images.filter(Boolean) : [];
      if (images.length) {{
        profileAvatar.classList.add('has-image');
        profileAvatar.style.backgroundImage = 'url(' + JSON.stringify(images[0]) + ')';
        profileAvatar.textContent = '';
      }} else {{
        profileAvatar.classList.remove('has-image');
        profileAvatar.style.backgroundImage = '';
        profileAvatar.textContent = username ? username[0].toUpperCase() : '@';
      }}

      const metaParts = [];
      if (user.lat != null && user.lon != null) {{
        metaParts.push('<span>📍 ' + user.lat.toFixed(5) + ', ' + user.lon.toFixed(5) + '</span>');
      }}
      if (user.added_by) {{
        if (user.added_by_link) {{
          metaParts.push('<span>👤 <a href="' + escapeHtml(user.added_by_link) + '" target="_blank" rel="noopener">' + escapeHtml(user.added_by) + '</a></span>');
        }} else {{
          metaParts.push('<span>👤 ' + escapeHtml(user.added_by) + '</span>');
        }}
      }}
      profileMeta.innerHTML = metaParts.join('');
      profileMeta.classList.toggle('hidden', metaParts.length === 0);

      const datasetParts = (user.datasets || []).map(ds => '<span class=\"tag\">' + escapeHtml(ds.label || ds.value || '') + '</span>');
      profileDatasets.innerHTML = datasetParts.join('');
      profileDatasets.classList.toggle('hidden', datasetParts.length === 0);

      const cityParts = (user.cities || []).map(city => '<span class=\"chip\">' + escapeHtml(city) + '</span>');
      profileCities.innerHTML = cityParts.join('');
      profileCities.classList.toggle('hidden', cityParts.length === 0);

      const infoExtra = [];
      if (user.first_created) {{
        infoExtra.push('<span>🕓 Первое: ' + escapeHtml(user.first_created) + '</span>');
      }}
      if (user.last_created && user.last_created !== user.first_created) {{
        infoExtra.push('<span>🕒 Последнее: ' + escapeHtml(user.last_created) + '</span>');
      }}
      profileInfoExtra.innerHTML = infoExtra.join('');
      profileInfoExtra.classList.toggle('hidden', infoExtra.length === 0);

      profileStats.innerHTML = [
        '<div class=\"stat\"><span class=\"value\">' + images.length + '</span><span class=\"label\">posts</span></div>',
        '<div class=\"stat\"><span class=\"value\">' + (user.comments_count || 0) + '</span><span class=\"label\">comments</span></div>'
      ].join('');

      if (images.length) {{
        profileGrid.innerHTML = images.map(src => '<div class=\"cell\"><img src=\"' + escapeHtml(src) + '\" loading=\"lazy\" alt=\"\"></div>').join('');
        profileEmpty.classList.add('hidden');
      }} else {{
        profileGrid.innerHTML = '';
        profileEmpty.classList.remove('hidden');
      }}

      galleryView.classList.add('hidden');
      profileView.classList.remove('hidden');
      if (updateHash) {{
        location.hash = '#/profile/' + encodeURIComponent(username || '');
      }}
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }}

    function showGallery(updateHash=true) {{
      profileView.classList.add('hidden');
      galleryView.classList.remove('hidden');
      if (updateHash) {{
        location.hash = '#gallery';
      }}
    }}

    profileBack.addEventListener('click', () => showGallery(true));


    function render(list) {{
      if (!gridEl || !statsEl) return;
      gridEl.innerHTML='';
      let entries=0;
      list.forEach(u=>{{
        const allImages = Array.isArray(u.images) ? u.images.filter(Boolean) : [];
        const imageCount = typeof u.images_count === 'number' ? u.images_count : allImages.length;
        const previews = allImages.slice(0,4);
        const cm = Array.isArray(u.comments) ? u.comments : [];
        const commentCount = typeof u.comments_count === 'number' ? u.comments_count : cm.length;
        entries += imageCount;
        let cmHtml='';
        if (cm.length===0) cmHtml = '<div class="empty">нет комментариев</div>';
        else {{
          const head = cm.slice(0,3).map(c=>'<li>' + escapeHtml(c) + '</li>').join('');
          const more = cm.length>3 ? '<div class="more">и ещё ' + (cm.length-3) + '…</div>' : '';
          cmHtml = '<ul>' + head + '</ul>' + more;
        }}
        const thumbs = previews.map(src=>'<img src="' + escapeHtml(src) + '" loading="lazy">').join('');
        const latStr = (u.lat!=null && u.lon!=null) ? u.lat.toFixed(6) + ', ' + u.lon.toFixed(6) : '';
        const addedBy = (()=>{{
          if (!u.added_by) return '';
          const label = escapeHtml(u.added_by);
          if (u.added_by_link) {{
            return '<a href="' + escapeHtml(u.added_by_link) + '" target="_blank">' + label + '</a>';
          }}
          return label;
        }})();
        const chipParts=[];
        (u._cityList || []).slice(0,3).forEach(entry=>{{
          chipParts.push('<span class="chip chip-city">' + escapeHtml(entry.label) + '</span>');
        }});
        (u._datasetList || []).slice(0,3).forEach(ds=>{{
          const label = ds.label || ds.value;
          if (label) chipParts.push('<span class="chip chip-data">' + escapeHtml(label) + '</span>');
        }});
        const chipsHtml = chipParts.length ? '<div class="chips">' + chipParts.join('') + '</div>' : '';
        const createdHtml = u._createdRangeLabel ? '<div class="meta created">Добавлено: ' + escapeHtml(u._createdRangeLabel) + '</div>' : '';
        const card = document.createElement('div');
        card.className = 'card';
        card.dataset.username = u.username || '';
        const profileUrl = u.profile_url || '';
        const safeProfileUrl = escapeHtml(profileUrl);
        const displayName = u.username ? '@' + escapeHtml(u.username) : 'Без username';
        card.innerHTML = `
          <div class="head">
            <div class="name"><a href="${{safeProfileUrl}}" target="_blank">${{displayName}}</a></div>
            <a class="btn" href="${{safeProfileUrl}}" target="_blank">View Profile</a>
          </div>
          <div class="meta">${{latStr ? latStr + ' • ' : ''}}${{imageCount}} item(s) • ${{commentCount}} comment(s)</div>
          ${{createdHtml}}
          ${{chipsHtml}}
          <div class="meta added">Добавил: ${{addedBy || '—'}}</div>
          <div class="thumbs">${{thumbs}}</div>
          <div class="cm">${{cmHtml}}</div>
          <div class="actions">
            <button class="btn-secondary profile-btn" type="button">Открыть галерею</button>
          </div>
        `;
        const openBtn = card.querySelector('.profile-btn');
        if (openBtn) {{
          openBtn.addEventListener('click', (ev) => {{
            ev.preventDefault();
            ev.stopPropagation();
            renderProfile(u);
          }});
        }}
        gridEl.appendChild(card);
      }});
      statsEl.textContent = `Users: ${{list.length}} • Entries: ${{entries}}`;
    }}

    function apply() {{
      const q = searchInput ? searchInput.value.trim().toLowerCase() : '';
      const datasetValue = datasetSelect ? datasetSelect.value : '';
      const datasetLower = datasetValue ? datasetValue.toLowerCase() : '';
      const cityRaw = cityInput ? cityInput.value.trim() : '';
      const cityLower = cityRaw.toLowerCase();
      const hasExactCity = cityLower && Object.prototype.hasOwnProperty.call(cityLookup, cityLower);
      const commentValue = commentInput ? commentInput.value.trim().toLowerCase() : '';
      const hasCommentsValue = hasCommentsSelect ? hasCommentsSelect.value : '';
      const addedValue = addedSelect ? addedSelect.value : '';
      const fromTs = dateFromInput ? toDateRangeValue(dateFromInput.value, false) : null;
      const toTs = dateToInput ? toDateRangeValue(dateToInput.value, true) : null;
      const sortMode = sortSelect ? (sortSelect.value || 'date_desc') : 'date_desc';

      const list = DATA.filter(u => {{
        if (q && (!u._searchText || u._searchText.indexOf(q) === -1)) return false;
        if (datasetLower) {{
          const dsList = u._datasetList || [];
          const datasetMatch = dsList.some(ds => ds.valueLower === datasetLower || ds.labelLower === datasetLower);
          if (!datasetMatch) return false;
        }}
        if (cityLower) {{
          if (hasExactCity) {{
            const matchCity = (u._cityList || []).some(entry => entry.lower === cityLower);
            if (!matchCity) return false;
          }} else {{
            if (!u._cityText || u._cityText.indexOf(cityLower) === -1) return false;
          }}
        }}
        if (commentValue) {{
          if (!u._commentText || u._commentText.indexOf(commentValue) === -1) return false;
        }}
        const commentCount = typeof u.comments_count === 'number' ? u.comments_count : (Array.isArray(u.comments) ? u.comments.length : 0);
        if (hasCommentsValue === 'with' && commentCount === 0) return false;
        if (hasCommentsValue === 'without' && commentCount > 0) return false;
        if (addedValue && u._addedKey !== addedValue) return false;
        if (fromTs != null && getNewestTs(u) < fromTs) return false;
        if (toTs != null && getOldestTs(u) > toTs) return false;
        return true;
      }});
      sortData(list, sortMode);
      render(list);
      return list;
    }}

    function reset() {{
      if (searchInput) searchInput.value='';
      if (datasetSelect) datasetSelect.value='';
      if (cityInput) cityInput.value='';
      if (commentInput) commentInput.value='';
      if (hasCommentsSelect) hasCommentsSelect.value='';
      if (addedSelect) addedSelect.value='';
      if (dateFromInput) dateFromInput.value='';
      if (dateToInput) dateToInput.value='';
      if (sortSelect) sortSelect.value='date_desc';
      apply();
    }}

    if (applyBtn) applyBtn.addEventListener('click', apply);
    if (resetBtn) resetBtn.addEventListener('click', reset);

    const debouncedApply = debounce(apply, 200);
    if (searchInput) searchInput.addEventListener('input', debouncedApply);
    if (cityInput) cityInput.addEventListener('input', debouncedApply);
    if (commentInput) commentInput.addEventListener('input', debouncedApply);

    [datasetSelect, hasCommentsSelect, addedSelect, sortSelect].forEach(el => {{
      if (el) el.addEventListener('change', apply);
    }});
    if (dateFromInput) dateFromInput.addEventListener('change', apply);
    if (dateToInput) dateToInput.addEventListener('change', apply);

    reset();

    function handleHashNavigation() {{
      const hash = location.hash || '';
      if (hash.startsWith('#/profile/')) {{
        const username = decodeURIComponent(hash.replace('#/profile/', ''));
        const user = DATA_MAP.get((username || '').toLowerCase());
        if (user) {{
          renderProfile(user, false);
          return;
        }}
      }}
      showGallery(false);
    }}

    window.addEventListener('hashchange', handleHashNavigation);
    handleHashNavigation();
  </script>
</body>
</html>"""
    return html


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
