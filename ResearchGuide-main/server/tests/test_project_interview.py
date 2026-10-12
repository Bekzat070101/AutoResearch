# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import projects  # noqa: E402
import store  # noqa: E402

llm.enabled = lambda: False


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    projects._IV.store.clear() if hasattr(projects._IV, "store") else None
    # TTLCache 没有统一清空接口时，换一个新的
    monkeypatch.setattr(projects, "_IV", type(projects._IV)(projects.IV_TTL, 2048))


def test_interview_starts_with_fixed_q0():
    uid = store.create_user("t")["uid"]
    r = projects.interview(uid)
    assert r["done"] is False
    assert "哪一类" in r["question"]
    assert any(o["id"] == "proof" for o in r["options"])
    assert any("还不知道" in o["label"] for o in r["options"])


def test_interview_unknown_is_ok():
    uid = store.create_user("t")["uid"]
    r = projects.interview(uid)
    r2 = projects.interview(uid, r["session_id"], option_id="unknown")
    assert "还不知道" in r2["chain"]
    assert r2["done"] is False or r2["done"] is True


def test_interview_belongs_to_uid():
    a = store.create_user("a")["uid"]
    b = store.create_user("b")["uid"]
    r = projects.interview(a)
    with pytest.raises((KeyError, PermissionError)):
        projects.interview(b, r["session_id"], option_id="proof")


def test_interview_caps_at_five_turns():
    uid = store.create_user("t")["uid"]
    r = projects.interview(uid)
    last = r
    for _ in range(6):
        last = projects.interview(uid, last["session_id"], option_id="unknown")
        if last["done"]:
            break
    assert last["done"] is True
    assert len(last["chain"]) <= 5


def test_interview_rejects_invented_course_in_options(monkeypatch):
    uid = store.create_user("t")["uid"]
    monkeypatch.setattr(projects.llm, "enabled", lambda: True)
    monkeypatch.setattr(projects.llm, "chat_json", lambda *a, **k: {
        "question": "你上过哪门？",
        "options": [
            {"id": "a", "label": "某校课程 XXX"},
            {"id": "b", "label": "另一门"},
        ],
    })
    r0 = projects.interview(uid)
    r1 = projects.interview(uid, r0["session_id"], option_id="proof")
    assert r1["done"] or all("某校课程" not in o["label"] for o in r1.get("options") or [])


def test_search_without_chain_matches_old_ranking():
    item = {
        "name": "证明题集", "excerpt": "本科证明练习", "practices": "", "todo": "",
        "directions": ["math"], "stage_fit": [0], "snapshot": False, "closed": False,
    }
    a = projects._score(item, "math", 0, "证明")
    b = projects._score(item, "math", 0, "证明", chain=None)
    assert a == b


def test_search_with_chain_prefers_matching_excerpt():
    proof = {
        "name": "习题", "excerpt": "证明 本科能做 不必先会程序", "practices": "", "todo": "",
        "directions": [], "stage_fit": [], "snapshot": False, "closed": False,
    }
    code = {
        "name": "练习", "excerpt": "能跑的程序 可视化", "practices": "", "todo": "",
        "directions": [], "stage_fit": [], "snapshot": False, "closed": False,
    }
    chain = ["证明", "本科能做", "不必先会程序"]
    assert projects._score(proof, "", 0, "", chain=chain) > projects._score(code, "", 0, "", chain=chain)


def test_search_never_invents_item(monkeypatch):
    uid = store.create_user("t")["uid"]
    cand = {
        "id": "c1", "name": "某学习赛：文本分类", "url": "https://example.org/c1",
        "source_id": "s", "source_name": "来源", "source_url": "", "kind": "competition",
        "excerpt": "分类 数据 评价指标", "practices": "", "todo": "", "difficulty": "",
        "deadline": "", "stage_fit": [1, 2], "directions": ["ai"], "evidence_quote": "",
        "retrieved_at": "", "snapshot": False, "closed": False,
    }
    monkeypatch.setattr(projects, "_collect", lambda src, d, st, kw: (
        [dict(cand)], {"id": src["id"], "name": src["name"], "ok": True, "count": 1}
    ))
    monkeypatch.setattr(projects.llm, "enabled", lambda: True)
    monkeypatch.setattr(projects.llm, "chat_json", lambda *a, **k: {
        "items": [{"id": "fake999", "practices": "x", "todo": "y", "why_fit": "z"}]
    })
    picked = projects._pick_llm([cand], "ai", 1, "", "")
    assert picked == []
    r = projects.search(uid, "ai", 1)
    assert all(it.get("id") != "fake999" for it in r["items"])
    assert all("编造" not in (it.get("name") or "") for it in r["items"])
