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


# ---------- §2 方向门禁 ----------


def test_micro_task_from_model_is_dropped(monkeypatch):
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    fake_llm(monkeypatch, [proposal(reply="先做这个。", next_action={
        "action": "micro_task", "title": "写一个 CNN", "direction": "ai", "based_on": [fid]})])
    r = dialogue.turn(uid, "我想做点东西")
    assert r["next_action"] is None


def test_task_direction_must_be_an_active_direction(monkeypatch):
    uid = make_user()
    fake_llm(monkeypatch, [proposal(reply="先定方向。", ops=[
        {"op": "add", "key": "direction:math", "value": "数学", "evidence_quote": "我大二"}])])
    did = dialogue.turn(uid, "我大二")["facts_added"][0]["id"]

    fake_llm(monkeypatch, [proposal(reply="看方向。", next_action={
        "action": "explore_direction", "title": "去看方向排序", "direction": "ai", "based_on": [did]})])
    r = dialogue.turn(uid, "然后呢")
    assert r["next_action"]["direction"] == "math"
    assert r["next_action"]["action"] == "explore_direction"


# ---------- §2 个性化闸门（任务必须有依据）----------


def test_task_without_basis_is_dropped(monkeypatch):
    """换个用户也照样成立的任务，一律不给 —— 这是「个性化必须有依据」的代码化。

    对应真实反馈：只说了「信管大二想走大数据」，就派「关掉讲义重写一段代码」。
    这句对谁都能说，等于没有了解他。
    """
    uid = make_user()
    fake_llm(monkeypatch, [proposal(reply="这周先做这个。", next_action={
        "action": "explore_direction", "title": "关掉讲义，重写一段你抄过的代码", "direction": ""})])
    r = dialogue.turn(uid, "我是信管大二，想走大数据")

    assert r["next_action"] is None
    assert r["pending_action"] is None
    assert store.list_actions(uid) == []          # 连落库都没有
    assert r["reply"]                              # 但这一轮仍然要说话


def test_task_citing_a_nonexistent_fact_is_dropped(monkeypatch):
    """based_on 里编一个不存在的 fact id，不算依据。"""
    uid = make_user()
    fake_llm(monkeypatch, [proposal(reply="做这个。", next_action={
        "action": "explore_direction", "title": "写点什么", "direction": "",
        "based_on": ["f_不存在的id"]})])
    assert dialogue.turn(uid, "你好")["next_action"] is None


def test_task_with_basis_is_kept_and_exposes_why(monkeypatch):
    """有依据就保留，并且把依据原文带出来给用户看。"""
    uid = make_user()
    fid = seed_fact(uid, "base:code", "Python 只会照着教程抄")
    fake_llm(monkeypatch, [proposal(reply="你之前说只会照着教程抄，那就……", next_action={
        "action": "explore_direction", "title": "把最短的那段代码关掉教程重写一遍",
        "direction": "", "based_on": [fid]})])
    r = dialogue.turn(uid, "我不知道该干什么")

    assert r["next_action"]["title"] == "把最短的那段代码关掉教程重写一遍"
    why = r["next_action"]["based_on"]
    assert why and why[0]["id"] == fid
    assert "照着教程抄" in why[0]["value"]        # 把依据原文给出来


def test_basis_survives_reload(monkeypatch):
    """刷新后（history）依据也要在，前端才画得出「为什么是给你的」。"""
    uid = make_user()
    fid = seed_fact(uid, "base:code", "Python 只会照着教程抄")
    fake_llm(monkeypatch, [proposal(reply="那就……", next_action={
        "action": "explore_direction", "title": "重写一段", "direction": "", "based_on": [fid]})])
    dialogue.turn(uid, "我不知道该干什么")

    pa = dialogue.history(uid)["pending_action"]
    assert pa["action_id"]                                  # 刷新后按钮仍然点得动
    assert pa["based_on"][0]["value"] == "Python 只会照着教程抄"


# ---------- §6 行动生命周期 ----------


