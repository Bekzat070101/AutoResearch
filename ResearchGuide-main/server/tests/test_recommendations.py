"""Portrait recommendations: behavioral selection, provenance and fallback."""
import json
import time

import pytest
from fastapi.testclient import TestClient

import main
import recommendations as rec
import reading_memory
import store
from schemas import UserFact, now_iso


AGENT = {"id": "2210.03629", "title": "ReAct: reasoning and acting", "summary": "An agent interleaves reasoning and tool use. " * 3,
         "url": "https://arxiv.org/abs/2210.03629", "published": now_iso()[:10]}
EVAL = {"id": "2009.03300", "title": "Measuring language understanding", "summary": "A benchmark evaluates language models. " * 3,
        "url": "https://arxiv.org/abs/2009.03300", "published": "2020-09-07"}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "recommend.db")
    store.init_db()
    for cache in (rec._PLANS, rec._RESULTS, rec._SEARCH):
        cache.clear()
    monkeypatch.setattr(rec.reading, "kits", lambda: {})
    monkeypatch.setattr(rec.llm, "ready", lambda *a: True)
    monkeypatch.setattr(rec, "search", lambda *a: ([AGENT, EVAL], ""))


def user(goal="Agent开发面试"):
    uid = store.create_user("reader")["uid"]
    pid = store.list_portraits(uid)[0]["id"]
    f = store.add_fact(UserFact(user_id=uid, key="goal:reading", value=goal, source="declared", status="active"))
    store.set_trail(uid, pid, {"dir": "520.20", "dirs": ["520.20"], "sel": ["520.20"]})
    return uid, pid, f


def model(monkeypatch, rank_override=None):
    calls = []
    def chat(system, payload, **kw):
        d = json.loads(payload)
        calls.append((kw["tag"], d))
        evidence = d.get("evidence") or d["profile"]
        f = next(e for e in evidence if e["key"] == "goal:reading")
        if kw["tag"] == "recommend-plan":
            return {"goal": f["value"], "queries": ["language model agents"], "evidence_refs": [f["id"]],
                    "directions": [{"node": "520.20.nlp", "evidence_refs": [f["id"]]}]}
        if rank_override is not None:
            return rank_override(d)
        wanted = AGENT if "Agent" in f["value"] else EVAL
        if wanted["id"] not in [p["id"] for p in d["candidates"]]:
            return {"items": []}
        return {"items": [{"id": wanted["id"], "evidence_refs": [f["id"]], "paper_quote": wanted["title"]}]}
    monkeypatch.setattr(rec.llm, "chat_json", chat)
    return calls


def test_same_direction_different_goal_changes_paper_not_just_reason(monkeypatch):
    calls = model(monkeypatch)
    u, p, f = user()
    a = rec.recommend(u, p)
    store.update_fact(f.id, value="评测科研：实验设计")
    b = rec.recommend(u, p)
    assert a["items"][0]["id"] == AGENT["id"]
    assert b["items"][0]["id"] == EVAL["id"]
    assert a["profile_version"] != b["profile_version"]
    assert len(calls) == 4
    assert store.get_trail(u, p)["dir"] == "520.20"


def test_cache_same_profile_and_publication_metadata(monkeypatch):
    calls = model(monkeypatch)
    u, p, _ = user()
    a = rec.recommend(u, p)
    a["items"][0]["title"] = "mutated caller copy"
    b = rec.recommend(u, p)
    assert len(calls) == 2
    assert b["items"][0]["title"] == AGENT["title"]
    assert b["items"][0]["url"] == AGENT["url"]


def test_no_direction_required_and_suggestions_are_broad(monkeypatch):
    model(monkeypatch)
    u, p, _ = user()
    store.set_trail(u, p, {})
    assert rec.recommend(u, p)["status"] == "personalized"
    assert rec.direction_suggestions(u, p)[0]["node"] == "520.20"
    assert store.get_trail(u, p).get("dir", "") == ""


