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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import arxiv
import biorxiv
import interests
import osf
import quotes
import store
import reading_memory
from schemas import UserFact, new_id, now_iso

OSF_KEY = re.compile(r"^(psyarxiv|socarxiv):[a-z0-9]{5,}(?:_v\d+)?$", re.I)

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
    ids = [p.get("arxiv_id") for p in k.get("papers") or [] if p.get("arxiv_id")]
    lib = store.paper_lib(ids)
    for p in k.get("papers") or []:
        p["lib"] = lib.get(p.get("arxiv_id") or "")
    for op in k.get("open_problems") or []:
        op["section_label"] = arxiv.section_cn(op.get("section") or "")
    return k


def kit_title_zh(aid: str) -> tuple[str, list]:
    for k in kits().values():
        p = kit_paper(k, aid)
        if p and ((p.get("title_zh") or "").strip() or p.get("keywords")):
            return (p.get("title_zh") or "").strip(), list(p.get("keywords") or [])
    return "", []


def search_library(q: str, limit: int = 20) -> list[dict[str, Any]]:
    items = []
    lib_ids = []
    for p in store.search_paper_dir(q, limit=limit):
        pid = p.get("id") or p.get("paper_id") or ""
        if not pid:
            continue
        lib_ids.append(pid)
        items.append({
            **p, "id": pid, "card_kit": card_kit_for(p), "lib": "",
            "in_kit": any(kit_paper(k, pid) for k in kits().values()),
        })
    lib = store.paper_lib(lib_ids)
    for it in items:
        it["lib"] = lib.get(it["id"] or "", "")
        zh = store.get_paper_zh(it["id"] or "") or {}
        if not it.get("title_zh"):
            kit_zh, _ = kit_title_zh(it["id"] or "")
            it["title_zh"] = (zh.get("title_zh") or "") or kit_zh
    return items[:limit]


def dir_as_papers(node: str) -> list[dict[str, Any]]:
    out = []
    for row in store.list_paper_dir(node):
        out.append({
            "id": row["paper_id"], "title": row["title"], "summary": row["summary"],
            "published": row["published"], "authors": row["authors"], "url": row["url"],
            "categories": row["categories"], "source": row["source"], "tags": [],
        })
    return out


def warm_targets() -> list[tuple[str, bool, dict]]:
    want: dict[str, tuple[bool, dict]] = {}
    for k in kits().values():
        for p in k.get("papers") or []:
            aid = p.get("arxiv_id")
            if aid:
                want[aid] = (True, {"title": p.get("title") or "", "summary": ""})
    try:
        counts = store.paper_dir_counts()
        thin = set((interests.table().get("thin") or []))
        for node, _n in counts.items():
            quota = 4 if node in thin else 8
            for row in store.list_paper_dir(node)[:quota]:
                pid = row["paper_id"]
                if pid.startswith("local:"):
                    continue
                want.setdefault(pid, (True, {"title": row.get("title") or "", "summary": row.get("summary") or ""}))
    except Exception:
        pass
    with store._conn() as c:
        for table, col in (("triage", "arxiv_id"), ("cards", "arxiv_id"), ("tutor_sessions", "arxiv_id")):
            try:
                rows = c.execute(f"SELECT DISTINCT {col} FROM {table}").fetchall()
            except Exception:
                continue
            for r in rows:
                aid = r[0] or ""
                if aid and not aid.startswith("local:"):
                    want.setdefault(aid, (False, {}))
    out = []
    for aid, (pinned, hint) in want.items():
        row = store.get_paper_text(aid)
        if row:
            if row.get("source") in ("html", "pdf") and not row.get("needs_reparse"):
                if int(row.get("pinned") or 0):
                    continue
                out.append((aid, True, hint))
                continue
            if row.get("source") == "abstract" and time.time() < float(row.get("retry_after") or 0):
                continue
        out.append((aid, pinned, hint))
    return out


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


