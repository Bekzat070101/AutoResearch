# -*- coding: utf-8 -*-
"""服务端带读：模型出题、模型按标准判卷；代码只钉硬事实。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import quotes  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402
import tutor  # noqa: E402

_REAL_CHAT_JSON = llm.chat_json

AID = "2009.03300"
KIT = "llm-eval"
PAD = (" This paragraph adds enough English words so the station is longer than two hundred "
       "characters and will not be merged into the next station by the length rule. ") * 3

DIMS = {"models": "LLaMA2", "task": "自建 canary 数据", "language": "英文", "metric": "p 值",
        "judge": "统计检验", "direction": "up", "contamination": "论文就是讲污染的"}

OWN = {
    "q_claim": "作者声称做成了一套能证明测试集污染的统计办法。",
    "q_problem": "这一节在讲模型可能把评测题背进参数里。",
    "q_method": "他们比较原始顺序和打乱顺序的似然。",
    "q_assumption": "测试集样本的原始顺序被当成可交换的。",
    "q_change": "我会在只出现一次的污染样本上再测一遍功效。",
}
QUOTES = {
    "q_claim_quote": "We show that a new benchmark measures contamination reliably.",
    "q_evidence": "On LLaMA2 the test reaches a p-value below 0.01 for canaries.",
    "q_limit": "The p-values presented do not have multiple test corrections applied.",
}


def _paper(**extra) -> dict:
    text = "\n".join([
        "We present a short abstract here. " + QUOTES["q_claim_quote"],
        "1 Introduction",
        "Large language models may memorize evaluation items from the internet. " + PAD,
        "2 Related Work",
        "Prior papers discussed contamination but did not give a numbered heading test. " + PAD,
        "3 A Multitask Test",
        "We compare original and shuffled order likelihoods in this section. " + PAD,
        "4 Experiments",
        QUOTES["q_evidence"] + " " + PAD,
        "5 Limitations",
        QUOTES["q_limit"] + " " + PAD,
        "6 Conclusion",
        "We believe single-duplication tests remain an open problem. " + PAD,
        "References",
        "Some citations live here.",
        "1 Introduction",
        "This appendix reprint of Introduction must not become a station. " + PAD,
    ])
    return {"id": AID, "title": "Measuring Massive Multitask Language Understanding",
            "source": "html", "url": f"https://arxiv.org/abs/{AID}", "text": text, **extra}


def _tutor_items() -> dict:
    return {"items": [
        {"qid": "q_claim_quote", "anchor": None, "question": "抄一句主张原话", "hint": "We show",
         "criteria": ["引的句子说的是本文做成或提出的东西"]},
        {"qid": "q_claim", "anchor": None, "question": "用自己的话写主张", "hint": "别抄",
         "criteria": ["说出了本文做成或提出的具体东西"]},
        {"qid": "q_problem", "anchor": None, "question": "这一节在解决什么", "hint": "读引言",
         "criteria": ["说出本节要解决的问题"]},
        {"qid": "q_method", "anchor": None, "question": "他们怎么做的", "hint": "写步骤",
         "criteria": ["写出至少一个具体步骤或做法"]},
        {"qid": "q_assumption", "anchor": None, "question": "默认没检验的前提", "hint": "找前提",
         "criteria": ["指出一个前提或默认条件"]},
        {"qid": "q_evidence", "anchor": None, "question": "抄一句带数字的结果", "hint": "找数字",
         "criteria": ["句子里的数字是本文自己的实验结果"]},
        {"qid": "q_limit", "anchor": None, "question": "抄一句局限", "hint": "看局限段",
         "criteria": ["句子是作者承认没做到的事"]},
        {"qid": "q_change", "anchor": None, "question": "你会改哪一处", "hint": "只写一处",
         "criteria": ["是一处具体改动并说明改了什么"]},
    ]}


def _grade_ok(user: str, **extra) -> dict:
    n = len(re.findall(r"^C\d+ \|", user or "", re.M)) or 1
    return {"pass": True, "reasons": [f"C{i + 1} 满足" for i in range(n)],
            "missing": [], "feedback": extra.get("feedback") or "对，说到了要点"}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "tutor.db")
    store.init_db()
    paper = _paper()
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    monkeypatch.setattr(arxiv, "cached", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    monkeypatch.setattr(llm, "ready", lambda uid=None: False)


@pytest.fixture
def fake_model(monkeypatch):
    counts = {"paper-tutor": 0, "paper-quiz-grade": 0, "other": 0}
    seen = []

    def chat_json(system, user, **kw):
        tag = kw.get("tag") or ""
        seen.append({"tag": tag, "user": user, "kw": kw, "system": system})
        if tag == "paper-tutor":
            counts["paper-tutor"] += 1
            return _tutor_items()
        if tag == "paper-quiz-grade":
            counts["paper-quiz-grade"] += 1
            return _grade_ok(user)
        counts["other"] += 1
        return None

    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    monkeypatch.setattr(llm, "enabled", lambda uid=None: True)
    monkeypatch.setattr(llm, "chat_json", chat_json)
    return {"counts": counts, "seen": seen}


def uid(name="t"):
    return store.create_user(name)["uid"]


def _answer(sl):
    if sl["qid"] in QUOTES:
        return QUOTES[sl["qid"]]
    return OWN[sl["qid"]]


def test_stations_use_numbered_headings_and_merge_unknown_into_method():
    keys = [s["key"] for s in tutor.stations(_paper())]
    assert keys == ["abstract", "intro", "method", "exp", "limit", "concl"]
    text = _paper()["text"]
    assert "appendix reprint" in text
    last = tutor.stations(_paper())[-1]
    assert "appendix reprint" not in text[last["start"]:last["end"]]


def test_stations_fallback_to_arxiv_sections_when_few_numbered_headings():
    text = "\n".join([
        "Abstract\nA short abstract about the method.\n",
        "Introduction\n" + PAD,
        "Methods\n" + PAD,
        "Experiments\n" + PAD,
        "Limitations\n" + PAD,
        "Conclusion\n" + PAD,
    ])
    paper = _paper(text=text)
    keys = [s["key"] for s in tutor.stations(paper)]
    assert "intro" in keys and "method" in keys


def test_abstract_only_paper_has_one_station_and_flags_it(fake_model, monkeypatch):
    paper = _paper(source="abstract", text="We show only the abstract in this fallback paper.")
    sts = tutor.stations(paper)
    assert len(sts) == 1 and sts[0]["key"] == "abstract" and "只拿到摘要" in sts[0]["label"]
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    monkeypatch.setattr(arxiv, "cached", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    pub = tutor.start(uid(), KIT, AID)
    assert "只拿到摘要" in pub["notice"]
    assert all(s["station"] == "abstract" for s in pub["slots"])
    assert pub["summary_only"] and pub["total"] == 3
    assert not any(s["type"] in ("number", "limitation") for s in pub["slots"])


def test_full_flow_with_model_grading(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    hard_ok = 0
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        if out["verdict"]["pass"] or out["verdict"]["state"] == "pending":
            hard_ok += 1
        sess = out["session"]
        assert out["verdict"]["pass"] or sl.get("skippable") or out["verdict"]["state"] == "pending"
    fin = tutor.finish(u, KIT, AID)
    for key in ("claim", "claim_quote", "evidence_quote", "method", "assumption", "limitation_quote", "change"):
        assert fin["prefill"].get(key), key
    review = reading.review_card(reading.kit(KIT), arxiv.cached(AID), fin["prefill"], DIMS, [])
    assert review["pass"], review
    assert fake_model["counts"]["paper-quiz-grade"] == hard_ok
    assert fake_model["counts"]["paper-tutor"] <= 6


def test_find_quote_must_be_verbatim_and_in_station(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    assert sl["type"] == "find_quote"
    before = fake_model["counts"]["paper-quiz-grade"]
    bad = tutor.turn(u, KIT, AID, sl["qid"], "this sentence is not in the paper at all", False, sess["rev"])
    assert not bad["verdict"]["pass"]
    assert fake_model["counts"]["paper-quiz-grade"] == before
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, bad["session"]["rev"])
    assert out["verdict"]["pass"]
    assert fake_model["counts"]["paper-quiz-grade"] == before + 1


def test_find_quote_rejects_paper_title():
    paper = _paper()
    paper = {**paper, "text": paper["title"] + "\n" + paper["text"]}
    slot = {"type": "find_quote", "field": "claim_quote", "question": "q", "hint": ""}
    st = {"key": "abstract", "label": "摘要", "start": 0, "end": len(paper["text"])}
    assert not tutor.judge(slot, paper["title"], paper, st)["pass"]
    assert tutor.judge(slot, QUOTES["q_claim_quote"], paper, st)["pass"]
    body = "We show that benchmark contamination is detectable even when models are small."
    titled = {
        **_paper(),
        "title": "Benchmark contamination is detectable",
        "text": ("Lead-in words that push the quote past the title zone. " + body + " " + PAD),
    }
    st2 = {"key": "abstract", "label": "摘要", "start": 0, "end": len(titled["text"])}
    assert not tutor.judge(slot, titled["title"], titled, st2)["pass"]
    assert tutor.judge(slot, body, titled, st2)["pass"]


def test_number_answer_needs_a_digit():
    paper = _paper()
    slot = {"type": "number", "field": "evidence_quote", "question": "q", "hint": ""}
    st = {"key": "exp", "label": "实验", "start": 0, "end": len(paper["text"])}
    assert not tutor.judge(slot, "no digits in this answer text", paper, st)["pass"]
    assert tutor.judge(slot, QUOTES["q_evidence"], paper, st)["pass"]


def test_limitation_judge_matches_review_card():
    paper = _paper()
    ok = reading.check_limitation(QUOTES["q_limit"], paper)
    st = next(s for s in tutor.stations(paper) if s["key"] == "limit")
    slot = {"type": "limitation", "field": "limitation_quote", "question": "q", "hint": ""}
    assert tutor.judge(slot, QUOTES["q_limit"], paper, st)["pass"] == ok["pass"]
    intro = "Large language models may memorize evaluation items from the internet."
    no_limit = _paper(text="Abstract\nWe show a result without naming a limitations heading.\n" + intro)
    via_card = reading.check_limitation(intro, no_limit)
    via_tutor = tutor.judge(slot, intro, no_limit, {"key": "abstract", "label": "摘要", "start": 0, "end": len(no_limit["text"])})
    assert via_card["pass"] == via_tutor["pass"]


def test_own_words_rules_same_as_card_and_no_copy_from_question():
    paper = _paper()
    slot = {"type": "own", "field": "claim", "question": "用你自己的话（10–160 字）说：作者声称做成了什么", "hint": "不要抄原句"}
    st = {"key": "abstract", "label": "摘要", "start": 0, "end": 80}
    copied = tutor.judge(slot, "用你自己的话（10–160 字）说：作者声称做成了什么", paper, st)
    assert not copied["pass"]
    prefixed_src = "我认为：" + QUOTES["q_claim_quote"]
    assert not tutor.judge(slot, prefixed_src, paper, st)["pass"]
    prefixed_q = "答：" + slot["question"]
    assert not tutor.judge(slot, prefixed_q, paper, st)["pass"]
    ok = tutor.judge(slot, OWN["q_claim"], paper, st)
    card_ok, _ = reading._check_own(OWN["q_claim"], paper, "主张")
    assert ok["pass"] and card_ok


@pytest.mark.parametrize("data", [
    {"items": [{"qid": "q_claim_quote", "anchor": "S99", "question": "好题目啊", "hint": "x"}]},
    {"items": [{"qid": "unknown", "anchor": None, "question": "好题目啊", "hint": "x"}]},
    {"items": [
        {"qid": "q_claim_quote", "anchor": None, "question": "第一题面文字", "hint": "x"},
        {"qid": "q_claim_quote", "anchor": None, "question": "重复题面文字", "hint": "x"},
    ]},
    {"items": [{"qid": "q_evidence", "anchor": "S1", "question": "数字题", "hint": "x"}]},
    {"items": [{"qid": "q_limit", "anchor": "S1", "question": "局限题", "hint": "x"}]},
    {"items": [{"qid": "q_claim_quote", "anchor": "S1",
                "question": "We show that a new benchmark measures contamination reliably. 泄露", "hint": "x"}]},
    {"items": [{"qid": "q_claim_quote", "anchor": None, "question": 12, "hint": "x"}]},
    {"items": [{"qid": "q_claim_quote", "anchor": None, "question": "x" * 80, "hint": "x"}]},
    {"items": [{"qid": "q_claim_quote", "anchor": None, "question": "缺标准的题", "hint": "x"}]},
    {"items": [{"qid": "q_claim_quote", "anchor": None, "question": "标准太长", "hint": "x",
                "criteria": ["x" * 80]}]},
    {"items": [{"qid": "q_claim_quote", "anchor": None, "question": "标准太多", "hint": "x",
                "criteria": ["a", "b", "c", "d", "e"]}]},
    {"items": [{"qid": "q_claim_quote", "anchor": "S1", "question": "标准含锚点", "hint": "x",
                "criteria": ["We show that a new benchmark measures contamination reliably."]}]},
    {"items": "nope"},
    "nope",
    {"items": [[[[["deep"]]]]] },
])
def test_invalid_model_items_fall_back_per_slot(data):
    paper = _paper()
    sents = tutor.sentences(paper["text"], 0, 200)
    if sents:
        sents[0]["text"] = "We show that a new benchmark measures contamination reliably."
        sents[0]["s"] = "S1"
    slots = [{"qid": "q_claim_quote", "station": "abstract", "type": "find_quote", "field": "claim_quote"},
             {"qid": "q_evidence", "station": "exp", "type": "number", "field": "evidence_quote"},
             {"qid": "q_limit", "station": "limit", "type": "limitation", "field": "limitation_quote"}]
    got = tutor._valid_items(data, slots, sents, paper)
    if got is None:
        return
    assert all(i.get("voice") == "rules" for i in got)


def test_station_questions_are_shared_across_users(fake_model, monkeypatch):
    a, b = uid("a"), uid("b")
    tutor.start(a, KIT, AID)
    tutor.start(b, KIT, AID)
    assert fake_model["counts"]["paper-tutor"] == 1
    monkeypatch.setattr(tutor, "skill_sha", lambda: "changed-skill-sha")
    tutor.start(uid("c"), KIT, AID, restart=True)
    assert fake_model["counts"]["paper-tutor"] == 2
    monkeypatch.setattr(tutor, "skill_sha", lambda: "fail-skill-sha")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)
    sha = tutor.paper_sha(_paper())
    before = store.get_tutor_questions(AID, "abstract", sha, tutor.PLAN_VERSION, "fail-skill-sha")
    tutor.start(uid("d"), KIT, AID, restart=True)
    after = store.get_tutor_questions(AID, "abstract", sha, tutor.PLAN_VERSION, "fail-skill-sha")
    assert before is None and after is None


def test_answers_only_go_to_grader(fake_model):
    a, b = uid("a"), uid("b")
    sess_a = tutor.start(a, KIT, AID)
    secret = "UNIQUE_STUDENT_ANSWER_TOKEN_918273"
    while sess_a["status"] == "active" and sess_a["cur"] < 3:
        sl = sess_a["slots"][sess_a["cur"]]
        ans = secret if sl["type"] == "own" else _answer(sl)
        out = tutor.turn(a, KIT, AID, sl["qid"], ans, False, sess_a["rev"])
        sess_a = out["session"]
    tutor.start(b, KIT, AID)
    for hit in fake_model["seen"]:
        if hit["tag"] == "paper-quiz-grade" and secret in (hit["user"] or ""):
            continue
        assert secret not in (hit["user"] or "")
    b_grade = [h["user"] for h in fake_model["seen"] if h["tag"] == "paper-quiz-grade"
               and "题型" in (h["user"] or "")]
    # B 还没作答时不应出现 A 的秘密串
    later = [h for h in fake_model["seen"] if h["tag"] == "paper-tutor"]
    assert all(secret not in (h["user"] or "") for h in later)


def test_budget_exhausted_marks_pending(fake_model, monkeypatch):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert out["verdict"]["pass"]
    monkeypatch.setattr(llm, "ready", lambda uid=None: False)
    sl = out["session"]["slots"][out["session"]["cur"]]
    pending = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, out["session"]["rev"])
    assert pending["verdict"]["state"] == "pending"
    assert pending["session"]["cur"] > out["session"]["cur"]
    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    again = tutor.turn(u, KIT, AID, sl["qid"], "", False, pending["session"]["rev"], True)
    assert again["verdict"]["pass"] or again["verdict"]["state"] in ("pass", "pending")


def test_model_calls_per_session_at_most_stations(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        if not out["verdict"]["pass"] and out["verdict"]["state"] != "pending":
            out = tutor.turn(u, KIT, AID, sl["qid"], "", True, out["session"]["rev"])
        sess = out["session"]
    assert fake_model["counts"]["paper-tutor"] <= len(tutor.stations(_paper()))
    assert fake_model["counts"]["paper-quiz-grade"] <= tutor.GRADE_MAX


def test_attempts_cap_then_skip(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    for _ in range(3):
        out = tutor.turn(u, KIT, AID, sl["qid"], "zzzz not in paper", False, sess["rev"])
        sess = out["session"]
    assert sess["slots"][0]["status"] == "skipped"
    assert sess["cur"] == 1
    assert fake_model["counts"]["paper-quiz-grade"] == 0


def test_stale_qid_or_rev_is_409(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    with pytest.raises(tutor.TutorConflict):
        tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"] + 9)
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    with pytest.raises(tutor.TutorConflict):
        tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    with pytest.raises(tutor.TutorConflict):
        tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, out["session"]["rev"])


def test_one_active_session_per_paper_and_restart_resets(fake_model):
    u = uid()
    first = tutor.start(u, KIT, AID)
    again = tutor.start(u, KIT, AID)
    assert again["rev"] == first["rev"]
    tutor.turn(u, KIT, AID, first["slots"][0]["qid"], QUOTES["q_claim_quote"], False, first["rev"])
    fresh = tutor.start(u, KIT, AID, restart=True)
    assert fresh["cur"] == 0 and fresh["status"] == "active"


def test_paper_text_change_marks_session_stale(fake_model, monkeypatch):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    changed = _paper(text=_paper()["text"] + "\nA new sentence appears after fetch.")
    monkeypatch.setattr(arxiv, "cached", lambda aid: changed)
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: changed)
    sl = sess["slots"][0]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert out["session"]["status"] == "stale"


def test_finish_does_not_submit_or_write_facts(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        if not out["verdict"]["pass"] and out["verdict"]["state"] != "pending":
            out = tutor.turn(u, KIT, AID, sl["qid"], "", True, out["session"]["rev"])
        sess = out["session"]
    tutor.finish(u, KIT, AID)
    assert store.list_facts(u) == []


def test_non_kit_paper_is_refused():
    with pytest.raises((tutor.TutorError, arxiv.ArxivError)):
        tutor.start(uid(), KIT, "not-an-id")


def test_start_refused_without_model():
    with pytest.raises(tutor.TutorError, match="用不了模型"):
        tutor.start(uid(), KIT, AID)


def test_locate_within_range():
    paper = _paper()
    q = QUOTES["q_claim_quote"]
    whole = quotes.locate(q, paper)
    assert whole["found"]
    far = quotes.locate(q, paper, within=(len(paper["text"]) - 10, len(paper["text"])))
    assert not far["found"]
    near = quotes.locate(q, paper, within=(0, 200))
    assert near["found"] and near.get("at") is not None


def test_hard_fail_never_calls_grader(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    q = sl["question"]
    cases = ["short", QUOTES["q_claim_quote"][:-1] + "X", "我认为：" + QUOTES["q_claim_quote"], "答：" + q]
    rev = sess["rev"]
    for ans in cases:
        out = tutor.turn(u, KIT, AID, sl["qid"], ans, False, rev)
        assert not out["verdict"]["pass"]
        rev = out["session"]["rev"]
        if out["session"]["slots"][0]["status"] == "skipped":
            break
    assert fake_model["counts"]["paper-quiz-grade"] == 0


def test_anchor_hit_passes_without_grader(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    paper = _paper()
    loc = quotes.locate(QUOTES["q_claim_quote"], paper)
    row = store.get_tutor(u, KIT, AID)
    sl = row["state"]["slots"][0]
    sl["anchor"] = {"s": "S1", "start": loc["at"], "end": loc["at"] + len(QUOTES["q_claim_quote"])}
    store.save_tutor(u, KIT, AID, row["status"], row["state"], None)
    before = fake_model["counts"]["paper-quiz-grade"]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, row["state"]["rev"])
    assert out["verdict"]["pass"] and out["verdict"]["hard"]
    assert fake_model["counts"]["paper-quiz-grade"] == before


@pytest.mark.parametrize("data", [
    {"pass": "false", "reasons": ["a"], "missing": [], "feedback": "x"},
    {"pass": False, "reasons": "nope", "missing": [], "feedback": "x"},
    {"pass": False, "reasons": ["a", "b"], "missing": [], "feedback": "x"},
    {"pass": True, "reasons": ["a"], "missing": ["缺内容"], "feedback": "x"},
    {"pass": False, "reasons": [[[[["deep"]]]]], "missing": [], "feedback": "x"},
])
def test_grade_shape_is_validated(fake_model, monkeypatch, data):
    def chat_json(system, user, **kw):
        if kw.get("tag") == "paper-tutor":
            return _tutor_items()
        return data

    monkeypatch.setattr(llm, "chat_json", chat_json)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert out["verdict"]["state"] == "pending"
    assert out["session"]["slots"][0]["status"] == "pending"


def test_grade_feedback_leaking_anchor_is_scrubbed():
    paper = _paper()
    st = {"key": "abstract", "label": "摘要"}
    leaked = QUOTES["q_claim_quote"]
    got = tutor._valid_grade(
        {"pass": False, "reasons": [leaked], "missing": [leaked[:30]], "feedback": leaked},
        1, leaked, st)
    assert got is not None
    assert leaked not in got["feedback"]
    assert all(leaked not in x for x in got["reasons"])


def test_pending_then_regrade_passes(fake_model, monkeypatch):
    n = {"g": 0}

    def chat_json(system, user, **kw):
        if kw.get("tag") == "paper-tutor":
            return _tutor_items()
        n["g"] += 1
        if n["g"] == 1:
            return None
        return _grade_ok(user)

    monkeypatch.setattr(llm, "chat_json", chat_json)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    first = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert first["verdict"]["state"] == "pending"
    again = tutor.turn(u, KIT, AID, sl["qid"], "", False, first["session"]["rev"], True)
    assert again["verdict"]["pass"]


def test_regrade_cap(fake_model, monkeypatch):
    def chat_json(system, user, **kw):
        if kw.get("tag") == "paper-tutor":
            return _tutor_items()
        return None

    monkeypatch.setattr(llm, "chat_json", chat_json)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    first = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    rev = first["session"]["rev"]
    for _ in range(tutor.REGRADE_MAX):
        out = tutor.turn(u, KIT, AID, sl["qid"], "", False, rev, True)
        rev = out["session"]["rev"]
    with pytest.raises(tutor.TutorError, match="重判"):
        tutor.turn(u, KIT, AID, sl["qid"], "", False, rev, True)


def test_free_text_answer_passes(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    sess = out["session"]
    sl = sess["slots"][sess["cur"]]
    assert sl["type"] == "own"
    out = tutor.turn(u, KIT, AID, sl["qid"], OWN["q_claim"], False, sess["rev"])
    assert out["verdict"]["pass"]


def test_empty_or_too_long_answer_costs_no_attempt(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    with pytest.raises(tutor.TutorError, match="先写答案"):
        tutor.turn(u, KIT, AID, sl["qid"], "", False, sess["rev"])
    with pytest.raises(tutor.TutorError, match="太长"):
        tutor.turn(u, KIT, AID, sl["qid"], "字" * 601, False, sess["rev"])
    again = tutor.get(u, KIT, AID)
    assert again["slots"][0]["attempts"] == 0


def test_skip_allowed_after_first_fail_only(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    with pytest.raises(tutor.TutorError, match="跳过"):
        tutor.turn(u, KIT, AID, sl["qid"], "", True, sess["rev"])
    bad = tutor.turn(u, KIT, AID, sl["qid"], "zzzz not in paper", False, sess["rev"])
    skipped = tutor.turn(u, KIT, AID, sl["qid"], "", True, bad["session"]["rev"])
    assert skipped["session"]["slots"][0]["status"] == "skipped"


def test_old_pending_hard_fail_stays_retry(fake_model, monkeypatch):
    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    monkeypatch.setattr(llm, "enabled", lambda uid=None: True)

    def chat_json(system, user, **kw):
        if kw.get("tag") == "paper-quiz-grade":
            return None
        return _tutor_items()

    monkeypatch.setattr(llm, "chat_json", chat_json)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert out["session"]["slots"][0]["status"] == "pending"
    cur = out["session"]["cur"]
    assert cur >= 1
    bad = tutor.turn(u, KIT, AID, sl["qid"], "this sentence is not in the paper at all",
                     False, out["session"]["rev"])
    slot = next(s for s in bad["session"]["slots"] if s["qid"] == sl["qid"])
    assert slot["status"] == "retry"
    assert bad["session"]["cur"] == cur
    assert QUOTES["q_claim_quote"] in (slot.get("last") or "")


def test_restart_bumps_rev_and_rejects_old(fake_model):
    u = uid()
    first = tutor.start(u, KIT, AID)
    sl = first["slots"][0]
    fresh = tutor.start(u, KIT, AID, restart=True)
    assert fresh["rev"] == first["rev"] + 1
    with pytest.raises(tutor.TutorConflict):
        tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, first["rev"])


def test_regrade_without_answer_is_error(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    with pytest.raises(tutor.TutorError):
        tutor.turn(u, KIT, AID, sl["qid"], "", False, sess["rev"], True)


def test_excerpt_keeps_late_sentence():
    quote = QUOTES["q_claim_quote"]
    long = ("word " * 1800) + quote + (" tail " * 80)
    paper = {**_paper(), "text": long}
    st = {"key": "abstract", "label": "摘要", "start": 0, "end": len(long)}
    at = long.find(quote)
    slot = {"type": "find_quote", "field": "claim_quote", "question": "q", "hint": "", "anchor": {}}
    ex = tutor._excerpt(paper, st, slot, at)
    assert quote in ex


def test_grade_calls_survive_restart(fake_model):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    row = store.get_tutor(u, KIT, AID)
    before = row["state"]["grade_calls"]
    assert before >= 1
    tutor.start(u, KIT, AID, restart=True)
    row2 = store.get_tutor(u, KIT, AID)
    assert row2["state"]["grade_calls"] == before


def test_old_session_without_criteria_uses_template_criteria(fake_model, monkeypatch):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    row = store.get_tutor(u, KIT, AID)
    sl = row["state"]["slots"][0]
    sl.pop("criteria", None)
    store.save_tutor(u, KIT, AID, row["status"], row["state"], None)
    seen = []

    def chat_json(system, user, **kw):
        seen.append(user)
        if kw.get("tag") == "paper-quiz-grade":
            return _grade_ok(user)
        return _tutor_items()

    monkeypatch.setattr(llm, "chat_json", chat_json)
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, row["state"]["rev"])
    assert out["verdict"]["pass"] or out["verdict"]["state"] == "pending"
    grade_users = [x for x in seen if "标准：" in x]
    assert grade_users
    assert "C1 |" in grade_users[0]


def test_grade_call_uses_short_total_timeout(fake_model, monkeypatch):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    calls = []

    def fetch2(req, timeout=0, idle=0, max_bytes=0):
        calls.append({"timeout": timeout, "idle": idle})
        import json
        payload = {
            "choices": [{"message": {"content":
                '{"pass": true, "reasons": ["C1 满足"], "missing": [], "feedback": "对"}'}}]
        }
        return json.dumps(payload).encode("utf-8")

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "fetch", fetch2)
    monkeypatch.setattr(llm, "enabled", lambda uid=None: True)
    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    monkeypatch.setattr(llm, "chat_json", _REAL_CHAT_JSON)
    sl = sess["slots"][0]
    tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert len(calls) == 1
    assert calls[0]["timeout"] == 30


@pytest.mark.real_auth
def test_routes_require_owner(monkeypatch):
    a = store.create_user("owner_a")
    b = store.create_user("owner_b")
    token_a = store.create_session(a["uid"], 3600)
    client = TestClient(main.app)
    h = {"Authorization": f"Bearer {token_a}"}
    r = client.get(f"/api/tutor/{AID}?uid={b['uid']}&kit={KIT}", headers=h)
    assert r.status_code in (401, 403, 404)
    r2 = client.post(f"/api/tutor/{AID}/start", json={"uid": b["uid"], "kit": KIT}, headers=h)
    assert r2.status_code in (401, 403, 404)
    bare = client.get(f"/api/tutor/{AID}?uid={a['uid']}&kit={KIT}")
    assert bare.status_code == 401
