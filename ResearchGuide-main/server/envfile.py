# -*- coding: utf-8 -*-
"""读项目根目录的 .env：已经在环境变量里的不覆盖。

单独一个模块、最先被 import：store 在 import 时就要算库路径，必须在那之前读到 .env 里的 QIYAN_DB。
原来 .env 是 llm 读的，而 main 先 import 了 auth → store，库路径已经按默认值定死，.env 里写的 QIYAN_DB 被悄悄忽略。
QIYAN_ENV_FILE 可以指到别的文件（测试用，免得读到本机真的 .env）。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def path() -> Path:
    return Path(os.environ.get("QIYAN_ENV_FILE") or ROOT / ".env")


def load(file: Path | None = None) -> None:
    file = file or path()
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


load()