@pytest.mark.parametrize("bad", ["foreign_id", "foreign_evidence", "invented_quote"])
def test_model_cannot_invent_papers_or_user_evidence(monkeypatch, bad):
    def wrong(d):
        row = {"id": AGENT["id"], "evidence_refs": [d["profile"][0]["id"]], "paper_quote": AGENT["title"]}
        row[{"foreign_id": "id", "foreign_evidence": "evidence_refs", "invented_quote": "paper_quote"}[bad]] = ["other-account-fact"] if bad == "foreign_evidence" else "fabricated"
        return {"items": [row]}
    model(monkeypatch, wrong)
    u, p, _ = user()
    r = rec.recommend(u, p)
    assert r["status"] == "basic" and r["warnings"]
    assert all(not v.get("evidence_refs") for v in r["items"])
    assert all(v["id"] in (AGENT["id"], EVAL["id"]) for v in r["items"])


def test_no_match_does_not_refill_irrelevant_toolkit(monkeypatch):
    model(monkeypatch, lambda d: {"items": []})
    u, p, _ = user()
    r = rec.recommend(u, p)
    assert r["status"] == "no_match" and r["items"] == []


def test_title_only_marks_insufficient_content(monkeypatch):
    model(monkeypatch)
    monkeypatch.setattr(rec, "search", lambda *a: ([{**AGENT, "summary": ""}], ""))
    u, p, _ = user()
    item = rec.recommend(u, p)["items"][0]
    assert item["evidence_strength"] == "title"
    assert "内容依据不足" in item["why"]


def test_search_failure_uses_local_candidates_with_warning(monkeypatch):
    model(monkeypatch)
    monkeypatch.setattr(rec, "search", lambda *a: ([], "外部搜索失败"))
    u, p, _ = user()
    r = rec.recommend(u, p, base=[AGENT])
    assert r["items"][0]["id"] == AGENT["id"]
    assert "外部搜索失败" in r["warnings"]


