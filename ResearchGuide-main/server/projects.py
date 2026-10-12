# -*- coding: utf-8 -*-
"""边学边练：按「方向 + 走到哪」检索公开来源的练手项目。

流程（代码先跑，模型后判；见 skills/README.md）：
1. 从 knowledge/project_sources.json 取出对得上方向和阶段的来源；
2. 有接口的来源由 project_adapters 实时检索（失败回退到该来源的已核对快照，并标明「快照」）；
3. 规则打分取前若干条候选；
4. 模型按 skills/project-scout/SKILL.md 从候选里挑、只依据原文写「在练什么 / 大概要做什么」；
   返回的 id 必须在候选里，否则丢弃并回退规则版；
5. 其余来源整理成「去哪找」路线，交给学生自己去看。

查不到就返回空列表，不补假项目。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Any

import llm
import planner
import project_adapters
import skills
import store
from limits import TTLCache
from schemas import now_iso

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "knowledge" / "project_sources.json"
PATHS = ROOT / "knowledge" / "paths.json"   # 任务 4 方向路径的机读版（目前有数学、人工智能）

STAGE_LABELS = ["只学了概念", "做过小任务", "学完一块", "做过项目"]
KIND_LABELS = {
    "competition": "竞赛", "open_source": "开源", "open_problem": "公开题", "dataset": "公开数据",
    "course_project": "课程大作业", "innovation_program": "创新项目",
}

# 方向关键词：给实时接口当查询词，也给规则打分用
DIR_TERMS: dict[str, list[str]] = {
    "ai": ["机器学习", "人工智能", "深度学习", "神经网络", "分类", "预测", "自然语言", "NLP", "图像", "视觉", "大模型", "推荐", "数据挖掘", "AI", "模型"],
    "math": ["数学", "建模", "优化", "证明", "概率", "微分方程", "图论", "数值", "规划", "算法", "几何", "数论"],
    "stat": ["统计", "数据分析", "回归", "抽样", "调查", "问卷", "可视化", "假设检验", "数据", "预测"],
    "psy": ["心理", "认知", "行为", "情绪", "决策", "实验", "问卷", "注意", "记忆", "脑", "社会"],
    "econ": ["经济", "金融", "市场", "消费", "价格", "政策", "收入", "就业", "贸易", "产业", "营销", "调研"],
    "se": ["软件", "系统", "开源", "前端", "后端", "数据库", "编译", "操作系统", "工具", "网络", "文档", "测试"],
}

_REG_CACHE: tuple[float, dict[str, Any]] | None = None
_PATHS_CACHE: tuple[float, dict[str, Any]] | None = None
DEFAULT_STEP = {0: 1, 1: 2, 2: 3, 3: 5}  # 没有路径进度时，由项目阶段推一个默认的路径步骤
LAST_TTL = 2 * 3600
# 每人最近一次检索的结果（选项目时只认这里面的）：两小时过期即删、最多记这么多人，不再无限攒着
_LAST = TTLCache(LAST_TTL, 2048)
SEARCH_BUDGET = 12  # 秒。慢的来源不等：先用它的快照，后台继续跑完并写进缓存，下一次就快了
# 所有检索共用一个有上限的线程池（原来每次检索新开 8 个，超时的还在后台跑，六次检索就能留下 48 个线程）
SOURCE_WORKERS = 8
_SOURCE_POOL = ThreadPoolExecutor(max_workers=SOURCE_WORKERS, thread_name_prefix="source")
# 提交前合并：同一来源、同一组条件正在跑的任务直接共用，不再提交一个只会干等的任务去占线程
_SOURCE_JOBS: dict[tuple, tuple[Future, list[float]]] = {}  # 任务，以及所有在等它的检索里最晚的截止时间
_SOURCE_JOBS_LOCK = threading.RLock()  # 已完成的任务加回调会立刻在本线程回调，所以要可重入


def registry() -> dict[str, Any]:
    global _REG_CACHE
    mtime = REGISTRY.stat().st_mtime
    if _REG_CACHE and _REG_CACHE[0] == mtime:
        return _REG_CACHE[1]
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    _REG_CACHE = (mtime, data)
    return data


def paths() -> dict[str, Any]:
    """{方向代码: {name, goal, source_doc, steps:[{step, name, focus, done_when, project_stage, project_form}]}}"""
    global _PATHS_CACHE
    if not PATHS.exists():
        return {}
    mtime = PATHS.stat().st_mtime
    if _PATHS_CACHE and _PATHS_CACHE[0] == mtime:
        return _PATHS_CACHE[1]
    data = json.loads(PATHS.read_text(encoding="utf-8")).get("paths") or {}
    _PATHS_CACHE = (mtime, data)
    return data


def path_step(direction: str, step: int) -> dict[str, Any] | None:
    for s in (paths().get(direction) or {}).get("steps") or []:
        if s["step"] == step:
            return s
    return None


def _pid(source_id: str, url: str, title: str) -> str:
    return hashlib.sha1(f"{source_id}|{url}|{title}".encode("utf-8")).hexdigest()[:16]


def direction_name(code: str) -> str:
    d = planner.DIRECTIONS.get(code)
    return d["name"] if d else code


# ---------- 走到哪 ----------

def context(uid: str) -> dict[str, Any]:
    """从服务端记录推一个默认阶段；前端让用户自己改。"""
    facts = store.list_facts(uid, statuses=["confirmed", "active"])
    dirs = [f for f in facts if f.key.startswith("direction:")]
    direction = dirs[-1].key.split(":", 1)[1] if dirs else ""
    tasks = [t for t in store.list_tasks(uid) if t.status == "done" and (not direction or t.direction == direction)]
    reviewed = [p for p in store.list_projects(uid) if (p.get("reviews") or [])]
    good = [p for p in reviewed if p["reviews"][0].get("passed", 0) >= 3]
    if good:
        stage, reason = 3, f"交过 {len(good)} 个项目且至少三条做到"
    elif len(tasks) >= 3:
        stage, reason = 2, f"在「{direction_name(direction)}」交过 {len(tasks)} 次小任务"
    elif tasks:
        stage, reason = 1, f"在「{direction_name(direction)}」交过 {len(tasks)} 次小任务"
    else:
        stage, reason = 0, "还没有交过小任务" if direction else "还没有选定方向"
    public = {code: {"name": p["name"], "goal": p.get("goal", ""), "source_doc": p.get("source_doc", ""),
                     "steps": [{k: st[k] for k in ("step", "name", "done_when", "project_stage")} for st in p["steps"]]}
              for code, p in paths().items()}
    return {"direction": direction, "stage": stage, "stage_label": STAGE_LABELS[stage], "reason": reason,
            "paths": public, "path_step": DEFAULT_STEP[stage] if direction in public else None}


# ---------- 检索 ----------

def _closed(deadline: str) -> bool:
    """截止日期早于今天就算已截止（天池等来源的「进行中」标记不可靠，以日期为准）。"""
    try:
        return datetime.fromisoformat(deadline[:10]).date() < datetime.now().date()
    except ValueError:
        return False


def _norm(src: dict[str, Any], raw: dict[str, Any], snapshot: bool, direction: str = "", keywords: str = "") -> dict[str, Any]:
    url = raw.get("url") or src.get("home_url") or ""
    title = (raw.get("title") or "").strip()
    if raw.get("direction"):
        dirs = [raw["direction"]]
    else:
        # 实时结果不继承来源的方向：只有标题或简介里真的出现方向词 / 关键词，才算这个方向的项目
        hay = f"{title} {raw.get('description') or ''}".lower()
        terms = DIR_TERMS.get(direction, []) + ([keywords] if keywords else [])
        dirs = [direction] if any(t.lower() in hay for t in terms if t) else []
    return {
        "id": _pid(src["id"], url, title),
        "name": title,
        "url": url,
        "source_id": src["id"],
        "source_name": src["name"],
        "source_url": src.get("home_url") or "",
        "kind": src.get("kind") or "",
        "excerpt": (raw.get("description") or raw.get("todo") or "").strip()[:600],
        "practices": (raw.get("practices") or "").strip(),
        "todo": (raw.get("todo") or "").strip(),
        "difficulty": (raw.get("difficulty") or raw.get("difficulty_raw") or "").strip(),
        "deadline": (raw.get("deadline") or "").strip(),
        "stage_fit": raw.get("stage_fit") if isinstance(raw.get("stage_fit"), list) else src.get("stage_fit") or [],
        "directions": dirs,
        "evidence_quote": (raw.get("evidence_quote") or "").strip()[:60],
        "retrieved_at": raw.get("retrieved_at") or now_iso(),
        "snapshot": snapshot,
        "closed": _closed((raw.get("deadline") or "").strip()),
    }


def _collect(src: dict[str, Any], direction: str, stage: int, keywords: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """一个来源：先实时，失败或没有接口就用快照。返回（候选，状态）。"""
    adapter = src.get("adapter")
    fetch = project_adapters.ADAPTERS.get(adapter or "")
    if fetch:
        terms = ([keywords] if keywords else []) + DIR_TERMS.get(direction, [])[:2]
        try:
            items = fetch(src, direction, stage, terms)
            got = [_norm(src, it, False, direction, keywords) for it in items if it.get("title") and it.get("url")]
            return got, {"id": src["id"], "name": src["name"], "ok": True, "count": len(got), "live": True}
        except project_adapters.AdapterError as exc:
            err = str(exc)
        except Exception as exc:  # 外部来源的任何异常都不能拖垮检索
            err = f"{type(exc).__name__}"
        snap = [_norm(src, s, True) for s in src.get("samples") or []]
        return snap, {"id": src["id"], "name": src["name"], "ok": bool(snap), "count": len(snap),
                      "snapshot": True, "error": f"实时检索失败（{err}）" + ("，改用快照" if snap else "")}
    snap = [_norm(src, s, True) for s in src.get("samples") or []]
    return snap, {"id": src["id"], "name": src["name"], "ok": bool(snap), "count": len(snap), "snapshot": True,
                  "error": "" if snap else "没有公开接口，也没有快照"}


def _snapshot(src: dict[str, Any], error: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    got = [_norm(src, x, True) for x in src.get("samples") or []]
    return got, {"id": src["id"], "name": src["name"], "ok": bool(got), "count": len(got), "snapshot": True,
                 "error": (error + "，这次先用快照") if got else error}


def _collect_before(src: dict[str, Any], direction: str, stage: int, keywords: str,
                    deadline: list[float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在共享池里跑。排队排到所有等它的检索都过了截止时间才轮到的，不再打上游，直接用快照。
    deadline 是可变的一格：后来加入的检索会把它往后推，不按最早那个检索的截止时间算。"""
    if time.time() >= deadline[0]:
        return _snapshot(src, f"排队超过 {SEARCH_BUDGET} 秒")
    return _collect(src, direction, stage, keywords)


