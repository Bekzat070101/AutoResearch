# -*- coding: utf-8 -*-
"""按方向树节点检索论文元数据/正文，写入 paper_dir（不是 paper_pool）。

只在 ephemeral=false 的库上跑。日志 JSON 行 event=paper_collect。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arxiv  # noqa: E402
import interests  # noqa: E402
import osf  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402


class CollectError(Exception):
    pass


def _log(payload: dict[str, Any]) -> None:
    print(json.dumps({"event": "paper_collect", **payload}, ensure_ascii=False), flush=True)


def _pause() -> None:
    time.sleep(3.0)


def _uniq(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for p in papers:
        pid = (p.get("id") or "").strip()
        title = (p.get("title") or "").strip()
        if not pid or not title or pid in seen:
            continue
        seen.add(pid)
        out.append(p)
    return out


def _fetch_topic(tid: str, max_results: int = 40) -> tuple[list[dict[str, Any]], str]:
    spec = interests.topic(tid) or {}
    if not spec:
        return [], "没有这个话题"
    try:
        if spec.get("source") == "osf":
            got = osf.recent(spec.get("provider") or "psyarxiv", spec.get("subjects") or [],
                             max_results=max_results)
            _pause()
            return got or [], ""
        got = arxiv.recent(spec.get("categories") or [], spec.get("en") or [],
                           max_results=max_results) or []
        return got, ""
    except (arxiv.ArxivError, osf.OsfError) as exc:
        if spec.get("source") == "osf":
            _pause()
        return [], str(exc)


def _fetch_node(node_id: str, quota: int) -> tuple[list[dict[str, Any]], list[str]]:
    reasons: list[str] = []
    papers: list[dict[str, Any]] = []
    nodes = interests.table()["nodes"]
    tids = list(nodes.get(node_id) or [])
    for tid in tids:
        got, err = _fetch_topic(tid, 40)
        if err:
            reasons.append(f"{tid}: {err}")
        papers.extend(got)
        papers = _uniq(papers)
        if len(papers) >= quota:
            return papers[: max(quota, 40)], reasons
    parent = node_id.rsplit(".", 1)[0] if "." in node_id else ""
    if len(papers) < quota and parent and parent in nodes:
        for tid in nodes[parent]:
            if tid in tids:
                continue
            got, err = _fetch_topic(tid, 40)
            if err:
                reasons.append(f"widen {tid}: {err}")
            papers.extend(got)
            papers = _uniq(papers)
            if len(papers) >= quota:
                break
        if len(papers) < quota:
            reasons.append(f"widen {parent} 仍不足")
    if not papers and not reasons:
        reasons.append("上游空")
    elif len(papers) < quota:
        reasons.append(f"目录 {len(papers)} < {quota}")
    return papers, reasons


def _write_dir(node_id: str, papers: list[dict[str, Any]], topic_id: str) -> int:
    n = 0
    for i, p in enumerate(papers, start=1):
        pid = p.get("id") or ""
        if not pid:
            continue
        store.put_paper_dir(
            node_id, pid, topic_id or "", i,
            p.get("source") or ("psyarxiv" if str(pid).startswith("psyarxiv:") else "arxiv"),
            p.get("title") or "", p.get("summary") or "",
            p.get("authors") or [], p.get("published") or "", p.get("url") or "",
            p.get("categories") or [],
        )
        n += 1
    return n


def _is_arxiv_id(pid: str) -> bool:
    try:
        arxiv.clean_id(pid)
        return True
    except arxiv.ArxivError:
        return False


def _fulltext_node(node_id: str, want: int) -> dict[str, int]:
    html_or_pdf = abstract_only = failed = 0
    rows = store.list_paper_dir(node_id)
    for row in rows:
        if html_or_pdf >= want:
            break
        pid = row["paper_id"]
        existing = store.get_paper_text(pid)
        if existing and existing.get("source") in ("html", "pdf") and not existing.get("needs_reparse"):
            store.put_paper_text(pid, existing, pinned=True)
            html_or_pdf += 1
            continue
        if not _is_arxiv_id(pid):
            abstract_only += 1
            continue
        try:
            got = arxiv.fulltext(pid, hint={"title": row.get("title") or "", "summary": row.get("summary") or ""})
            src = (got or {}).get("source") or ""
            if src in ("html", "pdf"):
                store.put_paper_text(pid, got, pinned=True)
                html_or_pdf += 1
            else:
                abstract_only += 1
        except Exception as exc:
            failed += 1
            _log({"phase": "fulltext", "node": node_id, "paper_id": pid, "error": str(exc)[:200]})
    return {"html_or_pdf": html_or_pdf, "abstract_only": abstract_only, "failed": failed}


def _xlate_node(node_id: str, limit: int = 20) -> int:
    try:
        import llm
        if not llm.enabled(None):
            return 0
    except Exception:
        return 0
    n = 0
    for row in store.list_paper_dir(node_id)[:limit]:
        pid = row["paper_id"]
        if (store.get_paper_zh(pid) or {}).get("title_zh"):
            continue
        reading.schedule_xlate([{"id": pid, "title": row.get("title") or ""}], None)
        n += 1
    return n


def _pin_kit() -> int:
    n = 0
    for k in reading.kits().values():
        for p in k.get("papers") or []:
            aid = p.get("arxiv_id")
            if not aid:
                continue
            row = store.get_paper_text(aid)
            if row and row.get("source") in ("html", "pdf"):
                store.put_paper_text(aid, row, pinned=True)
                n += 1
                continue
            if not _is_arxiv_id(aid):
                continue
            try:
                got = arxiv.fulltext(aid, hint={"title": p.get("title") or "", "summary": ""})
                if (got or {}).get("source") in ("html", "pdf"):
                    store.put_paper_text(aid, got, pinned=True)
                    n += 1
            except Exception as exc:
                _log({"phase": "kit_pin", "paper_id": aid, "error": str(exc)[:200]})
    return n


def node_quota(node_id: str, quota: int, fulltext: int) -> tuple[int, int]:
    thin = node_id in set(interests.table().get("thin") or [])
    return (8 if thin else quota, 4 if thin else fulltext)


def collect_node(node_id: str, quota: int = 20, fulltext: int = 8, xlate: bool = False,
                 metadata_only: bool = False) -> dict[str, Any]:
    if node_id not in interests.table()["nodes"]:
        raise CollectError(f"没有这个节点：{node_id}")
    q, ft = node_quota(node_id, quota, fulltext)
    topic_id = (interests.table()["nodes"].get(node_id) or [""])[0]
    have = len(store.list_paper_dir(node_id))
    reasons: list[str] = []
    if have < q:
        papers, reasons = _fetch_node(node_id, q)
        _write_dir(node_id, papers, topic_id)
    dir_n = len(store.list_paper_dir(node_id))
    ft_stats = {"html_or_pdf": 0, "abstract_only": 0, "failed": 0}
    if not metadata_only and ft:
        ft_stats = _fulltext_node(node_id, ft)
        html_n = 0
        for row in store.list_paper_dir(node_id):
            t = store.get_paper_text(row["paper_id"])
            if t and t.get("source") in ("html", "pdf"):
                html_n += 1
        ft_stats["html_or_pdf"] = html_n
        if html_n < ft:
            src0 = (interests.topic(topic_id) or {}).get("source") or "arxiv"
            if src0 == "osf":
                reasons.append("OSF 预印本多数只有摘要，正文定额未满")
            elif not reasons:
                reasons.append("正文不足")
    xlated = 0
    if xlate and not metadata_only:
        xlated = _xlate_node(node_id)
    rec = {
        "node": node_id, "thin": node_id in set(interests.table().get("thin") or []),
        "dir_n": dir_n, "need_dir": q, "need_text": ft, **ft_stats,
        "reasons": reasons, "xlate": xlated,
    }
    _log({"phase": "node", **rec})
    return rec


def collect(node: str = "", quota: int = 20, fulltext: int = 8, xlate: bool = False,
            metadata_only: bool = False, pin_kit: bool = True) -> dict[str, Any]:
    store.init_db()
    if store.ephemeral():
        raise CollectError("ephemeral 库禁止 collect，避免往生产 /tmp 灌库")
    nodes = [node] if node else list(interests.table()["nodes"])
    if node and node not in interests.table()["nodes"]:
        raise CollectError(f"没有这个节点：{node}")
    out = []
    for nid in nodes:
        try:
            out.append(collect_node(nid, quota=quota, fulltext=fulltext, xlate=xlate,
                                    metadata_only=metadata_only))
        except CollectError:
            raise
        except Exception as exc:
            rec = {"node": nid, "dir_n": len(store.list_paper_dir(nid)), "error": str(exc)[:200],
                   "reasons": [str(exc)]}
            _log({"phase": "node_fail", **rec})
            out.append(rec)
    kit_n = 0
    if pin_kit and not metadata_only:
        kit_n = _pin_kit()
    return {"ok": True, "nodes": out, "kit_pinned": kit_n, "ephemeral": store.ephemeral()}


def report() -> dict[str, Any]:
    store.init_db()
    thin = set(interests.table().get("thin") or [])
    rows = []
    for nid in interests.table()["nodes"]:
        catalog = store.list_paper_dir(nid)
        html_n = abstract_n = 0
        for r in catalog:
            t = store.get_paper_text(r["paper_id"])
            if t and t.get("source") in ("html", "pdf"):
                html_n += 1
            else:
                abstract_n += 1
        need_dir, need_text = (8, 4) if nid in thin else (20, 8)
        reasons = []
        if len(catalog) < need_dir:
            reasons.append("目录不足" if catalog else "上游空或未收集")
        if html_n < need_text:
            spec = interests.topic((interests.table()["nodes"].get(nid) or [""])[0]) or {}
            if spec.get("source") == "osf":
                reasons.append("OSF 多数只有摘要")
            elif catalog:
                reasons.append("正文不足")
        rows.append({
            "node": nid, "thin": nid in thin, "dir_n": len(catalog),
            "html_or_pdf": html_n, "abstract_only": abstract_n,
            "need_dir": need_dir, "need_text": need_text, "reasons": reasons,
        })
    return {"ephemeral": store.ephemeral(), "nodes": rows}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--node", default="")
    p.add_argument("--all", action="store_true")
    p.add_argument("--report", action="store_true")
    p.add_argument("--quota", type=int, default=20)
    p.add_argument("--fulltext", type=int, default=8)
    p.add_argument("--metadata-only", action="store_true")
    p.add_argument("--no-xlate", action="store_true")
    p.add_argument("--no-kit", action="store_true")
    args = p.parse_args()
    store.init_db()
    if args.report and not args.all and not args.node:
        rec = report()
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return
    if not args.all and not args.node:
        p.error("需要 --node、--all 或 --report")
    rec = collect(node="" if args.all else args.node, quota=args.quota, fulltext=args.fulltext,
                  xlate=not args.no_xlate, metadata_only=args.metadata_only,
                  pin_kit=not args.no_kit)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    if args.report:
        print(json.dumps(report(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
