# -*- coding: utf-8 -*-
"""服务端带读：模型出题，规则判对错，学生作答不发给模型。"""
from __future__ import annotations

import hashlib
import re
import threading
from typing import Any

import arxiv
import llm
import quotes
import reading
import skills
import store
from schemas import now_iso

PLAN_VERSION = 1
EXCERPT_MAX = 3500
SENT_MIN, SENT_MAX = 12, 400
ATTEMPTS_MAX, ANSWERS_MAX = 3, 40
STATION_MIN, ANSWER_MAX = 200, 600
MODEL_TIMEOUT = 20

HEAD_RE = re.compile(r"^(\d{1,2})[ \t]+([A-Z][^\n]{1,80})$", re.M)
SKIP_HEAD = re.compile(r"related work|background|preliminar", re.I)
NUM_RE = re.compile(r"\d")
_GEN_GUARD = threading.Lock()
_GEN_WAIT: dict[tuple, threading.Lock] = {}

STATION_KEYS = {
    "abstract": "摘要",
    "intro": "引言",
    "method": "方法",
    "exp": "实验",
    "limit": "局限",
    "concl": "结论",
}

SLOT_SPEC = [
    ("q_claim_quote", "find_quote", "claim_quote", "abstract"),
    ("q_claim", "own", "claim", "abstract"),
    ("q_problem", "own", "", "intro"),
    ("q_method", "own", "method", "method"),
    ("q_assumption", "own", "assumption", "method"),
    ("q_evidence", "number", "evidence_quote", "exp"),
    ("q_limit", "limitation", "limitation_quote", "limit"),
    ("q_change", "own", "change", "concl"),
]

TEMPLATES = {
    ("find_quote", "claim_quote"): ("从这一节抄一句作者写明本文做成了什么的原话", "找 We show / we propose / we introduce"),
    ("number", "evidence_quote"): ("抄一句带数字、能支撑主张的原句（表格里的字核对不了）", "找带百分号或样本量的句子"),
    ("limitation", "limitation_quote"): ("抄一句作者自己承认没做到或可能不成立的话", "看局限 / 讨论段"),
    ("own", "claim"): ("用你自己的话（10–160 字）说：作者声称做成了什么", "不要抄原句"),
    ("own", "method"): ("一句话：他们怎么做的", "写步骤，不要口号"),
    ("own", "assumption"): ("作者默认成立、却没检验的一件事", "找没做的对照或没声明的前提"),
    ("own", "change"): ("如果你来做，会改哪一处", "只写一处"),
    ("own", ""): ("用自己的话写：这一节在解决什么问题", "读完引言再写"),
}


class TutorError(reading.ReadingError):
    pass


class TutorConflict(Exception):
    pass


def paper_sha(paper: dict[str, Any]) -> str:
    return hashlib.sha256((paper.get("text") or "").encode("utf-8")).hexdigest()[:16]


def skill_sha() -> str:
    return hashlib.sha256(skills.load("paper-tutor").encode("utf-8")).hexdigest()[:16]


def _classify(title: str) -> str | None:
    t = title.lower()
    if SKIP_HEAD.search(t):
        return None
    if re.search(r"abstract", t):
        return "abstract"
    if re.search(r"introduct|overview", t):
        return "intro"
    if re.search(r"experiment|result|evaluat", t):
        return "exp"
    if re.search(r"limit|discuss|future work|broader", t):
        return "limit"
    if re.search(r"conclusion", t):
        return "concl"
    if re.search(r"method|approach|model|architect", t):
        return "method"
    return "method"


