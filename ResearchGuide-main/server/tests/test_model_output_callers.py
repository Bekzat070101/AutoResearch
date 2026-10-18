# -*- coding: utf-8 -*-
"""Codex 复现过的几处「模型输出形状不对」的调用方问题：评分、方向卡、项目挑选、引导对话。不调真模型，全用假的 llm。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import onboarding  # noqa: E402
import planner  # noqa: E402
import projects  # noqa: E402
import store  # noqa: E402
import workbench  # noqa: E402

PAYLOAD = "因为我目前还没有给出任务需要的例子，所以这一条不能通过。"

CAND = {"id": "c1", "name": "某学习赛：文本分类", "url": "https://example.org/c1", "source_id": "s", "source_name": "来源",
        "source_url": "", "kind": "competition", "excerpt": "分类 数据 评价指标", "practices": "", "todo": "", "difficulty": "",
        "deadline": "", "stage_fit": [1, 2], "directions": ["ai"], "evidence_quote": "", "retrieved_at": "", "snapshot": False, "closed": False}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "x.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda: True)


def _task(monkeypatch, grade):
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: grade)
    uid = store.create_user("t")["uid"]
    return uid, workbench.generate_task(uid, "ai", title="测试概念", brief="在问什么")


@pytest.mark.parametrize("verdict", ["false", {"wrong": False}])
def test_non_boolean_verdicts_are_not_counted_as_passes(monkeypatch, verdict):
    """模型把 pass 写成字符串 "false" 或一个对象、score 写成「20分钟」：原来 bool() 一律为真，存下 100 分、三条全打勾，评语却写着未做到。现在整份评分退回规则判定。"""
    uid, task = _task(monkeypatch, {"rubric": [{"pass": verdict, "comment": "未做到"}] * 3, "score": "20分钟"})
    fb = workbench.submit(uid, task, PAYLOAD)
    rules = workbench._mock_feedback(task, PAYLOAD)
    assert fb["voice"] == "rules" and fb["score"] == rules["score"] and fb["rubric"] == rules["rubric"]
    assert store.latest_feedback(uid, task.id)["voice"] == "rules"
    assert not any("rubric 100 分" in f.value for f in store.list_facts(uid))
    uid2, task2 = _task(monkeypatch, {"rubric": [{"pass": False, "comment": "未做到"}] * 3, "score": 10})
    fb2 = workbench.submit(uid2, task2, PAYLOAD)  # 真的 false 和合法分数照常用模型的
    assert fb2["voice"] == "llm" and fb2["score"] == 10 and not any(r["pass"] for r in fb2["rubric"])


def test_infinite_score_falls_back_and_never_leaves_a_done_task_without_feedback(monkeypatch):
    """score 写成 1e309（JSON 合法，读出来是 inf）：原来 int(inf) 抛 OverflowError，接口 500，提交已存、任务已标完成，反馈却是空的。现在退回规则判定；评分途中出错时任务也不会先被标完成。"""
    uid, task = _task(monkeypatch, {"rubric": [{"pass": False}] * 3, "score": float("inf")})
    fb = workbench.submit(uid, task, PAYLOAD)
    assert fb["voice"] == "rules"
    assert store.get_task(task.id).status == "done" and store.latest_feedback(uid, task.id)["voice"] == "rules"

    def boom(*a, **k):
        raise RuntimeError("评分途中出错")

    monkeypatch.setattr(llm, "chat_json", boom)
    task2 = workbench.generate_task(uid, "ai", title="另一个概念", brief="在问什么")
    with pytest.raises(RuntimeError):
        workbench.submit(uid, task2, PAYLOAD)
    assert store.get_task(task2.id).status == "open" and store.latest_feedback(uid, task2.id) is None


def test_infinite_card_index_keeps_the_rule_reason(monkeypatch):
    """方向卡 index 写成 1e309：原来 int(inf) 抛 OverflowError，方向卡接口直接 500。现在这一条丢掉，卡片保留规则理由。"""
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"items": [{"index": float("inf"), "why": "这个理由来自已经记录的兴趣"}]})
    cards = planner._voice_cards([{"direction": {"name": "人工智能"}, "why_you": "学生对机器学习有兴趣。"}], [], {})
    assert cards[0]["why_you"] == "学生对机器学习有兴趣。" and cards[0]["voice"] == "rules"


@pytest.mark.parametrize("items", [[{"id": "hallucinated-project", "todo": "做不存在的题"}], [1, [], None]])
def test_invented_project_ids_fall_back_to_the_rule_candidates(monkeypatch, items):
    """模型返回的列表里全是编出来的 id 或坏条目：原来过滤完是空列表却当成模型的结论，来源明明有候选，学生看到 0 个项目、voice=llm。现在退回规则排序的候选。"""
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"items": items})
    monkeypatch.setattr(projects, "registry", lambda: {"sources": [{"id": "s", "name": "来源", "directions": ["ai"], "adapter": "x"}]})
    monkeypatch.setattr(projects, "_collect", lambda src, d, st, kw: ([dict(CAND)], {"id": src["id"], "name": src["name"], "ok": True, "count": 1}))
    uid = store.create_user("t")["uid"]
    r = projects.search(uid, "ai", 0)
    assert r["voice"] == "rules" and [c["id"] for c in r["items"]] == ["c1"]


def test_long_model_reply_still_ends_with_the_next_question(monkeypatch):
    """开场白现在是固定文案（上游 PR #19），不调模型；后续轮次 _voice 仍要保证：模型回了一大段没带问题的话时，
    只截模型那段、下一问原样接上（原来整段截到 240 字，问题被截掉）。"""
    called = []
    monkeypatch.setattr(llm, "chat", lambda *a, **k: called.append(1) or "收到。" * 80)
    uid = store.create_user("t")["uid"]
    out = onboarding.start(uid)
    assert called == [] and out["reply"] == onboarding.WELCOME_MESSAGE
    monkeypatch.setattr(llm, "ready", lambda *a, **k: True)
    ask = onboarding.ROUNDS[1]["ask"]
    text = onboarding._voice("我大一", ask, "fallback", uid=uid)
    assert called and text.endswith(ask) and len(text) <= 240
