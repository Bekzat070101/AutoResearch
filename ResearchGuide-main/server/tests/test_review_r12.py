# -*- coding: utf-8 -*-
"""Codex 第十二轮：记忆的短值规则既不能放过矛盾、也不能拒掉学生真说过的；流到字数上限算半截；
模型给的落单代理字符；大整数分数；引文类型不对；跨零点倒序的分拣计数。不调真的模型。"""
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
import workbench  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r12.db")
    store.init_db()


def _uid():
    return store.create_user("t")["uid"]


def _op(key, value, quote, op="add", affects="task_difficulty"):
    return {"op": op, "key": key, "value": value, "evidence_quote": quote, "affects": affects}


def _check(key, value, quote, pool=None):
    acc, rej = memory.validate_ops(_uid(), [_op(key, value, quote)], pool or [quote])
    return acc, rej


@pytest.mark.parametrize("value,quote", [
    ("本科二年级", "本科在读"),             # 原话没说第几年
    ("大二", "我不是大二，我是研一"),        # 被否定的
    ("博士", "我大二"),
])
def test_grade_contradictions_are_rejected(value, quote):
    """「本科在读」写成「本科二年级」、「我不是大二」写成「大二」：原来都按自述存了（Codex 复现）。"""
    acc, rej = _check("grade", value, quote)
    assert acc == [] and rej[0]["reason"] in ("short_value_not_in_evidence", "grade_not_in_evidence")


@pytest.mark.parametrize("value,quote", [
    ("研一", "我不是大二，我是研一"),
    ("本科二年级", "I am a sophomore"),
    ("大二", "大二下"),
    ("本科", "本科在读"),
])
def test_grade_things_students_actually_say_are_kept(value, quote):
    acc, rej = _check("grade", value, quote)
    assert len(acc) == 1, rej


@pytest.mark.parametrize("key,value,quote", [
    ("interest:ai", "人工智能", "想试试 AI"),
    ("interest:ml", "ML", "对机器学习感兴趣"),
])
def test_common_abbreviations_count_as_the_same_thing(key, value, quote):
    """「AI」记成「人工智能」、「机器学习」记成「ML」：原来被当成引文里没有的值拒掉（Codex 复现）。"""
    acc, rej = _check(key, value, quote)
    assert len(acc) == 1, rej


def test_numbers_must_match_the_quote():
    """「每周2小时」撑不住「每周20小时」：原来内容字对得上就收（Codex 复现）。中文数也认：「每周两小时」撑得住「每周2小时」。"""
    acc, rej = _check("constraint:time", "每周20小时", "每周2小时")
    assert acc == [] and rej[0]["reason"] in ("number_not_in_evidence", "short_value_not_in_evidence")
    assert memory._numbers_supported("每周2小时", "每周两小时")
    assert not memory._numbers_supported("每周20小时", "每周2小时")


@pytest.mark.parametrize("value", [2024, 20, 2024.0])
def test_numeric_values_are_turned_into_text_not_rejected(value):
    """模型把入学年份写成数字 2024：原来按类型不对拒掉（Codex 复现）。"""
    raw = _op("background:enroll_year", value, "我是2024年入学的、今年20岁")
    acc, rej = memory.validate_ops(_uid(), [raw], ["我是2024年入学的、今年20岁"])
    assert all(r["reason"] != "bad_type:value" for r in rej)
    if acc:
        assert acc[0]["value"] in ("2024", "20")


def test_booleans_are_still_rejected_as_values():
    acc, rej = memory.validate_ops(_uid(), [_op("grade", True, "我大二")], ["我大二"])
    assert acc == [] and rej[0]["reason"] == "bad_type:value"


