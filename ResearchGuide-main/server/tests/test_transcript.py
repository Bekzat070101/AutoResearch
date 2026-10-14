# -*- coding: utf-8 -*-
"""成绩单解析 + 绩点 + 成绩单落库 + 字段覆盖度。

解析规则和公式来自 PKUMuZi/pku-gpa-calculator（MIT）；
这个文件里的样例文本是**照抄它自己的示例成绩单**，不是我手编的。
另外 审计与方案/verify_transcript_my.py 会拿上游 JS 的真实输出逐条对，
那个脚本保证「和参考实现一致」，这里保证「以后改动不会悄悄改坏」。

运行：pytest server/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False

import memory  # noqa: E402
import store  # noqa: E402
import transcript  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()


def make_user() -> str:
    return store.create_user("t")["uid"]


# 真实粘贴格式：学分 / 「学分」 / 课程名 / (课程性质)* / 成绩
SAMPLE = """25-26学年度1学期
4
学分
线性代数 (B)
专业必修
88
3
学分
机器学习导论
任选
95.5
1
学分
羽毛球
全校必修
W
24-25学年度2学期
3
学分
宏观经济学
专业必修
87
2
学分
英语听说
全校必修
96
1
学分
通识讲座
全校任选
合格
24-25学年度1学期
3
学分
线性代数 (B)
专业必修
缓考
"""


# ---------- 解析 ----------


def test_parses_the_real_paste_format():
    r = transcript.parse_transcript(SAMPLE)
    c = r["courses"]
    assert len(c) == 7, c
    assert r["warnings"] == [], r["warnings"]
    assert c[0] == {"term": "25-26学年度1学期", "course": "线性代数 (B)",
                    "credits": 4.0, "kind": "专业必修", "grade": "88",
                    "status": "completed"}


def test_semester_header_carries_down_to_later_courses():
    """学期标题只出现一次，下面所有课都归它——这是格式的关键，弄错就全串学期。"""
    r = transcript.parse_transcript(SAMPLE)
    got = {(c["course"], c["term"]) for c in r["courses"]}
    assert ("机器学习导论", "25-26学年度1学期") in got
    assert ("英语听说", "24-25学年度2学期") in got
    # 同名课在两个学期各出现一次，各自带自己的学期和成绩（重修/缓考很常见）。
    # 「缓考」在解析时归一成 I（和参考实现的 normalizeScore 一致）。
    la = [(c["term"], c["grade"]) for c in r["courses"] if c["course"] == "线性代数 (B)"]
    assert la == [("25-26学年度1学期", "88"), ("24-25学年度1学期", "I")]


def test_multiple_kind_lines_take_the_last():
    r = transcript.parse_transcript(
        "25-26学年度1学期\n3\n学分\n某课\n专业必修\n全校必修\n90")
    assert r["courses"][0]["kind"] == "全校必修"


def test_kind_is_optional():
    r = transcript.parse_transcript("25-26学年度1学期\n3\n学分\n某课\n90")
    assert r["courses"][0]["kind"] == ""
    assert r["courses"][0]["grade"] == "90"


def test_noise_lines_are_ignored():
    r = transcript.parse_transcript(
        "刷新\n总学分\n25-26学年度1学期\n3\n学分\n某课\n专业必修\n90\n3.700\n")
    assert len(r["courses"]) == 1
    assert r["courses"][0]["course"] == "某课"


def test_terms_are_sorted_chronologically_not_lexically():
    """字面排序会把「09-10学年度」排到「25-26学年度」后面。"""
    r = transcript.parse_transcript(SAMPLE)
    assert r["terms"] == ["24-25学年度1学期", "24-25学年度2学期", "25-26学年度1学期"]
    # 补零的早年学期要排在近年之前
    assert transcript.term_sort_key("09-10学年度1学期") < \
        transcript.term_sort_key("25-26学年度1学期")
    # 同一学年内，学期号升序
    assert transcript.term_sort_key("24-25学年度1学期") < \
        transcript.term_sort_key("24-25学年度2学期")


def test_unparsable_input_says_why_instead_of_guessing():
    r = transcript.parse_transcript("随便一段话")
    assert r["courses"] == []
    assert any("学期标题" in w for w in r["warnings"])
    assert any("没识别到课程" in w for w in r["warnings"])


def test_course_before_any_semester_is_reported_not_silently_kept():
    r = transcript.parse_transcript("3\n学分\n某课\n专业必修\n90")
    assert r["courses"] == []
    assert any("学期标题之前" in w for w in r["warnings"])


def test_unknown_lines_are_counted_in_warnings():
    r = transcript.parse_transcript(
        "25-26学年度1学期\n3\n学分\n某课\n专业必修\n90\n这行是什么鬼")
    assert len(r["courses"]) == 1
    assert any("没看懂" in w for w in r["warnings"])


# ---------- 绩点 ----------


@pytest.mark.parametrize("score,expect", [
    (100, 4.0), (90, 3.8125), (88, 3.73), (60, 1.0), (0, 0.0), (59, 0.0),
])
def test_gpa_formula(score, expect):
    assert transcript.gpa_of(str(score)) == pytest.approx(expect)


def test_failing_score_does_not_produce_negative_gpa():
    """参考实现不设下限，50 分会算出 -0.6875。负绩点没有意义，我们截到 0。
    这是**有意**和上游不同的一处，改回去只需把 GPA_FLOOR_AT_ZERO 设成 False。"""
    assert transcript.gpa_of("50") == 0.0
    assert transcript.gpa_of("59.9") == 0.0
    assert transcript.GPA_FLOOR_AT_ZERO is True


def test_non_numeric_grades_are_not_graded():
    for g in ("W", "I", "合格", "不合格", "缓考", "退课"):
        assert transcript.gpa_of(g) is None, g


def test_only_numeric_grades_count_toward_gpa_denominator():
    """「合格」算通过学分但不进 GPA 分母——当成 60 分会把绩点算低。"""
    courses = [
        {"course": "A", "grade": "90", "credits": 3},
        {"course": "B", "grade": "合格", "credits": 2},
        {"course": "C", "grade": "W", "credits": 1},
    ]
    s = transcript.summarize(courses)
    assert s["passed_credits"] == 5.0        # A + B
    assert s["gpa_credits"] == 3.0           # 只有 A
    assert s["gpa"] == pytest.approx(3.8125)
    assert s["gpa_courses"] == 1


def test_summary_is_credit_weighted():
    courses = [
        {"course": "A", "grade": "100", "credits": 1},
        {"course": "B", "grade": "60", "credits": 3},
    ]
    s = transcript.summarize(courses)
    assert s["gpa"] == pytest.approx((1 * 4.0 + 3 * 1.0) / 4)


def test_empty_transcript_summarises_to_none_not_zero():
    """没有可算的课要给 None，不能给 0——0 分和「没有成绩」是两件事。"""
    s = transcript.summarize([])
    assert s["gpa"] is None and s["avg_score"] is None
    assert s["gpa_credits"] == 0


def test_grade_normalisation():
    for raw, want in [("88.0", "88"), ("95.5", "95.5"), ("w", "W"),
                      ("退课", "W"), ("缓考", "I"), ("P", "合格"), ("不合格", "不合格")]:
        assert transcript.normalize_grade(raw) == want, raw


# ---------- 落库 ----------


def test_commit_then_read_back():
    uid = make_user()
    n = store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    assert n == 7
    rows = store.list_enrollments(uid)
    assert len(rows) == 7
    assert transcript.summarize(rows)["gpa"] is not None


def test_replace_is_a_snapshot_not_an_append():
    """重贴一份成绩单要以新为准。逐条追加会让重复粘贴的课把绩点算重。"""
    uid = make_user()
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    assert len(store.list_enrollments(uid)) == 7


def test_append_skips_exact_duplicates():
    uid = make_user()
    store.add_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    again = store.add_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    assert again == 0
    assert len(store.list_enrollments(uid)) == 7


def test_delete_enrollment_is_scoped_to_the_owner():
    uid = make_user()
    other = make_user()
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    eid = store.list_enrollments(uid)[0]["id"]
    assert store.delete_enrollment(other, eid) is False, "不能删别人的课"
    assert store.delete_enrollment(uid, eid) is True


def test_enrollments_survive_a_portrait_roundtrip():
    """画像切换是整块 dump/restore，新表最容易在这里被静默丢掉
    （affects 那一列就丢过一次）。"""
    uid = make_user()
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    store.open_new_portrait(uid)
    assert store.list_enrollments(uid) == [], "新画像不该看到旧画像的成绩单"
    store.activate_portrait(uid, store.list_portraits(uid)[-1]["id"])
    store.open_new_portrait(uid)
    store.activate_portrait(uid, store.list_portraits(uid)[0]["id"])
    assert len(store.list_enrollments(uid)) == 7, "切回来不能丢"


# ---------- 字段覆盖度（核对页要用）----------


def test_coverage_lists_every_group_with_empty_slots():
    uid = make_user()
    cov = memory.coverage(uid)
    labels = [g["label"] for g in cov["groups"]]
    assert labels[0] == "身份"
    assert "已修课程" in labels and "人际交往" in labels
    assert [s["key"] for g in cov["groups"] for s in g["slots"] if s["filled"]] == []
    assert cov["filled"] == 0 and cov["missing"] == cov["total"] and cov["total"] > 1
    assert all(s["why"] for g in cov["groups"] for s in g["slots"]), "每个空格都要说清填了有什么用"


def test_coverage_marks_filled_slots_with_their_value():
    uid = make_user()
    acc, _ = memory.validate_ops(uid, [{
        "op": "add", "key": "grade", "value": "大二",
        "evidence_quote": "我大二", "affects": "task_difficulty"}], ["我大二"])
    memory.apply_ops(uid, acc, "")
    cov = memory.coverage(uid)
    ident = cov["groups"][0]
    slot = next(s for s in ident["slots"] if s["key"] == "grade")
    assert slot["filled"] is True and slot["value"] == "大二"
    assert cov["filled"] == 1, "他自己说的一条"


def test_coverage_counts_transcript_under_courses_done():
    uid = make_user()
    before = memory.coverage(uid)
    done = next(g for g in before["groups"] if g["label"] == "已修课程")
    assert done["slots"][0]["filled"] is False

    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    after = memory.coverage(uid)
    done = next(g for g in after["groups"] if g["label"] == "已修课程")
    assert done["slots"][0]["filled"] is True
    assert "7" in done["slots"][0]["value"]
    assert after["filled"] > before["filled"]


def test_coverage_prefix_slots_report_a_real_value_not_the_key():
    """前缀类字段（current: / capability:）要显示真实的值，不能把 key 当值显示。"""
    uid = make_user()
    acc, _ = memory.validate_ops(uid, [{
        "op": "add", "key": "current:course", "value": "在修机器学习",
        "evidence_quote": "在修机器学习", "affects": "task_kind"}], ["在修机器学习"])
    memory.apply_ops(uid, acc, "")
    found = [s for g in memory.coverage(uid)["groups"] for s in g["slots"]
             if s["key"] == "current:" and s["filled"]]
    assert found and found[0]["value"] == "在修机器学习"


def test_department_and_major_are_separate_slots():
    """院系≠专业在北大很常见，合成一条会丢掉其中一个（school 就被丢过）。"""
    assert memory.spec_for("department") is not None
    labels = [s["label"] for g in memory.coverage(make_user())["groups"] for s in g["slots"]]
    assert "院系" in labels and "专业" in labels


# ---------- 对话里的查询工具 ----------


def test_transcript_tool_defaults_to_summary_only():
    """P3 优先级：成绩单**不整份进上下文**。
    一个人 20~40 门课全倒进去，既挤掉别的东西，又逼模型自己找。
    所以工具默认只回汇总——这条断言就是在守这个决定。"""
    import tools
    uid = make_user()
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    r = tools.run(uid, {"tool": "transcript.summary", "args": {}})
    assert r["ok"] and r["has_transcript"]
    assert "courses" not in r, "默认不能倒整份成绩单"
    assert "matched" not in r
    assert r["summary"]["gpa"] is not None
    assert r["by_term"], "分学期汇总要在"


def test_transcript_tool_returns_courses_only_when_asked():
    import tools
    uid = make_user()
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    r = tools.run(uid, {"tool": "transcript.summary", "args": {"keyword": "线性代数"}})
    assert r["ok"] and r["matched_count"] >= 1
    assert all("线性代数" in c["course"] for c in r["matched"])
    assert len(r["matched"]) <= 12


def test_transcript_tool_is_honest_when_there_is_no_transcript():
    import tools
    r = tools.run(make_user(), {"tool": "transcript.summary", "args": {}})
    assert r["ok"] and r["has_transcript"] is False
    assert "成绩单" in r["note"]


def test_transcript_tool_reports_no_match_instead_of_inventing():
    import tools
    uid = make_user()
    store.replace_enrollments(uid, transcript.parse_transcript(SAMPLE)["courses"])
    r = tools.run(uid, {"tool": "transcript.summary", "args": {"keyword": "量子场论"}})
    assert r["ok"] and r["matched_count"] == 0 and r["matched"] == []
    assert "没有匹配" in r["note"]


# ---------- 字母等级 / IP / 五级制 ----------
#
# 这一组夹具暴露了三个整条丢课的 bug：
#   字母等级（B+）        → 不认，整门课连学分丢掉
#   在修标记（IP）        → 不认，同样丢掉
#   连带一串「没看懂」    → 格式不认时只前进一行，剩下的行全被当成新记录开头
# 症状是总分对不上：算出的通过学分比成绩单末尾写的总学分少（少了 B+ 那门）。
#
# 注意：这是**合成的**课表，不是任何一位用户的真实成绩单。
# 课名和成绩是照着真实格式编的，覆盖同样的解析分支；
# 早先这里放的是一份真实成绩单，已替换——测试夹具不该来自某一个人的数据。
# 「总学分」那一行的数字必须和各课学分之和一致，这条断言才是有效的。

REAL = """刷新 隐藏文字
25-26学年度2学期
4
学分
高等数学B (二)
专业必修
89
3
学分
统计方法与数据分析
限选
92
0
学分
Python程序设计上机
专业必修
合格
1
学分
军事理论（下）
全校必修
IP
25-26学年度1学期
2
学分
中国美术简史
通选课
B+
2
学分
学术写作与表达
任选
W
1
学分
大学英语听说
全校必修
87
1
学分
军事理论（上）
全校必修
IP
总学分
14
"""


def test_letter_grades_are_not_dropped():
    """B+ 是通过的成绩，不能因为「不是数字」就整门课丢掉。"""
    r = transcript.parse_transcript(REAL)
    film = next((c for c in r["courses"] if c["course"] == "中国美术简史"), None)
    assert film is not None, "字母等级那门课被整条丢掉了"
    assert film["grade"] == "B+"
    assert transcript.is_passed("B+") is True
    assert transcript.normalize_grade("b+") == "B+", "小写也要认"


def test_in_progress_courses_are_kept_and_marked_current():
    """IP = 成绩还没出 = 在修。用户的信息分类里「在修课程」是单独一类，
    所以这类课要记下来，只是不能混进已修统计。"""
    r = transcript.parse_transcript(REAL)
    ip = [c for c in r["courses"] if c["grade"] == "IP"]
    assert len(ip) == 2, ip
    assert all(c["status"] == "current" for c in ip), "IP 要标成 current"
    assert all(c["status"] == "completed" for c in r["courses"] if c["grade"] != "IP")
    # IP 不算通过、也不算绩点
    assert transcript.is_passed("IP") is False
    assert transcript.gpa_of("IP") is None


def test_real_transcript_totals_match_the_paper():
    """最有说服力的一条：成绩单末尾写着总学分，我们必须算出一模一样的数。

    片段里是 14 学分（4+3+0+1+2+2+1+1）。IP 那两条共 2 学分不计。
    """
    r = transcript.parse_transcript(REAL)
    assert len(r["courses"]) == 8, [c["course"] for c in r["courses"]]
    assert r["warnings"] == [], r["warnings"]
    s = transcript.summarize(r["courses"])
    # 通过：4(高数) + 3(统计) + 0(上机·合格) + 2(美术史·B+) + 1(英语) = 10
    assert s["passed_credits"] == 10.0, s
    assert s["gpa_credits"] == 8.0, "只有百分制进 GPA：4+3+1"
    assert s["in_progress_credits"] == 2.0
    assert s["ungraded_credits"] == 2.0, "中国美术简史 2 学分是字母等级"


def test_a_bad_grade_does_not_cascade_into_fake_warnings():
    """格式不认的时候只前进一行，会把这条记录剩下的每一行都当成新记录开头，
    于是一处不认就炸出一串「没看懂」。真实成绩单上因此报了 14 行。"""
    r = transcript.parse_transcript(REAL)
    assert r["warnings"] == [], f"正常成绩单不该有任何警告：{r['warnings']}"


def test_trailing_total_number_is_not_reported_as_unreadable():
    """「总学分」下面那个孤立的数字每份成绩单都有，不该报成没看懂。"""
    r = transcript.parse_transcript(REAL)
    assert not any("没看懂" in w for w in r["warnings"]), r["warnings"]


def test_letter_grades_are_disclosed_not_silently_excluded():
    """字母等级不进 GPA，就必须**说出来**——否则用户看到的总绩点是少算过的，
    而他还以为那是全部。宁可多一句话，不要一个看起来完整其实是错的数。"""
    r = transcript.parse_transcript(REAL)
    s = transcript.summarize(r["courses"])
    assert [u["course"] for u in s["ungraded"]] == ["中国美术简史"]
    assert s["ungraded"][0]["grade"] == "B+"
    assert [u["course"] for u in s["in_progress"]] == ["军事理论（下）",
                                                     "军事理论（上）"]


@pytest.mark.parametrize("grade,passed", [
    ("A+", True), ("A", True), ("A-", True), ("B+", True), ("B", True),
    ("B-", True), ("C+", True), ("C", True), ("D", True), ("F", False),
    ("优秀", True), ("良好", True), ("中等", True), ("及格", True), ("不及格", False),
    ("合格", True), ("不合格", False), ("IP", False), ("W", False), ("I", False),
])
def test_pass_rules(grade, passed):
    assert transcript.is_passed(grade) is passed, grade


def test_no_letter_grade_is_converted_to_a_made_up_gpa():
    """我们不把字母等级换算成绩点——换算表没有权威来源可核，
    编一个会静默算错总分（正是「错记忆悄悄污染一切」那种错）。
    所以它们一律返回 None，并单独列出来让用户看到。"""
    for g in transcript.LETTER_GRADES + transcript.LEVEL_GRADES:
        assert transcript.gpa_of(g) is None, g


def test_zero_credit_courses_do_not_break_the_average():
    """0 学分的讨论课/上机课真实存在，不能把加权平均除出 ZeroDivisionError。"""
    s = transcript.summarize([
        {"course": "信息组织小班讨论课", "grade": "合格", "credits": 0},
        {"course": "高等数学C (一)", "grade": "80", "credits": 4},
    ])
    assert s["gpa_credits"] == 4.0
    assert s["gpa"] == pytest.approx(transcript.gpa_of("80"))


# ---------- 从成绩单推导能力 ----------


def cs(*items):
    """(课程名, 成绩, 学分) → 成绩单记录。

    推导测试不共用 SAMPLE：SAMPLE 是给「粘贴格式解析」用的，
    一共 7 门、数学类只过了一门，凑不够推导门槛。
    两件事的样例分开，改了不会互相绊倒。
    """
    return [{"course": c, "grade": g, "credits": cr, "term": "24-25学年度1学期",
             "kind": "专业必修"} for c, g, cr in items]


MATH_OK = cs(("高等数学 (B) (一)", "90", 5), ("线性代数 (B)", "88", 3))
MIXED = MATH_OK + cs(("概率统计 (B)", "96", 3), ("计算概论（C）", "92", 2),
                     ("博雅英语阅读", "88.5", 2))


def test_derivation_reports_facts_not_judgements():
    """这是 base:code 事故的正面修法。

    那次是模型写下「课程基础扎实」「（依赖AI生成）」这类**判断**，
    再存成事实，之后就再也分不清哪部分有依据了。
    所以推导出来的 value 里只准出现「修过哪几门、考了多少、加权平均多少」。
    """
    d = transcript.derive_capabilities(MIXED)
    assert d, "样例应该推得出东西"
    banned = ("扎实", "较强", "较弱", "优秀", "良好", "精通", "熟练", "擅长",
              "能力强", "偏弱", "基础好", "有天赋")
    for item in d:
        for w in banned:
            assert w not in item["value"], f"{item['key']} 里出现了判断词「{w}」：{item['value']}"
        assert "修过" in item["value"]


def test_derivation_is_credit_weighted():
    """学分加权，不是简单平均：5 学分的 100 分应该盖过 1 学分的 60 分。"""
    courses = [
        {"course": "高等数学 (B) (一)", "grade": "100", "credits": 5, "term": "24-25学年度1学期"},
        {"course": "线性代数 (B)", "grade": "60", "credits": 1, "term": "24-25学年度1学期"},
    ]
    math = next(x for x in transcript.derive_capabilities(courses)
                if x["key"] == "transcript:math")
    assert math["avg"] == pytest.approx((5 * 100 + 1 * 60) / 6, abs=0.01)   # 93.3
    assert math["avg"] != pytest.approx((100 + 60) / 2, abs=0.01)           # 不是 80


def test_failed_courses_do_not_count_as_taken():
    """挂了的高等数学不能算「修过数学类课程」——那句话会误导人。"""
    failed = [
        {"course": "高等数学 (B) (一)", "grade": "45", "credits": 5, "term": "24-25学年度1学期"},
        {"course": "线性代数 (B)", "grade": "52", "credits": 4, "term": "24-25学年度1学期"},
    ]
    assert transcript.derive_capabilities(failed) == []
    # 过了一门，数学类门槛是 2 门，还是不够
    assert transcript.derive_capabilities(failed[:1] + [
        {"course": "线性代数 (B)", "grade": "88", "credits": 4, "term": "24-25学年度1学期"}
    ]) == []


def test_pass_fail_only_courses_are_reported_honestly():
    """只有「合格」时不能编一个分数出来。"""
    courses = [
        {"course": "Python 程序设计", "grade": "合格", "credits": 2, "term": "24-25学年度1学期"},
    ]
    d = transcript.derive_capabilities(courses)
    code = next(x for x in d if x["key"] == "transcript:code")
    assert code["avg"] is None
    assert "合格/未出分" in code["value"]
    assert "平均" not in code["value"], "没有分数就不该出现「平均 X」"


def test_one_course_can_support_two_areas():
    """「概率统计」既是数学类也是统计类——两个领域各自成立，不是重复计数。"""
    courses = [
        {"course": "概率统计 (B)", "grade": "96", "credits": 3, "term": "24-25学年度1学期"},
        {"course": "线性代数 (B)", "grade": "88", "credits": 3, "term": "24-25学年度1学期"},
    ]
    areas = {x["key"] for x in transcript.derive_capabilities(courses)}
    assert "transcript:math" in areas and "transcript:data" in areas


def test_sync_writes_derived_facts_with_evidence():
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    res = memory.sync_transcript_facts(uid)
    assert "transcript:math" in res["added"]
    facts = [f for f in memory.active_facts(uid) if f.key.startswith("transcript:")]
    assert facts and all(f.source == "derived" for f in facts)
    assert all(f.affects for f in facts), "闸门③：每条都要写清改变哪个决策"
    math = next(f for f in facts if f.key == "transcript:math")
    # 依据必须是具体的课程和分数，不是一句空话
    assert math.evidence and all(e["type"] == "transcript" for e in math.evidence)
    assert any("高等数学" in e["course"] for e in math.evidence)
    assert math.confidence == 0.75


def test_sync_is_idempotent():
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    first = memory.sync_transcript_facts(uid)
    second = memory.sync_transcript_facts(uid)
    assert first["added"] and not second["added"]
    assert not second["updated"] and not second["removed"]


def test_sync_updates_when_a_grade_changes():
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    before_fact = next(f for f in memory.active_facts(uid) if f.key == "transcript:math")

    bumped = [dict(c) for c in MIXED]
    for c in bumped:
        if c["course"] == "线性代数 (B)":
            c["grade"] = "99"
    store.replace_enrollments(uid, bumped)
    res = memory.sync_transcript_facts(uid)
    assert "transcript:math" in res["updated"]
    after = next(f for f in memory.active_facts(uid) if f.key == "transcript:math")
    assert after.value != before_fact.value and "99" in after.value
    assert after.id == before_fact.id, "同一条被更新，不该新建一条"


def test_sync_retracts_stale_conclusions():
    """最危险的一种错：用户把课删了，而「他修过 3 门数学类课程」还留着。

    过期的硬证据比没有更糟——它看起来是**最可信**的那类东西，
    模型和用户都会信它。所以必须撤掉。
    """
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    assert any(f.key == "transcript:math" for f in memory.active_facts(uid))

    # 把所有数学类课程删掉
    for row in store.list_enrollments(uid):
        if "math" in transcript.match_areas(row["course"]):
            store.delete_enrollment(uid, row["id"])
    res = memory.sync_transcript_facts(uid)
    assert "transcript:math" in res["removed"]
    assert not any(f.key == "transcript:math" for f in memory.active_facts(uid)), \
        "过期结论必须撤掉，不能留在画像里"


def test_sync_removes_everything_when_transcript_is_emptied():
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    store.replace_enrollments(uid, [])
    res = memory.sync_transcript_facts(uid)
    assert res["added"] == [] and res["removed"]
    assert [f for f in memory.active_facts(uid) if f.key.startswith("transcript:")] == []


def test_model_cannot_write_derived_keys():
    """模型没有底稿，只能编判断——所以 transcript:* 一律拒。"""
    uid = make_user()
    accepted, rejected = memory.validate_ops(uid, [{
        "op": "add", "key": "transcript:math", "value": "数学基础扎实",
        "evidence_quote": "我数学还行", "affects": "task_difficulty"}], ["我数学还行"])
    assert accepted == []
    assert rejected[0]["reason"].startswith("derived_only")


def test_derived_facts_beat_self_report_when_slots_are_scarce():
    """能力层坑位不够时，硬证据应该压过自述。"""
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    # 再塞一堆自述，把能力层塞满
    quotes = [f"我自述会一点东西{i}" for i in range(8)]
    ops = [{"op": "add", "key": f"capability:self{i}", "value": q,
            "evidence_quote": q, "affects": "task_kind"}
           for i, q in enumerate(quotes)]
    acc, _ = memory.validate_ops(uid, ops, quotes)
    memory.apply_ops(uid, acc, "")
    grouped, _ = memory.recall_grouped(uid, "数学")
    cap = grouped["capability"]
    assert len(cap) <= memory.LAYER_BUDGET["capability"]
    assert any(f.source == "derived" and f.key == "transcript:math" for f in cap), \
        "硬证据被自述挤掉了"


def test_user_deletion_of_a_derived_fact_sticks():
    """用户删掉一条推导结论之后，**下次成绩单一动不能又冒出来**。

    能删掉才叫「用户是最终权威」。会自己长回来的东西，用户只会觉得删不掉。
    代价只是少一条注入的结论——成绩单还在，工具照样查得到。
    """
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    fact = next(f for f in memory.active_facts(uid) if f.key == "transcript:math")
    assert memory.user_retract(uid, fact.id) is True

    # 成绩单变了（加了一门课），触发重新推导
    store.replace_enrollments(uid, MIXED + cs(("数学分析 (一)", "85", 5)))
    res = memory.sync_transcript_facts(uid)
    assert "transcript:math" in res["protected"]
    assert not any(f.key == "transcript:math" for f in memory.active_facts(uid)), \
        "用户删过的结论不能自己长回来"


def test_user_edit_of_a_derived_fact_is_not_overwritten():
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    fact = next(f for f in memory.active_facts(uid) if f.key == "transcript:math")
    memory.user_edit(uid, fact.id, "这些课我都是水过去的，别当成底子")

    store.replace_enrollments(uid, MIXED + cs(("数学分析 (一)", "85", 5)))
    memory.sync_transcript_facts(uid)
    after = next(f for f in memory.active_facts(uid) if f.key == "transcript:math")
    assert after.value == "这些课我都是水过去的，别当成底子", "用户改过的不能被系统改回去"
    assert after.source == "user_edit"


def test_rejecting_one_area_does_not_silence_the_others():
    uid = make_user()
    store.replace_enrollments(uid, MIXED)
    memory.sync_transcript_facts(uid)
    fact = next(f for f in memory.active_facts(uid) if f.key == "transcript:math")
    memory.user_retract(uid, fact.id)
    memory.sync_transcript_facts(uid)
    keys = {f.key for f in memory.active_facts(uid) if f.key.startswith("transcript:")}
    assert "transcript:math" not in keys
    assert "transcript:code" in keys and "transcript:data" in keys



    """告诉模型它写不了的 key，就是叫它去撞墙——白花一次调用，还让它以为写成功了。"""
    import dialogue
    doc = dialogue._registry_doc()
    assert "transcript:<slug>" not in doc.split("你**写不了**")[0], \
        "transcript: 不该出现在「可写 key」列表里"
    assert "experience:<slug>" not in doc.split("你**写不了**")[0]
    assert "transcript:<slug>" in doc, "但要说清它写不了、以及为什么"
    assert "derived_from" in doc
