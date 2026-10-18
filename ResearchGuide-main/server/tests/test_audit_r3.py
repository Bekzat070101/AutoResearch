# -*- coding: utf-8 -*-
"""Codex 第三轮全量审计复现过的几处数据正确性问题。每条测试先把当时的场景搭出来，再看结果。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import memory  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402
import submission  # noqa: E402
from schemas import UserFact  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r3.db")
    store.init_db()


def _uid(name="t"):
    return store.create_user(name)["uid"]


def test_review_started_in_one_portrait_is_not_saved_into_another_with_the_same_project_id(monkeypatch):
    uid = _uid()
    store.list_portraits(uid)
    store.save_project(uid, "same", {"name": "同一个项目", "reviews": []})
    p = store.get_project(uid, "same")
    started_in = store.active_portrait_id(uid)
    store.open_new_portrait(uid)  # 评阅还在跑，切到 B
    store.save_project(uid, "same", {"name": "同一个项目", "reviews": []})  # 在 B 里又挑了同一个项目，id 一样
    monkeypatch.setattr(submission, "review", lambda data, p: {"passed": 5, "total": 5, "inventory": []})
    out = main._review_and_record(uid, p, b"zip", started_in)
    assert out["recorded"] is False
    assert store.get_project(uid, "same")["reviews"] == []  # B 里的这个项目没有被记上 A 的评阅


def test_correcting_the_direction_changes_the_key_not_just_the_label():
    uid = _uid()
    old = UserFact(user_id=uid, category="interest", key="direction:ai", value="人工智能", confidence=0.9,
                   source="declared", evidence=[], status="confirmed", affects="direction_choice")
    store.add_fact(old)
    quote = "我其实想做数学"
    op = {"op": "replace", "key": "direction:math", "value": "数学", "category": "interest", "source": "declared",
          "evidence_quote": quote, "target_fact_id": old.id, "affects": "direction_choice"}
    accepted, rejected = memory.validate_ops(uid, [op], [quote])
    assert not rejected and accepted[0]["op"] == "add" and "replace_key_changed_as_add" in accepted[0]["notes"]
    memory.apply_ops(uid, accepted, "d1")
    live = {f.key: f.value for f in memory.active_facts(uid)}
    assert live.get("direction:math") == "数学" and "direction:ai" not in live  # 原来是 direction:ai = 数学


def test_a_replacement_carries_its_own_source_and_confidence():
    uid = _uid()
    old = UserFact(user_id=uid, category="interest", key="interest:topic", value="机器学习", confidence=1.0,
                   source="user_edit", evidence=[], status="confirmed", affects="direction_choice")
    store.add_fact(old)
    quote = "我对图神经网络有兴趣"
    # 引文只撑住一部分（0.6，内容字够 8 个才算数）：这条新值会被降级成 inferred，可信度跟着降
    op = {"op": "replace", "key": "interest:topic", "value": "图神经网络的研究和兴趣方向", "category": "interest",
          "source": "declared", "evidence_quote": quote, "target_fact_id": old.id, "affects": "direction_choice"}
    accepted, rejected = memory.validate_ops(uid, [op], [quote])
    assert not rejected and accepted[0]["source"] == "inferred"
    memory.apply_ops(uid, accepted, "d1")
    f = store.get_fact(old.id)
    assert f.value == "图神经网络的研究和兴趣方向"
    assert f.source == "inferred" and f.confidence == memory.confidence_for("inferred")  # 原来还挂着 user_edit / 1.0


def test_task_feedback_survives_a_reload():
    c = TestClient(main.app)
    uid = c.post("/api/auth/login", json={"nickname": "反馈"}).json()["uid"]
    t = c.post("/api/tasks/generate", json={"uid": uid, "direction": "ai", "level": 1}).json()
    fb = c.post(f"/api/tasks/{t['id']}/submit", json={"uid": uid, "payload": "我读完了，因为例子清楚所以懂了。"}).json()
    again = c.get(f"/api/tasks/{t['id']}?uid={uid}").json()
    assert again["feedback"]["score"] == fb["score"]  # 刷新之后照样拿得到这次反馈


def test_nonfinite_credits_are_refused_and_old_rows_are_repaired():
    c = TestClient(main.app)
    uid = c.post("/api/auth/login", json={"nickname": "学分"}).json()["uid"]
    bad = [{"course": "无穷大", "grade": "90", "credits": "1e309", "term": "25-26学年度1学期"}]
    r = c.post("/api/me/transcript", json={"uid": uid, "courses": bad})
    assert r.status_code == 400 and "学分" in r.json()["detail"]
    assert c.get(f"/api/me/transcript?uid={uid}").status_code == 200
    # 老库里已经存进去的无穷大：启动时修掉，读成绩单、导出都不再 500
    with sqlite3.connect(store.DB_PATH) as db:
        db.execute("INSERT INTO enrollments(id,user_id,course,grade,credits,term,kind,status,created_at,updated_at)"
                   " VALUES('e1',?,'坏行','90',9e999,'t','','completed','x','x')", (uid,))
    store.init_db()
    assert c.get(f"/api/me/transcript?uid={uid}").status_code == 200
    assert store.list_enrollments(uid)[0]["credits"] == 0


def test_duplicate_derived_facts_are_all_retracted_when_their_courses_go():
    uid = _uid()
    for _ in range(2):  # 老库里同一个推导结论有两条
        store.add_fact(UserFact(user_id=uid, category="capability", key="transcript:code", value="Python 90",
                                confidence=0.95, source="derived", evidence=[], status="active", affects="task_difficulty"))
    memory.sync_transcript_facts(uid)  # 成绩单是空的：这个结论推不出来了
    assert [f for f in memory.active_facts(uid) if f.key == "transcript:code"] == []


def test_passing_a_card_in_a_new_portrait_writes_its_evidence_there(monkeypatch):
    uid = _uid()
    store.list_portraits(uid)
    monkeypatch.setattr(reading, "kit", lambda kit_id: {"id": kit_id, "name": "K", "version": 1, "dimensions": []})
    monkeypatch.setattr(reading.arxiv, "fulltext", lambda aid: {"title": "论文", "text": "x"})
    monkeypatch.setattr(reading, "review_card", lambda *a: {"pass": True, "passed": 5, "total": 5})
    reading.submit_card(uid, "k", "2401.00001", {}, {})
    assert any(f.key == "card:k:2401.00001" for f in memory.active_facts(uid))
    store.open_new_portrait(uid)  # 新画像：卡还在，证据不在
    reading.submit_card(uid, "k", "2401.00001", {}, {})  # 再交一版、照样过线
    assert any(f.key == "card:k:2401.00001" for f in memory.active_facts(uid))  # 原来这里没有


def test_duplicate_card_rows_count_as_one_paper():
    uid = _uid()
    with sqlite3.connect(store.DB_PATH) as db:
        for i in range(3):  # 老库里并发写出来的：同一篇、同一最高版本三行
            db.execute("INSERT INTO cards(id,user_id,kit_id,arxiv_id,version,data,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (f"c{i}", uid, "k", "2401.00001", 2,
                        json.dumps({"arxiv_id": "2401.00001", "version": 2, "status": "pass", "i": i}), "pass", f"2026-10-0{i + 1}"))
    rows = store.latest_cards(uid, "k")
    assert len(rows) == 1 and rows[0]["i"] == 2  # 取最后写的那行



def test_changing_a_multi_value_key_retires_the_old_value():
    uid = _uid()
    old = UserFact(user_id=uid, category="interest", key="goal:phd", value="读博", confidence=0.9,
                   source="declared", evidence=[], status="confirmed", affects="direction_choice")
    store.add_fact(old)
    quote = "我不打算读博了，想直接工作"
    op = {"op": "replace", "key": "goal:work", "value": "直接工作", "category": "interest", "source": "declared",
          "evidence_quote": quote, "target_fact_id": old.id, "affects": "direction_choice"}
    accepted, rejected = memory.validate_ops(uid, [op], [quote])
    assert not rejected
    memory.apply_ops(uid, accepted, "d1")
    live = {f.key for f in memory.active_facts(uid)}
    assert "goal:work" in live and "goal:phd" not in live  # 原来两个目标同时留着


def test_task_feedback_survives_switching_portraits_back_and_forth():
    c = TestClient(main.app)
    uid = c.post("/api/auth/login", json={"nickname": "来回切"}).json()["uid"]
    a = store.list_portraits(uid)[0]["id"]
    t = c.post("/api/tasks/generate", json={"uid": uid, "direction": "ai", "level": 1}).json()
    c.post(f"/api/tasks/{t['id']}/submit", json={"uid": uid, "payload": "我读完了，因为例子清楚所以懂了。"})
    assert c.post("/api/portraits", json={"uid": uid}).status_code == 200
    assert c.post("/api/portraits/activate", json={"uid": uid, "id": a}).status_code == 200
    again = c.get(f"/api/tasks/{t['id']}?uid={uid}").json()
    assert again["feedback"] and "score" in again["feedback"]
