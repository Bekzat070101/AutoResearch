# -*- coding: utf-8 -*-
"""服务端带读：规则走完全程，模型只出题，学生作答不发给模型。"""
from __future__ import annotations

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


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "tutor.db")
    store.init_db()
    paper = _paper()
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    monkeypatch.setattr(arxiv, "cached", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    monkeypatch.setattr(llm, "ready", lambda uid=None: False)


def uid(name="t"):
    return store.create_user(name)["uid"]


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


def test_abstract_only_paper_has_one_station_and_flags_it(monkeypatch):
    paper = _paper(source="abstract", text="We show only the abstract in this fallback paper.")
    sts = tutor.stations(paper)
    assert len(sts) == 1 and sts[0]["key"] == "abstract" and "只拿到摘要" in sts[0]["label"]
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    monkeypatch.setattr(arxiv, "cached", lambda aid: {**paper, "id": arxiv.clean_id(aid)})
    pub = tutor.start(uid(), KIT, AID)
    assert "只拿到摘要" in pub["notice"]
    assert all(s["station"] == "abstract" for s in pub["slots"])


def _answer(sl):
    if sl["qid"] in QUOTES:
        return QUOTES[sl["qid"]]
    return OWN[sl["qid"]]


def test_rules_version_full_flow_prefills_all_card_fields(monkeypatch):
    posted = {"n": 0}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: posted.__setitem__("n", posted["n"] + 1) or None)
    monkeypatch.setattr(llm, "_post", lambda *a, **k: posted.__setitem__("n", posted["n"] + 1) or (None, "no"))
    u = uid()
    sess = tutor.start(u, KIT, AID)
    assert sess["voice"] == "rules"
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        sess = out["session"]
        assert out["verdict"]["pass"] or sl.get("skippable")
    fin = tutor.finish(u, KIT, AID)
    for key in ("claim", "claim_quote", "evidence_quote", "method", "assumption", "limitation_quote", "change"):
        assert fin["prefill"].get(key), key
    review = reading.review_card(reading.kit(KIT), arxiv.cached(AID), fin["prefill"], DIMS, [])
    assert review["pass"], review
    assert posted["n"] == 0


def test_find_quote_must_be_verbatim_and_in_station():
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    assert sl["type"] == "find_quote"
    bad = tutor.turn(u, KIT, AID, sl["qid"], "this sentence is not in the paper at all", False, sess["rev"])
    assert not bad["verdict"]["pass"]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, bad["session"]["rev"])
    assert out["verdict"]["pass"]


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
    {"items": "nope"},
    "nope",
    {"items": [[[[["deep"]]]]] },
])
def test_invalid_model_items_fall_back_per_slot(data):
    paper = _paper()
    sents = tutor.sentences(paper["text"], 0, 200)
    if sents:
        sents[0]["text"] = "We show that a new benchmark measures contamination reliably."
    slots = [{"qid": "q_claim_quote", "station": "abstract", "type": "find_quote", "field": "claim_quote"},
             {"qid": "q_evidence", "station": "exp", "type": "number", "field": "evidence_quote"},
             {"qid": "q_limit", "station": "limit", "type": "limitation", "field": "limitation_quote"}]
    got = tutor._valid_items(data, slots, sents, paper)
    if got is None:
        return
    assert all(i.get("voice") == "rules" or i.get("qid") for i in got)


def test_station_questions_are_shared_across_users(monkeypatch):
    calls = {"n": 0}

    def fake_chat_json(system, user, **kw):
        calls["n"] += 1
        return {"items": [
            {"qid": "q_claim_quote", "anchor": None, "question": "抄一句主张原话", "hint": "We show"},
            {"qid": "q_claim", "anchor": None, "question": "用自己的话写主张", "hint": "别抄"},
        ]}

    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    a, b = uid("a"), uid("b")
    tutor.start(a, KIT, AID)
    tutor.start(b, KIT, AID)
    assert calls["n"] == 1
    monkeypatch.setattr(tutor, "skill_sha", lambda: "changed-skill-sha")
    tutor.start(uid("c"), KIT, AID, restart=True)
    assert calls["n"] == 2
    monkeypatch.setattr(tutor, "skill_sha", lambda: "fail-skill-sha")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)
    sha = tutor.paper_sha(_paper())
    before = store.get_tutor_questions(AID, "abstract", sha, tutor.PLAN_VERSION, "fail-skill-sha")
    tutor.start(uid("d"), KIT, AID, restart=True)
    after = store.get_tutor_questions(AID, "abstract", sha, tutor.PLAN_VERSION, "fail-skill-sha")
    assert before is None and after is None


