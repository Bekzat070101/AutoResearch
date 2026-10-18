"""导入成功不是对话结束：同一轮接话、同一会话延续，真实 SSE 路径也要覆盖。"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dialogue
import llm
import memory
import store
from schemas import UserFact
from test_transcript_in_dialogue import REAL


@pytest.fixture(autouse=True)
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "continue.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda: False)


def user():
    return store.create_user("合成导入后接话")["uid"]


def proposal(reply="你最近想了解什么主题？", ops=None):
    return {"understanding": {"gist": "粘贴成绩单"}, "memory_ops": ops or [], "tool_intent": None,
            "dialogue": {"move": "clarify", "reply": reply, "reason": "接回交流", "offered_actions": []}, "next_action": None}


def test_same_turn_receipt_then_model_continuation(monkeypatch):
    uid = user()
    captured = []
    monkeypatch.setattr(llm, "enabled", lambda: True)
    def decide(system, prompt, **kw):
        captured.append(prompt)
        assert len(store.list_enrollments(uid)) == 7, "模型开口前必须已正确提交课程"
        return proposal()
    monkeypatch.setattr(llm, "chat_json", decide)
    r = dialogue.turn(uid, REAL)
    assert r["reply"].startswith("已识别并保存 7 门")
    assert r["reply"].endswith("你最近想了解什么主题？")
    assert len(captured) == 1
    assert "代码已完成处理" in captured[0]
    assert "86.5\\n3\\n学分" not in captured[0], "模型不必重读原始成绩单"
    assert len(store.list_messages(uid)) == 2, "不能保存两条重复的助手回执"
    assert store.list_messages(uid)[-1]["text"] == r["reply"]
    assert not r["degraded"]


def test_stream_has_receipt_and_followup_and_same_conversation(monkeypatch):
    uid = user()
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **kw: proposal())
    def stream(*a, **kw):
        yield "你最近"
        yield "想了解什么主题？"
    monkeypatch.setattr(llm, "chat_stream", stream)
    events = list(dialogue.turn_steps(uid, REAL, stream_reply=True))
    result = events[-1][1]
    assert "".join(data["text"] for kind, data in events if kind == "delta") == result["reply"]
    assert [p["name"] for k, p in events if k == "stage"] == ["memory", "observe", "decide", "memory", "compose"]
    next_result = dialogue.turn(uid, "我想了解推荐系统", result["conversation_id"])
    assert next_result["conversation_id"] == result["conversation_id"]
    assert store.list_actions(uid) == [] and store.list_tasks(uid) == []


def test_intro_with_transcript_can_record_identity_without_changing_scores(monkeypatch):
    uid = user()
    monkeypatch.setattr(llm, "enabled", lambda: True)
    ops = [{"op":"add", "key":"grade", "value":"大二", "evidence_quote":"我是大二", "affects":"question_next"}]
    monkeypatch.setattr(llm, "chat_json", lambda *a, **kw: proposal(ops=ops))
    r = dialogue.turn(uid, "我是大二，下面是成绩单。\n" + REAL)
    assert not r["rejected_ops"]
    assert any(f.key == "grade" and f.value == "大二" for f in memory.active_facts(uid))
    assert len(store.list_enrollments(uid)) == 7


def test_model_failure_preserves_import_and_offers_one_contextual_question(monkeypatch):
    uid = user()
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **kw: None)
    r = dialogue.turn(uid, REAL)
    assert r["degraded"]
    assert r["transcript_import"]["written"] == 7
    assert "你现在读什么专业" in r["reply"]
    assert store.list_messages(uid)[-1]["text"] == r["reply"]


def test_ready_profile_not_asked_for_more_background():
    uid = user()
    for key, val in [("grade", "大二"), ("interest:eval", "大模型评测"), ("reading_experience", "没有读过论文")]:
        store.add_fact(UserFact(user_id=uid, key=key, value=val, status="confirmed", source="declared"))
    r = dialogue.turn(uid, REAL)
    assert r["onboard_done"]
    assert "现有信息已经够开始" in r["reply"]
    assert "你现在读什么专业" not in r["reply"]


def test_preview_does_not_turn_into_background_interview():
    uid = user()
    r = dialogue.turn(uid, "只检查，不要保存。\n" + REAL)
    assert "只预览" in r["reply"]
    assert "你现在读什么专业" not in r["reply"]
    assert store.list_enrollments(uid) == []