def _source_job(src: dict[str, Any], direction: str, stage: int, keywords: str, deadline: float) -> Future:
    key = (src["id"], direction, stage, keywords)
    with _SOURCE_JOBS_LOCK:
        entry = _SOURCE_JOBS.get(key)
        if entry is not None:
            entry[1][0] = max(entry[1][0], deadline)
            return entry[0]
        box = [deadline]
        job = _SOURCE_POOL.submit(_collect_before, src, direction, stage, keywords, box)
        _SOURCE_JOBS[key] = (job, box)

        def forget(done: Future, k: tuple = key) -> None:
            with _SOURCE_JOBS_LOCK:
                if k in _SOURCE_JOBS and _SOURCE_JOBS[k][0] is done:
                    del _SOURCE_JOBS[k]

        job.add_done_callback(forget)
    return job


def _score(item: dict[str, Any], direction: str, stage: int, keywords: str,
           chain: list[str] | None = None) -> float:
    hay = f"{item['name']} {item['excerpt']} {item['practices']} {item['todo']}"
    hay_l = hay.lower()
    s = 0.0
    if direction and direction in item["directions"]:
        s += 3
    if direction:
        s += sum(1 for t in DIR_TERMS.get(direction, []) if t.lower() in hay_l) * 0.6
    if keywords:
        parts = [k for k in keywords.replace("，", " ").replace(",", " ").split() if k]
        s += sum(4 for k in parts if k.lower() in hay_l)
    if chain:
        for phrase in chain:
            p = (phrase or "").strip()
            if not p:
                continue
            if p.lower() in hay_l:
                s += 6
            else:
                toks = [t for t in re.split(r"[\s/，,]+", p) if len(t) >= 2]
                s += sum(2.5 for t in toks if t.lower() in hay_l)
    fit = item["stage_fit"] or []
    if fit:
        s += 2 if stage in fit else (-1.5 if min(abs(stage - f) for f in fit) > 1 else 0)
    if item["snapshot"]:
        s -= 0.3
    if item.get("closed"):
        s -= 1
    return s


