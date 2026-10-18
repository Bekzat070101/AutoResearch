# -*- coding: utf-8 -*-
"""定位层第四轮复查：删掉的边不能留在陈述里；确认过的行为证据仍算已证明。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arxiv  # noqa: E402
import positioning  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402
from test_reading import DIMS, GOOD, PAPER  # noqa: E402

AID = "2310.17623"


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "p.db")
    store.init_db()
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: {**PAPER, "id": arxiv.clean_id(aid)})


def user(with_card: bool = True) -> str:
    u = store.create_user("t")["uid"]
    if with_card:
        assert reading.submit_card(u, "llm-eval", AID, GOOD, DIMS)["status"] == "pass"
    return u


def test_deleted_edge_leaves_statement_sentence_and_portfolio():
    u = user()
    e = positioning.add_edge(u, "access", "有一万道未公开的中文数学题")["edges"][-1]
    proof = next(x["id"] for x in positioning.edges(u)["edges"] if x["status"] == "proven")
    s = positioning.save_statement(u, "llm-eval", "multi-test", "中文数学题的污染检验多重比较校正", [proof, e["id"]])
    assert e["text"] in s["sentence"]
    positioning.delete_edge(u, e["id"])
    cur = positioning.statement(u, "llm-eval")["statement"]
    assert [y["id"] for y in cur["y"]] == [proof]
    assert e["text"] not in cur["sentence"] and cur["can_save"] and cur["portfolio_ready"]
    v = user(with_card=False)
    only = positioning.add_edge(v, "access", "有一万道未公开的中文数学题")["edges"][-1]["id"]
    positioning.save_statement(v, "llm-eval", "multi-test", "中文数学题的污染检验多重比较校正", [only])
    positioning.delete_edge(v, only)
    cur = positioning.statement(v, "llm-eval")["statement"]
    assert cur["y"] == [] and cur["sentence"] == "" and not cur["can_save"] and not cur["portfolio_ready"]


def test_confirmed_behavior_evidence_stays_proven(monkeypatch):
    monkeypatch.setattr(positioning, "POOL_MIN", 2)
    monkeypatch.setattr(positioning, "K_MIN", 1)
    me, peer = user(), user()
    for u in (me, peer):
        f = next(f for f in store.list_facts(u) if f.key.startswith("card:"))
        store.update_fact(f.id, status="confirmed")
        store.add_edge(u, "language", "粤语母语")
    assert positioning.edges(me)["proven"] == 1
    r = positioning.combo_rarity(me, "llm-eval")
    assert r["status"] == "ok" and r["pairs"][0]["band"] == "常见"  # 同学确认过的证据也算他有这条边
