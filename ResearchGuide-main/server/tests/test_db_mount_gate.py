# -*- coding: utf-8 -*-
"""盘没挂上不建空库：init_db 不建文件、/api/* 回 503 db_not_ready、health 报出来；挂上后恢复。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import account_store  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture
def nas(tmp_path, monkeypatch):
    db = tmp_path / "mnt" / "nas" / "qiyan.db"
    monkeypatch.setenv("FC_FUNCTION_NAME", "qiyan")
    monkeypatch.setenv("QIYAN_DB", str(db))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "DB_READY", store.DB_READY)
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE", raising=False)
    monkeypatch.delenv("BACKUP_OSS_BUCKET", raising=False)
    auth._HITS.clear()
    yield db
    account_store.reset()


def test_unmounted_nas_blocks_then_recovers(nas, monkeypatch):
    store.init_db()
    assert not nas.exists()
    assert store.DB_READY is False
    client = TestClient(main.app)
    body = {"username": "mount_user", "password": "secret-ok", "nickname": "m", "consent": True}
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 503
    assert r.json()["code"] == "db_not_ready"
    h = client.get("/api/health")
    assert h.status_code == 200
    j = h.json()
    assert j["ok"] is False
    assert j["db"]["ready"] is False
    assert j["db"]["blocked"] == "nas_not_mounted"
    assert j["db"]["writable"] is False
    assert not nas.exists()

    monkeypatch.setattr(store, "_own_mount", lambda path: True)
    store.init_db()
    assert store.DB_READY is True
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 200, r.text


def test_local_dev_without_fc_is_not_blocked(nas, monkeypatch):
    monkeypatch.delenv("FC_FUNCTION_NAME")
    assert store.db_blocked() is None
