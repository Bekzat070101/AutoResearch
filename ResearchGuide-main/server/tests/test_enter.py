# -*- coding: utf-8 -*-
"""进入启研按人记：account.entered、enter 幂等、prefs.kit_id 不被冲掉、history 带 onboard_done。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import dialogue  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "enter.db")
    store.init_db()
    auth._HITS.clear()
    monkeypatch.setattr(llm, "enabled", lambda: False)
    monkeypatch.setattr(llm, "ready", lambda uid=None: False)


@pytest.fixture
def client():
    return TestClient(main.app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, name="new_one"):
    r = client.post("/api/auth/register", json={
        "username": name, "password": "secret-ok", "nickname": name, "consent": True,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_new_account_is_not_entered(client):
    body = _register(client)
    assert body["entered"] is False
    me = client.get("/api/auth/me", headers=_h(body["token"])).json()
    assert me["entered"] is False


def test_enter_is_idempotent_and_keeps_kit(client):
    body = _register(client)
    uid, token = body["uid"], body["token"]
    store.set_prefs(uid, kit_id="llm-eval")
    first = client.post("/api/onboard/enter", json={"uid": uid}, headers=_h(token))
    assert first.status_code == 200, first.text
    assert first.json()["entered"] is True
    prefs = store.get_prefs(uid)
    at = prefs["entered_at"]
    assert at
    assert prefs["kit_id"] == "llm-eval"
    second = client.post("/api/onboard/enter", json={"uid": uid}, headers=_h(token))
    assert second.status_code == 200
    assert store.get_prefs(uid)["entered_at"] == at
    assert store.get_prefs(uid)["kit_id"] == "llm-eval"


def test_old_account_counts_as_entered(client):
    body = _register(client, "old_user")
    uid = body["uid"]
    with store._conn() as c:
        c.execute("UPDATE users SET created_at=? WHERE id=?", ("2026-10-01T00:00:00", uid))
    assert auth.account(uid)["entered"] is True


def test_history_has_onboard_done(client):
    body = _register(client, "talker")
    uid, token = body["uid"], body["token"]
    h0 = client.get("/api/dialogue/history", params={"uid": uid}, headers=_h(token)).json()
    assert "onboard_done" in h0
    assert h0["onboard_done"] is False
    client.post("/api/onboard/start", json={"uid": uid}, headers=_h(token))
    st = store.get_onboard_state(uid)
    store.set_onboard_state(uid, {**st, "keep": 1})
    dialogue._sync_onboard_state(uid, {"active_plan": {"direction": "ai"}})
    after = store.get_onboard_state(uid)
    assert after["phase"] == "gathering"
    assert after.get("keep") == 1
    h1 = client.get("/api/dialogue/history", params={"uid": uid}, headers=_h(token)).json()
    assert h1["onboard_done"] is False
    assert h1["guide"]["known_count"] == 0


def test_message_and_reload_do_not_enter_new_account(client):
    body = _register(client, "still_guiding")
    uid, token = body["uid"], body["token"]
    store.add_message(uid, "user", "我是大一，想先聊聊")
    me = client.get("/api/auth/me", headers=_h(token)).json()
    assert me["entered"] is False
    profile = client.get("/api/onboard/readiness", params={"uid": uid}, headers=_h(token))
    assert profile.status_code == 200
    assert profile.json()["first_visit"] is True
    assert profile.json()["ready"] is False
    # 提醒不是权限闸门；零信息也可以显式进入。
    assert client.post("/api/onboard/enter", json={"uid": uid}, headers=_h(token)).json()["entered"]


def test_readiness_does_not_disclose_another_accounts_profile(client):
    a = _register(client, "profile_owner")
    b = _register(client, "other_owner")
    assert client.get("/api/onboard/readiness", params={"uid": a['uid']}).status_code == 401
    assert client.get("/api/onboard/readiness", params={"uid": a['uid']}, headers=_h(b['token'])).status_code == 403


def test_enter_during_stream_is_not_409(client):
    body = _register(client, "stream_ok")
    uid, token = body["uid"], body["token"]
    r = client.post("/api/onboard/enter", json={"uid": uid}, headers=_h(token))
    assert r.status_code == 200
    assert r.status_code != 409
