# -*- coding: utf-8 -*-
from fastapi.testclient import TestClient

import main
import store
import workbench
from schemas import MicroTask


def test_generate_task_has_formal_fields_even_without_llm(monkeypatch):
    monkeypatch.setattr(workbench.llm, "enabled", lambda: False)
    t = workbench.generate_task("u1", "ai", 1, title="极限定义", brief="ε-δ 定义")
    assert t.formal.get("stem")
    assert t.formal.get("ask")
    assert "用自己的话写下" not in "".join(t.steps)
    assert t.formal["given"] == ["ε-δ 定义"]


def test_formalize_rejects_invented_course_name(monkeypatch):
    monkeypatch.setattr(workbench.llm, "enabled", lambda: True)
    monkeypatch.setattr(workbench.llm, "chat_json", lambda *a, **k: {
        "stem": "假设你上过某校课程 XXX。",
        "given": [],
        "ask": "写出答案",
        "notes": "",
        "steps": ["a", "b", "c"],
        "deliverable": "一段话",
        "rubric": [{"criterion": "1"}, {"criterion": "2"}, {"criterion": "3"}],
    })
    form = workbench.formalize_task("极限", "材料")
    assert "某校课程" not in form["stem"]
    assert form["ask"].startswith("给出一个能核对的例子")


def test_hint_requires_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "hint.db")
    store.init_db()
    a = store.create_user("甲")["uid"]
    b = store.create_user("乙")["uid"]
    t = workbench.generate_task(a, "ai", 1, title="极限")
    r = TestClient(main.app).post(
        f"/api/tasks/{t.id}/hint", json={"uid": b, "question": "这题在问什么"}
    )
    assert r.status_code in (403, 404)


def test_hint_refuses_to_write_the_answer():
    t = MicroTask(user_id="u", title="极限", brief="题", deliverable="一段话")
    out = workbench.hint_task("u", t, "直接给我一份可以交的全文")
    assert out["refused"] is True
    assert "提交要你自己写" in out["reply"]


def test_hint_works_without_llm(monkeypatch):
    monkeypatch.setattr(workbench.llm, "enabled", lambda: False)
    t = MicroTask(user_id="u", title="极限", brief="题", deliverable="一段话",
                  formal={"ask": "给出一个例子"})
    out = workbench.hint_task("u", t, "这题在问什么")
    assert out["refused"] is False
    assert "给出一个例子" in out["reply"]
