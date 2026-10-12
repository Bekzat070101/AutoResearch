# -*- coding: utf-8 -*-
"""Planner 规则层（W0：确定性规则 + rationale 模板 + 真引用）。

阶段判定（对应 USER_MODEL §6 简化版）：
  S0 无 confirmed 兴趣 → explore_direction（3 张方向卡）
  S1 已选方向 → micro_task
  S2 已完成任务 → 下一张 NBA（进阶任务）

方向卡 = 为什么是你（引用 onboarding 原话）+ 相关课程（真实检索）+ 一篇入门读物。
课程检索通过 pku_adapter 调 skills/pku-course（live，失败明说失败）。

W1 真实化（邬程灿）：在规则层候选集之上加 LLM 决策与措辞，
schema 不变：{chosen, rationale, alternatives[2], confidence}。
"""
from __future__ import annotations

import math
from typing import Any

import llm
import memory
import store
from pku_adapter import search_courses
from schemas import DECISION_STATUSES, NBA, UserFact

# ---------- 方向目录（策展：从 knowledge/disciplines.json 浓缩的入门友好子集）----------

DIRECTIONS: dict[str, dict[str, Any]] = {
    "ai": {
        "code": "ai",
        "name": "人工智能与机器学习",
        "discipline_ref": "计算机科学与技术（0812 相关方向）",
        "blurb": "研究机器如何从数据中学习规律，并解释它为什么犯错。",
        "course_query": "人工智能",
        "reading": {"title": "周志华《机器学习》第 1 章（绪论，约 40 分钟）",
                    "why": "不要求基础，读完能回答「机器学习到底在干什么」。"},
        "signal_keys": ["interest:ml", "interest:data_behavior", "interest:build_things"],
    },
    "math": {
        "code": "math",
        "name": "数学科学",
        "discipline_ref": "数学（110）",
        "blurb": "用严格的语言描述数量、结构、变化与空间，是一切推理的地基。",
        "course_query": "数学",
        "reading": {"title": "柯朗《什么是数学》第 1 章（自然数，约 40 分钟）",
                    "why": "经典通识入门，展示数学是「讲道理」而非「算技巧」。"},
        "signal_keys": ["interest:rigor", "interest:system_structure"],
    },
    "stat": {
        "code": "stat",
        "name": "统计学与数据科学",
        "discipline_ref": "统计学（9101）",
        "blurb": "在不确定的世界里做推断：数据怎么说、能信多少。",
        "course_query": "统计",
        "reading": {"title": "《统计学习导论》(ISL) 第 1 章（约 30 分钟）",
                    "why": "数据思维的第一课，例子多于公式。"},
        "signal_keys": ["interest:data_behavior", "interest:ml"],
    },
    "psy": {
        "code": "psy",
        "name": "心理学与认知科学",
        "discipline_ref": "心理学（190）",
        "blurb": "用实验和模型解释人如何感知、判断与决策。",
        "course_query": "心理",
        "reading": {"title": "卡尼曼《思考，快与慢》第 1 章（约 30 分钟）",
                    "why": "从直觉偏差切入认知科学的核心问题。"},
        "signal_keys": ["interest:human_behavior"],
    },
    "econ": {
        "code": "econ",
        "name": "经济学",
        "discipline_ref": "经济学（790）",
        "blurb": "研究激励、选择与均衡：人为什么这样决策，市场如何回应。",
        "course_query": "经济学",
        "reading": {"title": "《经济学的思维方式》第 1 章（约 30 分钟）",
                    "why": "零公式入门，建立「激励看世界」的视角。"},
        "signal_keys": ["interest:human_behavior", "interest:data_behavior"],
    },
    "se": {
        "code": "se",
        "name": "计算机系统与软件",
        "discipline_ref": "计算机科学与技术（0812）/ 软件工程（0835）",
        "blurb": "把复杂拆成可组合的部分，再把它建出来、让它可靠地跑。",
        "course_query": "软件",
        "reading": {"title": "《深入理解计算机系统》第 1 章选读（约 40 分钟）",
                    "why": "回答「你写的程序在机器里到底发生了什么」。"},
        "signal_keys": ["interest:system_structure", "interest:build_things"],
    },
    "med": {
        "code": "med",
        "name": "基础医学",
        "discipline_ref": "基础医学（310）",
        "blurb": "疾病机制的实验室科学底座：解剖、生理、病理、免疫、药理。不含临床各科。",
        "course_query": "基础医学",
        "reading": {"title": "OpenStax《Anatomy and Physiology》第 1 章（约 40 分钟）",
                    "why": "公开教材，先建立「结构如何对应功能」，不要求医学背景。"},
        "signal_keys": [],
    },
}


