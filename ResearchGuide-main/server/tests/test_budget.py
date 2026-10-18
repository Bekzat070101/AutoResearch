# -*- coding: utf-8 -*-
"""模型预算：访客、没同意隐私说明的人、匿名接口不能无限调团队付费的模型；全站每天有总上限。

走真的 HTTP 和真的 auth.guard（real_auth），模型的网络请求换成计数的假回复。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import budget  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch, real_llm):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "budget.db")
    store.init_db()
    auth._HITS.clear()
    for k in ("LLM_DAILY_TOTAL", "LLM_DAILY_USER", "LLM_DAILY_GUEST", "AUTH_DEV_CODES", "WECHAT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")


@pytest.fixture
def calls(monkeypatch):
    """假装模型：每次真正发出去的请求记一笔，回一个能用的 JSON。"""
    sent: list[dict] = []

    def fake_post(url, key, payload, timeout):
        sent.append(payload)
        body = {"score": 3, "passed": [], "missing": [], "next_step": "继续", "why": "因为", "reply": "好"}
        return {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}],
                "usage": {"total_tokens": 1}}, ""

    monkeypatch.setattr(llm, "_post", fake_post)
    return sent


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _guest(client, consent=True):
    r = client.post("/api/auth/login", json={"nickname": "访客", "consent": consent}).json()
    return r["uid"], r["token"]


def _task(client, uid, token):
    r = client.post("/api/tasks/generate", json={"uid": uid, "direction": "ai", "level": 1}, headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _submit(client, uid, token, tid):
    return client.post(f"/api/tasks/{tid}/submit", json={"uid": uid, "payload": "我读完了这一节，写了三行总结。"},
                       headers=_h(token))


@pytest.fixture
def client():
    return TestClient(main.app)


def test_guest_calls_stop_at_the_guest_cap_and_features_fall_back(client, calls, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_GUEST", "3")
    uid, token = _guest(client)
    tid = _task(client, uid, token)
    for _ in range(8):
        assert _submit(client, uid, token, tid).status_code == 200  # 超了额度照样有反馈（规则版）
    assert len(calls) == 3  # 正好用满访客额度：生成任务和交任务的调用加起来
    assert client.get("/api/auth/me", headers=_h(token)).json()["llm_left"] == 0


def test_no_consent_means_no_model_calls(client, calls):
    uid, token = _guest(client, consent=False)
    tid = _task(client, uid, token)
    for _ in range(3):
        assert _submit(client, uid, token, tid).status_code == 200
    assert calls == []
    assert client.get("/api/auth/me", headers=_h(token)).json()["llm_left"] == 0


def test_site_wide_daily_cap_covers_everyone(client, calls, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_TOTAL", "4")
    monkeypatch.setenv("LLM_DAILY_GUEST", "100")
    for _ in range(3):
        uid, token = _guest(client)
        tid = _task(client, uid, token)
        for _ in range(3):
            _submit(client, uid, token, tid)
    assert len(calls) == 4


def test_charging_is_atomic_under_concurrency(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    uid = store.create_user("并发")["uid"]
    store.record_consent(uid, auth.PRIVACY_VERSION)
    monkeypatch.setenv("LLM_DAILY_GUEST", "5")

    def one(_):
        budget.bind(uid)
        return budget.charge()

    with ThreadPoolExecutor(max_workers=16) as pool:
        got = list(pool.map(one, range(40)))
    assert sum(got) == 5


def test_anonymous_requests_cannot_use_the_model(calls):
    budget.bind(budget.ANON)
    try:
        assert llm.enabled() is False
        assert llm.chat("s", "u") is None
        assert calls == []
    finally:
        budget._WHO.set(None)


def test_admin_probe_is_not_charged_to_anyone(calls):
    budget.bind(budget.ANON)
    try:
        assert llm.probe()["ok"] is True
        assert len(calls) == 1
    finally:
        budget._WHO.set(None)


def test_project_review_runs_with_the_requesters_budget(client, monkeypatch):
    uid, token = _guest(client)
    store.save_project(uid, "p1", {"name": "项目", "reviews": []})
    seen = {}

    def fake_review(u, p, data, portrait=""):
        seen["who"] = budget._WHO.get()
        return {"ok": True}

    monkeypatch.setattr(main, "_review_and_record", fake_review)
    r = client.post(f"/api/projects/p1/submit?uid={uid}", content=b"PK\x03\x04" + b"x" * 100,
                    headers={**_h(token), "Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert seen["who"] == uid  # 评阅线程里也知道是谁在调模型
