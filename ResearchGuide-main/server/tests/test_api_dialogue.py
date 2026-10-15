# -*- coding: utf-8 -*-
"""HTTP 层测试：新对话接口 + /api/onboard/* 兼容 + P0 归属校验。

不联网、不调模型。运行：pytest server/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda: False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def login(client, nick="小明") -> str:
    return client.post("/api/auth/login", json={"nickname": nick}).json()["uid"]


# ---------- 新对话接口 ----------


def test_dialogue_turn_returns_reply_and_metadata(client):
    uid = login(client)
    r = client.post("/api/dialogue/turn", json={"uid": uid, "message": "我不知道该研究什么"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"]
    assert body["conversation_id"]
    assert body["degraded"] is True          # 没配模型
    assert "memory_version" in body and "trace" in body


def test_dialogue_turn_rejects_empty_message(client):
    uid = login(client)
    r = client.post("/api/dialogue/turn", json={"uid": uid, "message": "   "})
    assert r.status_code == 400


def test_dialogue_turn_rejects_unknown_user(client):
    r = client.post("/api/dialogue/turn", json={"uid": "nope", "message": "在吗"})
    assert r.status_code == 404


def test_dialogue_continues_the_same_conversation(client):
    uid = login(client)
    first = client.post("/api/dialogue/turn", json={"uid": uid, "message": "第一句"}).json()
    second = client.post("/api/dialogue/turn",
                         json={"uid": uid, "message": "第二句",
                               "conversation_id": first["conversation_id"]}).json()
    assert second["conversation_id"] == first["conversation_id"]

    h = client.get("/api/dialogue/history", params={"uid": uid}).json()
    assert [m["role"] for m in h["messages"]] == ["user", "assistant", "user", "assistant"]


def test_dialogue_history_is_portrait_scoped(client):
    uid = login(client)
    client.post("/api/dialogue/turn", json={"uid": uid, "message": "我在旧画像说的话"})
    client.post("/api/portraits", json={"uid": uid})
    h = client.get("/api/dialogue/history", params={"uid": uid}).json()
    # 旧画像的对话不能泄漏到新画像（新画像只有开场问候）
    assert not any(m["text"] == "我在旧画像说的话" for m in h["messages"])
    assert not any(m["role"] == "user" for m in h["messages"])


# ---------- 行动事件 ----------


def test_dialogue_action_flow(client):
    import store
    import dialogue
    uid = login(client)
    client.post("/api/directions/choose", json={"uid": uid, "code": "ai"})

    r = client.post("/api/dialogue/turn", json={"uid": uid, "message": "开始吧"}).json()
    assert r.get("next_action") is None, "降级路径本轮不派任务"

    old = store.save_action(uid, conversation_id="c1", action="micro_task",
                            title="老微任务", direction="ai")
    acc = dialogue.action_event(uid, old["id"], "accept")
    assert acc["status"] == "accepted"
    assert acc.get("task_id")

    explore = store.save_action(uid, conversation_id="c1", action="explore_direction",
                                title="去看方向")
    ev = dialogue.action_event(uid, explore["id"], "accept")
    assert ev["status"] == "accepted"
    assert not ev.get("task_id")


def test_dialogue_action_rejects_bad_input(client):
    uid = login(client)
    assert client.post("/api/dialogue/action",
                       json={"uid": uid, "action_id": "x", "event": "explode"}).status_code == 400
    assert client.post("/api/dialogue/action",
                       json={"uid": uid, "action_id": "missing", "event": "accept"}).status_code == 404


# ---------- 流式 ----------


def parse_sse(text: str) -> list[tuple[str, dict]]:
    import json
    out = []
    event = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: ") and event:
            out.append((event, json.loads(line[6:])))
            event = None
    return out


def test_stream_emits_stage_progress_and_result(client):
    uid = login(client)
    r = client.post("/api/dialogue/stream", json={"uid": uid, "message": "我不知道该研究什么"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(r.text)
    names = [p["name"] for k, p in events if k == "stage"]
    assert names[:3] == ["observe", "decide", "memory"]
    assert "compose" in names

    kinds = [k for k, _ in events]
    assert kinds[-1] == "result"
    result = events[-1][1]
    assert result["reply"]
    # 流式与一次性返回同一个形状
    assert set(result) >= {"reply", "conversation_id", "memory_version", "next_action", "degraded"}


def test_stream_does_not_lose_the_turn(client):
    """走流式之后，历史里必须留下这一轮，和 /turn 行为一致。"""
    uid = login(client)
    client.post("/api/dialogue/stream", json={"uid": uid, "message": "流式的一句话"})
    h = client.get("/api/dialogue/history", params={"uid": uid}).json()
    assert [m["role"] for m in h["messages"]] == ["user", "assistant"]
    assert h["messages"][0]["text"] == "流式的一句话"


def test_stream_rejects_empty_message(client):
    uid = login(client)
    assert client.post("/api/dialogue/stream",
                       json={"uid": uid, "message": " "}).status_code == 400


# ---------- /api/onboard/* 兼容 ----------


def test_onboard_wizard_still_works_offline(client):
    uid = login(client)
    start = client.post("/api/onboard/start", json={"uid": uid}).json()
    assert start["reply"] and start["options"]
    r = client.post("/api/onboard/message", json={"uid": uid, "msg": start["options"][0]})
    assert r.status_code == 200
    assert r.json()["reply"]


def test_onboard_result_shape_unchanged(client):
    uid = login(client)
    client.post("/api/onboard/start", json={"uid": uid})
    r = client.get("/api/onboard/result", params={"uid": uid}).json()
    assert set(r) == {"messages", "facts", "state"}


def test_onboard_cold_start_goes_to_kernel_when_model_enabled(client, monkeypatch):
    """冷启动第一句也必须进新内核。

    老逻辑把 `not state` 排在模型判断之前，新用户的第一句会落到老向导的第 1 轮问题，
    要等到第二句才进内核——同一功能两种行为，前端拿到的响应形状也会前后不一致。
    """
    monkeypatch.setattr(llm, "enabled", lambda: True)
    import dialogue
    monkeypatch.setattr(dialogue, "turn", lambda uid, msg, **k: {
        "reply": "内核回复", "conversation_id": "c1", "move": "clarify",
        "memory_version": 1, "degraded": False, "rejected_ops": [],
        "next_action": None, "offered_actions": [], "tool_results": [],
        "facts_added": [], "facts_changed": [], "pending_action": None, "trace": {},
    })

    uid = login(client)   # 全新用户，onboard_state 还是空的
    r = client.post("/api/onboard/message", json={"uid": uid, "msg": "我不知道该研究什么"})

    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "内核回复"
    assert "dialogue" in body, "冷启动第一句没有走新内核"
    # 旧前端要的字段一个都不能少
    assert set(body) >= {"reply", "hint", "options", "facts", "state", "done"}


# ---------- P0：任务归属 ----------


def test_cannot_submit_someone_elses_task(client):
    owner = login(client, "owner")
    attacker = login(client, "attacker")
    t = client.post("/api/tasks/generate",
                    json={"uid": owner, "direction": "ai", "level": 1}).json()

    r = client.post(f"/api/tasks/{t['id']}/submit",
                    json={"uid": attacker, "payload": "我随便写点什么就能完成别人的任务"})
    assert r.status_code == 403
    # 拥有者的任务状态没有被别人改动
    assert client.get(f"/api/tasks/{t['id']}?uid={owner}").json()["status"] != "done"
