# -*- coding: utf-8 -*-
"""记忆写入闸门按字段核对：年级 {stage, year}、数量 {value, unit, per}、学校院系专业查别名表、兴趣目标要原话片段，都不能在否定/过去/改口里。不调真的模型。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dialogue  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "gate.db")
    store.init_db()


def _check(key, value, quote):
    op = {"op": "add", "key": key, "value": value, "evidence_quote": quote, "affects": "task_difficulty"}
    if key.startswith("constraint:"):
        op["valid_until"] = "2030-01-01"
    return memory.validate_ops(store.create_user("t")["uid"], [op], [quote])


@pytest.mark.parametrize("key,value,quote", [
    ("interest:ml", "机器学习", "我不想碰机器学习"),                       # 「不想碰」原来不在否定词表里（Codex 复现）
    ("school", "清华大学", "我在北京大学读书"),                            # 「大学」两个字对上就算撑住（Codex 复现）
    ("grade", "研二", "I am second-year, not a graduate student"),         # 没说阶段的 second-year 撑住了任何阶段（Codex 复现）
    ("base:code", "会", "我没学过Python"),                                 # 全是虚词的值原来算撑得住（Codex 复现）
    ("base:code", "会Python", "我没学过Python"),
    ("interest:ml", "机器学习", "我对机器学习一点兴趣都没有"),
    ("interest:cv", "CV", "我喜欢NLP，不喜欢CV"),
    ("interest:ml", "机器学习", "机器学习我不太感兴趣"),
    ("interest:ml", "机器学习", "我对深度学习感兴趣"),                     # 「学习」两个字对上不算
    ("interest:nlp", "NLP", "以前喜欢NLP，现在更想做CV"),
    ("goal:phd", "读博", "我不打算读博"),
    ("goal:abroad", "出国", "我没想过出国"),
    ("goal:work", "就业", "我不想工作，想读研"),
    ("grade", "研究生", "我想读研"),                                      # 打算的不是现在的年级
    ("grade", "博士", "我在读博士后"),
    ("grade", "大二", "我以前是大二，现在研一"),
    ("grade", "大一", "大一的时候学过C"),
    ("grade", "大三", "大三，哦打错了，我大二"),
    ("grade", "大二", "我最多同时做大2个项目"),
    ("school", "北京大学", "我在东北大学读书"),                            # 「北大」藏在「东北大学」里
    ("school", "北京大学", "我不是北大的"),
    ("school", "清华大学", "我想考清华的研究生"),
    ("school", "上海交通大学", "我在西安交大"),
    ("department", "信息科学技术学院", "我想转到信科"),
    ("department", "信息管理系", "我不是信管的，是数院的"),
    ("department", "数学科学学院", "我喜欢数学"),                          # 学科词不是院系
    ("department", "信息科学技术学院", "以前在信科，后来转到了信管"),
    ("major", "计算机科学与技术", "我是学经济学的"),
    ("base:math", "数学很好", "数学不太好"),
    ("current:course", "在修线性代数", "我没修过线性代数"),
    ("role:club", "参加了机器学习社团", "我没参加社团"),
    ("capability:python", "熟练使用Python", "我不会Python"),
    ("enroll_year", "2023", "我2024年入学"),                              # 原来按比例容差，2023 和 2024 差不到千分之一
])
def test_contradicted_or_unsupported_values_are_rejected(key, value, quote):
    acc, rej = _check(key, value, quote)
    assert acc == [], acc


@pytest.mark.parametrize("key,value,quote", [
    ("age", {"value": 19, "unit": "岁"}, "我有19门课"),
    ("age", {"value": 20, "unit": "岁"}, "我去年20岁"),
    ("age", {"value": 19, "unit": "岁"}, "我不是19岁，是20岁"),
    ("pace", {"value": 20, "unit": "小时", "per": "周"}, "我每周2小时，总共20周"),
    ("pace", {"value": 2, "unit": "小时", "per": "周"}, "每天2小时"),
    ("pace", {"value": 10, "unit": "小时", "per": "周"}, "以前每周10小时，现在每周只有2小时"),
    ("pace", {"value": 10, "unit": "小时", "per": "周"}, "我每周没有10小时那么多"),
    ("pace", {"value": 10, "unit": "小时", "per": "周"}, "每周10分钟"),
    ("pace", {"value": 5, "unit": "小时", "per": "周"}, "每周5小时，哦不对，每周3小时"),
    ("enroll_year", {"value": 2023, "unit": "年"}, "我2024年入学"),
    ("grade", {"stage": "硕士", "year": 2}, "I am second-year, not a graduate student"),
    ("grade", {"stage": "硕士", "year": 1}, "我是一年级本科生"),
    ("grade", {"stage": "本科", "year": 4}, "本科四年里想做一次科研"),
])
def test_structured_values_are_checked_field_by_field(key, value, quote):
    acc, rej = _check(key, value, quote)
    assert acc == [] and rej[0]["reason"] in ("number_not_in_evidence", "negated_in_evidence", "grade_not_in_evidence"), rej


@pytest.mark.parametrize("key,value,quote,shown", [
    ("grade", {"stage": "本科", "year": 2}, "我是二年级本科生", "大二"),
    ("grade", {"stage": "硕士", "year": 1}, "first-year master's student", "研一"),
    ("grade", {"stage": "本科", "year": 1}, "一年级，还在适应", "大一"),     # 没说阶段按本科
    ("grade", {"stage": "博士", "year": None}, "我是博士生", "博士"),
    ("age", {"value": 19, "unit": "岁"}, "我今年十九岁", "19岁"),           # 中文写的年龄原来认不出（Codex 复现）
    ("age", {"value": 20, "unit": "岁"}, "我20周岁", "20岁"),              # 「周岁」原来被当成「周」
    ("age", {"value": 20, "unit": "岁"}, "今年二十", "20岁"),
    ("age", {"value": 20, "unit": "岁"}, "虚岁二十", "20岁"),
    ("enroll_year", {"value": 2024, "unit": "级"}, "24级的", "2024"),
    ("enroll_year", {"value": 2025, "unit": "年"}, "二〇二五年入学", "2025"),
    ("enroll_year", {"value": 2024, "unit": "年"}, "我是2024入学的", "2024"),
    ("pace", {"value": 80, "unit": "分钟", "per": "天"}, "每天一小时二十分钟", "每天80分钟"),  # 复合时长原来拆成两个数（Codex 复现）
    ("pace", {"value": 100, "unit": "分钟", "per": "天"}, "每天1h40min", "每天100分钟"),
    ("pace", {"value": 1.5, "unit": "小时", "per": "天"}, "每天一个半小时", "每天1.5小时"),
    ("pace", {"value": [2, 3], "unit": "小时", "per": "周"}, "每周两三个小时", "每周2到3小时"),
    ("pace", {"value": 6, "unit": "hours", "per": "week"}, "a week maybe 6 hours", "每周6小时"),
    ("pace", {"value": 2, "unit": "小时", "per": "天"}, "每天两个钟头", "每天2小时"),
])
def test_structured_values_that_match_are_kept_in_a_canonical_form(key, value, quote, shown):
    acc, rej = _check(key, value, quote)
    assert rej == [] and acc[0]["value"] == shown and acc[0]["source"] == "declared", rej


@pytest.mark.parametrize("key,value,quote", [
    ("grade", "大二", "I'm a second-year student"),
    ("grade", "大三", "我是大三的，上次说大二是打错了"),
    ("grade", "大二", "我是大二学生，不是研究生"),
    ("school", "人大", "我是人大的学生"),
    ("school", "北京大学", "我是北大学生"),
    ("department", "信息管理系", "我是信管的"),
    ("department", "数学科学学院", "数学系的"),
    ("major", "古典文献学", "古典文献学专业大一"),
    ("interest:ml", "机器学习", "数学我不太行，但是对机器学习感兴趣"),
    ("interest:ml", "机器学习", "想做不需要太多数学的机器学习"),       # 否定管不到「的」后面的词
    ("interest:ml", "机器学习", "我不仅喜欢机器学习，还喜欢数学"),
    ("interest:cv", "CV", "除了NLP，我对CV也有兴趣"),
    ("interest:llm", "大模型", "对LLM很感兴趣"),
    ("interest:ml", "对机器学习感兴趣", "我对机器学习有兴趣"),         # 「对…感兴趣」是包装，按里面的词找
    ("goal:phd", "PhD", "I want to do a PhD"),
    ("base:code", "没学过编程", "我没学过编程"),                       # 值自己带着否定，和原话一致
    ("constraint:time", "这学期每周最多2小时", "这学期每周不能超过两小时"),
    ("project:app.traction", "日活200", "日活两百左右"),               # 没单位的「两百」也是数
])
def test_things_students_actually_say_are_kept(key, value, quote):
    acc, rej = _check(key, value, quote)
    assert len(acc) == 1, rej


def test_a_paraphrased_interest_needs_an_anchor_and_is_only_inferred():
    acc, _ = _check("interest:gnn", "图神经网络的研究", "我对图神经网络有兴趣")
    assert acc[0]["source"] == "inferred" and "partially_inferred" in acc[0]["notes"]
    acc, rej = _check("interest:x", "强化学习的应用", "我对深度学习感兴趣")
    assert acc == [] and rej[0]["reason"] == "short_value_not_in_evidence"


@pytest.mark.parametrize("key,value", [
    ("grade", {"stage": "本科", "year": 9}), ("grade", {"stage": "大专", "year": 1}), ("grade", {"stage": None, "year": None}),
    ("grade", {"stage": "本科", "year": True}), ("age", {"value": "很多", "unit": "岁"}), ("age", {"value": 19, "unit": "小时"}),
    ("age", {"value": 19, "unit": "岁", "note": "x"}), ("pace", {"value": 10, "unit": "小时", "per": "世纪"}),
    ("pace", {"value": float("inf"), "unit": "小时"}), ("interest:ml", {"value": "机器学习"}), ("school", {"name": "清华大学"}),
])
def test_a_structured_value_of_the_wrong_shape_rejects_only_that_op(key, value):
    uid = store.create_user("t")["uid"]
    good = {"op": "add", "key": "grade", "value": {"stage": "本科", "year": 2}, "evidence_quote": "我大二", "affects": "task_difficulty"}
    acc, rej = memory.validate_ops(uid, [{**good, "key": key, "value": value}, good], ["我大二"])
    assert [a["value"] for a in acc] == ["大二"] and [r["reason"] for r in rej] == ["bad_type:value"]


def test_the_proposal_prompt_asks_for_the_structured_shapes():
    """模型照提示写：提示里没说的形状，代码按字段核对时只能当作字符串去猜。"""
    prompt = dialogue._proposal_system()
    for shape in ('"stage"', '"year"', '"unit"', '"per"', "直接抄原话"):
        assert shape in prompt


@pytest.mark.parametrize("text", [
    "不" * 20000, "大二不" * 7000, "对没兴趣" * 5000, "不" + "的" * 20000, "打错" * 10000, "not " * 5000 + "sophomore",
    "对" * 20000 + "没兴趣", "百分之" * 7000, "我不是大二，以前在信科，想读博，每周2小时，打错了。" * 800,
])
def test_the_gate_stays_linear_on_long_messages(text):
    """每条范围都管到分句末尾：原来按提示词逐个往后找「的」、逐处比对所有范围，几万字的消息会卡住整轮。"""
    ops = [{"op": "add", "key": k, "value": v, "evidence_quote": text, "affects": "task_difficulty"} for k, v in (
        ("grade", {"stage": "本科", "year": 2}), ("pace", {"value": 2, "unit": "小时", "per": "周"}), ("school", "清华大学"),
        ("department", "信息管理系"), ("interest:ml", "机器学习"), ("base:code", "会Python"), ("current:x", "在修线性代数课程的作业"))]
    t0 = time.perf_counter()
    memory.validate_ops(store.create_user("t")["uid"], ops, [text])
    assert time.perf_counter() - t0 < 2.0


@pytest.mark.parametrize("key,value,quote,ok", [
    ("age", "19岁，计算机基础扎实，擅长深度学习", "我今年19岁", False),    # 只核了 19，整串按自述存下（Codex 复现）
    ("pace", "每周能投入约十小时", "每周能投入约十小时", True),
    ("direction:ai", "人工智能", "我不喜欢AI", False),                      # 否定检查没换缩写找（Codex 复现）
    ("goal:papers", "每周读5篇机器学习论文", "我想每周读2篇机器学习论文", False),  # 锚住了就不查数字（Codex 复现）
    ("major", "国际政治", "我是外交学专业的", False),                      # 一张卡上的两个专业被当成一个（Codex 复现）
    ("major", "外交学", "我是外交学专业的", True),
    ("enroll_year", "2025", "我去年2025年入学", True),                     # 入学年份本来就是过去的事（Codex 复现）
    ("interest:ml", "机器学习", "我对机器学习不是不感兴趣", True),          # 抵掉的否定又被后置否定认了一遍（Codex 复现）
    ("grade", "大二", "我是大二.", True),                                  # 句号被当成小数点（Codex 复现）
    ("grade", {"stage": "本科", "year": 2}, "我是大二.", True),
])
def test_review_round18(key, value, quote, ok):
    acc, rej = _check(key, value, quote)
    assert (len(acc) == 1) is ok, rej


def test_an_oversized_structured_number_rejects_only_that_op():
    """10**400 是合法 JSON：原来 float() 抛 OverflowError，整轮校验中断（Codex 复现）。"""
    acc, rej = _check("age", {"value": 10 ** 400, "unit": "岁"}, "我今年19岁")
    assert acc == [] and rej[0]["reason"] == "bad_type:value"


def test_an_age_range_keeps_both_ends():
    """{"value": [19, 20]} 两头都核对了，原来只存「19岁」（Codex 复现）。"""
    acc, _ = _check("age", {"value": [19, 20], "unit": "岁"}, "我19到20岁")
    assert acc[0]["value"] == "19到20岁"
    acc, rej = _check("enroll_year", {"value": [2023, 2024], "unit": "年"}, "2023到2024年入学")
    assert acc == [] and rej[0]["reason"] == "bad_type:value"


def _check_in(key, value, message, quote=None):
    """引文是整条消息里的一段：范围按整条消息算。"""
    op = {"op": "add", "key": key, "value": value, "evidence_quote": quote or message, "affects": "task_difficulty"}
    if key.startswith("constraint:"):
        op["valid_until"] = "2030-01-01"
    return memory.validate_ops(store.create_user("t")["uid"], [op], [message])


G = lambda s, y: {"stage": s, "year": y}  # noqa: E731
Q = lambda v, u, p=None: {"value": v, "unit": u, **({"per": p} if p else {})}  # noqa: E731


@pytest.mark.parametrize("key,value,message,quote", [
    # 引文只截了值那一段，否定、过去、主语都在外面（Codex 复现）
    ("grade", G("本科", 4), "我不是大四，是大二", "大四"),
    ("age", Q(22, "岁"), "不是22岁，我刚成年", "22岁"),
    ("interest:robotics", "机器人", "机器人我已经完全没兴趣了", "机器人"),
    ("goal:phd", "读博", "毕业读博是别人给的建议，不是我的目标", "毕业读博"),
    ("base:code", "会用 R", "我并不会用 R，只能照着同学代码跑", "会用 R"),
    # 说的是别人（Codex 复现）
    ("grade", G("本科", 2), "我室友大二，我是刚入学的新生", None),
    ("age", Q(21, "岁"), "我姐今年21岁，我还没成年", None),
    ("school", "清华大学", "我的高中同桌在清华，我们偶尔一起自习", None),
    ("department", "数学科学学院", "同宿舍那位是数院的，我经常问他数学题", None),
    ("goal:phd", "读博", "我妈妈希望我读博，这只是她的想法", None),
    ("current:course", "在上计量经济学", "我朋友在上计量经济学，经常吐槽作业", None),
    # 只是提到，不是学籍；附属机构；有别的意思的简称（Codex 复现）
    ("school", "清华大学", "我今天读了一本清华大学出版社的教材", None),
    ("school", "北京大学", "我弟在北大附中读高中，我的学籍还没说呢", None),
    ("department", "法学院", "今天去法院旁听了一个案子，挺有意思", None),
    ("major", "经济学", "我今天借了本经济学教材，专业还是英语", None),
    ("major", "化学", "我是化学生物学专业的，不是化学专业", None),
    ("enroll_year", Q(2021, "年"), "2021年我参加了高中数学竞赛", None),
    ("pace", Q(4, "小时", "周"), "我每周上四小时体育课，科研时间另算", None),
    # 打算、假设、过去（Codex 复现）
    ("pace", Q(9, "小时", "周"), "我打算以后每周拿出九小时，目前还做不到", None),
    ("interest:ml", "机器学习", "假如我喜欢机器学习，你会怎么安排路线？我只是举例", None),
    ("current:course", "在上概率论", "我上学期在上概率论，已经结课了", None),
    ("grade", G("本科", 2), "大二已经是过去的事了，我都快毕业了", None),
    ("interest:ai", "人工智能", "人工智能是我去年的兴趣，现在换了", None),
    # 否定的说法（Codex 复现）
    ("grade", G("本科", 3), "我并不是现在大三，我才刚入学", None),
    ("pace", Q(8, "小时", "周"), "我每周八小时真的拿不出来", None),
    ("interest:rl", "强化学习", "强化学习我没兴趣😂", None),
    ("goal:phd", "读博", "读博？我完全没这个打算", None),
    ("major", "金融学", "金融学并不是我的专业，我只是选过一门课", None),
    ("interest:systems", "操作系统", "我不喜欢数据库、网络，或者操作系统这几块", None),
    ("capability:sql", "不会 SQL", "我会 SQL，课程项目天天用", None),
    ("base:math", "数学很好", "数学不是很好，别高估我啊", None),
    ("constraint:exam", "这周没考试", "我这周考试很多，几乎天天有", None),
    # 改口隔着插话（Codex 复现）
    ("grade", G("本科", 2), "我大二。等下。刚说错了，我已经大三了", None),
    ("age", Q(19, "岁"), "我19岁。啊抱歉，刚才年龄说错了，我20岁", None),
    ("school", "清华大学", "清华大学。啊我口误，是北京大学", None),
    # 数量（Codex 复现）
    ("age", "2025", "我2025年入学北大，读的是本科", None),
    ("age", "20岁，已发表论文", "我今年20岁，本科就读", None),
    ("pace", "五周", "这个项目总共五周，是学期中间那段", None),
    ("pace", Q(2, "小时", "周"), "我总共就两小时能给这个一次性活动", None),
    ("pace", Q(19, "小时", "周"), "每周十几小时吧，没算过具体多少", None),
    ("pace", Q(6, "小时", "周"), "每周四到六小时，不一定能到上限", None),
    ("pace", Q(5, "小时", "周"), "我每周不到五小时，很难挤出完整的五小时", None),
    ("pace", Q(130, "分钟", "周"), "每周能投入两小时十分钟三十秒", None),
    ("pace", Q(2006, "分钟", "月"), "每月两千六分钟，也就是2600分钟左右", None),
    # 值里多出原话没有的东西（Codex 复现）
    ("base:code", "精通 Java", "我会一点 JavaScript，刚开始做网页", None),
    ("base:code", "编程能力非常扎实", "我刚开始学编程，这周还在找入门资料", None),
    ("current:project", "正在开发大型推荐系统", "我在看推荐系统入门教程，项目还没开工", None),
    ("interest:ml", "机器学习和量子化学", "我喜欢机器学习，对另一边的领域还没了解", None),
])
def test_review_round19_rejects(key, value, message, quote):
    acc, rej = _check_in(key, value, message, quote)
    assert acc == [], acc


@pytest.mark.parametrize("key,value,message", [
    ("age", Q(21, "岁"), "I am twenty-one years old, 本科在读。"),                 # 英文复合数（Codex 复现）
    ("enroll_year", Q(2023, "年"), "我二三年入学北大，到现在还在探索专业方向"),   # 「二三年入学」是 2023（Codex 复现）
    ("enroll_year", Q(2025, "年"), "两千零二十五年我来北大报到"),
    ("pace", Q(20, "分钟", "天"), "我每天能花三分之一小时读文献"),               # 分之（Codex 复现）
    ("pace", Q(1.5, "小时", "周"), "每周一点五小时是我的上限内可用时间"),         # 中文小数点（Codex 复现）
    ("pace", Q(130.5, "分钟", "周"), "每周能投入两小时十分钟三十秒，番茄钟算的"),  # 三段时长（Codex 复现）
    ("pace", Q(90, "分钟", "周"), "我每周一小时又半小时可以用"),
    ("pace", "每周5小时", "我课余固定每周5小时能做实验"),                         # 「课余」不是课
    ("department", "考古文博学院", "我属于考古文博学院，最近在整理出土数据"),
    ("base:math_code", "数学比编程熟一点", "数学比编程熟一点，代码常常调不出来"),  # 在册 key 规范化后查不到（Codex 复现）
    ("base:math", "学过高等数学", "我以前学过高等数学，知识记得一些"),            # 「学过」本来就是以前
    ("interest:quantum", "量子计算", "我不是不喜欢量子计算，就是找不到入门材料"),
])
def test_review_round19_accepts(key, value, message):
    acc, rej = _check_in(key, value, message)
    assert len(acc) == 1, rej


@pytest.mark.parametrize("n", [16000, 64000])
def test_corrections_and_repeated_quotes_stay_linear(n):
    """每个改口词都从句首重扫语气词：六万四千字要六秒（Codex 复现）；引文在长消息里重复出现也不逐处比。"""
    text = "哦" * (n // 2) + "说错" * (n // 4) + "我大二"
    t0 = time.perf_counter()
    _check_in("grade", G("本科", 2), text)
    nat = ("我是信管大二的，每周大概能投入10个小时，对机器学习感兴趣，不想碰前端，以前在物院，室友在清华。" * 2000)[:n]
    ops = [{"op": "add", "key": k, "value": v, "evidence_quote": nat[:200], "affects": "task_difficulty"} for k, v in (
        ("grade", G("本科", 2)), ("pace", Q(10, "小时", "周")), ("school", "清华大学"), ("department", "信息管理系"),
        ("interest:ml", "机器学习"), ("base:code", "会Python"), ("current:x", "在修线性代数课程的作业"), ("age", Q(19, "岁")))]
    memory.validate_ops(store.create_user("t")["uid"], ops, [nat])
    assert time.perf_counter() - t0 < 3.0


@pytest.mark.parametrize("key,value,message,ok", [
    ("interest:ml", "机器学习\n方向", "我对机器学习有兴趣", True),            # 值里带换行原来整轮抛异常（Codex 复现）
    ("base:code", "不会机器学习", "我不会ML", True),                         # 换过缩写的片段丢了值里的否定（Codex 复现）
    ("pace", Q(10, "小时", "周"), "我每周能在课题组投入10小时", True),        # 「课题组」不是课（Codex 复现）
    ("pace", Q(10, "小时", "周"), "每周有10小时做科研课题", True),
    ("grade", "研二", "我现在读研二", True),                                 # 「读」后面的研二原来认不出（Codex 复现）
    ("grade", G("硕士", 3), "在读研三", True),
    ("grade", G("硕士", 1), "读研一年就工作", False),
])
def test_review_round20(key, value, message, ok):
    acc, rej = _check_in(key, value, message)
    assert (len(acc) == 1) is ok, rej


def test_a_negated_value_against_a_long_quote_stays_linear():
    """值有否定、引文没有：原来按整段引文逐位重数提示词，一万六千字的引文要六秒（Codex 复现）。"""
    t0 = time.perf_counter()
    _check_in("base:math", "不会数学", "以前学过数学。" * 4000)
    assert time.perf_counter() - t0 < 2.0


@pytest.mark.parametrize("key,value,message,quote", [
    # 别人的事隔着逗号接着管；修饰别人的；替别人问（Codex 复现）
    ("pace", Q(4, "小时", "周"), "我室友说自己很闲，每周能抽四小时做小项目", "每周能抽四小时"),
    ("grade", "大四", "大四的学长说可以帮我看看开题表", "大四"),
    ("age", Q(23, "岁"), "23岁的师兄给我推荐了这本书", "23岁"),
    ("grade", G("本科", 4), "我刚才报大四是替同学问的，本人还在大一", "大四"),
    ("current:reading", "在读社会网络分析", "在读社会网络分析的是室友，我在写文学评论", "在读社会网络分析"),
    ("career:public", "准备考公务员", "准备考公务员是爸妈给我定的路，我自己还没选", "准备考公务员"),
    ("role:club", "社团负责人", "社团负责人把表格发给我，让我帮填个空", "社团负责人"),
    # 问句、条件后件、只是理想（Codex 复现）
    ("grade", G("本科", 2), "大二可以去听研究生的讨论课吗？我还是新生", "大二"),
    ("school", "复旦大学", "复旦大学能申请旁听吗？我想先了解一下", "复旦大学"),
    ("goal:abroad", "出国留学", "如果拿到奖学金，我才会考虑出国留学", "出国留学"),
    ("grade", "大三", "假设我转专业成功，那我大三就能把基础课补完", "大三"),
    ("pace", Q(5, "小时", "周"), "每周五小时只是理想状态，实际上我挤不出来", "每周五小时"),
    # 只是提到，不是学籍（Codex 复现）
    ("school", "清华大学", "我在清华大学附近租了个自习室，周末过去学习", "清华大学"),
    ("department", "物理学院", "我在物院门口等外卖，顺手看了一眼讲座海报", "物院"),
    ("department", "法学院", "我在法学院选过一门通选课，主修其实是化学", "法学院"),
    ("major", "经济学", "我正在读经济学教材，专业其实是日语", "经济学"),
    ("major", "金融学", "金融学是招聘岗位的专业要求，我自己学汉语言文学", "金融学"),
    ("field:quantum", "量子计算", "量子计算是今天讲座的题目，我路过拍了张照片", "量子计算"),
    ("current:project", "在开发大模型应用", "我在看开发大模型应用的教程，页面还没新建", None),
    # 数量：带单位的字符串也严格；入学年份要连着入学（Codex 复现）
    ("pace", "每周7小时", "每周最多七小时能搞科研，考试周会更少", "每周最多七小时"),
    ("pace", "每周5小时", "这个小实验总共五小时能结束，后面安排还没想", "总共五小时"),
    ("age", "19岁", "我这门课的序号是19，平台总让我填成年龄", "序号是19"),
    ("enroll_year", "2021年", "2021年我去燕园参观过，当时还在上高中", "2021年"),
    ("enroll_year", Q(2024, "年"), "我在2024年参加了新生入学志愿服务，当时已经大三", "2024年"),
    ("age", "20岁", "我生日蛋糕上误写了20岁，实际才19岁", "20岁"),
    # 改口隔着插话；后置否定；对不上的三字片段（Codex 复现）
    ("grade", "大二", "我大二。等会。嗯。刚那句是我室友的情况，我大三", "我大二"),
    ("interest:topology", "拓扑学", "拓扑学我谈不上有兴趣，能通过考试就行", "拓扑学"),
    ("field:robot", "机器人控制", "机器人控制我还没决定要不要碰，先了解学什么", "机器人控制"),
    ("base:math", "线性代数和实分析都学过", "线性代数学过；实分析还没学过", None),
    ("capability:math", "会证明收敛性", "我能看懂证明格式，收敛性那段完全不会", None),
])
def test_review_round21_rejects(key, value, message, quote):
    acc, rej = _check_in(key, value, message, quote)
    assert acc == [], acc


@pytest.mark.parametrize("key,value,message,quote", [
    # 他自己又当主语、一起做的不算别人（Codex 复现：原来这些都被当成别人的事拒掉）
    ("grade", G("本科", 3), "室友大二而我大三所以我们的课表不太一样", "大三"),
    ("school", "北京大学", "同学在北航而我在北大所以没法一起上课", "我在北大"),
    ("department", "物理学院", "我跟隔壁的朋友同属物院，常一起去做实验", "物院"),
    ("interest:ecology", "城市生态", "我和隔壁同学都喜欢城市生态，想从燕园植物做起", "城市生态"),
    ("goal:research", "做计量研究", "老师说我适合做计量研究后我就决定做计量研究了", "决定做计量研究了"),
    ("age", Q(19, "岁"), "我朋友刚满20；我的话是19周岁", "19周岁"),
    ("age", "19岁", "室友说十九岁就该早点试科研，我确实十九岁", "我确实十九岁"),
    # 回答式、问句里的自我介绍、表格式（Codex 复现）
    ("department", "信息管理系", "信管，平常课表里数据库比较多", "信管"),
    ("school", "北京大学", "北京大学，我就在这儿念书，最近经常去静园", None),
    ("major", "统计学", "统计学是我的本科专业，还在学概率论", None),
    ("major", "数据科学与大数据技术", "本科专业：数据科学与大数据技术，想试一下跨学科", None),
    ("department", "生命科学学院", "院系：生科。培养计划应该按这个找", "院系：生科"),
    ("grade", "大四", "我这个大四还能从读论文起步吗？", "大四"),
    ("school", "北京大学", "我这个PKU本科生适合参加什么科研入门活动？", "PKU"),
    ("major", "汉语言文学", "我和同学都是汉语言文学专业，想做语料的小课题", "汉语言文学"),
    # 数量和入学（Codex 复现）
    ("enroll_year", Q(2024, "年"), "24年进的北大，当时是在校本部报到", "24年进的北大"),
    ("enroll_year", "2024年", "想先说明一下：我2024年报到入学的", "我2024年报到入学的"),
    ("enroll_year", Q(2023, "年"), "学籍上写着入学年份2023", None),
    ("enroll_year", Q(2023, "年"), "入学那会是两千零二十三年，后来换过一次宿舍", None),
    ("pace", Q(8, "小时", "周"), "每周八小时做课题", "每周八小时"),
    ("base:math", "接触过群论", "我以前接触过群论，至少基本定义还有印象", "我以前接触过群论"),
    ("project:quiz.stage", "完成了测验小程序原型", "上周完成了测验小程序原型，今天开始找人试用", "完成了测验小程序原型"),
])
def test_review_round21_accepts(key, value, message, quote):
    acc, rej = _check_in(key, value, message, quote)
    assert len(acc) == 1, rej