def _pick_llm(cands: list[dict[str, Any]], direction: str, stage: int, keywords: str, node: str,
              step: dict[str, Any] | None = None, chain: list[str] | None = None) -> list[dict[str, Any]] | None:
    if not llm.enabled() or not cands:
        return None
    lines = [
        f"{c['id']} | {c['source_name']} | {c['name']} | {c['difficulty'] or '-'} | {(c['deadline'] + ('（已截止，只能当练习）' if c['closed'] else '')) if c['deadline'] else '-'} | {(c['excerpt'] or c['todo'] or c['practices'])[:260]}"
        for c in cands
    ]
    dir_label = direction_name(direction) if direction else "还不能对应到一个学院方向"
    chain_line = f"用户从宽到窄说过：{' / '.join(chain)}\n" if chain else ""
    data = llm.chat_json(
        skills.load("project-scout"),
        f"学生：方向={dir_label}；阶段={stage}（{STAGE_LABELS[stage]}）；关键词={keywords or '无'}；当前节点={node or '无'}\n"
        + chain_line
        + (f"路径步骤：第 {step['step']} 步「{step['name']}」；这一步做完要交：{step['done_when']}" + (f"；任务 4 建议的项目形态（参考）：{step['project_form']}" if step.get('project_form') else "") + "\n" if step else "路径步骤：无\n")
        + "只能从下面的候选 id 里挑，不许造一条项目。\n"
        + "候选（id | 来源 | 标题 | 难度 | 截止 | 原文摘录）：\n" + "\n".join(lines),
        timeout=40, tag="project-scout",
    )
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    by_id = {c["id"]: c for c in cands}
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("id") or "")
        if cid not in by_id or cid in seen:
            continue  # 只接受检索到的候选
        seen.add(cid)
        c = dict(by_id[cid])
        for key, cap in (("practices", 60), ("todo", 120), ("why_fit", 60)):
            val = str(it.get(key) or "").strip()
            if val:
                c[key] = val[:cap]
        c["voice"] = "llm"
        out.append(c)
        if len(out) >= 6:
            break
    return out


