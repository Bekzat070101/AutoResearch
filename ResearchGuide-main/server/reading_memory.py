# -*- coding: utf-8 -*-
"""Portrait-owned reading records. Only accepted cards establish completion.

Ownership uses store's existing live-table/portrait-snapshot protocol; callers
must hold the user lock for writes, including portrait activation.
"""
from __future__ import annotations

import json
import re
from typing import Any

import store
from schemas import UserFact, new_id, now_iso


def document(card: dict, paper: dict) -> dict:
    return {
        "paper_id": card["arxiv_id"], "kit_id": card["kit_id"],
        "title": paper.get("title") or card["title"],
        "url": paper.get("url") or "", "abstract": paper.get("summary") or "",
        "card_id": card["id"], "card_version": card["version"],
        "completed_at": card.get("created_at") or now_iso(),
        "notes": card.get("fields") or {}, "dimensions": card.get("dims") or {},
        "evidence_kind": "passed_reading_card",
    }


def _put(c, uid: str, doc: dict) -> None:
    c.execute(
        "INSERT INTO read_documents(user_id,paper_id,data,completed_at,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id,paper_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
        (uid, doc["paper_id"], json.dumps(doc, ensure_ascii=False), doc["completed_at"], now_iso()),
    )


def commit_card(card: dict, paper: dict) -> None:
    """Card, reading index, behavioral evidence and version commit together."""
    uid = card["uid"]
    card.setdefault("created_at", now_iso())
    with store._LOCK, store._conn() as c:
        c.execute(
            "INSERT INTO cards(id,user_id,kit_id,arxiv_id,version,data,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (card["id"], uid, card["kit_id"], card["arxiv_id"], card["version"],
             json.dumps(card, ensure_ascii=False), card["status"], card["created_at"]),
        )
        if card["status"] != "pass":
            return
        doc = document(card, paper)
        _put(c, uid, doc)
        key = f"card:{card['kit_id']}:{card['arxiv_id']}"
        evidence = [{"type": "reading_card", "card_id": card["id"], "arxiv_id": card["arxiv_id"], "task_title": doc["title"]}]
        value = f"已证明：已完成《{doc['title']}》的阅读卡（v{card['version']}），已通过引文和阅读记录核对"
        old = c.execute("SELECT id FROM facts WHERE user_id=? AND key=? AND status IN ('active','confirmed')",
                        (uid, key)).fetchone()
        if old:
            c.execute("UPDATE facts SET value=?,evidence=?,updated_at=? WHERE id=?",
                      (value, json.dumps(evidence), now_iso(), old["id"]))
        else:
            f = UserFact(user_id=uid, category="experience", key=key, value=value,
                         confidence=0.85, source="behavior", evidence=evidence, status="active")
            c.execute(
                "INSERT INTO facts(id,user_id,category,key,value,confidence,source,evidence,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f.id, uid, f.category, key, value, f.confidence, f.source, json.dumps(evidence),
                 f.status, f.created_at, f.updated_at),
            )
            card["fact"] = f.to_dict()
        c.execute("UPDATE users SET memory_version=memory_version+1 WHERE id=?", (uid,))


def search(uid: str, query: str = "", limit: int = 5) -> dict:
    limit = max(1, min(int(limit), 20))
    with store._conn() as c:
        rows = c.execute("SELECT data FROM read_documents WHERE user_id=? ORDER BY updated_at DESC, rowid DESC",
                         (uid,)).fetchall()
    docs = [json.loads(r["data"]) for r in rows]
    # Include CJK bigrams so natural Chinese questions can match a title/note.
    terms = set(re.findall(r"[a-z0-9][a-z0-9.:/-]*|[\u4e00-\u9fff]{2,}", query.lower()))
    terms |= {s[i:i + 2] for s in re.findall(r"[\u4e00-\u9fff]{2,}", query) for i in range(len(s) - 1)}
    ranked = []
    for d in docs:
        text = json.dumps(d, ensure_ascii=False).lower()
        score = sum(1 for t in terms if t in text)
        if score or not query.strip():
            ranked.append((score, d))
    ranked.sort(key=lambda x: x[0], reverse=True)
    items = [{k: d.get(k) for k in ("paper_id", "kit_id", "title", "url", "card_id", "card_version", "completed_at")}
             | {"abstract": str(d.get("abstract") or "")[:600],
                "notes": {k: str(v)[:350] for k, v in (d.get("notes") or {}).items()}}
             for _, d in ranked[:limit]]
    return {"ok": True, "total": len(docs), "matched_count": len(ranked), "items": items}


def detail(uid: str, paper_id: str) -> dict:
    with store._conn() as c:
        row = c.execute("SELECT data FROM read_documents WHERE user_id=? AND paper_id=?", (uid, paper_id)).fetchone()
    if not row:
        return {"ok": False, "error": "当前画像没有这篇已读文献"}
    doc = json.loads(row["data"])
    if paper_id.startswith("local:") and not store.get_user_paper(uid, paper_id):
        return {"ok": False, "error": "当前画像已无法访问这份私有论文"}
    return {"ok": True, "document": doc,
            "note": "notes 为用户通过核对的阅读笔记；引文仅以卡片中已核对的原文片段为依据，不代表整篇正文。"}


