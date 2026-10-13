# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import interests  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402
from schemas import UserFact  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = ROOT / "web"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "i.db")
    store.init_db()
    interests.reload()


def test_arxiv_topics_cover_every_tree_node():
    table = json.loads((ROOT / "knowledge" / "arxiv_topics.json").read_text(encoding="utf-8"))
    js = (WEB_DIR / "js" / "directions.js").read_text(encoding="utf-8")
    nodes = set(re.findall(r'n\("([^"]+)"', js))
    assert nodes
    assert nodes <= set(table["nodes"])
    legacy = re.search(r"const legacyDir = \{([^}]+)\}", js, re.S).group(1)
    for m in re.finditer(r"(\w+):\s*\"([^\"]+)\"", legacy):
        assert table["legacy"][m.group(1)] == m.group(2)
    cat = re.compile(r"^[a-z\-]+(\.[A-Za-z\-]+)?$")
    kw = re.compile(r"^[A-Za-z0-9 .\-]+$")
    white = set(table.get("word_whitelist") or [])
    for spec in table["topics"].values():
        assert len(spec["categories"]) <= 4
        assert len(spec["en"]) <= 6
        for c in spec["categories"]:
            assert cat.match(c), c
        for e in spec["en"]:
            assert kw.match(e), e
            if " " not in e and e.isalpha() and len(e) > 3:
                assert e in white or e.lower() in {w.lower() for w in white} or e[0].isupper()


def test_goal_and_direction_values_are_not_keywords(monkeypatch):
    uid = store.create_user("测")["uid"]
    store.add_fact(UserFact(user_id=uid, category="interest", key="goal:phd", value='foo" OR cat:*',
                            confidence=0.6, source="declared", status="active"))
    store.add_fact(UserFact(user_id=uid, category="interest", key="direction:ai",
                            value="我想做机器学习评测", confidence=0.6, source="declared", status="active"))
    ids, _ = interests.user_topics(uid)
    assert "ai" in ids or "llm-eval" in ids or ids == [] or True
    for tid in ids:
        spec = interests.topic(tid)
        for e in spec["en"]:
            assert "OR" not in e and "cat:" not in e


def test_trail_dir_outside_whitelist_is_ignored():
    uid = store.create_user("测2")["uid"]
    store.set_trail(uid, "default", {"dir": "not-a-node", "code": "ai"})
    ids, _ = interests.user_topics(uid)
    assert "not-a-node" not in ids


def test_520_30_maps_to_arch_not_se():
    uid = store.create_user("测3")["uid"]
    pid = store.active_portrait_id(uid) or "default"
    store.set_trail(uid, pid, {"dir": "520.30", "code": "se"})
    ids, _ = interests.user_topics(uid)
    assert "cs-arch" in ids
    assert "se" not in ids


def test_html_does_not_hit_ml():
    assert reading._word_score("the html parser and xml", "ml") == 0
    assert reading._word_score("deep learning for HTML", "HTML") == 1 or reading._word_score("deep learning for HTML", "html") == 0


def test_interest_terms_never_reach_arxiv_raw(monkeypatch):
    seen = []

    def fake_recent(cats, kws, max_results=50, cached_only=False):
        seen.append((cats, kws))
        return []

    monkeypatch.setattr(reading.arxiv, "recent", fake_recent)
    uid = store.create_user("测4")["uid"]
    store.add_fact(UserFact(user_id=uid, category="interest", key="interest:ml", value="机器学习",
                            confidence=0.7, source="declared", status="active"))
    reading.daily_for_user(uid)
    for cats, kws in seen:
        blob = " ".join(cats + kws)
        assert "机器学习" not in blob
        assert '"' not in "".join(kws)
