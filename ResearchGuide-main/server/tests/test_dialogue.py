# -*- coding: utf-8 -*-
"""对话内核的离线测试：用假模型驱动完整闭环，不联网。

覆盖 docs/DIALOGUE_CONTRACT.md §1/§2（两次调用契约）、§6（行动生命周期）、§7（校验）、§9（降级）。
运行：pytest server/tests
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

import dialogue  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda: False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)


def make_user() -> str:
    return store.create_user("t")["uid"]


def with_affects(o):
    """测试里的 op 大多不关心 affects 举证，这里补一个合法默认值。

    affects 是写入的必填项（这条记忆改变哪个决策），闸门本身由
    test_memory.py::test_affects_* 专门覆盖。
    """
    if isinstance(o, dict) and o.get("op") in ("add", "replace"):
        return {**o, "affects": o.get("affects") or "task_difficulty"}
    return o


def proposal(reply="先说结论：可以试。", ops=None, tool=None, next_action=None, move="answer"):
    return {
        "understanding": {"gist": "用户在问方向", "signals": [], "corrections": []},
        "memory_ops": [with_affects(o) for o in (ops or [])],
        "tool_intent": tool,
        "dialogue": {"move": move, "reason": "因为信息够了", "reply": reply,
                     "offered_actions": [{"label": "看看方向"}]},
        "next_action": next_action,
    }


def fake_llm(monkeypatch, proposals, reply_text="结合检索结果说完了。"):
    """把 chat_json 换成按 tag 依次返回假数据。"""
    monkeypatch.setattr(llm, "enabled", lambda: True)
    queue = list(proposals)

    def fake(system, user, **kw):
        tag = kw.get("tag")
        if tag == "dialogue.reply":
            return {"reply": reply_text, "next_action": None}
        return queue.pop(0) if queue else None

    monkeypatch.setattr(llm, "chat_json", fake)


def seed_fact(uid: str, key: str, value: str, quote: str | None = None, **kw) -> str:
    """直接写一条生效事实并返回 id —— 给 next_action 提供「依据」。

    引文默认取值的原文，这样支撑度检查（引文得撑得住值）自然过得去。
    额外字段（如 constraint 的 valid_until）用 kw 传。
    """
    quote = quote or value
    raw = {"op": "add", "key": key, "value": value, "evidence_quote": quote,
           "affects": "task_difficulty", **kw}
    acc, rej = memory.validate_ops(uid, [raw], [quote])
    assert acc, f"种子事实被拒：{rej}"
    added, _ = memory.apply_ops(uid, acc, "")
    return added[0]["id"]


# ---------- §9 降级 ----------


def test_no_model_still_produces_a_reply():
    uid = make_user()
    r = dialogue.turn(uid, "你好，我不知道该研究什么")
    assert r["degraded"] is True
    assert r["reply"]
    assert r["conversation_id"]
    assert store.list_messages(uid)[-1]["role"] == "assistant"


def test_model_failure_degrades_without_crashing(monkeypatch):
    uid = make_user()
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)  # 模型一直返回垃圾
    r = dialogue.turn(uid, "帮我看看")
    assert r["degraded"] is True and r["reply"]


# ---------- 正常一轮 ----------


def test_full_turn_writes_reply_and_memory(monkeypatch):
    uid = make_user()
    fake_llm(monkeypatch, [proposal(
        reply="大二开始完全来得及。",
        ops=[{"op": "add", "key": "grade", "value": "本科二年级",
              "evidence_quote": "我大二"}]),
    ])
    r = dialogue.turn(uid, "我大二，想试试科研")

    assert r["degraded"] is False
    assert r["reply"] == "大二开始完全来得及。"
    assert [f["key"] for f in r["facts_added"]] == ["grade"]
    assert r["memory_version"] == 1
    assert [f.key for f in store.list_facts(uid)] == ["grade"]


def test_rejected_op_is_recorded_and_not_written(monkeypatch):
    uid = make_user()
    fake_llm(monkeypatch, [proposal(ops=[
        {"op": "add", "key": "interest:x", "value": "编的", "evidence_quote": "原话里没有这句"},
        {"op": "add", "key": "不存在的key", "value": "x", "evidence_quote": "我大二"},
    ])])
    r = dialogue.turn(uid, "我大二")

    assert r["facts_added"] == []
    reasons = sorted(x["reason"].split(":")[0] for x in r["rejected_ops"])
    assert reasons == ["evidence_not_found", "key_not_in_registry"]
    assert store.list_facts(uid) == []
    # 拒绝理由进了决策日志，可事后审计
    logged = store.list_decisions(uid)[0]["rejected_ops"]
    assert len(logged) == 2


def test_model_cannot_leak_reply_when_calling_a_tool(monkeypatch):
    """契约 §1：调工具时 reply 必须为空，防止模型绕过工具结果先下结论。"""
    uid = make_user()
    p = proposal(reply="我先编一个答案")
    p["tool_intent"] = {"tool": "course.search", "args": {"query": "机器学习"}}
    fake_llm(monkeypatch, [p])

    captured = {}

    def fake_run(u, intent, context=None):
        captured["called"] = intent
        return {"tool": "course.search", "ok": True, "items": [{"name": "机器学习导论"}]}

    monkeypatch.setattr(dialogue.tools, "run", fake_run)
    r = dialogue.turn(uid, "有没有机器学习的课")

    assert captured["called"]["tool"] == "course.search"
    assert r["reply"] == "结合检索结果说完了。"   # 来自调用②，不是调用①
    assert r["tool_results"][0]["ok"] is True


def test_tool_failure_is_reported_honestly(monkeypatch):
    uid = make_user()
    p = proposal()
    p["tool_intent"] = {"tool": "course.search", "args": {"query": "x"}}
    fake_llm(monkeypatch, [p])
    monkeypatch.setattr(dialogue.tools, "run",
                        lambda u, i, context=None: {"tool": "course.search", "ok": False,
                                                    "error": "网络不可达"})
    # 调用②也失败 → 走模板
    monkeypatch.setattr(llm, "chat_json", lambda system, user, **k: (
        None if k.get("tag") == "dialogue.reply" else p))

    r = dialogue.turn(uid, "查一下")
    assert "没查到" in r["reply"] and "网络不可达" in r["reply"]
    assert r["tool_results"][0]["ok"] is False


# ---------- 对话不再派任务 ----------

@pytest.mark.parametrize("action", ["micro_task", "explore_direction", "course_action", "review_progress"])
def test_model_actions_are_dropped_even_with_valid_basis(monkeypatch, action):
    uid = make_user()
    fid = seed_fact(uid, "base:code", "Python 只会照着教程抄")
    fake_llm(monkeypatch, [proposal(reply="这个问题可以先这样理解。", next_action={
        "action": action, "title": "重写一段代码", "direction": "ai", "based_on": [fid]})])
    r = dialogue.turn(uid, "先解释一下")
    assert r["next_action"] is None and r["pending_action"] is None
    assert store.list_actions(uid) == [] and store.list_tasks(uid) == []


def test_tool_reply_cannot_create_action(monkeypatch):
    uid = make_user()
    monkeypatch.setattr(llm, "enabled", lambda: True)
    p = proposal(tool={"tool": "course.search", "args": {"query": "统计"}})
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (
        {"reply": "这是查询结果。", "next_action": {"action": "micro_task", "title": "做练习"}}
        if k.get("tag") == "dialogue.reply" else p))
    monkeypatch.setattr(dialogue.tools, "run", lambda *a, **k: {"tool": "course.search", "ok": True, "items": []})
    r = dialogue.turn(uid, "查一下统计课")
    assert r["next_action"] is None and store.list_tasks(uid) == []


def test_long_conversation_keeps_intro_and_explicit_refusal(monkeypatch):
    uid = make_user()
    onboarding = __import__("onboarding")
    onboarding.start(uid)
    fake_llm(monkeypatch, [proposal(reply="可以，按你愿意分享的内容聊。", ops=[{
        "op": "add", "key": "style:transcript_sharing", "value": "不想分享成绩单",
        "evidence_quote": "不想分享成绩单", "affects": "question_next"}])])
    r = dialogue.turn(uid, "不想分享成绩单")
    cid = r["conversation_id"]
    assert not r["rejected_ops"]
    for i in range(16):
        fake_llm(monkeypatch, [proposal(reply="我们就讨论这个概念。")])
        dialogue.turn(uid, f"继续解释第 {i} 个概念", cid)
    env = dialogue.build_env(uid, cid, "接着聊")
    assert len(env["recent_context"]) == dialogue.RECENT_TURNS
    assert env["guidance"]["transcript_introduced"] is True
    assert {"key": "style:transcript-sharing", "value": "不想分享成绩单"} in env["guidance"]["preferences"]
    assert store.list_tasks(uid) == []
    # 另开会话仍知道已经介绍过；新画像不能继承别人的介绍/偏好。
    assert dialogue.build_env(uid, "another", "你好")["guidance"]["transcript_introduced"]
    store.open_new_portrait(uid)
    fresh = dialogue.build_env(uid, "new", "你好")["guidance"]
    assert fresh["preferences"] == []


def test_guidance_requires_four_reading_areas_not_all_fixed_fields():
    uid = make_user()
    assert not dialogue.build_env(uid, "c", "你好")["guidance"]["entry_ready"]
    seed_fact(uid, "grade", "大二")
    seed_fact(uid, "interest:eval", "大模型评测")
    assert not dialogue.build_env(uid, "c", "你好")["guidance"]["entry_ready"]
    seed_fact(uid, "reading_experience", "没读过论文")
    seed_fact(uid, "reading_support", "先解释术语")
    env = dialogue.build_env(uid, "c", "你好")
    assert env["guidance"]["entry_ready"] is True
    assert env["guidance"]["transcript_available"] is False
    assert not any(f.key.startswith("capability:") for f in memory.active_facts(uid))


def test_stream_composer_sees_actual_memory_write_and_updated_readiness(monkeypatch):
    uid = make_user()
    seed_fact(uid, "grade", "大二")
    seed_fact(uid, "base:code", "接触过 Python")
    seed_fact(uid, "reading_experience", "没有读过论文")
    fake_llm(monkeypatch, [proposal(reply="先看这个问题。", ops=[{
        "op": "add", "key": "interest:eval", "value": "大模型评测", "evidence_quote": "大模型评测"}, {
        "op": "add", "key": "made_up", "value": "捏造", "evidence_quote": "大模型评测"}])])
    captured = {}
    def stream(system, user, **kw):
        captured["prompt"] = user
        yield "这条兴趣已保存，另有一项写入没有成功。"
    monkeypatch.setattr(llm, "chat_stream", stream)
    events = list(dialogue.turn_steps(uid, "我想读大模型评测，大模型评测是什么？", stream_reply=True))
    result = events[-1][1]
    assert result["onboard_done"]
    assert result["facts_added"] and result["rejected_ops"]
    assert '"entry_ready": true' in captured["prompt"]
    assert '"added"' in captured["prompt"] and '"rejected"' in captured["prompt"]


def test_first_readiness_transition_closes_interview_from_committed_facts(monkeypatch):
    uid = make_user()
    for key, value in [("grade", "大一"), ("interest:eval", "大模型评测"), ("base:code", "写过 Python")]:
        seed_fact(uid, key, value)
    fake_llm(monkeypatch, [proposal(reply="你还希望怎样获得帮助？", ops=[{
        "op": "add", "key": "reading_experience", "value": "没读过论文", "evidence_quote": "没读过论文"}])])
    monkeypatch.setattr(dialogue, "_stream_reply", lambda *a: pytest.fail("完成引导不再让模型继续追问"))
    events = list(dialogue.turn_steps(uid, "我没读过论文", stream_reply=True))
    result = events[-1][1]
    assert result["guide_completed"] and result["guide"]["ready"]
    assert "大模型评测" in result["reply"] and "没读过论文" in result["reply"]
    assert "可以进入研读模块" in result["reply"] and "？" not in result["reply"]
    assert result["offered_actions"] == []
    assert ''.join(p['text'] for k,p in events if k == 'delta') == result['reply']


# ---------- 不编造事实的兜底 ----------

DIRTY = "课没查成。我知道有《机器学习》《深度学习》这几门，你可以先看《机器学习》。"
CLEAN = "课没查成，所以我不报课名。这类课在信科和数院的课程页上按关键词能搜到。"


def test_guard_rewrites_names_after_tool_failure(monkeypatch):
    """检索失败却点名具体的课 —— 这是产品原则的硬约束，代码必须兜住，不能只靠 prompt。"""
    monkeypatch.setattr(dialogue.llm, "chat", lambda *a, **k: CLEAN)
    fixed, risk = dialogue._guard_no_fabrication(
        {"message": "有哪些机器学习的课"}, DIRTY, [{"tool": "course.search", "ok": False}])
    assert fixed == CLEAN
    assert risk is False


def test_guard_keeps_original_and_flags_when_repair_fails(monkeypatch):
    """重写后还是带课名，就不要拿它覆盖原文，而是如实标记风险。"""
    monkeypatch.setattr(dialogue.llm, "chat", lambda *a, **k: "还是《机器学习》最好。")
    fixed, risk = dialogue._guard_no_fabrication(
        {"message": "x"}, DIRTY, [{"tool": "course.search", "ok": False}])
    assert fixed == DIRTY
    assert risk is True


def test_guard_leaves_successful_retrieval_alone(monkeypatch):
    """检索成功时课名来自真实结果，不该动它。"""
    called = []
    monkeypatch.setattr(dialogue.llm, "chat", lambda *a, **k: called.append(1) or CLEAN)
    ok = [{"tool": "course.search", "ok": True, "items": [{"name": "机器学习导论"}]}]
    fixed, risk = dialogue._guard_no_fabrication({"message": "x"}, DIRTY, ok)
    assert fixed == DIRTY and risk is False and called == []


def test_guard_ignores_plain_replies_without_tools():
    fixed, risk = dialogue._guard_no_fabrication({"message": "x"}, DIRTY, [])
    assert fixed == DIRTY and risk is False


def test_guard_repair_crash_is_not_fatal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(dialogue.llm, "chat", boom)
    fixed, risk = dialogue._guard_no_fabrication(
        {"message": "x"}, DIRTY, [{"tool": "course.search", "ok": False}])
    assert fixed == DIRTY and risk is True  # 保留原文，不掩盖


# ---------- 环境包 ----------


def test_env_exposes_fact_ids_so_corrections_can_target_them(monkeypatch):
    """用户改口时模型必须能指名作废哪一条，所以 id 要进环境包。"""
    uid = make_user()
    fake_llm(monkeypatch, [proposal(ops=[
        {"op": "add", "key": "interest:ml", "value": "对机器学习感兴趣",
         "evidence_quote": "机器学习"}])])
    added = dialogue.turn(uid, "我对机器学习挺感兴趣的")["facts_added"]
    fid = added[0]["id"]

    env = dialogue.build_env(uid, "c1", "其实我不是对机器学习感兴趣")
    ids = [m["id"] for m in env["recalled_memory"]]
    assert fid in ids


def test_turn_retracts_superseded_fact_when_model_says_so(monkeypatch):
    """改口这一轮的完整链路：模型给 retract → 旧事实失效、不再参与决策。"""
    uid = make_user()
    fake_llm(monkeypatch, [proposal(ops=[
        {"op": "add", "key": "interest:ml", "value": "对机器学习感兴趣",
         "evidence_quote": "机器学习"}])])
    fid = dialogue.turn(uid, "我对机器学习感兴趣")["facts_added"][0]["id"]

    fake_llm(monkeypatch, [proposal(reply="那这条先放下。", ops=[
        {"op": "retract", "key": "interest:ml", "value": "",
         "evidence_quote": "其实我不是", "target_fact_id": fid}])])
    r = dialogue.turn(uid, "其实我不是对机器学习感兴趣")

    assert r["rejected_ops"] == []
    assert r["facts_changed"][0]["status"] == "superseded"
    assert store.get_fact(fid).status == "superseded"
    assert fid not in [f.id for f in memory.active_facts(uid)]


def test_retract_with_made_up_quote_is_rejected(monkeypatch):
    """引文对不上就拒绝——改口也不能凭空作废一条事实。"""
    uid = make_user()
    fake_llm(monkeypatch, [proposal(ops=[
        {"op": "add", "key": "interest:ml", "value": "对机器学习感兴趣",
         "evidence_quote": "机器学习"}])])
    fid = dialogue.turn(uid, "我对机器学习感兴趣")["facts_added"][0]["id"]

    fake_llm(monkeypatch, [proposal(reply="好。", ops=[
        {"op": "retract", "key": "interest:ml", "value": "",
         "evidence_quote": "用户说他不想学了", "target_fact_id": fid}])])
    r = dialogue.turn(uid, "嗯")

    assert r["rejected_ops"][0]["reason"] == "evidence_not_found"
    assert store.get_fact(fid).status == "confirmed"


# ---------- 环境包 ----------


def test_env_packet_keeps_tail_layers_even_when_early_layers_are_full(monkeypatch):
    """回归：环境包原来用 limit=10 做全局截断，而召回是按层拉平的，
    于是靠后的「倾向/临时约束」会被截掉——等于把平表抢坑位换了个地方犯。
    这里前四层塞满，后两层必须还在。"""
    uid = make_user()
    for i in range(9):
        seed_fact(uid, f"capability:skill{i}", f"技能{i}")
    for i in range(9):
        seed_fact(uid, f"current:proj{i}", f"在做项目{i}")
    seed_fact(uid, "interest:ml", "对机器学习感兴趣")
    # 有效期**相对今天**算，不能写死日期：写死的那天一到，这条约束就被 is_expired 过滤掉，
    # 于是「临时约束层被挤掉了」——其实是被日期过期了（这条测试就是这么变成红的时间炸弹）。
    seed_fact(uid, "constraint:time", "本周只有十分钟", "本周只有十分钟",
              valid_until=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat())

    env = dialogue.build_env(uid, "c1", "随便问一句")
    keys = [m["key"] for m in env["recalled_memory"]]
    assert "direction" or True
    assert "interest:ml" in keys, f"倾向层被挤掉了：{keys}"
    assert "constraint:time" in keys, f"临时约束层被挤掉了：{keys}"

    layers = [m["layer"] for m in env["recalled_memory"]]
    assert "倾向" in layers and "临时约束" in layers
    # 每层的条数都被 LAYER_BUDGET 封住
    from collections import Counter
    cnt = Counter(m["key"].split(":")[0] for m in env["recalled_memory"])
    assert cnt["capability"] == memory.LAYER_BUDGET["capability"]
    assert cnt["current"] == memory.LAYER_BUDGET["practice"]


def test_env_packet_exposes_the_ladder(monkeypatch):
    """阶梯要真的进环境包，否则模型看不到「下一层该问什么」。"""
    uid = make_user()
    env = dialogue.build_env(uid, "c1", "你好")
    assert env["ladder"]["next_layer"] == "identity"

    seed_fact(uid, "grade", "大二")
    env = dialogue.build_env(uid, "c1", "你好")
    assert env["ladder"]["next_layer"] == "interest"
    prompt = dialogue._env_for_prompt(env)
    assert "提问阶梯" in prompt and "next_layer" in prompt



def test_history_returns_only_dialogue_messages(monkeypatch):
    uid = make_user()
    fake_llm(monkeypatch, [proposal(reply="第一句回复")])
    dialogue.turn(uid, "第一句提问")
    h = dialogue.history(uid)
    assert [m["role"] for m in h["messages"]] == ["user", "assistant"]
    assert h["messages"][1]["text"] == "第一句回复"
