# -*- coding: utf-8 -*-
"""把成绩单直接粘进对话，应该被直接识别。

用户的原话：「为什么我直接在对话里输入成绩单，不能直接智能识别？
只能在核对页面手动输入才能识别」

这里钉住三件事：
  ① 解析与落库由代码完成，随后必须接回对话；模型不决定课程/分数
  ② 随口说「高数 85、线代 90」→ **不能**被当成成绩单（那是自述，不是底稿）
  ③ 像成绩单但读不出来 → 如实说为什么，并指向核对页
"""
from __future__ import annotations

import pathlib
import sys

import pytest

SERVER = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import dialogue  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402
import transcript  # noqa: E402

# 一份缩小但真实形状的成绩单（学期标题 + 学分 / 学分 / 课名 / 成绩）
REAL = """25-26学年度1学期
3
学分
高等数学 (B) (二)
专业必修
86.5
3
学分
线性代数 (B)
专业必修
88
2
学分
计算概论（C）
专业必修
92
2
学分
中国美术简史
通选课
B+
2
学分
军事理论（上）
公共必修
IP
24-25学年度2学期
3
学分
概率统计 (B)
专业必修
96
1
学分
大学英语听说
全校必修
88.5
"""


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    # 关掉模型：这一轮本来就不该调它。测试要能证明「没用模型也认得出」。
    monkeypatch.setattr(llm, "enabled", lambda: False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)


@pytest.fixture()
def uid():
    return store.create_user("成绩单")["uid"]


def test_detector_accepts_a_real_transcript():
    assert transcript.looks_like_transcript(REAL), "这份成绩单应该被认出来"


@pytest.mark.parametrize("text", [
    "我高数 85、线代 90，成绩还行",
    "这学期有 3 门课，学分分别是 3 3 2",
    "我想问问怎么选课",
    "",
    "25-26学年度1学期",          # 只有学期标题，没有课
])
def test_detector_rejects_casual_talk(text):
    """随口一句全是数字的话**不是**成绩单。

    这是最关键的一条：错判会让一句闲聊变成「硬证据」。
    宁可漏判（让他去核对页粘），也不要错判。
    """
    assert not transcript.looks_like_transcript(text), f"不该被当成成绩单：{text!r}"


def test_pasting_a_transcript_imports_it(uid):
    r = dialogue.turn(uid, REAL)
    imp = r.get("transcript_import") or {}
    assert imp.get("written") == 7, f"应该落 7 门，实际 {imp.get('written')}"

    courses = store.list_enrollments(uid)
    assert len(courses) == 7
    names = {c["course"] for c in courses}
    assert "中国美术简史" in names, "字母等级（B+）的课不该被丢掉"
    assert "军事理论（上）" in names, "在修（IP）的课不该被丢掉"


def test_the_reply_says_what_was_read(uid):
    r = dialogue.turn(uid, REAL)
    reply = r["reply"]
    assert "7 门课" in reply, f"要说清读了几门：{reply[:120]}"
    assert "下方" in reply
    imp = r["transcript_import"]
    assert imp["summary"]["gpa"] is not None
    assert any(c["course"] == "中国美术简史" for c in imp["summary"]["ungraded"])
    assert any(c["course"] == "军事理论（上）" for c in imp["summary"]["in_progress"])
    assert "你现在读什么专业" in reply, "仅有课程时要自然接话，不能停在回执"


def test_it_derives_capability_facts(uid):
    """导入成绩单真正的用处：让「能力」从模型编的印象变成从底稿推出来的事实。"""
    r = dialogue.turn(uid, REAL)
    derived = (r.get("transcript_import") or {}).get("derived") or []
    assert derived, "应该推出能力结论"
    keys = {f["key"] for f in derived}
    assert any(k.startswith("transcript:") for k in keys), keys


def test_parser_and_commit_do_not_depend_on_model(uid, monkeypatch):
    """成绩单是确定性数据，这一轮不该花一次模型调用。

    如果哪天有人把它改成「先问模型」，这条会红。
    """
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("导入成绩单不该调模型")

    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", boom)
    monkeypatch.setattr(llm, "chat_text", boom, raising=False)

    r = dialogue._transcript_turn(uid, "parser-only", REAL)
    assert called["n"] == 0
    assert (r.get("transcript_import") or {}).get("written") == 7


