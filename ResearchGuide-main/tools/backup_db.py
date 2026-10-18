# -*- coding: utf-8 -*-
"""在线备份 SQLite 库：服务不用停，拷一份一致的快照，按时间命名，只留最近几天。

用法（服务器上每天跑一次，比如 cron `0 4 * * *`）：
  python tools/backup_db.py                          # 库：$QIYAN_DB（环境变量或 .env），没设就是 server/data/demo.db；备份放在库旁边的 backups/
  python tools/backup_db.py --out /data/backups --keep 7
  docker compose exec qiyan python tools/backup_db.py --out /data/backups

恢复：停服务，把要用的那份备份拷成 $QIYAN_DB（同目录下的 -wal / -shm 文件一并删掉），再起服务。
隐私说明里承诺「删号后备份最多再留 7 天」：--keep 调大了要同步改 web/js/app.js 的 PRIVACY_HTML。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import envfile  # noqa: E402,F401  和服务读同一份 .env：.env 里写的 QIYAN_DB 备份脚本也要认

DEFAULT_DB = Path(__file__).resolve().parent.parent / "server" / "data" / "demo.db"
PREFIX = "qiyan-"


def backup(db: Path, out: Path, keep_days: float) -> Path:
    if not db.exists():
        raise SystemExit(f"找不到库文件：{db}")
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.db"
    part = dest.with_suffix(".db.part")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(part)
        try:
            src.backup(dst)  # 一边有人写也能拷出一致的快照
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        part.unlink(missing_ok=True)
        raise SystemExit(f"备份校验没过：{ok}")
    part.replace(dest)
    cutoff = time.time() - keep_days * 86400
    for old in out.glob(f"{PREFIX}*.db"):
        if old != dest and old.stat().st_mtime < cutoff:
            old.unlink()
    return dest


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("QIYAN_DB") or DEFAULT_DB))
    ap.add_argument("--out", type=Path, default=None, help="备份目录，默认是库旁边的 backups/")
    ap.add_argument("--keep", type=float, default=7, help="保留几天，默认 7")
    args = ap.parse_args(argv)
    dest = backup(args.db, args.out or args.db.parent / "backups", args.keep)
    print(dest)


if __name__ == "__main__":
    main(sys.argv[1:])
