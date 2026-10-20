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
    assert r.json()["reason"] == "nas_not_mounted"
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


@pytest.mark.parametrize("db_env, db_path, reason", [
    (None, "/code/server/data/demo.db", "no_qiyan_db"),      # 没设 QIYAN_DB：库落在代码包里，每次发版清空
    ("/tmp/qiyan.db", "/tmp/qiyan.db", "db_in_tmp"),          # 现网 2026-10-20 就是这种
    ("/private/tmp/qiyan.db", "/private/tmp/qiyan.db", "db_in_tmp"),
    ("/var/tmp/qiyan.db", "/var/tmp/qiyan.db", "db_in_tmp"),
])
def test_fc_without_persistent_db_is_blocked_unless_allowed(nas, monkeypatch, db_env, db_path, reason):
    if db_env is None:
        monkeypatch.delenv("QIYAN_DB")
    else:
        monkeypatch.setenv("QIYAN_DB", db_env)
    monkeypatch.setattr(store, "DB_PATH", Path(db_path))
    monkeypatch.delenv("QIYAN_ALLOW_EPHEMERAL", raising=False)
    assert store.db_blocked() == reason
    assert store.db_writable() is False
    monkeypatch.setenv("QIYAN_ALLOW_EPHEMERAL", "1")  # 测试函数 / 试运行：deploy_fc.py 自动带上
    assert store.db_blocked() is None
    monkeypatch.setenv("QIYAN_ALLOW_EPHEMERAL", "0")
    assert store.db_blocked() == reason


def test_fc_without_qiyan_db_refuses_to_serve_and_says_why(nas, tmp_path, monkeypatch):
    pkg_db = tmp_path / "code" / "server" / "data" / "demo.db"
    monkeypatch.delenv("QIYAN_DB")
    monkeypatch.delenv("QIYAN_ALLOW_EPHEMERAL", raising=False)
    monkeypatch.setattr(store, "DB_PATH", pkg_db)
    store.init_db()
    assert not pkg_db.exists() and store.DB_READY is False
    client = TestClient(main.app)
    body = {"username": "pkg_user", "password": "secret-ok", "nickname": "p", "consent": True}
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 503
    assert r.json()["code"] == "db_not_ready" and r.json()["reason"] == "no_qiyan_db"
    assert "QIYAN_DB" in r.json()["detail"]
    j = client.get("/api/health").json()
    assert j["ok"] is False and j["db"]["blocked"] == "no_qiyan_db" and j["db"]["ephemeral"] is True
    monkeypatch.setenv("QIYAN_ALLOW_EPHEMERAL", "1")
    store.init_db()
    assert store.DB_READY is True and pkg_db.exists()
    assert client.post("/api/auth/register", json=body).status_code == 200
