# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import main  # noqa: E402
import planner  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "dir.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)


def _user(name="d"):
    row = store.create_user(name)
    token = store.create_session(row["uid"], 3600)
    return row["uid"], {"Authorization": f"Bearer {token}"}


def test_kit_title_zh_for_mmlu():
    zh, kws = reading.kit_title_zh("2009.03300")
    assert zh
    assert kws or zh


def test_paper_dir_empty_then_upsert_keeps_rank():
    assert store.list_paper_dir("190.20") == []
    store.put_paper_dir("190.20", "psyarxiv:abcde", "psy-soc", 1, "psyarxiv",
                        "Social identity", "abs", ["A"], "2024-01-01", "https://osf.io/abcde", [])
    store.put_paper_dir("190.20", "psyarxiv:abcde", "psy-soc", 9, "psyarxiv",
                        "Social identity", "abs2", ["A"], "2024-01-01", "https://osf.io/abcde", [])
    rows = store.list_paper_dir("190.20")
    assert len(rows) == 1
    assert rows[0]["rank"] == 1
    assert rows[0]["summary"] == "abs2"
    assert rows[0]["paper_id"].startswith("psyarxiv:")
    store.put_paper_dir("190.20", "psyarxiv:abcde", "psy-soc", 1, "psyarxiv",
                        "Social identity", "abs3", [], "2024-01-01", "", [])
    rows = store.list_paper_dir("190.20")
    assert rows[0]["authors"] == ["A"]
    assert rows[0]["url"] == "https://osf.io/abcde"


