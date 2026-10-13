# -*- coding: utf-8 -*-
"""每人一把 DeepSeek：探测后入库，调用按 uid 带 key，不写 os.environ。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "llm.db")
    store.init_db()
    auth._HITS.clear()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("QIYAN_SECRET", "test-wrap")


@pytest.fixture
def client():
    return TestClient(main.app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _reg(client, name="demo_user"):
    r = client.post("/api/auth/register", json={
        "username": name, "password": "secret-ok", "nickname": name, "consent": True,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_guest_cannot_save_key(client, monkeypatch):
    monkeypatch.setattr(llm, "probe", lambda **k: {"ok": True, "model": "deepseek-flash"})
    g = client.post("/api/auth/login", json={"nickname": "过客", "consent": True}).json()
    r = client.post("/api/llm/mine", json={"api_key": "sk-guest-key-xxxx"}, headers=_h(g["token"]))
    assert r.status_code == 403


def test_save_key_needs_probe_and_never_echoes(client, monkeypatch, tmp_path):
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1"}

    monkeypatch.setattr(llm, "probe", probe)
    acc = _reg(client)
    env_path = tmp_path / "no.env"
    monkeypatch.setattr(llm, "_ROOT", tmp_path)
    r = client.post("/api/llm/mine", json={
        "api_key": "sk-demo-secret-key",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
    }, headers=_h(acc["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_key"] is True
    assert body["tail"] == "sk-demo-secret-key"[-4:]
    assert "sk-demo-secret-key" not in str(body)
    me = client.get("/api/auth/me", headers=_h(acc["token"])).json()
    assert me["has_llm_key"] is True
    assert me["llm_key_tail"] == "sk-demo-secret-key"[-4:]
    assert "sk-demo" not in str(me)
    dumped = client.get("/api/me/export", headers=_h(acc["token"])).json()
    blob = str(dumped)
    assert "sk-demo-secret-key" not in blob
    assert "wrapped" not in blob
    assert not env_path.exists() or "sk-demo-secret-key" not in env_path.read_text(encoding="utf-8")
    assert calls and calls[0].get("override", {}).get("api_key") == "sk-demo-secret-key"


def test_probe_fail_does_not_store(client, monkeypatch):
    monkeypatch.setattr(llm, "probe", lambda **k: {"ok": False, "error": "bad"})
    acc = _reg(client, "no_save")
    r = client.post("/api/llm/mine", json={"api_key": "sk-bad-key-zzzz"}, headers=_h(acc["token"]))
    assert r.status_code == 400
    assert client.get("/api/llm/mine", headers=_h(acc["token"])).json()["has_key"] is False


def test_two_users_chat_use_own_keys_without_touching_environ(client, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-server-shared")
    a = store.register_user("user_a", "secret-ok", "甲")
    b = store.register_user("user_b", "secret-ok", "乙")
    store.save_llm_secret(a["uid"], "sk-aaa-aaaa", "https://api.deepseek.com/v1", "deepseek-flash")
    store.save_llm_secret(b["uid"], "sk-bbb-bbbb", "https://api.deepseek.com/v1", "deepseek-flash")
    seen: list[str] = []

    def fake_post(url, key, payload, timeout):
        seen.append(key)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}, ""

    monkeypatch.setattr(llm, "_post", fake_post)
    assert llm.chat("s", "u", uid=a["uid"]) == "ok"
    assert llm.chat("s", "u", uid=b["uid"]) == "ok"
    assert seen == ["sk-aaa-aaaa", "sk-bbb-bbbb"]
    assert os.environ.get("LLM_API_KEY") == "sk-server-shared"


def test_user_without_key_falls_back_to_rules(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    uid = store.create_user("无钥")["uid"]
    assert llm.ready(uid) is False
    assert llm.chat("s", "u", uid=uid) is None


def test_same_length_keys_cannot_unwrap_each_other():
    a = store.wrap_secret("sk-aaa-aaaa")
    b = store.wrap_secret("sk-bbb-bbbb")
    assert a.startswith("v2:") and b.startswith("v2:") and a != b
    assert store.unwrap_secret(a) == "sk-aaa-aaaa"
    assert store.unwrap_secret(b) == "sk-bbb-bbbb"


def test_model_output_includes_own(capsys, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-team-key")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: ({"choices": [{"message": {"content": "ok"}}], "usage": {}}, ""))
    assert llm.chat("s", "u") == "ok"
    logged = capsys.readouterr().out
    assert '"own": false' in logged
    uid = store.create_user("自钥")["uid"]
    store.save_llm_secret(uid, "sk-own-key", "https://api.deepseek.com/v1", "deepseek-flash")
    assert llm.chat("s", "u", uid=uid) == "ok"
    logged = capsys.readouterr().out
    assert '"own": true' in logged
