# -*- coding: utf-8 -*-
"""Onboarding 对话状态机。

状态机：greet → background → courses → interest_1 → interest_2 → work_style → done
- 每轮都带选项卡；「不知道」永远合法（记 interest: unknown，不追问）；
- 选项命中仍走规则抽取（可复现、有证据）；自由文本优先用 LLM 归纳，失败回退原文入库；
- 回复措辞在配置了 LLM_API_KEY 时由模型生成，失败回退模板。
"""
from __future__ import annotations

from typing import Any

import llm
import store
from schemas import UserFact

UNKNOWN = "不知道"

# 开场固定说明产品职责与可选导入，避免模型把它改回问卷或遗漏操作方法。
TRANSCRIPT_GUIDANCE = (
    "想让我更了解你的基础，可以把树洞成绩单页全选复制后粘贴进来（可选；会存到你的资料里，先去掉不想分享的信息）。"
)
WELCOME_MESSAGE = (
    "你好，我是启研。先说说你的院系、专业和年级（还没分专业就说院系和年级），我好知道从哪开始；也可以直接问我问题。\n\n"
    "不想聊也行：点上面的「进入启研」或「直接去读论文」。" + TRANSCRIPT_GUIDANCE
)

# 每轮定义：问题、选项、抽取规则（选某项/自由文本 → FactCandidate）
ROUNDS: list[dict[str, Any]] = [
    {
        "id": "background",
        "ask": "先认识一下：你现在的年级和大致方向是？",
        "hint": "可以点下面的选项，也可以直接打字描述。",
        "options": [
            {"label": "大一 · 理科 / 工科", "tag": "freshman_stem"},
            {"label": "大二 · 理科 / 工科", "tag": "sophomore_stem"},
            {"label": "大一 · 人文 / 社科", "tag": "freshman_hum"},
            {"label": "大二 · 人文 / 社科", "tag": "sophomore_hum"},
            {"label": "还没想好，先看看", "tag": "undecided"},
        ],
    },
    {
        "id": "courses",
        "ask": "到目前为止，数学和编程这两类基础，你的情况更接近？",
        "hint": "这决定推荐给你的起点难度，如实选就好。",
        "options": [
            {"label": "数学课学过一些（高数 / 线代等）", "tag": "math_some"},
            {"label": "写过代码（任何语言都算）", "tag": "code_some"},
            {"label": "两者都接触过", "tag": "both"},
            {"label": "都还没怎么接触", "tag": "neither"},
        ],
    },
    {
        "id": "interest_1",
        "ask": "遇到一个陌生的复杂系统（比如一个 App），你更想先做什么？",
        "hint": "没有对错，凭第一直觉。",
        "options": [
            {"label": "拆开看它是怎么组成的", "tag": "structure"},
            {"label": "看它的数据和运行行为", "tag": "data"},
            {"label": "直接动手做一个类似的", "tag": "build"},
            {"label": UNKNOWN, "tag": "unknown"},
        ],
    },
    {
        "id": "interest_2",
        "ask": "哪类问题更让你有「想知道为什么」的感觉？",
        "hint": "如果都不明显，选「不知道」完全没问题。",
        "options": [
            {"label": "为什么这个证明 / 公式是对的", "tag": "proof"},
            {"label": "为什么机器能从数据里学会东西", "tag": "ml"},
            {"label": "人为什么会这样想、这样决策", "tag": "human"},
            {"label": UNKNOWN, "tag": "unknown"},
        ],
    },
    {
        "id": "work_style",
        "ask": "学新东西的时候，你更偏向哪种节奏？",
        "hint": "最后一问，聊完我会给你看我对你的初始认知。",
        "options": [
            {"label": "先动手跑起来，再回头补理论", "tag": "hands_on"},
            {"label": "先把原理看懂，再动手", "tag": "theory_first"},
            {"label": "看情况，两边都行", "tag": "mixed"},
            {"label": UNKNOWN, "tag": "unknown"},
        ],
    },
]

_ROUND_INDEX = {r["id"]: i for i, r in enumerate(ROUNDS)}

