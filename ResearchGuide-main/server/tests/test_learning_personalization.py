"""Evidence, focus and cross-account question-cache regression checks."""
import json
import time

import pytest

import datacite
import interests
import llm
import memory
import recommendations as rec
import store
import tutor
import workbench
from schemas import UserFact


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "learning.db")
    store.init_db()
    for cache in (rec._PLANS, rec._RESULTS, rec._SEARCH, datacite._CACHE, datacite._SEARCH):
        cache.clear()


def user(text="我想准备Agent工程开发面试，先理解工具调用和失败处理"):
    uid = store.create_user("学习者")["uid"]
    pid = store.list_portraits(uid)[0]["id"]
    fact = store.add_fact(UserFact(user_id=uid, key="goal:practice", value=text,
                                  source="declared", status="active"))
    return uid, pid, fact


def op(key, value, quote):
    return {"op": "add", "key": key, "value": value, "evidence_quote": quote,
            "source": "declared", "affects": "task_kind"}


def test_department_short_answer_with_space():
    uid, _, _ = user()
    good, bad = memory.validate_dialogue_ops(uid, [op("department", "信息管理系", "信管 大二")], ["信管 大二"])
    assert not bad and good[0]["value"] == "信息管理系"


def test_unsupported_summary_recovers_only_original_sentence():
    uid, _, _ = user()
    quote = "我会写Python，但还没设计过完整的软件架构"
    good, bad = memory.validate_dialogue_ops(uid, [op("base:code", "精通Python，独立部署过大型推荐系统", quote)], [quote])
    assert not bad and good[0]["value"] == quote
    assert "verbatim_quote_recovered" in good[0]["notes"]
    assert "大型" not in good[0]["value"]


@pytest.mark.parametrize("quote", ["我室友想做Agent开发，我还没有想法", "假如我会Python就可以做Agent开发", "不存在的原话目标与个人经历"])
def test_recovery_does_not_bypass_evidence_or_subject(quote):
    uid, _, _ = user()
    pool = [quote] if "不存在" not in quote else ["我大二"]
    good, bad = memory.validate_dialogue_ops(uid, [op("base:code", "精通Python和Agent开发", quote)], pool)
    assert not good and bad


def test_current_direction_overrides_stale_selection_without_changing_interests():
    uid, pid, f = user()
    store.set_trail(uid, pid, {"dir": "790", "sel": ["520.40"], "dirs": ["520.20", "520.40", "790"],
                               "byDir": {"520.20": {"done": ["a"], "tasks": {"a": "task-a"}}}})
    assert interests.direction_nodes(uid) == ["790"]
    s = rec.snapshot(uid, pid)
    assert s["nodes"] == ["790"]
    assert any(e["id"] == "focus:790" for e in s["evidence"])
    assert store.list_facts(uid)[0].value == f.value
    assert store.get_trail(uid, pid)["byDir"]["520.20"]["done"] == ["a"]


def test_focus_is_sent_to_both_plan_and_rank(monkeypatch):
    uid, pid, f = user()
    store.set_trail(uid, pid, {"dir": "790", "sel": ["520.20"]})
    paper = {"id": "2001.00001", "title": "Economics and causal inference", "summary": "Economic causal inference. " * 5}
    seen = []
    def model(system, payload, **kw):
        d = json.loads(payload); seen.append((kw["tag"], d))
        if kw["tag"] == "recommend-plan":
            return {"queries": ["economics causal inference"], "evidence_refs": [f.id], "directions": []}
        return {"items": [{"id": paper["id"], "evidence_refs": ["focus:790"], "paper_quote": paper["title"]}]}
    monkeypatch.setattr(llm, "ready", lambda *a: True)
    monkeypatch.setattr(llm, "chat_json", model)
    monkeypatch.setattr(rec, "search", lambda *a: ([paper], ""))
    r = rec.recommend(uid, pid)
    assert r["status"] == "personalized"
    assert seen[0][1]["selected_nodes"] == ["790"]
    assert list(seen[1][1]["selected_nodes"]) == ["790"]


def test_datacite_only_accepts_arxiv_registration_and_preserves_submitted_date(monkeypatch):
    record = {"id": "10.48550/arxiv.2210.03629", "attributes": {
        "titles": [{"title": "Real paper"}], "dates": [{"dateType": "Submitted", "date": "2022-10-06T00:00:00Z"}],
        "descriptions": [{"descriptionType": "Abstract", "description": "Registered abstract"}],
        "url": "https://malicious.invalid/ignore"}}
    p = datacite.parse(record)
    assert p["id"] == "2210.03629" and p["published"] == "2022-10-06"
    assert p["url"] == "https://arxiv.org/abs/2210.03629" and "text" not in p
    assert datacite.parse({**record, "id": "10.1234/other"}) is None
    with pytest.raises(ValueError):
        datacite.paper("https://private.invalid")


