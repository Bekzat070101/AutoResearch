# -*- coding: utf-8 -*-
"""11：prefs.directions、ranking、最长匹配、会话失效提示。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import interests  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
import planner  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda: False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)


def _client_user():
    client = TestClient(main.app)
    body = client.post("/api/auth/register", json={
        "username": "diruser", "password": "secret-ok", "nickname": "dir", "consent": True,
    }).json()
    return client, body["uid"], {"Authorization": f"Bearer {body['token']}"}


def test_prefs_put_directions_does_not_clear_kit():
    client, uid, h = _client_user()
    first = client.put("/api/me/prefs", json={"kit_id": "llm-eval"}, headers=h)
    assert first.status_code == 200, first.text
    assert first.json()["kit_id"] == "llm-eval"
    r = client.put("/api/me/prefs", json={"directions": ["190.20", "nope", "190.20"]}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kit_id"] == "llm-eval"
    assert body["directions"] == ["190.20"]


def test_prefs_drops_unknown_nodes_and_caps_at_eight():
    client, uid, h = _client_user()
    ids = list(interests.table()["nodes"])[:10]
    r = client.put("/api/me/prefs", json={"directions": ids + ["not-a-node"]}, headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()["directions"]) == 8
    assert all(x in interests.table()["nodes"] for x in r.json()["directions"])


def test_match_topic_takes_longest_zh():
    assert interests._match_topic("", "对社会心理学感兴趣") == "psy-soc"


def test_ranking_reads_model_written_interest():
    uid = store.create_user("r")["uid"]
    memory.apply_ops(uid, memory.validate_ops(uid, [{
        "op": "add", "key": "interest:social-psychology", "value": "社会心理学",
        "evidence_quote": "社会心理学", "affects": "direction_choice",
    }], ["社会心理学"])[0], "")
    items = planner.direction_ranking(uid)
    assert items
    assert items[0]["node"] == "190.20"
    assert any("社会心理" in w for w in items[0]["why"])


def test_ranking_route_is_rules_only():
    client, uid, h = _client_user()
    r = client.get(f"/api/directions/ranking?uid={uid}", headers=h)
    assert r.status_code == 200
    assert "items" in r.json()


def test_web_has_session_persist_and_choose_direction():
    js = (Path(__file__).resolve().parents[2] / "web" / "js" / "app.js").read_text(encoding="utf-8")
    assert "function persistSession" in js
    assert "function chooseDirection" in js
    assert "await saveTrail" in js
    assert "function reloadInto" in js
    assert "登录已失效，请重新登录" in js
    assert 'localStorage.setItem("rg_token"' in js
    chat = (Path(__file__).resolve().parents[2] / "web" / "js" / "chat.js").read_text(encoding="utf-8")
    assert 'rg_chat_${kind}_${S.uid}_${S.portraitId}_${cid || "new"}' in chat
    assert "可能适合你的方向" in chat
    assert "规则推测" in chat
    assert "这里只看摘要" not in chat
