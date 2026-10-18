"""首次伴读引导的可解释覆盖度。只建议进入，绝不作为访问权限。"""
from __future__ import annotations

import re

import auth
import memory
import store

AREAS = (
    ("background", "学习背景", "年级、院系或专业，任选你愿意说的"),
    ("topic", "关注主题与目的", "想了解的主题，或这次想通过读论文解决什么"),
    ("foundation", "课程与实践基础", "学过的内容、做过的事，或可选成绩单"),
    ("reading", "阅读经历与困难", "是否读过论文，以及通常卡在哪里"),
    ("support", "希望怎样获得帮助", "解释深浅、语言、例子或阅读节奏"),
)
_EMPTY = {"不知道", "不清楚", "不确定", "暂不确定", "随便", "都可以", "未知", "unknown", "未提供"}


def assess(uid: str) -> dict:
    facts = [f for f in memory.active_facts(uid)
             if f.source in ("declared", "user_edit") and f.value.strip().lower() not in _EMPTY
             and not f.key.endswith(":unknown")]
    bins = {k: [] for k, _, _ in AREAS}
    for f in facts:
        k = f.key
        if k in ("grade", "department", "major") or k.startswith("enroll:"):
            bins["background"].append(f.value)
        if k.startswith(("interest:", "goal:", "field:", "direction:")):
            bins["topic"].append(f.value)
        if k.startswith(("base:", "capability:", "project:")):
            bins["foundation"].append(f.value)
        if k in ("reading_experience", "reading_barrier"):
            bins["reading"].append(f.value)
        if k in ("reading_support", "pace") or (
            k.startswith("style:") and re.search(r"解释|例子|术语|练习|考我|中文|英文|推导|先理论|动手", f.value)
        ):
            bins["support"].append(f.value)
    courses = store.list_enrollments(uid)
    if courses:
        bins["foundation"].insert(0, f"已有 {len(courses)} 门课程记录（不代表熟练掌握）")
    def summary(values):
        unique = list(dict.fromkeys(values))
        return "；".join(v for v in unique if not any(v != other and v in other for other in unique))[:180]
    areas = [{"id": k, "label": label, "hint": hint, "known": bool(bins[k]),
              "summary": summary(bins[k])} for k, label, hint in AREAS]
    known = sum(a["known"] for a in areas)
    ready = bool(bins["topic"]) and known >= 4
    return {"version": 2, "first_visit": not auth.user_entered(uid), "ready": ready,
            "known_count": known, "areas": areas,
            "missing": [a["id"] for a in areas if not a["known"]],
            "rule": "关注主题或阅读目的明确，且五个方面中至少了解四个；始终可以提前进入。"}
