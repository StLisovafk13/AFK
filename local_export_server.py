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
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from vsco_bot import (
    DB_PATH,
    build_map_images,
    build_map_users,
    build_rich_gallery,
    db_connect,
    fetch_gallery_users,
    fetch_items_for_map,
    utc_now_iso,
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


def _sanitize_comments(payload: Any) -> List[str]:
    """Convert *payload* to a cleaned list of comment strings."""

    if payload is None:
        return []

    entries: List[str] = []
    if isinstance(payload, str):
        text = payload.replace("\r\n", "\n").replace("\r", "\n")
        entries = text.split("\n")
    elif isinstance(payload, (list, tuple, set)):
        entries = [str(item) for item in payload]
    else:
        return []

    cleaned: List[str] = []
    for item in entries:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _update_profile_comments(username: str, comments: List[str]) -> Dict[str, Any]:
    """Replace all comments for *username* with *comments* and return stats."""

    clean_username = (username or "").strip()
    if not clean_username:
        raise ValueError("username is required")

    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT id, chat_id FROM items WHERE username = ? ORDER BY created_at ASC",
            (clean_username,),
        ).fetchall()
        if not rows:
            raise LookupError("profile not found")

        item_pairs = [(int(item_id), int(chat_id)) for item_id, chat_id in rows]
        item_ids = [item_id for item_id, _ in item_pairs]

        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            conn.execute(
                f"DELETE FROM comments WHERE item_id IN ({placeholders})",
                item_ids,
            )

        if comments:
            from itertools import cycle

            iterator = cycle(item_pairs)
            now = utc_now_iso()
            for comment in comments:
                item_id, chat_id = next(iterator)
                conn.execute(
                    "INSERT INTO comments(item_id, chat_id, comment, created_at) VALUES(?,?,?,?)",
                    (item_id, chat_id, comment, now),
                )

        conn.commit()
        return {
            "username": clean_username,
            "comments": comments,
            "comments_count": len(comments),
        }
    finally:
        conn.close()


def _delete_profiles(usernames: Iterable[str]) -> Dict[str, Any]:
    """Remove all DB entries associated with the provided usernames."""

    cleaned: List[str] = []
    seen: set[str] = set()
    for raw in usernames:
        candidate = (raw or "").strip()
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(candidate)

    if not cleaned:
        raise ValueError("usernames are required")

    conn = db_connect()
    try:
        pending: List[Tuple[str, List[int]]] = []
        missing: List[str] = []
        for username in cleaned:
            rows = conn.execute(
                "SELECT id FROM items WHERE username = ?",
                (username,),
            ).fetchall()
            if not rows:
                missing.append(username)
                continue
            item_ids = [int(row[0]) for row in rows]
            pending.append((username, item_ids))

        if missing:
            if len(cleaned) == 1:
                raise LookupError("profile not found")
            raise LookupError("profiles not found: " + ", ".join(missing))

        total_items = 0
        total_comments = 0
        total_links = 0
        details: List[Dict[str, Any]] = []

        for username, item_ids in pending:
            deleted_comments = 0
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                cur = conn.execute(
                    f"DELETE FROM comments WHERE item_id IN ({placeholders})",
                    item_ids,
                )
                deleted_comments = cur.rowcount or 0

            cur_items = conn.execute(
                "DELETE FROM items WHERE username = ?",
                (username,),
            )
            removed_items = cur_items.rowcount or len(item_ids)

            cur_links = conn.execute(
                "DELETE FROM links WHERE username = ?",
                (username,),
            )
            removed_links = cur_links.rowcount or 0

            total_items += removed_items
            total_comments += deleted_comments
            total_links += removed_links
            details.append(
                {
                    "username": username,
                    "removed_items": removed_items,
                    "removed_comments": deleted_comments,
                    "removed_links": removed_links,
                }
            )

        conn.commit()
        return {
            "usernames": cleaned,
            "removed_profiles": len(details),
            "removed_items": total_items,
            "removed_comments": total_comments,
            "removed_links": total_links,
            "details": details,
        }
    finally:
        conn.close()