def stations(paper: dict[str, Any]) -> list[dict[str, Any]]:
    text = paper.get("text") or ""
    if paper.get("source") == "abstract" or not text.strip():
        return [{"key": "abstract", "label": "摘要（只拿到摘要）", "heading": "Abstract", "start": 0, "end": len(text)}]
    found = []
    last_n = 0
    for m in HEAD_RE.finditer(text):
        n = int(m.group(1))
        if last_n and n != last_n + 1:
            continue
        if n == 1 or last_n:
            found.append((n, m.group(2), m.start()))
            last_n = n
    refs = re.search(r"^references\b", text, re.I | re.M)
    cut = refs.start() if refs else len(text)
    found = [x for x in found if x[2] < cut]
    out: list[dict[str, Any]] = []
    if text[: found[0][2]].strip() if found else text.strip():
        end0 = found[0][2] if found else len(text)
        if end0 > 40:
            out.append({"key": "abstract", "label": "摘要", "heading": "Abstract", "start": 0, "end": end0})
    if len(found) >= 3:
        for i, (n, title, start) in enumerate(found):
            end = found[i + 1][2] if i + 1 < len(found) else cut
            key = _classify(title)
            if key is None:
                continue
            out.append({"key": key, "label": STATION_KEYS[key], "heading": f"{n} {title}", "start": start, "end": end})
    else:
        seen = set()
        for name, at in arxiv.sections(text):
            key = _classify(name) or ("intro" if name in ("introduction", "abstract") else None)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"key": key, "label": STATION_KEYS.get(key, name), "heading": name, "start": at, "end": at})
        for i, st in enumerate(out):
            nxt = out[i + 1]["start"] if i + 1 < len(out) else cut
            st["end"] = nxt if nxt > st["start"] else min(len(text), st["start"] + 2000)
        if not any(s["key"] == "abstract" for s in out):
            out.insert(0, {"key": "abstract", "label": "摘要", "heading": "Abstract", "start": 0, "end": out[0]["start"] if out else min(800, len(text))})
    merged: list[dict[str, Any]] = []
    for st in out:
        if merged and (st["end"] - st["start"]) < STATION_MIN:
            merged[-1]["end"] = st["end"]
            continue
        if merged and st["key"] == merged[-1]["key"]:
            merged[-1]["end"] = st["end"]
            continue
        merged.append(dict(st))
    if not merged:
        merged = [{"key": "abstract", "label": "摘要（只拿到摘要）", "heading": "Abstract", "start": 0, "end": len(text)}]
    return merged


def sentences(text: str, start: int, end: int) -> list[dict[str, Any]]:
    chunk = text[start:end]
    out = []
    off = start
    for part in re.split(r"(?<=[.!?])\s+", chunk):
        raw = part
        if re.match(r"^\[\[table:", raw.strip()):
            off += len(raw) + 1
            continue
        if re.match(r"^\d+$", raw.strip()):
            off += len(raw) + 1
            continue
        body = raw.strip()
        if SENT_MIN <= len(body) <= SENT_MAX:
            out.append({"s": f"S{len(out)+1}", "text": body, "start": off, "end": off + len(raw)})
        off += len(raw) + 1
        if sum(len(x["text"]) for x in out) >= EXCERPT_MAX:
            break
    return out