def _fallback_text(c: dict[str, Any]) -> dict[str, Any]:
    c = dict(c)
    if not c["todo"]:
        c["todo"] = (c["excerpt"][:110] + "…") if len(c["excerpt"]) > 110 else (c["excerpt"] or "来源没写明具体要求，打开链接看原题。")
    c["voice"] = "rules"
    return c


def _routes(reg: dict[str, Any], direction: str, used: set[str]) -> list[dict[str, Any]]:
    out = []
    for s in reg["sources"]:
        if direction not in (s.get("directions") or []) or s.get("access") == "unreachable":
            continue
        if not s.get("manual_route"):
            continue
        out.append({
            "id": s["id"], "name": s["name"], "url": s.get("home_url") or "",
            "kind_label": KIND_LABELS.get(s.get("kind") or "", ""), "cadence": s.get("cadence") or "",
            "manual_route": s["manual_route"], "search_terms": s.get("search_terms") or [],
            "used": s["id"] in used,
        })
    out.sort(key=lambda r: r["used"])
    return out


def search(uid: str, direction: str, stage: int, keywords: str = "", node: str = "", step_no: int = 0,
           chain: list[str] | None = None) -> dict[str, Any]:
    reg = registry()
    step = path_step(direction, int(step_no or 0)) if direction else None
    if step:
        # 有任务 4 的路径时，以路径步骤为准：步骤决定项目阶段，步骤名当作「正在学的节点」
        stage = step["project_stage"]
        node = node or step["name"]
    stage = max(0, min(3, int(stage)))
    keywords = (keywords or "").strip()[:40]
    chain = [c.strip() for c in (chain or []) if isinstance(c, str) and c.strip()][:8]
    sources = [s for s in reg["sources"]
               if s.get("access") != "unreachable"
               and (not direction or direction in (s.get("directions") or []))
               and (not s.get("stage_fit") or stage in s["stage_fit"] or any(abs(stage - f) <= 1 for f in s["stage_fit"]))]
    cands: list[dict[str, Any]] = []
    status: list[dict[str, Any]] = []
    # 既没有接口也没有快照的来源只出现在「去哪找」里，不算一次检索
    sources = [s for s in sources if s.get("adapter") or s.get("samples")]
    deadline = time.time() + SEARCH_BUDGET
    futs = {_source_job(s, direction, stage, keywords, deadline): s for s in sources}
    done: set = set()
    try:
        for fut in as_completed(futs, timeout=max(0.0, deadline - time.time())):
            got, st = fut.result()
            cands.extend(got)
            status.append(st)
            done.add(fut)
    except FutureTimeout:
        for fut, src in futs.items():
            if fut in done:
                continue
            if fut.done():
                got, st = fut.result()
            else:
                # 任务可能和别的检索共用，不取消：还在排队的轮到时已过截止时间，会直接用快照；
                # 已经在跑的跑完把结果留在适配器缓存里，且受请求的总时限约束
                got, st = _snapshot(src, f"超过 {SEARCH_BUDGET} 秒没返回")
            cands.extend(got)
            status.append(st)
    status.sort(key=lambda s: (not s["ok"], s["name"]))
    uniq: dict[str, dict[str, Any]] = {}
    for c in cands:
        if c["name"] and c["url"]:
            uniq.setdefault(c["id"], c)

    def sc(c: dict[str, Any]) -> float:
        return _score(c, direction, stage, keywords, chain or None)

    ranked = sorted(uniq.values(), key=lambda c: -sc(c))
    ranked = [c for c in ranked if sc(c) > 0][:24]
    chosen = _pick_llm(ranked, direction, stage, keywords, node, step, chain=chain or None)
    voice = "llm"
    if not chosen:
        voice = "rules"
        chosen = [_fallback_text(c) for c in ranked[:5]]
    for c in chosen:
        c.setdefault("why_fit", "")
        if step:
            c["path_step"] = step["step"]
            c["path_step_name"] = step["name"]
    picked_ids = {p.get("source_ref") for p in store.list_projects(uid)}
    for c in chosen:
        c["picked"] = c["id"] in picked_ids
    _LAST.set(uid, {c["id"]: c for c in ranked + chosen})
    used = {c["source_id"] for c in chosen}
    empty_reason = ""
    if not chosen:
        empty_reason = ("这些来源里没有和这个方向、这个阶段对得上的公开项目。" if ranked or cands
                        else "这次所有来源都没有返回结果。") + "不补假项目：可以换个关键词，或照下面的路线自己去看。"
    dir_name = direction_name(direction) if direction else "还不能对应到一个学院方向，先按你的描述找"
    return {
        "query": {"direction": direction, "direction_name": dir_name, "stage": stage,
                  "stage_label": STAGE_LABELS[stage], "keywords": keywords,
                  "path_step": step["step"] if step else None, "path_step_name": step["name"] if step else "",
                  "chain": chain},
        "items": chosen, "sources": status, "routes": _routes(reg, direction, used),
        "empty_reason": empty_reason, "voice": voice, "retrieved_at": now_iso(),
    }


