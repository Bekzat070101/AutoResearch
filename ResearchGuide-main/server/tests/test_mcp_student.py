# -*- coding: utf-8 -*-
"""人工学生：AI agent 通过 MCP 工具当学生。这里不调模型（规则版引导、规则版带读），
用 TestClient 当传输，走的是真实接口（会话、预算、限频、锁都在）。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import budget  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import mcp_student  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth
TOK = "test-operator-token-123"


class ClientApi(mcp_student.Api):
    """用 TestClient 代替 urllib；SSE 退到 /api/dialogue/turn。"""

    def __init__(self, client: TestClient):
        super().__init__("http://testserver", TOK, "qa")
        self.c = client
        self.prefer_stream = False

    def call(self, method, path, body=None, token=None, raw=None, content_type=None, timeout=None):
        headers = {"X-Qiyan-Agent": self.operator}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kw = {"headers": headers}
        if raw is not None:
            kw["content"] = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None:
            kw["json"] = body
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.text
            except Exception:
                detail = r.text
            raise mcp_student.ApiError(r.status_code, str(detail)[:300])
        if "json" in (r.headers.get("content-type") or ""):
            return r.json()
        return r.content


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "agent.db")
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE", raising=False)
    monkeypatch.setenv("AGENT_TOKENS", f"qa:{TOK},other:another-token-xyz")
    monkeypatch.delenv("QIYAN_SELF_URL", raising=False)
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    monkeypatch.setattr(llm, "ready", lambda uid=None: False)
    store.init_db()
    auth._HITS.clear()
    mcp_student._SESSIONS.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def _tools(client) -> mcp_student.Tools:
    return mcp_student.Tools(ClientApi(client))


def test_operator_token_required_and_personas_listed(client):
    assert client.get("/api/agent/personas").status_code == 401
    r = client.get("/api/agent/personas", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = client.get("/api/agent/personas", headers={"Authorization": f"Bearer {TOK}"})
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["personas"]]
    assert "diligent_y1" in ids and "bored_y3" in ids
    assert [p["id"] for p in r.json()["plan"]] == ["guide", "enter", "tutor", "project"]
    assert client.post("/api/agent/register", json={}).status_code == 401
    bad = client.post("/api/agent/register", json={"persona": "nobody"}, headers={"Authorization": f"Bearer {TOK}"})
    assert bad.status_code == 400


def test_register_marks_account_synthetic_and_budget_has_operator_cap(client, monkeypatch):
    r = client.post("/api/agent/register", json={"persona": "non_cs", "agent_model": "deepseek-v4.1"},
                    headers={"Authorization": f"Bearer {TOK}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"].startswith("bot_qa_") and body["token"] and body["run_id"]
    assert body["consent_ok"] is True and body["guest"] is False
    acc = store.agent_account(body["uid"])
    assert acc["operator"] == "qa" and acc["persona"] == "non_cs" and acc["agent_model"] == "deepseek-v4.1"
    caps = budget._caps(body["uid"])
    assert "*agent:qa" in caps and caps["*agent:qa"] == budget.agent_cap()
    monkeypatch.setenv("LLM_DAILY_AGENT", "1")
    assert budget.left(body["uid"]) == 1
    store.bump_llm_usage("*agent:qa", 1)
    assert budget.left(body["uid"]) == 0
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["uid"] == body["uid"]
    text = client.get("/metrics").text
    assert re.search(r'^qiyan_users\{kind="synthetic"\} 1$', text, re.M)
    assert re.search(r"^qiyan_agent_runs_total 1$", text, re.M)


def test_first_day_journey_through_the_tools(client):
    """像 agent 一样：上线 → 回答引导 → 进入 → 今日/研读 → 打开论文 → 带读 → 交卷 → 我的 → end_day。"""
    t = _tools(client)
    first = t.start_as_student("diligent_y1", "test-model")
    assert first["ok"] and "你现在是" in first["text"] and t.s.screen == "dialogue"
    assert "你的目标" in first["text"]
    # 回答几轮规则版引导：每轮都有回复和屏幕
    for msg in ("我是信管大一，刚学完程序设计", "对 AI 感兴趣", "想做研究", "本科", "暂时没有"):
        out = t.reply(msg)
        assert out["ok"] and "【屏幕：对话】" in out["text"]
    assert t.s.turns == 5
    labels = [lab for lab, _ in t.s.buttons]
    assert any("进入启研" in lab for lab in labels), labels
    out = t.press(next(lab for lab in labels if "进入启研" in lab))
    assert t.s.entered and "enter" in t.s.done
    assert out["screen"] == "today"
    # 导航到研读，打开目录里有的话就第一篇；没有目录时屏幕说清楚
    out = t.press("研读")
    assert out["screen"] == "read"
    if t.s.papers:
        out = t.open_paper(1)
        assert out["screen"] == "paper" and t.s.paper.get("id")
    # 我的
    out = t.press("我的")
    assert out["screen"] == "me" and "昵称" in out["text"]
    # 按一个不存在的按钮：告诉它没有，不崩
    out = t.press("随便按")
    assert "没有「随便按」" in out["text"]
    # 卡住 + 结束
    t.stuck("不知道研读在哪里")
    end = t.end_day("看完了今日")
    assert end["ok"] and end["run"]["stuck"] == 1 and "enter" in end["run"]["done"]
    assert end["run"]["steps"] >= 8
    summary = store.agent_runs_summary()
    assert summary["accounts"] == 1 and summary["runs"][0]["done"] == end["run"]["done"]
    assert summary["stuck_screens"] and summary["stuck_screens"][0]["screen"] == "me"
    # 结束后再用：要求先上线（MCP 层把它变成 isError 的回复）
    with pytest.raises(mcp_student.ApiError):
        t.look()
    err = mcp_student.call_tool(t, "look", {})
    assert err["isError"] is True and "start_as_student" in err["content"][0]["text"]


def test_event_route_rejects_other_users_run(client):
    t = _tools(client)
    t.start_as_student("careless")
    other = client.post("/api/auth/register", json={"username": "real_one", "password": "secret-ok", "nickname": "真人", "consent": True}).json()
    r = client.post("/api/agent/event", json={"uid": other["uid"], "run_id": t.s.run_id, "kind": "stuck"},
                    headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 404
    r = client.post("/api/agent/run/end", json={"uid": other["uid"], "run_id": t.s.run_id},
                    headers={"Authorization": f"Bearer {other['token']}"})
    assert r.status_code == 404
    # 真人账号不是人工学生
    assert store.agent_account(other["uid"]) is None
    assert "*agent:qa" not in budget._caps(other["uid"])


def test_jsonrpc_surface_and_remote_mode_gate(client, monkeypatch):
    t = _tools(client)
    init = mcp_student.handle(t, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "researchguide-student"
    lst = mcp_student.handle(t, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [x["name"] for x in lst["result"]["tools"]]
    assert {"start_as_student", "look", "reply", "press", "stuck", "end_day"} <= set(names)
    call = mcp_student.handle(t, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "look", "arguments": {}}})
    assert call["result"]["isError"] is True and "start_as_student" in call["result"]["content"][0]["text"]
    # 远程挂载：没设 QIYAN_SELF_URL → 503 说明用 stdio；设了但令牌不对 → 401
    r = client.post("/mcp/student", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 503 and "QIYAN_SELF_URL" in r.json()["error"]["message"]
    monkeypatch.setenv("QIYAN_SELF_URL", "http://127.0.0.1:1")
    r = client.post("/mcp/student", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    r = client.post("/mcp/student", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {TOK}"})
    assert r.status_code == 200 and r.json()["result"] == {}
    info = client.get("/mcp/student").json()
    assert info["remote"] is True and "reply" in info["tools"]
    # 管理员统计
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token-xyz-admin-token-xyz-0123456789")
    r = client.get("/api/admin/agent-runs", headers={"X-Admin-Token": "admin-token-xyz-admin-token-xyz-0123456789"})
    assert r.status_code == 200 and set(r.json()) >= {"runs", "by", "stuck_screens", "accounts"}


def test_operator_tokens_parse():
    import os
    os.environ["AGENT_TOKENS"] = "qa:abcdefgh12, bad:short ,team-x:zzzzzzzzzz"
    assert mcp_student.operator_for("abcdefgh12") == "qa"
    assert mcp_student.operator_for("short") == ""
    assert mcp_student.token_for("team-x") == "zzzzzzzzzz"
    assert mcp_student.operator_for("") == ""


def test_direction_choices_goal_text_and_lenient_arguments(client):
    """三个 Haiku 学生踩到的：今日/研读没方向时要列出可选方向；goal 不能裸出一个 3；参数名写歪要认；选方向可用整段按钮文字。"""
    t = _tools(client)
    t.start_as_student("knows_direction", "test-model")
    # press 的参数名写成 button / text 也认；空 label 说清楚要什么
    out = mcp_student.call_tool(t, "press", {"button": "研读"})
    assert out.get("isError") is not True and "【屏幕：研读】" in out["content"][0]["text"]
    assert "press 要带 label" in mcp_student.call_tool(t, "press", {})["content"][0]["text"]
    text = out["content"][0]["text"]
    assert "还没有方向，先选一个" in text and "选方向：" in text, text
    assert "\n3\n" not in text
    # 今日：同样给出方向，且目标数字有解释
    out = t.press("今日")
    assert "选方向：" in out["text"] and ("今天看完" in out["text"] or "今天还没有推荐" in out["text"])
    # 用整段按钮文字、用 direction 这个参数名都能选
    out = mcp_student.call_tool(t, "choose_direction", {"direction": "选方向：ai"})
    assert out.get("isError") is not True, out
    assert t.s.direction == "ai"
    out = t.press("研读")
    assert out["screen"] == "read" and "还没有方向" not in out["text"]
    # 引导没完成时，对话屏幕说清还想了解什么
    out = t.press("对话")
    out = t.reply("我已经决定做机器学习理论了")
    assert "【屏幕：对话】" in out["text"]
    if not t.s.guide.get("ready"):
        assert "引导还想了解" in out["text"]