def _signals(facts: list[UserFact]) -> dict[str, list[UserFact]]:
    """fact.key → 命中的事实列表（只统计已确认/行为类，draft 不参与决策）。

    用白名单而不是排除列表：新增状态（如 superseded）必须显式加入才会参与决策，
    否则会被静默当成有效信号（例如换了方向后旧方向仍被计分）。
    """
    out: dict[str, list[UserFact]] = {}
    for f in facts:
        if f.status not in DECISION_STATUSES:
            continue
        out.setdefault(f.key, []).append(f)
    return out


def _quote(uid: str, round_id: str) -> str | None:
    """取 onboarding 某轮的用户原话，作为 rationale 引用。"""
    for m in store.list_messages(uid):
        if m["role"] == "user" and m["round"] == round_id:
            return m["text"]
    return None


def direction_cards(uid: str, limit: int = 3, with_courses: bool = True, voice: bool = True) -> list[dict[str, Any]]:
    """按当前画像的兴趣事实生成方向推荐。没有命中信号时不硬凑三张。"""
    facts = store.list_facts(uid)
    signals = _signals(facts)
    scored: list[tuple[int, dict[str, Any]]] = []
    for d in DIRECTIONS.values():
        score, hits = 0, []
        for k in d["signal_keys"]:
            for f in signals.get(k, []):
                score += 1 if f.source == "inferred" else 2
                hits.append(f)
        scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    top = [d for s, d in scored if s > 0][:limit]

    quotes = {
        "interest_1": _quote(uid, "interest_1"),
        "interest_2": _quote(uid, "interest_2"),
        "work_style": _quote(uid, "work_style"),
    }
    def first(key: str) -> UserFact | None:
        v = signals.get(key)
        return v[0] if v else None

    pref = first("style:hands_on") or first("style:theory_first") or first("style:flexible")
    base = first("base:math") or first("base:code") or first("base:math_code") or first("base:none")

    cards: list[dict[str, Any]] = []
    for d in top:
        why_parts: list[str] = []

        def hit(round_id: str) -> bool:
            return any(k in signals for k in _round_keys(d, round_id))

        interest_hit = hit("interest_1") or hit("interest_2")
        if quotes["interest_2"] and hit("interest_2"):
            why_parts.append(f"你说「{quotes['interest_2']}」——这正是{d['name']}每天面对的核心问题")
        if quotes["interest_1"] and hit("interest_1"):
            why_parts.append(f"你的第一直觉是「{quotes['interest_1']}」，与这个方向的工作方式吻合")
        if not why_parts:
            why_parts.append(f"结合你目前的年级与基础，{d['name']}是探索成本最低的入口之一")
        if pref:
            why_parts.append(f"同时考虑到你的{pref.value}")
        if base:
            why_parts.append(f"你的基础（{base.value}）足以支撑第一个 20 分钟任务")
        cards.append(_build_card(uid, d, why_parts, with_courses, hits_exist=interest_hit))
    if not voice:
        for c in cards:
            c["voice"] = "rules"
        return cards
    return _voice_cards(cards, facts, quotes, uid=uid)


def _round_keys(d: dict[str, Any], round_id: str) -> list[str]:
    """方向 signal_keys 中属于某一轮兴趣探测的 key（用于判断引用哪句话）。"""
    mapping = {
        "interest_1": ["interest:system_structure", "interest:data_behavior", "interest:build_things"],
        "interest_2": ["interest:rigor", "interest:ml", "interest:human_behavior"],
    }
    return [k for k in d["signal_keys"] if k in mapping.get(round_id, [])]


