import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

import vsco_rescan as rescan
from profile_link_scanner import ProfileMediaCollection, connect_db, utc_now_iso


@pytest.fixture()
def rescan_db(tmp_path):
    db = tmp_path / "items.db"
    conn = connect_db(db)
    for chat in (101, 202):
        conn.execute("INSERT INTO links(chat_id,username,url,created_at) VALUES(?,?,?,?)",
                     (chat, "example", "https://vsco.co/example/gallery", utc_now_iso()))
    conn.commit()
    conn.close()
    state = rescan.connect_state_db(tmp_path / "state.db")
    yield db, state
    state.close()


def load(db, state, chat_id=None):
    return rescan._load_profiles(db, chat_id=chat_id, limit=None,
                                 state_conn=state, alphabetical=False)


def run(entries, db, state, **kwargs):
    return asyncio.run(rescan.rescan_profiles(
        entries, db_path=db, concurrency=2, max_width=2048, delay=0,
        target_count=0, state_conn=state, retry_delay=0, **kwargs))


def test_rescan_saves_each_chat_and_tracks_completion_separately(rescan_db, monkeypatch):
    db, state = rescan_db
    collect = AsyncMock(return_value=ProfileMediaCollection(["https://cdn.example/photo.jpg"]))
    monkeypatch.setattr(rescan, "collect_profile_media", collect)
    entries, skipped = load(db, state, 101)
    assert skipped == 0
    assert run(entries, db, state).succeeded == 1
    pending, skipped = load(db, state)
    assert skipped == 1 and [entry.chat_id for entry in pending] == [202]
    assert run(pending, db, state).succeeded == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT chat_id, image_url FROM items ORDER BY chat_id").fetchall() == [
            (101, "https://cdn.example/photo.jpg"), (202, "https://cdn.example/photo.jpg"),
        ]
    finally:
        conn.close()
    assert load(db, state) == ([], 2)


def test_rescan_retries_transient_failures_and_keeps_permanent_failures_retryable(rescan_db, monkeypatch):
    db, state = rescan_db
    entries, _ = load(db, state, 101)
    collect = AsyncMock(side_effect=TimeoutError("temporary failure"))
    monkeypatch.setattr(rescan, "collect_profile_media", collect)
    assert run(entries, db, state).succeeded == 0
    assert collect.await_count == 3
    assert load(db, state, 101)[0] == entries
    collect.side_effect = [TimeoutError(), ProfileMediaCollection(["https://cdn.example/photo.jpg"])]
    assert run(entries, db, state).succeeded == 1
    assert load(db, state, 101) == ([], 1)


def test_partial_rescan_is_not_marked_complete(rescan_db, monkeypatch):
    db, state = rescan_db
    entries, _ = load(db, state, 101)
    collect = AsyncMock(return_value=ProfileMediaCollection(
        ["https://cdn.example/photo.jpg"], warnings=["incomplete"]))
    monkeypatch.setattr(rescan, "collect_profile_media", collect)
    assert run(entries, db, state).succeeded == 0
    assert load(db, state, 101)[0] == entries
    collect.return_value = ProfileMediaCollection(["https://cdn.example/photo.jpg"])
    assert run(entries, db, state).succeeded == 1
    assert state.execute("SELECT status FROM scanned_profiles_by_chat").fetchone() == ("unchanged",)


def test_legacy_url_only_history_does_not_skip_unidentified_chats(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE scanned_profiles(profile_url TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO scanned_profiles VALUES('https://vsco.co/example/gallery','updated')")
    conn.commit()
    conn.close()
    state = rescan.connect_state_db(path)
    try:
        assert rescan._load_processed_profiles(state) == set()
        assert state.execute("SELECT COUNT(*) FROM scanned_profiles").fetchone() == (1,)
    finally:
        state.close()
