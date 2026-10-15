# -*- coding: utf-8 -*-
"""任务2 对话内核评测跑分器（需要真实模型）。

用法：
    # 1. 另开一个终端起服务（默认 8100）
    uv run --no-project --with fastapi --with uvicorn --with pydantic python server/main.py
    # 2. 跑评测
    python eval/run_eval.py
    python eval/run_eval.py --base http://127.0.0.1:8100 --only action_has_landing

断言都是确定性的：是否重问、是否有落点、是否编造、记忆写对没有。
不判主观文采——那需要人来判。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "dialogue_cases.json"
DB = ROOT / "server" / "data" / "demo.db"

# 复用记忆层的判定（支撑度、分层），别在评测里重写一份规则
sys.path.insert(0, str(ROOT / "server"))
import memory  # noqa: E402


# ---------- HTTP ----------


def post(base: str, path: str, body: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(base: str, path: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- 断言 ----------


def _keys(d: dict) -> set[str]:
    return set(d.get("checks") or {})


def check_one(turn: dict, all_turns: list[dict], checks: dict, uid: str) -> list[str]:
    """返回失败说明列表；空列表 = 通过。"""
    bad: list[str] = []
    reply = turn.get("reply") or ""

    if checks.get("reply_nonempty") and not reply.strip():
        bad.append("回复为空")

    if "reply_min_len" in checks and len(reply) < checks["reply_min_len"]:
        bad.append(f"回复只有 {len(reply)} 字，少于 {checks['reply_min_len']}")

    if "reply_max_len" in checks and len(reply) > checks["reply_max_len"]:
        bad.append(f"回复 {len(reply)} 字，超过 {checks['reply_max_len']}")

    for word in checks.get("reply_not_ask") or []:
        if word in reply:
            bad.append(f"重问了已经知道的事：「{word}」")

    for word in checks.get("reply_contains_any") or []:
        if word in reply:
            break
    else:
        if checks.get("reply_contains_any"):
            bad.append(f"回复里没有出现任何一项：{checks['reply_contains_any']}")

    if checks.get("next_action_absent") and turn.get("next_action"):
        bad.append(f"不该派任务，却给了：{turn['next_action'].get('title')}")

    if checks.get("next_action_present") and not turn.get("next_action"):
        bad.append("期望给出下一步，实际没有")

    if checks.get("pending_action_absent") and turn.get("pending_action"):
        bad.append(f"该停下却还挂着行动：{turn['pending_action'].get('title')}")

    if checks.get("action_basis_required"):
        # 给了任务就必须带依据；没有依据说明它多半是套话（代码侧本来就会拦掉）
        a = turn.get("next_action") or turn.get("pending_action")
        if a:
            why = [x for x in (a.get("based_on") or []) if x and x.get("value")]
            if not why:
                bad.append(f"任务「{a.get('title')}」没有说明依据了用户的哪句话")

    if checks.get("degraded_false") and turn.get("degraded"):
        bad.append("走了规则降级（模型没接上或调用失败）")

    if checks.get("constraint_has_valid_until"):
        cons = [f for f in (turn.get("facts_added") or []) if str(f.get("key", "")).startswith("constraint:")]
        if cons and not all(f.get("valid_until") for f in cons):
            bad.append("临时约束没有有效期")
        if not cons:
            bad.append("没认出这是临时约束")

    if checks.get("evidence_verbatim"):
        # 代码已经强制校验过引文；这里再核一遍写进库的事实都有 dialect 依据
        for f in turn.get("facts_added") or []:
            if not (f.get("evidence") or []):
                bad.append(f"事实「{f.get('key')}」没有依据")

    if checks.get("no_invented_category"):
        allowed = {"background", "interest", "capability", "preference", "experience"}
        for f in turn.get("facts_added") or []:
            if f.get("category") not in allowed:
                bad.append(f"类别不在白名单：{f.get('category')}")

    if checks.get("value_supported_by_quote"):
        # 值里不能出现引文里没有的内容——真实事故是靠这条不该发生的
        for f in turn.get("facts_added") or []:
            quote = ""
            for e in f.get("evidence") or []:
                quote += str(e.get("quote") or "")
            if not quote:
                bad.append(f"事实「{f.get('key')}」没有引文")
                continue
            value = str(f.get("value") or "")
            if len(value) >= 8 and memory.support_ratio(value, quote) < 0.5:
                bad.append(f"事实「{f.get('key')}」的值超出了引文能支撑的范围：{value[:40]}")

    if checks.get("no_assertion_words"):
        # 「基础扎实」「能力偏弱」这类是模型自己的评价，不该写进用户画像
        banned = ("基础扎实", "能力偏弱", "基础薄弱", "能力较强", "水平较高", "天赋")
        for f in turn.get("facts_added") or []:
            for w in banned:
                if w in str(f.get("value") or ""):
                    bad.append(f"事实「{f.get('key')}」里写了模型自己的评价：「{w}」")

    if checks.get("facts_have_affects"):
        for f in turn.get("facts_added") or []:
            if not f.get("affects"):
                bad.append(f"事实「{f.get('key')}」没说清改变哪个决策")

    if checks.get("practice_layer_used"):
        got = [f for f in (turn.get("facts_added") or [])
               if memory.layer_of(str(f.get("key", ""))) == "practice"]
        if not got:
            bad.append("提到手头在做的事，却没写进「当前实践」层")

    if checks.get("action_closure"):
        # 只有在「一句话明确让『你』去做某件具体的事、且这句话不是提问」时才算布置了动作。
        # 早先的写法跨标点匹配，会把「想试 AI 的人第一步是写代码」这种解释句误判成派任务。
        for sent in re.split(r"[。；\n]", reply):
            s = sent.strip()
            if not s or s.endswith("？") or s.endswith("?"):
                continue
            if re.search(r"你[^，。]{0,12}(可以|不妨|试着|先|需要|建议)", s) and \
               re.search(r"(做|写|读|跑|试|完成|改|看|练|复现|装|提交|算|查)", s):
                act = (turn.get("next_action") or {}).get("action")
                if act and act != "explore_direction":
                    bad.append(f"本轮不派任务，却给了 {act}：「{s[:30]}…」")
                break

    if checks.get("supersede_or_retract"):
        changed = turn.get("facts_changed") or []
        ops = {r.get("operation") for r in sqlite_revisions(uid)}
        if not changed and not ({"supersede", "retract", "replace"} & ops):
            bad.append("用户改口后没有产生取代/撤回记录")

    if checks.get("max_facts_with_same_key"):
        rows = sqlite_facts(uid)
        seen: dict[str, int] = {}
        for r in rows:
            if r["status"] in ("confirmed", "active"):
                seen[r["key"]] = seen.get(r["key"], 0) + 1
        over = {k: v for k, v in seen.items() if v > checks["max_facts_with_same_key"]}
        if over:
            bad.append(f"重复记忆：{over}")

    if checks.get("tool_honesty"):
        results = turn.get("tool_results") or []
        if results and not results[0].get("ok"):
            # 工具失败时回复必须承认失败（说法可以很多样），且不能出现编造的课名/书名
            admitted = re.search(
                r"(没查到|查不到|没查成|查不成|没查成功|没成功|查失败|失败|报错|出错|"
                r"没有找到|没找到|没能|不可用|暂时|无法查|数据源|接口)", reply)
            if not admitted:
                bad.append("工具失败却没有如实说明")
            if re.search(r"《[^》]{2,40}》", reply):
                bad.append("工具失败却提到了具体课程/书名，疑似编造")

    return bad


def sqlite_facts(uid: str) -> list[dict]:
    if not DB.exists():
        return []
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT key,status,value FROM facts WHERE user_id=? ORDER BY rowid", (uid,))]
    db.close()
    return rows


def sqlite_revisions(uid: str) -> list[dict]:
    if not DB.exists():
        return []
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT operation FROM fact_revisions WHERE user_id=? ORDER BY rowid", (uid,))]
    db.close()
    return rows


# ---------- 跑分 ----------


def run_case(base: str, case: dict, n: int) -> dict:
    uid = post(base, "/api/auth/login", {"nickname": f"eval-{case['id']}-{n}"})["uid"]
    turns: list[dict] = []
    conv = None
    for text in case["turns"]:
        body = {"uid": uid, "message": text}
        if conv:
            body["conversation_id"] = conv
        t = post(base, "/api/dialogue/turn", body)
        conv = t.get("conversation_id")
        turns.append(t)

    targets = [turns[-1]] if case.get("check_last_only") else turns
    failures: list[str] = []
    for idx, t in enumerate(targets):
        which = f"第{len(turns) - len(targets) + idx + 1}轮："
        for msg in check_one(t, turns, case.get("checks") or {}, uid):
            failures.append(which + msg if len(targets) > 1 else msg)
    return {"id": case["id"], "why": case.get("why", ""), "ok": not failures,
            "failures": failures, "last_reply": turns[-1].get("reply", "")[:400],
            "degraded": turns[-1].get("degraded")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--only", default="")
    ap.add_argument("--report", default=str(ROOT / "eval" / "last-report.json"))
    args = ap.parse_args()

    spec = json.loads(CASES.read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if not args.only or args.only in c["id"]]
    if not cases:
        print("没有匹配的用例")
        return 2

    try:
        get(args.base, "/api/health")
    except (urllib.error.URLError, OSError) as exc:
        print(f"连不上 {args.base}：{exc}\n先起服务：python server/main.py")
        return 2

    results = []
    for i, case in enumerate(cases, 1):
        try:
            r = run_case(args.base, case, i)
        except Exception as exc:  # 单条用例崩了不影响其它
            r = {"id": case["id"], "why": case.get("why", ""), "ok": False,
                 "failures": [f"{type(exc).__name__}: {exc}"], "last_reply": "", "degraded": None}
        results.append(r)
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"[{mark}] {r['id']}")
        for f in r["failures"]:
            print(f"        - {f}")

    passed = sum(1 for r in results if r["ok"])
    print(f"\n{passed}/{len(results)} 通过")
    Path(args.report).write_text(
        json.dumps({"passed": passed, "total": len(results), "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{args.report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