# tag → 事实抽取规则（category, key, value, source, confidence）
_TAG_FACTS: dict[str, dict[str, Any]] = {
    "freshman_stem": ("background", "grade", "本科一年级，理科/工科方向", "declared", 0.6),
    "sophomore_stem": ("background", "grade", "本科二年级，理科/工科方向", "declared", 0.6),
    "freshman_hum": ("background", "grade", "本科一年级，人文/社科方向", "declared", 0.6),
    "sophomore_hum": ("background", "grade", "本科二年级，人文/社科方向", "declared", 0.6),
    "undecided": ("background", "grade", "年级方向未定，处于探索期", "declared", 0.5),
    "math_some": ("capability", "base:math", "有高等数学/线性代数基础", "declared", 0.6),
    "code_some": ("capability", "base:code", "有一定编程经验（自述写过代码）", "declared", 0.6),
    "both": ("capability", "base:math_code", "数学与编程均有基础", "declared", 0.65),
    "neither": ("capability", "base:none", "数学/编程基础尚浅，处于起步阶段", "declared", 0.5),
    "structure": ("interest", "interest:system_structure", "对「系统如何组成」有第一直觉的好奇（自述想拆开看结构）", "inferred", 0.4),
    "data": ("interest", "interest:data_behavior", "对「数据与运行行为」敏感（自述想看数据和行为）", "inferred", 0.4),
    "build": ("interest", "interest:build_things", "偏向动手构建（自述想直接做一个）", "inferred", 0.4),
    "proof": ("interest", "interest:rigor", "被严格性与证明吸引（自述想知道证明为什么对）", "inferred", 0.4),
    "ml": ("interest", "interest:ml", "对机器学习机理好奇（自述想知道机器为什么能学会）", "inferred", 0.4),
    "human": ("interest", "interest:human_behavior", "对人的行为与决策好奇（自述想知道人为什么这样决策）", "inferred", 0.4),
    "hands_on": ("preference", "style:hands_on", "偏好「先动手后理论」的学习节奏", "declared", 0.6),
    "theory_first": ("preference", "style:theory_first", "偏好「先理论后动手」的学习节奏", "declared", 0.6),
    "mixed": ("preference", "style:flexible", "学习节奏灵活，动手/理论均可", "declared", 0.5),
    "unknown": ("interest", "interest:unknown", "该方面兴趣尚未显现（选择了「不知道」）", "inferred", 0.2),
}

_TRANSITIONS = {
    "background": "courses",
    "courses": "interest_1",
    "interest_2": "work_style",
}

_ACK: dict[str, str] = {
    "background": "收到。接着聊聊你的基础——",
    "courses": "明白了，这个起点很常见。下面两问凭直觉就行：",
    "interest_1": "有意思，这是个很强的信号。",
    "interest_2": "了解了。",
    "work_style": "好，节奏我也记下了。",
}


def start(uid: str) -> dict[str, Any]:
    """新用户进入 onboarding，返回第一轮消息。"""
    state = {"phase": ROUNDS[0]["id"], "round_no": 1}
    store.set_onboard_state(uid, state)
    first = ROUNDS[0]
    reply = WELCOME_MESSAGE
    store.add_message(uid, "assistant", reply, first["id"])
    return _turn(reply, first, state)


def message(uid: str, msg: str) -> dict[str, Any]:
    """处理一轮用户回答：抽取事实 + 推进状态机。空消息 = 幂等地重发当前轮。"""
    state = store.get_onboard_state(uid)
    phase = state.get("phase", ROUNDS[0]["id"])
    if not msg.strip():
        round_def = ROUNDS[_ROUND_INDEX.get(phase, 0)]
        return _turn(round_def.get("cached_reply") or round_def["ask"], round_def, state)
    idx = _ROUND_INDEX.get(phase, 0)
    round_def = ROUNDS[idx]
    store.add_message(uid, "user", msg, phase)

    facts = _extract(uid, round_def, msg)

    nxt = _TRANSITIONS.get(phase)
    if nxt is None:
        nxt = ROUNDS[idx + 1]["id"] if idx + 1 < len(ROUNDS) else "done"
    if nxt == "done":
        state.update({"phase": "done", "round_no": idx + 1})
        store.set_onboard_state(uid, state)
        fallback = (
            "聊完了！基于刚才 5 轮对话，我整理了对你的初始认知（「我的」页）。"
            "每一条都标了来源和置信度——说得不对的你可以直接改，不想要的可以删。"
            "去「我的」页核对，再看方向。"
        )
        reply = _voice(msg, "请去「我的」页核对初始认知。", fallback, closing=True, uid=uid)
        store.add_message(uid, "assistant", reply, "done")
        return {"reply": reply, "options": [], "facts": [f.to_dict() for f in facts],
                "state": state, "done": True}

    state.update({"phase": nxt, "round_no": idx + 2})
    store.set_onboard_state(uid, state)
    nxt_def = ROUNDS[_ROUND_INDEX[nxt]]
    ack = _ACK.get(phase, "")
    fallback = f"{ack}{nxt_def['ask']}" if ack else nxt_def["ask"]
    reply = _voice(msg, nxt_def["ask"], fallback, uid=uid)
    store.add_message(uid, "assistant", reply, nxt_def["id"])
    return _turn(reply, nxt_def, state, facts)