def _delete_profile(username: str) -> Dict[str, Any]:
    """Remove all DB entries associated with *username*."""

    result = _delete_profiles([username])
    details = result.get("details") or []
    if details:
        return details[0]
    clean_username = (username or "").strip()
    return {
        "username": clean_username,
        "removed_items": 0,
        "removed_comments": 0,
        "removed_links": 0,
    }


_ADMIN_PANEL_SNIPPET = r"""
<style>
  .admin-flash-container { position: fixed; top: 20px; right: 20px; z-index: 5000; display: flex; flex-direction: column; gap: 12px; }
  .admin-flash { padding: 12px 16px; border-radius: 12px; font-size: 14px; box-shadow: 0 12px 24px rgba(15,23,42,0.18); color: #0f172a; background: #f8fafc; opacity: 0; transform: translateY(-8px); transition: opacity .2s ease, transform .2s ease; }
  .admin-flash.show { opacity: 1; transform: translateY(0); }
  .admin-flash.hide { opacity: 0; transform: translateY(-10px); }
  .admin-flash.error { background: #fee2e2; color: #991b1b; }
  .admin-modal { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; z-index: 4000; }
  .admin-modal.hidden { display: none; }
  .admin-modal__backdrop { position: absolute; inset: 0; background: rgba(15,23,42,0.45); backdrop-filter: blur(4px); }
  .admin-modal__dialog { position: relative; background: #fff; border-radius: 20px; padding: 28px; width: min(520px, 90vw); max-height: 80vh; display: flex; flex-direction: column; gap: 18px; box-shadow: 0 28px 60px rgba(15,23,42,0.35); }
  .admin-modal__dialog h2 { margin: 0; font-size: 22px; }
  .admin-modal__dialog textarea { flex: 1 1 auto; min-height: 200px; border-radius: 14px; border: 1px solid #cbd5f5; padding: 12px 14px; font-size: 14px; resize: vertical; }
  .admin-modal__dialog textarea:focus { outline: none; border-color: #6366f1; box-shadow: 0 0 0 3px rgba(99,102,241,0.15); }
  .admin-modal__actions { display: flex; gap: 12px; justify-content: flex-end; }
  .admin-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 10px 16px; border-radius: 12px; border: none; cursor: pointer; font-weight: 600; font-size: 13px; transition: transform .15s ease, box-shadow .15s ease, background .15s ease; }
  .admin-btn.primary { background: #4f46e5; color: #fff; }
  .admin-btn.primary:hover { background: #4338ca; transform: translateY(-1px); }
  .admin-btn.secondary { background: #e2e8f0; color: #0f172a; }
  .admin-btn.secondary:hover { background: #cbd5f5; transform: translateY(-1px); }
  .admin-danger { background: #ef4444; color: #fff; }
  .admin-danger:hover { background: #dc2626; transform: translateY(-1px); }
  .profile-actions .admin-control { background: #f97316; color: #fff; border: none; border-radius: 10px; padding: 8px 14px; cursor: pointer; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; transition: background .15s ease, transform .15s ease; }
  .profile-actions .admin-control:hover { background: #ea580c; transform: translateY(-1px); }
  .profile-actions .admin-control.delete { background: #ef4444; }
  .profile-actions .admin-control.delete:hover { background: #dc2626; }
  .admin-bulk-toggle { position: fixed; bottom: 28px; right: 28px; z-index: 4500; display: inline-flex; align-items: center; gap: 8px; padding: 12px 18px; border-radius: 999px; background: #2563eb; color: #fff; border: none; font-weight: 600; font-size: 14px; cursor: pointer; box-shadow: 0 18px 40px rgba(37,99,235,0.35); transition: transform .18s ease, box-shadow .18s ease, background .18s ease; }
  .admin-bulk-toggle:hover { background: #1d4ed8; transform: translateY(-2px); box-shadow: 0 22px 48px rgba(29,78,216,0.4); }
  .admin-bulk-toggle:disabled { opacity: .6; cursor: default; transform: none; box-shadow: none; }
  .admin-bulk-bar { position: fixed; left: 50%; bottom: 28px; transform: translate(-50%, 24px); z-index: 4600; display: flex; align-items: center; gap: 16px; padding: 14px 20px; border-radius: 18px; background: rgba(15,23,42,0.95); color: #f8fafc; box-shadow: 0 24px 48px rgba(15,23,42,0.45); transition: opacity .2s ease, transform .2s ease; }
  .admin-bulk-bar.hidden { opacity: 0; pointer-events: none; transform: translate(-50%, 40px); }
  .admin-bulk-info { font-size: 14px; font-weight: 600; }
  .admin-bulk-info .count { font-variant-numeric: tabular-nums; margin: 0 4px; }
  .admin-bulk-bar .admin-btn { font-size: 13px; padding: 8px 14px; border-radius: 12px; }
  .card.admin-selectable { position: relative; }
  .admin-select-indicator { position: absolute; top: 12px; left: 12px; width: 26px; height: 26px; border-radius: 8px; border: 2px solid rgba(15,23,42,0.25); background: rgba(255,255,255,0.9); display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 700; color: rgba(15,23,42,0.55); box-shadow: 0 10px 20px rgba(15,23,42,0.18); opacity: 0; transform: scale(0.8); transition: opacity .18s ease, transform .18s ease, background .18s ease, color .18s ease, border-color .18s ease; pointer-events: none; }
  body.admin-selection-active .card.admin-selectable { cursor: pointer; }
  body.admin-selection-active .card.admin-selectable .admin-select-indicator { opacity: 1; transform: scale(1); }
  body.admin-selection-active .card.admin-selectable.admin-selected { box-shadow: 0 0 0 3px rgba(239,68,68,0.45); }
  body.admin-selection-active .card.admin-selectable.admin-selected .admin-select-indicator { background: #ef4444; border-color: #b91c1c; color: #fff; }
  body.admin-selection-active .card.admin-selectable.admin-selected::after { content: ''; position: absolute; inset: 0; border-radius: inherit; box-shadow: inset 0 0 0 2px rgba(239,68,68,0.45); pointer-events: none; }
</style>
<script>
(function() {
  if (window.__VSCO_ADMIN_ENABLED__) {
    return;
  }
  window.__VSCO_ADMIN_ENABLED__ = true;

  const COMMENTS_URL = '/api/admin/comments';
  const DELETE_URL = '/api/admin/delete-profile';
  const DELETE_BULK_URL = '/api/admin/delete-profiles';

  const body = document.body;
  if (!body) {
    return;
  }

  function ensureFlashContainer() {
    let container = document.querySelector('.admin-flash-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'admin-flash-container';
      body.appendChild(container);
    }
    return container;
  }

  function showFlash(message, type) {
    const container = ensureFlashContainer();
    const item = document.createElement('div');
    item.className = 'admin-flash' + (type === 'error' ? ' error' : '');
    item.textContent = message;
    container.appendChild(item);
    requestAnimationFrame(() => {
      item.classList.add('show');
    });
    setTimeout(() => {
      item.classList.add('hide');
    }, 3200);
    setTimeout(() => {
      if (item.parentNode) {
        item.parentNode.removeChild(item);
      }
    }, 3800);
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
      return window.CSS.escape(value);
    }
    return value.replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  const grid = document.getElementById('grid');

  let selectionMode = false;
  let bulkDeleteInProgress = false;
  const selectedProfiles = new Map();
  let bulkToggleBtn = null;
  let bulkBar = null;
  let bulkSelectAllBtn = null;
  let bulkCancelBtn = null;
  let bulkDeleteBtn = null;
  let bulkCountEl = null;
  let gridObserver = null;

  function normalizeUsername(value) {
    return (value || '').toString().trim().toLowerCase();
  }

  function countSelectableProfiles() {
    if (!grid) {
      return 0;
    }
    let count = 0;
    const cards = grid.querySelectorAll('.card.admin-selectable');
    cards.forEach(card => {
      const username = card.dataset ? card.dataset.username : '';
      if (normalizeUsername(username)) {
        count += 1;
      }
    });
    return count;
  }

  function updateBulkUi() {
    if (bulkCountEl) {
      bulkCountEl.textContent = selectedProfiles.size.toString();
    }
    if (bulkDeleteBtn) {
      bulkDeleteBtn.disabled = bulkDeleteInProgress || selectedProfiles.size === 0;
    }
    if (bulkToggleBtn) {
      bulkToggleBtn.disabled = bulkDeleteInProgress;
    }
    if (bulkSelectAllBtn) {
      const total = countSelectableProfiles();
      const allSelected = total > 0 && selectedProfiles.size >= total;
      bulkSelectAllBtn.disabled = !selectionMode || bulkDeleteInProgress || total === 0 || allSelected;
    }
  }

  function syncCardSelectionState(card) {
    if (!card) {
      return;
    }
    if (!selectionMode) {
      card.classList.remove('admin-selected');
      return;
    }
    const key = normalizeUsername(card.dataset ? card.dataset.username : '');
    if (key && selectedProfiles.has(key)) {
      card.classList.add('admin-selected');
    } else {
      card.classList.remove('admin-selected');
    }
  }

  function toggleCardSelection(card) {
    if (!card) {
      return;
    }
    const username = card.dataset ? card.dataset.username : '';
    const key = normalizeUsername(username);
    if (!key) {
      return;
    }
    if (selectedProfiles.has(key)) {
      selectedProfiles.delete(key);
      card.classList.remove('admin-selected');
    } else {
      selectedProfiles.set(key, { username });
      card.classList.add('admin-selected');
    }
    updateBulkUi();
  }

  function selectAllProfiles() {
    if (!grid || bulkDeleteInProgress) {
      return;
    }
    setSelectionMode(true);
    const cards = grid.querySelectorAll('.card.admin-selectable');
    cards.forEach(card => {
      const username = card.dataset ? card.dataset.username : '';
      const key = normalizeUsername(username);
      if (!key) {
        return;
      }
      selectedProfiles.set(key, { username });
      card.classList.add('admin-selected');
    });
    updateBulkUi();
  }

  function handleCardClick(event) {
    if (!selectionMode) {
      return;
    }
    const card = event.currentTarget;
    if (!card) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    toggleCardSelection(card);
  }

  function refreshSelectableCards() {
    if (!grid) {
      return;
    }
    const cards = grid.querySelectorAll('.card');
    const existing = new Set();
    cards.forEach(card => {
      const username = card.dataset ? card.dataset.username : '';
      const key = normalizeUsername(username);
      if (key) {
        existing.add(key);
      }
      card.classList.add('admin-selectable');
      if (!card.querySelector('.admin-select-indicator')) {
        const indicator = document.createElement('div');
        indicator.className = 'admin-select-indicator';
        indicator.textContent = '✓';
        card.appendChild(indicator);
      }
      if (!card.dataset.adminSelectBound) {
        card.addEventListener('click', handleCardClick);
        card.dataset.adminSelectBound = '1';
      }
      syncCardSelectionState(card);
    });
    Array.from(selectedProfiles.keys()).forEach(key => {
      if (!existing.has(key)) {
        selectedProfiles.delete(key);
      }
    });
    if (!selectionMode && selectedProfiles.size) {
      selectedProfiles.clear();
    }
    updateBulkUi();
  }

  function setSelectionMode(next) {
    if (!grid) {
      return;
    }
    const enabled = !!next;
    if (!enabled) {
      if (selectionMode) {
        selectionMode = false;
        body.classList.remove('admin-selection-active');
        if (bulkToggleBtn) {
          bulkToggleBtn.style.display = 'inline-flex';
        }
        if (bulkBar) {
          bulkBar.classList.add('hidden');
        }
      }
      selectedProfiles.clear();
      grid.querySelectorAll('.card.admin-selectable').forEach(card => card.classList.remove('admin-selected'));
      updateBulkUi();
      return;
    }
    if (selectionMode) {
      return;
    }
    selectionMode = true;
    body.classList.add('admin-selection-active');
    if (bulkToggleBtn) {
      bulkToggleBtn.style.display = 'none';
    }
    if (bulkBar) {
      bulkBar.classList.remove('hidden');
    }
    refreshSelectableCards();
    updateBulkUi();
  }

  async function deleteSelectedProfiles() {
    if (!grid || selectedProfiles.size === 0 || bulkDeleteInProgress) {
      return;
    }
    const usernames = Array.from(selectedProfiles.values()).map(entry => entry.username).filter(Boolean);
    if (!usernames.length) {
      return;
    }
    if (!window.confirm('Удалить выбранные профили (' + usernames.length + ') и все связанные записи?')) {
      return;
    }
    bulkDeleteInProgress = true;
    updateBulkUi();
    try {
      const response = await fetch(DELETE_BULK_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        body: JSON.stringify({ usernames })
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || 'HTTP ' + response.status);
      }
      const payload = await response.json();
      if (!payload || payload.ok !== true) {
        const message = payload && payload.error ? payload.error : 'Не удалось удалить выбранные профили';
        throw new Error(message);
      }
      const detailList = Array.isArray(payload.details) ? payload.details : [];
      const removedKeys = new Set();
      detailList.forEach(entry => {
        const uname = entry && entry.username ? entry.username : '';
        const key = normalizeUsername(uname);
        if (key) {
          removedKeys.add(key);
        }
      });
      if (!removedKeys.size) {
        usernames.forEach(name => {
          const key = normalizeUsername(name);
          if (key) {
            removedKeys.add(key);
          }
        });
      }
      if (window.DATA_MAP && typeof window.DATA_MAP.delete === 'function') {
        removedKeys.forEach(key => window.DATA_MAP.delete(key));
      }
      if (Array.isArray(window.DATA)) {
        for (let idx = window.DATA.length - 1; idx >= 0; idx -= 1) {
          const item = window.DATA[idx];
          const key = normalizeUsername(item && item.username);
          if (key && removedKeys.has(key)) {
            window.DATA.splice(idx, 1);
          }
        }
      }
      if (typeof window.apply === 'function') {
        try {
          window.apply();
        } catch (err) {
          console.error('Failed to re-render gallery after bulk deletion', err);
        }
      }
      if (typeof window.showGallery === 'function') {
        try {
          window.showGallery(true);
        } catch (err) {
          console.error('Failed to return to gallery view after bulk deletion', err);
        }
      }
      setSelectionMode(false);
      const removedCount = typeof payload.removed_profiles === 'number' ? payload.removed_profiles : removedKeys.size;
      showFlash('Удалено профилей: ' + removedCount, 'success');
    } catch (err) {
      console.error('Failed to delete profiles', err);
      const message = err && err.message ? err.message : 'Не удалось удалить выбранные профили';
      showFlash(message, 'error');
    } finally {
      bulkDeleteInProgress = false;
      updateBulkUi();
      refreshSelectableCards();
    }
  }

  if (grid) {
    bulkToggleBtn = document.createElement('button');
    bulkToggleBtn.type = 'button';
    bulkToggleBtn.className = 'admin-bulk-toggle';
    bulkToggleBtn.textContent = '🗑️ Выбор профилей';
    bulkToggleBtn.addEventListener('click', () => setSelectionMode(true));
    body.appendChild(bulkToggleBtn);

    bulkBar = document.createElement('div');
    bulkBar.className = 'admin-bulk-bar hidden';
    bulkBar.innerHTML = '\n    <div class="admin-bulk-info">Выбрано: <span class="count">0</span></div>\n    <button type="button" class="admin-btn secondary admin-bulk-select-all">Выбрать все</button>\n    <button type="button" class="admin-btn secondary admin-bulk-cancel">Отмена</button>\n    <button type="button" class="admin-btn admin-danger admin-bulk-delete">Удалить выбранные</button>\n  ';
    bulkCountEl = bulkBar.querySelector('.count');
    bulkSelectAllBtn = bulkBar.querySelector('.admin-bulk-select-all');
    bulkCancelBtn = bulkBar.querySelector('.admin-bulk-cancel');
    bulkDeleteBtn = bulkBar.querySelector('.admin-bulk-delete');
    if (bulkSelectAllBtn) {
      bulkSelectAllBtn.addEventListener('click', () => selectAllProfiles());
    }
    if (bulkCancelBtn) {
      bulkCancelBtn.addEventListener('click', () => setSelectionMode(false));
    }
    if (bulkDeleteBtn) {
      bulkDeleteBtn.addEventListener('click', () => deleteSelectedProfiles());
    }
    body.appendChild(bulkBar);

    refreshSelectableCards();
    updateBulkUi();
    gridObserver = new MutationObserver(() => refreshSelectableCards());
    gridObserver.observe(grid, { childList: true });

    const handleGalleryRendered = () => {
      refreshSelectableCards();
      updateBulkUi();
    };
    document.addEventListener('gallery:rendered', handleGalleryRendered);
  }

  const modal = document.createElement('div');
  modal.className = 'admin-modal hidden';
  modal.innerHTML = '\n    <div class="admin-modal__backdrop"></div>\n    <div class="admin-modal__dialog">\n      <h2>Редактировать комментарии</h2>\n      <p style="margin:0;color:#475569;font-size:13px;">По одному комментарию в строке. Пустые строки будут проигнорированы.</p>\n      <textarea placeholder="Введите комментарии, каждый с новой строки"></textarea>\n      <div class="admin-modal__actions">\n        <button type="button" class="admin-btn secondary admin-cancel">Отмена</button>\n        <button type="button" class="admin-btn primary admin-save">Сохранить</button>\n      </div>\n    </div>\n  ';
  body.appendChild(modal);

  const backdrop = modal.querySelector('.admin-modal__backdrop');
  const textarea = modal.querySelector('textarea');
  const saveBtn = modal.querySelector('.admin-save');
  const cancelBtn = modal.querySelector('.admin-cancel');

  let activeUser = null;
  let saveInProgress = false;

  function closeModal() {
    modal.classList.add('hidden');
    saveInProgress = false;
    if (saveBtn) {
      saveBtn.disabled = false;
    }
  }

  function openModal(user) {
    activeUser = user;
    if (textarea) {
      const list = Array.isArray(user && user.comments) ? user.comments : [];
      textarea.value = list.join('\n');
      setTimeout(() => {
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
      }, 30);
    }
    modal.classList.remove('hidden');
  }

  function parseCommentsFromTextarea() {
    if (!textarea) {
      return [];
    }
    return textarea.value
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(line => line.length > 0);
  }

  function updateProfileStatsIfVisible(user) {
    const profileNameEl = document.getElementById('profileUsername');
    if (!profileNameEl) {
      return;
    }
    const text = (profileNameEl.textContent || '').trim().toLowerCase();
    const normalized = text.startsWith('@') ? text.slice(1) : text;
    const target = (user && user.username ? user.username : '').toString().toLowerCase();
    if (!target || normalized !== target) {
      return;
    }
    const profileStats = document.getElementById('profileStats');
    if (profileStats) {
      const images = Array.isArray(user.images) ? user.images.filter(Boolean) : [];
      const imageCount = typeof user.images_count === 'number' ? user.images_count : images.length;
      const commentCount = Array.isArray(user.comments) ? user.comments.length : (typeof user.comments_count === 'number' ? user.comments_count : 0);
      profileStats.innerHTML = '<div class="stat"><span class="value">' + imageCount + '</span><span class="label">posts</span></div>' +
        '<div class="stat"><span class="value">' + commentCount + '</span><span class="label">comments</span></div>';
    }
  }

  function refreshCardForUser(user) {
    const username = (user && user.username ? user.username : '').toString();
    if (!username) {
      return;
    }
    const selector = '.card[data-username="' + cssEscape(username) + '"] .cm';
    const cardComments = document.querySelector(selector);
    const commentsList = Array.isArray(user.comments) ? user.comments : [];
    if (cardComments) {
      if (commentsList.length === 0) {
        cardComments.innerHTML = '<div class="empty">нет комментариев</div>';
      } else {
        const preview = commentsList.slice(0, 3).map(entry => '<li>' + entry.replace(/[&<>"']/g, function(ch) {
          return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] || ch;
        }) + '</li>').join('');
        const more = commentsList.length > 3 ? '<div class="more">и ещё ' + (commentsList.length - 3) + '…</div>' : '';
        cardComments.innerHTML = '<ul>' + preview + '</ul>' + more;
      }
    }
  }

  function applyCommentsToUser(user, comments) {
    const cleaned = Array.isArray(comments) ? comments.filter(Boolean) : [];
    user.comments = cleaned;
    user.comments_count = cleaned.length;
    user._commentText = cleaned.map(entry => entry.toLowerCase()).join(' ');
    if (typeof window.apply === 'function') {
      try {
        window.apply();
      } catch (err) {
        console.error('Failed to re-render gallery after comment update', err);
      }
    }
    refreshCardForUser(user);
    updateProfileStatsIfVisible(user);
    showFlash('Комментарии обновлены', 'success');
  }

  async function submitComments() {
    if (saveInProgress || !activeUser) {
      return;
    }
    const username = activeUser.username || '';
    if (!username) {
      showFlash('Нельзя обновить комментарии без username', 'error');
      return;
    }
    const comments = parseCommentsFromTextarea();
    saveInProgress = true;
    if (saveBtn) {
      saveBtn.disabled = true;
    }
    try {
      const response = await fetch(COMMENTS_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        body: JSON.stringify({ username, comments })
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || 'HTTP ' + response.status);
      }
      const payload = await response.json();
      const updated = Array.isArray(payload.comments) ? payload.comments : comments;
      applyCommentsToUser(activeUser, updated);
      closeModal();
    } catch (err) {
      console.error('Failed to update comments', err);
      showFlash('Не удалось обновить комментарии', 'error');
      saveInProgress = false;
      if (saveBtn) {
        saveBtn.disabled = false;
      }
    }
  }

  async function deleteProfile(user) {
    const username = user && user.username ? user.username : '';
    if (!username) {
      showFlash('Нельзя удалить профиль без username', 'error');
      return;
    }
    if (!window.confirm('Удалить профиль @' + username + ' и все связанные записи?')) {
      return;
    }
    try {
      const response = await fetch(DELETE_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        body: JSON.stringify({ username })
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || 'HTTP ' + response.status);
      }
      await response.json();
      const key = (username || '').toLowerCase();
      if (window.DATA_MAP && typeof window.DATA_MAP.delete === 'function') {
        window.DATA_MAP.delete(key);
      }
      if (Array.isArray(window.DATA)) {
        const index = window.DATA.indexOf(user);
        if (index !== -1) {
          window.DATA.splice(index, 1);
        } else {
          const foundIndex = window.DATA.findIndex(item => (item && (item.username || '').toLowerCase()) === key);
          if (foundIndex !== -1) {
            window.DATA.splice(foundIndex, 1);
          }
        }
      }
      if (typeof window.apply === 'function') {
        try {
          window.apply();
        } catch (err) {
          console.error('Failed to re-render gallery after deletion', err);
        }
      }
      if (typeof window.showGallery === 'function') {
        try {
          window.showGallery(true);
        } catch (err) {
          console.error('Failed to return to gallery view', err);
        }
      }
      setSelectionMode(false);
      showFlash('Профиль удалён', 'success');
    } catch (err) {
      console.error('Failed to delete profile', err);
      showFlash('Не удалось удалить профиль', 'error');
    }
  }

  if (backdrop) {
    backdrop.addEventListener('click', closeModal);
  }
  if (cancelBtn) {
    cancelBtn.addEventListener('click', closeModal);
  }
  if (saveBtn) {
    saveBtn.addEventListener('click', submitComments);
  }

  const originalRender = window.renderProfile;
  if (typeof originalRender !== 'function') {
    console.warn('Admin панель: renderProfile не найден');
    return;
  }

  window.renderProfile = function(user, updateHash, preferredTabKey) {
    setSelectionMode(false);
    const result = originalRender.apply(this, arguments);
    try {
      const profileView = document.getElementById('profileView');
      if (!profileView) {
        return result;
      }
      const actions = profileView.querySelector('.profile-actions');
      if (!actions) {
        return result;
      }
      let editBtn = actions.querySelector('.admin-control.edit');
      if (!editBtn) {
        editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'admin-control edit';
        editBtn.innerHTML = '✏️ Редактировать комментарии';
        actions.appendChild(editBtn);
      }
      let deleteBtn = actions.querySelector('.admin-control.delete');
      if (!deleteBtn) {
        deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'admin-control delete';
        deleteBtn.innerHTML = '🗑️ Удалить профиль';
        actions.appendChild(deleteBtn);
      }
      editBtn.onclick = function(ev) {
        ev.preventDefault();
        openModal(user);
      };
      deleteBtn.onclick = function(ev) {
        ev.preventDefault();
        deleteProfile(user);
      };
    } catch (err) {
      console.error('Admin UI error', err);
    }
    return result;
  };
})();
</script>
"""


