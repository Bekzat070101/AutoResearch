# -*- coding: utf-8 -*-
"""服务端带读：模型出题、模型按标准判卷；代码只钉硬事实。"""
from __future__ import annotations

import hashlib
import json
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

PLAN_VERSION = 3
EXCERPT_MAX = 3500
SENT_MIN, SENT_MAX = 12, 400
ATTEMPTS_MAX, ANSWERS_MAX = 3, 40
STATION_MIN, ANSWER_MAX = 200, 600
MODEL_TIMEOUT = 20
GRADE_MAX, REGRADE_MAX = 15, 2
GRADE_IDLE, GRADE_TOTAL, GRADE_EXCERPT = 20, 30, 2000
COPY_WIN, ASK_WIN = 40, 16

HEAD_RE = re.compile(r"^(\d{1,2})[ \t]+([A-Z][^\n]{1,80})$", re.M)
PDF_HEAD_RE = re.compile(r"^(\d{1,2})\.?[ \t]+([A-Z][^\n]{1,58})$", re.M)
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
    ("find_quote", "claim_quote"): ("从这一节抄一句作者写明本文做成了什么的原话", "找 We show / we propose / we introduce",
                                   ["引的句子说的是本文做成或提出的东西，不是背景或别人的工作"]),
    ("number", "evidence_quote"): ("抄一句带数字、能支撑主张的原句（表格里的字核对不了）", "找带百分号或样本量的句子",
                                  ["句子里的数字是本文自己的实验结果", "这个结果支撑本文的主要主张"]),
    ("limitation", "limitation_quote"): ("抄一句作者自己承认没做到或可能不成立的话", "看局限 / 讨论段",
                                        ["句子是作者承认没做到、没检验或可能不成立的事"]),
    ("own", "claim"): ("用你自己的话（10–160 字）说：作者声称做成了什么", "不要抄原句",
                      ["说出了本文做成或提出的具体东西", "没有下摘要以外的结论"]),
    ("own", "method"): ("一句话：他们怎么做的", "写步骤，不要口号",
                       ["写出至少一个具体步骤或做法，不是口号"]),
    ("own", "assumption"): ("作者默认成立、却没检验的一件事", "找没做的对照或没声明的前提",
                            ["指出一个前提或默认条件", "这个前提文中没有检验或论证"]),
    ("own", "change"): ("如果你来做，会改哪一处", "只写一处",
                       ["是一处具体改动：改什么", "说明改了能检验或改进什么"]),
    ("own", ""): ("用自己的话写：这一节在解决什么问题", "读完引言再写",
                 ["说出本节要解决的问题，而不是复述方法"]),
}


class TutorError(reading.ReadingError):
    pass


class TutorConflict(Exception):
    pass


def _is_paper_title(answer: str, paper: dict[str, Any], loc: dict[str, Any] | None = None) -> bool:
    title = quotes.norm(paper.get("title") or "")
    got = quotes.norm(answer or "")
    if not title or not got:
        return False
    if got == title:
        return True
    at = None if loc is None else loc.get("at")
    if at is not None and at < len(paper.get("title") or "") + 50 and got in title:
        return True
    return False


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
    head_re = PDF_HEAD_RE if paper.get("source") == "pdf" else HEAD_RE
    found = []
    last_n = 0
    for m in head_re.finditer(text):
        n = int(m.group(1))
        if last_n and n != last_n + 1:
            continue
        if n == 1 or last_n:
            found.append((n, m.group(2).strip(), m.start()))
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
        strict = paper.get("source") == "pdf"
        for name, at in arxiv.sections(text, strict=strict):
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
        if paper.get("source") == "pdf" and len(found) < 3 and not arxiv.sections(text, strict=True):
            out = _pdf_parts(text, cut)
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
        if paper.get("source") == "pdf":
            merged = _pdf_parts(text, len(text))
        else:
            merged = [{"key": "abstract", "label": "摘要（只拿到摘要）", "heading": "Abstract", "start": 0, "end": len(text)}]
    return merged