def advance(uid: str, msg: str) -> dict[str, Any] | None:
    """模型不可用时给对话内核用：按规则抽取这一轮的回答、推进五轮问答，返回下一问和可点的选项。
    不写消息（对话内核自己写），已经问完返回 None。
    原来模型额度用完时，对话只会一遍遍说同一句「先看看你想往哪个方向走」，新同学卡在原地（Codex 复现）。"""
    state = store.get_onboard_state(uid)
    phase = state.get("phase") or ROUNDS[0]["id"]
    if phase == "done":
        return None
    idx = _ROUND_INDEX.get(phase, 0)
    round_def = ROUNDS[idx]
    if msg.strip():
        # 只用规则抽取：走到这里就是模型不可用（额度用完或刚刚连失败），不能再去调它（Codex 复现：又一次超时、又扣一笔）
        _extract(uid, round_def, msg, use_model=False)
    nxt = _TRANSITIONS.get(phase) or (ROUNDS[idx + 1]["id"] if idx + 1 < len(ROUNDS) else "done")
    if nxt == "done":
        store.set_onboard_state(uid, {**state, "phase": "done", "round_no": idx + 1})
        return {"reply": "五个问题聊完了。刚才记下的几条在「核对」页，说得不对可以改；"
                         "然后去「方向」页挑一个方向，我按它给你排第一件二十分钟的小事。",
                "options": [], "done": True}
    store.set_onboard_state(uid, {**state, "phase": nxt, "round_no": idx + 2})
    nxt_def = ROUNDS[_ROUND_INDEX[nxt]]
    return {"reply": f"{_ACK.get(phase, '')}{nxt_def['ask']}",
            "options": [o["label"] for o in nxt_def["options"]], "done": False}


def answers_current_round(uid: str, msg: str) -> bool:
    """这句话是不是在点当前这一问的选项。"""
    state = store.get_onboard_state(uid)
    phase = state.get("phase") or ROUNDS[0]["id"]
    if phase == "done":
        return False
    round_def = ROUNDS[_ROUND_INDEX.get(phase, 0)]
    return any(o["label"] == msg.strip() for o in round_def["options"])


def _turn(reply: str, round_def: dict[str, Any], state: dict[str, Any],
          facts: list[UserFact] | None = None) -> dict[str, Any]:
    return {
        "reply": reply,
        "hint": round_def.get("hint", ""),
        "options": [o["label"] for o in round_def["options"]],
        "facts": [f.to_dict() for f in (facts or [])],
        "state": state,
        "done": False,
    }


def _extract(uid: str, round_def: dict[str, Any], msg: str, use_model: bool = True) -> list[UserFact]:
    """W0 规则抽取：命中选项 tag → 预设事实；自由文本 → declared 原文事实。"""
    matched = next((o for o in round_def["options"] if o["label"] == msg), None)
    evidence = [{"type": "onboard_message", "round": round_def["id"], "quote": msg}]
    out: list[UserFact] = []
    if matched and matched["tag"] in _TAG_FACTS:
        cat, key, value, source, conf = _TAG_FACTS[matched["tag"]]
        out.append(UserFact(user_id=uid, category=cat, key=key, value=value,
                            confidence=conf, source=source, evidence=evidence, status="draft"))
    else:
        fact = _extract_free_text(uid, round_def, msg, evidence, use_model)
        out.append(fact)
    for f in out:
        store.add_fact(f)
    return out