def warm_fetch(aid: str, hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """预热取正文：按 id 前缀分派（arXiv / OSF / bioRxiv）。osf/biorxiv 的 fulltext 自己落库。"""
    if osf.is_id(aid):
        return osf.fulltext(aid)
    if biorxiv.is_id(aid):
        return biorxiv.fulltext(aid)
    return arxiv.fulltext(aid, hint=hint)


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
        if spec.get("source") == "osf":
            papers = osf.recent(spec.get("provider") or "psyarxiv", spec.get("subjects") or [], max_results=25) or []
        elif spec.get("source") == "biorxiv":
            papers = biorxiv.recent(spec.get("server") or "biorxiv", spec.get("biorxiv_category") or "",
                                    spec.get("en") or [], max_results=25) or []
        else:
            papers = arxiv.recent(spec.get("categories") or [], spec.get("en") or [], max_results=50) or []
        store.put_paper_pool(topic_id, day, papers)
        schedule_xlate(papers[:12], spec)
        return papers, "", False
    except (arxiv.ArxivError, osf.OsfError, biorxiv.BiorxivError) as exc:
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
        if (cats & kcats) and any(_word_score(blob, kw) for kw in daily.get("keywords") or []):
            return kid
    return None


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()


def _attach_zh(p: dict[str, Any], spec: dict[str, Any]) -> tuple[str, list[str]]:
    key = p.get("id") or ""
    row = store.get_paper_zh(key) if key else None
    title_zh = (row or {}).get("title_zh") or ""
    kws = list((row or {}).get("keywords") or [])
    if spec.get("source") == "osf" and not kws:
        kws = [str(t) for t in (p.get("tags") or []) if str(t).strip()][:5]
    return title_zh, kws[:5]


def _item_of(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    p = row["paper"]
    key = row["arxiv_id"]
    title_zh, kws = _attach_zh(p, spec)
    src = p.get("source") or spec.get("provider") or spec.get("source") or "arxiv"
    return {
        "arxiv_id": key, "key": key, "title": p.get("title"), "abstract": (p.get("summary") or "")[:600],
        "published": p.get("published"), "authors": (p.get("authors") or [])[:4],
        "url": p.get("url"), "hits": row["hits"], "topic": row["topic"],
        "card_kit": card_kit_for(p), "source": src, "title_zh": title_zh, "keywords": kws,
    }


def daily_for_user(uid: str, sources: dict[str, tuple[list, str, bool]] | None = None) -> dict[str, Any]:
    topic_ids, unmatched = interests.user_topics(uid)
    nodes = interests.direction_nodes(uid)
    node = nodes[0] if nodes else ""
    label = interests.direction_label(nodes) or interests.direction_name(node)
    seen = {r["arxiv_id"]: r for r in store.list_triage_all(uid)}
    today = now_iso()[:10]
    done_today = [r for r in seen.values() if r["created_at"][:10] == today]
    keeps = [r for r in seen.values() if r["verdict"] == "keep"]
    empty = {
        "kit": None, "items": [], "error": "", "goal": DAILY_GOAL,
        "done_today": len(done_today), "kept_total": len(keeps), "triaged_total": len(seen),
        "recent_keeps": sorted(keeps, key=lambda r: r["created_at"], reverse=True)[:5],
        "topics": topic_ids, "unmatched": unmatched, "stale": False, "channels": [],
        "direction": {"node": node, "name": label, "nodes": nodes} if nodes else {"node": "", "name": "", "nodes": []},
    }
    if not topic_ids:
        return {**empty, "empty_reason": "no_topics"}
    # 组合里每个方向各取前两个话题；目录论文按所属方向挂到对应话题桶
    dir_topics: list[str] = []
    for n in nodes:
        for tid in (interests.table()["nodes"].get(n) or [])[:2]:
            if tid not in dir_topics:
                dir_topics.append(tid)
    catalog: list[tuple[str, dict[str, Any]]] = []
    for n in nodes:
        for p in dir_as_papers(n):
            catalog.append((n, p))
    thin_ids = dir_topics or topic_ids
    if not catalog and thin_ids and all(interests.is_thin_topic(t) for t in thin_ids):
        return {**empty, "empty_reason": "thin", "channels": interests.channel_exits(node)}
    stale = False
    error = ""
    buckets: dict[str, list[dict[str, Any]]] = {t: [] for t in topic_ids}
    seen_ids: set[str] = set()
    node_topics = {n: (interests.table()["nodes"].get(n) or [])[:2] for n in nodes}
    if catalog:
        for n, p in catalog:
            tids = node_topics.get(n) or []
            tid = tids[0] if tids else None
            if not tid:
                continue
            spec = interests.topic(tid) or {}
            buckets.setdefault(tid, [])
            pid = p.get("id") or ""
            if pid in seen or pid in seen_ids:
                continue
            seen_ids.add(pid)
            title = p.get("title") or ""
            abstract = p.get("summary") or ""
            hits, score = [], 0
            for en, zh in zip(spec.get("en") or [], (spec.get("zh") or []) + [""] * 8):
                s = _word_score(title, en) * 2 + _word_score(abstract, en)
                if s:
                    hits.append({"term_zh": zh, "term_en": en})
                    score += s
            buckets[tid].append({
                "paper": p, "score": score, "hits": hits, "topic": tid,
                "published": p.get("published") or "", "arxiv_id": p.get("id") or "",
            })
    for tid in topic_ids:
        got = (sources or {}).get(tid)
        if got is None:
            got = topic_source(tid, cached_only=True)
        if got is None:
            continue
        papers, err, is_stale = got
        stale = stale or is_stale
        if err:
            error = err
        spec = interests.topic(tid) or {}
        for p in papers:
            pid = p.get("id") or ""
            if pid in seen or pid in seen_ids:
                continue
            seen_ids.add(pid)
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
        buckets[tid].sort(key=lambda x: (x["score"], x["published"] or "", x["arxiv_id"]), reverse=True)
    cutoff = _cutoff()
    recent_buckets = {t: [r for r in buckets[t] if (r["published"] or "9999") >= cutoff] for t in topic_ids}
    old_buckets = {t: [r for r in buckets[t] if (r["published"] or "") < cutoff] for t in topic_ids}

    used: set[str] = set()

    def take(from_ids: list[str], n: int, pool: dict[str, list]) -> list[dict[str, Any]]:
        items = []
        while len(items) < n:
            added = False
            for tid in from_ids:
                while pool.get(tid):
                    row = pool[tid].pop(0)
                    if row["arxiv_id"] in used:
                        continue
                    used.add(row["arxiv_id"])
                    items.append(row)
                    added = True
                    break
                if len(items) >= n:
                    break
            if not added:
                break
        return items

    side = [t for t in topic_ids if t not in dir_topics]
    primary, extra = (dir_topics or topic_ids), (side if dir_topics else [])
    want_dir, want_side = (4, 2) if dir_topics else (6, 0)
    picked = take(primary, want_dir, recent_buckets)
    picked += take(extra or primary, want_side, recent_buckets)
    if len(picked) < 3:
        picked += take(primary + extra, 6 - len(picked), old_buckets)
        dated = True
    else:
        dated = False
    items = []
    for row in picked[:6]:
        spec = interests.topic(row["topic"]) or {}
        it = _item_of(row, spec)
        if dated and (row["published"] or "") < cutoff:
            it["old"] = True
        items.append(it)
    reason = ""
    if not items:
        reason = "upstream" if error else "exhausted"
    lib = store.paper_lib([it["arxiv_id"] for it in items])
    for it in items:
        it["lib"] = lib.get(it["arxiv_id"], "")
    return {
        **empty, "items": items, "error": error, "stale": stale,
        "empty_reason": reason, "topics": topic_ids, "unmatched": unmatched,
    }


def schedule_xlate(papers: list[dict[str, Any]], spec: dict[str, Any] | None = None) -> None:
    """展示请求不阻塞；缺译的后台补。没配模型就跳过。"""
    try:
        import llm
        if not llm.enabled(None):
            return
    except Exception:
        return
    batch = []
    for p in papers[:6]:
        key = p.get("id") or ""
        if not key:
            continue
        row = store.get_paper_zh(key)
        if row and (row.get("ok") or _zh_fresh(row)):
            continue
        batch.append({"key": key, "title": p.get("title") or "", "abstract": (p.get("summary") or "")[:600],
                      "tags": p.get("tags") or [], "osf": (spec or {}).get("source") == "osf"})
    if batch:
        try:
            apply_xlate(batch)
        except Exception:
            pass


def _zh_fresh(row: dict[str, Any]) -> bool:
    tried = str(row.get("tried_at") or "")
    return bool(tried) and tried[:10] >= (datetime.now(timezone.utc) - timedelta(hours=24)).date().isoformat()


def _valid_xlate_item(it: Any, allowed: set[str], titles: dict[str, str]) -> dict[str, Any] | None:
    if not isinstance(it, dict):
        return None
    key = str(it.get("key") or "")
    zh = str(it.get("title_zh") or "").strip()
    if key not in allowed or not (2 <= len(zh) <= 60):
        return None
    if zh == (titles.get(key) or "") or "\n" in zh or "<" in zh or ">" in zh:
        return None
    if sum(1 for c in zh if "\u4e00" <= c <= "\u9fff") < 2:
        return None
    kws = it.get("keywords") if isinstance(it.get("keywords"), list) else []
    out_kw, seen = [], set()
    for k in kws:
        s = str(k).strip()
        if not (1 <= len(s) <= 16) or s in seen:
            continue
        seen.add(s)
        out_kw.append(s)
        if len(out_kw) >= 5:
            break
    return {"key": key, "title_zh": zh, "keywords": out_kw}


def apply_xlate(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import llm
    import budget
    allowed = {b["key"] for b in batch}
    titles = {b["key"]: b["title"] for b in batch}
    user = json.dumps([{"key": b["key"], "title": b["title"], "abstract": b["abstract"]} for b in batch], ensure_ascii=False)
    with budget.metered_as("*xlate"):
        if not budget.allowed():
            return []
        data = llm.chat_json("把论文标题译成中文，并给不超过 5 个中文关键词。", user,
                             timeout=20, total=30, retry=False, tag="xlate", uid=None)
    now = now_iso()
    ok_items = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        for it in data["items"]:
            got = _valid_xlate_item(it, allowed, titles)
            if got:
                store.put_paper_zh(got["key"], got["title_zh"], got["keywords"], model="xlate", ok=1)
                ok_items.append(got)
                allowed.discard(got["key"])
    for key in allowed:
        store.put_paper_zh(key, "", [], model="xlate", ok=0, tried_at=now)
    return ok_items


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
    lib = store.paper_lib([it["arxiv_id"] for it in items])
    for it in items:
        it["lib"] = lib.get(it["arxiv_id"], "")
    keeps = [r for r in seen.values() if r["verdict"] == "keep"]
    return {"kit": {"id": k["id"], "name": k["name"]}, "items": items, "error": error,
            "day": today,  # done_today 是哪一天的：前端同一天里只往大里改，换了一天就照新的来
            "goal": DAILY_GOAL, "done_today": len(done_today), "kept_total": len(keeps), "triaged_total": len(seen),
            "recent_keeps": sorted(keeps, key=lambda r: r["created_at"], reverse=True)[:5]}


def triage(uid: str, kit_id: str, arxiv_id: str, verdict: str, why: str, title: str = "",
           source: tuple[list[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    kit_id = kit_id or "*daily"
    if kit_id != "*daily":
        kit(kit_id)
    raw = (arxiv_id or "").strip()
    if biorxiv.is_id(raw):
        aid = biorxiv.clean_id(raw)
    else:
        aid = osf.clean_id(raw) if osf.is_id(raw) else arxiv.clean_id(raw)
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
    loc = quotes.locate(quote, paper, prefer=LIMIT_SECTIONS)  # 同一句多处出现时，有一处在局限段就算那一处（Codex 复现）
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
        paper = store.get_user_paper(uid, aid) if arxiv.is_local(aid) else arxiv.fulltext(aid)
        if not paper:
            raise ReadingError("当前画像没有这篇论文")
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
    reading_memory.commit_card(card, paper)
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