def _pdf_parts(text: str, cut: int) -> list[dict[str, Any]]:
    keys = ["abstract", "intro", "method", "concl"]
    labels = ["第 1 部分", "第 2 部分", "第 3 部分", "第 4 部分"]
    end = cut if cut > 0 else len(text)
    chunk = max(1, end // 4)
    out = []
    for i, (key, label) in enumerate(zip(keys, labels)):
        start = i * chunk
        stop = end if i == 3 else (i + 1) * chunk
        out.append({"key": key, "label": label, "heading": label, "start": start, "end": stop})
    return out


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
        q, h, crit = TEMPLATES.get((typ, field), TEMPLATES.get((typ, ""), ("回答这一题", "", [])))
        item = {"qid": qid, "station": st, "type": typ, "field": field, "question": q, "hint": h, "criteria": list(crit)}
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
        q, h, crit = TEMPLATES.get((sl["type"], sl["field"]), ("回答这一题", "", []))
        if sl.get("summary_only") and sl["qid"] == "q_problem":
            h = "只依据摘要，说清作者想解决的问题；摘要没说的保持未知"
        items.append(_fresh({**sl, "question": q, "hint": h, "criteria": list(crit), "voice": "rules", "anchor": None}))
    return items


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _overlap(a: str, b: str, n: int) -> bool:
    aa, bb = _norm(a), _norm(b)
    if n <= 0 or len(aa) < n or len(bb) < n:
        return False
    return any(aa[i:i + n] in bb for i in range(len(aa) - n + 1))


def _slot_criteria(slot: dict[str, Any]) -> list[str]:
    crit = slot.get("criteria")
    if isinstance(crit, list) and crit:
        return [str(x).strip() for x in crit if str(x).strip()]
    spec = TEMPLATES.get((slot.get("type"), slot.get("field")), TEMPLATES.get((slot.get("type"), ""), ("", "", [])))
    return list(spec[2]) if len(spec) > 2 else []


def _leaks(text: str, *blobs: str) -> bool:
    blob = _norm(text)
    if len(blob) < 20:
        return False
    hay = _norm(" ".join(blobs))
    return any(blob[i:i + 20] in hay for i in range(len(blob) - 19))


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
        crit = hit.get("criteria")
        if not isinstance(crit, list) or not (1 <= len(crit) <= 4):
            out.append({**_template_items([sl])[0]})
            continue
        cleaned = [str(c).strip() for c in crit if isinstance(c, str) and str(c).strip() and len(str(c).strip()) <= 60]
        if len(cleaned) != len(crit):
            out.append({**_template_items([sl])[0]})
            continue
        leak_src = sent["text"] if sent else ""
        if leak_src and (_leaks(leak_src, q, hint, *cleaned) or any(_leaks(c, leak_src) for c in cleaned)):
            out.append({**_template_items([sl])[0]})
            continue
        out.append(_fresh({**sl, "question": q, "hint": hint, "criteria": cleaned, "voice": "llm",
                           "anchor": {"s": sent["s"], "start": sent["start"], "end": sent["end"]} if sent else None}))
        ok_any = True
    return out if ok_any or out else None


def _cache_blob(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: i[k] for k in i if k not in ("attempts", "status", "answer", "note")} for i in items]


