# -*- coding: utf-8 -*-
"""本机预热：把工具包论文和已读论文取进 paper_text。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arxiv  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402


def import_old_json() -> int:
    n = 0
    root = Path(arxiv.CACHE_DIR)
    if not root.exists():
        return 0
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        aid = data.get("id") or path.stem
        if aid:
            store.put_paper_text(aid, data)
            n += 1
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--kit", default="")
    p.add_argument("--budget", type=int, default=600)
    args = p.parse_args()
    store.init_db()
    imported = import_old_json()
    t0 = time.time()
    done = 0
    for aid, pinned, hint in reading.warm_targets():
        if args.kit and not any(p.get("arxiv_id") == aid for p in (reading.kits().get(args.kit) or {}).get("papers") or []):
            continue
        if time.time() - t0 > args.budget:
            break
        try:
            reading.warm_fetch(aid, hint=hint or None)
            done += 1
        except Exception as exc:
            print(f"fail {aid}: {exc}")
    print(f"imported {imported}, warmed {done}")


if __name__ == "__main__":
    main()
