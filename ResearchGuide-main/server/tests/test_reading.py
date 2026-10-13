# -*- coding: utf-8 -*-
"""研读层的离线测试：不联网，论文正文用一篇按真实结构拼的样例。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402
import quotes  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402

PAPER = {
    "id": "2310.17623", "title": "Proving Test Set Contamination in Black Box Language Models", "source": "html",
    "url": "https://arxiv.org/abs/2310.17623",
    "text": "\n".join([
        "Abstract",
        "We show that it is possible to provide provable guarantees of test set contamination in language models.",
        "1 Introduction",
        "Large language models are trained on vast amounts of internet data, prompting concerns about memorized benchmarks.",
        "Our test flags contamination when the canonical ordering is significantly more likely than shuffled orderings.",
        "4 Experiments",
        "On LLaMA2 the test reaches a p-value below 0.01 for the contaminated canaries in Table 2.",
        "6 Limitations",
        "First, the p-values presented in this paper do not have multiple test corrections applied, as it is difficult to define the hypotheses.",
        "7 Conclusion",
        "We believe it is an exciting open problem to build tests at the single-duplication-count regime.",
    ]),
}

GOOD = {
    "claim": "不看预训练数据，也能用统计检验证明模型见过某个测试集",
    "claim_quote": "We show that it is possible to provide provable guarantees of test set contamination",
    "evidence_quote": "the test reaches a p-value below 0.01 for the contaminated canaries",
    "method": "比较原始顺序和打乱顺序的似然",
    "assumption": "测试集里样本的原始顺序是可交换的，打乱不改变分布",
    "limitation_quote": "the p-values presented in this paper do not have multiple test corrections applied",
    "change": "在只出现一次的污染样本上重做实验，看检验还剩多少功效",
}
DIMS = {"models": "LLaMA2", "task": "自建 canary 数据", "language": "英文", "metric": "p 值", "judge": "统计检验",
        "direction": "up", "contamination": "论文就是讲污染的"}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r.db")
    store.init_db()
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: {**PAPER, "id": arxiv.clean_id(aid)})


def uid():
    return store.create_user("t")["uid"]


def test_kit_file_is_well_formed():
    k = reading.kit("llm-eval")
    assert len(k["papers"]) >= 8 and all(p["url"].startswith("https://arxiv.org/abs/") for p in k["papers"])
    assert all(o["quote"] and o["section"] for o in k["open_problems"])
    assert {d.get("role") for d in k["dimensions"]} >= {"metric", "direction"}
    assert all(d["url"].startswith("https://github.com/") for d in k["datasets"])


def test_clean_id_accepts_common_forms():
    for raw in ["2310.17623", "2310.17623v2", "https://arxiv.org/abs/2310.17623", "https://arxiv.org/pdf/2310.17623v1.pdf"]:
        assert arxiv.clean_id(raw) == "2310.17623"
    with pytest.raises(arxiv.ArxivError):
        arxiv.clean_id("not-an-id")


def test_quote_must_be_verbatim():
    assert quotes.locate("the p-values presented in this paper do not have multiple test corrections applied", PAPER)["section"] == "limitations"
    assert not quotes.locate("the p-values in this paper lack multiple test corrections", PAPER)["found"]
    assert not quotes.locate("too short", PAPER)["found"]


def test_good_card_passes_and_writes_ledger():
    u = uid()
    card = reading.submit_card(u, "llm-eval", "2310.17623", GOOD, DIMS)
    assert card["status"] == "pass", card["review"]["checks"]
    assert card["fact"]["source"] == "behavior" and card["fact"]["value"].startswith("已证明")
    again = reading.submit_card(u, "llm-eval", "2310.17623v2", GOOD, DIMS)
    assert again["version"] == 2 and again["prev_passed"] == card["review"]["passed"] and "fact" not in again


def test_check_limitation_matches_review_card():
    ok = reading.check_limitation(GOOD["limitation_quote"], PAPER)
    assert ok["pass"]
    bad = reading.check_limitation("Large language models are trained on vast amounts of internet data", PAPER)
    assert not bad["pass"] and "局限" in (bad.get("reason") or "")


def test_limitation_must_come_from_limitations_section():
    bad = dict(GOOD, limitation_quote="Large language models are trained on vast amounts of internet data")
    review = reading.submit_card(uid(), "llm-eval", "2310.17623", bad, DIMS)["review"]
    lim = next(c for c in review["checks"] if c["key"] == "limitation_quote")
    assert not lim["pass"] and "局限" in lim["note"]
    assert review["next_step"].startswith("先改")


def test_own_words_cannot_be_copied_from_paper():
    bad = dict(GOOD, change="Our test flags contamination when the canonical ordering is significantly more likely")
    review = reading.submit_card(uid(), "llm-eval", "2310.17623", bad, DIMS)["review"]
    assert not next(c for c in review["checks"] if c["key"] == "change")["pass"]


def test_decision_log_needs_reasons_for_rejections():
    log = [{"suggestion": "A1", "action": "adopt"}, {"suggestion": "A2", "action": "reject", "reason": ""}]
    review = reading.submit_card(uid(), "llm-eval", "2310.17623", GOOD, DIMS, log)["review"]
    assert not next(c for c in review["checks"] if c["key"] == "decision_log")["pass"]


def test_matrix_flags_conflicts_and_empty_columns():
    u = uid()
    for aid, direction, judge in [("2310.17623", "up", ""), ("2308.08493", "down", ""), ("2306.05685", "up", "")]:
        reading.submit_card(u, "llm-eval", aid, GOOD, dict(DIMS, direction=direction, judge=judge))
    m = reading.matrix(u, "llm-eval")
    assert m["ready"] and len(m["rows"]) == 3
    assert "judge" in m["flags"]["empty_columns"]
    (group,) = m["flags"]["conflicts"]  # 同一指标一组，不再逐对列出
    assert set(group["up"]) == {"2310.17623", "2306.05685"} and group["down"] == ["2308.08493"]
    assert (group["up_total"], group["down_total"]) == (2, 1)


def test_matrix_ready_with_one_passed_card():
    u = uid()
    reading.submit_card(u, "llm-eval", "2310.17623", GOOD, DIMS)
    m = reading.matrix(u, "llm-eval")
    assert m["need_more"] == 0 and m["ready"] and len(m["rows"]) == 1


def test_triage_requires_a_reason(monkeypatch):
    monkeypatch.setattr(arxiv, "recent", lambda *a, **k: [
        {"id": "2501.00001", "title": "A", "summary": "s", "published": "2026-10-03", "authors": ["x"], "url": "https://arxiv.org/abs/2501.00001"},
        {"id": "2501.00002", "title": "B", "summary": "s", "published": "2026-10-03", "authors": ["y"], "url": "https://arxiv.org/abs/2501.00002"}])
    u = uid()
    with pytest.raises(reading.ReadingError):
        reading.triage(u, "llm-eval", "2501.00001", "keep", "好")
    out = reading.triage(u, "llm-eval", "2501.00001", "keep", "和我想查的污染检测直接相关")
    assert out["done_today"] == 1 and [i["arxiv_id"] for i in out["items"]] == ["2501.00002"]


def test_brief_carries_the_rules():
    text = reading.brief("llm-eval", "2310.17623")
    assert "不替学生写" in text and "逐字" in text and "arxiv.org/abs/2310.17623" in text
