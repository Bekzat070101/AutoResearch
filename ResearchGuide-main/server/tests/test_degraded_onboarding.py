# -*- coding: utf-8 -*-
"""模型不可用（额度用完、没配密钥）时，新同学在对话里也能把五问走完，不会卡在同一句。Codex 第四轮复现过卡住。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dialogue  # noqa: E402
import onboarding  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "deg.db")
    store.init_db()
    monkeypatch.setattr(dialogue.llm, "enabled", lambda: False)
    monkeypatch.setattr(onboarding.llm, "enabled", lambda: False)


def test_a_new_student_gets_through_the_five_questions_without_the_model():
    uid = store.create_user("新同学")["uid"]
    onboarding.start(uid)
    replies, offers = [], []
    answers = ["大一 · 理科 / 工科", "两者都接触过", "我想知道大模型为什么会胡说", "随便", "我喜欢先动手"]
    for msg in answers:
        r = dialogue.turn(uid, msg)
        replies.append(r["reply"])
        offers.append([a["label"] for a in r.get("offered_actions") or []])
    assert store.get_onboard_state(uid)["phase"] == "done"
    assert len(set(replies)) == len(replies)  # 每一轮都往前走了，不是同一句
    assert offers[0]  # 前几轮给了可点的选项
    assert "方向" in replies[-1] and "核对" in replies[-1]
    assert store.list_facts(uid)  # 回答记成了待核对的条目
    # 问完之后再说话：不重新开始五问；上游 PR #19 把没模型时的话改成了指向研读模块
    after = dialogue.turn(uid, "然后呢")
    assert "研读" in after["reply"] and store.get_onboard_state(uid)["phase"] == "done"


def test_an_option_that_mentions_a_subject_still_answers_the_question():
    """「数学课学过一些」会命中知识库里的数学专业；原来没模型时就转去讲数学专业，这一问永远答不完。"""
    uid = store.create_user("选项")["uid"]
    onboarding.start(uid)
    dialogue.turn(uid, "大一 · 理科 / 工科")
    assert store.get_onboard_state(uid)["phase"] == "courses"
    r = dialogue.turn(uid, "数学课学过一些（高数 / 线代等）")
    assert store.get_onboard_state(uid)["phase"] != "courses"  # 往下走了
    assert r.get("offered_actions")


def test_the_no_model_wizard_never_calls_the_model(monkeypatch):
    """模型配着、但这一轮连不上（_propose 失败）：规则版五问不能再去调它。"""
    monkeypatch.setattr(dialogue.llm, "enabled", lambda: True)
    monkeypatch.setattr(onboarding.llm, "enabled", lambda: True)
    monkeypatch.setattr(dialogue, "_propose", lambda env: None)
    called = []
    monkeypatch.setattr(onboarding.llm, "chat_json", lambda *a, **k: called.append(1) or None)
    monkeypatch.setattr(onboarding.llm, "chat", lambda *a, **k: called.append(1) or None)
    uid = store.create_user("连不上")["uid"]
    store.set_onboard_state(uid, {"phase": "background", "round_no": 1})
    dialogue.turn(uid, "我是大二的，平时喜欢自己动手写点小工具")  # 自由文本（不提院系）：原来会走模型抽取
    assert called == []
    assert store.get_onboard_state(uid)["phase"] == "courses"