def _build_card(uid: str, d: dict[str, Any], why_parts: list[str],
                with_courses: bool, hits_exist: bool) -> dict[str, Any]:
    course_block: dict[str, Any] = {"status": "pending"}
    if with_courses:
        course_block = _course_block(d)
    return {
        "direction": {"code": d["code"], "name": d["name"], "blurb": d["blurb"],
                      "discipline_ref": d["discipline_ref"], "course_query": d["course_query"]},
        "why_you": "；".join(why_parts) + "。",
        "courses": course_block,
        "reading": d["reading"],
        "confidence": 0.55 if hits_exist else 0.35,
    }


def _course_block(d: dict[str, Any]) -> dict[str, Any]:
    res = search_courses(d["course_query"], limit=4)
    if not res.get("ok"):
        return {"status": "error", "message": res.get("error") or "课程检索暂时不可用（如实告知：不编造）"}
    items = [
        {
            "name": it.get("name") or it.get("courseName") or "(未命名课程)",
            "teacher": it.get("teacher") or it.get("teachers") or "",
            "dept": it.get("department") or it.get("dept") or "",
            "term": res.get("term") or "",
            "ref": it.get("ref") or it.get("url") or "",
        }
        for it in (res.get("items") or [])[:4]
    ]
    if not items:
        return {"status": "empty", "message": f"本轮检索「{d['course_query']}」没有查到课程——检索为真实调用，查不到就说查不到"}
    return {"status": "ok", "source": "北大教务公开接口（pku-course-skill 实时检索）",
            "term": res.get("term") or "", "items": items}


def next_best_action(uid: str) -> dict[str, Any]:
    """规则层 NBA 主入口（对应 NEXT_PRE 演示链路 ④/⑨）。"""
    facts = store.list_facts(uid, statuses=["confirmed", "active"])
    tasks = store.list_tasks(uid)
    direction_state = _user_direction(uid, facts)

    if not direction_state:
        # S0：还没有确认的兴趣方向 → 推荐方向卡
        return _speak(NBA(
            action="explore_direction",
            title="先看看你可能适合的 3 个探索方向",
            rationale="你刚完成初始对话，AI 对你的兴趣已有初步信号，但还没有确认任何方向。按产品原则，下一步是「探索」而不是「上课」。",
            rationale_facts=[f.id for f in facts if f.category == "interest"][:3],
            payload={"cards": direction_cards(uid, with_courses=False)},
        ).to_dict(), facts, uid=uid)

    done = [t for t in tasks if t.status == "done"]
    if direction_state and not done:
        # S1：已选方向，未完成任务 → 生成首个微任务
        d = DIRECTIONS.get(direction_state)
        return _speak(NBA(
            action="micro_task",
            title=f"开始你在「{d['name']}」的第一个 20 分钟微任务",
            rationale=f"你已选择「{d['name']}」且尚未完成任何任务。行动大于信息——先用一个小任务验证这个方向对你的吸引程度。",
            payload={"direction": direction_state, "task_level": 1},
        ).to_dict(), facts, uid=uid)

    # S2：有已完成任务 → 进阶 NBA
    d = DIRECTIONS.get(direction_state) or DIRECTIONS["ai"]
    last = done[-1] if done else None
    behavior_fact = next((f for f in facts if f.source == "behavior"), None)
    quote = f"「{behavior_fact.value}」" if behavior_fact else "你提交的第一次任务产出"
    return _speak(NBA(
        action="micro_task",
        title=f"进阶一步：「{d['name']}」的第 2 个微任务",
        rationale=f"行为证据：{quote[:60]}——说明你能在这个方向完成闭环。下一个任务难度 +1，仍然 20 分钟内。",
        rationale_facts=[behavior_fact.id] if behavior_fact else [],
        alternatives=[
            {"action": "explore_direction", "title": "换个方向再试试（生成新的 3 张推荐卡）"},
            {"action": "review_progress", "title": "回顾「AI 认识的我」，看看积累的证据"},
        ],
        payload={"direction": direction_state, "task_level": 2},
    ).to_dict(), facts, uid=uid)