# ---------- 由宽到窄的问答 ----------

IV_TTL = 24 * 3600
_IV = TTLCache(IV_TTL, 2048)
_FAKE_OPT = re.compile(r"某校课程|我猜|假项目")

Q0 = {
    "question": "你现在想先碰哪一类？",
    "options": [
        {"id": "proof", "label": "把式子或证明弄清楚"},
        {"id": "code", "label": "写一点能跑的程序"},
        {"id": "observe", "label": "做观察或小实验"},
        {"id": "write", "label": "读材料并写清楚"},
        {"id": "unknown", "label": "还不知道"},
    ],
}
Q1 = {
    "question": "更想要哪一种？",
    "options": [
        {"id": "closed", "label": "有标准答案的练习"},
        {"id": "open", "label": "自己定义对错的问题"},
        {"id": "unknown", "label": "还不知道"},
    ],
}
Q2 = {
    "question": "有没有必须用上的课或工具？",
    "options": [
        {"id": "has", "label": "有，我下面写"},
        {"id": "none", "label": "没有"},
        {"id": "unknown", "label": "还不知道"},
    ],
}


def _iv_key(uid: str, sid: str) -> str:
    return f"{uid}:{sid}"


def _new_sid(uid: str) -> str:
    raw = f"{uid}|{time.time()}|{threading.get_ident()}"
    return "iv_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _valid_interview_q(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("done") is True:
        return True
    q = str(data.get("question") or "").strip()
    opts = data.get("options")
    if not q or not isinstance(opts, list) or not (2 <= len(opts) <= 5):
        return False
    for o in opts:
        if not isinstance(o, dict):
            return False
        lab = str(o.get("label") or "").strip()
        if not lab or len(lab) > 24 or _FAKE_OPT.search(lab):
            return False
        if not str(o.get("id") or "").strip():
            return False
    return True


