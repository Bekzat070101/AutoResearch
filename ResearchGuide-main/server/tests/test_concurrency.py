# -*- coding: utf-8 -*-
"""多人同时用时会出事的几处：流式模型调用没有总时限、对话默认开思考、线程池只有 40 个、库在临时盘上没人知道。

不联网、不调模型。慢服务器起在本机。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import limits  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402


class _Drip(BaseHTTPRequestHandler):
    """像 DeepSeek 排队时那样：先发几条 SSE 行，然后每 20 毫秒发一个空行，一直不结束（共两秒）。"""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(3):
            self.wfile.write(f'data: {json.dumps({"choices": [{"delta": {"content": f"块{i}"}}]})}\n\n'.encode())
            self.wfile.flush()
        for _ in range(100):
            self.wfile.write(b"\n")
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"data: [DONE]\n")

    def log_message(self, *a):
        pass


@pytest.fixture
def drip():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
    srv.shutdown()


def _req(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})


def test_stream_delivers_lines_and_survives_keepalives(drip):
    lines = list(limits.stream(_req(drip), timeout=10, idle=0.3, max_bytes=1 << 20))  # 空行不断，不算卡住
    data = [ln for ln in lines if ln.startswith(b"data:")]
    assert len(data) == 4 and data[-1] == b"data: [DONE]"


def test_stream_has_a_total_deadline_despite_keepalives(drip):
    t0 = time.perf_counter()
    got = []
    with pytest.raises(limits.ReadLimitError):
        for ln in limits.stream(_req(drip), timeout=0.4, idle=0.3, max_bytes=1 << 20):
            got.append(ln)
    assert time.perf_counter() - t0 < 1.5
    assert any(ln.startswith(b"data:") for ln in got)  # 到期前收到的照样交给了调用方


def test_stream_caps_bytes(drip):
    with pytest.raises(limits.ReadLimitError):
        list(limits.stream(_req(drip), timeout=10, idle=0.3, max_bytes=50))


def test_chat_stream_uses_the_same_guards_as_chat(monkeypatch, drip):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", drip.rsplit("/chat", 1)[0])
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    sent: dict = {}
    real = limits.stream

    def spy(req, **kw):
        sent["payload"] = json.loads(req.data)
        sent["kw"] = kw
        return real(req, **kw)

    monkeypatch.setattr(llm, "stream", spy)
    pieces = list(llm.chat_stream("s", "u", timeout=0.3))
    assert pieces == ["块0", "块1", "块2"]
    assert sent["payload"]["max_tokens"] == 2000 and sent["payload"]["stream"] is True
    assert sent["kw"]["timeout"] == llm.LLM_TOTAL_SECONDS and sent["kw"]["idle"] == 0.3
    assert sent["kw"]["max_bytes"] == llm.MAX_RESPONSE_BYTES

    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setattr(llm, "stream", lambda req, **kw: sent.update(payload=json.loads(req.data)) or iter(()))
    assert list(llm.chat_stream("s", "u")) == []
    assert sent["payload"]["thinking"] == {"type": "disabled"}  # DeepSeek 默认开思考：流式对话原来没关


def test_chat_stream_stops_at_the_total_deadline(monkeypatch, drip, capsys):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", drip.rsplit("/chat", 1)[0])
    monkeypatch.setattr(llm, "LLM_TOTAL_SECONDS", 0.4)
    t0 = time.perf_counter()
    pieces = list(llm.chat_stream("s", "u", timeout=0.3))
    assert pieces == ["块0", "块1", "块2"] and time.perf_counter() - t0 < 1.5
    log = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"llm"' in ln][-1])
    assert log["error"].startswith("limit") and log["chunks"] == 3


def test_thread_pool_is_raised_at_startup(monkeypatch):
    import anyio.to_thread
    monkeypatch.setattr(main, "THREADS", 96)
    with TestClient(main.app) as c:
        assert c.get("/api/health").json()["threads"] == 96
        assert c.portal.call(lambda: anyio.to_thread.current_default_thread_limiter().total_tokens) == 96


def test_health_says_whether_the_db_will_survive_a_restart(monkeypatch):
    monkeypatch.delenv("FC_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("QIYAN_DB", raising=False)
    monkeypatch.setattr(store, "DB_PATH", Path("/data/qiyan.db"))
    assert TestClient(main.app).get("/api/health").json()["db"]["ephemeral"] is False
    monkeypatch.setattr(store, "DB_PATH", Path("/tmp/qiyan.db"))
    assert TestClient(main.app).get("/api/health").json()["db"]["ephemeral"] is True
    monkeypatch.setattr(store, "DB_PATH", Path("/code/server/data/demo.db"))
    monkeypatch.setenv("FC_FUNCTION_NAME", "qiyan")  # 函数计算上没设 QIYAN_DB：默认路径在代码包里
    assert TestClient(main.app).get("/api/health").json()["db"]["ephemeral"] is True
    monkeypatch.setenv("QIYAN_DB", "/mnt/nas/qiyan.db")
    monkeypatch.setattr(store, "DB_PATH", Path("/mnt/nas/qiyan.db"))
    monkeypatch.setattr(store.os.path, "ismount", lambda p: str(p) in ("/", "/mnt/nas"))
    assert TestClient(main.app).get("/api/health").json()["db"]["ephemeral"] is False
    # 设了 QIYAN_DB，但 /data 没挂盘（镜像自带的目录，实例自己的盘）：照样会没
    monkeypatch.setenv("QIYAN_DB", "/data/qiyan.db")
    monkeypatch.setattr(store, "DB_PATH", Path("/data/qiyan.db"))
    assert TestClient(main.app).get("/api/health").json()["db"]["ephemeral"] is True