def _voice_cards(cards: list[dict[str, Any]], facts: list[UserFact], quotes: dict[str, str | None],
                 uid: str | None = None) -> list[dict[str, Any]]:
    """规则层已选出方向；模型只改写「为什么是你」，失败保留原文。"""
    if not llm.ready(uid) or not cards:
        for c in cards:
            c["voice"] = "rules"
        return cards
    lines = []
    for i, c in enumerate(cards):
        lines.append(f"{i}. {c['direction']['name']}｜规则理由：{c['why_you']}")
    said = [f"{k}={v}" for k, v in quotes.items() if v]
    # 用白名单（schemas.DECISION_STATUSES），不用排除列表：
    # 「用户删掉的」在合并后的代码里有两个状态（retracted / deleted），
    # 排除列表漏掉任何一个，被删的事实就会继续进模型上下文。
    facts_txt = [f.value for f in facts if f.status in DECISION_STATUSES][:8]
    data = llm.chat_json(
        "你在改写给本科低年级的方向推荐理由。每条必须引用学生原话或已记录事实，禁止编造经历。"
        "why 不超过 70 个汉字。条数必须与输入一致。",
        "学生原话：\n" + "\n".join(said) + "\n已记录：\n" + "\n".join(facts_txt)
        + "\n候选：\n" + "\n".join(lines)
        + '\n输出 JSON：{"items":[{"index":0,"why":"..."}]}',
        timeout=28, uid=uid,
    )
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        for c in cards:
            c["voice"] = "rules"
        return cards
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("index")
        # 取整前先查是有限数：原来 1e309 读成 inf，int(inf) 抛 OverflowError，方向卡接口 500（Codex 复现）
        if isinstance(raw, float) and not math.isfinite(raw):
            continue
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        why = str(item.get("why") or "").strip()
        if 0 <= idx < len(cards) and 8 <= len(why) <= 160:
            cards[idx]["why_you"] = why if why.endswith("。") else why + "。"
            cards[idx]["voice"] = "llm"
    for c in cards:
        c.setdefault("voice", "rules")
    return cards


def _speak(nba: dict[str, Any], facts: list[UserFact], uid: str | None = None) -> dict[str, Any]:
    """规则层决定行动；模型只改写标题和理由，失败保持原文。"""
    if not llm.ready(uid):
        nba["voice"] = "rules"
        return nba
    quotes = [f"{f.category}:{f.value}" for f in facts[:6]]
    data = llm.chat_json(
        "你在改写科研导师的「下一步」文案。不得改变行动类型，不得编造证据里没有的经历。"
        "title 不超过 28 字，rationale 不超过 90 字，必须能对应给出的事实。",
        "行动=" + nba.get("action", "") + "\n原标题=" + nba.get("title", "")
        + "\n原理由=" + nba.get("rationale", "") + "\n已知事实=\n- " + "\n- ".join(quotes)
        + '\n输出 JSON：{"title":"...","rationale":"..."}',
        timeout=20, uid=uid,
    )
    if not data:
        nba["voice"] = "rules"
        return nba
    title = str(data.get("title") or "").strip()
    rationale = str(data.get("rationale") or "").strip()
    if 4 <= len(title) <= 40:
        nba["title"] = title
    if 12 <= len(rationale) <= 180:
        nba["rationale"] = rationale
    nba["voice"] = "llm"
    return nba


def _user_direction(uid: str, facts: list[UserFact]) -> str | None:
    for f in facts:
        if f.key.startswith("direction:") and f.status in ("confirmed", "active"):
            return f.key.split(":", 1)[1]
    return None


def choose_direction(uid: str, code: str) -> UserFact:
    """用户在推荐卡中选择一个方向 → 写入 UM（confirmed）。"""
    d = DIRECTIONS[code]
    quote = _quote(uid, "interest_2") or _quote(uid, "interest_1") or "初始对话"
    f = UserFact(
        user_id=uid, category="interest", key=f"direction:{code}",
        value=f"已确认探索方向：{d['name']}（基于初始对话「{quote[:30]}」）",
        confidence=0.55, source="declared",
        evidence=[{"type": "card_selection", "direction": code, "quote": quote}],
        status="confirmed",
    )
    # 先撤掉旧方向再记新的：原来两条都留着，前端按最新的显示、规划和对话按最早的走（Codex 复现：选了数学还在出 AI 的任务）
    memory._supersede_existing(uid, f.key, memory._NAMESPACED["direction"], keep=None, decision_id="card_selection")
    store.add_fact(f)
    return f
