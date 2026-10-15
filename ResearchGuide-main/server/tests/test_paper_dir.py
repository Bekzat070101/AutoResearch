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