def _label_of(option_id: str, answer: str, options: list[dict[str, Any]]) -> str:
    if option_id == "unknown":
        return "还不知道"
    if option_id == "done":
        return ""
    if option_id:
        hit = next((o for o in options if o.get("id") == option_id), None)
        if hit:
            return str(hit.get("label") or option_id)
    return (answer or "").strip()[:40]


def _chain_to_query(chain: list[str], answers: list[dict[str, str]]) -> dict[str, Any]:
    facets: list[str] = []
    direction = ""
    first = (answers[0] or {}).get("option_id") if answers else ""
    if first == "proof":
        direction, facets = "math", ["proof"]
    elif first == "code":
        direction, facets = "se", ["code"]
    elif first == "observe":
        facets = ["observe"]
    elif first == "write":
        facets = ["write"]
    for a in answers[1:]:
        oid = a.get("option_id") or ""
        if oid == "closed":
            facets.append("undergraduate")
        elif oid == "open":
            facets.append("open")
    keywords = ""
    for a in reversed(answers):
        txt = (a.get("answer") or "").strip()
        if txt:
            keywords = txt[:40]
            break
    return {"direction": direction, "stage": 0, "keywords": keywords, "facets": facets}


def _interview_done(sess: dict[str, Any]) -> dict[str, Any]:
    sess["done"] = True
    _IV.set(_iv_key(sess["uid"], sess["id"]), sess)
    return {
        "done": True,
        "session_id": sess["id"],
        "chain": list(sess["chain"]),
        "query": _chain_to_query(sess["chain"], sess["answers"]),
    }


