# -*- coding: utf-8 -*-
"""Codex 复现过的模型输出问题：证据只认学生原话、短值也要撑得住、字段类型错只拒那一条、提议先规整再写库、坏包算失败、流断了不当成功。不调真的模型。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import budget  # noqa: E402
import dialogue  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "x.db")
    store.init_db()


@pytest.fixture
def model_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "dummy-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.invalid/v1")
    with budget.unmetered():  # 别的测试文件可能在主线程里绑过某个人的额度，这里的调用不算在任何人头上
        yield


def _uid():
    return store.create_user("t")["uid"]


def _proposal(reply="规则保底正文", ops=None, gist="正常"):
    return {"understanding": {"gist": gist, "signals": [], "corrections": []}, "memory_ops": ops or [],
            "tool_intent": None, "next_action": None,
            "dialogue": {"move": "answer", "reason": "正常", "reply": reply, "offered_actions": []}}


def _model(monkeypatch, proposal):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda system, user, **kw: proposal if kw.get("tag") == "dialogue.proposal" else None)


GRADE = {"op": "add", "key": "grade", "value": "大二", "evidence_quote": "我大二", "affects": "task_difficulty"}
OK_LINE = b'data: {"choices":[{"delta":{"content":"partial"}}]}'


def test_the_assistants_own_words_are_not_evidence(monkeypatch):
    """学生只说了「你好」「下一步呢」，「擅长Python」是助手上一轮自己说的：原来证据池里混着助手的话，这条照样按学生自述写进画像。"""
    uid = _uid()
    said = "你擅长Python，并且做过自然语言处理项目"
    _model(monkeypatch, _proposal(reply=said + "。"))
    dialogue.turn(uid, "你好")
    _model(monkeypatch, _proposal(ops=[{"op": "add", "key": "capability:python", "value": "擅长Python，并且做过自然语言处理项目",
                                        "evidence_quote": said, "affects": "task_difficulty"}]))
    r = dialogue.turn(uid, "下一步呢")
    assert r["facts_added"] == [] and [x["reason"] for x in r["rejected_ops"]] == ["evidence_not_found"]
    assert store.list_facts(uid) == []


def test_a_short_value_must_match_its_quote():
    """学生说「我大二」，模型拿这句当依据把年级写成「博士」：短值原来跳过支撑度检查，照样按 declared 存进去。"""
    uid = _uid()
    acc, rej = memory.validate_ops(uid, [{**GRADE, "value": "博士"}, {**GRADE, "key": "major", "value": "物理"}], ["我大二"])
    assert acc == [] and [x["reason"] for x in rej] == ["grade_not_in_evidence", "short_value_not_in_evidence"]
    acc, rej = memory.validate_ops(uid, [{**GRADE, "value": "本科二年级"}, GRADE], ["我大二"])
    assert rej == [] and [a["value"] for a in acc] == ["本科二年级", "大二"]  # 同一个年级换个说法照收


@pytest.mark.parametrize("field,bad", [
    ("key", ["grade"]), ("key", {"k": "grade"}), ("key", 20), ("value", ["大二"]), ("value", {"学年": "大二"}),
    ("category", ["background"]), ("source", ["declared"]), ("evidence_quote", ["我大二"]), ("target_fact_id", ["f1"]),
    ("affects", ["task_difficulty"]), ("valid_until", []), ("valid_until", {}), ("op", ["add"]),
])
def test_a_field_of_the_wrong_type_rejects_only_that_op(field, bad):
    """模型把字段写成列表、对象、数字：原来 key 一错就 .strip() 抛异常（整轮 500，流里只剩 error），valid_until 写成 [] / {} 能过校验、到 SQLite 才炸。"""
    uid = _uid()
    acc, rej = memory.validate_ops(uid, [{**GRADE, field: bad}, GRADE], ["我大二"])
    assert [a["key"] for a in acc] == ["grade"]
    assert len(rej) == 1 and rej[0]["reason"] == f"bad_type:{field}"
    memory.apply_ops(uid, acc, "")
    assert [f.value for f in store.list_facts(uid)] == ["大二"]


@pytest.mark.parametrize("field,bad", [("key", ["grade"]), ("valid_until", [])])
def test_a_wrong_type_in_a_memory_op_does_not_break_the_turn(monkeypatch, field, bad):
    """同一个问题走整轮对话：原来这一轮直接抛异常，没有回复。"""
    uid = _uid()
    _model(monkeypatch, _proposal(ops=[{**GRADE, field: bad}]))
    r = dialogue.turn(uid, "我大二")
    assert r["reply"] == "规则保底正文" and [x["reason"] for x in r["rejected_ops"]] == [f"bad_type:{field}"]
    assert [m["role"] for m in store.list_messages(uid)] == ["user", "assistant"]


@pytest.mark.parametrize("gist", [{"summary": "嵌套"}, 20, ["x"]])
def test_a_bad_gist_does_not_break_the_turn_after_writes(monkeypatch, gist):
    """understanding.gist 写成对象、数字、列表：原来事实和助手消息都存了，最后更新会话标题时才抛异常，这一轮没有结果。"""
    uid = _uid()
    _model(monkeypatch, _proposal(ops=[GRADE], gist=gist))
    r = dialogue.turn(uid, "我大二")
    assert r["reply"] == "规则保底正文" and [f["key"] for f in r["facts_added"]] == ["grade"]
    assert [m["role"] for m in store.list_messages(uid)] == ["user", "assistant"]


@pytest.mark.parametrize("data", [
    [1], "oops", 5, {"choices": {"0": 1}}, {"choices": [1]}, {"choices": [{"message": "x"}]},
    {"choices": [{"message": {"content": ["text"]}}]}, {"choices": [{"message": {"content": {"text": "x"}}}]},
    {"choices": [{"message": {"content": 23}}]},
])
def test_a_malformed_envelope_is_a_failed_call(monkeypatch, model_env, capsys, data):
    """顶层是数组、content 是列表这类回包：原来在降级边界外抛异常，整轮 500。"""
    monkeypatch.setattr(llm, "_post", lambda *a: (data, ""))
    assert llm.chat("s", "u") is None
    assert '"error": "bad payload"' in capsys.readouterr().out


def test_usage_of_the_wrong_shape_keeps_the_reply(monkeypatch, model_env):
    """usage 是列表：原来读 token 数时抛异常，好好的回复也丢了。"""
    monkeypatch.setattr(llm, "_post", lambda *a: ({"choices": [{"message": {"content": "hello"}}], "usage": [1]}, ""))
    assert llm.chat("s", "u") == "hello"


def test_invalid_utf8_is_a_failed_call(monkeypatch, model_env):
    """回包不是合法 UTF-8：原来 UnicodeDecodeError 从 _post 漏出去。"""
    monkeypatch.setattr(llm, "fetch", lambda *a, **k: b'{"x":"\xff"}')
    data, err = llm._post("https://model.invalid/v1/chat/completions", "dummy-test-key", {}, 1)
    assert data is None and err
    assert llm.chat("s", "u") is None


def test_malformed_stream_chunks_are_skipped(monkeypatch, model_env):
    """流里的块形状不对（choices 不是列表、delta 不是对象、content 不是字符串）：原来要么抛异常，要么把列表当正文交出去。"""
    lines = [b'data: [1]', b'data: "x"', b'data: {"choices":{"delta":{"content":"a"}}}', b'data: {"choices":[1]}',
             b'data: {"choices":[{"delta":[1]}]}', b'data: {"choices":[{"delta":{"content":["b"]}}]}',
             b'data: {"choices":[{"delta":{"content":7}}]}', b'data: {"choices":[{"delta":{"content":"hello"}}]}', b'data: [DONE]']
    monkeypatch.setattr(llm, "stream", lambda *a, **k: iter(lines))
    assert list(llm.chat_stream("s", "u")) == ["hello"]


@pytest.mark.parametrize("tail,finished", [
    (b'data: [DONE]', True), (b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', True),
    (b'data: {"choices":', False), (b'data: {"choices":[{"delta":{"content":"\xe4\xbd', False),
])
def test_chat_stream_tells_the_caller_whether_it_finished(monkeypatch, model_env, tail, finished):
    """流正常收尾（[DONE] 或 finish_reason）和中途断掉，原来调用方分不出来。"""
    monkeypatch.setattr(llm, "stream", lambda *a, **k: iter([OK_LINE, tail]))
    status: dict = {}
    assert list(llm.chat_stream("s", "u", status=status)) == ["partial"]
    assert bool(status.get("finished")) is finished


@pytest.mark.parametrize("tail,reply,degraded", [
    (b'data: [DONE]', "partial", False),
    (b'data: {"choices":', "规则保底正文", False),  # 留下的是调用①的完整回复，本来就是模型写的：不算降级（不提示「未连接模型」）
    (b'data: {"choices":[{"delta":{"content":"\xe4\xbd', "规则保底正文", False),
])
def test_a_stream_cut_off_midway_keeps_the_complete_reply(monkeypatch, model_env, tail, reply, degraded):
    """正文流了一段后断在半个 JSON / 半个汉字上：原来把这半截「partial」当成功回复存下、degraded=false，盖掉了调用①写好的完整回复。"""
    uid = _uid()
    _model(monkeypatch, _proposal())
    monkeypatch.setattr(llm, "stream", lambda *a, **k: iter([OK_LINE, tail]))
    events = list(dialogue.turn_steps(uid, "我大二", stream_reply=True))
    assert [p["text"] for k, p in events if k == "delta"] == ["partial"]  # 已经流给浏览器的照旧
    result = events[-1][1]
    assert result["reply"] == reply and result["degraded"] is degraded
    assert store.list_messages(uid)[-1]["text"] == reply