def test_task_uses_current_portrait_evidence_and_persists_basis(monkeypatch):
    uid, pid, f = user()
    monkeypatch.setattr(llm, "ready", lambda *a: True)
    def model(system, payload, **kw):
        d = json.loads(payload)
        assert d["learning_context"]["portrait_id"] == pid
        assert any(e["id"] == f.id for e in d["learning_context"]["evidence"])
        return {"stem": "观察一个工具失败的例子", "ask": "比较失败处理", "given": [], "steps": ["观察", "比较", "解释"],
                "rubric": [{"criterion": str(i)} for i in range(3)], "evidence_refs": [f.id, "other-account-fact"]}
    monkeypatch.setattr(llm, "chat_json", model)
    task = workbench.generate_task(uid, "ai", title="误差与反馈", brief="观察并比较系统行为")
    basis = store.get_task(task.id).formal["personalization"]
    assert basis["evidence_refs"] == [f.id] and basis["portrait_id"] == pid


def test_verified_search_survives_process_cache_clear_and_network_failure(monkeypatch):
    paper = {"id": "2210.03629", "title": "Agent tool use", "summary": "Registered abstract"}
    monkeypatch.setattr(rec, "_retrieve", lambda q: [paper])
    got, warning = rec.search("agent tool use", time.monotonic() + 30)
    assert not warning and got[0]["id"] == paper["id"]
    rec._SEARCH.clear()
    key = "recommend-search:" + rec.digest("agent tool use")
    with store._conn() as c:
        c.execute("UPDATE paper_pool SET fetched_at=datetime('now','-11 minutes') WHERE key=?", (key,))
        old = c.execute("SELECT fetched_at FROM paper_pool WHERE key=?", (key,)).fetchone()[0]
        c.execute("UPDATE paper_pool SET fetched_at=? WHERE key=?", (old.replace(" ", "T") + "+00:00", key))
    monkeypatch.setattr(rec, "_retrieve", lambda q: (_ for _ in ()).throw(OSError("offline")))
    got, warning = rec.search("agent tool use", time.monotonic() + 30)
    assert got[0]["id"] == paper["id"] and "缓存" in warning
    assert "uid" not in store.latest_paper_pool(key)["papers"][0]


def test_public_abstract_fallback_does_not_become_personal_reading_or_include_uploads():
    uid, pid, _ = user()
    store.put_paper_text("2210.03629", {"title": "Public Agent paper", "text": "Verified public abstract", "source": "abstract"})
    store.put_paper_text("local:abcdef123456", {"title": "Private upload", "text": "Secret", "source": "abstract"})
    s = rec.snapshot(uid, pid)
    assert "2210.03629" in [p["id"] for p in rec.local_candidates(s)]
    assert not any(p["id"].startswith("local:") for p in rec.local_candidates(s))
    assert s["read_ids"] == [] and not any(e["key"] == "completed_reading" for e in s["evidence"])


def test_task_feedback_changes_snapshot_without_claiming_mastery(monkeypatch):
    uid, pid, _ = user()
    monkeypatch.setattr(llm, "ready", lambda *a: False)
    task = workbench.generate_task(uid, "ai", title="误差与反馈")
    before = rec.snapshot(uid, pid)
    workbench.submit(uid, task, "这是一个具体的观察：输入变化导致输出变化，因为系统对提示里的条件敏感。")
    after = rec.snapshot(uid, pid)
    assert before["version"] != after["version"]
    record = next(e for e in after["evidence"] if e["id"] == "task:" + task.id)
    assert record["source"] == "behavior" and "不等于已掌握" in record["value"]


def test_personal_tutor_cannot_reuse_another_accounts_questions(monkeypatch):
    a, _, _ = user("我想准备Agent开发面试并理解系统设计")
    b, _, _ = user("我想研究基准污染，理解统计检验与实验对照")
    monkeypatch.setattr(llm, "ready", lambda *args: True)
    calls = []
    def model(system, payload, **kw):
        calls.append(kw["uid"])
        return {}
    monkeypatch.setattr(llm, "chat_json", model)
    monkeypatch.setattr(tutor, "_valid_items", lambda data, slots, sents, paper: [{"qid": "q", "voice": "llm", "question": "理解本站原文", "criteria": []}])
    paper = {"id": "2210.03629", "text": "A real original sentence about tool use. " * 8, "source": "html"}
    st = {"key": "abstract", "start": 0, "end": len(paper["text"]), "label": "摘要"}
    slots = [{"qid": "q", "type": "own", "field": "理解"}]
    tutor._gen_station(a, paper, st, slots)
    tutor._gen_station(b, paper, st, slots)
    tutor._gen_station(a, paper, st, slots)
    assert calls == [a, b]
