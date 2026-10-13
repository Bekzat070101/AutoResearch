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
    nodes = table()["nodes"]
    thin = set(table().get("thin") or [])
    homes = [nid for nid, ids in nodes.items() if tid in ids]
    return bool(homes) and all(n in thin for n in homes)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _match_topic(slug: str, value: str) -> str | None:
    slug = (slug or "").lower()
    val = _norm(value)
    best = None
    for tid, spec in table()["topics"].items():
        slugs = [s.lower() for s in spec.get("slugs") or []]
        zh = [_norm(z) for z in spec.get("zh") or []]
        if slug and slug in slugs:
            return tid
        if val and val in zh:
            best = tid
        elif val and any(val in z or z in val for z in zh if len(z) >= 2):
            best = best or tid
    return best


def user_topics(uid: str) -> tuple[list[str], list[str]]:
    """兴趣 + 方向节点 + 工具包，按上限截断。返回 (topic_ids, unmatched_terms)。"""
    ids: list[str] = []
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
            if hit not in ids:
                ids.append(hit)
        else:
            term = tail or (f.value or "")[:20]
            if term:
                unmatched.append(term)
                UNMATCHED[term] += 1
    ids = ids[:3]

    trail = store.get_trail(uid, store.active_portrait_id(uid) or "default")
    node = str(trail.get("dir") or "")
    nodes = table()["nodes"]
    if node and node in nodes:
        for tid in nodes[node]:
            if tid not in ids:
                ids.append(tid)
                break
    else:
        for f in facts:
            if (f.key or "").startswith("direction:"):
                code = f.key.split(":", 1)[1]
                legacy = table()["legacy"].get(code)
                if legacy and legacy in nodes:
                    for tid in nodes[legacy]:
                        if tid not in ids:
                            ids.append(tid)
                        break
                break

    kit_id = store.get_prefs(uid).get("kit_id") or ""
    if kit_id and kit_id in table()["topics"] and kit_id not in ids:
        ids.append(kit_id)
    return ids[:5], unmatched


def channel_exits(node_id: str) -> list[dict[str, str]]:
    path = ROOT / "knowledge" / "channels.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    want = []
    if node_id.startswith("190"):
        want = ["psyarxiv"]
    elif node_id.startswith("790"):
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
    return out