def _inject_admin_panel(html: str) -> str:
    """Append admin editing assets to the gallery HTML."""

    marker = "</body>"
    if marker in html:
        return html.replace(marker, _ADMIN_PANEL_SNIPPET + marker, 1)
    return html + _ADMIN_PANEL_SNIPPET


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

    def _read_json_payload(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None, "Empty body"
        raw = self.rfile.read(length)
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "Body must be UTF-8 encoded"
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as err:
            return None, f"Invalid JSON: {err.msg}"
        if not isinstance(payload, dict):
            return None, "JSON body must be an object"
        return payload, None

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
                html = _inject_admin_panel(html)
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

        if path == "/login":
            if self.auth_manager is None:
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
            return

        if not self._ensure_authenticated():
            return

        payload, error = self._read_json_payload()
        if error:
            self._send_text(
                json.dumps({"error": error}, ensure_ascii=False),
                status=HTTPStatus.BAD_REQUEST,
                content_type="application/json; charset=utf-8",
            )
            return

        try:
            if path == "/api/admin/comments":
                username = str(payload.get("username") or "").strip()
                comments = _sanitize_comments(payload.get("comments"))
                result = _update_profile_comments(username, comments)
                self._send_text(
                    json.dumps({"ok": True, **result}, ensure_ascii=False),
                    content_type="application/json; charset=utf-8",
                )
                return

            if path == "/api/admin/delete-profile":
                username = str(payload.get("username") or "").strip()
                result = _delete_profile(username)
                self._send_text(
                    json.dumps({"ok": True, **result}, ensure_ascii=False),
                    content_type="application/json; charset=utf-8",
                )
                return

            if path == "/api/admin/delete-profiles":
                raw_usernames = payload.get("usernames")
                if isinstance(raw_usernames, str):
                    usernames = [raw_usernames]
                elif isinstance(raw_usernames, (list, tuple)):
                    usernames = [str(item) for item in raw_usernames]
                else:
                    raise ValueError("usernames must be a list of strings")
                result = _delete_profiles(usernames)
                self._send_text(
                    json.dumps({"ok": True, **result}, ensure_ascii=False),
                    content_type="application/json; charset=utf-8",
                )
                return

        except ValueError as err:
            self._send_text(
                json.dumps({"error": str(err)}, ensure_ascii=False),
                status=HTTPStatus.BAD_REQUEST,
                content_type="application/json; charset=utf-8",
            )
            return
        except LookupError as err:
            self._send_text(
                json.dumps({"error": str(err)}, ensure_ascii=False),
                status=HTTPStatus.NOT_FOUND,
                content_type="application/json; charset=utf-8",
            )
            return
        except sqlite3.Error as err:
            log.exception("Database error during POST %s", path)
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