def test_papers_dir_route_does_not_hit_arxiv(monkeypatch):
    uid, h = _user("diru")
    store.set_trail(uid, store.active_portrait_id(uid) or "default", {"dir": "190.20", "code": "psy"})
    for i in range(3):
        store.put_paper_dir("190.20", f"psyarxiv:id{i:04d}", "psy-soc", i + 1, "psyarxiv",
                            f"Paper {i}", "s", [], "2024-02-01", "u", [])

    def boom(*a, **k):
        raise AssertionError("should not call arxiv")

    monkeypatch.setattr("arxiv.search", boom)
    monkeypatch.setattr("arxiv.recent", boom)
    client = TestClient(main.app)
    r = client.get(f"/api/papers/dir?node=190.20&uid={uid}", headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 3
    mine = client.get(f"/api/papers/dir?uid={uid}", headers=h)
    assert mine.status_code == 200
    assert mine.json()["node"] == "190.20"
    assert len(mine.json()["items"]) == 3


def test_search_paper_dir_dedups_before_limit():
    for i in range(25):
        pid = f"2310.{10000 + i}"
        store.put_paper_dir("520.20", pid, "ai", i + 1, "arxiv", f"unique paper {i} tokenzz", "tokenzz", [], "2024-01-01", "u", [])
        store.put_paper_dir("520.20.nlp", pid, "nlp", i + 1, "arxiv", f"unique paper {i} tokenzz", "tokenzz", [], "2024-01-01", "u", [])
    got = store.search_paper_dir("tokenzz", 25)
    assert len(got) == 25


def test_db_html_not_overwritten_by_abstract():
    aid = "2310.17623"
    html = {"id": aid, "title": "T", "source": "html", "text": "x" * 3000, "url": "u"}
    store.put_paper_text(aid, html)
    store.put_paper_text(aid, {"id": aid, "title": "T", "source": "abstract", "text": "short", "url": "u"})
    row = store.get_paper_text(aid)
    assert row["source"] == "html"
    assert len(row["text"]) >= 3000


def test_search_prefers_library(monkeypatch):
    uid, h = _user("lib")
    store.put_paper_dir("520.20", "2009.03300", "ai", 1, "arxiv",
                        "Measuring Massive Multitask Language Understanding", "mmlu", [], "2020-01-01", "u", ["cs.CL"])
    monkeypatch.setattr("reading.arxiv.search", lambda q, max_results=20: [{"id": "2401.00001", "title": "live extra"}])
    client = TestClient(main.app)
    r = client.get(f"/api/papers/search?q=multitask+language&uid={uid}", headers=h)
    assert r.status_code == 200, r.text
    ids = [it["id"] for it in r.json()["items"]]
    assert ids[0] == "2009.03300"


def test_choose_without_trail_does_not_pretend_fine_node():
    uid, _h = _user("notfine")
    planner.choose_direction(uid, "psy")
    import interests
    assert interests.direction_node(uid) == ""


def test_osf_catalog_paper_opens(monkeypatch):
    uid, h = _user("osfopen")
    store.put_paper_dir("190.20", "psyarxiv:abcde", "psy-soc", 1, "psyarxiv",
                        "Social identity", "an abstract", ["A"], "2024-01-01", "https://osf.io/abcde", [])
    client = TestClient(main.app)
    r = client.get("/api/papers/psyarxiv:abcde", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "psyarxiv:abcde"
    assert "abstract" in (body.get("source") or "")
    assert "https://osf.io/abcde" in (body.get("url") or "")


def test_search_keeps_local_when_upstream_fails(monkeypatch):
    uid, h = _user("osfsearch")
    store.put_paper_dir("520.20", "2009.03300", "ai", 1, "arxiv",
                        "Measuring Massive Multitask Language Understanding", "mmlu", [], "2020-01-01", "u", ["cs.CL"])

    def boom(*a, **k):
        raise RuntimeError("503 upstream")

    monkeypatch.setattr("arxiv.search", boom)
    client = TestClient(main.app)
    r = client.get(f"/api/papers/search?q=multitask+language&uid={uid}", headers=h)
    assert r.status_code == 200, r.text
    ids = [it["id"] for it in r.json()["items"]]
    assert "2009.03300" in ids
    assert r.json().get("upstream_error")


def test_daily_dedupes_catalog_and_pool():
    uid, _h = _user("dup")
    store.set_trail(uid, store.active_portrait_id(uid) or "default", {"dir": "190.20", "code": "psy"})
    papers = []
    for i in range(6):
        pid = f"psyarxiv:id{i:04d}"
        store.put_paper_dir("190.20", pid, "psy-soc", i + 1, "psyarxiv",
                            f"Paper {i}", "s", [], "2026-10-01", "u", [])
        papers.append({"id": pid, "title": f"Paper {i}", "summary": "s",
                       "published": "2026-10-01", "authors": [], "url": "u", "categories": [], "source": "psyarxiv"})
    d = reading.daily_for_user(uid, {"psy-soc": (papers, "", False)})
    ids = [it["arxiv_id"] for it in d["items"]]
    assert len(ids) == len(set(ids))


def test_thin_with_catalog_is_not_empty():
    uid, _h = _user("thinok")
    store.set_trail(uid, store.active_portrait_id(uid) or "default", {"dir": "310.11", "code": "med"})
    for i in range(8):
        store.put_paper_dir("310.11", f"2401.1000{i}", "q-bio.QM", i + 1, "arxiv",
                            f"Med {i}", "s", [], "2026-10-01", "u", [])
    d = reading.daily_for_user(uid, {})
    assert d["items"]
    assert d.get("empty_reason") != "thin"


def test_osf_versioned_id_can_triage():
    uid, _h = _user("osfver")
    store.set_trail(uid, store.active_portrait_id(uid) or "default", {"dir": "190.20", "code": "psy"})
    out = reading.triage(uid, "", "psyarxiv:uemvp_v3", "keep", "值得一读的预印本", "T")
    assert out
    rows = [r for r in store.list_triage_all(uid) if "uemvp" in r["arxiv_id"]]
    assert rows and rows[0]["arxiv_id"] == "psyarxiv:uemvp"


def test_recommend_fills_from_ranking():
    import memory
    uid, h = _user("rec")
    memory.apply_ops(uid, memory.validate_ops(uid, [{
        "op": "add", "key": "interest:social-psychology", "value": "社会心理学",
        "evidence_quote": "社会心理学", "affects": "direction_choice",
    }], ["社会心理学"])[0], "")
    items = planner.direction_ranking(uid)
    assert items and items[0]["node"] == "190.20"
    client = TestClient(main.app)
    r = client.get(f"/api/directions/recommend?uid={uid}", headers=h)
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert cards
    assert cards[0]["direction"]["code"] == "psy"
    assert cards[0].get("voice") == "rules"
