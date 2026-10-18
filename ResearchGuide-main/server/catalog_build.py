# -*- coding: utf-8 -*-
"""把当前学期全院系公开课翻进 knowledge/catalog/。中断可续跑。"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills" / "pku-course" / "scripts"
OUT = ROOT / "knowledge" / "catalog"
STAGING = OUT / "_pages.jsonl"
TERM = "2026-2027-1"
PAGE = 10
SLEEP = 0.28
OPENALEX = "https://api.openalex.org/authors"
PKU_ROR = "02v51n119"

sys.path.insert(0, str(SKILL_SCRIPTS))
import dean  # noqa: E402


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def split_teachers(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    chunks = [p.strip() for p in re.split(r"[/／、;；,，|]+", text) if p.strip()]
    names: list[str] = []
    for chunk in chunks:
        tokens = chunk.split()
        if tokens and all(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", t) for t in tokens):
            names.extend(tokens)
        else:
            names.append(chunk)
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def row_to_course(term: str, row: dict) -> dict:
    teachers = dean.text(row.get("teacher"))
    return {
        "ref": dean.ref(term, row),
        "term": term,
        "course_id": str(row.get("kch") or ""),
        "section": str(row.get("jxbh") or ""),
        "name": dean.text(row.get("kcmc")),
        "teachers": teachers or None,
        "teacher_names": split_teachers(teachers),
        "department": dean.text(row.get("kkxsmc")) or None,
        "credits": dean.text(row.get("xf")) or None,
        "schedule": dean.text(row.get("sksj"), paragraphs=True) or None,
        "weeks": dean.text(row.get("qzz")) or None,
        "category": dean.text(row.get("kctxm")) or None,
    }


def already_offset() -> int:
    if not STAGING.exists():
        return 0
    n = 0
    with STAGING.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def _connect():
    last_err = None
    for attempt in range(12):
        try:
            client = dean.Client()
            filters = client.filters(TERM, "0", "", "")
            return client, filters
        except (dean.Error, OSError) as exc:
            last_err = exc
            wait = 4 * (attempt + 1)
            print(f"connect attempt={attempt + 1}: {exc}; sleep {wait}s", flush=True)
            time.sleep(wait)
    raise SystemExit(f"cannot open dean catalog: {last_err}")


def crawl_courses() -> list[dict]:
    client, filters = _connect()
    total, first = client.page(TERM, filters, 0)
    print(f"term={TERM} total={total}", flush=True)
    start = already_offset()
    if start and start % PAGE:
        raise SystemExit(f"staging 行数 {start} 不是 {PAGE} 的倍数，先检查 {STAGING}")
    if start == 0:
        STAGING.write_text("", encoding="utf-8")
        _append_rows(first)
        start = len(first)
        print(f"wrote 0-{start}/{total}", flush=True)
    offset = start
    while offset < total:
        time.sleep(SLEEP)
        got = rows = None
        last_err = None
        for attempt in range(8):
            try:
                got, rows = client.page(TERM, filters, offset)
                last_err = None
                break
            except (dean.Error, OSError) as exc:
                last_err = exc
                wait = 2.5 * (attempt + 1)
                print(f"retry offset={offset} attempt={attempt + 1}: {exc}; sleep {wait}s", flush=True)
                time.sleep(wait)
                client = dean.Client()
                filters = client.filters(TERM, "0", "", "")
        if last_err:
            raise SystemExit(f"stopped at offset={offset}: {last_err}")
        if got != total:
            raise SystemExit(f"总数变了：{total} → {got}，停在 {offset}")
        if not rows:
            break
        _append_rows(rows)
        offset += len(rows)
        print(f"wrote {offset}/{total}", flush=True)
    courses = []
    with STAGING.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                courses.append(json.loads(line))
    if len(courses) != total:
        print(f"warning: saved {len(courses)} != announced {total}", flush=True)
    return courses


def _append_rows(rows: list[dict]) -> None:
    with STAGING.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row_to_course(TERM, row), ensure_ascii=False) + "\n")


def build_teachers(courses: list[dict]) -> list[dict]:
    by_name: dict[str, dict] = {}
    for c in courses:
        for name in c.get("teacher_names") or []:
            rec = by_name.setdefault(name, {
                "name": name,
                "departments": [],
                "course_refs": [],
                "courses": [],
                "bio": None,
                "bio_source": None,
            })
            if c["department"] and c["department"] not in rec["departments"]:
                rec["departments"].append(c["department"])
            rec["course_refs"].append(c["ref"])
            rec["courses"].append({
                "ref": c["ref"],
                "name": c["name"],
                "department": c["department"],
                "credits": c["credits"],
                "section": c["section"],
            })
    teachers = list(by_name.values())
    teachers.sort(key=lambda t: (-len(t["courses"]), t["name"]))
    print(f"teachers={len(teachers)}", flush=True)
    return teachers


def _openalex(name: str) -> dict | None:
    q = urllib.parse.urlencode({
        "search": name,
        "filter": f"last_known_institutions.ror:{PKU_ROR}",
        "per_page": "5",
        "mailto": "autoresearch@local",
    })
    req = urllib.request.Request(
        f"{OPENALEX}?{q}",
        headers={"User-Agent": "AutoResearch catalog (mailto:autoresearch@local)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    results = data.get("results") or []
    hit = None
    for item in results:
        display = str(item.get("display_name") or "")
        aliases = [str(a) for a in (item.get("display_name_alternatives") or [])]
        if name == display or name in aliases or name in display:
            hit = item
            break
    if hit is None and len(results) == 1 and name in str(results[0].get("display_name") or ""):
        hit = results[0]
    if not hit:
        return None
    inst = ""
    for i in hit.get("last_known_institutions") or []:
        if (i.get("ror") or "").endswith(PKU_ROR) or "Peking" in str(i.get("display_name") or ""):
            inst = i.get("display_name") or "Peking University"
            break
    if not inst:
        return None
    topics = [t.get("display_name") for t in (hit.get("topics") or [])[:3] if t.get("display_name")]
    stats = hit.get("summary_stats") or {}
    works = stats.get("works_count")
    cited = stats.get("cited_by_count")
    bits = [f"OpenAlex 上任职于{inst}"]
    if works is not None:
        bits.append(f"收录著作 {works} 篇")
    if cited is not None:
        bits.append(f"被引 {cited}")
    if topics:
        bits.append("近期主题：" + "、".join(topics))
    return {
        "text": "；".join(bits) + "。",
        "source": "openalex",
        "openalex_id": hit.get("id"),
        "works_count": works,
        "cited_by_count": cited,
        "topics": topics,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fill_bios(teachers: list[dict]) -> None:
    done = 0
    hits = 0
    for rec in teachers:
        time.sleep(0.12)
        bio = _openalex(rec["name"])
        done += 1
        if bio:
            rec["bio"] = bio["text"]
            rec["bio_source"] = bio
            hits += 1
        if done % 50 == 0:
            print(f"bios {done}/{len(teachers)} hits={hits}", flush=True)
            _write_json(OUT / "teachers.json", teachers)
    print(f"bios done hits={hits}/{len(teachers)}", flush=True)


def load_staging() -> list[dict]:
    if not STAGING.exists():
        raise SystemExit(f"missing {STAGING}")
    courses = []
    with STAGING.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                courses.append(json.loads(line))
    return courses


def write_outputs(courses: list[dict], teachers: list[dict], complete: bool) -> None:
    _write_json(OUT / "courses.json", courses)
    _write_json(OUT / "teachers.json", teachers)
    _write_json(OUT / "meta.json", {
        "term": TERM,
        "source": "https://dean.pku.edu.cn/service/web/courseSearch.php",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "course_count": len(courses),
        "teacher_count": len(teachers),
        "teacher_bio_count": sum(1 for t in teachers if t.get("bio")),
        "complete": complete,
        "note": "当前学期全院系公开课快照。老师简介只写对上北京大学的 OpenAlex 记录，对不上就空着。"
                + ("" if complete else " 教务中途连不上，这份还没收齐，_pages.jsonl 留下以便续爬。"),
    })
    print("ok", OUT, "courses", len(courses), "teachers", len(teachers), "complete", complete, flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    finalize_only = "--finalize-only" in argv
    skip_bios = "--skip-bios" in argv
    OUT.mkdir(parents=True, exist_ok=True)
    if finalize_only:
        courses = load_staging()
        complete = False
    else:
        courses = crawl_courses()
        complete = True
    teachers = build_teachers(courses)
    if not skip_bios:
        fill_bios(teachers)
    write_outputs(courses, teachers, complete)
    if complete and STAGING.exists():
        STAGING.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