def test_a_bare_confirmation_does_not_turn_assistant_words_into_a_declared_fact(monkeypatch):
    """助手问「人工智能还是数学」，学生只回「前者」/「好」：不借助手的话写成学生自述。
    第十二轮试过借，第十三轮 Codex 复现「好」能把助手整段话（含编的）变成学生自述、还能被伪造——宁可不记、下次再问。"""
    uid = _uid()
    monkeypatch.setattr(llm, "enabled", lambda: True)

    def proposal(ops, reply):
        return {"understanding": {"gist": "选方向", "signals": [], "corrections": []}, "memory_ops": ops,
                "tool_intent": None, "next_action": None,
                "dialogue": {"move": "answer", "reason": "确认", "reply": reply, "offered_actions": []}}

    with budget.unmetered():
        monkeypatch.setattr(llm, "chat_json", lambda s, u, **k: proposal([], "你同学每周有20小时。你更想先看人工智能还是数学？")
                            if k.get("tag") == "dialogue.proposal" else None)
        dialogue.turn(uid, "我还没想好方向")
        ops = [{"op": "add", "key": "direction:math", "value": "数学", "evidence_quote": "前者", "affects": "direction_choice"},
               {"op": "add", "key": "constraint:time", "value": "每周20小时", "evidence_quote": "你同学每周有20小时",
                "affects": "task_difficulty"}]
        monkeypatch.setattr(llm, "chat_json", lambda s, u, **k: proposal(ops, "好。")
                            if k.get("tag") == "dialogue.proposal" else None)
        r = dialogue.turn(uid, "前者")
    assert r["facts_added"] == []
    assert {x["reason"] for x in r["rejected_ops"]} <= {"short_value_not_in_evidence", "evidence_not_found", "number_not_in_evidence"}


def test_a_plain_greeting_still_cannot_borrow_the_assistants_words():
    """「你好」不是确认：助手上一句照样不能当证据（第十一轮修的那条不能被上面这条放开）。"""
    pool = ["你好"]
    acc, rej = memory.validate_ops(_uid(), [_op("capability:python", "擅长Python", "你擅长Python")], pool)
    assert acc == [] and rej[0]["reason"] == "evidence_not_found"


def _stream(monkeypatch, lines):
    monkeypatch.setenv("LLM_API_KEY", "dummy-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setattr(llm, "stream", lambda *a, **k: iter(lines))


@pytest.mark.parametrize("tail", [
    [b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'],
    [b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}', b"data: [DONE]"],
    [b'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}', b"data: [DONE]"],
])
def test_a_stream_cut_by_the_token_limit_is_not_finished(monkeypatch, tail):
    """到了字数上限（finish_reason=length）的也是半截：原来当成说完了，半截覆盖了完整回复（Codex 复现）。"""
    _stream(monkeypatch, ['data: {"choices":[{"delta":{"content":"请完成步骤一，然后"}}]}'.encode(), *tail])
    status: dict = {}
    with budget.unmetered():
        assert list(llm.chat_stream("s", "u", status=status)) == ["请完成步骤一，然后"]
    assert not status.get("finished")


def test_a_stream_that_says_stop_is_finished(monkeypatch):
    _stream(monkeypatch, ['data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}'.encode()])
    status: dict = {}
    with budget.unmetered():
        assert list(llm.chat_stream("s", "u", status=status)) == ["好"]
    assert status.get("finished") is True


def test_lone_surrogates_from_the_model_are_cleaned(monkeypatch):
    """模型 JSON 里的 \\ud800：原来规整过了，存回复时才报错，事实已经写了一半（Codex 复现）。"""
    monkeypatch.setenv("LLM_API_KEY", "dummy-test-key")
    monkeypatch.setattr(llm, "chat", lambda *a, **k: '{"gist": "坏\\ud800字", "list": ["\\udfff"]}')
    with budget.unmetered():
        data = llm.chat_json("s", "u")
    assert data["gist"].encode("utf-8") and data["list"][0].encode("utf-8")


@pytest.mark.parametrize("score", [10 ** 309, -(10 ** 309), 101, -1])
def test_huge_or_out_of_range_scores_fall_back_without_500(monkeypatch, score):
    """合法 JSON 大整数 10**309：原来进 math.isfinite 溢出，提交接口 500（Codex 复现）。"""
    from schemas import MicroTask
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"rubric": [{"pass": True, "comment": "好"}] * 3, "score": score})
    t = MicroTask(user_id="u", direction="ai", title="读一节", brief="b", steps=["s"], deliverable="d",
                  rubric=[{"criterion": f"c{i}"} for i in range(3)], time_budget_min=20, difficulty=1)
    assert workbench._llm_feedback(t, "我读完了这一节并写了总结。") is None
    assert workbench._as_int(10 ** 309, 20, 5, 240) == 240