def test_model_timeout_is_explicit_basic_fallback(monkeypatch):
    monkeypatch.setattr(rec.llm, "chat_json", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    u, p, _ = user()
    r = rec.recommend(u, p, base=[AGENT])
    assert r["status"] == "basic" and r["items"] and r["warnings"]


def test_read_and_triage_records_excluded_and_invalidate_cache(monkeypatch):
    calls = model(monkeypatch)
    u, p, _ = user()
    assert rec.recommend(u, p, "today")["items"]
    store.save_triage(u, "*daily", AGENT["id"], "skip", "不符合当前目标", AGENT["title"])
    assert not rec.recommend(u, p, "today")["items"]
    reading_memory.commit_card({"id": "read-card", "uid": u, "kit_id": "llm-eval", "arxiv_id": AGENT["id"],
                               "title": AGENT["title"], "version": 1, "status": "pass", "fields": {}, "dims": {}}, AGENT)
    assert not rec.recommend(u, p)["items"]
    assert len(calls) == 6


def test_courses_status_and_drafts_in_snapshot(monkeypatch):
    u, p, _ = user()
    store.replace_enrollments(u, [{"course": "概率统计", "status": "current", "grade": "IP"}])
    store.add_fact(UserFact(user_id=u, key="base:code", value="精通Python", status="draft"))
    s = rec.snapshot(u, p)
    assert any('"status": "current"' in e["value"] for e in s["evidence"])
    assert not any(e["value"] == "精通Python" for e in s["evidence"])
    store.replace_enrollments(u, [{"course": "概率统计", "status": "completed", "grade": "90"}])
    assert s["version"] != rec.snapshot(u, p)["version"]


@pytest.mark.parametrize("change", ["portrait", "fact"])
def test_late_model_result_rejected(monkeypatch, change):
    u, p, f = user()
    def late(*a, **kw):
        if change == "portrait":
            store.open_new_portrait(u)
        else:
            store.update_fact(f.id, value="已改变的目标")
        return {"queries": [], "evidence_refs": [f.id], "directions": []}
    monkeypatch.setattr(rec.llm, "chat_json", late)
    with pytest.raises(rec.StaleProfile):
        rec.recommend(u, p)


def test_large_direction_includes_descendant_catalogues():
    u, p, _ = user()
    store.set_trail(u, p, {"dir": "520", "sel": ["520"]})
    store.put_paper_dir("520.40.db", AGENT["id"], "cs-db", 1, "arxiv", AGENT["title"])
    assert AGENT["id"] in [v["id"] for v in rec.local_candidates(rec.snapshot(u, p))]


def test_no_topic_basic_without_model_call(monkeypatch):
    u, p, f = user()
    store.update_fact(f.id, status="deleted")
    monkeypatch.setattr(rec.llm, "chat_json", lambda *a, **kw: pytest.fail("should not call model"))
    r = rec.recommend(u, p, base=[AGENT])
    assert r["status"] == "basic" and "依据较少" in r["message"]


def test_api_scope_and_daily_shape(monkeypatch):
    model(monkeypatch)
    u, p, _ = user()
    other, other_pid, _ = user("数学科研")
    client = TestClient(main.app)
    assert client.get(f"/api/papers/recommend?uid={u}&portrait_id={other_pid}").status_code == 409
    assert client.get(f"/api/papers/recommend?uid={u}&portrait_id={p}&scene=invalid").status_code == 400
    r = client.get(f"/api/papers/recommend?uid={u}&portrait_id={p}")
    assert r.status_code == 200 and r.json()["status"] == "personalized"
    r = client.get(f"/api/daily?uid={u}&portrait_id={p}")
    assert r.status_code == 200
    assert r.json()["recommendation"]["status"] == "personalized"
    assert r.json()["goal"] == 3 and "done_today" in r.json()
    r = client.get(f"/api/directions/recommend?uid={u}&portrait_id={p}")
    assert r.json()["cards"][0]["node"] == "520.20"


@pytest.mark.real_auth
def test_cannot_read_another_account_profile(monkeypatch):
    model(monkeypatch)
    u, p, _ = user()
    v, _, _ = user()
    token = store.create_session(v, 3600)
    r = TestClient(main.app).get(f"/api/papers/recommend?uid={u}&portrait_id={p}", headers={"Authorization": "Bearer " + token})
    assert r.status_code in (403, 404)


def test_cached_result_does_not_survive_candidate_change(monkeypatch):
    calls = model(monkeypatch)
    u, p, _ = user()
    rec.recommend(u, p, base=[AGENT])
    rec.recommend(u, p, base=[AGENT, EVAL])
    assert len(calls) == 3  # shared profile plan, fresh candidate ranking


def test_search_budget_and_unsafe_queries(monkeypatch):
    u, p, f = user()
    monkeypatch.setattr(rec.llm, "chat_json", lambda *a, **kw: {"queries": ["Peking University", "agent tool", "third query"],
                       "evidence_refs": [f.id], "directions": [{"node": "invented", "evidence_refs": [f.id]}]})
    plan = rec.prepare(rec.snapshot(u, p), time.monotonic() + 10)
    assert plan["queries"] == ["agent tool"] and plan["directions"] == []


def test_expired_fact_not_injected():
    u, p, _ = user()
    f = store.add_fact(UserFact(user_id=u, key="interest:old", value="旧兴趣", status="active", valid_until="2000-01-01T00:00:00Z"))
    assert f.id not in [e["id"] for e in rec.snapshot(u, p)["evidence"]]


def test_no_model_metadata_is_merged(monkeypatch):
    def rank(d):
        return {"items": [{"id": AGENT["id"], "evidence_refs": [d["profile"][0]["id"]], "paper_quote": AGENT["title"],
                           "title": "invented title", "url": "https://malicious.invalid"}]}
    model(monkeypatch, rank)
    u, p, _ = user()
    item = rec.recommend(u, p)["items"][0]
    assert item["title"] == AGENT["title"] and item["url"] == AGENT["url"]


@pytest.mark.parametrize("key,before,after", [
    ("reading_barrier", "不懂工具调用与系统设计", "不懂基准实验与统计检验"),
    ("base:code", "有Python工程实践", "更熟悉统计理论，尚无编程实践"),
])
def test_foundation_or_difficulty_reaches_ranker_and_invalidates_result(monkeypatch, key, before, after):
    def rank(d):
        context = next(e for e in d["profile"] if e["key"] == key)
        wanted = AGENT if context["value"] == before else EVAL
        return {"items": [{"id": wanted["id"], "evidence_refs": [context["id"]], "paper_quote": wanted["title"]}]}
    calls = model(monkeypatch, rank)
    u, p, _ = user()
    f = store.add_fact(UserFact(user_id=u, key=key, value=before, source="declared", status="active"))
    first = rec.recommend(u, p)
    store.update_fact(f.id, value=after)
    second = rec.recommend(u, p)
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert second["items"][0]["evidence_refs"] == [f.id]
    assert len(calls) == 4
