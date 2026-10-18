# -*- coding: utf-8 -*-
"""论文磁盘缓存有上限：超过文件数或总字节就按修改时间删最旧的，刚写的那篇留着。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402


def _paper(i: int) -> tuple[str, dict]:
    aid = f"2401.{i:05d}"
    return aid, {"id": aid, "title": f"Paper {i}", "source": "html", "text": "x" * 1000, "url": f"https://arxiv.org/abs/{aid}"}


def _save_all(tmp_path: Path, n: int) -> list[str]:
    base = time.time() - 1000
    ids = []
    for i in range(n):
        aid, result = _paper(i)
        arxiv._save(aid, result)
        os.utime(tmp_path / f"{aid}.json", (base + i, base + i))
        ids.append(aid)
    return ids


def _reset(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(arxiv, "_QUOTA", {"dir": None, "sizes": {}, "bytes": 0})


def test_file_count_limit_keeps_most_recent(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setattr(arxiv, "CACHE_MAX_FILES", 5)
    monkeypatch.setattr(arxiv, "CACHE_MAX_BYTES", 10**9)
    ids = _save_all(tmp_path, 12)
    left = sorted(p.stem for p in tmp_path.glob("*.json"))
    assert left == ids[-5:]
    assert (tmp_path / f"{ids[-1]}.json").exists()


def test_byte_limit_keeps_most_recent(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    size = len(json.dumps(_paper(0)[1], ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(arxiv, "CACHE_MAX_FILES", 1000)
    monkeypatch.setattr(arxiv, "CACHE_MAX_BYTES", 3 * size + size // 2)
    ids = _save_all(tmp_path, 8)
    left = sorted(p.stem for p in tmp_path.glob("*.json"))
    assert left == ids[-3:]
    assert sum(p.stat().st_size for p in tmp_path.glob("*.json")) <= 3 * size + size // 2