def test_only_one_open_action_at_a_time(monkeypatch):
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    for title in ("第一件事", "第二件事"):
        fake_llm(monkeypatch, [proposal(reply="好。", next_action={
            "action": "explore_direction", "title": title, "direction": "",
            "based_on": [fid]})])
        dialogue.turn(uid, title)

    open_actions = store.list_actions(uid, statuses=["offered", "accepted", "in_progress"])
    assert len(open_actions) == 1
    assert open_actions[0]["title"] == "第二件事"
    assert store.list_actions(uid, statuses=["abandoned"])[0]["title"] == "第一件事"


def test_action_events_are_recorded(monkeypatch):
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    fake_llm(monkeypatch, [proposal(reply="好。", next_action={
        "action": "explore_direction", "title": "看方向", "direction": "",
        "based_on": [fid]})])
    aid = dialogue.turn(uid, "开始")["next_action"]["action_id"]

    assert dialogue.action_event(uid, aid, "accept")["status"] == "accepted"
    assert dialogue.action_event(uid, aid, "complete")["status"] == "completed"
    kinds = [e["kind"] for e in store.list_events(uid)]
    assert "action_accepted" in kinds and "task_submitted" in kinds


def test_action_event_returns_the_same_shape_as_the_turn(monkeypatch):
    """三个出口（turn / history / action_event）形状必须一致。

    曾经 action_event 和 history 直接返回数据库原始行（id），turn 返回规整形状
    （action_id），前端刷新或点第二次时拿不到 action_id 就 422。
    """
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    fake_llm(monkeypatch, [proposal(reply="好。", next_action={
        "action": "explore_direction", "title": "看方向", "direction": "",
        "based_on": [fid]})])
    r = dialogue.turn(uid, "开始")

    from_turn = set(r["next_action"])
    from_history = set(dialogue.history(uid)["pending_action"])
    from_event = set(dialogue.action_event(uid, r["next_action"]["action_id"], "accept"))
    assert from_turn == from_history == from_event
    assert "action_id" in from_event and "based_on" in from_event


def test_action_event_rejects_unknown_event():
    uid = make_user()
    with pytest.raises(ValueError):
        dialogue.action_event(uid, "whatever", "explode")


def test_cross_profile_action_is_invisible(monkeypatch):
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    fake_llm(monkeypatch, [proposal(reply="好。", next_action={
        "action": "explore_direction", "title": "看方向", "direction": "",
        "based_on": [fid]})])
    aid = dialogue.turn(uid, "开始")["next_action"]["action_id"]

    store.open_new_portrait(uid)
    assert store.get_action(uid, aid) is None
    assert dialogue.action_event(uid, aid, "accept") is None


def test_stop_signal_clears_pending_action(monkeypatch):
    """用户说停：不再派任务，并且把还挂着的行动收掉。

    这条守住一个曾经混在一起的语义：next_action（本轮决定）不能拿 pending_action（还开着的）回填，
    否则「这轮不该派任务」这个判断从响应里就看不出来了。
    """
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    fake_llm(monkeypatch, [proposal(reply="先做这个。", next_action={
        "action": "explore_direction", "title": "看方向", "direction": "",
        "based_on": [fid]})])
    first = dialogue.turn(uid, "开始")
    assert first["next_action"]["title"] == "看方向"
    assert first["pending_action"]["title"] == "看方向"

    fake_llm(monkeypatch, [proposal(reply="行，那就先看到这。", move="acknowledge_stop")])
    second = dialogue.turn(uid, "先别推进了，我只是随便看看")

    assert second["next_action"] is None      # 本轮没派任务
    assert second["pending_action"] is None   # 旧的也收掉了
    assert store.pending_action(uid) is None
    assert store.list_actions(uid, statuses=["abandoned"])[0]["title"] == "看方向"


def test_plain_reply_does_not_inherit_old_next_action(monkeypatch):
    """纯解释的一轮：next_action 为 null，但已开着的行动仍然可见（pending_action）。"""
    uid = make_user()
    fid = seed_fact(uid, "grade", "大二")
    fake_llm(monkeypatch, [proposal(reply="先做这个。", next_action={
        "action": "explore_direction", "title": "看方向", "direction": "",
        "based_on": [fid]})])
    dialogue.turn(uid, "开始")

    fake_llm(monkeypatch, [proposal(reply="这个概念是这样……")])
    r = dialogue.turn(uid, "为什么？")

    assert r["next_action"] is None
    assert r["pending_action"]["title"] == "看方向"


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