def _place_slots(sts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = [s["key"] for s in sts]
    def first(*want):
        for w in want:
            if w in keys:
                return w
        return keys[-1]
    placed = []
    for qid, typ, field, home in SLOT_SPEC:
        skippable = False
        if home == "method":
            st = first("method", "exp", "intro", "abstract")
        elif home == "exp":
            st = first("exp", "method", "intro", "abstract")
        elif home == "limit":
            st = first("limit", "concl") if ("limit" in keys or "concl" in keys) else keys[-1]
            skippable = "limit" not in keys and "concl" not in keys
        elif home == "concl":
            st = first("concl", "limit", keys[-1])
        elif home == "intro":
            st = first("intro", "abstract")
        else:
            st = first("abstract", keys[0])
        q, h = TEMPLATES.get((typ, field), TEMPLATES.get((typ, ""), ("回答这一题", "")))
        item = {"qid": qid, "station": st, "type": typ, "field": field, "question": q, "hint": h}
        if qid == "q_limit" and skippable:
            item["skippable"] = True
        placed.append(item)
    return placed


def _fresh(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: item[k] for k in item if k not in ("attempts", "status", "answer", "note")}
    out.update({"attempts": 0, "status": "open", "answer": "", "note": ""})
    return out


def _template_items(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for sl in slots:
        q, h = TEMPLATES.get((sl["type"], sl["field"]), ("回答这一题", ""))
        items.append(_fresh({**sl, "question": q, "hint": h, "voice": "rules", "anchor": None}))
    return items


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _valid_items(data: Any, slots: list[dict[str, Any]], sents: list[dict[str, Any]], paper: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    by_s = {s["s"]: s for s in sents}
    seen = set()
    out = []
    ok_any = False
    for sl in slots:
        hit = next((it for it in data["items"] if isinstance(it, dict) and it.get("qid") == sl["qid"]), None)
        if not hit or sl["qid"] in seen:
            out.append({**_template_items([sl])[0]})
            continue
        seen.add(sl["qid"])
        q = hit.get("question")
        hint = hit.get("hint") or ""
        anc = hit.get("anchor")
        if not isinstance(q, str) or not (4 <= len(q) <= 60) or len(str(hint)) > 40:
            out.append({**_template_items([sl])[0]})
            continue
        sent = by_s.get(anc) if isinstance(anc, str) else None
        if anc is not None and sent is None:
            out.append({**_template_items([sl])[0]})
            continue
        if sl["type"] == "number" and sent and not NUM_RE.search(sent["text"]):
            out.append({**_template_items([sl])[0]})
            continue
        if sl["type"] == "limitation" and sent and not reading.check_limitation(sent["text"], paper).get("pass"):
            out.append({**_template_items([sl])[0]})
            continue
        leak = _norm(sent["text"]) if sent else ""
        if leak and len(leak) >= 20 and (leak[:20] in _norm(q) or leak[:20] in _norm(hint)):
            out.append({**_template_items([sl])[0]})
            continue
        out.append(_fresh({**sl, "question": q, "hint": hint, "voice": "llm",
                           "anchor": {"s": sent["s"], "start": sent["start"], "end": sent["end"]} if sent else None}))
        ok_any = True
    return out if ok_any or out else None


def _cache_blob(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: i[k] for k in i if k not in ("attempts", "status", "answer", "note")} for i in items]


def _gen_station(uid: str, paper: dict[str, Any], station: dict[str, Any], slots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    sha, sk = paper_sha(paper), skill_sha()
    key = (paper.get("id") or "", station["key"], sha, PLAN_VERSION, sk)
    with _GEN_GUARD:
        wait = _GEN_WAIT.setdefault(key, threading.Lock())
    with wait:
        cached = store.get_tutor_questions(*key)
        if cached:
            return [_fresh(x) for x in cached], False
        sents = sentences(paper.get("text") or "", station["start"], station["end"])
        used_model = False
        if llm.ready(uid):
            used_model = True
            user = (
                f"标题：{paper.get('title','')}\n站：{station['label']}\n"
                + "\n".join(f"{s['s']} | {s['text']}" for s in sents)
                + "\n题位：\n" + "\n".join(f"{sl['qid']} | {sl['type']} | {sl['field'] or '不进卡'} | 考察本节" for sl in slots)
            )
            data = llm.chat_json(skills.load("paper-tutor"), user, timeout=MODEL_TIMEOUT, tag="paper-tutor", uid=uid)
            items = _valid_items(data, slots, sents, paper)
            if items and any(i.get("voice") == "llm" for i in items):
                store.put_tutor_questions(*key, _cache_blob(items))
                return [_fresh(x) for x in items], True
        return _template_items(slots), used_model


def judge(slot: dict[str, Any], answer: str, paper: dict[str, Any], station: dict[str, Any]) -> dict[str, Any]:
    ans = (answer or "").strip()
    if len(ans) > ANSWER_MAX:
        return {"pass": False, "note": "作答太长，压到 600 字以内", "where": station["label"]}
    within = (station["start"], station["end"])
    typ = slot["type"]
    if typ == "find_quote":
        loc = quotes.locate(ans, paper, within=within)
        return {"pass": loc["found"], "note": "已定位" if loc["found"] else (loc.get("reason") or f"这句不在「{station['label']}」"), "where": loc.get("where") or station["label"]}
    if typ == "number":
        if not NUM_RE.search(ans):
            return {"pass": False, "note": "表格里的字核对不了，选正文句子", "where": station["label"]}
        loc = quotes.locate(ans, paper, within=within)
        return {"pass": loc["found"], "note": "已定位" if loc["found"] else (loc.get("reason") or "选正文里带数字的句子"), "where": loc.get("where") or station["label"]}
    if typ == "limitation":
        loc = reading.check_limitation(ans, paper)
        return {"pass": loc.get("pass"), "note": "已定位" if loc.get("pass") else loc.get("reason", "局限要从局限 / 讨论段引"), "where": loc.get("where") or station["label"]}
    ok, note = reading._check_own(ans, paper, slot.get("field") or "理解")
    leaked = _norm(slot.get("question", "") + " " + slot.get("hint", ""))
    if ok and len(ans) >= 16 and _norm(ans)[:16] in leaked:
        ok, note = False, "不要照抄题面"
    return {"pass": ok, "note": note or "可以", "where": station["label"]}


def public(state: dict[str, Any]) -> dict[str, Any]:
    voices = {s.get("voice") for s in state.get("slots") or []}
    voice = "mixed" if voices == {"llm", "rules"} else ("llm" if voices == {"llm"} else "rules")
    slots = []
    for s in state.get("slots") or []:
        slots.append({k: s[k] for k in s if k != "anchor"})
        if s.get("anchor"):
            slots[-1]["anchor_station"] = next((st["label"] for st in state["stations"] if st["key"] == s["station"]), "")
    cur = state.get("cur", 0)
    total = len(state.get("plan") or state.get("slots") or [])
    notice = []
    if any("只拿到摘要" in (st.get("label") or "") for st in state.get("stations") or []):
        notice.append("只拿到摘要")
    if state.get("status") == "stale":
        notice.append("原文变了，请重开")
    if state.get("rules_fallback"):
        notice.append("模型额度用完，后面是规则题")
    return {
        "status": state.get("status"), "rev": state.get("rev", 0),
        "voice": voice, "notice": "；".join(notice),
        "cur": cur, "total": total, "slots": slots,
        "stations": [{"key": s["key"], "label": s["label"]} for s in state.get("stations") or []],
        "prefill": state.get("prefill"),
        "answers_total": state.get("answers_total", 0),
    }


def _require(uid: str, kit_id: str, aid: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    k = reading.kit(kit_id)
    if not reading.kit_paper(k, aid):
        raise TutorError("只能带读工具包里的论文")
    paper = arxiv.cached(aid) or arxiv.fulltext(aid)
    return k, paper, arxiv.clean_id(aid)


def _fill_station(uid: str, paper: dict[str, Any], state: dict[str, Any], st_key: str) -> None:
    if any(s.get("station") == st_key for s in state.get("slots") or []):
        return
    plan = [p for p in state.get("plan") or [] if p["station"] == st_key]
    if not plan:
        return
    st = next(s for s in state["stations"] if s["key"] == st_key)
    items, used = _gen_station(uid, paper, st, plan)
    if used:
        state["model_calls"] = state.get("model_calls", 0) + 1
        if not any(i.get("voice") == "llm" for i in items):
            state["rules_fallback"] = True
    elif not llm.ready(uid) and state.get("model_calls", 0):
        state["rules_fallback"] = True
    state.setdefault("slots", []).extend(items)


def _build(uid: str, kit_id: str, paper: dict[str, Any]) -> dict[str, Any]:
    sts = stations(paper)
    plan = _place_slots(sts)
    state = {
        "v": 1, "plan_version": PLAN_VERSION, "paper_source": paper.get("source"),
        "paper_sha": paper_sha(paper), "stations": sts, "plan": plan, "slots": [],
        "cur": 0, "answers_total": 0, "model_calls": 0, "rev": 1,
        "prefill": None, "status": "active", "rules_fallback": False,
    }
    if plan:
        _fill_station(uid, paper, state, plan[0]["station"])
    return state


def start(uid: str, kit_id: str, aid: str, restart: bool = False) -> dict[str, Any]:
    k, paper, aid = _require(uid, kit_id, aid)
    row = store.get_tutor(uid, kit_id, aid)
    if row and not restart:
        st = row["state"]
        if st.get("paper_sha") and st.get("paper_sha") != paper_sha(paper):
            st["status"] = "stale"
            store.save_tutor(uid, kit_id, aid, "stale", st, None)
            return public(st)
        if row.get("status") in ("active", "read", "stale", "done"):
            return public(st)
    state = _build(uid, kit_id, paper)
    store.save_tutor(uid, kit_id, aid, state["status"], state, None)
    return public(state)


def get(uid: str, kit_id: str, aid: str) -> dict[str, Any] | None:
    row = store.get_tutor(uid, kit_id, aid)
    return public(row["state"]) if row else None


def turn(uid: str, kit_id: str, aid: str, qid: str, answer: str = "", skip: bool = False, rev: int = 0) -> dict[str, Any]:
    k, paper, aid = _require(uid, kit_id, aid)
    row = store.get_tutor(uid, kit_id, aid)
    if not row:
        raise TutorError("还没开始带读")
    state = row["state"]
    if state.get("paper_sha") != paper_sha(paper):
        state["status"] = "stale"
        store.save_tutor(uid, kit_id, aid, "stale", state, None)
        return {"verdict": {"pass": False, "note": "原文变了，请重开", "where": ""}, "session": public(state)}
    if state.get("rev") != rev:
        raise TutorConflict("这题已经答过了，刷新")
    slots = state["slots"]
    cur = state.get("cur", 0)
    if cur >= len(slots) or slots[cur]["qid"] != qid:
        raise TutorConflict("这题已经答过了，刷新")
    sl = slots[cur]
    st = next(s for s in state["stations"] if s["key"] == sl["station"])
    if skip:
        sl["status"] = "skipped"
        sl["note"] = sl.get("note") or f"已跳过。看「{st['label']}」"
    else:
        if state.get("answers_total", 0) >= ANSWERS_MAX:
            raise TutorError("这篇作答次数用完了")
        verd = judge(sl, answer, paper, st)
        sl["attempts"] = sl.get("attempts", 0) + 1
        state["answers_total"] = state.get("answers_total", 0) + 1
        sl["note"] = verd["note"]
        if verd["pass"]:
            sl["status"] = "pass"
            sl["answer"] = answer.strip()
        elif sl["attempts"] >= ATTEMPTS_MAX:
            sl["status"] = "skipped"
            sl["note"] = f"{verd['note']}。已经 3 次，可跳过。提示：在「{st['label']}」"
        else:
            state["rev"] = state.get("rev", 1) + 1
            if not store.save_tutor(uid, kit_id, aid, state["status"], state, rev):
                raise TutorConflict("这题已经答过了，刷新")
            return {"verdict": verd, "session": public(state)}
    state["cur"] = cur + 1
    state["rev"] = state.get("rev", 1) + 1
    plan = state.get("plan") or slots
    if state["cur"] >= len(plan):
        state["status"] = "read"
    elif state["cur"] >= len(slots):
        nxt = plan[state["cur"]]["station"]
        _fill_station(uid, paper, state, nxt)
        if not llm.ready(uid) and state.get("model_calls", 0):
            state["rules_fallback"] = True
    if not store.save_tutor(uid, kit_id, aid, state["status"], state, rev):
        raise TutorConflict("这题已经答过了，刷新")
    verd = {"pass": sl["status"] == "pass", "note": sl.get("note", ""), "where": st["label"]}
    return {"verdict": verd, "session": public(state)}


def finish(uid: str, kit_id: str, aid: str) -> dict[str, Any]:
    k, paper, aid = _require(uid, kit_id, aid)
    row = store.get_tutor(uid, kit_id, aid)
    if not row:
        raise TutorError("还没开始带读")
    state = row["state"]
    prefill: dict[str, str] = {}
    source: dict[str, str] = {}
    for sl in state.get("slots") or []:
        field = sl.get("field")
        if field and sl.get("status") == "pass" and sl.get("answer"):
            prefill[field] = sl["answer"]
            source[field] = sl["qid"]
    review = reading.review_card(k, paper, prefill, {}, [])
    need = max(3, len(k["dimensions"]) - 2)
    missing = [c["label"] for c in review["checks"] if not c["pass"]]
    if not any(c["key"] == "dims" and c["pass"] for c in review["checks"]):
        if "矩阵维度至少 5 个" not in missing:
            missing.append("矩阵维度至少 5 个" if need >= 5 else f"矩阵维度至少 {need} 个")
    state["prefill"] = prefill
    state["status"] = "done"
    state["rev"] = state.get("rev", 1) + 1
    store.save_tutor(uid, kit_id, aid, "done", state, None)
    return {"prefill": prefill, "from": source, "review": review, "missing": missing, "session": public(state)}
