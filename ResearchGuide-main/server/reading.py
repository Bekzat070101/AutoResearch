# -*- coding: utf-8 -*-
"""研读层：每日情报（arXiv 分拣）、阅读卡（引文逐字核对）、综合矩阵、交给学生 Agent 的简报。

规矩（见 docs/DESIGN_PROPOSAL.md §1C）：
- 学生写主张、我会改什么、矩阵论证；模型和学生的 Agent 都不替他写。
- 引文必须逐字出自原文（quotes.locate），局限引文必须出自论文的局限 / 讨论 / 结论段。
- 矩阵的格子只来自学生自己过线的阅读卡；空格、空列、冲突由规则标出，不由模型推荐。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import arxiv
import interests
import quotes
import store
from schemas import UserFact, new_id, now_iso

ROOT = Path(__file__).resolve().parent.parent
KITS = ROOT / "knowledge" / "kits"
LIMIT_SECTIONS = {"limitations", "limitation", "future work", "discussion", "conclusion", "conclusions", "broader impact"}
OWN_MIN, OWN_MAX = 10, 160
DAILY_GOAL = 3  # 每天分拣几篇算完成——轻量，五分钟


class ReadingError(Exception):
    pass


# ---------- 领域工具包 ----------

def kits() -> dict[str, dict[str, Any]]:
    out = {}
    for f in sorted(KITS.glob("*.json")):
        k = json.loads(f.read_text(encoding="utf-8"))
        out[k["id"]] = k
    return out


def kit(kit_id: str) -> dict[str, Any]:
    k = kits().get(kit_id)
    if not k:
        raise ReadingError(f"没有这个工具包：{kit_id}")
    return k


def kit_paper(k: dict[str, Any], arxiv_id: str) -> dict[str, Any] | None:
    return next((p for p in k["papers"] if p["arxiv_id"] == arxiv_id), None)


# ---------- 每日情报：分拣今天的新论文 ----------

def daily_source(kit_id: str, cached_only: bool = False) -> tuple[list[dict[str, Any]], str] | None:
    """这个工具包今天的 arXiv 候选，或者取不到的原因。所有人共用同一份，失败也共用（不各自重试）。
    cached_only=True 时只看缓存，没有就返回 None。"""
    k = kit(kit_id)
    if cached_only:
        try:
            got = arxiv.recent(k["daily"]["categories"], k["daily"]["keywords"], max_results=25, cached_only=True)
        except arxiv.ArxivError:
            got = None  # 只看缓存这一步出任何错，都当作没命中，交给共享的取数那一步
        return None if got is None else (got, "")
    try:
        return arxiv.recent(k["daily"]["categories"], k["daily"]["keywords"], max_results=25), ""
    except arxiv.ArxivError as exc:
        return [], str(exc)


def arxiv_day() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat()


def topic_source(topic_id: str, cached_only: bool = False) -> tuple[list[dict[str, Any]], str, bool] | None:
    spec = interests.topic(topic_id)
    if not spec:
        return [], "没有这个话题", False
    day = arxiv_day()
    hit = store.get_paper_pool(topic_id, day)
    if hit:
        return hit["papers"], "", False
    old = store.latest_paper_pool(topic_id)
    if cached_only:
        if old:
            return old["papers"], "", True
        return None
    try:
        papers = arxiv.recent(spec.get("categories") or [], spec.get("en") or [], max_results=50) or []
        store.put_paper_pool(topic_id, day, papers)
        return papers, "", False
    except arxiv.ArxivError as exc:
        if old:
            return old["papers"], str(exc), True
        return [], str(exc), False


def _word_score(text: str, term: str) -> int:
    if not term:
        return 0
    if " " in term or not term.isalpha():
        return 1 if term.lower() in text.lower() else 0
    return 1 if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text) else 0


def card_kit_for(paper: dict[str, Any]) -> str | None:
    cats = set(paper.get("categories") or paper.get("category") and [paper.get("category")] or [])
    if not cats and paper.get("id"):
        cats = set()
    blob = f"{paper.get('title', '')} {paper.get('summary', '')}"
    for kid, k in kits().items():
        daily = k.get("daily") or {}
        kcats = set(daily.get("categories") or [])
        if not (cats & kcats) and cats:
            continue
        if any(_word_score(blob, kw) for kw in daily.get("keywords") or []):
            return kid
        if cats & kcats:
            return kid
    return None


def daily_for_user(uid: str, sources: dict[str, tuple[list, str, bool]] | None = None) -> dict[str, Any]:
    topic_ids, unmatched = interests.user_topics(uid)
    seen = {r["arxiv_id"]: r for r in store.list_triage_all(uid)}
    today = now_iso()[:10]
    done_today = [r for r in seen.values() if r["created_at"][:10] == today]
    keeps = [r for r in seen.values() if r["verdict"] == "keep"]
    empty = {
        "kit": None, "items": [], "error": "", "goal": DAILY_GOAL,
        "done_today": len(done_today), "kept_total": len(keeps), "triaged_total": len(seen),
        "recent_keeps": sorted(keeps, key=lambda r: r["created_at"], reverse=True)[:5],
        "topics": topic_ids, "unmatched": unmatched, "stale": False, "channels": [],
    }
    if not topic_ids:
        return {**empty, "empty_reason": "no_topics"}
    if topic_ids and all(interests.is_thin_topic(t) for t in topic_ids):
        trail = store.get_trail(uid, store.active_portrait_id(uid) or "default")
        return {**empty, "empty_reason": "thin", "channels": interests.channel_exits(str(trail.get("dir") or ""))}
    stale = False
    error = ""
    buckets: dict[str, list[dict[str, Any]]] = {t: [] for t in topic_ids}
    for tid in topic_ids:
        got = (sources or {}).get(tid)
        if got is None:
            got = topic_source(tid, cached_only=True)
            if got is None:
                got = topic_source(tid, cached_only=False)
        if got is None:
            continue
        papers, err, is_stale = got
        stale = stale or is_stale
        if err:
            error = err
        spec = interests.topic(tid) or {}
        for p in papers:
            if p.get("id") in seen:
                continue
            title = p.get("title") or ""
            abstract = p.get("summary") or ""
            hits = []
            score = 0
            for en, zh in zip(spec.get("en") or [], (spec.get("zh") or []) + [""] * 8):
                s = _word_score(title, en) * 2 + _word_score(abstract, en)
                if s:
                    hits.append({"term_zh": zh, "term_en": en})
                    score += s
            buckets[tid].append({
                "paper": p, "score": score, "hits": hits, "topic": tid,
                "published": p.get("published") or "", "arxiv_id": p.get("id") or "",
            })
        buckets[tid].sort(key=lambda x: (-x["score"], x["published"], x["arxiv_id"]), reverse=False)
        buckets[tid].sort(key=lambda x: (-x["score"], x["published"] == "", x["published"], x["arxiv_id"]))
    items = []
    used = set()
    while len(items) < 6:
        added = False
        for tid in topic_ids:
            while buckets[tid]:
                row = buckets[tid].pop(0)
                aid = row["arxiv_id"]
                if aid in used:
                    continue
                used.add(aid)
                p = row["paper"]
                items.append({
                    "arxiv_id": aid, "title": p.get("title"), "abstract": (p.get("summary") or "")[:600],
                    "published": p.get("published"), "authors": (p.get("authors") or [])[:4],
                    "url": p.get("url"), "hits": row["hits"], "topic": tid,
                    "card_kit": card_kit_for(p),
                })
                added = True
                break
            if len(items) >= 6:
                break
        if not added:
            break
    reason = ""
    if not items:
        reason = "upstream" if error else "exhausted"
    return {
        **empty, "items": items, "error": error, "stale": stale,
        "empty_reason": reason, "topics": topic_ids, "unmatched": unmatched,
    }


def daily(uid: str, kit_id: str, source: tuple[list[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    if not kit_id:
        return daily_for_user(uid)
    k = kit(kit_id)
    seen = {r["arxiv_id"]: r for r in store.list_triage(uid, kit_id)}
    today = now_iso()[:10]
    done_today = [r for r in seen.values() if r["created_at"][:10] == today]
    fresh, error = source if source is not None else daily_source(kit_id)
    items = [{"arxiv_id": p["id"], "title": p["title"], "abstract": p["summary"][:600], "published": p["published"],
              "authors": p["authors"][:4], "url": p["url"]} for p in fresh if p["id"] not in seen][:6]
    keeps = [r for r in seen.values() if r["verdict"] == "keep"]
    return {"kit": {"id": k["id"], "name": k["name"]}, "items": items, "error": error,
            "goal": DAILY_GOAL, "done_today": len(done_today), "kept_total": len(keeps), "triaged_total": len(seen),
            "recent_keeps": sorted(keeps, key=lambda r: r["created_at"], reverse=True)[:5]}


def triage(uid: str, kit_id: str, arxiv_id: str, verdict: str, why: str, title: str = "",
           source: tuple[list[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    kit_id = kit_id or "*daily"
    if kit_id != "*daily":
        kit(kit_id)
    aid = arxiv.clean_id(arxiv_id)
    if verdict not in ("keep", "skip"):
        raise ReadingError("只能选「留」或「跳过」")
    why = (why or "").strip()
    if not 4 <= len(why) <= 80:
        raise ReadingError("用一句话写为什么（4–80 字）：留下是因为它碰到了什么，跳过是因为它离你的问题多远")
    store.save_triage(uid, kit_id, aid, verdict, why, title[:200])
    if kit_id == "*daily":
        return daily_for_user(uid) | {"saved": aid}
    return daily(uid, kit_id, source) | {"saved": aid}


# ---------- 阅读卡 ----------

CARD_FIELDS = [
    {"key": "claim", "label": "主张", "own": True, "hint": "作者说自己做成了什么——用你自己的话，一两句"},
    {"key": "claim_quote", "label": "主张 · 原句", "quote": True, "hint": "从原文里选一句作者原话作锚点"},
    {"key": "evidence_quote", "label": "证据", "quote": True, "hint": "支撑主张的那句：哪张表、哪个数字"},
    {"key": "method", "label": "方法", "own": False, "hint": "怎么做的，一句"},
    {"key": "assumption", "label": "假设", "own": True, "hint": "作者默认成立、但没检验的事"},
    {"key": "limitation_quote", "label": "局限 · 原句", "quote": True, "hint": "必须出自论文的局限 / 讨论 / 结论段"},
    {"key": "change", "label": "我会改什么", "own": True, "hint": "一句，你自己的：换数据、加对照、改指标……"},
]


def _check_own(text: str, paper: dict[str, Any], label: str) -> tuple[bool, str]:
    t = (text or "").strip()
    if not OWN_MIN <= len(t) <= OWN_MAX:
        return False, f"「{label}」写 {OWN_MIN}–{OWN_MAX} 字，用自己的话"
    if len(t) >= 24 and quotes.locate(t, paper).get("found"):
        return False, f"「{label}」和原文一字不差——这一栏要你自己的话"
    return True, ""


def check_limitation(quote: str, paper: dict[str, Any]) -> dict[str, Any]:
    loc = quotes.locate(quote, paper)
    has_limit_section = any(n in LIMIT_SECTIONS for n, _ in arxiv.sections(paper.get("text") or ""))
    if loc.get("found") and has_limit_section and loc.get("section") not in LIMIT_SECTIONS:
        return {**loc, "pass": False,
                "reason": f"这句在「{loc.get('where', '')}」里。论文有局限 / 讨论段，局限要从那里引"}
    return {**loc, "pass": bool(loc.get("found"))}


def review_card(k: dict[str, Any], paper: dict[str, Any], fields: dict[str, str], dims: dict[str, str],
                decision_log: list[dict[str, str]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, ok: bool, note: str, where: str = "") -> None:
        checks.append({"key": key, "label": label, "pass": ok, "note": note, "where": where})

    ok, note = _check_own(fields.get("claim", ""), paper, "主张")
    add("claim", "主张用自己的话", ok, note or "有自己的概括。")
    for key, label in (("claim_quote", "主张有原句锚点"), ("evidence_quote", "证据能在原文找到")):
        loc = quotes.locate(fields.get(key, ""), paper)
        add(key, label, loc["found"], "已定位" if loc["found"] else loc["reason"], loc.get("where", ""))
    loc = check_limitation(fields.get("limitation_quote", ""), paper)
    add("limitation_quote", "局限出自作者自己的局限段", loc["pass"],
        "已定位" if loc["pass"] else loc.get("reason", ""), loc.get("where", ""))
    for key, label in (("assumption", "假设"), ("change", "我会改什么")):
        ok, note = _check_own(fields.get(key, ""), paper, label)
        add(key, f"「{label}」是自己写的", ok, note or "有自己的判断。")
    filled = [d for d in k["dimensions"] if (dims.get(d["key"]) or "").strip()]
    need = max(3, len(k["dimensions"]) - 2)
    add("dims", "矩阵维度填够", len(filled) >= need, f"填了 {len(filled)} / {len(k['dimensions'])} 个维度（至少 {need} 个）")
    bad = [i + 1 for i, d in enumerate(decision_log or [])
           if d.get("action") not in ("adopt", "modify", "reject") or (d.get("action") == "reject" and len((d.get("reason") or "").strip()) < 6)]
    if decision_log:
        add("decision_log", "Agent 建议逐条表态", not bad,
            "每条都有表态" if not bad else f"第 {'、'.join(map(str, bad))} 条没表态，或拒绝了却没写理由")
    passed = all(c["pass"] for c in checks)
    first = next((c for c in checks if not c["pass"]), None)
    return {"pass": passed, "checks": checks, "passed": sum(c["pass"] for c in checks), "total": len(checks),
            "next_step": "" if passed else f"先改「{first['label']}」：{first['note']}",
            "paper_source": paper.get("source"), "reviewed_at": now_iso()}


def _short(title: str, n: int = 40) -> str:
    return title if len(title) <= n else title[:n].rstrip() + "…"


def submit_card(uid: str, kit_id: str, arxiv_id: str, fields: dict[str, str], dims: dict[str, str],
                decision_log: list[dict[str, str]] | None = None) -> dict[str, Any]:
    k = kit(kit_id)
    aid = arxiv.clean_id(arxiv_id)
    try:
        paper = arxiv.fulltext(aid)
    except arxiv.ArxivError as exc:
        raise ReadingError(f"取不到原文：{exc}") from exc
    review = review_card(k, paper, fields, dims, decision_log or [])
    prev = store.latest_card(uid, kit_id, aid)
    version = (prev["version"] + 1) if prev else 1
    card = {"id": new_id(), "uid": uid, "kit_id": kit_id, "arxiv_id": aid, "title": paper["title"], "version": version,
            "fields": {f["key"]: (fields.get(f["key"]) or "").strip() for f in CARD_FIELDS},
            "dims": {d["key"]: (dims.get(d["key"]) or "").strip() for d in k["dimensions"]},
            "decision_log": decision_log or [], "review": review, "status": "pass" if review["pass"] else "revise",
            "prev_passed": prev["review"]["passed"] if prev else None}
    store.save_card(card)
    if review["pass"] and not (prev and prev["status"] == "pass"):
        f = UserFact(user_id=uid, category="capability", key=f"card:{kit_id}:{aid}",
                     value=f"已证明：能读《{_short(paper['title'])}》，定位作者自述的局限并写出自己的改动（阅读卡 v{version}）",
                     confidence=0.85, source="behavior",
                     evidence=[{"type": "reading_card", "card_id": card["id"], "task_title": _short(paper["title"]), "arxiv_id": aid}],
                     status="active")
        store.add_fact(f)
        card["fact"] = f.to_dict()
    return card


# ---------- 综合矩阵 ----------

DIRECTION = {"up": "↑", "down": "↓", "mixed": "~", "none": "?"}
CONFLICT_SHOW = 5  # 每组冲突每边列出几篇；总数另给


def matrix(uid: str, kit_id: str) -> dict[str, Any]:
    k = kit(kit_id)
    dims = k["dimensions"]
    rows = [c for c in store.latest_cards(uid, kit_id) if c["status"] == "pass"]
    table = [{"arxiv_id": c["arxiv_id"], "title": c["title"], "version": c["version"],
              "cells": {d["key"]: c["dims"].get(d["key"], "") for d in dims}} for c in rows]
    empty_cells = [(r["arxiv_id"], d["key"]) for r in table for d in dims if not r["cells"][d["key"]].strip()]
    empty_cols = [d["key"] for d in dims if table and sum(1 for r in table if not r["cells"][d["key"]].strip()) * 2 >= len(table)]
    # 冲突按指标分组：同一指标下有人「提升」、有人「下降」。原来逐对列出，两边各 250 篇就是六万多对
    conflicts = []
    metric_key = next((d["key"] for d in dims if d.get("role") == "metric"), None)
    dir_key = next((d["key"] for d in dims if d.get("role") == "direction"), None)
    if metric_key and dir_key:
        groups: dict[str, dict[str, Any]] = {}
        for r in table:
            m = r["cells"][metric_key].strip()
            if m and r["cells"][dir_key] in ("up", "down"):
                g = groups.setdefault(m.lower(), {"metric": m, "up": [], "down": []})
                g[r["cells"][dir_key]].append(r["arxiv_id"])
        in_conflict: set[str] = set()
        for g in groups.values():
            if g["up"] and g["down"]:
                conflicts.append({"metric": g["metric"], "up_total": len(g["up"]), "down_total": len(g["down"]),
                                  "up": g["up"][:CONFLICT_SHOW], "down": g["down"][:CONFLICT_SHOW]})
                in_conflict.update(g["up"] + g["down"])
        for r in table:  # 行高亮用完整成员，不用截断后的预览
            r["conflict"] = r["arxiv_id"] in in_conflict
    need = max(0, 1 - len(table))
    return {"kit": {"id": k["id"], "name": k["name"]}, "dimensions": dims, "rows": table,
            "flags": {"empty_cells": empty_cells, "empty_columns": empty_cols, "conflicts": conflicts},
            "ready": need == 0, "need_more": need}


# ---------- 交给学生自己的 Agent ----------

def brief(kit_id: str, arxiv_id: str) -> str:
    k = kit(kit_id)
    aid = arxiv.clean_id(arxiv_id)
    p = kit_paper(k, aid) or {"title": aid, "why": ""}
    dims = "\n".join(f"- {d['label']}：{d.get('hint', '')}" for d in k["dimensions"])
    fields = "\n".join(f"- {f['label']}：{f['hint']}" for f in CARD_FIELDS)
    return f"""# 启研 · 阅读卡简报（标准 reading-card v1 · 工具包 {k['id']} v{k['version']}）