def _voice(user_msg: str, next_ask: str, fallback: str, closing: bool = False,
           uid: str | None = None) -> str:
    """有密钥时用模型写一句回应；必须带上下一问或收束句，失败用模板。"""
    if not llm.ready(uid):
        return fallback
    system = (
        "你是「启研」，面向北大本科低年级的科研导师。语气克制、具体，像熟悉的学长。"
        "不要编造学生没说过的经历，不要推销课程，不要使用感叹号堆砌。"
        "回复控制在 80 字以内的中文。"
    )
    if closing:
        user = f"学生刚说：{user_msg[:200]}\n请用一两句话收束对话，并提醒他去「我的」页核对、修改或删除认知条目。不要新增事实。"
    else:
        user = (
            f"学生刚说：{user_msg[:200]}\n"
            f"先用一句接住这句话，然后必须原样提出下一个问题：{next_ask}"
        )
    text = llm.chat(system, user, temperature=0.5, timeout=20, uid=uid)
    if not text:
        return fallback
    if closing or (next_ask in text and len(text) <= 240):
        return text[:240]
    # 只截模型那段、给下一问留位置：原来接上问题再整段截 240 字，模型话一长问题被截掉，存下的消息里没有问题（Codex 复现）
    head = text.split(next_ask, 1)[0]
    return head[:240 - len(next_ask) - 1].rstrip("。") + "。" + next_ask


def _extract_free_text(uid: str, round_def: dict[str, Any], msg: str, evidence: list, use_model: bool = True) -> UserFact:
    category = _guess_category(round_def["id"])
    fallback = UserFact(
        user_id=uid, category=category, key=f"raw:{round_def['id']}",
        value=f"自述：「{msg[:80]}」", confidence=0.6, source="declared",
        evidence=evidence, status="draft",
    )
    if not use_model:
        return fallback
    data = llm.chat_json(
        "把学生的自述归纳成一条可核对的事实。category 只能是 background/interest/capability/preference。"
        "value 必须能在原话里找到依据，不要脑补。",
        json_user(round_def["id"], round_def["ask"], msg),
        timeout=20, uid=uid,
    ) if llm.ready(uid) else None
    if not data:
        return fallback
    value = str(data.get("value") or "").strip()
    cat = str(data.get("category") or category)
    if cat not in {"background", "interest", "capability", "preference", "experience"}:
        cat = category
    if not value or len(value) > 120:
        return fallback
    key = str(data.get("key") or f"raw:{round_def['id']}")[:48]
    return UserFact(
        user_id=uid, category=cat, key=key, value=value,
        confidence=0.55, source="inferred", evidence=evidence, status="draft",
    )


def json_user(round_id: str, ask: str, msg: str) -> str:
    return (
        f"轮次={round_id}\n问题={ask}\n学生原话={msg[:300]}\n"
        '输出 JSON：{"category":"...","key":"短英文键","value":"一句中文事实"}'
    )


def _guess_category(round_id: str) -> str:
    return {
        "background": "background", "courses": "capability",
        "interest_1": "interest", "interest_2": "interest", "work_style": "preference",
    }.get(round_id, "interest")


def confirm(uid: str, edits: list[dict[str, Any]]) -> list[UserFact]:
    """确认页：用户改/删之后，draft → confirmed。"""
    edit_map = {e["id"]: e.get("value") for e in edits if isinstance(e, dict) and e.get("id")}
    dismiss = {e["id"] for e in edits if isinstance(e, dict) and e.get("dismissed")}
    result: list[UserFact] = []
    for f in store.list_facts(uid, statuses=["draft"]):
        if f.id in dismiss:
            store.update_fact(f.id, status="dismissed")
            continue
        if f.id in edit_map and edit_map[f.id]:
            store.update_fact(f.id, value=edit_map[f.id])
        store.update_fact(f.id, status="confirmed")
        result.append(store.get_fact(f.id))  # type: ignore[arg-type]
    return result
