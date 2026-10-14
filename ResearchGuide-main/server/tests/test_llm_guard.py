# -*- coding: utf-8 -*-
"""模型设置只许管理员改；DeepSeek 默认关思考、输出封顶；写 .env 不冲掉别的配置。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import main  # noqa: E402

BODY = {"base_url": "https://evil.example/v1", "api_key": "sk-x", "model": "m"}


@pytest.fixture()
def no_write(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "apply_config", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(ValueError("stop")))
    return calls


def test_connect_refuses_remote_visitors(monkeypatch, no_write):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    r = TestClient(main.app).post("/api/llm/connect", json=BODY)  # TestClient 的来源不是本机
    assert r.status_code == 403 and not no_write


def test_connect_needs_the_admin_token_when_set(monkeypatch, no_write):
    monkeypatch.setenv("ADMIN_TOKEN", "t0ken")
    c = TestClient(main.app)
    assert c.post("/api/llm/connect", json=BODY, headers={"X-Admin-Token": "nope"}).status_code == 403
    assert c.post("/api/llm/connect", json=BODY, headers={"X-Admin-Token": "t0ken"}).status_code == 400  # 过了门，停在 apply_config
    assert len(no_write) == 1


def test_deepseek_payload_disables_thinking_and_caps_output(monkeypatch):
    sent = {}
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(llm, "_post", lambda url, key, payload, timeout, total=None: (sent.update(payload) or {"choices": [{"message": {"content": "ok"}}]}, ""))
    assert llm.chat("s", "u") == "ok"
    assert sent["model"] == "deepseek-flash" and sent["thinking"] == {"type": "disabled"} and sent["max_tokens"] == 2000
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    sent.clear()
    llm.chat("s", "u")
    assert "thinking" not in sent  # 别的兼容服务不认这个字段


def test_save_dotenv_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_ROOT", tmp_path)
    (tmp_path / ".env").write_text("# 注释\nADMIN_TOKEN=keep\nLLM_API_KEY=old\n", encoding="utf-8")
    llm._save_dotenv("https://api.deepseek.com/v1", "new", "deepseek-flash")
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ADMIN_TOKEN=keep" in text and "LLM_API_KEY=new" in text and "LLM_API_KEY=old" not in text and "# 注释" in text
    assert "LLM_MODEL=deepseek-flash" in text