你是一名本科生的研究助手。这名学生在为 arXiv:{aid}《{p['title']}》写一张阅读卡。卡由学生自己写，你只做下面允许的事。

## 原文
https://arxiv.org/abs/{aid} （HTML 版：https://arxiv.org/html/{aid}）

## 卡的栏目（学生交到启研）
{fields}

矩阵维度（每篇卡都填，用来和别的论文对比）：
{dims}

## 你可以做
- 解释论文里学生看不懂的概念、公式、实验设置；
- 对学生写好的栏目挑错：主张有没有超出证据、局限有没有漏掉作者自己写的那段、假设是不是其实被检验过；
- 指出原文位置（节号 + 原句），让学生自己去引。

## 你不能做
- 不替学生写「主张」「假设」「我会改什么」这三栏，也不给可以直接抄的版本；
- 不编引文：每句引用必须逐字出自原文，给出所在节；找不到就说找不到；
- 思考类问题先让学生回答，再评价和追问。

## 交回格式
把你的每条建议编号（A1、A2…），一条一行。学生会在启研的「决策日志」里逐条标「采纳 / 修改 / 拒绝」并写理由；启研会逐字核对你给的每句引文，找不到的会被标红并阻断过线。

## 轨迹
如果学生要附对话轨迹，由学生复制真实对话，你不要生成摘要冒充轨迹。
"""