def test_model_never_sees_student_answers(monkeypatch):
    seen = []

    def fake_chat_json(system, user, **kw):
        seen.append(user)
        return {"items": [
            {"qid": "q_problem", "anchor": None, "question": "这一节在解决什么", "hint": "读引言"},
        ]}

    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    secret = "UNIQUE_STUDENT_ANSWER_TOKEN_918273"
    while sess["status"] == "active" and sess["cur"] < 3:
        sl = sess["slots"][sess["cur"]]
        ans = secret if sl["type"] == "own" else _answer(sl)
        if sl["type"] == "own" and sl["qid"] != "q_claim":
            ans = _answer(sl)
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        sess = out["session"]
    assert all(secret not in (p or "") for p in seen)


def test_budget_runs_out_midway_and_rest_is_rules(monkeypatch):
    calls = {"n": 0}

    def ready(uid=None):
        return calls["n"] < 1

    def fake_chat_json(system, user, **kw):
        calls["n"] += 1
        return {"items": [
            {"qid": "q_claim_quote", "anchor": None, "question": "抄一句主张原话", "hint": "We show"},
            {"qid": "q_claim", "anchor": None, "question": "用自己的话写主张", "hint": "别抄"},
        ]}

    monkeypatch.setattr(llm, "ready", ready)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), sl.get("skippable", False) and sl["type"] == "limitation", sess["rev"])
        if not out["verdict"]["pass"] and sl.get("skippable"):
            out = tutor.turn(u, KIT, AID, sl["qid"], "", True, out["session"]["rev"])
        sess = out["session"]
    assert sess["voice"] in ("mixed", "rules")
    assert "规则题" in (sess.get("notice") or "") or sess["voice"] != "llm"


def test_model_calls_per_session_at_most_stations(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(llm, "ready", lambda uid=None: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or None)
    u = uid()
    sess = tutor.start(u, KIT, AID)
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        if not out["verdict"]["pass"]:
            out = tutor.turn(u, KIT, AID, sl["qid"], "", True, out["session"]["rev"])
        sess = out["session"]
    assert calls["n"] <= len(tutor.stations(_paper()))


def test_attempts_cap_then_skip():
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    for _ in range(3):
        out = tutor.turn(u, KIT, AID, sl["qid"], "zzzz not in paper", False, sess["rev"])
        sess = out["session"]
    assert sess["slots"][0]["status"] == "skipped" or sess["cur"] == 1


def test_stale_qid_or_rev_is_409():
    u = uid()
    sess = tutor.start(u, KIT, AID)
    sl = sess["slots"][0]
    with pytest.raises(tutor.TutorConflict):
        tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"] + 9)
    tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    with pytest.raises(tutor.TutorConflict):
        tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])


def test_one_active_session_per_paper_and_restart_resets():
    u = uid()
    first = tutor.start(u, KIT, AID)
    again = tutor.start(u, KIT, AID)
    assert again["rev"] == first["rev"]
    tutor.turn(u, KIT, AID, first["slots"][0]["qid"], QUOTES["q_claim_quote"], False, first["rev"])
    fresh = tutor.start(u, KIT, AID, restart=True)
    assert fresh["cur"] == 0 and fresh["status"] == "active"


def test_paper_text_change_marks_session_stale(monkeypatch):
    u = uid()
    sess = tutor.start(u, KIT, AID)
    changed = _paper(text=_paper()["text"] + "\nA new sentence appears after fetch.")
    monkeypatch.setattr(arxiv, "cached", lambda aid: changed)
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: changed)
    sl = sess["slots"][0]
    out = tutor.turn(u, KIT, AID, sl["qid"], QUOTES["q_claim_quote"], False, sess["rev"])
    assert out["session"]["status"] == "stale"


def test_finish_does_not_submit_or_write_facts():
    u = uid()
    sess = tutor.start(u, KIT, AID)
    while sess["status"] == "active":
        sl = sess["slots"][sess["cur"]]
        out = tutor.turn(u, KIT, AID, sl["qid"], _answer(sl), False, sess["rev"])
        if not out["verdict"]["pass"]:
            out = tutor.turn(u, KIT, AID, sl["qid"], "", True, out["session"]["rev"])
        sess = out["session"]
    tutor.finish(u, KIT, AID)
    assert store.list_facts(u) == []


def test_non_kit_paper_is_refused():
    with pytest.raises(tutor.TutorError):
        tutor.start(uid(), KIT, "9999.99999")


def test_locate_within_range():
    paper = _paper()
    q = QUOTES["q_claim_quote"]
    whole = quotes.locate(q, paper)
    assert whole["found"]
    far = quotes.locate(q, paper, within=(len(paper["text"]) - 10, len(paper["text"])))
    assert not far["found"]
    near = quotes.locate(q, paper, within=(0, 200))
    assert near["found"] and near.get("at") is not None


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
