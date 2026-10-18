# -*- coding: utf-8 -*-
"""启研 · AI Research Mentor —— W0 Demo 后端（FastAPI 单体）。

运行：python server/main.py  （或 uvicorn server.main:app --port 8100）
默认地址 http://127.0.0.1:8100/
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import admin
import auth
import catalog
import curriculum
import dialogue
import llm
import mcp_curriculum
import memory
import onboarding
import osf
import pdf as pdf_mod
import planner
import positioning
import projects
import quotes
import reading
import store
import submission
import transcript
import tutor
import workbench
import workpool
from schemas import now_iso
from pku_adapter import search_courses
from limits import TTLCache
from singleflight import AsyncFlight

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Starlette 的工作线程池默认 40 个：每个同步接口、每个流式对话各占一个线程直到做完。
# 四十个学生同时在对话，第四十一个人的任何请求都要排队。线程等模型回话几乎不占资源，可以多开。
THREADS = int(os.environ.get("QIYAN_THREADS") or 128)
_LIMITER = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _LIMITER
    _LIMITER = anyio.to_thread.current_default_thread_limiter()
    _LIMITER.total_tokens = THREADS
    if store.ephemeral():
        print(json.dumps({"warn": "db_ephemeral", "msg": "库在临时盘上，实例回收会丢掉所有账号和记录；设 QIYAN_DB 指到持久盘"},
                         ensure_ascii=False))
    yield


app = FastAPI(title="启研 · AI Research Mentor (W0 Demo)", version="0.1.0", dependencies=[Depends(auth.guard)],
              lifespan=_lifespan)
store.init_db()
app.include_router(auth.router)
app.include_router(admin.router)


@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    if os.environ.get("QIYAN_MAINTENANCE") == "1":
        path = request.url.path or ""
        if path.startswith("/api/") and not path.startswith("/api/admin") and path not in ("/api/health", "/metrics"):
            return JSONResponse({"detail": "维护中"}, status_code=503)
    return await call_next(request)


@app.middleware("http")
async def record_metrics(request: Request, call_next):
    import metrics as _metrics
    t0 = time.time()
    raw = request.url.path or ""
    watch = raw.startswith("/api/") or raw in ("/", "/metrics")
    try:
        resp = await call_next(request)
    except Exception:
        if watch:
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "unknown"
            _metrics.observe_request(path, 500, time.time() - t0)
        raise
    if watch:
        code = getattr(resp, "status_code", None)
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unknown"
        media = (getattr(resp, "media_type", None) or resp.headers.get("content-type") or "")
        iterator = getattr(resp, "body_iterator", None)
        if iterator is not None and "text/event-stream" in media:
            async def _wrap():
                err = False
                try:
                    async for chunk in iterator:
                        raw_chunk = chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
                        if b"event: error" in raw_chunk:
                            err = True
                            _metrics.observe_stream_error()
                        yield chunk
                finally:
                    _metrics.observe_request(path, 500 if err else (0 if code is None else int(code)),
                                             time.time() - t0)
            resp.body_iterator = _wrap()
        else:
            _metrics.observe_request(path, 0 if code is None else int(code), time.time() - t0)
    return resp


@app.middleware("http")
async def prefer_inline_browse(request: Request, call_next):
    """页面和静态资源按网页打开，不要被当成附件下载。

    真要下载的接口自己带了 filename=（作业 zip、README），那些不动。
    函数计算默认域名有时会补一个光秃秃的 attachment，这里改回 inline。
    """
    resp = await call_next(request)
    cd = (resp.headers.get("content-disposition") or "").lower()
    if "filename=" in cd:
        return resp
    resp.headers["content-disposition"] = "inline"
    return resp


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 请求模型 ----------

class OnboardMsgReq(BaseModel):
    uid: str
    msg: str


class OnboardConfirmReq(BaseModel):
    uid: str
    edits: list[dict] = []


class NBAReq(BaseModel):
    uid: str


class ChooseDirectionReq(BaseModel):
    uid: str
    code: str


class TaskGenerateReq(BaseModel):
    uid: str
    direction: str
    level: int = 1
    title: str = ""
    brief: str = ""


class SubmitReq(BaseModel):
    uid: str
    payload: str


class TaskHintReq(BaseModel):
    uid: str
    question: str
    draft: str = ""


class FactPatchReq(BaseModel):
    uid: str
    value: str | None = None
    status: str | None = None


class LlmConnectReq(BaseModel):
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str
    model: str = "deepseek-flash"


class ProjectSearchReq(BaseModel):
    uid: str
    direction: str = ""
    stage: int = 0
    keywords: str = ""
    node: str = ""
    path_step: int = 0
    chain: list[str] | None = None


class ProjectInterviewReq(BaseModel):
    uid: str
    session_id: str = ""
    answer: str = ""
    option_id: str = ""


class ProjectPickReq(BaseModel):
    uid: str
    id: str


class TriageReq(BaseModel):
    uid: str
    kit: str = ""
    arxiv_id: str
    verdict: str
    why: str
    title: str = ""


class CardReq(BaseModel):
    uid: str
    kit: str
    arxiv_id: str
    fields: dict[str, str] = {}
    dims: dict[str, str] = {}
    decision_log: list[dict[str, str]] = []


class TutorStartReq(BaseModel):
    uid: str
    kit: str
    restart: bool = False


class TutorTurnReq(BaseModel):
    uid: str
    kit: str
    qid: str
    answer: str = Field("", max_length=2000)
    skip: bool = False
    rev: int = 0
    regrade: bool = False


class TutorFinishReq(BaseModel):
    uid: str
    kit: str


class EdgeReq(BaseModel):
    uid: str
    kind: str
    text: str
    evidence_url: str = ""


class StatementReq(BaseModel):
    uid: str
    kit: str
    x_ref: str = ""
    x_text: str = ""
    y: list[str] = []
    dry_run: bool = False


class ChannelReq(BaseModel):
    uid: str
    id: str
    on: bool
    direction: str


class BetReq(BaseModel):
    uid: str
    name: str
    kind: str
    tier: str
    kit: str = ""
    niche: str = ""


class BetCloseReq(BaseModel):
    uid: str
    outcome: str
    reason: str


class PortraitReq(BaseModel):
    uid: str
    id: str = ""


class PortraitRenameReq(BaseModel):
    uid: str
    id: str
    name: str = ""


class DialogueTurnReq(BaseModel):
    uid: str
    message: str
    conversation_id: str | None = None


class DialogueActionReq(BaseModel):
    uid: str
    action_id: str
    event: str  # accept | decline | complete


class TranscriptParseReq(BaseModel):
    uid: str
    text: str = ""


class TranscriptCommitReq(BaseModel):
    uid: str
    text: str = ""                      # 有原文就以原文为准，重新解析
    courses: list[dict] = []
    mode: str = "replace"               # replace | append


def _user_or_404(uid: str) -> dict:
    u = store.get_user(uid)
    if not u:
        raise HTTPException(404, "user not found")
    return u


# ---------- onboarding ----------

@app.post("/api/onboard/start")
def onboard_start(req: NBAReq):
    _user_or_404(req.uid)
    return onboarding.start(req.uid)


@app.post("/api/onboard/message")
def onboard_message(req: OnboardMsgReq):
    _user_or_404(req.uid)
    state = store.get_onboard_state(req.uid)
    if state.get("phase") == "done":
        raise HTTPException(400, "onboarding already done")
    # 任务2：配了模型就由新对话内核接管，**冷启动第一句也算**。
    # 这个判断必须排在 `not state` 之前：否则新用户的第一句会落到老向导的第 1 轮问题，
    # 要等到第二句才进内核，行为前后不一致。
    if llm.ready(req.uid):
        return _onboard_from_dialogue(req.uid, req.msg)
    # 没配模型时保持老向导，保证离线 demo 与既有测试不受影响。
    if not state:
        return onboarding.start(req.uid)
    return onboarding.message(req.uid, req.msg)


def _onboard_from_dialogue(uid: str, msg: str) -> dict:
    """把新内核的一轮结果翻译成旧向导的响应形状，老前端不动也能继续跑。"""
    # 空消息要在进内核之前挡住：内核按契约抛 ValueError，放它出去就是一个 500。
    # 之前这里没校验，前端发空串（例如点了没有内容的按钮）会直接把请求打崩。
    if not (msg or "").strip():
        raise HTTPException(400, "message is required")
    r = dialogue.turn(uid, msg)
    state = store.get_onboard_state(uid)
    return {
        "reply": r["reply"],
        "hint": "",
        "options": [a["label"] for a in r.get("offered_actions") or []],
        "facts": [f.to_dict() for f in store.list_facts(uid, statuses=["draft"])],
        "state": state,
        "done": bool(r.get("next_action")) or state.get("phase") == "done",
        # 新内核的附加信息；老前端会忽略这些字段
        "dialogue": {
            "conversation_id": r["conversation_id"],
            "move": r["move"],
            "memory_version": r["memory_version"],
            "degraded": r["degraded"],
            "rejected_ops": r["rejected_ops"],
            "next_action": r["next_action"],
            "pending_action": r["pending_action"],
            "tool_results": r["tool_results"],
            "trace": r["trace"],
        },
    }


@app.get("/api/onboard/result")
def onboard_result(uid: str):
    _user_or_404(uid)
    msgs = store.list_messages(uid)
    facts = [f.to_dict() for f in store.list_facts(uid)]
    return {"messages": msgs, "facts": facts,
            "state": store.get_onboard_state(uid)}


@app.post("/api/onboard/confirm")
def onboard_confirm(req: OnboardConfirmReq):
    _user_or_404(req.uid)
    confirmed = onboarding.confirm(req.uid, req.edits)
    return {"facts": [f.to_dict() for f in confirmed]}


@app.post("/api/onboard/enter")
def onboard_enter(req: NBAReq):
    """按人记下「进入启研」。幂等：entered_at 只写第一次。不碰画像、不挂 SERIAL。"""
    _user_or_404(req.uid)
    state = store.get_onboard_state(req.uid)
    phase = str(state.get("phase") or "")
    extra = {
        "entered_phase": phase,
        "entered_early": not dialogue.onboard_done(req.uid),
    }
    if not store.get_prefs(req.uid).get("entered_at"):
        extra["entered_at"] = now_iso()
    store.set_prefs(req.uid, extra=extra)
    return auth.account(req.uid)


# ---------- 对话内核（任务2：环境观察 → 决策 → 工具 → 记忆）----------

@app.post("/api/dialogue/turn")
def dialogue_turn(req: DialogueTurnReq):
    _user_or_404(req.uid)
    if not req.message.strip():
        raise HTTPException(400, "message is required")
    try:
        return dialogue.turn(req.uid, req.message, req.conversation_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/dialogue/stream")
def dialogue_stream(req: DialogueTurnReq):
    """与 /api/dialogue/turn 同一份内核，但把阶段推进和回复增量实时推给前端。

    事件：stage（observe/decide/memory/tool/compose）、delta（正文增量）、
    result（完整响应体，与 turn 一致）、error。
    """
    _user_or_404(req.uid)
    if not req.message.strip():
        raise HTTPException(400, "message is required")

    def gen():
        try:
            for kind, payload in dialogue.turn_steps(req.uid, req.message,
                                                     req.conversation_id, stream_reply=True):
                yield _sse(kind, payload)
        except ValueError as exc:
            yield _sse("error", {"error": str(exc)})
        except Exception as exc:  # 流已经开始，不能再抛 HTTP 错误，只能作为事件送出去
            yield _sse("error", {"error": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/dialogue/action")
def dialogue_action(req: DialogueActionReq):
    _user_or_404(req.uid)
    try:
        a = dialogue.action_event(req.uid, req.action_id, req.event)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if a is None:
        raise HTTPException(404, "action not found")
    return {"action": a}


@app.get("/api/dialogue/history")
def dialogue_history(uid: str, conversation_id: str | None = None):
    _user_or_404(uid)
    return dialogue.history(uid, conversation_id)


def _portrait_dir_nodes(trail: dict) -> list[str]:
    """画像收藏过的方向：trail.dirs 优先，老数据退到当前方向 trail.dir。"""
    import interests
    nodes = interests.table()["nodes"]
    out: list[str] = []
    for n in (trail.get("dirs") or []) if isinstance(trail.get("dirs"), list) else []:
        n = str(n or "")
        if n in nodes and n not in out:
            out.append(n)
    cur = str(trail.get("dir") or "")
    if cur in nodes and cur not in out:
        out.append(cur)
    return out


@app.get("/api/portraits")
def portraits_list(uid: str):
    _user_or_404(uid)
    import interests
    out = []
    for p in store.list_portraits(uid):
        trail = store.get_trail(uid, p["id"])
        dirs = _portrait_dir_nodes(trail)
        named = [{"node": n, "name": interests.direction_name(n)} for n in dirs]
        # 默认名：一个方向 = 方向名；多个 = 最先加的两个，形如 方向1·方向2
        default = "·".join([d["name"] for d in named if d["name"]][:2])[:40]
        # 勾选状态：sel 是列表就按它来（空 = 全没勾）；没有 sel 键的老数据视为全选
        raw_sel = trail.get("sel")
        if isinstance(raw_sel, list):
            ok = {d["node"] for d in named}
            sel_ids = [str(n or "") for n in raw_sel if str(n or "") in ok]
        else:
            sel_ids = [d["node"] for d in named]
        out.append({
            **p,
            # 展示名优先级：自己起的 > 按方向生成的默认名 > 画像N
            "label": p["name"] or default or p["fallback"],
            "default_label": default or p["fallback"],
            "custom": bool(p["name"]),
            "dir": str(trail.get("dir") or ""),
            "dirs": named,
            "sel": sel_ids,
        })
    return {"portraits": out}


@app.get("/api/memory")
def memory_board(uid: str):
    """按层返回「它记住的我」，供界面分列展示。"""
    _user_or_404(uid)
    return memory.board(uid)


@app.get("/api/me/coverage")
def memory_coverage(uid: str):
    """按用户给的九大类，列出每个固定字段填了没有 + 填了有什么用。

    「核对」页用它把空格显示出来：空格是邀请，但得说清填了能得到什么，
    否则就变成一张逼人填的表。
    """
    _user_or_404(uid)
    return memory.coverage(uid)


# ---------- 成绩单 ----------
# 分两步：先 parse（只解析、不写库，让用户看清楚识别到了什么），
# 再 commit（确认后才落库）。成绩单是快照，写错了会污染后面所有难度判断，
# 所以不让「粘贴即入库」。

@app.post("/api/me/transcript/parse")
def transcript_parse(req: TranscriptParseReq):
    """只解析，不落库。返回识别到的课程 + 汇总 + 警告 + 会推导出什么。"""
    _user_or_404(req.uid)
    parsed = transcript.parse_transcript(req.text)
    courses = parsed["courses"]
    return {
        "courses": courses,
        "by_term": transcript.summarize_by_term(courses),
        "summary": transcript.summarize(courses),
        # 先给他看「我会从中记下什么」。落库前就能发现我们读错了课，
        # 比事后去记忆面板里一条条删强。
        "preview": transcript.derive_capabilities(courses),
        "warnings": parsed["warnings"],
        "terms": parsed["terms"],
    }


@app.post("/api/me/transcript")
def transcript_commit(req: TranscriptCommitReq):
    """确认后落库。`mode=replace`（默认，整表替换）或 `append`（跳过完全重复）。"""
    _user_or_404(req.uid)
    items = req.courses or []
    # 不信前端逐条传回来的内容，优先拿原文重新解析一遍。
    # 顺序很重要：先解析再判空。写成先判空的话，
    # 只传原文（最常见的情形）会被当成「没给课程」直接 400——
    # 这个 bug 是端到端跑真实成绩单时抓到的，单元测试没覆盖到这条路径。
    if req.text:
        reparsed = transcript.parse_transcript(req.text)["courses"]
        if reparsed:
            items = reparsed
    if not items:
        raise HTTPException(400, "没有可写入的课程：请给 text 或 courses")
    clean = []
    for it in items:
        course = str(it.get("course") or "").strip()
        if not course:
            continue
        clean.append({
            "course": course[:120],
            "grade": transcript.normalize_grade(it.get("grade")),
            "credits": max(0.0, float(it.get("credits") or 0)),
            "term": str(it.get("term") or "")[:40],
            "kind": str(it.get("kind") or "")[:40],
            "status": (str(it.get("status") or "completed")
                       if str(it.get("status") or "") in ("completed", "current", "audit")
                       else "completed"),
        })
    if not clean:
        raise HTTPException(400, "没有可写入的课程")
    n = (store.add_enrollments(req.uid, clean) if req.mode == "append"
         else store.replace_enrollments(req.uid, clean))
    store.add_event(req.uid, "transcript_imported", "",
                    {"count": n, "mode": req.mode or "replace"})
    # 成绩单变了 → 重新推导能力结论。这一步是成绩单真正的用处：
    # 让「能力」从模型凭一段话编的判断，变成从底稿确定性推出来的事实。
    derived = memory.sync_transcript_facts(req.uid)
    return {"ok": True, "written": n, "derived": derived, **transcript_board(req.uid)}


@app.get("/api/me/transcript")
def transcript_get(uid: str):
    _user_or_404(uid)
    return transcript_board(uid)


@app.delete("/api/me/enrollments/{eid}")
def enrollment_delete(uid: str, eid: str):
    _user_or_404(uid)
    if not store.delete_enrollment(uid, eid):
        raise HTTPException(404, "enrollment not found")
    # 删课之后必须重新推导，否则「修过 N 门某某课」会留着过期结论——
    # 那比没有更糟，因为它看起来是最硬的那类证据。
    derived = memory.sync_transcript_facts(uid)
    return {"ok": True, "derived": derived, **transcript_board(uid)}


def transcript_board(uid: str) -> dict:
    """成绩单 + 汇总 + 分学期 + 推导出来的能力结论。界面直接用这一个。"""
    courses = store.list_enrollments(uid)
    derived = [f.to_dict() for f in memory.active_facts(uid)
               if str(f.key).startswith("transcript:")]
    return {
        "courses": courses,
        "summary": transcript.summarize(courses),
        "by_term": transcript.summarize_by_term(courses),
        "derived": derived,
    }


@app.post("/api/portraits")
def portraits_create(req: PortraitReq):
    _user_or_404(req.uid)
    store.open_new_portrait(req.uid)
    onboarding.start(req.uid)
    return {"portraits": store.list_portraits(req.uid)}


@app.post("/api/portraits/activate")
def portraits_activate(req: PortraitReq):
    _user_or_404(req.uid)
    if not req.id:
        raise HTTPException(400, "id is required")
    try:
        store.activate_portrait(req.uid, req.id)
    except KeyError:
        raise HTTPException(404, "portrait not found")
    return {"portraits": store.list_portraits(req.uid)}


@app.post("/api/portraits/rename")
def portraits_rename(req: PortraitRenameReq):
    _user_or_404(req.uid)
    if not req.id:
        raise HTTPException(400, "id is required")
    name = req.name.strip()[:24]
    try:
        store.rename_portrait(req.uid, req.id, name)
    except KeyError:
        raise HTTPException(404, "portrait not found")
    return {"ok": True, "name": name}


@app.delete("/api/portraits/{pid}")
def portraits_delete(pid: str, uid: str):
    _user_or_404(uid)
    try:
        reset = store.delete_portrait(uid, pid)
    except KeyError:
        raise HTTPException(404, "portrait not found")
    if reset:
        onboarding.start(uid)
    return {"portraits": store.list_portraits(uid)}


# ---------- directions ----------

@app.get("/api/directions/recommend")
def directions_recommend(uid: str):
    """按当前画像的兴趣给方向建议，不检索课程、不等模型改写。"""
    _user_or_404(uid)
    cards = planner.direction_cards(uid, with_courses=False, voice=False)
    if not cards:
        cards = planner.cards_from_ranking(uid)
    return {"cards": cards}


@app.get("/api/directions/ranking")
def directions_ranking(uid: str):
    """按树节点排方向。规则，不调模型。"""
    _user_or_404(uid)
    return {"items": planner.direction_ranking(uid)}


@app.post("/api/directions/cards")
def direction_cards(req: NBAReq):
    """带真实课程检索的推荐卡（课程 live，可能较慢，前端按卡懒加载时不用此聚合接口）。"""
    _user_or_404(req.uid)
    return {"cards": planner.direction_cards(req.uid, with_courses=True)}


@app.post("/api/directions/choose")
def choose_direction(req: ChooseDirectionReq):
    _user_or_404(req.uid)
    if req.code not in planner.DIRECTIONS:
        raise HTTPException(400, f"unknown direction: {req.code}")
    f = planner.choose_direction(req.uid, req.code)
    return {"fact": f.to_dict()}


# ---------- tasks / workbench ----------

@app.post("/api/tasks/generate")
def task_generate(req: TaskGenerateReq):
    _user_or_404(req.uid)
    if req.direction not in planner.DIRECTIONS:
        raise HTTPException(400, f"unknown direction: {req.direction}")
    t = workbench.generate_task(req.uid, req.direction, req.level, title=req.title, brief=req.brief)
    return t.to_dict()


@app.get("/api/tasks/{tid}")
def task_get(tid: str, request: Request):
    t = store.get_task(tid)
    if not t or t.user_id != auth.me(request):
        raise HTTPException(404, "task not found")
    return t.to_dict()


@app.get("/api/tasks")
def task_list(uid: str):
    _user_or_404(uid)
    return {"tasks": [t.to_dict() for t in store.list_tasks(uid)]}


@app.post("/api/tasks/{tid}/submit")
def task_submit(tid: str, req: SubmitReq):
    _user_or_404(req.uid)
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    # 归属校验：否则任何 uid 都能提交别人的任务并写进别人的画像（审计 P0）
    if t.user_id != req.uid:
        raise HTTPException(403, "task does not belong to this user")
    if len(req.payload.strip()) < 10:
        raise HTTPException(400, "提交内容太短，至少写一句话")
    fb = workbench.submit(req.uid, t, req.payload)
    return fb


@app.post("/api/tasks/{tid}/hint")
def task_hint(tid: str, req: TaskHintReq):
    _user_or_404(req.uid)
    t = store.get_task(tid)
    if not t or t.user_id != req.uid:
        raise HTTPException(404, "task not found")
    if t.status == "done":
        raise HTTPException(400, "已完成的这一步不用再要提示")
    try:
        return workbench.hint_task(req.uid, t, req.question, req.draft)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OverflowError as e:
        raise HTTPException(429, str(e))


class DraftReq(BaseModel):
    text: str = ""


class PrefsReq(BaseModel):
    kit_id: str | None = None
    directions: list[str] | None = None


class TrailReq(BaseModel):
    data: dict = {}


@app.get("/api/tasks/{tid}/draft")
def task_draft_get(tid: str, request: Request):
    uid = auth.me(request)
    t = store.get_task(tid)
    if not t or t.user_id != uid:
        raise HTTPException(404, "task not found")
    return store.get_draft_row(uid, tid)


@app.put("/api/tasks/{tid}/draft")
def task_draft_put(tid: str, req: DraftReq, request: Request):
    uid = auth.me(request)
    t = store.get_task(tid)
    if not t or t.user_id != uid:
        raise HTTPException(404, "task not found")
    if t.status == "done":
        raise HTTPException(400, "已完成的作业不用再改草稿")
    return store.set_draft_row(uid, tid, req.text)


def _prefs_out(raw: dict) -> dict:
    out = dict(raw or {})
    out.setdefault("kit_id", "")
    dirs = out.get("directions")
    out["directions"] = dirs if isinstance(dirs, list) else []
    return out


@app.get("/api/me/prefs")
def prefs_get(request: Request):
    return _prefs_out(store.get_prefs(auth.me(request)))


@app.put("/api/me/prefs")
def prefs_put(req: PrefsReq, request: Request):
    extra = None
    if req.directions is not None:
        import interests
        nodes = set(interests.table()["nodes"])
        seen: list[str] = []
        for n in req.directions:
            nid = str(n or "").strip()
            if nid in nodes and nid not in seen:
                seen.append(nid)
            if len(seen) >= 8:
                break
        extra = {"directions": seen}
    if req.kit_id:
        try:
            reading.kit(req.kit_id)
        except reading.ReadingError as exc:
            raise HTTPException(400, str(exc)) from exc
    return _prefs_out(store.set_prefs(auth.me(request), kit_id=req.kit_id, extra=extra))


@app.get("/api/projects/{pid}/trail")
def trail_get(pid: str, request: Request):
    return store.get_trail(auth.me(request), pid)


@app.put("/api/projects/{pid}/trail")
def trail_put(pid: str, req: TrailReq, request: Request):
    data = req.data if isinstance(req.data, dict) else {}
    return store.set_trail(auth.me(request), pid, data)


# ---------- feedback（用户意见反馈） ----------

class FeedbackReq(BaseModel):
    uid: str
    text: str = ""


@app.post("/api/feedback")
def feedback_post(req: FeedbackReq):
    _user_or_404(req.uid)
    text = (req.text or "").strip()
    if len(text) < 10:
        raise HTTPException(400, "至少写 10 个字，说清哪里不对")
    if len(text) > 4000:
        raise HTTPException(400, "太长了（最多 4000 字），拆成几条分开提")
    item = store.add_feedback(req.uid, text)
    store.add_event(req.uid, "feedback_sent", "", {"id": item["id"]})
    return {"ok": True, "item": item}


@app.get("/api/feedback")
def feedback_list(uid: str):
    _user_or_404(uid)
    return {"items": store.list_feedback(uid)}


# ---------- me（AI 认识的我） ----------

@app.get("/api/me/facts")
def me_facts(uid: str):
    _user_or_404(uid)
    return {"facts": [f.to_dict() for f in store.list_facts(uid)]}


@app.get("/api/me/facts/{fid}")
def me_fact(fid: str, uid: str):
    _user_or_404(uid)
    f = store.get_fact(fid)
    if not f or f.user_id != uid:
        raise HTTPException(404, "fact not found")
    return f.to_dict()


@app.patch("/api/me/facts/{fid}")
def me_fact_patch(fid: str, req: FactPatchReq):
    """用户自己改一条记忆。

    走 memory.user_edit 而不是裸 update_fact，是为了拿到三件必须的事：
    来源升级为 user_edit（用户关于自己的话是最高可信，1.0）、留 revision、memory_version+1。
    少了这些，用户改完的记忆和模型推断的在库里长得一样，也没法追溯是谁改的。
    """
    f = store.get_fact(fid)
    if not f or f.user_id != req.uid:
        raise HTTPException(404, "fact not found")
    if req.value is None and req.status is None:
        raise HTTPException(400, "nothing to update")
    if req.value is not None:
        updated = memory.user_edit(req.uid, fid, req.value)
        if not updated:
            raise HTTPException(400, "value is required")
        return updated
    f2 = store.update_fact(fid, status=req.status)
    return f2.to_dict() if f2 else {}


@app.delete("/api/me/facts/{fid}")
def me_fact_delete(fid: str, uid: str):
    """用户删掉一条记忆。不硬删：置为 retracted 并留 revision，可追溯。"""
    f = store.get_fact(fid)
    if not f or f.user_id != uid:
        raise HTTPException(404, "fact not found")
    memory.user_retract(uid, fid)
    return {"ok": True, "memory_version": store.get_memory_version(uid)}


# ---------- explore（真实课程检索透传） ----------

@app.get("/api/explore/courses")
async def explore_courses(query: str, limit: int = 5, term: str = ""):
    """相同的课程检索在事件循环里共等一个结果；原来跟随者各占一个工作线程干等。"""
    if not query.strip():
        raise HTTPException(400, "query is required")
    key = ("courses", query.strip(), min(10, max(1, limit)), term.strip())
    return await _shared(key, search_courses, query, limit, term)


@app.get("/api/explore/teachers")
def explore_teacher(name: str):
    if not name.strip():
        raise HTTPException(400, "name is required")
    return catalog.teacher_payload(name.strip())


# ---------- 院系-专业知识库（培养方案）：先查表，再让模型说话 ----------
# 数据是离线抽好的（文理两卷 + 辅修双专业），这里只查表：不联网、不调模型。
# 用户说「我是经济学院的」这类问题，答案必须来自这些接口，而不是模型回忆。

class MatchReq(BaseModel):
    codes: list[str] = []
    low_only: bool = False        # 只看大一/大二必修（低年级成绩单更稳）
    top: int = 5


@app.get("/api/explore/majors")
def explore_majors(q: str = "", minor: bool = False, limit: int = 8):
    """按用户说法查专业；一个名字对上多个就返回专业簇。"""
    return curriculum.find_major(q, minor=minor, limit=min(30, max(1, limit)))


@app.get("/api/explore/major")
def explore_major(name: str):
    """单个专业：卡片 + 学分结构 + 必修课清单 + 同院兄弟专业（低年级认不出细分专业时要用）。"""
    if not name.strip():
        raise HTTPException(400, "name is required")
    return curriculum.major_detail(name.strip())


@app.post("/api/curriculum/match")
def curriculum_match(req: MatchReq):
    """成绩单课号 → 院系排名 + 专业排名 + 还缺哪几门必修。"""
    return curriculum.match(req.codes, low_only=req.low_only, top=min(20, max(1, req.top)))


@app.get("/api/explore/minor")
def explore_minor(q: str = "", dept: str = "", limit: int = 20):
    """辅修 / 双专业：学分量、核心课程、替代课程（主修修过同名课时改修这些）。"""
    return curriculum.find_minor(q, dept, top=min(60, max(1, limit)))


@app.get("/api/explore/course")
def explore_course(code: str):
    """课号反查：这门课是什么、哪些专业必修它、课号前缀属于哪个院系。"""
    if not code.strip():
        raise HTTPException(400, "code is required")
    return curriculum.course_detail(code.strip())


@app.get("/api/curriculum/stats")
def curriculum_stats():
    """知识库健康检查：现在库里有多少专业/方案/课程。"""
    return curriculum.stats()


# ---------- 同一个知识库的 MCP 服务（streamable-http） ----------
# 工具表在 server/mcp_curriculum.py，stdio 和 http 两种传输共用一套工具：
#   · DSH 的 dsh-mcp-client 配置见 docs/DEPARTMENT_KNOWLEDGE.md §4.4
#   · stdio：python server/mcp_curriculum.py

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    try:
        payload = await request.json()
    except Exception:                                          # noqa: BLE001
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}}, status_code=200)
    resp = mcp_curriculum.http_handle(payload)
    if resp is None:                                            # 通知类消息：按 MCP 规范回 202
        return Response(status_code=202)
    return JSONResponse(resp)


@app.get("/mcp")
def mcp_info():
    """给人看的：这个地址是 MCP 的 streamable-http 端点，用 POST 发 JSON-RPC。"""
    return {"ok": True, "endpoint": "/mcp", "protocol": mcp_curriculum.PROTOCOL,
            "tools": [t["name"] for t in mcp_curriculum.list_tools()],
            "hint": "MCP streamable-http：POST JSON-RPC（initialize / tools/list / tools/call）"}


# ---------- 边学边练：项目检索 / 选定 / 交成果（任务 3） ----------

@app.get("/api/projects/sources")
def project_sources():
    reg = projects.registry()
    return {"generated_at": reg.get("generated_at"), "sources": [
        {k: s.get(k) for k in ("id", "name", "home_url", "kind", "directions", "stage_fit", "access", "cadence", "manual_route", "search_terms")}
        for s in reg["sources"]]}


@app.get("/api/paths")
def direction_paths():
    """任务 4 的方向路径（knowledge/paths.json），前端用它把方向树画成「6 步主干 + 原有节点」。"""
    return {"paths": projects.paths()}


@app.get("/api/projects/context")
def project_context(uid: str):
    _user_or_404(uid)
    return projects.context(uid)


@app.post("/api/projects/interview")
def project_interview(req: ProjectInterviewReq):
    _user_or_404(req.uid)
    try:
        return projects.interview(req.uid, req.session_id, req.answer, req.option_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0] if exc.args else "问答已过期，请重新开始")) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc.args[0] if exc.args else "不是你的问答")) from exc


@app.post("/api/projects/search")
def project_search(req: ProjectSearchReq):
    _user_or_404(req.uid)
    if req.direction and req.direction not in planner.DIRECTIONS:
        raise HTTPException(400, f"unknown direction: {req.direction}")
    return projects.search(req.uid, req.direction, req.stage, req.keywords, req.node, req.path_step, req.chain)


@app.post("/api/projects/pick")
def project_pick(req: ProjectPickReq):
    _user_or_404(req.uid)
    try:
        return projects.pick(req.uid, req.id)
    except KeyError as exc:
        raise HTTPException(409, str(exc.args[0])) from exc


@app.get("/api/projects/mine")
def project_mine(uid: str):
    _user_or_404(uid)
    return {"projects": [{k: v for k, v in p.items() if k != "reviews"} | {"reviews": [
        {"passed": r.get("passed"), "total": r.get("total"), "reviewed_at": r.get("reviewed_at")} for r in (p.get("reviews") or [])[:1]]}
        for p in store.list_projects(uid)]}


def _project_or_404(uid: str, pid: str) -> dict:
    _user_or_404(uid)
    p = store.get_project(uid, pid)
    if not p:
        raise HTTPException(404, "project not found")
    return p


@app.get("/api/projects/{pid}")
def project_get(pid: str, uid: str):
    return _project_or_404(uid, pid)


@app.get("/api/projects/{pid}/readme")
def project_readme(pid: str, uid: str):
    p = _project_or_404(uid, pid)
    return Response(projects.readme_template(p), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=README.md"})


@app.get("/api/projects/{pid}/sample.zip")
def project_sample(pid: str, uid: str):
    p = _project_or_404(uid, pid)
    return Response(projects.sample_zip(p), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=sample.zip"})


# 评阅（解压、规则检查、模型调用、写库）都是阻塞的：放进有上限的线程池，不占事件循环。
# 上传和评阅分开计数：传到一半卡住的连接只占上传名额，而且有总时限，不会把评阅名额占光。
REVIEW_WORKERS = 2
REVIEW_PENDING_MAX = 6      # 已收完、在排队或在评阅的；每个握着最多 20 MB
UPLOADS_MAX = 8             # 同时在上传的
UPLOAD_IDLE_SECONDS = 20    # 这么久一个字节都没收到，就当连接卡住了
UPLOAD_TOTAL_SECONDS = 300  # 总时限：20 MB 在 70 KB/s 的慢网上也传得完；只防一直吊着不传完的连接
_REVIEW_POOL = ThreadPoolExecutor(max_workers=REVIEW_WORKERS, thread_name_prefix="review")
_review_pending = 0  # 这两个计数只在事件循环线程里改，不用锁
_uploads = 0


async def _read_capped(request: Request, limit: int, too_big: str | None = None) -> bytes:
    """边收边数，超过上限立刻停（原来先把整个请求体读进内存再判断大小）。
    慢网照样能传完：只在「很久没收到数据」或「总时间太长」时断开，不按固定的几十秒一刀切。"""
    too_big = too_big or f"压缩包超过 {limit // 1024 // 1024} MB。大数据集请只放样例，并在 README 里写下载链接。"
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, too_big)
    buf = bytearray()
    chunks = request.stream().__aiter__()
    deadline = asyncio.get_running_loop().time() + UPLOAD_TOTAL_SECONDS
    while True:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            raise HTTPException(408, f"上传超过 {UPLOAD_TOTAL_SECONDS // 60} 分钟还没传完，已断开。请换个网络再交，或把压缩包弄小一点。")
        try:
            chunk = await asyncio.wait_for(chunks.__anext__(), min(UPLOAD_IDLE_SECONDS, left))
        except StopAsyncIteration:
            return bytes(buf)
        except asyncio.TimeoutError as exc:
            raise HTTPException(408, f"上传中断：{UPLOAD_IDLE_SECONDS} 秒没有收到数据。请检查网络后再交。") from exc
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, too_big)


def _review_and_record(uid: str, p: dict, data: bytes) -> dict:
    try:
        result = submission.review(data, p)
    except submission.SubmissionError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["fact"] = projects.record_review(uid, p, result)
    return result


@app.post("/api/projects/{pid}/submit")
async def project_submit(pid: str, uid: str, request: Request):
    """请求体就是 .zip 本身（Content-Type: application/zip），不需要 multipart 依赖。"""
    global _review_pending, _uploads
    p = await run_in_threadpool(_project_or_404, uid, pid)
    if _uploads >= UPLOADS_MAX or _review_pending >= REVIEW_PENDING_MAX:
        raise HTTPException(503, "现在交的人太多，评阅在排队。请过一两分钟再交。")
    _uploads += 1
    try:
        data = await _read_capped(request, submission.MAX_ZIP_BYTES)
    finally:
        _uploads -= 1
    if not data:
        raise HTTPException(400, "没有收到文件")
    if _review_pending >= REVIEW_PENDING_MAX:
        raise HTTPException(503, "现在交的人太多，评阅在排队。请过一两分钟再交。")
    _review_pending += 1
    try:
        return await asyncio.get_running_loop().run_in_executor(_REVIEW_POOL, _review_and_record, uid, p, data)
    finally:
        _review_pending -= 1


# ---------- 研读：领域工具包 / 每日情报 / 阅读卡 / 矩阵 ----------

def _reading(fn, *args):
    try:
        return fn(*args)
    except (reading.ReadingError, positioning.PositionError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except reading.arxiv.ArxivError as exc:
        raise HTTPException(400 if getattr(exc, "status", 0) == 400 else 502, str(exc)) from exc


# 共享的活按种类分开线程池：arXiv 要排限速锁（每次至少隔 3 秒），几篇不同的论文就能把线程全占在等锁上；
# 分开以后，等 arXiv 的不会拖住课程检索和建引文索引
_FLIGHTS = {
    "arxiv": AsyncFlight(ThreadPoolExecutor(max_workers=4, thread_name_prefix="arxiv")),
    "index": AsyncFlight(ThreadPoolExecutor(max_workers=2, thread_name_prefix="index")),
    "courses": AsyncFlight(ThreadPoolExecutor(max_workers=4, thread_name_prefix="courses")),
    "daily": AsyncFlight(ThreadPoolExecutor(max_workers=1, thread_name_prefix="daily")),
    "xlate": AsyncFlight(ThreadPoolExecutor(max_workers=1, thread_name_prefix="xlate")),
}
_FLIGHT_OF = {"paper": "arxiv", "daily": "daily", "index": "index", "courses": "courses", "xlate": "xlate", "search": "daily"}


async def _shared(key: tuple, fn, *args):
    """多人同时要同一份数据：只有一个线程去做，其余在事件循环里等结果，不各占一个工作线程。"""
    return await _FLIGHTS[_FLIGHT_OF[key[0]]].do(key, _reading, fn, *args)


def _payload_of(p: dict) -> dict:
    return {**p, "tables": p.get("tables") or [],
            "sections": [{"name": n, "label": reading.arxiv.section_cn(n), "at": at} for n, at in reading.arxiv.sections(p["text"])]}


def _paper_payload(aid: str) -> dict:
    return _payload_of(reading.arxiv.fulltext(aid))


def _paper_cached(aid: str) -> dict | None:
    p = reading.arxiv.cached(aid)
    return None if p is None else _payload_of(p)


def _paper_index(aid: str) -> None:
    """交卡之前先把这篇论文的引文索引和分节建好：并发交同一篇时，跟随者在事件循环里等，不在评阅线程里干等。"""
    text = reading.arxiv.fulltext(aid)["text"]
    quotes.prepare(text)
    reading.arxiv.sections(text)


_DAILY_MISS = TTLCache(30, 64)  # arXiv 取不到时，三十秒内的请求直接用这个失败结果，不再去打


def _daily_cached(kit_id: str) -> tuple[list, str] | None:
    hit, miss = _DAILY_MISS.lookup(kit_id)
    return miss if hit else reading.daily_source(kit_id, cached_only=True)


async def _daily_source_for(kit: str) -> tuple[list, str]:
    """缓存里有就直接给（普通线程池里读一下内存，几毫秒）；没有才进 arXiv 线程池排队去取。
    原来所有请求都先进 arXiv 线程池再查缓存：四个慢的取数占满线程时，缓存命中的也得等。"""
    got = await run_in_threadpool(_reading, _daily_cached, kit)
    return got if got is not None else await _shared(("daily", kit), _daily_source, kit)


def _daily_source(kit_id: str) -> tuple[list, str]:
    """共享的那一步：候选或失败原因，作为参数交给 daily，不让每个请求在自己的线程里再取一次。"""
    hit, miss = _DAILY_MISS.lookup(kit_id)
    if hit:
        return miss
    got = reading.daily_source(kit_id)
    if got[1]:
        _DAILY_MISS.set(kit_id, got)
    return got


@app.get("/api/kits")
def kits_list():
    return {"kits": [{k: v for k, v in kit.items() if k in ("id", "name", "version", "direction", "goal", "status")}
                     for kit in reading.kits().values()]}


@app.get("/api/kits/{kit_id}")
def kit_get(kit_id: str):
    return _reading(reading.kit, kit_id)


def _daily_payload(uid: str, kit: str, source: tuple[list, str]) -> dict:
    d = _reading(reading.daily, uid, kit, source)
    prefs_kit = store.get_prefs(uid).get("kit_id") or ""
    if not prefs_kit:
        return d
    today = [r for r in d["recent_keeps"] if r["created_at"][:10] == reading.now_iso()[:10]]
    return {**d, "tweak": _reading(positioning.daily_tweak, uid, prefs_kit, today)}


_TOPIC_GUARD = threading.Lock()
_TOPIC_LOCKS: dict[str, threading.Lock] = {}


def _topic_fetch(tid: str):
    hit, miss = _DAILY_MISS.lookup(tid)
    if hit:
        return miss
    with _TOPIC_GUARD:
        lock = _TOPIC_LOCKS.setdefault(tid, threading.Lock())
    with lock:
        hit, miss = _DAILY_MISS.lookup(tid)
        if hit:
            return miss
        got = reading.topic_source(tid, cached_only=False)
        if got is None or got[1]:
            _DAILY_MISS.set(tid, got if got is not None else ([], "取不到", False))
        _TOPIC_REFRESHED.add((tid, reading.arxiv_day()))
        return got


def _interest_prep(uid: str) -> dict:
    """要不要现场取话题池：目录够就不上游；昨日 stale 每天每话题最多刷 1 次。"""
    import interests
    ids, _ = interests.user_topics(uid)
    node = interests.direction_node(uid)
    catalog = reading.dir_as_papers(node) if node else []
    cutoff = reading._cutoff()
    catalog_recent = [p for p in catalog if (p.get("published") or "9999") >= cutoff]
    fetch_tid = None
    day = reading.arxiv_day()
    for tid in ids:
        got = reading.topic_source(tid, cached_only=True)
        empty = got is None
        stale = bool(got) and len(got) > 2 and got[2]
        if empty and len(catalog_recent) < 6:
            fetch_tid = tid
            break
        if stale and (tid, day) not in _TOPIC_REFRESHED and len(catalog_recent) < 6:
            fetch_tid = tid
            break
    return {"fetch_tid": fetch_tid}


def _interest_daily(uid: str, allow_fetch: bool = True) -> dict:
    import interests
    ids, _ = interests.user_topics(uid)
    node = interests.direction_node(uid)
    catalog = reading.dir_as_papers(node) if node else []
    cutoff = reading._cutoff()
    catalog_recent = [p for p in catalog if (p.get("published") or "9999") >= cutoff]
    sources = {}
    fetched = False
    need_fetch = len(catalog_recent) < 6
    day = reading.arxiv_day()
    for tid in ids:
        got = reading.topic_source(tid, cached_only=True)
        stale = bool(got) and len(got) > 2 and got[2]
        if allow_fetch and not fetched and need_fetch and (
            got is None or (stale and (tid, day) not in _TOPIC_REFRESHED)
        ):
            got = _topic_fetch(tid)
            fetched = True
        if got is not None:
            sources[tid] = got
    d = reading.daily_for_user(uid, sources)
    prefs_kit = store.get_prefs(uid).get("kit_id") or ""
    if prefs_kit:
        try:
            k = reading.kit(prefs_kit)
        except reading.ReadingError:
            k = None
        node = interests.direction_node(uid)
        code = (interests.table().get("legacy") or {})
        want = None
        if node:
            want = next((c for c, n in code.items() if n == node or node.startswith(str(n))), None)
        if k and (not want or k.get("direction") == want or k.get("direction") == node):
            try:
                today = [r for r in d["recent_keeps"] if r["created_at"][:10] == reading.now_iso()[:10]]
                d = {**d, "tweak": positioning.daily_tweak(uid, prefs_kit, today)}
            except Exception:
                pass
    return d


@app.get("/api/daily")
async def daily_feed(uid: str, kit: str = ""):
    await run_in_threadpool(_user_or_404, uid)
    if kit:
        source = await _daily_source_for(kit)
        return await run_in_threadpool(_daily_payload, uid, kit, source)
    prep = await run_in_threadpool(_interest_prep, uid)
    if prep.get("fetch_tid"):
        try:
            fut = _DAILY_POOL.submit(_topic_fetch, prep["fetch_tid"])
        except workpool.PoolFull:
            raise HTTPException(503, "今日正在取，稍后再试") from None
        await asyncio.wrap_future(fut)
    return await run_in_threadpool(_reading, _interest_daily, uid, False)


@app.post("/api/daily/triage")
async def daily_triage(req: TriageReq):
    await run_in_threadpool(_user_or_404, req.uid)
    if not req.kit:
        return await run_in_threadpool(_reading, reading.triage, req.uid, "", req.arxiv_id, req.verdict, req.why, req.title, None)
    source = await _daily_source_for(req.kit)
    return await run_in_threadpool(_reading, reading.triage, req.uid, req.kit, req.arxiv_id, req.verdict, req.why, req.title, source)


_PDF_POOL = workpool.BoundedPool(1, 4, "pdf")
_DAILY_POOL = workpool.BoundedPool(1, 16, "daily")
_TOPIC_REFRESHED: set[tuple[str, str]] = set()


@app.get("/api/papers/dir")
def papers_dir(uid: str, node: str = ""):
    """当前方向（或指定 node）的预存目录。不打 arXiv。"""
    _user_or_404(uid)
    import interests
    nid = (node or "").strip() or interests.direction_node(uid)
    if nid and nid not in interests.table()["nodes"]:
        raise HTTPException(400, "没有这个方向")
    rows = store.list_paper_dir(nid) if nid else []
    ids = [r["paper_id"] for r in rows]
    lib = store.paper_lib(ids)
    items = []
    for r in rows:
        zh = store.get_paper_zh(r["paper_id"]) or {}
        kit_zh, kit_kw = reading.kit_title_zh(r["paper_id"])
        kid = None
        for k in reading.kits().values():
            if reading.kit_paper(k, r["paper_id"]):
                kid = k.get("id")
                break
        items.append({
            **r, "id": r["paper_id"],
            "title_zh": (zh.get("title_zh") or "") or kit_zh,
            "keywords": zh.get("keywords") or kit_kw or [],
            "lib": lib.get(r["paper_id"], ""),
            "card_kit": kid or "",
        })
    return {"node": nid or "", "items": items}


@app.get("/api/papers/search")
async def papers_search(q: str, uid: str):
    await run_in_threadpool(_user_or_404, uid)
    if not auth._allow(f"search:{uid}", 30):
        raise HTTPException(429, "搜索太勤，过一会儿再试")
    if reading.arxiv.ZH_RE.search(q or ""):
        raise HTTPException(400, "arXiv 只认英文关键词")
    lib_items = await run_in_threadpool(reading.search_library, q, 20)
    got: list = []
    upstream_error = ""
    if len(lib_items) < 20:
        key = tuple((q or "").lower().split())
        try:
            got = await _shared(("search", key), reading.arxiv.search, q, 20 - len(lib_items))
        except Exception as exc:
            if not lib_items:
                raise HTTPException(502, str(exc) or "搜索上游失败") from exc
            upstream_error = str(exc)
    seen = {it.get("id") for it in lib_items}
    live_lib = store.paper_lib([p.get("id") for p in got or []])
    items = list(lib_items)
    for p in got or []:
        pid = p.get("id") or ""
        if pid in seen:
            continue
        seen.add(pid)
        items.append({
            **p, "card_kit": reading.card_kit_for(p), "lib": live_lib.get(pid, ""),
            "in_kit": any(reading.kit_paper(k, pid) for k in reading.kits().values()),
        })
    items = items[:20]
    out = {"items": items}
    if upstream_error:
        out["upstream_error"] = upstream_error
    return out


@app.get("/api/papers/mine")
def papers_mine(uid: str):
    _user_or_404(uid)
    return {"items": store.list_user_papers(uid)}


@app.post("/api/papers/upload")
async def papers_upload(uid: str, request: Request):
    await run_in_threadpool(_user_or_404, uid)
    if not auth._allow(f"pdf:{uid}", 10):
        raise HTTPException(429, "导入太勤，过一会儿再试")
    data = await _read_capped(request, 10 << 20, "PDF 超过 10 MB")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "不是 PDF")
    raw_name = request.headers.get("X-Filename") or "upload.pdf"
    filename = urllib.parse.unquote(raw_name)[:200]
    deadline = time.time() + 30
    try:
        fut = _PDF_POOL.submit(pdf_mod.extract_pdf, data, deadline)
        extracted = await asyncio.wait_for(asyncio.wrap_future(fut), 30)
    except workpool.PoolFull:
        raise HTTPException(503, "现在解析的人太多，过一会儿再试")
    except asyncio.TimeoutError:
        raise HTTPException(400, "解析超过 30 秒")
    except pdf_mod.PdfError as exc:
        raise HTTPException(400, str(exc)) from exc
    text = extracted["text"]
    pid = "local:" + hashlib.sha256((uid + text).encode("utf-8")).hexdigest()[:12]
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    title = extracted.get("title") or filename
    store.put_user_paper(uid, pid, sha, title, filename, text, extracted["pages"])
    return {"pid": pid, "title": title, "pages": extracted["pages"], "created_at": now_iso()}


@app.delete("/api/papers/{arxiv_id}")
def papers_delete(arxiv_id: str, uid: str):
    _user_or_404(uid)
    if not reading.arxiv.is_local(arxiv_id):
        raise HTTPException(400, "只能删自己导入的 PDF")
    if not store.get_user_paper(uid, arxiv_id):
        raise HTTPException(404, "没有这篇")
    store.delete_user_paper(uid, arxiv_id)
    return {"ok": True}


@app.get("/api/papers/{arxiv_id}")
async def paper_text(arxiv_id: str, request: Request):
    """论文正文（arXiv HTML 版，取不到则只有摘要）。只读、缓存。"""
    if reading.arxiv.is_local(arxiv_id):
        uid = auth.me(request)
        paper = store.get_user_paper(uid, arxiv_id)
        if not paper:
            raise HTTPException(404, "没有这篇")
        text = paper.get("text") or ""
        return {**paper, "lib_at": "user", "tables": [],
                "sections": [{"name": n, "label": n if n.startswith("第") else reading.arxiv.section_cn(n), "at": at}
                             for n, at in reading.arxiv.sections(text, strict=True)]}
    if osf.is_id(arxiv_id):
        aid = osf.clean_id(arxiv_id)
        row = store.get_paper_text(aid)
        if row and (row.get("text") or "") and row.get("source") in ("html", "pdf"):
            return {**row, "lib_at": "db", "tables": row.get("tables") or [],
                    "sections": [{"name": n, "label": reading.arxiv.section_cn(n), "at": at}
                                 for n, at in reading.arxiv.sections(row.get("text") or "")]}
        meta = store.get_paper_dir_item(aid) or {}
        summary = (row or {}).get("text") or meta.get("summary") or ""
        return {
            "id": aid, "title": (row or {}).get("title") or meta.get("title") or aid,
            "source": "abstract", "text": summary, "url": meta.get("url") or "",
            "tables": [], "lib_at": "db" if row else "",
            "title_zh": "", "keywords": [],
            "sections": [],
        }
    aid = _reading(reading.arxiv.clean_id, arxiv_id)
    got = await run_in_threadpool(_reading, _paper_cached, aid)
    if got is None:
        got = await _shared(("paper", aid), _paper_payload, aid)
    zh = store.get_paper_zh(aid)
    title_zh = (zh or {}).get("title_zh") or ""
    keywords = (zh or {}).get("keywords") or []
    if not title_zh:
        kit_zh, kit_kw = reading.kit_title_zh(aid)
        title_zh = kit_zh
        if not keywords:
            keywords = kit_kw
    return {**got, "lib_at": "db" if store.get_paper_text(aid) else "",
            "title_zh": title_zh, "keywords": keywords}


@app.get("/api/cards")
def cards_list(uid: str, kit: str):
    _user_or_404(uid)
    return {"cards": store.latest_cards(uid, kit), "fields": reading.CARD_FIELDS}


@app.get("/api/cards/{arxiv_id}/history")
def card_history(arxiv_id: str, uid: str, kit: str):
    _user_or_404(uid)
    return {"versions": store.card_history(uid, kit, arxiv_id)}


def _card_submit(req: CardReq) -> dict:
    _user_or_404(req.uid)
    return _reading(reading.submit_card, req.uid, req.kit, req.arxiv_id, req.fields, req.dims, req.decision_log)


def _tutor_call(fn, *args):
    try:
        return fn(*args)
    except tutor.TutorConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except tutor.TutorError as exc:
        raise HTTPException(400, str(exc)) from exc
    except reading.arxiv.ArxivError as exc:
        raise HTTPException(400 if getattr(exc, "status", 0) == 400 else 502, str(exc)) from exc


def _paper_ref(uid: str, raw: str) -> str:
    if reading.arxiv.is_local(raw):
        if not store.get_user_paper(uid, raw):
            raise tutor.TutorError("没有这篇")
        return raw
    if osf.is_id(raw):
        return osf.clean_id(raw)
    return reading.arxiv.clean_id(raw)


@app.get("/api/tutor/{arxiv_id}")
def tutor_get(arxiv_id: str, uid: str, kit: str):
    _user_or_404(uid)

    def _do() -> dict:
        aid = _paper_ref(uid, arxiv_id)
        sess = tutor.get(uid, kit, aid)
        in_kit = False
        try:
            in_kit = bool(reading.kit_paper(reading.kit(kit), aid))
        except Exception:
            in_kit = False
        return {"session": sess, "can_grade": llm.ready(uid), "in_kit": in_kit}

    return _tutor_call(_do)


@app.post("/api/tutor/{arxiv_id}/start")
async def tutor_start(arxiv_id: str, req: TutorStartReq):
    await run_in_threadpool(_user_or_404, req.uid)
    aid = _tutor_call(_paper_ref, req.uid, arxiv_id)
    if not reading.arxiv.is_local(aid):
        if await run_in_threadpool(_reading, reading.arxiv.cached, aid) is None:
            await _shared(("paper", aid), _paper_payload, aid)
        await _shared(("index", aid), _paper_index, aid)
    return await run_in_threadpool(_tutor_call, tutor.start, req.uid, req.kit, aid, req.restart)


@app.post("/api/tutor/{arxiv_id}/turn")
async def tutor_turn(arxiv_id: str, req: TutorTurnReq):
    await run_in_threadpool(_user_or_404, req.uid)
    aid = _tutor_call(_paper_ref, req.uid, arxiv_id)
    if not reading.arxiv.is_local(aid):
        if await run_in_threadpool(_reading, reading.arxiv.cached, aid) is None:
            await _shared(("paper", aid), _paper_payload, aid)
        await _shared(("index", aid), _paper_index, aid)
    return await run_in_threadpool(
        _tutor_call, tutor.turn, req.uid, req.kit, aid, req.qid, req.answer, req.skip, req.rev, req.regrade)


@app.post("/api/tutor/{arxiv_id}/finish")
async def tutor_finish(arxiv_id: str, req: TutorFinishReq):
    await run_in_threadpool(_user_or_404, req.uid)
    aid = _tutor_call(_paper_ref, req.uid, arxiv_id)
    return await run_in_threadpool(_tutor_call, tutor.finish, req.uid, req.kit, aid)


@app.post("/api/cards")
async def card_submit(req: CardReq):
    aid = _reading(reading.arxiv.clean_id, req.arxiv_id)
    await run_in_threadpool(_user_or_404, req.uid)
    if await run_in_threadpool(_reading, reading.arxiv.cached, aid) is None:
        await _shared(("paper", aid), _paper_payload, aid)  # 缓存里没有才去取；并发提交同一篇只取一次
    await _shared(("index", aid), _paper_index, aid)    # 再把引文索引建好；之后每张卡的核对都直接命中
    return await run_in_threadpool(_card_submit, req)


@app.get("/api/matrix")
def matrix_get(uid: str, kit: str):
    _user_or_404(uid)
    return _reading(reading.matrix, uid, kit)


@app.get("/api/brief")
def agent_brief(kit: str, arxiv_id: str):
    text = _reading(reading.brief, kit, arxiv_id)
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=AGENTS.md"})


# ---------- 定位：边清单、竞争地图、定位陈述、下注组合 ----------

@app.get("/api/edges")
def edges_list(uid: str):
    _user_or_404(uid)
    return positioning.edges(uid)


@app.post("/api/edges")
def edges_add(req: EdgeReq):
    _user_or_404(req.uid)
    return _reading(positioning.add_edge, req.uid, req.kind, req.text, req.evidence_url)


@app.delete("/api/edges/{edge_id}")
def edges_delete(edge_id: str, uid: str):
    _user_or_404(uid)
    return _reading(positioning.delete_edge, uid, edge_id)


@app.get("/api/channels")
def channels_map(uid: str, direction: str):
    """这个方向的人在哪说话：信息源地图（knowledge/channels.json），标出学生常看的和盲区。"""
    _user_or_404(uid)
    return positioning.channel_map(uid, direction)


@app.post("/api/channels/toggle")
def channels_toggle(req: ChannelReq):
    _user_or_404(req.uid)
    return _reading(positioning.toggle_channel, req.uid, req.id, req.on, req.direction)


@app.get("/api/map")
def competition_map(uid: str, kit: str):
    _user_or_404(uid)
    return _reading(positioning.competition_map, uid, kit)


@app.get("/api/statement")
def statement_get(uid: str, kit: str):
    _user_or_404(uid)
    return _reading(positioning.statement, uid, kit)


@app.post("/api/statement")
def statement_save(req: StatementReq):
    """dry_run=true 只跑检查不保存，给编辑时实时提示用。"""
    _user_or_404(req.uid)
    if req.dry_run:
        return _reading(positioning.review_statement, req.uid, req.kit, req.x_ref, req.x_text, req.y)
    return _reading(positioning.save_statement, req.uid, req.kit, req.x_ref, req.x_text, req.y)


@app.get("/api/bets")
def bets_list(uid: str):
    _user_or_404(uid)
    return positioning.bets(uid)


@app.post("/api/bets")
def bets_add(req: BetReq):
    _user_or_404(req.uid)
    return _reading(positioning.add_bet, req.uid, req.name, req.kind, req.tier, req.kit, req.niche)


@app.post("/api/bets/{bet_id}/close")
def bets_close(bet_id: str, req: BetCloseReq):
    _user_or_404(req.uid)
    return _reading(positioning.close_bet, req.uid, bet_id, req.outcome, req.reason)


def _may_configure(request: Request) -> bool:
    """改服务器的模型设置（密钥、地址）只许管理员：设了 ADMIN_TOKEN 就核对请求头；没设就只许本机。
    原来任何访客都能改，等于能换掉团队的密钥，或把所有请求转到任意 https 地址。"""
    token = os.environ.get("ADMIN_TOKEN", "")
    if token:
        return hmac.compare_digest(request.headers.get("X-Admin-Token", ""), token)
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1", "localhost")


@app.post("/api/llm/connect")
def llm_connect(req: LlmConnectReq, request: Request):
    if not _may_configure(request):
        raise HTTPException(403, "只有在服务器本机，或带管理员口令，才能改模型设置")
    override = _mine_override(req)
    probe = llm.probe(override=override)
    if not probe.get("ok"):
        raise HTTPException(502, probe.get("error") or "模型连通失败")
    try:
        cfg = llm.apply_config(req.base_url, req.api_key, req.model, persist=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "model": cfg["model"], "base_url": cfg["base_url"]}


def _mine_override(req: LlmConnectReq) -> dict:
    base = (req.base_url or "https://api.deepseek.com/v1").strip().rstrip("/")
    key = (req.api_key or "").strip()
    model = (req.model or "deepseek-flash").strip()
    if not key:
        raise HTTPException(400, "API key is required")
    if not base.startswith("https://"):
        raise HTTPException(400, "base_url must start with https://")
    return {"api_key": key, "base_url": base, "model": model}


@app.get("/api/llm/mine")
def llm_mine_get(request: Request):
    return store.llm_secret_meta(auth.me(request))


@app.post("/api/llm/mine")
def llm_mine_set(req: LlmConnectReq, request: Request):
    uid = auth.me(request)
    if auth.account(uid)["guest"]:
        raise HTTPException(403, "先注册一个账号，再保存自己的密钥")
    cfg = _mine_override(req)
    probe = llm.probe(override=cfg)
    if not probe.get("ok"):
        raise HTTPException(400, probe.get("error") or "模型连通失败")
    meta = store.save_llm_secret(uid, cfg["api_key"], cfg["base_url"], cfg["model"])
    return {"ok": True, **meta}


@app.delete("/api/llm/mine")
def llm_mine_del(request: Request):
    store.delete_llm_secret(auth.me(request))
    return {"ok": True, "has_key": False}


def _build_stamp() -> str:
    for cand in (Path(__file__).resolve().parent.parent / "BUILD", Path(__file__).resolve().parent / "BUILD"):
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    return ""


def _wechat_secure() -> bool:
    try:
        import wechat
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return bool(os.environ.get("WECHAT_AES_KEY") and os.environ.get("WECHAT_APP_ID") and wechat.configured())
    except Exception:
        return False


def _metrics_allowed(request: Request) -> bool:
    token = os.environ.get("METRICS_TOKEN") or ""
    if token:
        authz = request.headers.get("Authorization") or ""
        got = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
        q = request.query_params.get("token") or ""
        return hmac.compare_digest(got, token) or hmac.compare_digest(q, token)
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


@app.get("/metrics")
def prometheus_metrics(request: Request):
    if not _metrics_allowed(request):
        raise HTTPException(401, "metrics 需要本机或 METRICS_TOKEN")
    import metrics as _metrics
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_metrics.render(), media_type="text/plain; version=0.0.4")


def _model_pool() -> dict:
    pdf = _PDF_POOL.snapshot()
    borrowed, cap = 0, THREADS
    try:
        lim = _LIMITER or anyio.to_thread.current_default_thread_limiter()
        borrowed = int(getattr(lim, "borrowed_tokens", 0) or 0)
        cap = int(getattr(lim, "total_tokens", THREADS) or THREADS)
    except Exception:
        pass
    return {
        "queued": pdf["queued"],
        "running": pdf["running"] + borrowed,
        "max_queued": pdf["max_queued"],
        "threads_borrowed": borrowed,
        "threads_max": cap,
        "pdf_queued": pdf["queued"],
        "pdf_running": pdf["running"],
    }


@app.get("/api/health")
async def health():
    cfg = llm.config()
    return {
        "ok": True,
        "service": "research-mentor",
        "version": "0.2.0",
        "llm": {
            "enabled": cfg["enabled"],
            "model": cfg["model"] if cfg["enabled"] else "",
            "base_url": cfg["base_url"] if cfg["enabled"] else "",
        },
        "auth": auth.status(),
        "db": {"ephemeral": store.ephemeral()},  # true 就是库会随实例一起没：上线前必须是 false
        "threads": THREADS,
        "model_pool": _model_pool(),
        "checks": {
            "secret_set": bool(os.environ.get("QIYAN_SECRET")),
            "wechat_secure": _wechat_secure(),
            "build": _build_stamp(),
        },
    }


# ---------- 静态前端 ----------

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(
        str(WEB_DIR / "index.html"),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": "inline"},
    )


if __name__ == "__main__":
    import uvicorn
    print("启研 W0 Demo: http://127.0.0.1:8100/")
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="info")
