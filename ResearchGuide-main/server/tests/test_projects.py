# -*- coding: utf-8 -*-
"""边学边练的离线测试：不联网、不调模型。

运行：uv run --no-project --with pytest --with fastapi --with pydantic --with httpx pytest server/tests
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False  # 测规则版：模型永远不可用

import projects  # noqa: E402
import store  # noqa: E402
import submission  # noqa: E402

PROJECT = {"name": "用公开评论数据做情感分类基线", "practices": "数据清洗、分类评价",
           "todo": "做两个基线并报告每类精确率召回率", "url": "https://example.org/p/1"}

GOOD_README = """## 题目
用公开评论数据做情感分类基线（https://example.org/p/1）。我想回答：规则和逻辑回归在差评上谁更容易漏判？

## 我做了什么
去重后按 8:2 划分，做关键词规则与逻辑回归两个基线。

## 结果在哪
- results/metrics.csv：每类精确率、召回率

## 怎么复现
运行 python src/baseline.py

## 还没做完的
只试了逻辑回归，下一步加一个小模型对比。
"""


def make_zip(files: dict[str, str | bytes], root: str = "work/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(root + name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()


def test_good_submission_passes_rule_checks():
    blob = make_zip({"README.md": GOOD_README, "results/metrics.csv": "a,b\n1,2\n", "src/baseline.py": "print(1)\n"})
    r = submission.review(blob, PROJECT)
    status = {c["key"]: c["status"] for c in r["criteria"]}
    assert r["voice"] == "rules"
    assert status["problem"] == status["results"] == status["check"] == status["limits"] == "pass"
    assert status["match"] == "partial"  # 没有模型时不假装对照过项目要求
    assert r["inventory"][0]["path"] == "README.md"  # 顶层文件夹被去掉


def test_missing_readme_fails_and_points_to_fix():
    r = submission.review(make_zip({"作业.py": "print(1)\n"}), PROJECT)
    first = r["criteria"][0]
    assert first["status"] == "fail" and "README.md" in first["fix"]
    assert r["next_step"].startswith("先改「说清了要解决什么问题」")


def test_referenced_but_missing_file_is_reported():
    readme = GOOD_README.replace("results/metrics.csv", "results/missing.png")
    r = submission.review(make_zip({"README.md": readme, "results/metrics.csv": "a\n1\n"}), PROJECT)
    assert "results/missing.png" in r["checks"]["missing_refs"]


def test_gbk_filenames_are_restored():
    real = "结果/混淆矩阵.csv".encode("gbk")
    placeholder = "X" * len(real)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.md", "## 题目\n测试\n")
        z.writestr(placeholder, "a,b\n1,2\n")
    raw = buf.getvalue().replace(placeholder.encode(), real)
    paths = [i["path"] for i in submission.read_zip(raw)["inventory"]]
    assert "结果/混淆矩阵.csv" in paths


@pytest.mark.parametrize("blob, msg", [
    (b"not a zip", "不是一个能打开的"),
    (make_zip({"a.txt": b"0" * (8 * 1024 * 1024)}), "压缩比异常"),
])
def test_bad_archives_are_rejected(blob, msg):
    with pytest.raises(submission.SubmissionError, match=msg):
        submission.read_zip(blob)


def test_path_traversal_entries_are_ignored():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
        z.writestr("README.md", "## 题目\nx\n")
    paths = [i["path"] for i in submission.read_zip(buf.getvalue())["inventory"]]
    assert paths == ["README.md"]


def test_llm_cannot_exceed_rule_caps(monkeypatch):
    blob = make_zip({"README.md": "## 题目\n很短\n"})
    bundle = submission.read_zip(blob)
    checks = submission.rule_checks(bundle, PROJECT)
    fake = {"criteria": [{"key": c["key"], "status": "pass", "evidence": [{"file": "README.md", "quote": "编造的话"}],
                          "comment": "好", "fix": ""} for c in submission.CRITERIA], "summary": "都好"}
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: fake)
    judged = submission._llm_review(bundle, checks, PROJECT)
    by = {c["key"]: c for c in judged}
    assert by["results"]["status"] == "fail"          # 没有结果文件，模型说 pass 也不算
    assert by["results"]["fix"]                       # 降级后必须给出改法
    assert all(not e["quote"] or e["quote"] in bundle["texts"]["README.md"] for c in judged for e in c["evidence"])


def test_pick_only_accepts_ids_from_last_search():
    uid = store.create_user("t")["uid"]
    with pytest.raises(KeyError):
        projects.pick(uid, "deadbeefdeadbeef")
    cand = {"id": "abc123", "name": "某项目", "url": "https://example.org", "source_name": "来源"}
    projects._LAST.set(uid, {"abc123": cand})
    saved = projects.pick(uid, "abc123")
    assert saved["name"] == "某项目" and projects.pick(uid, "abc123")["id"] == saved["id"]


def test_registry_samples_have_provenance():
    reg = json.loads(projects.REGISTRY.read_text(encoding="utf-8"))
    for s in reg["sources"]:
        for x in s.get("samples") or []:
            assert x["url"].startswith("http") and x["retrieved_at"] and x["title"], (s["id"], x)


def test_closed_deadline_detection():
    assert projects._closed("2020-01-01") is True
    assert projects._closed("2999-01-01") is False
    assert projects._closed("") is False


# ---------- 任务 4 路径 ----------

def test_paths_have_six_steps_with_pass_criteria():
    data = projects.paths()
    assert {"math", "ai", "psy", "econ"} <= set(data)
    for code, p in data.items():
        steps = p["steps"]
        assert [s["step"] for s in steps] == [1, 2, 3, 4, 5, 6], code
        assert all(s["name"] and s["done_when"] for s in steps), code
        stages = [s["project_stage"] for s in steps]
        assert stages == sorted(stages) and stages[0] == 0 and stages[-1] == 3, code


def test_context_offers_path_step_for_directions_with_a_path():
    from schemas import UserFact
    uid = store.create_user("t")["uid"]
    store.add_fact(UserFact(user_id=uid, category="interest", key="direction:ai", value="ai", status="confirmed"))
    ctx = projects.context(uid)
    assert ctx["direction"] == "ai" and ctx["path_step"] == 1 and "ai" in ctx["paths"]


def test_path_step_sets_stage_and_is_passed_to_the_model(monkeypatch):
    uid = store.create_user("t")["uid"]
    cand = {"id": "c1", "name": "某学习赛：文本分类", "url": "https://example.org/c1", "source_id": "s", "source_name": "来源",
            "source_url": "", "kind": "competition", "excerpt": "分类 数据 评价指标", "practices": "", "todo": "", "difficulty": "",
            "deadline": "", "stage_fit": [1, 2], "directions": ["ai"], "evidence_quote": "", "retrieved_at": "", "snapshot": False, "closed": False}
    monkeypatch.setattr(projects, "_collect", lambda src, d, st, kw: ([dict(cand)], {"id": src["id"], "name": src["name"], "ok": True, "count": 1}))
    seen = {}

    def fake_pick(cands, direction, stage, keywords, node, step=None, chain=None, uid=None):
        seen.update(stage=stage, node=node, step=step)
        return None

    monkeypatch.setattr(projects, "_pick_llm", fake_pick)
    r = projects.search(uid, "ai", 0, "", "", step_no=5)
    assert seen["stage"] == 3 and seen["step"]["step"] == 5 and seen["node"] == seen["step"]["name"]
    assert r["query"]["path_step"] == 5 and r["items"][0]["path_step"] == 5
    r2 = projects.search(uid, "stat", 1, "", "", step_no=5)   # 没有路径的方向忽略 path_step
    assert r2["query"]["path_step"] is None and r2["query"]["stage"] == 1


def test_paths_json_is_in_sync_with_docs():
    """docs/paths/*.md 改了却没重新生成 knowledge/paths.json 时失败。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_paths", projects.ROOT / "knowledge" / "build_paths.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    on_disk = json.loads(projects.PATHS.read_text(encoding="utf-8"))
    assert on_disk == mod.build(), "路径文档有改动：请运行 uv run --no-project python knowledge/build_paths.py"


def test_slow_source_falls_back_to_snapshot_within_budget(monkeypatch):
    """一个来源卡住时，检索按时返回，并如实标出这个来源用了快照。"""
    import threading
    uid = store.create_user("t")["uid"]
    release = threading.Event()
    real = projects._collect

    def slow_collect(src, d, st, kw):
        if src.get("adapter"):
            release.wait(5)
            return [], {"id": src["id"], "name": src["name"], "ok": False, "count": 0}
        return real(src, d, st, kw)

    monkeypatch.setattr(projects, "_collect", slow_collect)
    monkeypatch.setattr(projects, "SEARCH_BUDGET", 0.3)
    t0 = projects.time.time()
    r = projects.search(uid, "econ", 1)
    release.set()
    assert projects.time.time() - t0 < 3
    slow = [s for s in r["sources"] if "没返回" in (s.get("error") or "")]
    assert slow and all(s.get("snapshot") for s in slow)


def test_project_is_done_only_when_all_criteria_pass():
    uid = store.create_user("t")["uid"]
    projects._LAST.set(uid, {"c9": {"id": "c9", "name": "某项目", "url": "https://example.org", "source_name": "来源"}})
    p = projects.pick(uid, "c9")
    review = {"passed": 4, "total": 5, "criteria": [], "summary": "", "next_step": "先改一处", "inventory": []}
    projects.record_review(uid, p, review)
    assert store.get_project(uid, p["id"])["status"] == "reviewed"
    projects.record_review(uid, store.get_project(uid, p["id"]), dict(review, passed=5))
    again = store.get_project(uid, p["id"])
    assert again["status"] == "done" and [r["passed"] for r in again["reviews"]] == [5, 4]