def _next_interview_q(sess: dict[str, Any]) -> dict[str, Any]:
    if not llm.enabled():
        if sess["turns"] == 1:
            return dict(Q1)
        if sess["turns"] == 2:
            return dict(Q2)
        return {"done": True}
    data = llm.chat_json(
        "你在帮本科生收窄「想练哪一类项目」。只返回 JSON："
        '{"question":"一句问题","options":[{"id":"a","label":"≤24字"}],"done":false}。'
        "必须 2–5 个选项。允许不知道。不许发明课程名、项目名、老师。材料不够就 done=true。",
        "已经说过：" + " / ".join(sess["chain"]),
        timeout=20, tag="project-interview",
    )
    if not _valid_interview_q(data):
        if sess["turns"] == 1:
            return dict(Q1)
        if sess["turns"] == 2:
            return dict(Q2)
        return {"done": True}
    if data.get("done") is True:
        return {"done": True}
    return {
        "question": str(data["question"]).strip(),
        "options": [{"id": str(o["id"]).strip(), "label": str(o["label"]).strip()} for o in data["options"]],
    }


def interview(uid: str, session_id: str = "", answer: str = "", option_id: str = "") -> dict[str, Any]:
    sid = (session_id or "").strip()
    option_id = (option_id or "").strip()
    answer = (answer or "").strip()[:80]
    if not sid:
        sid = _new_sid(uid)
        sess = {"uid": uid, "id": sid, "turns": 0, "chain": [], "answers": [],
                "last_options": list(Q0["options"]), "done": False}
        _IV.set(_iv_key(uid, sid), sess)
        return {
            "session_id": sid, "done": False, "question": Q0["question"],
            "options": list(Q0["options"]), "allow_free": True, "unknown_ok": True, "chain": [],
        }
    hit, sess = _IV.lookup(_iv_key(uid, sid))
    if not hit:
        raise KeyError("问答已过期，请重新开始")
    if sess.get("uid") != uid:
        raise PermissionError("不是你的问答")
    if sess.get("done"):
        return _interview_done(sess)
    label = _label_of(option_id, answer, sess.get("last_options") or Q0["options"])
    if option_id == "done" or (not label and not option_id and not answer):
        if option_id == "done":
            return _interview_done(sess)
        return {
            "session_id": sid, "done": False, "question": Q0["question"] if not sess["turns"] else "还可以再说具体一点。",
            "options": list(sess.get("last_options") or Q0["options"]),
            "allow_free": True, "unknown_ok": True, "chain": list(sess["chain"]),
        }
    if label:
        sess["chain"].append(label)
        sess["answers"].append({"option_id": option_id, "answer": answer})
        sess["turns"] += 1
    if sess["turns"] >= 5 or (not llm.enabled() and sess["turns"] >= 3):
        return _interview_done(sess)
    nxt = _next_interview_q(sess)
    if nxt.get("done"):
        return _interview_done(sess)
    sess["last_options"] = nxt["options"]
    _IV.set(_iv_key(uid, sid), sess)
    return {
        "session_id": sid, "done": False, "question": nxt["question"],
        "options": nxt["options"], "allow_free": True, "unknown_ok": True,
        "chain": list(sess["chain"]),
    }


# ---------- 选定 / 模板 / 示例 ----------

PUBLIC_KEYS = ("name", "url", "source_id", "source_name", "source_url", "practices", "todo", "why_fit",
               "difficulty", "deadline", "closed", "retrieved_at", "snapshot", "evidence_quote", "kind",
               "path_step", "path_step_name")


def pick(uid: str, cid: str) -> dict[str, Any]:
    hit, last = _LAST.lookup(uid)
    if not hit or cid not in last:
        raise KeyError("这个项目不在最近一次检索结果里，请重新检索后再选。")
    c = last[cid]
    existing = next((p for p in store.list_projects(uid) if p.get("source_ref") == cid), None)
    if existing:
        return existing
    data = {k: c.get(k) for k in PUBLIC_KEYS}
    data.update({"source_ref": cid, "reviews": [], "picked_at": now_iso()})
    pid = hashlib.sha1(f"{uid}|{cid}".encode()).hexdigest()[:16]
    return store.save_project(uid, pid, data, status="picked")


