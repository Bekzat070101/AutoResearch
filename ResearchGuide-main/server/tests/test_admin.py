# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth
TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "a.db")
    store.init_db()
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("BACKUP_KEY", "backup-key-16++++")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("QIYAN_SNAP", str(tmp_path / "snap.db"))
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    admin.SNAP = tmp_path / "snap.db"


@pytest.fixture
def client():
    return TestClient(main.app)


def test_admin_requires_token(client):
    assert client.post("/api/admin/backup").status_code == 403
    assert client.post("/api/admin/backup", headers={"X-Admin-Token": "nope"}).status_code == 403
    r = client.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert "users" in body and "expired_sessions" in body


def test_backup_vacuum_and_restore(client, tmp_path):
    import gzip
    import sqlite3
    uid = store.create_user("备份人")["uid"]
    out = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN}).json()
    assert out["ok"] and out["integrity"] == "ok" and out["oss"] == "local"
    src = tmp_path / "bk" / out["object"]
    raw = gzip.decompress(admin._aes(src.read_bytes(), b"backup-key-16++++", False))
    dest = tmp_path / "restored.db"
    dest.write_bytes(raw)
    with sqlite3.connect(dest) as c:
        assert c.execute("SELECT nickname FROM users WHERE id=?", (uid,)).fetchone()[0] == "备份人"


def test_weekly_maintenance_clears_expired_sessions(client):
    uid = store.create_user("过期")["uid"]
    store.create_session(uid, -10)
    r = client.post("/api/admin/maintenance", headers={"X-Admin-Token": TOKEN}).json()
    assert r["ok"]
    assert r["sessions"] >= 1
    after = client.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN}).json()
    assert after["expired_sessions"] == 0


def test_admin_user_delete(client):
    uid = store.create_user("人工删")["uid"]
    r = client.post("/api/admin/user/delete", json={"uid": uid}, headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200
    assert store.get_user(uid) is None


def test_health_gains_checks_and_keeps_ok(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert "checks" in h and "secret_set" in h["checks"]
    assert "model_pool" in h
    assert h["version"] == "0.2.0"
