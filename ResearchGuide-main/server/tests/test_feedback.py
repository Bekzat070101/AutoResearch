# -*- coding: utf-8 -*-
"""意见反馈：写入用户库、校验长度、只看自己的、删号一并清掉。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import store  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(llm, "config", lambda: {"enabled": False, "base_url": "", "model": "", "has_key": False})
    monkeypatch.setattr(llm, "enabled", lambda: False)
    from fastapi.testclient import TestClient
    import main
    store.init_db()
    return TestClient(main.app)


def _uid(client, name="反馈人"):
    import auth
    auth._HITS.clear()
    return client.post("/api/auth/login", json={"nickname": name}).json()["uid"]


def test_feedback_roundtrip(client):
    uid = _uid(client)
    r = client.post("/api/feedback", json={"uid": uid, "text": "方向树点开三级之后，说明浮层挡住了一半，希望能往左挪一点。"})
    assert r.status_code == 200 and r.json()["ok"] is True
    item = r.json()["item"]
    assert item["status"] == "open" and item["created_at"]

    got = client.get(f"/api/feedback?uid={uid}").json()["items"]
    assert len(got) == 1 and got[0]["text"].startswith("方向树") and got[0]["id"] == item["id"]


def test_feedback_length_guard(client):
    uid = _uid(client)
    assert client.post("/api/feedback", json={"uid": uid, "text": "太短"}).status_code == 400
    assert client.post("/api/feedback", json={"uid": uid, "text": "字" * 4001}).status_code == 400
    assert client.get(f"/api/feedback?uid={uid}").json()["items"] == []


def test_feedback_is_per_user_and_wiped_on_delete(client):
    a, b = _uid(client, "甲"), _uid(client, "乙")
    client.post("/api/feedback", json={"uid": a, "text": "甲的反馈：今日页论文分拣的目标数写错了。"})
    client.post("/api/feedback", json={"uid": b, "text": "乙的反馈：研读搜索希望能保存历史关键词。"})
    mine = client.get(f"/api/feedback?uid={a}").json()["items"]
    assert len(mine) == 1 and "甲的反馈" in mine[0]["text"]

    store.delete_user(a)
    assert client.get(f"/api/feedback?uid={a}").status_code == 404
    assert len(client.get(f"/api/feedback?uid={b}").json()["items"]) == 1


def test_admin_sees_all_feedback_and_sets_status(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "x" * 40)
    a, b = _uid(client, "甲"), _uid(client, "乙")
    client.post("/api/feedback", json={"uid": a, "text": "甲的反馈：侧栏反馈入口希望更显眼一点。"})
    client.post("/api/feedback", json={"uid": b, "text": "乙的反馈：希望支持导出阅读卡。"})
    h = {"X-Admin-Token": "x" * 40}

    items = client.get("/api/admin/feedback", headers=h).json()["items"]
    assert len(items) == 2
    assert {i["nickname"] for i in items} == {"甲", "乙"}
    assert items[0]["created_at"] >= items[1]["created_at"]

    fid = items[0]["id"]
    r = client.post("/api/admin/feedback/status", headers=h, json={"id": fid, "status": "done"})
    assert r.status_code == 200
    done = client.get("/api/admin/feedback?status=done", headers=h).json()["items"]
    assert len(done) == 1 and done[0]["id"] == fid

    # 口令不对看不了；状态值不合法改不了
    assert client.get("/api/admin/feedback").status_code in (401, 403, 429, 503)
    assert client.post(
        "/api/admin/feedback/status", headers=h, json={"id": fid, "status": "乱写"}
    ).status_code == 400
