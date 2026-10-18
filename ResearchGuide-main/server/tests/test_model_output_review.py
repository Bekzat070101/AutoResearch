# -*- coding: utf-8 -*-
"""Codex 第十一轮：项目评阅里模型给的结构不对、证据是编的；关服务时在评的那份；每日分拣跨天。不调真的模型。"""
from __future__ import annotations

import io
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402
import submission  # noqa: E402
import workpool  # noqa: E402

PROJECT = {"name": "用公开评论数据做情感分类基线", "practices": "数据清洗、分类评价",
           "todo": "做两个基线并报告每类精确率召回率", "url": "https://example.org/p/1"}
README = ("## 题目\n我们解决公开数据中的分类问题，比较结果是否稳定。" + "说明文字。" * 20
          + "\n## 我做了什么\n我读了数据，results/table.csv 是结果。\n## 怎么复现\n运行 src/main.py\n## 还没做完的\n分类器和指标还没做完。\n")


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r11.db")
    store.init_db()


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", README)
        z.writestr("src/main.py", "print(1)")
        z.writestr("results/table.csv", "a,b\n1,2\n")
    return buf.getvalue()


def _with_model(monkeypatch, data):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: data)


def _criteria(**over):
    base = {c["key"]: {"key": c["key"], "status": "pass", "evidence": [{"file": "README.md", "quote": "我读了数据"}],
                       "comment": "做到了", "fix": ""} for c in submission.CRITERIA}
    for k, v in over.items():
        base[k] = {**base[k], **v}
    return {"criteria": list(base.values()), "summary": "好"}


@pytest.mark.parametrize("bad", [
    {"criteria": [{"key": [], "status": "pass"}]},
    {"criteria": [{"key": {}, "status": "pass"}]},
])
def test_a_criterion_key_of_the_wrong_type_falls_back_to_rules(monkeypatch, bad):
    """key 是列表、对象：原来建字典时就 500，额度扣了、评阅也没存（Codex 复现）。现在回退规则评阅。"""
    _with_model(monkeypatch, bad)
    r = submission.review(_zip(), PROJECT)
    assert r["voice"] == "rules" and r["total"] == len(submission.CRITERIA)


@pytest.mark.parametrize("evidence", [1, True, "README.md", {"file": "README.md"}])
def test_evidence_of_the_wrong_type_is_ignored_not_fatal(monkeypatch, evidence):
    """evidence 是数字、布尔、字符串：原来一迭代就 500（Codex 复现）。现在当没给证据。"""
    _with_model(monkeypatch, _criteria(problem={"evidence": evidence}))
    r = submission.review(_zip(), PROJECT)
    assert r["voice"] == "llm"
    problem = next(c for c in r["criteria"] if c["key"] == "problem")
    assert problem["evidence"] == [] and problem["status"] != "pass"


def test_invented_files_and_quotes_cannot_carry_a_pass(monkeypatch):
    """不存在的文件配空引文、编出来的引文：都不算证据；说做到了却一条证据都站不住，降成部分做到。
    原来不存在的文件也算证据，编的引文滤掉了 pass 还留着，一份承认没做完的成果被记成 5/5（Codex 复现）。"""
    _with_model(monkeypatch, _criteria(
        results={"evidence": [{"file": "results/ghost.png", "quote": ""}]},
        check={"evidence": [{"file": "README.md", "quote": "我们在五个数据集上都超过了基线"}]},
    ))
    r = submission.review(_zip(), PROJECT)
    by = {c["key"]: c for c in r["criteria"]}
    assert by["results"]["evidence"] == [] and by["results"]["status"] != "pass"
    assert by["check"]["evidence"] == [] and by["check"]["status"] != "pass"
    assert r["passed"] < len(submission.CRITERIA)


def test_real_evidence_still_counts(monkeypatch):
    """真在压缩包里的文件、真在文件里的引文照样算；只给存在的文件不写引文也算（比如结果图）。"""
    _with_model(monkeypatch, _criteria(results={"evidence": [{"file": "results/table.csv", "quote": ""}]}))
    r = submission.review(_zip(), PROJECT)
    by = {c["key"]: c for c in r["criteria"]}
    assert by["results"]["evidence"] == [{"file": "results/table.csv", "quote": ""}]
    assert by["problem"]["evidence"] == [{"file": "README.md", "quote": "我读了数据"}]


def test_shutdown_waits_for_a_running_review(monkeypatch):
    """关服务时还在评的那份：等它评完、记进项目再退。原来只等对话那个池子，评阅写一半就没了（Codex 复现）。"""
    pool = workpool.BoundedPool(1, 2, "t")
    monkeypatch.setattr(main, "_REVIEW", pool)
    started, wrote = threading.Event(), []

    def review_job():
        started.set()
        time.sleep(0.3)
        wrote.append("记进项目")

    with TestClient(main.app):
        pool.submit(review_job)
        assert started.wait(5)
    assert wrote == ["记进项目"]  # 退出 lifespan 时已经写完


def test_the_daily_feed_says_which_day_its_count_is_for():
    """每日分拣的计数带上日期：前端同一天里只往大里改，换了一天照新的来（原来一律取大，跨天还显示昨天的数，Codex 复现）。"""
    import reading
    kit = next(iter(reading.kits()))
    uid = store.create_user("分拣")["uid"]
    d = reading.daily(uid, kit, ([], ""))
    assert d["day"] == store.now_iso()[:10] and d["done_today"] == 0