def migrate(c) -> None:
    """One transaction; unknown ownership is retained in an exportable archive."""
    c.execute("CREATE TABLE IF NOT EXISTS read_documents (user_id TEXT NOT NULL,paper_id TEXT NOT NULL,"
              "data TEXT NOT NULL,completed_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,paper_id))")
    c.execute("CREATE TABLE IF NOT EXISTS reading_unassigned (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,"
              "table_name TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS feature_migrations (name TEXT PRIMARY KEY,data TEXT NOT NULL)")
    if not c.execute("SELECT 1 FROM feature_migrations WHERE name='conversation_partition_v1'").fetchone():
        for user in c.execute("SELECT id FROM users").fetchall():
            uid = user["id"]
            messages = c.execute("SELECT * FROM messages WHERE user_id=? ORDER BY rowid", (uid,)).fetchall()
            groups = {}
            for m in messages:
                groups.setdefault(m["conversation_id"] or "", []).append(m)
            for cid, msgs in groups.items():
                if cid and c.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (cid, uid)).fetchone():
                    continue
                recovered = cid or new_id()
                title = next((m["text"][:60] for m in msgs if m["role"] == "user"), "历史对话")
                c.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?)",
                          (recovered, uid, "open", title, msgs[0]["created_at"], msgs[-1]["created_at"]))
                if not cid:
                    c.execute("UPDATE messages SET conversation_id=? WHERE user_id=? AND conversation_id=''", (recovered, uid))
            # Inactive portraits remain snapshots: normalize there too.
            for p in store._portrait_rows(c, uid):
                if p["active"]:
                    continue
                snap = json.loads(p["snapshot"] or "{}")
                convs = snap.setdefault("conversations", [])
                known = {v["id"] for v in convs}
                legacy = None
                for m in snap.get("messages") or []:
                    cid = m.get("conversation_id")
                    if not cid:
                        legacy = legacy or new_id()
                        cid = m["conversation_id"] = legacy
                    if cid not in known:
                        known.add(cid)
                        convs.append({"id": cid, "user_id": uid, "status": "open", "topic_hint": m.get("text", "")[:60],
                                      "created_at": m.get("created_at") or now_iso(), "updated_at": m.get("created_at") or now_iso()})
                c.execute("UPDATE portraits SET snapshot=? WHERE id=?", (json.dumps(snap, ensure_ascii=False), p["id"]))
        c.execute("INSERT INTO feature_migrations VALUES('conversation_partition_v1','{}')")
    if c.execute("SELECT 1 FROM feature_migrations WHERE name='portrait_reading_v1'").fetchone():
        return
    report = {"assigned_cards": 0, "unassigned": {}}
    for user in c.execute("SELECT id FROM users").fetchall():
        uid = user["id"]
        portraits = store._portrait_rows(c, uid)
        if not portraits:
            pid = new_id()
            c.execute("INSERT INTO portraits(id,user_id,snapshot,active,created_at) VALUES(?,?,?,?,?)",
                      (pid, uid, "{}", 1, now_iso()))
            portraits = store._portrait_rows(c, uid)
        snapshots = {p["id"]: (store._dump_live(c, uid) if p["active"] else json.loads(p["snapshot"] or "{}"))
                     for p in portraits}
        originals = {t: [dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE user_id=?", (uid,))]
                     for t in store._READING_COLS}
        for snap in snapshots.values():
            for t in store._READING_COLS:
                snap[t] = []
        owners: dict[str, set[str]] = {}
        for pid, snap in snapshots.items():
            for f in snap.get("facts") or []:
                ev = f.get("evidence") or []
                if isinstance(ev, str):
                    ev = json.loads(ev)
                for e in ev:
                    if e.get("type") == "reading_card" and e.get("card_id"):
                        owners.setdefault(e["card_id"], set()).add(pid)
        families: dict[tuple[str, str], set[str]] = {}
        for r in originals["cards"]:
            families.setdefault((r["kit_id"], r["arxiv_id"]), set()).update(owners.get(r["id"], set()))
        for table, rows in originals.items():
            for r in rows:
                targets = set()
                if table == "cards":
                    # A paper match alone does not establish ownership of a
                    # different version's notes or an old tutoring session.
                    targets = owners.get(r["id"], set())
                elif table == "user_papers":
                    targets = set().union(*(v for (_, aid), v in families.items() if aid == r["pid"]))
                elif table == "read_documents":
                    targets = set().union(*(v for (_, aid), v in families.items() if aid == r["paper_id"]))
                if not targets:
                    c.execute("INSERT INTO reading_unassigned VALUES(?,?,?,?,?)",
                              (new_id(), uid, table, json.dumps(r, ensure_ascii=False), now_iso()))
                    report["unassigned"][table] = report["unassigned"].get(table, 0) + 1
                for pid in targets:
                    snapshots[pid][table].append(r)
                    if table == "cards":
                        report["assigned_cards"] += 1
        for p in portraits:
            snap = snapshots[p["id"]]
            passed = sorted((r for r in snap["cards"] if r["status"] == "pass"),
                            key=lambda r: (r["created_at"], r["version"]))
            indexed = {}
            for r in passed:
                card = json.loads(r["data"])
                cached = c.execute("SELECT title,url FROM paper_text WHERE arxiv_id=?", (r["arxiv_id"],)).fetchone()
                d = document(card, dict(cached) if cached else {})
                indexed[d["paper_id"]] = {"user_id": uid, "paper_id": d["paper_id"], "data": json.dumps(d, ensure_ascii=False),
                                          "completed_at": d["completed_at"], "updated_at": now_iso()}
            snap["read_documents"] = list(indexed.values())
            c.execute("UPDATE portraits SET snapshot=? WHERE id=?", (json.dumps(snap, ensure_ascii=False), p["id"]))
            if p["active"]:
                for t in store._READING_COLS:
                    c.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
                    store._restore_rows(c, t, uid, snap[t])
    c.execute("INSERT INTO feature_migrations VALUES('portrait_reading_v1',?)", (json.dumps(report),))


def migration_report() -> dict[str, Any]:
    with store._conn() as c:
        row = c.execute("SELECT data FROM feature_migrations WHERE name='portrait_reading_v1'").fetchone()
    return json.loads(row["data"]) if row else {}