def test_it_creates_no_action_card(uid):
    """导入是一次数据录入，不是一步行动——别顺手派个任务。"""
    r = dialogue.turn(uid, REAL)
    assert r["next_action"] is None
    assert r["pending_action"] is None
    assert store.list_tasks(uid) == []


def test_the_user_message_is_still_in_the_history(uid):
    """粘的成绩单要留在对话记录里，用户回头能看到自己粘了什么。"""
    dialogue.turn(uid, REAL)
    msgs = store.list_messages(uid)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"], roles
    assert "高等数学" in msgs[0]["text"]
    assert "7 门课" in msgs[1]["text"]


def test_pasting_twice_does_not_duplicate_courses(uid):
    """再粘一次是整份替换，不是追加——否则课程会翻倍。"""
    dialogue.turn(uid, REAL)
    dialogue.turn(uid, REAL)
    assert len(store.list_enrollments(uid)) == 7, "重复粘贴不该产生重复课程"


@pytest.mark.parametrize("instruction", ["这是合成示例，只检查，不要保存。", "先别导入，只预览。"])
def test_preview_does_not_save_or_derive_capabilities(uid, instruction):
    r = dialogue.turn(uid, instruction + "\n" + REAL)
    assert r["transcript_import"]["mode"] == "preview"
    assert r["transcript_import"]["written"] == 0
    assert store.list_enrollments(uid) == []
    assert not any(f.key.startswith("transcript:") for f in memory.active_facts(uid))
    assert "没有保存" in r["reply"]


def test_appending_keeps_existing_courses_and_is_idempotent(uid):
    dialogue.turn(uid, REAL)
    extra = "25-26学年度2学期\n3\n学分\n数据库系统概论\n专业必修\n85\n2\n学分\n信息检索\n专业必修\n92\n"
    r = dialogue.turn(uid, "补充这两门，不要替换原来的课程。\n" + extra)
    assert r["transcript_import"]["mode"] == "append"
    assert r["transcript_import"]["written"] == 2
    assert len(store.list_enrollments(uid)) == 9
    r = dialogue.turn(uid, "追加这两门。\n" + extra)
    assert r["transcript_import"]["written"] == 0
    assert len(store.list_enrollments(uid)) == 9


def test_a_transcript_without_semester_headers_explains_itself(uid):
    """像成绩单但缺学期标题 → 说清为什么读不出来，并指向核对页。"""
    body = "\n".join([
        "3", "学分", "高等数学 (B) (二)", "专业必修", "86.5",
        "3", "学分", "线性代数 (B)", "专业必修", "88",
    ])
    assert not transcript.looks_like_transcript(body), \
        "缺学期标题时不该自动导入（会被解析器整条跳过）"
    # 走正常对话路径，不该崩
    r = dialogue.turn(uid, body)
    assert r["reply"], "要给出回复"
    assert not (r.get("transcript_import") or {}).get("written")


def test_partial_parse_reports_warnings(uid):
    """有一行读不懂时，如实报出来，不替用户补。"""
    # 「待定」不是任何一种成绩形态，解析器应该把它报成「没看懂」
    broken = REAL + "2\n学分\n神秘课程\n专业必修\n待定\n"
    parsed = transcript.parse_transcript(broken)
    assert parsed["warnings"], "读不懂的行要报出来"
    assert any("神秘课程" in w for w in parsed["warnings"]), \
        f"要指出是哪一门：{parsed['warnings']}"
    # 而且它是**可被识别**的成绩单（前面有 7 门好的），所以会走导入路径
    assert transcript.looks_like_transcript(broken)
    r = dialogue.turn(uid, broken)
    assert "没看懂" in r["reply"], f"回复里要说出来：{r['reply'][-200:]}"
    assert any("神秘课程" in w for w in r["transcript_import"]["warnings"]), "在展开详情中指出是哪一门"
    # 读不懂的那门不该被凭空补一条
    assert len(store.list_enrollments(uid)) == 7


def test_coverage_updates_after_import(uid):
    """导入之后「已修课程」应该从空变成已填。"""
    before = memory.coverage(uid)
    assert before["filled"] == 0, "学校已从核对格剪掉；他自己还没说过任何东西"
    dialogue.turn(uid, REAL)
    after = memory.coverage(uid)
    assert after["filled"] > before["filled"], "覆盖度应该上升"
    done = next(g for g in after["groups"] if g["group"] == "courses_done")
    assert done["filled"] == 1, "「已修课程」应该已填"