def _gen_station(uid: str, paper: dict[str, Any], station: dict[str, Any], slots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    import recommendations
    context = recommendations.learning_context(uid)
    sha, sk = paper_sha(paper), skill_sha()
    personal = any(e["key"] != "current_direction" for e in context["evidence"])
    if personal:
        # Same public text may be shared; personalized questions must not be.
        sk = recommendations.digest({"skill": sk, "uid": uid, "portrait": context["portrait_id"],
                                     "evidence": context["evidence"], "direction": context["current_direction"]})
    key = (paper.get("id") or "", station["key"], sha, PLAN_VERSION, sk)
    with _GEN_GUARD:
        wait = _GEN_WAIT.setdefault(key, threading.Lock())
    with wait:
        local = arxiv.is_local(paper.get("id") or "")
        cached = None if local else store.get_tutor_questions(*key)
        if cached:
            return [_fresh(x) for x in cached], False
        sents = sentences(paper.get("text") or "", station["start"], station["end"])
        used_model = False
        if llm.ready(uid):
            used_model = True
            user = (
                f"标题：{paper.get('title','')}\n站：{station['label']}\n"
                + "\n".join(f"{s['s']} | {s['text']}" for s in sents)
                + "\n题位：\n" + "\n".join(
                    f"{sl['qid']} | {sl['type']} | {sl['field'] or '不进卡'} | {TEMPLATES.get((sl['type'], sl.get('field') or ''), ('考察本节', '', []))[0]}"
                    for sl in slots)
            )
            if personal:
                user += ("\n用户学习依据（只用于调整问法、提示和例子，不能替用户作答或改写原文）：\n"
                         + json.dumps(context, ensure_ascii=False)
                         + "\n必须仍围绕本站原文及既定题位出题；在修不等于掌握，历史兴趣不能改变本文事实。"
                           "没有全文时仅检验摘要中实际出现的主张，不问未提供的实验细节。"
                           "至少一个解释题或提示要具体衔接用户的已知实践或困难，例如Tool调用、统计对照或公式不熟；"
                           "保持题干不超过60字、提示不超过40字，不泄露题目答案。")
            data = llm.chat_json(skills.load("paper-tutor"), user, timeout=MODEL_TIMEOUT, tag="paper-tutor", uid=uid)
            items = _valid_items(data, slots, sents, paper)
            if items and any(i.get("voice") == "llm" for i in items):
                if not local:
                    store.put_tutor_questions(*key, _cache_blob(items))
                return [_fresh(x) for x in items], True
        return _template_items(slots), used_model


def judge(slot: dict[str, Any], answer: str, paper: dict[str, Any], station: dict[str, Any]) -> dict[str, Any]:
    ans = (answer or "").strip()
    if not ans:
        return {"pass": False, "note": "先写答案", "where": station["label"], "at": None}
    if len(ans) > ANSWER_MAX:
        return {"pass": False, "note": "作答太长，压到 600 字以内", "where": station["label"], "at": None}
    within = (station["start"], station["end"])
    typ = slot["type"]
    loc: dict[str, Any] = {}
    if typ == "find_quote":
        loc = quotes.locate(ans, paper, within=within)
        if loc["found"] and _is_paper_title(ans, paper, loc):
            ok, note = False, "这是标题，不是主张原句"
        else:
            ok, note = loc["found"], ("引文和原文对上了" if loc["found"] else (loc.get("reason") or f"这句不在「{station['label']}」"))
    elif typ == "number":
        if not NUM_RE.search(ans):
            return {"pass": False, "note": "表格里的字核对不了，选正文句子", "where": station["label"], "at": None}
        loc = quotes.locate(ans, paper, within=within)
        ok, note = loc["found"], ("引文和原文对上了" if loc["found"] else (loc.get("reason") or "选正文里带数字的句子"))
    elif typ == "limitation":
        loc = reading.check_limitation(ans, paper)
        ok, note = loc.get("pass"), ("引文和原文对上了" if loc.get("pass") else loc.get("reason", "局限要从局限 / 讨论段引"))
    else:
        ok, note = reading._check_own(ans, paper, slot.get("field") or "理解")
        body = paper.get("text") or ""
        if ok and len(ans) >= COPY_WIN and _overlap(ans, body, COPY_WIN):
            ok, note = False, "不要照抄原文"
        asked = (slot.get("question") or "") + " " + (slot.get("hint") or "")
        if ok and _overlap(ans, asked, ASK_WIN):
            ok, note = False, "不要照抄题面"
        loc = {}
    at = loc.get("at")
    hard_pass = False
    anc = slot.get("anchor") or {}
    if typ in ("find_quote", "number", "limitation") and ok and at is not None and anc.get("start") is not None:
        if anc["start"] <= at < anc.get("end", anc["start"] + 1):
            hard_pass = True
    return {"pass": bool(ok), "note": note or "可以", "where": loc.get("where") or station["label"],
            "at": at, "hard_pass": hard_pass}


def _excerpt(paper: dict[str, Any], station: dict[str, Any], slot: dict[str, Any], at: Any) -> str:
    text = paper.get("text") or ""
    start, end = station["start"], station["end"]
    if at is not None:
        lo = max(start, int(at) - 1000)
        hi = min(end, int(at) + 1000)
        window = text[lo:hi]
        sents = sentences(text, lo, hi)
        if sents:
            return "\n".join(f"{s['s']} | {s['text']}" for s in sents)[:GRADE_EXCERPT]
        return window[:GRADE_EXCERPT]
    sents = sentences(text, start, end)
    if not sents:
        return text[start:start + GRADE_EXCERPT]
    center = None
    anc = slot.get("anchor") or {}
    if anc.get("s"):
        center = next((s for s in sents if s["s"] == anc["s"]), None)
    if center is None:
        center = sents[0]
    i = sents.index(center)
    chunk, lo, hi = [center], i, i
    while sum(len(s["text"]) for s in chunk) < GRADE_EXCERPT and (lo > 0 or hi + 1 < len(sents)):
        if hi + 1 < len(sents):
            hi += 1
            chunk.append(sents[hi])
        if sum(len(s["text"]) for s in chunk) >= GRADE_EXCERPT:
            break
        if lo > 0:
            lo -= 1
            chunk.insert(0, sents[lo])
    return "\n".join(f"{s['s']} | {s['text']}" for s in chunk)[:GRADE_EXCERPT]


def _valid_grade(data: Any, n_criteria: int, anchor_text: str, station: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict) or not isinstance(data.get("pass"), bool):
        return None
    reasons, missing, feedback = data.get("reasons"), data.get("missing"), data.get("feedback")
    if not isinstance(reasons, list) or len(reasons) != n_criteria:
        return None
    if not all(isinstance(x, str) for x in reasons):
        return None
    if not isinstance(missing, list) or len(missing) > 4 or not all(isinstance(x, str) for x in missing):
        return None
    if not isinstance(feedback, str):
        return None
    if data["pass"] and missing:
        return None
    fallback = f"回到「{station.get('label') or '本节'}」再找一遍"
    def scrub(s: str) -> str:
        t = str(s)[:80]
        return fallback if anchor_text and _overlap(t, anchor_text, 20) else t
    return {
        "pass": data["pass"],
        "reasons": [scrub(x) for x in reasons],
        "missing": [scrub(x) for x in missing],
        "feedback": (fallback if anchor_text and _overlap(feedback, anchor_text, 20) else feedback[:120]),
    }


def grade(uid: str, slot: dict[str, Any], ans: str, paper: dict[str, Any], station: dict[str, Any], at: Any) -> dict[str, Any] | None:
    crit = _slot_criteria(slot)
    if not crit:
        return None
    anc = slot.get("anchor") or {}
    sents = sentences(paper.get("text") or "", station["start"], station["end"])
    anchor_text = ""
    if anc.get("s"):
        hit = next((s for s in sents if s["s"] == anc["s"]), None)
        anchor_text = hit["text"] if hit else ""
    safe = (ans or "").replace("<answer>", "").replace("</answer>", "")
    user = (
        f"题型：{slot.get('type')}\n题面：{slot.get('question')}\n"
        + "标准：\n" + "\n".join(f"C{i+1} | {c}" for i, c in enumerate(crit))
        + f"\n锚点句：{anchor_text or '无'}\n本节节选：\n{_excerpt(paper, station, slot, at)}\n"
        + f"<answer>\n{safe}\n</answer>"
    )
    data = llm.chat_json(skills.load("paper-quiz-grade"), user, timeout=GRADE_IDLE,
                         total=GRADE_TOTAL, retry=False, tag="paper-quiz-grade", uid=uid)
    return _valid_grade(data, len(crit), anchor_text, station)


def public(state: dict[str, Any], ready: bool | None = None) -> dict[str, Any]:
    voices = {s.get("voice") for s in state.get("slots") or []}
    voice = "mixed" if voices == {"llm", "rules"} else ("llm" if voices == {"llm"} else "rules")
    hide = {"anchor", "criteria"}
    slots = []
    for s in state.get("slots") or []:
        row = {k: s[k] for k in s if k not in hide}
        row["last"] = s.get("last") or ""
        row["feedback"] = s.get("feedback") or ""
        row["missing"] = s.get("missing") or []
        row["pending_reason"] = s.get("pending_reason") or ""
        slots.append(row)
    cur = state.get("cur", 0)
    total = len(state.get("plan") or state.get("slots") or [])
    notice = []
    if any("只拿到摘要" in (st.get("label") or "") for st in state.get("stations") or []):
        notice.append("只拿到摘要，仅做摘要练习；不计为完成全文阅读，获取全文后可重开完整带读")
    if state.get("status") == "stale":
        notice.append("原文变了，请重开")
    if state.get("rules_fallback") and ready is not True:
        if state.get("rules_fallback_reason") == "quota":
            notice.append("今天的模型额度用完了，后面的题由固定题库出")
        else:
            notice.append("模型刚才没响应，后面的题由固定题库出")
    n = sum(1 for s in state.get("slots") or [] if s.get("status") == "pending")
    if n:
        notice.append(f"有 {n} 题已保存，等模型恢复后判分")
    return {
        "status": state.get("status"), "rev": state.get("rev", 0),
        "summary_only": state.get("paper_source") == "abstract",
        "voice": voice, "notice": "；".join(notice),
        "cur": cur, "total": total, "slots": slots,
        "stations": [{"key": s["key"], "label": s["label"], "heading": s.get("heading") or "",
                      "start": s.get("start", 0)} for s in state.get("stations") or []],
        "prefill": state.get("prefill"),
        "answers_total": state.get("answers_total", 0),
    }


def _require(uid: str, kit_id: str, aid: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    if arxiv.is_local(aid):
        paper = store.get_user_paper(uid, aid)
        if not paper:
            raise TutorError("没有这篇")
        k = reading.kits().get(kit_id) or {"id": kit_id or "", "name": "", "papers": [], "dimensions": []}
        return k, {**paper, "id": aid, "source": "pdf"}, aid
    aid = arxiv.clean_id(aid)
    try:
        k = reading.kit(kit_id)
    except reading.ReadingError:
        k = {"id": kit_id or "", "name": "", "papers": [], "dimensions": []}
    paper = arxiv.cached(aid) or arxiv.fulltext(aid)
    return k, paper, aid


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
            state["rules_fallback_reason"] = "model" if llm.ready(uid) else "quota"
    elif not llm.ready(uid) and state.get("model_calls", 0):
        state["rules_fallback"] = True
        state["rules_fallback_reason"] = "model" if llm.ready(uid) else "quota"
    state.setdefault("slots", []).extend(items)


def _build(uid: str, kit_id: str, paper: dict[str, Any]) -> dict[str, Any]:
    sts = stations(paper)
    plan = _place_slots(sts)
    if paper.get("source") == "abstract":
        plan = [{**sl, "summary_only": True} for sl in plan
                if sl["qid"] in ("q_claim_quote", "q_claim", "q_problem")]
    state = {
        "v": 1, "plan_version": PLAN_VERSION, "paper_source": paper.get("source"),
        "paper_sha": paper_sha(paper), "stations": sts, "plan": plan, "slots": [],
        "cur": 0, "answers_total": 0, "model_calls": 0, "grade_calls": 0, "rev": 1,
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
            return public(st, ready=llm.ready(uid))
        if row.get("status") in ("active", "read", "stale", "done"):
            return public(st, ready=llm.ready(uid))
    if not row and not llm.ready(uid):
        raise TutorError("现在用不了模型（没配 / 没同意隐私说明 / 今天额度用完），先写阅读卡")
    inherit = row["state"] if row else {}
    state = _build(uid, kit_id, paper)
    state["grade_calls"] = inherit.get("grade_calls", 0)
    state["answers_total"] = inherit.get("answers_total", 0)
    if restart and inherit:
        state["rev"] = int(inherit.get("rev") or 1) + 1
    store.save_tutor(uid, kit_id, aid, state["status"], state, None)
    return public(state, ready=llm.ready(uid))


def get(uid: str, kit_id: str, aid: str) -> dict[str, Any] | None:
    row = store.get_tutor(uid, kit_id, aid)
    return public(row["state"], ready=llm.ready(uid)) if row else None


def _verdict_of(sl: dict[str, Any], st: dict[str, Any], *, hard: bool = False) -> dict[str, Any]:
    status = sl.get("status") or "open"
    state = "pass" if status == "pass" else "pending" if status == "pending" else "skipped" if status == "skipped" else "fail"
    return {
        "pass": status == "pass", "state": state, "hard": hard,
        "note": sl.get("note") or "", "where": st.get("label") or "",
        "missing": sl.get("missing") or [], "feedback": sl.get("feedback") or "",
    }


def turn(uid: str, kit_id: str, aid: str, qid: str, answer: str = "", skip: bool = False,
         rev: int = 0, regrade: bool = False) -> dict[str, Any]:
    k, paper, aid = _require(uid, kit_id, aid)
    row = store.get_tutor(uid, kit_id, aid)
    if not row:
        raise TutorError("还没开始带读")
    state = row["state"]
    if state.get("paper_sha") != paper_sha(paper):
        state["status"] = "stale"
        store.save_tutor(uid, kit_id, aid, "stale", state, None)
        return {"verdict": {"pass": False, "state": "fail", "hard": False, "note": "原文变了，请重开",
                            "where": "", "missing": [], "feedback": ""}, "session": public(state, ready=llm.ready(uid))}
    if state.get("rev") != rev:
        raise TutorConflict("这题已经答过了，刷新")
    slots = state["slots"]
    cur = state.get("cur", 0)
    sl = next((s for s in slots if s["qid"] == qid), None)
    if sl is None:
        raise TutorConflict("这题已经答过了，刷新")
    current = slots[cur] if cur < len(slots) else None
    if sl is not current and sl.get("status") not in ("pending", "retry", "open"):
        raise TutorConflict("这题已经答过了，刷新")
    st = next(s for s in state["stations"] if s["key"] == sl["station"])
    sl.setdefault("criteria", _slot_criteria(sl))
    if skip:
        if sl.get("attempts", 0) < 1 and not sl.get("skippable"):
            raise TutorError("答错过一次才能跳过")
        sl["status"] = "skipped"
        sl["note"] = sl.get("note") or f"已跳过。看「{st['label']}」"
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=False)
    if regrade:
        raw_last = sl.get("last")
        if not raw_last:
            raise TutorError("还没有作答")
        sl["regrades"] = sl.get("regrades", 0) + 1
        if sl["regrades"] > REGRADE_MAX:
            raise TutorError("这题重判次数用完了，改答案或跳过")
        ans = str(raw_last).strip()
    else:
        ans = (answer or "").strip()
    if not ans:
        raise TutorError("先写答案")
    if len(ans) > ANSWER_MAX:
        raise TutorError("作答太长，压到 600 字以内")
    if state.get("answers_total", 0) >= ANSWERS_MAX:
        raise TutorError("这篇作答次数用完了")
    prev_last = sl.get("last")
    sl["last"] = ans
    verd = judge(sl, ans, paper, st)
    if not verd["pass"]:
        sl["attempts"] = sl.get("attempts", 0) + 1
        state["answers_total"] = state.get("answers_total", 0) + 1
        sl["note"] = verd["note"]
        sl["feedback"] = ""
        sl["missing"] = []
        if sl["attempts"] >= ATTEMPTS_MAX:
            sl["status"] = "skipped"
            sl["note"] = f"{verd['note']}。已经 3 次，可跳过。提示：在「{st['label']}」"
            return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=True)
        if sl is not current:
            if prev_last:
                sl["last"] = prev_last
            sl["status"] = "retry"
        else:
            sl["status"] = "open"
        state["rev"] = state.get("rev", 1) + 1
        if not store.save_tutor(uid, kit_id, aid, state["status"], state, rev):
            raise TutorConflict("这题已经答过了，刷新")
        return {"verdict": {**_verdict_of(sl, st, hard=True), "pass": False, "state": "fail",
                            "note": verd["note"]}, "session": public(state, ready=llm.ready(uid))}
    if verd.get("hard_pass"):
        sl["status"] = "pass"
        sl["answer"] = ans
        sl["note"] = verd["note"]
        sl["attempts"] = sl.get("attempts", 0) + 1
        state["answers_total"] = state.get("answers_total", 0) + 1
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=True)
    if state.get("grade_calls", 0) >= GRADE_MAX:
        sl["status"] = "pending"
        sl["pending_reason"] = "cap"
        sl["note"] = "判卷次数用完，稍后再判"
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=False)
    if not llm.ready(uid):
        sl["status"] = "pending"
        sl["pending_reason"] = "quota"
        sl["note"] = "模型暂时用不了：作答先存着，稍后可重判"
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=False)
    state["grade_calls"] = state.get("grade_calls", 0) + 1
    graded = grade(uid, sl, ans, paper, st, verd.get("at"))
    if graded is None:
        sl["status"] = "pending"
        sl["pending_reason"] = "model"
        sl["note"] = "模型暂时判不了，稍后可重判"
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=False)
    sl["attempts"] = sl.get("attempts", 0) + 1
    state["answers_total"] = state.get("answers_total", 0) + 1
    sl["feedback"] = graded.get("feedback") or ""
    sl["missing"] = graded.get("missing") or []
    sl["reasons"] = graded.get("reasons") or []
    sl["note"] = sl["feedback"] or verd["note"]
    if graded["pass"]:
        sl["status"] = "pass"
        sl["answer"] = ans
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=False)
    sl["status"] = "skipped" if sl["attempts"] >= ATTEMPTS_MAX else "retry"
    if sl["status"] == "skipped":
        return _advance(uid, kit_id, aid, paper, state, sl, st, rev, hard=False)
    state["rev"] = state.get("rev", 1) + 1
    if not store.save_tutor(uid, kit_id, aid, state["status"], state, rev):
        raise TutorConflict("这题已经答过了，刷新")
    return {"verdict": _verdict_of(sl, st, hard=False), "session": public(state, ready=llm.ready(uid))}


def _advance(uid: str, kit_id: str, aid: str, paper: dict[str, Any], state: dict[str, Any],
             sl: dict[str, Any], st: dict[str, Any], rev: int, hard: bool) -> dict[str, Any]:
    slots = state["slots"]
    cur = state.get("cur", 0)
    if cur < len(slots) and slots[cur] is sl:
        state["cur"] = cur + 1
    plan = state.get("plan") or slots
    if state["cur"] >= len(plan):
        state["status"] = "read"
    elif state["cur"] >= len(slots):
        _fill_station(uid, paper, state, plan[state["cur"]]["station"])
    state["rev"] = state.get("rev", 1) + 1
    if not store.save_tutor(uid, kit_id, aid, state["status"], state, rev):
        raise TutorConflict("这题已经答过了，刷新")
    return {"verdict": _verdict_of(sl, st, hard=hard), "session": public(state, ready=llm.ready(uid))}


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
    return {"prefill": prefill, "from": source, "review": review, "missing": missing, "session": public(state, ready=llm.ready(uid))}
