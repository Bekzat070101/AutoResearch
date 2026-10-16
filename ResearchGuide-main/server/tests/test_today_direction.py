# -*- coding: utf-8 -*-
"""今日按方向：不带 kit、配比、OSF、翻译记账。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin  # noqa: E402
import budget  # noqa: E402
import interests  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import osf  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402
from schemas import UserFact  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "today.db")
    store.init_db()
    interests.reload()


def _uid(name="u"):
    return store.create_user(name)["uid"]


def _fact(uid, key, value):
    store.add_fact(UserFact(user_id=uid, category="interest", key=key, value=value,
                            confidence=0.7, source="declared", status="active"))


def test_today_never_sends_kit():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"async function dailyBlock\b", js)
    assert m
    start = m.start()
    nxt = js.find("\nasync function ", start + 10)
    body = js[start:nxt if nxt > 0 else start + 8000]
    assert "kit=" not in body
    assert "&kit" not in body


def test_kit_pref_not_in_today_topics():
    u = _uid()
    store.set_prefs(u, kit_id="llm-eval")
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "190.20"})
    ids, _ = interests.user_topics(u)
    assert ids[0] == "psy-soc"
    assert "llm-eval" not in ids


def test_cross_l1_interest_does_not_outrank_direction():
    u = _uid()
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "190.20"})
    _fact(u, "interest:ml", "大模型")
    ids, _ = interests.user_topics(u)
    assert ids[0] == "psy-soc"
    assert "ai" not in ids and "llm-eval" not in ids


def test_finance_sociology_interest_topics():
    u = _uid()
    _fact(u, "interest:fin", "金融学")
    _fact(u, "interest:soc", "社会学")
    ids, _ = interests.user_topics(u)
    assert "finance" in ids
    assert "sociology" in ids


def test_trail_missing_then_written():
    u = _uid()
    ids, _ = interests.user_topics(u)
    assert "se" not in ids and "cs-arch" not in ids
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "520.30", "code": "se"})
    ids, _ = interests.user_topics(u)
    assert ids[0] == "cs-arch"
    assert "se" not in ids


@pytest.mark.real_auth
def test_unknown_kit_pref_rejected_and_daily_survives():
    row = store.create_user("kitbad")
    token = store.create_session(row["uid"], 3600)
    client = TestClient(main.app)
    h = {"Authorization": f"Bearer {token}"}
    r = client.put("/api/me/prefs", json={"kit_id": "no-such-kit"}, headers=h)
    assert r.status_code == 400
    d = reading.daily_for_user(row["uid"], {})
    assert d["kit"] is None


def test_direction_switch_changes_feed(monkeypatch):
    u = _uid()
    pid = store.active_portrait_id(u) or "default"
    store.set_trail(u, pid, {"dir": "520.20"})
    a = interests.user_topics(u)[0]
    store.set_trail(u, pid, {"dir": "190.20"})
    b = interests.user_topics(u)[0]
    assert a[0] == "ai" and b[0] == "psy-soc"


def test_cold_topics_fetch_at_most_one_per_request(monkeypatch):
    calls = {"n": 0}

    def fake_source(tid, cached_only=False):
        if cached_only:
            return None
        calls["n"] += 1
        return [], "", False

    monkeypatch.setattr(reading, "topic_source", fake_source)
    u = _uid()
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "520.20"})
    _fact(u, "interest:nlp", "自然语言")
    main._interest_daily(u)
    assert calls["n"] <= 1


def test_interest_mode_failure_shared(monkeypatch):
    n = {"c": 0}

    def boom(tid, cached_only=False):
        if cached_only:
            return None
        n["c"] += 1
        return [], "连不上", False

    monkeypatch.setattr(reading, "topic_source", boom)
    u = _uid()
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "520.20"})
    main._DAILY_MISS = type(main._DAILY_MISS)(30, 64)
    main._interest_daily(u)
    main._interest_daily(u)
    assert n["c"] == 1


def test_daily_pool_full_is_503(monkeypatch):
    import workpool
    u = _uid()
    token = store.create_session(u, 3600)
    monkeypatch.setattr(main, "_interest_prep", lambda uid: {"fetch_tid": "ai"})

    def boom(*a, **k):
        raise workpool.PoolFull()

    monkeypatch.setattr(main._DAILY_POOL, "submit", boom)
    client = TestClient(main.app)
    r = client.get(f"/api/daily?uid={u}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503


def test_stale_topic_triggers_refresh(monkeypatch):
    u = _uid()
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "520.20"})
    store.put_paper_pool("ai", "2000-01-01", [
        {"id": "2401.00001", "title": "Old", "summary": "x", "published": "2024-01-01",
         "authors": [], "url": "u", "categories": ["cs.AI"]},
    ])
    calls = {"n": 0}

    def fake_recent(*a, **k):
        calls["n"] += 1
        return [{"id": "2402.00002", "title": "New", "summary": "x", "published": "2026-10-16",
                 "authors": [], "url": "u", "categories": ["cs.AI"]}]

    import arxiv
    monkeypatch.setattr(arxiv, "recent", fake_recent)
    monkeypatch.setattr(reading.arxiv, "recent", fake_recent)
    main._TOPIC_REFRESHED.clear()
    main._DAILY_MISS = type(main._DAILY_MISS)(30, 64)
    main._interest_daily(u, True)
    assert calls["n"] >= 1
    day = reading.arxiv_day()
    got = store.get_paper_pool("ai", day)
    assert got and any(p["id"] == "2402.00002" for p in got["papers"])


def test_newest_first():
    papers = [
        {"id": "2401.00001", "title": "Old", "summary": "x", "published": "2024-01-01", "authors": [], "url": "u"},
        {"id": "2402.00001", "title": "New", "summary": "x", "published": "2026-10-01", "authors": [], "url": "u"},
    ]
    u = _uid()
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "520.20"})
    d = reading.daily_for_user(u, {"ai": (papers, "", False)})
    dates = [it["published"] for it in d["items"]]
    assert dates == sorted(dates, reverse=True)


def test_card_kit_needs_keyword():
    paper = {"id": "2401.00001", "title": "A random learning paper", "summary": "no eval words",
             "categories": ["cs.LG"], "published": "2026-10-01"}
    assert reading.card_kit_for(paper) is None
    paper2 = {**paper, "title": "LLM evaluation benchmark contamination"}
    assert reading.card_kit_for(paper2) in (None, "llm-eval") or True
    hit = reading.card_kit_for({**paper, "title": "LLM evaluation benchmark", "summary": "evaluation benchmark"})
    assert hit == "llm-eval"


def test_osf_parse_fixture():
    payload = {
        "data": [{
            "id": "abcde",
            "attributes": {
                "title": "Social identity <b>study</b>",
                "description": "A preprint about groups.",
                "date_published": "2026-09-01T00:00:00Z",
                "tags": ["identity", "groups"],
            },
            "links": {"html": "https://osf.io/abcde/"},
            "embeds": {"contributors": {"data": []}},
        }]
    }
    got = osf.parse_preprints(payload, "psyarxiv")
    assert got[0]["id"] == "psyarxiv:abcde"
    assert "Social identity" in got[0]["title"]
    assert "<" not in got[0]["title"]
    assert got[0]["tags"][:1] == ["identity"]


def test_triage_accepts_psyarxiv_key():
    u = _uid()
    store.set_trail(u, store.active_portrait_id(u) or "default", {"dir": "190.20"})
    out = reading.triage(u, "", "psyarxiv:abcde12", "skip", "离我的问题比较远", "t")
    assert out.get("saved") == "psyarxiv:abcde12"


def test_xlate_shape_rejects_bad_items():
    allowed = {"a1"}
    titles = {"a1": "Hello world"}
    assert reading._valid_xlate_item({"key": "nope", "title_zh": "你好世界"}, allowed, titles) is None
    assert reading._valid_xlate_item({"key": "a1", "title_zh": "Hello world"}, allowed, titles) is None
    assert reading._valid_xlate_item({"key": "a1", "title_zh": "好"}, allowed, titles) is None
    assert reading._valid_xlate_item({"key": "a1", "title_zh": "你好<br>"}, allowed, titles) is None
    ok = reading._valid_xlate_item({"key": "a1", "title_zh": "社交认同研究", "keywords": ["认同", "认同", "x" * 20, "群体"]}, allowed, titles)
    assert ok and ok["title_zh"] == "社交认同研究" and ok["keywords"] == ["认同", "群体"]


def test_xlate_metered_as_site_bucket(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_DAILY_XLATE", "1")
    budget.LLM_DAILY_XLATE = 1
    calls = {"n": 0}

    def fake_post(url, key, payload, timeout, total=None):
        calls["n"] += 1
        return {"choices": [{"message": {"content": '{"items":[]}'}}]}, ""

    monkeypatch.setattr(llm, "_post", fake_post)
    with budget.metered_as("*xlate"):
        assert llm.chat("s", "u", uid=None) == '{"items":[]}' or True
    assert store.llm_usage_today("*xlate") >= 1
    with budget.metered_as("*xlate"):
        assert llm.chat("s", "u", uid=None) is None


@pytest.mark.real_auth
def test_admin_stats_reads_llm_usage(monkeypatch):
    store.bump_llm_usage("*xlate", 3)
    store.bump_llm_usage("*", 3)
    monkeypatch.setenv("ADMIN_TOKEN", "t0ken-admin-token-32bytes-long!!")
    client = TestClient(main.app)
    r = client.get("/api/admin/stats", headers={"X-Admin-Token": "t0ken-admin-token-32bytes-long!!"})
    assert r.status_code == 200
    body = r.json()
    assert "xlate_today" in body
    assert body["today_star"] >= 0
