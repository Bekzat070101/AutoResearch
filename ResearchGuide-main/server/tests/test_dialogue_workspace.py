"""Behavioral coverage for multi-chat, portrait ownership and reading recall."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import dialogue
import main
import memory
import reading_memory
import store
import tools
import userlock
from schemas import UserFact


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    store.init_db()
    monkeypatch.setattr(dialogue.llm, "enabled", lambda *a: False)


def user():
    uid = store.create_user("reader")["uid"]
    pid = store.list_portraits(uid)[0]["id"]
    return uid, pid


def card(uid, status="pass", version=1, paper="2401.12345"):
    return {"id": f"card-{paper}-{version}", "uid": uid, "kit_id": "llm-eval", "arxiv_id": paper,
            "version": version, "title": "Attention evaluation", "status": status,
            "fields": {"claim": "Attention evaluation uses a benchmark", "limitation": "small sample"},
            "dims": {"method": "benchmark"}}


def commit(uid, **kw):
    c = card(uid, **kw)
    reading_memory.commit_card(c, {"title": c["title"], "summary": "Benchmark evaluation",
                                   "url": "https://arxiv.org/abs/2401.12345"})
    return c


def test_create_select_and_default_history_never_merge():
    uid, pid = user()
    client = TestClient(main.app)
    a = client.post("/api/dialogue/conversations", json={"uid": uid, "portrait_id": pid}).json()["conversation"]
    b = client.post("/api/dialogue/conversations", json={"uid": uid, "portrait_id": pid}).json()["conversation"]
    assert a["id"] != b["id"]
    for c, text in [(a, "A only"), (b, "B only")]:
        r = client.post("/api/dialogue/turn", json={"uid": uid, "portrait_id": pid, "conversation_id": c["id"], "message": text})
        assert r.status_code == 200
    for cid, expected in [(a["id"], "A only"), (b["id"], "B only"), (None, "B only")]:
        h = dialogue.history(uid, cid)
        assert [m["text"] for m in h["messages"] if m["role"] == "user"] == [expected]
        assert h["conversation_id"]
    assert store.get_conversation(uid, a["id"])["topic_hint"] == "A only"


@pytest.mark.parametrize("endpoint", ["/api/dialogue/turn", "/api/dialogue/stream"])
def test_stale_portrait_and_foreign_conversation_rejected_before_write(endpoint):
    uid, pid = user()
    cid = store.create_conversation(uid)["id"]
    store.open_new_portrait(uid)
    other = store.active_portrait_id(uid)
    client = TestClient(main.app)
    base = {"uid": uid, "message": "should not be saved", "conversation_id": cid}
    assert client.post(endpoint, json={**base, "portrait_id": pid}).status_code == 409
    assert client.post(endpoint, json={**base, "portrait_id": other}).status_code == 404
    assert not store.list_messages(uid)


def test_context_and_evidence_are_per_conversation_facts_shared():
    uid, _ = user()
    a, b = (store.create_conversation(uid)["id"] for _ in range(2))
    store.add_message(uid, "user", "A private text", conversation_id=a)
    store.add_message(uid, "user", "B private text", conversation_id=b)
    store.add_fact(UserFact(user_id=uid, category="background", key="major", value="计算机", source="declared", status="active"))
    env = dialogue.build_env(uid, b, "background")
    assert [m["text"] for m in env["recent_context"]] == ["B private text"]
    assert any(f["value"] == "计算机" for f in env["recalled_memory"])
    assert dialogue._evidence_pool(uid, b) == ["B private text"]


def test_only_passed_cards_indexed_updates_are_idempotent_and_revision_keeps_pass():
    uid, _ = user()
    commit(uid, status="revise")
    assert reading_memory.search(uid)["total"] == 0
    before = store.get_memory_version(uid)
    commit(uid, version=2)
    commit(uid, version=3)
    commit(uid, status="revise", version=4)
    assert reading_memory.search(uid)["total"] == 1
    assert reading_memory.detail(uid, "2401.12345")["document"]["card_version"] == 3
    assert store.get_memory_version(uid) == before + 2
    facts = [f for f in store.list_facts(uid) if f.key.startswith("card:")]
    assert len(facts) == 1 and memory.layer_of(facts[0].key) == "experience"
    assert memory.spec_for(facts[0].key).sources == ("behavior",)


def test_reading_commit_rolls_back_all_side_effects():
    uid, _ = user()
    with store._conn() as c:
        c.execute("CREATE TRIGGER reject_read BEFORE INSERT ON read_documents BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(Exception, match="test failure"):
        commit(uid)
    assert not store.latest_cards(uid, "llm-eval")
    assert not store.list_facts(uid)
    assert store.get_memory_version(uid) == 0


def test_reading_tools_and_environment_only_access_current_portrait():
    uid, pid = user()
    commit(uid)
    store.put_user_paper(uid, "local:aaaaaaaaaaaa", "sha", "Private", "p.pdf", "private text", 1)
    store.save_triage(uid, "llm-eval", "2401.12345", "keep", "interesting")
    store.save_tutor(uid, "llm-eval", "2401.12345", "active", {"rev": 1}, None)
    assert tools.run(uid, {"tool": "reading.search", "args": {"query": "benchmark"}})["items"]
    cid = store.create_conversation(uid)["id"]
    assert dialogue.build_env(uid, cid, "attention")["reading"]["total"] == 1
    store.open_new_portrait(uid)
    assert not reading_memory.search(uid)["items"]
    assert not reading_memory.detail(uid, "2401.12345")["ok"]
    assert not store.latest_cards(uid, "llm-eval")
    assert not store.get_user_paper(uid, "local:aaaaaaaaaaaa")
    assert not store.get_tutor(uid, "llm-eval", "2401.12345")
    assert not store.list_triage(uid, "llm-eval")
    store.activate_portrait(uid, pid)
    assert reading_memory.search(uid)["total"] == 1
    assert store.get_user_paper(uid, "local:aaaaaaaaaaaa")
    assert store.get_tutor(uid, "llm-eval", "2401.12345")
    other, _ = user()
    assert not reading_memory.detail(other, "2401.12345")["ok"]


def test_sources_survive_history_and_portrait_roundtrip():
    uid, pid = user()
    c = store.create_conversation(uid)["id"]
    src = [{"paper_id": "2401.12345", "title": "Paper", "card_version": 1}]
    store.add_message(uid, "assistant", "Answer", conversation_id=c, reading_sources=src)
    store.open_new_portrait(uid)
    store.activate_portrait(uid, pid)
    assert dialogue.history(uid, c)["messages"][0]["reading_sources"] == src


def test_migration_assigns_by_evidence_archives_unknown_and_is_repeatable():
    uid, pid = user()
    a = commit(uid)
    unknown = card(uid, version=2, paper="2402.22222")
    store.save_card(unknown)
    store.put_user_paper(uid, "local:bbbbbbbbbbbb", "sha", "Unassigned", "u.pdf", "secret", 1)
    with store._conn() as c:
        c.execute("DELETE FROM feature_migrations WHERE name='portrait_reading_v1'")
    store.init_db()
    assert store.latest_card(uid, "llm-eval", a["arxiv_id"])
    assert not store.latest_card(uid, "llm-eval", unknown["arxiv_id"])
    assert not store.get_user_paper(uid, "local:bbbbbbbbbbbb")
    first = store.export_user(uid)
    assert len(first["reading_unassigned"]) == 2
    assert reading_memory.migration_report()["assigned_cards"] == 1
    store.init_db()
    assert len(store.export_user(uid)["reading_unassigned"]) == 2
    assert reading_memory.search(uid)["total"] == 1
    store.delete_user(uid)
    assert not store.export_user(uid)["reading_unassigned"]


def test_legacy_messages_get_a_dedicated_conversation():
    uid, _ = user()
    store.add_message(uid, "user", "legacy message")
    with store._conn() as c:
        c.execute("DELETE FROM feature_migrations WHERE name='conversation_partition_v1'")
    store.init_db()
    h = dialogue.history(uid)
    assert h["conversation_id"] and h["messages"][0]["text"] == "legacy message"
    store.init_db()
    assert len(store.list_conversations(uid)) == 1


def test_request_waiting_for_lock_rechecks_portrait_before_writing():
    uid, pid = user()
    cid = store.create_conversation(uid)["id"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        with userlock.hold(uid):
            request = pool.submit(TestClient(main.app).post, "/api/dialogue/turn",
                                  json={"uid": uid, "portrait_id": pid, "conversation_id": cid, "message": "old profile"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with userlock._GUARD:
                    waiting = userlock._LOCKS[uid][1] >= 2
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting
            store.open_new_portrait(uid)
        assert request.result(timeout=10).status_code == 409
    assert store.list_messages(uid) == []
    store.activate_portrait(uid, pid)
    assert store.list_messages(uid) == []


def test_deleted_private_paper_disappears_from_reading_memory():
    uid, _ = user()
    paper = "local:aaaaaaaaaaaa"
    store.put_user_paper(uid, paper, "sha", "Private", "p.pdf", "text", 1)
    commit(uid, paper=paper)
    assert reading_memory.detail(uid, paper)["ok"]
    store.delete_user_paper(uid, paper)
    assert not reading_memory.search(uid)["items"]
    assert not store.latest_cards(uid, "llm-eval")
    assert not store.list_facts(uid)
