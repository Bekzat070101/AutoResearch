# -*- coding: utf-8 -*-
"""把在用话题取进 paper_pool，并预译每话题前 12 篇。部署后每天美东午夜后跑一次。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import interests  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402


def main() -> None:
    store.init_db()
    interests.reload()
    for tid, spec in interests.table()["topics"].items():
        if spec.get("source") == "osf" or spec.get("categories"):
            reading.topic_source(tid, cached_only=False)


if __name__ == "__main__":
    main()
