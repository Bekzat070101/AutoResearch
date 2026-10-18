# -*- coding: utf-8 -*-
"""记忆治理的离线测试：不联网、不调模型。

覆盖 docs/DIALOGUE_CONTRACT.md §3（registry）、§7（校验）、§4（召回）。
运行：pytest server/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False  # 测规则层：模型永远不可用

import memory  # noqa: E402
import store  # noqa: E402

POOL = [
    "我大二，想试试 AI，但 Python 只会抄",
    "本周只有十分钟",
    "我不是不喜欢 AI，只是这次不想写模型",
    # 下面几句给「值要撑得住」：短值也得在学生原话里有着落（Codex 第十一轮），夹具里的值要有对应的原话
    "我对机器学习有兴趣，也关心注意力",
    "其实只想了解经济学",
    "我还是大一",
    "跟着教程跑过几遍",
    "其实更想学数学",
    "我在做选课数据大作业",
]


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()


def make_user() -> str:
    return store.create_user("t")["uid"]


def ops(*items):
    return list(items)


def op(kind, key, value="", quote="", **kw):
    # affects 是写入的必填举证（这条记忆改变哪个决策）。绝大多数用例不关心它，
    # 这里给个合法默认值；闸门本身由 test_affects_* 专门覆盖。
    kw.setdefault("affects", "task_difficulty")
    return {"op": kind, "key": key, "value": value, "evidence_quote": quote, **kw}


# ---------- §3 registry ----------


def test_unknown_key_is_rejected():
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "随便写的key", "x", "我大二")), POOL)
    assert acc == []
    assert rej[0]["reason"] == "key_not_in_registry"


def test_experience_cannot_be_written_by_model():
    """经历类事实只能由提交事件写入，模型不能自己造。"""
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "experience:python", "写过 Python", "Python 只会抄")), POOL)
    assert acc == []
    assert rej[0]["reason"].startswith("behavior_only")


def test_constraint_requires_valid_until():
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "constraint:time", "本周十分钟", "本周只有十分钟")), POOL)
    assert acc == []
    assert rej[0]["reason"] == "constraint_requires_valid_until"

    acc, rej = memory.validate_ops(uid, ops(
        op("add", "constraint:time", "本周十分钟", "本周只有十分钟", valid_until="2026-10-05")), POOL)
    assert len(acc) == 1 and acc[0]["valid_until"] == "2026-10-05"


def test_namespaced_key_requires_valid_slug():
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "interest:Bad Slug!", "x", "我大二")), POOL)
    assert acc == [] and rej[0]["reason"] == "key_not_in_registry"


def test_slug_accepts_chinese_and_hyphen():
    """模型很自然会写 interest:machine-learning，也会写 current:选课数据大作业。
    只放行 [a-z0-9_] 会把真实信息整条丢掉——这个错犯过两次，第二次是中文。"""
    uid = make_user()
    pool = ["我对机器学习感兴趣", "我在做选课数据大作业"]
    acc, rej = memory.validate_ops(uid, ops(
        op("add", "interest:machine-learning", "机器学习", "我对机器学习感兴趣"),
        op("add", "current:选课数据大作业", "在做选课数据大作业", "我在做选课数据大作业"),
    ), pool)
    assert rej == [], rej
    assert sorted(a["key"] for a in acc) == ["current:选课数据大作业", "interest:machine-learning"]


def test_layer_survives_portrait_roundtrip():
    """切画像会把 live 库整体搬进快照再搬回来，新列不能在搬运里掉。"""
    uid = make_user()
    accepted, _ = memory.validate_ops(uid, ops(
        op("add", "grade", "大二", "我大二")), POOL)
    memory.apply_ops(uid, accepted, "")

    store.open_new_portrait(uid)
    store.activate_portrait(uid, store.list_portraits(uid)[0]["id"])

    facts = store.list_facts(uid, statuses=list(memory.DECISION_STATUSES))
    assert [f.key for f in facts] == ["grade"]
    assert facts[0].affects == "task_difficulty", "affects 在画像搬运中丢了"


# ---------- 闸门③：这条记忆改变哪个决策 ----------


def test_affects_is_required():
    """填不出「改变哪个决策」的记忆不许进画像（默认不写，要写就举证）。"""
    uid = make_user()
    raw = {"op": "add", "key": "grade", "value": "本科二年级", "evidence_quote": "我大二"}
    acc, rej = memory.validate_ops(uid, [raw], POOL)
    assert acc == []
    assert rej[0]["reason"].startswith("no_affects")


def test_affects_must_be_a_known_decision_point():
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "grade", "本科二年级", "我大二",
                                              affects="随便写的")), POOL)
    assert acc == [] and rej[0]["reason"].startswith("bad_affects")


def test_retract_does_not_need_affects():
    """retract/support 是对已有事实的操作，不要求重新举证。"""
    uid = make_user()
    acc, _ = memory.apply_ops(uid, memory.validate_ops(
        uid, ops(op("add", "interest:ml", "对机器学习感兴趣", "我对机器学习有兴趣")), POOL)[0], "")
    fid = acc[0]["id"]
    raw = {"op": "retract", "key": "interest:ml", "value": "",
           "evidence_quote": "不是不喜欢 AI", "target_fact_id": fid}
    accepted, rejected = memory.validate_ops(uid, [raw], POOL)
    assert len(accepted) == 1 and rejected == []


# ---------- 闸门②：引文得撑得住这个值 ----------


def test_value_exceeding_evidence_is_rejected():
    """复现真实事故：值里塞了三门课加成绩（来自别的消息），
    却挂一句不相干的引文当依据，旧校验照样放行——模型自己加的推断就这样进了画像。

    阈值是拿真实例子 calibrate 过的（审计与方案/calibrate_support.py）：
    这条 0.29，而正常的值在 0.67–1.00，分得开。
    """
    uid = make_user()
    quote = "也做过一些个人小项目，但都是Vibecoding的"
    acc, rej = memory.validate_ops(uid, ops(op(
        "add", "base:code",
        "修过计算概论B(Python)93、程序设计与算法(Python)92、Python数据分析89，"
        "课程基础扎实；做过个人小项目但均为Vibecoding（依赖AI生成）",
        quote)), [quote])
    assert acc == []
    assert rej[0]["reason"].startswith("value_exceeds_evidence")


def test_partially_supported_value_is_downgraded_not_dropped():
    """引文只撑住一部分 → 保留，但降级成 inferred，不再冒充「用户说的」。"""
    uid = make_user()
    quote = "我 Python 只会照着教程抄"
    acc, rej = memory.validate_ops(uid, ops(op(
        "add", "base:code", "Python 只会照着教程抄，未独立写过", quote)), [quote])
    assert rej == []
    assert acc[0]["source"] == "inferred"
    assert acc[0]["confidence"] == 0.4
    assert "partially_inferred" in acc[0]["notes"]


def test_short_value_skips_support_check():
    """短值是规范化缩写，不该按长值的标准误杀。"""
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "direction:ai", "AI", "想试试 AI")), POOL)
    assert rej == [] and acc[0]["value"] == "AI"


# ---------- 时间跨度分层 ----------


def test_layers_cover_every_registered_key():
    """每个在册 key 都必须有归属层，否则注入时会掉出去。"""
    for key in list(memory._EXACT) + ["goal:x", "interest:x", "direction:x", "style:x",
                                     "capability:x", "current:project", "constraint:x",
                                     "experience:x"]:
        assert memory.layer_of(key) in memory.LAYER_LABELS, key


def test_school_and_major_are_identity_practice_is_its_own_layer():
    assert memory.layer_of("school") == "identity"
    assert memory.layer_of("major") == "identity"
    assert memory.layer_of("current:project") == "practice"


def test_current_practice_gets_an_expiry_so_it_gets_reasked():
    """「当前实践」不填有效期时自动带 TTL：过期后会被重新问，
    而不是拿半年前的项目当「他正在做的事」。"""
    uid = make_user()
    quote = "我在做课程大作业的数据分析"
    acc, rej = memory.validate_ops(uid, ops(op(
        "add", "current:project", "在做课程大作业的数据分析", quote)), [quote])
    assert rej == []
    assert acc[0]["valid_until"], "当前实践应该自动带有效期"


def test_recall_gives_every_layer_a_slot():
    """分层预算：一门课的成绩不该挤掉他的方向。"""
    uid = make_user()
    pool = ["想试试 AI", "我大二", "我在做数据课的大作业"]
    accepted, rej = memory.validate_ops(uid, ops(
        op("add", "direction:ai", "AI", "想试试 AI"),
        op("add", "grade", "大二", "我大二"),
        op("add", "current:project", "在做数据课的大作业", "我在做数据课的大作业"),
    ), pool)
    assert rej == [], rej
    memory.apply_ops(uid, accepted, "")

    grouped, _ = memory.recall_grouped(uid, "随便问一句")
    assert [f.key for f in grouped["identity"]] == ["grade"]
    assert [f.key for f in grouped["interest"]] == ["direction:ai"]
    assert [f.key for f in grouped["practice"]] == ["current:project"]


def test_active_facts_excludes_expired():
    uid = make_user()
    accepted, _ = memory.validate_ops(uid, ops(
        op("add", "constraint:time", "本周只有十分钟", "本周只有十分钟",
           valid_until="2020-01-01")), POOL)
    memory.apply_ops(uid, accepted, "")
    assert memory.active_facts(uid) == []


# ---------- 分层面板 / 提问阶梯 ----------


def test_board_puts_hint_on_the_layer_not_on_each_fact():
    """layer_hint 描述的是「这一层」。挂在每条 fact 上，前端读 L.layer_hint 会取不到
    ——这个 bug 是靠真的把渲染路径跑一遍才发现的，grep 关键字查不出来。"""
    uid = make_user()
    accepted, _ = memory.validate_ops(uid, ops(op("add", "grade", "大二", "我大二")), POOL)
    memory.apply_ops(uid, accepted, "")

    b = memory.board(uid)
    assert len(b["layers"]) == len(memory.LAYERS), "空层也要出现，否则看不出这层还空着"
    for L in b["layers"]:
        assert L["layer_hint"], f"{L['layer']} 缺说明文案"
    ident = next(L for L in b["layers"] if L["layer"] == "identity")
    assert [f["key"] for f in ident["facts"]] == ["grade"]
    assert ident["facts"][0]["layer_label"] == "身份"


def test_board_reports_expired_separately():
    """过期的不是删掉，是挪到 expired 里让用户看得见——他可能想重新填。"""
    uid = make_user()
    accepted, _ = memory.validate_ops(uid, ops(
        op("add", "constraint:time", "本周只有十分钟", "本周只有十分钟",
           valid_until="2020-01-01")), POOL)
    memory.apply_ops(uid, accepted, "")

    b = memory.board(uid)
    assert b["expired_count"] == 1
    assert [L for L in b["layers"] if L["facts"]] == []


def test_user_edit_upgrades_source_and_keeps_a_revision():
    """用户亲手改的必须和「模型推断的」在库里长得不一样，否则没法追溯是谁改的。"""
    uid = make_user()
    accepted, _ = memory.validate_ops(uid, ops(op("add", "grade", "大二", "我大二")), POOL)
    fid = memory.apply_ops(uid, accepted, "")[0][0]["id"]

    updated = memory.user_edit(uid, fid, "大二（元培）")
    assert updated["value"] == "大二（元培）"
    assert updated["source"] == "user_edit"
    assert updated["confidence"] == 1.0
    # 历史是完整的：先 add 再 replace，不是只剩最后一条
    history = [r["operation"] for r in store.list_revisions(uid, fact_id=fid)]
    assert history == ["add", "replace"], history
    assert updated["evidence"][-1]["type"] == "user_edit"


def test_user_edit_rejects_someone_elses_fact():
    uid, other = make_user(), make_user()
    accepted, _ = memory.validate_ops(other, ops(op("add", "grade", "大二", "我大二")), POOL)
    fid = memory.apply_ops(other, accepted, "")[0][0]["id"]
    assert memory.user_edit(uid, fid, "大三") is None


def test_user_retract_is_soft_and_leaves_history():
    uid = make_user()
    accepted, _ = memory.validate_ops(uid, ops(op("add", "grade", "大二", "我大二")), POOL)
    fid = memory.apply_ops(uid, accepted, "")[0][0]["id"]

    assert memory.user_retract(uid, fid) is True
    assert store.get_fact(fid).status == "retracted", "软删，不是硬删"
    assert memory.active_facts(uid) == []
    assert memory.user_retract(uid, "不存在") is False


def test_ladder_reports_earliest_gap():
    """阶梯要指出最早空的那一层——用户实测「只说了大二就先问课程」就是跳级。"""
    uid = make_user()
    assert memory.ladder_state(uid)["next_layer"] == "identity"

    accepted, _ = memory.validate_ops(uid, ops(op("add", "grade", "大二", "我大二")), POOL)
    memory.apply_ops(uid, accepted, "")
    st = memory.ladder_state(uid)
    assert st["next_layer"] == "interest", "身份有数了就该问倾向"
    assert next(L for L in st["layers"] if L["layer"] == "identity")["known"] is True


def test_ladder_counts_every_layer():
    uid = make_user()
    st = memory.ladder_state(uid)
    assert [L["layer"] for L in st["layers"]] == [
        n for n, _ in memory.LAYERS if memory.LADDER_PROBE.get(n)]


def test_a_fat_layer_cannot_crowd_out_a_thin_one():
    """这是「替代平表 top-8」的核心断言：原来全局抢坑位，能力层塞满 8 条之后
    「他的方向」会被整个挤出上下文，而且没有任何信号说明被截断了。
    改成按层给预算后，每层有保底名额。"""
    uid = make_user()
    caps = [("capability:skill%d" % i, f"技能{i}", f"我学过技能{i}") for i in range(10)]
    pool = [q for _, _, q in caps] + ["想试试 AI"]
    accepted, rej = memory.validate_ops(uid, ops(
        *[op("add", k, v, q) for k, v, q in caps],
        op("add", "direction:ai", "AI", "想试试 AI"),
    ), pool)
    assert rej == [], rej
    memory.apply_ops(uid, accepted, "")

    grouped, _ = memory.recall_grouped(uid, "")
    assert len(grouped["capability"]) == memory.LAYER_BUDGET["capability"], \
        "能力层要被预算封住，不能无限占位"
    assert len(grouped["capability"]) < 10, "证明预算真的在起作用"
    assert [f.key for f in grouped["interest"]] == ["direction:ai"], \
        "胖层不能把方向挤出去"

    # 旧的平表接口拉平后总条数 = 各层预算之和，不再是一个拍脑袋的 limit
    flat, _ = memory.recall(uid, "", limit=0)
    assert len(flat) == sum(len(v) for v in grouped.values())


# ---------- 信息分类（用户给的九大类，落地成固定字段）----------


def test_fixed_slots_of_each_category_are_all_recognised():
    """用户列出的固定字段必须每条都能存下来——认不出就等于没分类。"""
    uid = make_user()
    slots = [
        # 身份
        ("grade", "大二"), ("school", "示例大学"), ("major", "信息管理系"),
        ("enroll_year", "2024"), ("gender", "女"), ("age", "19"), ("location", "北京"),
        # 学籍（双专业/辅修）
        ("enroll:minor", "经济学辅修"),
        # 在修课程 / 当前实践
        ("current:course", "在修机器学习导论"),
        ("current:project", "在做选课数据大作业"),
        ("current:audit-course", "旁听认知科学导论"),
        ("research_group", "在张老师组里做数据标注"),
        # 能力
        ("base:code", "Python 只会照着教程抄"),
        ("capability:python", "会写简单脚本"),
        # 已修课程 → 能力结论（成绩单本身不进 facts，见下方说明）
        ("capability:calculus", "修过高等数学，成绩 88"),
        # 进修方向
        ("field:embodied-ai", "想细分到具身智能"),
        ("direction:ai", "AI"),
        # 就业方向
        ("career:industry", "想去工业界做算法"),
        ("career:location", "希望留在北京"),
        # 人际交往
        ("role:club", "在数据科学社团"),
        # 节奏
        ("pace", "每周能投入约十小时"),
    ]
    for key, value in slots:
        assert memory.spec_for(key) is not None, f"{key} 认不出"
        # 引文至少 2 字（防编造的底线），所以补一句完整的原话当依据
        quote = f"我这边的情况是：{value}"
        acc, rej = memory.validate_ops(
            uid, ops(op("add", key, value, quote)), [quote])
        assert acc, f"{key} 存不下：{rej}"
        memory.apply_ops(uid, acc, "")

    stored = {f.key for f in memory.active_facts(uid)}
    assert {k for k, _ in slots} <= stored


def test_roles_are_self_reported_but_experience_still_needs_behavior():
    """社团/学工是「担任什么」，自述即可；
    做出过什么（experience）仍然只能由提交事件写入，不能被自述稀释。"""
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(
        op("add", "role:club", "在数据科学社团", "我在数据科学社团")),
        ["我在数据科学社团"])
    assert rej == [] and len(acc) == 1

    acc, rej = memory.validate_ops(uid, ops(
        op("add", "experience:python", "写过 Python 项目", "我在数据科学社团")),
        ["我在数据科学社团"])
    assert acc == [] and rej[0]["reason"].startswith("behavior_only")


def test_unsolicited_fields_do_not_block_the_ladder():
    """gender/age 是「识别到就存，但不主动问」。
    它们不能让阶梯一直卡在身份层等一个我们根本不该问的字段。"""
    uid = make_user()
    assert memory.spec_for("gender").ask is False
    assert memory.spec_for("age").ask is False
    assert memory.spec_for("grade").ask is True

    # 只存了性别和年龄，身份层仍然算「没数」——因为没有年级/学校/专业
    for key, val, q in (("gender", "女", "我是女生"), ("age", "19", "我今年19岁")):
        acc, _ = memory.validate_ops(uid, ops(op("add", key, val, q)), [q])
        assert acc, key
        memory.apply_ops(uid, acc, "")
    st = memory.ladder_state(uid)
    ident = next(L for L in st["layers"] if L["layer"] == "identity")
    assert ident["known"] is False, "不该靠性别/年龄认为身份层已经有数"
    assert st["next_layer"] == "identity"
    # 但仍然记下来了，并且如实标注「有，但不是靠问拿到的」
    assert set(ident["stored_only"]) == {"gender", "age"}


def test_unsolicited_fields_still_render_in_the_panel():
    """不主动问不等于不展示——用户说了就该看得见、改得掉。"""
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(op("add", "gender", "女", "我是女生")), ["我是女生"])
    memory.apply_ops(uid, acc, "")
    other = next(L for L in memory.board(uid)["layers"] if L["layer"] == "other")
    assert [f["key"] for f in other["facts"]] == ["gender"]


def test_slug_allows_hyphen_and_dot():
    """模型很自然会写 interest:machine-learning，这类 key 必须放行——
    否则丢的是用户真实说的话，不是垃圾数据。"""
    uid = make_user()
    # 能力的引文得是能力：「想试试 AI」撑不住 capability「AI」（结构化闸门之后），这里换成原话里真说过的
    for key, value, quote in (("interest:machine-learning", "AI", "想试试 AI"), ("goal:phd.app", "AI", "想试试 AI"),
                              ("capability:python_3", "Python 只会抄", "Python 只会抄")):
        acc, rej = memory.validate_ops(uid, ops(op("add", key, value, quote)), POOL)
        assert len(acc) == 1, f"{key} 应被接受，实际 {rej}"
    # 中文 slug 要放行：模型写 current:选课数据大作业 是很自然的，
    # 只放行 [a-z0-9_] 会把真实信息整条丢掉。
    acc, rej = memory.validate_ops(uid, ops(op("add", "current:选课数据大作业", "在做选课数据大作业",
                                               "我在做选课数据大作业")), POOL)  # 原来拿「我大二」当证据，只因为都有个「大」
    assert len(acc) == 1, f"中文 slug 应被接受，实际 {rej}"
    # 仍然挡住标点、超长、以及规范化后为空的
    for bad in ("interest:bad!", "interest:" + "x" * 41, "interest:___"):
        acc, _ = memory.validate_ops(uid, ops(op("add", bad, "x", "我大二")), POOL)
        assert acc == [], f"{bad} 不该被接受"
    # 首尾分隔符不算错：规范化会把它削掉，而不是把整条信息拒掉
    acc, _ = memory.validate_ops(uid, ops(op("add", "interest:-leading", "AI", "想试试 AI")), POOL)
    assert [a["key"] for a in acc] == ["interest:leading"]


def test_key_is_canonicalised_so_one_thing_lands_on_one_key():
    """空格/下划线统一成连字符、首尾干净，避免同一个东西长出好几个 key。"""
    assert memory.canon_key("current:选课 数据") == "current:选课-数据"
    assert memory.canon_key("capability:python_3") == "capability:python-3"
    assert memory.canon_key("  interest:ml  ") == "interest:ml"
    # 规范化是幂等的：写进去再读出来还是同一个 key
    assert memory.canon_key(memory.canon_key("goal:big  data_")) == "goal:big-data"


# ---------- §7 证据校验 ----------


def test_evidence_must_be_verbatim_from_this_profile():
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(op("add", "interest:x", "编的", "这句话原话里没有")), POOL)
    assert acc == [] and rej[0]["reason"] == "evidence_not_found"


def test_model_cannot_claim_behavior_source():
    """模型自称 behavior 会被降级为 declared，confidence 随之下降。"""
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(
        op("add", "capability:python", "跟着教程跑过", "跟着教程跑过几遍", source="behavior")), POOL)
    assert len(acc) == 1
    assert acc[0]["source"] == "declared"
    assert acc[0]["confidence"] == pytest.approx(0.6)


def test_mastery_claim_without_behavior_is_flagged():
    """自述「已掌握」但没有行为证据：保留但标注，不得据此升级难度。"""
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(
        op("add", "capability:python", "已掌握 Python，能独立写", "Python 只会抄")), POOL)
    assert len(acc) == 1
    assert "needs_behavior_evidence" in acc[0]["notes"]


def test_category_comes_from_registry_not_model():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(
        {**op("add", "interest:x", "关心注意力", "也关心注意力"), "category": "background"}), POOL)
    assert acc[0]["category"] == "interest"


# ---------- 提交 ----------


def test_apply_writes_fact_and_revision():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(op("add", "grade", "本科二年级", "我大二")), POOL)
    added, _ = memory.apply_ops(uid, acc, "d1")
    assert added[0]["status"] == "confirmed"
    revs = store.list_revisions(uid, added[0]["id"])
    assert [r["operation"] for r in revs] == ["add"]


def test_direction_is_exclusive():
    """方向互斥：选新方向后旧方向自动失效，M3 的分歧从根上消失。"""
    uid = make_user()
    # 值要有原话撑着：原来 direction:math 拿「想试试 AI」当证据，只因为 math 和 AI 都有字母 a
    for code, value, quote in (("ai", "AI", "想试试 AI"), ("math", "数学", "其实更想学数学")):
        acc, _ = memory.validate_ops(uid, ops(op("add", f"direction:{code}", value, quote)), POOL)
        memory.apply_ops(uid, acc, "d1")

    all_dirs = {f.key: f.status for f in store.list_facts(uid) if f.key.startswith("direction:")}
    assert all_dirs == {"direction:ai": "superseded", "direction:math": "confirmed"}
    # 只有生效的那个参与决策
    active = [f.key for f in memory.active_facts(uid) if f.key.startswith("direction:")]
    assert active == ["direction:math"]


def test_single_cardinality_key_supersedes_old_value():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(op("add", "grade", "大一", "我还是大一")), POOL)
    memory.apply_ops(uid, acc, "d1")
    acc, _ = memory.validate_ops(uid, ops(op("add", "grade", "大二", "我大二")), POOL)
    a2, changed = memory.apply_ops(uid, acc, "d2")
    assert changed[0]["status"] == "superseded"
    assert [f.value for f in memory.active_facts(uid) if f.key == "grade"] == ["大二"]


def test_replace_and_retract_keep_history():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(op("add", "interest:ml", "对机器学习有兴趣", "我对机器学习有兴趣")), POOL)
    added, _ = memory.apply_ops(uid, acc, "d1")
    fid = added[0]["id"]

    acc, _ = memory.validate_ops(uid, ops(
        op("replace", "interest:ml", "其实只想了解经济学", "其实只想了解经济学", target_fact_id=fid)), POOL)
    _, changed = memory.apply_ops(uid, acc, "d2")
    assert changed[0]["old_value"] == "对机器学习有兴趣"
    assert store.get_fact(fid).value == "其实只想了解经济学"

    acc, _ = memory.validate_ops(uid, ops(
        op("retract", "interest:ml", "", "我不是不喜欢 AI", target_fact_id=fid)), POOL)
    memory.apply_ops(uid, acc, "d3")
    assert store.get_fact(fid).status == "superseded"
    assert store.get_fact(fid) not in memory.active_facts(uid)
    ops_seen = [r["operation"] for r in store.list_revisions(uid, fid)]
    assert ops_seen == ["add", "replace", "retract"]


def test_replace_target_must_belong_to_profile():
    uid = make_user()
    acc, rej = memory.validate_ops(uid, ops(
        op("replace", "interest:ml", "x", "想试试 AI", target_fact_id="not-a-real-id")), POOL)
    assert acc == [] and rej[0]["reason"] == "target_fact_not_found"


def test_duplicate_submission_does_not_duplicate_facts():
    """同一轮重试不应把同一条记忆写两遍（幂等由 key 基数保证）。"""
    uid = make_user()
    o = ops(op("add", "interest:attention", "关心注意力", "也关心注意力"))
    for _ in range(3):
        acc, _ = memory.validate_ops(uid, o, POOL)
        memory.apply_ops(uid, acc, "d1")
    live = [f for f in memory.active_facts(uid) if f.key == "interest:attention"]
    assert len(live) == 1


# ---------- §4 召回 ----------


def test_expired_constraint_leaves_recall():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(
        op("add", "constraint:time", "本周十分钟", "本周只有十分钟", valid_until="2020-01-01")), POOL)
    added, _ = memory.apply_ops(uid, acc, "d1")
    assert memory.is_expired(store.get_fact(added[0]["id"]))
    recalled, expired = memory.recall(uid, "这周想做点什么")
    assert added[0]["id"] not in [f.id for f in recalled]
    assert added[0]["id"] in expired


def test_direction_is_always_recalled_even_without_keyword_hit():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, ops(op("add", "direction:ai", "AI", "想试试 AI")), POOL)
    memory.apply_ops(uid, acc, "d1")
    recalled, _ = memory.recall(uid, "完全无关的一句话")
    assert [f.key for f in recalled] == ["direction:ai"]


# ---------- 决策日志 ----------


def test_decision_log_records_rejected_ops_and_version():
    uid = make_user()
    store.bump_memory_version(uid)
    did = store.add_decision(uid, conversation_id="c1", memory_version=store.get_memory_version(uid),
                             move="clarify", reason="信息不足", rationale_refs=["f1"],
                             tool_calls=[], rejected_ops=[{"reason": "evidence_not_found"}])
    d = store.list_decisions(uid)[0]
    assert d["id"] == did and d["memory_version"] == 1
    assert d["rejected_ops"][0]["reason"] == "evidence_not_found"
    assert d["degraded"] is False
