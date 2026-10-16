# -*- coding: utf-8 -*-
"""草稿 / 路径 / 工具包按人隔离，刷新后仍在。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402
import workbench  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mem.db")
    store.init_db()
    auth._HITS.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _reg(client, name):
    r = client.post("/api/auth/register", json={
        "username": name, "password": "secret-ok", "nickname": name, "consent": True,
    })
    assert r.status_code == 200
    return r.json()


def test_draft_and_trail_and_prefs_isolated(client):
    a = _reg(client, "user_a")
    b = _reg(client, "user_b")
    t = workbench.generate_task(a["uid"], "ai", 1, title="极限")
    put = client.put(f"/api/tasks/{t.id}/draft", json={"text": "我先写下已知"}, headers=_h(a["token"]))
    assert put.status_code == 200
    assert put.json()["text"] == "我先写下已知"
    assert client.get(f"/api/tasks/{t.id}/draft", headers=_h(b["token"])).status_code == 404
    assert client.put(f"/api/tasks/{t.id}/draft", json={"text": "偷"}, headers=_h(b["token"])).status_code == 404
    got = client.get(f"/api/tasks/{t.id}/draft", headers=_h(a["token"]))
    assert got.json()["text"] == "我先写下已知"
    assert got.json().get("updated_at")

    pid = "portrait-a"
    client.put(f"/api/projects/{pid}/trail", json={"data": {"code": "ai", "done": ["n1"]}},
               headers=_h(a["token"]))
    other = client.get(f"/api/projects/{pid}/trail", headers=_h(b["token"]))
    assert other.json() == {}
    mine = client.get(f"/api/projects/{pid}/trail", headers=_h(a["token"]))
    assert mine.json()["code"] == "ai"

    client.put("/api/me/prefs", json={"kit_id": "llm-eval"}, headers=_h(a["token"]))
    assert client.get("/api/me/prefs", headers=_h(a["token"])).json()["kit_id"] == "llm-eval"
    assert client.get("/api/me/prefs", headers=_h(b["token"])).json().get("kit_id") == ""


def test_no_per_user_sqlite_file(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "one.db")
    store.init_db()
    a = store.register_user("aa", "secret-ok", "甲")
    b = store.register_user("bb", "secret-ok", "乙")
    store.set_draft(a["uid"], "t1", "hello")
    store.set_draft(b["uid"], "t1", "other")
    assert store.DB_PATH.exists()
    assert not list(store.DB_PATH.parent.glob("users/*"))
    assert store.get_draft(a["uid"], "t1") == "hello"
    assert store.get_draft(b["uid"], "t1") == "other"
