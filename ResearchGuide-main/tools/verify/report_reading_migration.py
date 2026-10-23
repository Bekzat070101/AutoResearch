"""Read-only report; never opens the application or migrates a database.

python tools/verify/report_reading_migration.py --db /path/to/copy.db
"""
import argparse
import json
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", type=Path, required=True)
args = parser.parse_args()
db = args.db.resolve()
with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as c:
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    report = {"database": str(db), "applied": False, "unassigned": {}}
    if "feature_migrations" in tables:
        row = c.execute("SELECT data FROM feature_migrations WHERE name='portrait_reading_v1'").fetchone()
        if row:
            report.update(applied=True, migration=json.loads(row[0]))
    if "reading_unassigned" in tables:
        report["unassigned"] = dict(c.execute("SELECT table_name,COUNT(*) FROM reading_unassigned GROUP BY table_name"))
    report["active_read_documents"] = c.execute("SELECT COUNT(*) FROM read_documents").fetchone()[0] if "read_documents" in tables else 0
print(json.dumps(report, ensure_ascii=False, indent=2))