@pytest.mark.parametrize("quote", [["我读了数据"], 3])
def test_evidence_with_a_quote_of_the_wrong_type_is_dropped(monkeypatch, quote):
    """引文是列表、数字：原来当成空引文、文件在就算证据，没做完的项目也拿了 5/5（Codex 复现）。"""
    import io
    import zipfile

    import submission
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.md", "## 题目\n分类问题。" + "说明。" * 30 + "\n## 还没做完的\n分类器还没训练，指标没算。\n")
        z.writestr("src/main.py", "print(1)")
    crit = [{"key": c["key"], "status": "pass", "evidence": [{"file": "README.md", "quote": quote}], "comment": "好", "fix": ""}
            for c in submission.CRITERIA]
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"criteria": crit, "summary": "好"})
    r = submission.review(buf.getvalue(), {"name": "情感分类基线", "todo": "做两个基线"})
    assert all(c["evidence"] == [] and c["status"] != "pass" for c in r["criteria"])


@pytest.mark.parametrize("key,value,quote,ok", [
    ("grade", "本科二年级", "我是本科第2年", True),
    ("grade", "大二", "读本科第二年了", True),
    ("grade", "大一", "以前是大一，现在大二", False),   # 过去的不算
    ("grade", "大二", "以前是大一，现在大二", True),
    ("grade", "大二", "不算大二吧，我休学了", False),   # 含糊的否定
    ("grade", "大四", "我2024年入学，现在大二", False),  # 入学年份里的 4 不是年级
])
def test_grade_phrasings_round13(key, value, quote, ok):
    """第十三轮：「本科第2年」认得；「以前是大一」「不算大二」不算现在的；年份里的数字不当年级（Codex 复现）。"""
    acc, rej = _check(key, value, quote)
    assert (len(acc) == 1) is ok, rej


@pytest.mark.parametrize("value,quote,ok", [
    ("GPA 3.7", "GPA 3.70", True),        # 同一个数
    ("GPA 3.7", "GPA 3.07", False),       # 改了值
    ("每周二十小时", "每周两小时", False),  # 中文数带单位也比
    ("每周2小时", "每周两小时", True),
    ("有一些基础", "有些基础", True),      # 不带单位的「一」不算数
])
def test_numbers_are_compared_by_value(value, quote, ok):
    assert memory._numbers_supported(value, quote) is ok


def test_deeply_nested_model_json_is_a_failed_call(monkeypatch):
    """六百层数组的合法 JSON：清理时递归出错，原来对话直接 500（Codex 复现）。现在当这次调用失败、走回退。"""
    monkeypatch.setenv("LLM_API_KEY", "dummy-test-key")
    deep = '{"a": ' + "[" * 5000 + "]" * 5000 + "}"
    monkeypatch.setattr(llm, "chat", lambda *a, **k: deep)
    with budget.unmetered():
        assert llm.chat_json("s", "u") is None


