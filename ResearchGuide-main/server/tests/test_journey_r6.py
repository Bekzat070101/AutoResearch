# -*- coding: utf-8 -*-
"""Codex 第六轮按学生实际走一遍复现的问题（服务端部分）。不调真的模型。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dialogue  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402
import submission  # noqa: E402
import workbench  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r6.db")
    store.init_db()


def _two_portraits_with_the_same_project():
    uid = store.create_user("两份画像")["uid"]
    a = store.list_portraits(uid)[0]["id"]
    store.save_project(uid, "same", {"name": "A 里的项目", "reviews": []})
    store.open_new_portrait(uid)
    b = store.active_portrait_id(uid)
    store.save_project(uid, "same", {"name": "B 里的项目", "reviews": []})
    store.activate_portrait(uid, a)
    return uid, a, b


def test_a_switch_between_reading_the_project_and_the_portrait_cannot_mix_them(monkeypatch):
    """原来先读项目、再在另一个事务里读画像：两次之间切到 B，A 的项目配上 B 的画像，评阅记进了 B（Codex 复现）。
    现在项目和画像一次读出来；这里让第一次读项目之后立刻切到 B，B 的项目都不该被记上。"""
    uid, a, b = _two_portraits_with_the_same_project()
    real_get, switched = store.get_project, []

    def get_then_switch(uid_, pid):
        got = real_get(uid_, pid)
        if not switched:
            switched.append(1)
            store.activate_portrait(uid_, b)
        return got

    monkeypatch.setattr(store, "get_project", get_then_switch)
    seen = []
    monkeypatch.setattr(submission, "review", lambda data, p: seen.append(p["name"]) or {"passed": 5, "total": 5, "inventory": []})
    r = TestClient(main.app).post(f"/api/projects/same/submit?uid={uid}", content=b"PK zip",
                                  headers={"Content-Type": "application/zip"})
    assert r.status_code == 200
    assert seen == ["A 里的项目"]
    assert real_get(uid, "same")["name"] == "B 里的项目" and real_get(uid, "same")["reviews"] == []


def test_a_project_page_from_another_portrait_is_refused():
    """项目页是在 A 里打开的，交之前切到了 B（B 里也有同一个项目）：不收，让学生刷新。"""
    uid, a, b = _two_portraits_with_the_same_project()
    store.activate_portrait(uid, b)
    c = TestClient(main.app)
    r = c.post(f"/api/projects/same/submit?uid={uid}&portrait={a}", content=b"PK zip",
               headers={"Content-Type": "application/zip"})
    assert r.status_code == 409 and "画像已经切换" in r.json()["detail"]
    assert store.get_project(uid, "same")["reviews"] == []


def _action(uid, payload):
    return store.save_action(uid, conversation_id="c1", action="micro_task", title="读一篇综述", payload=payload)


def test_accepting_a_model_action_is_refused_and_creates_no_task():
    """上游 PR #19：对话不再布置任务。原来这里测「20分钟」这类松散字段也能建出任务；
    现在接受任何模型动作都要被拒（ValueError），不能悄悄建任务；拒绝则记成 abandoned。"""
    uid = store.create_user("行动")["uid"]
    a = _action(uid, {"time_budget_min": "20分钟", "difficulty": "中等偏难 3", "steps": "先读摘要再读结论",
                      "rubric": "能说清楚结论", "brief": ["不是字符串"]})
    aid = a.get("action_id") or a["id"]
    with pytest.raises(ValueError, match="对话不再布置任务"):
        dialogue.action_event(uid, aid, "accept")
    assert store.list_tasks(uid) == []
    out = dialogue.action_event(uid, aid, "decline")
    assert out["status"] == "abandoned"


def test_a_failed_task_creation_leaves_the_action_unaccepted(monkeypatch):
    """建任务出错时，行动不能先记成「已接受」：原来刷新后只剩「去任务区完成」，任务区却什么都没有。"""
    uid = store.create_user("行动")["uid"]
    a = _action(uid, {})
    aid = a.get("action_id") or a["id"]

    def broken(uid_, action):
        raise ValueError("bad task")

    monkeypatch.setattr(workbench, "task_from_action", broken)
    with pytest.raises(ValueError):
        dialogue.action_event(uid, aid, "accept")
    assert store.get_action(uid, aid)["status"] == "offered"
    assert store.list_tasks(uid) == []


@pytest.mark.parametrize("raw, want", [(20, 20), (20.7, 20), ("20", 20), ("约 30-40 分钟", 30), ("", 20), (None, 20),
                                       ("很久", 20), (True, 20), (float("nan"), 20), (100000, 240), (1, 5)])
def test_model_numbers_are_read_leniently(raw, want):
    assert workbench._as_int(raw, 20, 5, 240) == want
