# -*- coding: utf-8 -*-
"""环境包里的「库内事实」：用户说到院系/专业（尤其简称）时，代码先替他查好。

起因是一次真实对话：用户第一句是「我是信管的大二学生」，系统答完一句评价后反问
「你是哪个学校的？」——而「信管」是个简称，本该校内知识库去认；学校也本就默认北大。
这批用例钉三件事：
  1. 说到库内名字 ⟹ 环境包里就有该院系的专业名单（第二轮之前就摆上桌）；
  2. 不问「你是哪个学校」（学校是默认前提，不是待填的空）；
  3. 模型这一轮不可用时，规则版也要把这几条事实说出来，而不是答别的。
运行：pytest server/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False

import dialogue  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()


def make_env(uid: str, message: str) -> dict:
    conv = store.open_conversation(uid, topic_hint=message[:40])
    store.add_message(uid, "user", message, conversation_id=conv["id"])
    return dialogue.build_env(uid, conv["id"], message)


# ---------- 1. 说到名字就先查 ----------


def test_shorthand_in_the_message_puts_the_majors_in_the_env():
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "我是信管的大二学生")

    assert [f["用户说到的词"] for f in env["lib_facts"]] == ["信管"]
    fact = env["lib_facts"][0]
    assert fact["院系"] == "信息管理系"
    assert fact["库里认成"].startswith("院系")
    names = [m["专业"] for m in fact["命中"]]
    assert "图书馆学专业" in names and "大数据管理与应用专业" in names
    # 带上来源页：引用时能说出依据，而不是「我印象里」
    assert all(m["来源页"] for m in fact["命中"])
    # 有界：环境包每轮都进上下文，不能塞成第二份全量数据
    assert all(len(m["核心必修课"]) <= tools.KB_ENV_MAX_COURSES for m in fact["命中"])
    assert env["trace"]["lib_facts"] == ["信管"]


def test_official_name_and_major_name_both_work():
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "信息管理系都分流到哪些专业")
    assert env["lib_facts"][0]["用户说到的词"] in ("信息管理系", "信息管理")

    env2 = make_env(uid, "大数据管理与应用专业要修什么课")
    names = [m["专业"] for m in env2["lib_facts"][0]["命中"]]
    assert "大数据管理与应用专业" in names


def test_no_name_no_lookup():
    """没说到库内名字就不要白查一遍、也不要往环境包里塞东西。"""
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "我今天有点累，先随便聊聊")
    assert env["lib_facts"] == []
    assert env["trace"]["lib_facts"] == []


def test_at_most_two_names():
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "信管和图书馆学、大数据管理与应用、信息管理与信息系统是什么关系")
    assert 1 <= len(env["lib_facts"]) <= tools.KB_ENV_MAX_NAMES


def test_facts_are_in_the_prompt_that_both_calls_see():
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "我是信管的大二学生")
    prompt = dialogue._env_for_prompt(env)
    assert "图书馆学专业" in prompt and "信息管理系" in prompt
    assert "已查好的培养方案原文" in prompt


# ---------- 2. 默认北大，不问学校 ----------


def test_prompt_forbids_asking_which_school():
    p = dialogue._proposal_system()
    assert "默认用户是北大学生，不问学校" in p
    assert "培养方案知识库只覆盖北大" in dialogue._SYSTEM_BASE


def test_prompt_tells_it_to_look_the_name_up_first():
    p = dialogue._proposal_system()
    assert "先去库里认这个名字" in p
    assert "别等他说" in p


def test_school_slot_is_presumed_pku_not_a_hole():
    uid = store.create_user("t")["uid"]
    groups = {g["group"]: g for g in memory.coverage(uid)["groups"]}
    keys = [s["key"] for s in groups["identity"]["slots"]]
    assert "school" not in keys


def test_ladder_does_not_wait_for_school_but_counts_department():
    assert "school" not in memory.LADDER_PROBE["identity"]
    assert "department" in memory.LADDER_PROBE["identity"]

    uid = store.create_user("t")["uid"]
    assert memory.ladder_state(uid)["next_layer"] == "identity"
    accepted, _ = memory.validate_ops(
        uid, [{"op": "add", "key": "grade", "value": "大二", "affects": "task_difficulty",
               "evidence_quote": "我是信管的大二学生"},
              {"op": "add", "key": "department", "value": "信息管理系",
               "affects": "task_difficulty", "evidence_quote": "我是信管的大二学生"}],
        ["我是信管的大二学生"])
    memory.apply_ops(uid, accepted, "我是信管的大二学生")
    st = memory.ladder_state(uid)
    assert st["next_layer"] == "interest", "年级 + 院系已经在「你是谁」这层有数了，别再问身份"
    identity = next(L for L in st["layers"] if L["layer"] == "identity")
    assert identity["known"] is True


# ---------- 3. 模型不可用时也要说出这几条事实 ----------


def test_degraded_reply_uses_the_library_facts():
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "信管有哪些专业分流")
    prop = dialogue._degraded_proposal(uid, "信管有哪些专业分流", env)
    assert prop["dialogue"]["move"] == "answer"
    reply = prop["dialogue"]["reply"]
    assert "信息管理系" in reply and "图书馆学专业" in reply
    assert "先看看你更想往哪个方向走" not in reply


def test_degraded_reply_unchanged_without_library_facts():
    uid = store.create_user("t")["uid"]
    env = make_env(uid, "我今天有点累")
    prop = dialogue._degraded_proposal(uid, "我今天有点累", env)
    assert prop["dialogue"]["move"] == "clarify"