@pytest.mark.parametrize("tail", [
    ['data: {"choices":[{"delta":{"content":"再读方法，然后"}}]}'.encode()],                      # stop 之后又来正文，然后断了
    ['data: {"choices":[{"delta":{"content":"再读方法，然后"}}]}'.encode(), OSError("reset")],     # 之后传输出错
])
def test_an_early_stop_followed_by_more_text_is_not_finished(monkeypatch, tail):
    """先给了 stop、后面又来正文再断：原来按 stop 算说完了，半截覆盖完整回复（Codex 复现）。"""
    def lines():
        yield 'data: {"choices":[{"delta":{"content":"先读摘要。"},"finish_reason":"stop"}]}'.encode()
        for t in tail:
            if isinstance(t, Exception):
                raise t
            yield t

    monkeypatch.setenv("LLM_API_KEY", "dummy-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setattr(llm, "stream", lambda *a, **k: lines())
    status: dict = {}
    with budget.unmetered():
        list(llm.chat_stream("s", "u", status=status))
    assert not status.get("finished")


@pytest.mark.parametrize("value,quote,ok", [
    ("大三", "我大二，学 Python 三年了", False),             # 学了几年不是年级
    ("大二", "我不是大二，我已经学了二年编程", False),
    ("大四", "二零二四年入学，现在大二", False),
    ("大一", "本科在读，计划一年内读完这本书", False),
    ("大二", "我去年读大二，现在已经大三了", False),          # 过去的
    ("大三", "我去年读大二，现在已经大三了", True),
    ("大二", "大二，哦打错了我是大三", False),                # 改口
    ("大三", "大二，哦打错了我是大三", True),
    ("本科二年级", "second-year undergrad at PKU", True),
    ("大二", "我现在是大 2，空格手滑", True),
    ("本科三年级", "third year undergraduate", True),
    ("本科二年级，GPA4.0", "我本科二年级", False),            # 年级里夹带没说过的 GPA
])
def test_grade_phrasings_round14(value, quote, ok):
    """第十四轮 Codex 复现的年级说法：学了几年、计划、入学年份不是年级；过去的、改口的不算；英文和带空格的认得。"""
    assert memory._short_value_supported("grade", value, quote) is ok


@pytest.mark.parametrize("value,quote,ok", [
    ("每周20小时", "我每周2小时，总共20周", False),   # 20 是周数
    ("每周1.5小时", "每周90分钟", True),               # 换算
    ("每周3至5小时", "每周三到五小时", True),           # 范围
    ("GPA 3.7", "GPA：３．７", True),                  # 全角
    ("高等数学A（1）", "高数A（一）", True),           # 课程序号
    ("英语CET4", "大学英语四级", True),
])
def test_quantities_round14(value, quote, ok):
    assert memory._numbers_supported(value, quote) is ok


def test_a_negated_interest_is_not_an_interest():
    """「我对机器学习没有兴趣」不能记成兴趣「机器学习」；「我不是不喜欢 AI」这种双重否定照样算（Codex 复现）。"""
    assert not memory._short_value_supported("interest:ml", "机器学习", "我对机器学习没有兴趣")
    assert memory._short_value_supported("interest:ai", "AI", "我不是不喜欢 AI，只是这次不想写模型")


@pytest.mark.parametrize("text", [
    "对机器学习" + " " * 20000 + "没有兴趣",
    " " * 20000 + "x",
    "本科" + " " * 20000 + "x",
    "大 " * 10000,
    "3到" * 10000,
])
def test_evidence_helpers_stay_linear_on_long_messages(text):
    """一长串空格原来让年级正则每个位置都把后面的空格吃完再吐回来：两万个空格要二十秒，整轮对话卡住。"""
    import time
    t0 = time.perf_counter()
    memory._grades_in(text)
    memory._quantities(text)
    memory._short_value_supported("interest:ml", "机器学习", text)
    memory._short_value_supported("grade", "大二", text)
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.parametrize("key,value,quote,ok", [
    ("grade", "本科二年级，GPA4.0", "我本科二年级", False),                     # 长值也查夹带
    ("interest:nlp", "机器学习与自然语言处理", "我对机器学习与自然语言处理没有兴趣", False),  # 长值也查否定
    ("grade", "大二", "我不是sophomore，是junior", False),
    ("grade", "大三", "我不是sophomore，是junior", True),
    ("grade", "大一", "I am not a first-year undergraduate, I am a second-year undergraduate.", False),
    ("grade", "本科二年级", "I am not a first-year undergraduate, I am a second-year undergraduate.", True),
    ("grade", "本科二年级", "我不是本科生，I am a second-year graduate student.", False),
    ("grade", "本科二年级", "我本科二年，北大的", True),
    ("grade", "sophomore", "我现在是大二", True),
    ("grade", "phd", "我在读博士", True),
    ("grade", "大二", "我大二，作业错了三题。", True),                         # 作业错了不是改口
    ("grade", "大二", "我大二，代码写错了", True),
    ("interest:ai", "人工智能", "我不是对人工智能没有兴趣，只是暂时没空", True),  # 双重否定
    ("interest:ml", "ML", "我不喜欢MLOps，只喜欢ML算法", True),                 # MLOps 不是 ML
])
def test_memory_rules_round15(key, value, quote, ok):
    """第十五轮 Codex 复现的：长值绕过年级和否定检查、英文年级的否定和阶段、「本科二年」、英文规范值、
    「作业错了」当成改口、双重否定、缩写没有词界。"""
    affects = "task_difficulty" if key == "grade" else "direction_choice"
    acc, rej = memory.validate_ops(_uid(), [_op(key, value, quote, affects=affects)], [quote])
    assert (len(acc) == 1) is ok, rej


@pytest.mark.parametrize("value,quote,ok", [
    ("每周2小时", "我每周只能投入½小时", False),     # ½ 不拆成 1 和 2
    ("每周0.5小时", "我每周只能投入½小时", True),
    ("这学期写了1篇论文", "我这学期选了1门课", False),  # 门和篇不是一回事
])
def test_quantities_round15(value, quote, ok):
    assert memory._numbers_supported(value, quote) is ok


@pytest.mark.parametrize("value,quote,ok", [
    ("每周5.5小时", "每周可以投入1½小时", False),   # 带分数原来被拼成 11⁄2
    ("每周1.5小时", "每周可以投入1½小时", True),
    ("每周33小时", "只能投入⅓小时", False),         # 循环小数原来被拆成两个数
    ("每周0.5小时", "每周半小时", True),
    ("每周1.5小时", "一个半小时", True),
    ("完成2项科研项目", "完成2个科研项目", True),    # 「个」和别的量词互通
    ("学完3门课程", "学完3个课程", True),
])
def test_quantities_round16(value, quote, ok):
    assert memory._numbers_supported(value, quote) is ok


@pytest.mark.parametrize("value,quote,ok", [
    ("大二", "我大二，不对，我大三", False),             # 紧跟的「不对」「错了」是改口
    ("大二", "我大二，错了，是大三", False),
    ("大二", "我大二，作业错了三题。", True),            # 中间隔着别的词不算
    ("大四", "本科四年里想做一次科研，我现在大二", False),  # 学制时长不是年级
    ("大二", "本科四年里想做一次科研，我现在大二", True),
    ("博二", "I am a second-year PhD graduate student.", True),   # 博士优先
    ("研二", "I am a second-year PhD graduate student.", False),
    ("本科二年级", "I am not first-year, I am a sophomore.", True),  # 否定的那一处不吞后面的年级
])
def test_grade_phrasings_round16(value, quote, ok):
    acc, rej = memory.validate_ops(_uid(), [_op("grade", value, quote)], [quote])
    assert (len(acc) == 1) is ok, rej


@pytest.mark.parametrize("text", [
    "1" + " " * 20000 + "半小时",
    "半" + " " * 20000 + "x",
    "二" + " " * 20000 + "个x",
])
def test_half_hour_patterns_stay_linear(text):
    """「\\s*个?\\s*」两段空白连着写，一万六千个空格卡住事件循环两秒多（Codex 第十七轮复现）。"""
    import time
    t0 = time.perf_counter()
    memory._quantities(text)
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.parametrize("value,quote,ok", [
    ("GPA 3.7", "我的GPA是3.7/4.0", True),
    ("GPA 3.1", "我的GPA是3.7/4.0", False),   # 原来把「7/4」当分数算成 1.75
    ("GPA 3.75", "GPA 3.75/4.0", True),
    ("每周0.5小时", "每周1/2小时", True),
])
def test_gpa_scale_is_not_a_fraction(value, quote, ok):
    assert memory._numbers_supported(value, quote) is ok
