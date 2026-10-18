# -*- coding: utf-8 -*-
"""第四轮复查：成果压缩包的三处规则漏洞（离线，不调模型）。"""
from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

llm.enabled = lambda: False  # 测规则版：模型永远不可用

import submission  # noqa: E402

PROJECT = {"name": "用公开评论数据做情感分类基线", "practices": "数据清洗、分类评价",
           "todo": "做两个基线并报告每类精确率召回率", "url": "https://example.org/p/1"}

BASE = ("## 题目\n我们解决公开数据中的分类问题，比较结果是否稳定。" + "说明文字。" * 20
        + "\n## 我做了什么\n我读了数据。\n## 怎么复现\n运行 src/main.py\n## 还没做完的\n没有优化。\n")


def make_zip(files: dict[str, str | bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_unreferenced_result_file_does_not_pass_results():
    r = submission.review(make_zip({"README.md": BASE, "src/main.py": "print(1)", "unused.png": b"x"}), PROJECT)
    status = {c["key"]: c["status"] for c in r["criteria"]}
    assert r["checks"]["referenced"] == ["src/main.py"]
    assert status["results"] == "partial"


def test_reference_with_wrong_directory_is_missing():
    readme = BASE + "\n结果见 results/table.csv\n"
    r = submission.review(make_zip({"README.md": readme, "src/main.py": "print(1)", "data/table.csv": "x,y\n1,2"}), PROJECT)
    assert "results/table.csv" in r["checks"]["missing_refs"]
    assert "data/table.csv" not in r["checks"]["referenced"]


def test_corrupted_member_raises_submission_error():
    raw = make_zip({"README.md": "## 题目\nhello world corrupted member\n"}, zipfile.ZIP_STORED)
    assert raw.count(b"hello world") == 1
    bad = raw.replace(b"hello world", b"HELLO world", 1)
    msg = "压缩包里的「README.md」读不出来（文件损坏或用了不支持的压缩方式），请重新打包再交。"
    with pytest.raises(submission.SubmissionError, match=re.escape(msg)):
        submission.review(bad, PROJECT)


def test_references_resolve_relative_to_a_nested_readme():
    """压缩包里 README 在 work/ 下（顶层还有 LICENSE，所以不会被当成外层目录剥掉）：它写的 results/table.csv 指 work/results/table.csv。"""
    readme = BASE + "\n## 结果在哪\nresults/table.csv 是两个基线的结果表。\n"
    r = submission.review(make_zip({"LICENSE": "MIT", "work/README.md": readme, "work/src/main.py": "print(1)",
                                    "work/results/table.csv": "a,b\n1,2\n"}), PROJECT)
    assert "work/results/table.csv" in r["checks"]["referenced"]
    assert "results/table.csv" not in r["checks"]["missing_refs"]


def test_parent_directory_links_resolve_from_the_readme():
    """work/docs/README.md 写的 ../results/table.csv 指 work/results/table.csv。原来先剥掉 ../ 再拼，判成没找到（Codex 复现）。"""
    readme = BASE + "\n## 结果在哪\n../results/table.csv 是结果表，./notes.md 是笔记。\n"
    r = submission.review(make_zip({"LICENSE": "MIT", "work/docs/README.md": readme, "work/docs/notes.md": "n",
                                    "work/src/main.py": "print(1)", "work/results/table.csv": "a,b\n1,2\n"}), PROJECT)
    assert "work/results/table.csv" in r["checks"]["referenced"] and "work/docs/notes.md" in r["checks"]["referenced"]
    assert not any("table.csv" in m for m in r["checks"]["missing_refs"])
