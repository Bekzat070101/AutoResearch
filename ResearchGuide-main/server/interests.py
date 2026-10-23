# -*- coding: utf-8 -*-
"""把记忆和方向树映射到 arXiv 话题。只走策展词表，不调模型，原话不进查询。"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import memory
import store

ROOT = Path(__file__).resolve().parent.parent
TABLE_PATH = ROOT / "knowledge" / "arxiv_topics.json"
SKIP_KEYS = {"goal", "career", "style", "pace"}
SKIP_SLUGS = {"unknown"}
ONBOARD_TAGS = {
    "system_structure", "data_behavior", "build_things", "rigor",
    "human_behavior", "none",
}
UNMATCHED: Counter[str] = Counter()
WIDE_ZH = {"网络", "分类", "数值", "生理", "计算机", "经济"}
L1_ROOTS = ("520", "110", "190", "790", "310")
_TABLE: dict[str, Any] | None = None


def table() -> dict[str, Any]:
    global _TABLE
    if _TABLE is None:
        _TABLE = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    return _TABLE


def reload() -> None:
    global _TABLE
    _TABLE = None


def topic(tid: str) -> dict[str, Any] | None:
    return table()["topics"].get(tid)


def is_thin_topic(tid: str) -> bool:
    spec = topic(tid) or {}
    if spec.get("source") in ("osf", "psyarxiv", "socarxiv", "biorxiv", "medrxiv"):
        return False
    if spec.get("source") == "arxiv" and not spec.get("en"):
        return False
    nodes = table()["nodes"]
    thin = set(table().get("thin") or [])
    homes = [nid for nid, ids in nodes.items() if tid in ids]
    return bool(homes) and all(n in thin for n in homes)


def l1_of_node(node: str) -> str:
    n = str(node or "")
    for root in L1_ROOTS:
        if n == root or n.startswith(root + "."):
            return root
    return n.split(".", 1)[0] if n else ""


def l1_of_topic(tid: str) -> str:
    for nid, ids in table()["nodes"].items():
        if tid in ids:
            return l1_of_node(nid)
    spec = topic(tid) or {}
    return str(spec.get("l1") or "")


def direction_nodes(uid: str) -> list[str]:
    """本轮方向以 trail.dir 为准；dirs 是收藏，不是当前推荐范围。
    老数据没有当前方向时仍兼容 sel，但不把所有收藏混入推荐。
    """
    trail = store.get_trail(uid, store.active_portrait_id(uid) or "default")
    nodes = table()["nodes"]

    def as_ids(raw: Any) -> list[str] | None:
        if not isinstance(raw, list):
            return None
        return [str(n or "") for n in raw if str(n or "") in nodes]

    node = str(trail.get("dir") or "")
    if node in nodes:
        return [node]
    sel = as_ids(trail.get("sel"))
    if sel is not None:
        return sel
    return []


def direction_node(uid: str) -> str:
    nodes = direction_nodes(uid)
    return nodes[0] if nodes else ""


def direction_label(nodes: list[str]) -> str:
    names = [direction_name(n) for n in nodes if direction_name(n)]
    if not names:
        return ""
    head = " + ".join(names[:3])
    return head + (f" 等{len(names)} 个方向" if len(names) > 3 else "")


def direction_name(node: str) -> str:
    tids = table()["nodes"].get(node) or []
    if tids and topic(tids[0]):
        return topic(tids[0]).get("name") or node
    return node or ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _match_topic(slug: str, value: str) -> str | None:
    """中文最长命中优先；2 字宽别名只做全等；再才用 slug。"""
    slug = (slug or "").lower().replace("_", "-")
    val = _norm(value)
    best, best_len = None, -1
    for tid, spec in table()["topics"].items():
        for z in spec.get("zh") or []:
            nz = _norm(z)
            if not nz or len(nz) < 2 or not val:
                continue
            wide = nz in WIDE_ZH and len(nz) == 2
            hit = (val == nz) if wide else (val == nz or nz in val or val in nz)
            if hit and len(nz) > best_len:
                best, best_len = tid, len(nz)
    if best:
        return best
    for tid, spec in table()["topics"].items():
        slugs = [s.lower().replace("_", "-") for s in spec.get("slugs") or []]
        if slug and (slug in slugs or slug == tid):
            return tid
    return None


def user_topics(uid: str) -> tuple[list[str], list[str]]:
    """方向话题在前，同级学科兴趣随后；prefs.kit_id 不进今日。"""
    interests: list[str] = []
    unmatched: list[str] = []
    facts = memory.active_facts(uid)
    for f in facts:
        key = f.key or ""
        head, _, tail = key.partition(":")
        if head in SKIP_KEYS or tail in SKIP_SLUGS or tail in ONBOARD_TAGS:
            continue
        if head == "direction":
            continue
        if head not in ("interest", "field"):
            continue
        hit = _match_topic(tail, f.value or "")
        if hit:
            if hit not in interests:
                interests.append(hit)
        else:
            term = tail or (f.value or "")[:20]
            if term:
                unmatched.append(term)
                UNMATCHED[term] += 1

    # 选了的方向（多选）：每个方向带前两个话题，总量封顶；推荐论文按所选组合来
    nodes = direction_nodes(uid)
    dir_ids: list[str] = []
    if nodes:
        for node in nodes:
            for tid in (table()["nodes"].get(node) or [])[:2]:
                if tid not in dir_ids:
                    dir_ids.append(tid)
                if len(dir_ids) >= 6:
                    break
            if len(dir_ids) >= 6:
                break
        l1 = l1_of_node(nodes[0])
        same: list[str] = []
        for hit in interests:
            if hit in dir_ids:
                continue
            if l1_of_topic(hit) != l1:
                continue
            same.append(hit)
            if len(same) >= 2:
                break
        return dir_ids + same, unmatched
    return interests[:3], unmatched


def channel_exits(node_id: str) -> list[dict[str, str]]:
    path = ROOT / "knowledge" / "channels.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    want: list[str] = []
    if node_id.startswith("190"):
        want = ["psyarxiv"]
    elif node_id.startswith("790"):
        want = ["nep-repec", "nber-wp"]
    elif node_id.startswith("finance") or node_id == "finance":
        want = ["nber-wp"]
    elif node_id.startswith("310"):
        want = []
    out = []
    groups = data if isinstance(data, list) else (data.get("channels") or data.get("items") or [])
    if isinstance(groups, dict):
        groups = list(groups.values())
    for ch in groups:
        if not isinstance(ch, dict):
            continue
        if ch.get("id") in want:
            out.append({"id": ch.get("id", ""), "name": ch.get("name", ""), "url": ch.get("url", "")})
    if not out and want:
        fallback = {
            "psyarxiv": {"id": "psyarxiv", "name": "PsyArXiv", "url": "https://psyarxiv.com/"},
            "nep-repec": {"id": "nep-repec", "name": "NEP (RePEc)", "url": "https://nep.repec.org/"},
            "nber-wp": {"id": "nber-wp", "name": "NBER Working Papers", "url": "https://www.nber.org/papers"},
        }
        out = [fallback[w] for w in want if w in fallback]
    return out
