# -*- coding: utf-8 -*-
"""工具 registry 的离线测试：不联网。

工具是模型唯一的「看世界」通道，全部只读；失败必须结构化返回，不能抛异常中断对话。
运行：pytest server/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False

import store  # noqa: E402
import tools  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()


def make_user() -> str:
    return store.create_user("t")["uid"]


def test_unknown_tool_is_rejected_not_raised():
    uid = make_user()
    r = tools.run(uid, {"tool": "db.delete_everything", "args": {}})
    assert r["ok"] is False and r["error"] == "unknown tool"


def test_bad_args_shape_does_not_crash():
    uid = make_user()
    r = tools.run(uid, {"tool": "course.search", "args": "not a dict"})
    assert r["ok"] is False and r["error"] == "query is required"


def test_only_registered_tools_are_described():
    text = tools.describe()
    for name in tools.TOOL_SPECS:
        assert name in text


def test_course_search_falls_back_to_the_user_message(monkeypatch):
    """模型漏传 query 时不浪费一整轮：用用户这句话当检索词，并标明来源。"""
    uid = make_user()
    called = {}

    def fake_search(query, **kw):
        called["query"] = query
        return {"ok": True, "items": [{"name": "机器学习导论"}], "term": "2026秋"}

    monkeypatch.setattr(tools, "search_courses", fake_search)
    r = tools.run(uid, {"tool": "course.search", "args": {}},
                  context={"query_hint": "北大有哪些机器学习的课"})

    assert called["query"] == "北大有哪些机器学习的课"
    assert r["ok"] is True
    assert r["query_from"] == "user_message"
    assert r["items"][0]["name"] == "机器学习导论"


def test_course_search_marks_model_supplied_query(monkeypatch):
    uid = make_user()
    monkeypatch.setattr(tools, "search_courses", lambda q, **kw: {"ok": True, "items": []})
    r = tools.run(uid, {"tool": "course.search", "args": {"query": "线性代数"}})
    assert r["query_from"] == "model"


def test_course_search_without_query_or_context_still_fails_cleanly():
    uid = make_user()
    r = tools.run(uid, {"tool": "course.search", "args": {}})
    assert r["ok"] is False and "query" in r["error"]


def test_course_search_reports_failure_without_inventing(monkeypatch):
    uid = make_user()
    monkeypatch.setattr(tools, "search_courses",
                        lambda q, **kw: {"ok": False, "error": "network URLError"})
    r = tools.run(uid, {"tool": "course.search", "args": {"query": "机器学习"}})
    assert r["ok"] is False
    assert "items" not in r  # 失败时不带任何结果，避免下游误用
    assert "network" in r["error"]


def test_tool_exception_becomes_structured_failure(monkeypatch):
    uid = make_user()

    def boom(*a, **k):
        raise RuntimeError("pku subprocess died")

    monkeypatch.setattr(tools, "search_courses", boom)
    r = tools.run(uid, {"tool": "course.search", "args": {"query": "x"}})
    assert r["ok"] is False and "RuntimeError" in r["error"]


@pytest.mark.parametrize("name", ["project.search", "project.review"])
def test_project_tools_are_unavailable_to_dialogue(name):
    uid = make_user()
    assert name not in tools.TOOL_SPECS
    assert name not in tools.describe()
    # 旧模型决策即使还带着这个名字，也不能绕过工具清单执行。
    r = tools.run(uid, {"tool": name, "args": {"direction": "ai", "project_id": "p1"}})
    assert r == {"ok": False, "tool": name, "error": "unknown tool"}


def test_dialogue_keeps_profile_curriculum_and_reading_tools():
    assert set(tools.TOOL_SPECS) == {
        "course.search", "transcript.summary", "major_lookup", "major_detail",
        "match_transcript", "minor_programs", "course_lookup",
        "reading.search", "reading.detail",
    }
