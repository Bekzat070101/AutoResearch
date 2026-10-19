# -*- coding: utf-8 -*-
"""25-L06(d)：/api/health 的 auth.recovery 公告全局恢复状态。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402,F401
import main  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "live" / "qiyan.db")
    monkeypatch.delenv("QIYAN_RECOVERY_STATE", raising=False)
    monkeypatch.delenv("QIYAN_RECOVERY_EVENT", raising=False)
    store.init_db()


@pytest.fixture
def client():
    return TestClient(main.app)


def test_recovery_lost(client, monkeypatch):
    monkeypatch.setenv("QIYAN_RECOVERY_STATE", "lost")
    monkeypatch.setenv("QIYAN_RECOVERY_EVENT", "2026-10-18")
    r = client.get("/api/health")
    assert r.json()["auth"]["recovery"] == {"event_id": "2026-10-18", "state": "lost"}


def test_recovery_bogus_is_normal(client, monkeypatch):
    monkeypatch.setenv("QIYAN_RECOVERY_STATE", "bogus")
    r = client.get("/api/health")
    assert r.json()["auth"]["recovery"]["state"] == "normal"


def test_recovery_unset(client):
    r = client.get("/api/health")
    assert r.json()["auth"]["recovery"] == {"event_id": "", "state": "normal"}