def readme_template(p: dict[str, Any]) -> str:
    return f"""# {p['name']}

> 来源：{p.get('source_name', '')} {p.get('url', '')}（检索于 {str(p.get('retrieved_at', ''))[:10]}）

## 题目

<!-- 用一两句自己的话写：这个项目要回答什么问题？你打算做到哪一步？ -->

## 我做了什么

<!-- 按顺序写 3–5 步：数据从哪来、怎么处理、用了什么方法。对照项目要求：{p.get('todo', '')} -->

## 结果在哪

<!-- 每个结果文件一行：路径 + 它说明了什么。例如：
- results/figure1.png：两组的分布对比，B 组明显偏右
- results/table.csv：每类的精确率和召回率 -->

## 怎么复现

<!-- 代码类：运行哪条命令 / 打开哪个 notebook，会得到 results/ 里的哪个文件。
调查、写作类：数据来源链接、问卷或访谈提纲放在哪。 -->

## 还没做完的

<!-- 哪些要求没做到、结果哪里不可靠、下一步打算怎么改。 -->
"""


def sample_zip(p: dict[str, Any]) -> bytes:
    """一份「交成什么样」的示例：结构和写法是真的，内容标明是示例，不能直接交。"""
    readme = f"""# {p['name']}（示例成果，只演示格式）

> 来源：{p.get('source_name', '')} {p.get('url', '')}

## 题目

这是一份演示格式的示例。真实成果里，这里用一两句自己的话写清要回答的问题，例如「在这份公开数据上，A 和 B 两组到底差多少、差异稳不稳」。

## 我做了什么

1. 从来源页面下载公开数据，保留原始文件的下载链接（见「怎么复现」）。
2. 去掉缺失值和重复行，记录删掉了多少条。
3. 按项目要求做了两组对比，并画了分布图。

## 结果在哪

- results/summary.csv：两组的样本量、均值和中位数
- results/notes.md：三条观察，以及每条观察对应的图表

## 怎么复现

运行 `python src/analysis.py`，会重新生成 results/summary.csv。数据下载链接：{p.get('url', '')}

## 还没做完的

- 只做了描述性对比，没有做显著性检验。
- 样本只来自一个公开来源，结论不能推到别的人群。
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("示例成果/README.md", readme)
        z.writestr("示例成果/results/summary.csv", "group,n,mean,median\nA,120,3.42,3.4\nB,118,3.91,4.0\n")
        z.writestr("示例成果/results/notes.md", "1. B 组均值高约 0.5（见 summary.csv）。\n2. 两组中位数差与均值差一致，说明不是少数极端值拉出来的。\n3. A 组有 6 条缺失，已删除。\n")
        z.writestr("示例成果/src/analysis.py", "import csv\n\n# 示例：读取数据、分组、写出 results/summary.csv\nprint('replace with your own analysis')\n")
    return buf.getvalue()


def record_review(uid: str, p: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """保存评阅，并把这次提交作为一条行为事实写回画像（真实入库）。"""
    from schemas import UserFact
    slim = [{k: i.get(k) for k in ("path", "kind", "size")} for i in (review.get("inventory") or [])[:60]]
    reviews = [dict(review, inventory=slim)] + (p.get("reviews") or [])
    fact = UserFact(
        user_id=uid, category="experience", key=f"project:{p['id']}",
        value=f"交了项目《{p['name'][:30]}》的成果：{review['passed']}/{review['total']} 条做到",
        confidence=0.8, source="behavior",
        evidence=[{"type": "project_submission", "project_id": p["id"], "task_title": p["name"][:40],
                   "files": len(review.get("inventory") or [])}],
        status="active",
    )
    store.add_fact(fact)
    data = {k: v for k, v in p.items() if k not in ("id", "status", "created_at", "updated_at")}
    data["reviews"] = reviews[:10]
    # 五条都做到算这个项目做完了；否则留在「已评阅」，今日会提醒按评阅再改一处
    store.save_project(uid, p["id"], data, status="done" if review["passed"] == review["total"] else "reviewed")
    return fact.to_dict()
