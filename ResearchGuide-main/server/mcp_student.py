# -*- coding: utf-8 -*-
"""人工学生：让任何 AI agent（Codex / Claude / DSH / 我们自己的 DeepSeek 跑法）像学生一样用启研。

两种传输，工具表只有一份：
  · stdio            `python server/mcp_student.py --base http://127.0.0.1:8100 --token <AGENT_TOKENS 里的令牌>`
                     给 Codex / Claude Desktop / Cursor / DSH 当本地 MCP 服务，自己通过 HTTP 调用正在跑的 API
  · streamable-http  `POST <API>/mcp/student`（Authorization: Bearer <令牌>），服务端要设 QIYAN_SELF_URL
                     工具层仍然通过回环地址调公开接口：限频、预算、每人一把锁都和真人一样

设计纪律：
  · 工具是「学生能做的事」（看屏幕、回一句、按一个按钮、打开第几篇），不是接口清单；agent 看到的文字就是页面上的文案。
  · agent 不知道怎么办时必须调 stuck(原因)：这是产品最值钱的信号，按屏幕聚合在 /api/admin/agent-runs。
  · 每个账号都是 /api/agent/register 开的，带 operator / 人设 / 模型；metrics 的 qiyan_users{kind="synthetic"} 和
    qiyan_agent_* 把它们和真人分开，预算另有 LLM_DAILY_AGENT 按 operator 合计封顶。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROTOCOL = "2025-06-18"
SERVER = {"name": "researchguide-student", "version": "0.1.0"}
NAV = ["今日", "研读", "项目", "我的", "对话"]
SESSION_TTL = 6 * 3600


# ---------------- operator 令牌 ----------------

def _tokens() -> dict[str, str]:
    """AGENT_TOKENS="组名:令牌,组名2:令牌2" → {令牌: 组名}。"""
    out: dict[str, str] = {}
    for part in (os.environ.get("AGENT_TOKENS") or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        name, tok = part.split(":", 1)
        name, tok = name.strip(), tok.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name) and len(tok) >= 8:
            out[tok] = name
    return out


def operator_for(token: str) -> str:
    return _tokens().get(token or "", "")


def token_for(operator: str) -> str:
    return next((t for t, n in _tokens().items() if n == operator), "")


# ---------------- HTTP 传输 ----------------

class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class Api:
    """对正在跑的 API 的最小客户端。所有请求带 X-Qiyan-Agent 头，metrics 把它们另计。"""

    def __init__(self, base: str, operator_token: str, operator: str = "", timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.operator_token = operator_token
        self.operator = operator or operator_for(operator_token) or "agent"
        self.timeout = timeout
        self.prefer_stream = True

    def _headers(self, token: str | None, content_type: str | None) -> dict[str, str]:
        h = {"X-Qiyan-Agent": self.operator}
        if content_type:
            h["Content-Type"] = content_type
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def call(self, method: str, path: str, body: Any = None, token: str | None = None,
             raw: bytes | None = None, content_type: str | None = None, timeout: float | None = None) -> Any:
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        ctype = content_type or ("application/json" if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=self._headers(token, ctype))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
                ct = resp.headers.get("content-type") or ""
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(text).get("detail") or text
            except Exception:
                detail = text
            raise ApiError(e.code, str(detail)[:300])
        except urllib.error.URLError as e:
            raise ApiError(0, f"连不上服务器：{e.reason}")
        if "json" in ct:
            return json.loads(payload.decode("utf-8") or "null")
        return payload

    def sse(self, path: str, body: Any, token: str, timeout: float = 180.0) -> dict:
        """POST 一个 SSE 流，返回 event: result 的 data；event: error 抛 ApiError。"""
        req = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body).encode("utf-8"),
                                     headers=self._headers(token, "application/json"))
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")[:300])
        event, result = "", None
        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event == "result":
                        result = json.loads(data)
                    elif event == "error":
                        try:
                            msg = json.loads(data).get("error") or data
                        except Exception:
                            msg = data
                        raise ApiError(500, str(msg)[:300])
        if result is None:
            raise ApiError(0, "流结束了但没有结果")
        return result


# ---------------- 学生状态与屏幕 ----------------

class Student:
    def __init__(self) -> None:
        self.uid = ""
        self.token = ""
        self.run_id = ""
        self.persona: dict = {}
        self.plan: list[dict] = []
        self.done: list[str] = []
        self.screen = "login"
        self.title = ""
        self.body = ""
        self.buttons: list[tuple[str, Callable[[], str]]] = []
        self.input_hint = ""
        self.conversation_id: str | None = None
        self.onboard_done = False
        self.guide: dict = {}
        self.entered = False
        self.turns = 0
        self.papers: list[dict] = []
        self.paper: dict = {}
        self.kit = ""
        self.tutor: dict = {}
        self.direction = ""
        self.projects: list[dict] = []
        self.project: dict = {}
        self.interview: dict = {}
        self.llm_enabled = False
        self.steps = 0
        self.stuck = 0
        self.ended = False
        self.touched = time.time()


class Tools:
    """一个 agent 的一位学生。每个工具：做事 → 更新屏幕 → 返回屏幕文字。"""

    def __init__(self, api: Api):
        self.api = api
        self.s = Student()

    # ---- 对外工具 ----

    def start_as_student(self, persona: str = "", agent_model: str = "") -> dict:
        if self.s.uid and not self.s.ended:
            return self._out("你已经在线了，先 end_day 再换人。")
        r = self.api.call("POST", "/api/agent/register", {"persona": persona or "", "agent_model": agent_model or ""},
                          token=self.api.operator_token)
        s = Student()
        s.uid, s.token, s.run_id = r["uid"], r["token"], r["run_id"]
        s.persona, s.plan = r.get("persona") or {}, r.get("plan") or []
        self.s = s
        try:
            h = self.api.call("GET", "/api/health")
            s.llm_enabled = bool((h.get("llm") or {}).get("enabled")) and int(r.get("llm_left") or 0) > 0
        except ApiError:
            s.llm_enabled = False
        self._dialogue_screen(initial=True)
        self._event("step", "start_as_student")
        card = s.persona
        intro = (f"你现在是「{card.get('name', '')}」。\n{card.get('card', '')}\n"
                 f"你会的：{card.get('knowledge', '')}\n你的风格：{card.get('style', '')}\n"
                 f"今天想做到：{card.get('goal', '')}\n"
                 f"耐心：{card.get('patience', 5)}（每遇到一个看不懂的页面减一，到 0 就 end_day）\n"
                 "规则：只根据屏幕上的文字行动；不知道该做什么就调 stuck 说明原因；今天的目标做完就 end_day。")
        return self._out(intro)

    def look(self) -> dict:
        self._need_student()
        self._event("step", "look")
        return self._out()

    def reply(self, text: str) -> dict:
        self._need_student()
        text = (text or "").strip()
        if not text:
            return self._out("你什么也没说。")
        s = self.s
        self._event("step", f"reply@{s.screen}:{text[:80]}")
        if s.screen == "dialogue":
            return self._say_in_dialogue(text)
        if s.screen == "tutor":
            return self.answer_tutor(text)
        if s.screen == "projects" and s.interview.get("session_id") and not s.interview.get("done"):
            return self._interview(answer=text)
        return self._out("这个页面没有输入框，可以按下面的按钮。")

    def press(self, label: str) -> dict:
        self._need_student()
        want = _norm(label)
        self._event("step", f"press@{self.s.screen}:{label[:40]}")
        if not want:
            return self._out("press 要带 label（按钮上的文字）。")
        for lab, fn in self.s.buttons:
            if _norm(lab) == want or (want and _norm(lab).startswith(want) and len(want) >= 2):
                return self._guard(fn)
        if want in [_norm(n) for n in NAV]:
            return self._guard(lambda: self._nav(label))
        return self._out(f"屏幕上没有「{label}」这个按钮。")

    def open_paper(self, n: int = 1) -> dict:
        self._need_student()
        self._event("step", f"open_paper:{n}")
        return self._guard(lambda: self._open_paper(int(n)))

    def start_tutor(self) -> dict:
        self._need_student()
        self._event("step", "start_tutor")
        return self._guard(self._start_tutor)

    def answer_tutor(self, text: str = "", skip: bool = False) -> dict:
        self._need_student()
        self._event("step", f"answer_tutor:{(text or '')[:60]}{' skip' if skip else ''}")
        return self._guard(lambda: self._answer_tutor(text or "", bool(skip)))

    def finish_tutor(self) -> dict:
        self._need_student()
        self._event("step", "finish_tutor")
        return self._guard(self._finish_tutor)

    def choose_direction(self, code: str) -> dict:
        self._need_student()
        self._event("step", f"choose_direction:{code}")
        return self._guard(lambda: self._choose_direction(code))

    def search_projects(self, keywords: str = "") -> dict:
        self._need_student()
        self._event("step", f"search_projects:{keywords[:40]}")
        return self._guard(lambda: self._search_projects(keywords))

    def pick_project(self, n: int = 1) -> dict:
        self._need_student()
        self._event("step", f"pick_project:{n}")
        return self._guard(lambda: self._pick_project(int(n)))

    def submit_project(self) -> dict:
        self._need_student()
        self._event("step", "submit_project")
        return self._guard(self._submit_project)

    def stuck(self, reason: str) -> dict:
        self._need_student()
        self.s.stuck += 1
        self._event("stuck", (reason or "")[:500])
        return self._out("记下了。再看一遍屏幕上的按钮和文字；实在不行就 end_day。")

    def end_day(self, summary: str = "") -> dict:
        self._need_student()
        s = self.s
        r = self.api.call("POST", "/api/agent/run/end", {"uid": s.uid, "run_id": s.run_id, "summary": summary or ""}, token=s.token)
        s.ended = True
        run = r.get("run") or {}
        return {"ok": True, "text": f"今天结束。做完的目标：{run.get('done') or []}；步数 {run.get('steps')}；卡住 {run.get('stuck')} 次。",
                "run": run}

    # ---- 屏幕 ----

    def _out(self, note: str = "") -> dict:
        s = self.s
        s.touched = time.time()
        lines = []
        if note:
            lines.append(note)
        lines.append(f"【屏幕：{s.title or s.screen}】")
        if s.body:
            lines.append(s.body)
        if s.input_hint:
            lines.append(f"（输入框：{s.input_hint}）")
        if s.buttons:
            lines.append("可以按的：" + " ".join(f"[{lab}]" for lab, _ in s.buttons))
        if s.plan:
            todo = [p for p in s.plan if p["id"] not in s.done]
            if todo:
                lines.append(f"你的目标（{len(s.done)}/{len(s.plan)} 已完成）：下一项 → {todo[0]['text']}")
            else:
                lines.append("今天的目标都做完了，可以 end_day。")
        return {"ok": True, "screen": s.screen, "text": "\n".join(lines), "done": list(s.done)}

    def _guard(self, fn: Callable[[], Any]) -> dict:
        try:
            note = fn() or ""
        except ApiError as exc:
            note = f"页面提示：{exc.detail}" if exc.status else f"出错了：{exc.detail}"
        return self._out(note)

    def _nav(self, label: str) -> str:
        want = _norm(label)
        if want == "今日":
            return self._today_screen()
        if want == "研读":
            return self._read_screen()
        if want == "项目":
            return self._projects_screen()
        if want == "我的":
            return self._me_screen()
        if want == "对话":
            self._dialogue_screen()
            return ""
        return f"没有「{label}」这个入口。"

    def _nav_buttons(self, extra: list[tuple[str, Callable[[], str]]] | None = None) -> list[tuple[str, Callable[[], str]]]:
        s = self.s
        out = list(extra or [])
        if s.entered:
            for n in NAV:
                if _norm(n) != _norm(s.title):
                    out.append((n, (lambda lab=n: self._nav(lab))))
        return out

    # 对话 / 首次引导
    def _dialogue_screen(self, initial: bool = False) -> None:
        s = self.s
        h = self.api.call("GET", f"/api/dialogue/history?uid={s.uid}", token=s.token)
        s.guide = h.get("guide") or {}
        s.onboard_done = bool(h.get("onboard_done"))
        msgs = h.get("messages") or []
        last = [m for m in msgs if m.get("role") == "assistant"][-1:] if msgs else []
        text = (last[0]["text"] if last else "") or h.get("welcome_message") or ""
        s.screen, s.title = "dialogue", "对话"
        s.body = text
        s.input_hint = "像和人聊天一样回一句话"
        self._dialogue_buttons([])

    def _dialogue_buttons(self, options: list[str]) -> None:
        s = self.s
        btns: list[tuple[str, Callable[[], str]]] = [(o, (lambda opt=o: self._say_in_dialogue(opt, as_note=True))) for o in options[:6]]
        if not s.entered and s.turns >= 1:
            ready = bool(s.guide.get("ready")) or s.onboard_done
            btns.append(("进入启研" if ready else "先进入启研（引导还没完，也可以先进）", self._enter))
        s.buttons = self._nav_buttons(btns)

    def _say_in_dialogue(self, text: str, as_note: bool = False) -> Any:
        s = self.s
        reply, options = "", []
        if not s.onboard_done:
            try:
                r = self.api.call("POST", "/api/onboard/message", {"uid": s.uid, "msg": text}, token=s.token, timeout=180)
            except ApiError as exc:
                if exc.status == 400 and "already done" in exc.detail:
                    s.onboard_done = True
                    r = self._turn(text)
                else:
                    raise
        else:
            r = self._turn(text)
        reply = str(r.get("reply") or "")
        options = [str(o) for o in (r.get("options") or []) if o]
        d = r.get("dialogue") if isinstance(r.get("dialogue"), dict) else {}
        for a in (r.get("offered_actions") or d.get("offered_actions") or []):
            lab = a.get("title") if isinstance(a, dict) else str(a)
            if lab and lab not in options:
                options.append(str(lab)[:40])
        if r.get("guide"):
            s.guide = r["guide"]
        if "onboard_done" in r:
            s.onboard_done = bool(r["onboard_done"])
        elif r.get("done"):
            s.onboard_done = True
        s.turns += 1
        s.screen, s.title, s.body = "dialogue", "对话", reply
        areas = [a for a in (s.guide.get("areas") or []) if isinstance(a, dict) and not a.get("known")]
        if areas and not s.guide.get("ready") and not s.onboard_done:
            s.body += "\n（引导还想了解：" + "；".join(f"{a.get('label')}（{a.get('hint')}）" if a.get("hint") else str(a.get("label")) for a in areas[:4]) + "）"
        s.input_hint = "像和人聊天一样回一句话"
        self._dialogue_buttons(options)
        if (s.guide.get("ready") or s.onboard_done) and "guide" not in s.done:
            self._mark_done("guide")
        note = f"你说：{text}" if as_note else ""
        return note if as_note else self._out()

    def _turn(self, text: str) -> dict:
        s = self.s
        body = {"uid": s.uid, "message": text, "conversation_id": s.conversation_id}
        if self.api.prefer_stream and s.llm_enabled:
            try:
                r = self.api.sse("/api/dialogue/stream", body, s.token)
            except ApiError as exc:
                if exc.status in (503, 0, 500):
                    r = self.api.call("POST", "/api/dialogue/turn", body, token=s.token, timeout=180)
                else:
                    raise
        else:
            r = self.api.call("POST", "/api/dialogue/turn", body, token=s.token, timeout=180)
        if r.get("conversation_id"):
            s.conversation_id = r["conversation_id"]
        return r

    def _direction_choices(self, lines: list[str], btns: list) -> None:
        """「还没有方向」时把能选的方向列出来：先用推荐卡（卡片里方向在 card["direction"]），没有就列全部方向。"""
        s = self.s
        found: list[tuple[str, str, str]] = []
        try:
            cards = (self.api.call("GET", f"/api/directions/recommend?uid={s.uid}", token=s.token) or {}).get("cards") or []
        except ApiError:
            cards = []
        for c in cards[:6]:
            d = c.get("direction") if isinstance(c.get("direction"), dict) else c
            code = str(d.get("code") or d.get("id") or "")
            if code:
                found.append((code, str(d.get("name") or d.get("title") or code), str(c.get("why_you") or d.get("blurb") or "")[:60]))
        rest: list[tuple[str, str, str]] = []
        try:
            import planner
            have = {c for c, _, _ in found}
            rest = [(k, v["name"], "") for k, v in planner.DIRECTIONS.items() if k not in have]
        except Exception:
            rest = []
        if found or rest:
            lines.append("还没有方向，先选一个：")
            for code, name, why in found:
                lines.append(f"· {name}（{code}）{('：' + why) if why else ''}（按你说过的推荐）")
                btns.append((f"选方向：{code}", (lambda cd=code: self._choose_direction(cd))))
            if rest:
                lines.append("其它方向：" + "、".join(f"{name}（{code}）" for code, name, _ in rest))
                for code, name, _ in rest:
                    btns.append((f"选方向：{code}", (lambda cd=code: self._choose_direction(cd))))
        else:
            lines.append("还没有方向，去「对话」里说说你感兴趣什么。")

    def _enter(self) -> str:
        s = self.s
        self.api.call("POST", "/api/onboard/enter", {"uid": s.uid}, token=s.token)
        s.entered = True
        self._mark_done("enter")
        return self._today_screen()

    # 今日 / 研读 / 论文
    def _today_screen(self) -> str:
        s = self.s
        if not s.entered:
            self.api.call("POST", "/api/onboard/enter", {"uid": s.uid}, token=s.token)
            s.entered = True
            self._mark_done("enter")
        d = self.api.call("GET", f"/api/daily?uid={s.uid}", token=s.token, timeout=120)
        items = [i for i in (d.get("items") or []) if isinstance(i, dict)][:6]
        if not items and (s.direction or d.get("kit")):
            # 兴趣抓取没东西（本机没联网、或还没采集）：退到这个方向的阅读工具包，论文随仓库自带
            try:
                kits = (self.api.call("GET", "/api/kits") or {}).get("kits") or []
                kit = next((k for k in kits if k.get("direction") == s.direction), None) or (kits[0] if kits else None)
                if kit:
                    s.kit = str(kit["id"])
                    d2 = self.api.call("GET", f"/api/daily?uid={s.uid}&kit={s.kit}", token=s.token, timeout=120)
                    items = [i for i in (d2.get("items") or []) if isinstance(i, dict)][:6]
                    if items:
                        d = {**d2, "note": f"来自工具包「{kit.get('name') or s.kit}」"}
            except ApiError:
                pass
        s.papers = items
        lines = []
        goal = d.get("goal")
        if isinstance(goal, (int, float)) and goal:
            lines.append(f"今天看完 {int(goal)} 篇就算完成（每篇说要不要留下）。")
        elif isinstance(goal, str) and goal:
            lines.append(goal)
        if d.get("error"):
            lines.append(f"提示：{d['error']}")
        if d.get("note"):
            lines.append(str(d["note"]))
        btns: list[tuple[str, Callable[[], str]]] = []
        if not items:
            if s.direction or d.get("kit"):
                lines.append("今天还没有推荐，去「研读」看方向目录。")
            else:
                self._direction_choices(lines, btns)
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it.get('title_zh') or it.get('title') or it.get('arxiv_id')}")
            btns.append((f"打开论文 {i}", (lambda k=i: self._open_paper(k))))
        s.screen, s.title, s.body, s.input_hint = "today", "今日", "\n".join(lines), ""
        s.buttons = self._nav_buttons(btns)
        return ""

    def _read_screen(self) -> str:
        s = self.s
        d = self.api.call("GET", f"/api/papers/dir?uid={s.uid}", token=s.token, timeout=120)
        items = [i for i in (d.get("items") or []) if isinstance(i, dict)][:8]
        s.papers = items
        lines = [f"方向：{d.get('node_label') or d.get('node') or s.direction or '（未选）'}"]
        if d.get("note"):
            lines.append(str(d["note"]))
        btns: list[tuple[str, Callable[[], str]]] = []
        for i, it in enumerate(items, 1):
            kw = "、".join((it.get("keywords") or [])[:3])
            lines.append(f"{i}. {it.get('title_zh') or it.get('title') or it.get('id')}" + (f"（{kw}）" if kw else ""))
            btns.append((f"打开论文 {i}", (lambda k=i: self._open_paper(k))))
        if not items:
            if not (d.get("node") or s.direction):
                self._direction_choices(lines, btns)
            else:
                lines.append("这个方向还没有目录；可以回「今日」。")
        s.screen, s.title, s.body, s.input_hint = "read", "研读", "\n".join(lines), ""
        s.buttons = self._nav_buttons(btns)
        return ""

    def _open_paper(self, n: int) -> str:
        s = self.s
        if n < 1 or n > len(s.papers):
            return f"没有第 {n} 篇；屏幕上只有 {len(s.papers)} 篇。"
        it = s.papers[n - 1]
        aid = str(it.get("arxiv_id") or it.get("id") or it.get("paper_id") or "")
        s.paper = {**it, "id": aid}
        s.kit = str(it.get("card_kit") or it.get("kit") or s.kit or "")
        text = ""
        try:
            p = self.api.call("GET", f"/api/papers/{aid}", token=s.token, timeout=120)
            if isinstance(p, dict):
                text = str(p.get("text") or p.get("abstract") or p.get("summary") or "")
                s.paper["title"] = p.get("title") or s.paper.get("title")
        except ApiError as exc:
            text = f"（正文没取到：{exc.detail}）"
        title = s.paper.get("title_zh") or s.paper.get("title") or aid
        s.screen, s.title = "paper", "论文"
        s.body = f"{title}\n{(it.get('abstract') or '')[:400]}\n{text[:1200]}".strip()
        s.input_hint = ""
        s.buttons = self._nav_buttons([("开始带读", self._start_tutor), ("返回研读", self._read_screen)])
        return ""

    # 带读
    def _start_tutor(self) -> str:
        s = self.s
        if not s.paper.get("id"):
            return "先打开一篇论文。"
        if not s.kit:
            kits = (self.api.call("GET", "/api/kits") or {}).get("kits") or []
            s.kit = str(kits[0]["id"]) if kits else ""
        if not s.kit:
            return "这篇论文没有对应的带读工具包。"
        r = self.api.call("POST", f"/api/tutor/{s.paper['id']}/start", {"uid": s.uid, "kit": s.kit, "restart": False},
                          token=s.token, timeout=240)
        s.tutor = r.get("session") if isinstance(r.get("session"), dict) else r
        return self._tutor_screen()

    def _current_q(self) -> dict | None:
        t = self.s.tutor or {}
        qs = t.get("questions") or t.get("slots") or t.get("items") or []
        for q in qs:
            if isinstance(q, dict) and q.get("qid") and (q.get("status") or "active") in ("active", "pending", "open", "", None):
                return q
        return next((q for q in qs if isinstance(q, dict) and q.get("qid")), None)

    def _tutor_screen(self) -> str:
        s = self.s
        q = self._current_q()
        t = s.tutor or {}
        lines = [f"带读：{s.paper.get('title_zh') or s.paper.get('title') or s.paper.get('id')}"]
        if t.get("status"):
            lines.append(f"状态：{t['status']}")
        if q:
            lines.append(f"问题（{q.get('qid')}）：{q.get('question') or ''}")
            if q.get("hint"):
                lines.append(f"提示：{q['hint']}")
            if q.get("feedback"):
                lines.append(f"上次反馈：{q['feedback']}")
        else:
            lines.append("没有待答的问题了，可以交卷。")
        s.screen, s.title, s.body = "tutor", "带读", "\n".join(lines)
        s.input_hint = "把你的回答写出来（可以说不知道）"
        s.buttons = self._nav_buttons([("跳过这题", lambda: self._answer_tutor("", True)), ("交卷", self._finish_tutor),
                                       ("返回论文", lambda: self._open_paper(s.papers.index(next((p for p in s.papers if str(p.get('arxiv_id') or p.get('id') or p.get('paper_id')) == s.paper.get('id')), s.papers[0])) + 1) if s.papers else "")])
        return ""

    def _answer_tutor(self, text: str, skip: bool) -> str:
        s = self.s
        q = self._current_q()
        if not q:
            return "没有待答的问题，可以交卷。"
        body = {"uid": s.uid, "kit": s.kit, "qid": q["qid"], "answer": text[:2000], "skip": skip, "rev": int((s.tutor or {}).get("rev") or 0)}
        r = self.api.call("POST", f"/api/tutor/{s.paper['id']}/turn", body, token=s.token, timeout=240)
        v = r.get("verdict") or {}
        if isinstance(r.get("session"), dict):
            s.tutor = r["session"]
        note = ""
        if v:
            note = f"判卷：{'通过' if v.get('pass') else '没过'}；{v.get('feedback') or v.get('note') or ''}".strip("；")
        self._tutor_screen()
        return note

    def _finish_tutor(self) -> str:
        s = self.s
        r = self.api.call("POST", f"/api/tutor/{s.paper['id']}/finish", {"uid": s.uid, "kit": s.kit}, token=s.token, timeout=120)
        self._mark_done("tutor")
        summary = r.get("summary") or r.get("result") or r
        s.screen, s.title = "tutor_done", "带读结束"
        s.body = f"交卷了。结果：{json.dumps(summary, ensure_ascii=False)[:600]}"
        s.input_hint = ""
        s.buttons = self._nav_buttons([("返回研读", self._read_screen)])
        return ""

    # 方向
    def _choose_direction(self, code: str) -> str:
        s = self.s
        code = (code or "").strip()
        m = re.search(r"[：:]\s*([a-z_]+)\s*$", code)  # 「选方向：ai」整段按钮文字也认
        if m:
            code = m.group(1)
        try:
            import planner
            if code not in planner.DIRECTIONS:
                code = next((k for k, v in planner.DIRECTIONS.items() if v["name"] == code or code in v["name"]), code)
        except Exception:
            pass
        self.api.call("POST", "/api/directions/choose", {"uid": s.uid, "code": code}, token=s.token)
        s.direction = code
        return self._today_screen() or f"选了方向：{code}"

    # 项目（边学边练）
    def _projects_screen(self) -> str:
        s = self.s
        if s.interview.get("session_id") and not s.interview.get("done"):
            return self._interview_screen()
        if s.projects:
            return self._project_list_screen()
        return self._search_projects("")

    def _search_projects(self, keywords: str) -> str:
        s = self.s
        body = {"uid": s.uid, "direction": s.direction or "", "keywords": keywords or ""}
        r = self.api.call("POST", "/api/projects/search", body, token=s.token, timeout=180)
        s.projects = [p for p in (r.get("items") or []) if isinstance(p, dict)][:6]
        if r.get("session_id") and r.get("question"):
            s.interview = {"session_id": r["session_id"], "question": r["question"], "options": r.get("options") or [], "done": bool(r.get("done"))}
            if not s.projects:
                return self._interview_screen()
        return self._project_list_screen(r.get("note") or "")

    def _project_list_screen(self, note: str = "") -> str:
        s = self.s
        lines = [note] if note else []
        btns: list[tuple[str, Callable[[], str]]] = []
        for i, p in enumerate(s.projects, 1):
            lines.append(f"{i}. {p.get('name') or p.get('title') or p.get('id')}：{str(p.get('summary') or p.get('why') or p.get('desc') or '')[:80]}")
            btns.append((f"挑第 {i} 个", (lambda k=i: self._pick_project(k))))
        if not s.projects:
            lines.append("没有搜到项目。可以换个关键词再搜。")
        if s.project.get("id"):
            lines.append(f"已选项目：{s.project.get('name') or s.project.get('id')}")
            btns.append(("交作品（示例 zip）", self._submit_project))
        btns.append(("换个关键词搜", lambda: self._search_projects("入门")))
        s.screen, s.title, s.body = "projects", "项目", "\n".join(lines)
        s.input_hint = "想搜什么项目（关键词）" if not s.interview.get("session_id") else ""
        s.buttons = self._nav_buttons(btns)
        return ""

    def _interview_screen(self) -> str:
        s = self.s
        iv = s.interview
        lines = [str(iv.get("question") or "")]
        btns: list[tuple[str, Callable[[], str]]] = []
        for o in iv.get("options") or []:
            if isinstance(o, dict) and o.get("label"):
                btns.append((str(o["label"]), (lambda oid=str(o.get("id") or ""): self._interview(option_id=oid))))
        s.screen, s.title, s.body = "projects", "项目", "\n".join(lines)
        s.input_hint = "回答这个问题"
        s.buttons = self._nav_buttons(btns)
        return ""

    def _interview(self, answer: str = "", option_id: str = "") -> Any:
        s = self.s
        r = self.api.call("POST", "/api/projects/interview",
                          {"uid": s.uid, "session_id": s.interview.get("session_id", ""), "answer": answer, "option_id": option_id},
                          token=s.token, timeout=120)
        if r.get("done"):
            s.interview = {**s.interview, "done": True}
            note = self._search_projects(answer or "")
            return self._out(note) if answer else note
        s.interview = {"session_id": r.get("session_id") or s.interview.get("session_id"), "question": r.get("question"),
                       "options": r.get("options") or [], "done": False}
        note = self._interview_screen()
        return self._out(note) if answer else note

    def _pick_project(self, n: int) -> str:
        s = self.s
        if n < 1 or n > len(s.projects):
            return f"没有第 {n} 个；屏幕上只有 {len(s.projects)} 个。"
        p = s.projects[n - 1]
        r = self.api.call("POST", "/api/projects/pick", {"uid": s.uid, "id": str(p.get("id"))}, token=s.token)
        s.project = {**p, **(r if isinstance(r, dict) else {}), "id": str((r or {}).get("id") or p.get("id"))}
        return self._project_list_screen(f"选定了：{s.project.get('name') or s.project.get('id')}。可以交作品。")

    def _submit_project(self) -> str:
        s = self.s
        pid = str(s.project.get("id") or "")
        if not pid:
            return "先挑一个项目。"
        blob = self.api.call("GET", f"/api/projects/{pid}/sample.zip?uid={s.uid}", token=s.token, timeout=120)
        if not isinstance(blob, (bytes, bytearray)):
            return "示例作品没取到。"
        portrait = ""
        try:
            ps = (self.api.call("GET", f"/api/portraits?uid={s.uid}", token=s.token) or {}).get("portraits") or []
            portrait = next((str(p.get("id")) for p in ps if p.get("active")), "")
        except ApiError:
            portrait = ""
        r = self.api.call("POST", f"/api/projects/{pid}/submit?uid={s.uid}&portrait={portrait}", raw=bytes(blob),
                          content_type="application/zip", token=s.token, timeout=300)
        self._mark_done("project")
        s.screen, s.title = "project_review", "评阅"
        s.body = f"交上去了。评阅：{json.dumps(r, ensure_ascii=False)[:800]}"
        s.input_hint = ""
        s.buttons = self._nav_buttons([("回项目", self._project_list_screen)])
        return ""

    # 我的
    def _me_screen(self) -> str:
        s = self.s
        me = self.api.call("GET", "/api/auth/me", token=s.token)
        facts = []
        try:
            facts = (self.api.call("GET", f"/api/me/facts?uid={s.uid}", token=s.token) or {}).get("facts") or []
        except ApiError:
            facts = []
        lines = [f"昵称：{me.get('nickname')}  用户名：{me.get('username')}  今天还能让模型回答 {me.get('llm_left')} 次",
                 f"网站记住的关于你的事：{len(facts)} 条"]
        for f in facts[:8]:
            if isinstance(f, dict):
                lines.append(f"· {f.get('key') or ''}：{str(f.get('value') or '')[:60]}")
        s.screen, s.title, s.body, s.input_hint = "me", "我的", "\n".join(lines), ""
        s.buttons = self._nav_buttons([])
        return ""

    # ---- 记录 ----

    def _mark_done(self, pid: str) -> None:
        s = self.s
        if pid in s.done:
            return
        s.done.append(pid)
        self._event(f"done:{pid}", pid)

    def _event(self, kind: str, detail: str) -> None:
        s = self.s
        if not s.uid or not s.run_id:
            return
        if kind == "step":
            s.steps += 1
        try:
            self.api.call("POST", "/api/agent/event", {"uid": s.uid, "run_id": s.run_id, "kind": kind, "screen": s.screen, "detail": detail},
                          token=s.token, timeout=20)
        except ApiError:
            pass

    def _need_student(self) -> None:
        if not self.s.uid or self.s.ended:
            raise ApiError(0, "还没上线：先 start_as_student")


def _norm(x: str) -> str:
    return re.sub(r"[\s（）()：:「」\[\]]", "", (x or "")).lower()


# ---------------- 工具表 ----------------

TOOLS: list[dict] = [
    {"name": "start_as_student",
     "description": "以一个学生人设上线（注册一个新账号）。返回人设卡、今天的目标和第一个屏幕。persona 可选：diligent_y1 / bored_y3 / non_cs / knows_direction / careless。",
     "inputSchema": {"type": "object", "properties": {"persona": {"type": "string"}, "agent_model": {"type": "string", "description": "你是什么模型，例如 deepseek-v4.1、gpt-5-codex"}}}},
    {"name": "look", "description": "再看一眼当前屏幕（文字、可按的按钮、输入框、你的目标）。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "reply", "description": "在当前屏幕的输入框里说一句话（对话、带读作答、项目问答都用它）。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "press", "description": "按屏幕上「可以按的」里的一个按钮，按它的文字。", "inputSchema": {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}},
    {"name": "open_paper", "description": "打开屏幕上列出的第 n 篇论文。", "inputSchema": {"type": "object", "properties": {"n": {"type": "integer", "default": 1}}}},
    {"name": "start_tutor", "description": "对当前打开的论文开始带读（网站会出题）。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "answer_tutor", "description": "回答带读的当前问题；skip=true 跳过这题。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "skip": {"type": "boolean", "default": False}}}},
    {"name": "finish_tutor", "description": "带读交卷。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "choose_direction", "description": "选一个研究方向（屏幕上「选方向：xx」里的代码）。", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "search_projects", "description": "在「项目」里按关键词搜项目。", "inputSchema": {"type": "object", "properties": {"keywords": {"type": "string"}}}},
    {"name": "pick_project", "description": "选定屏幕上列出的第 n 个项目。", "inputSchema": {"type": "object", "properties": {"n": {"type": "integer", "default": 1}}}},
    {"name": "submit_project", "description": "把示例作品（zip）交给当前选定的项目，看评阅。", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stuck", "description": "不知道该做什么、看不懂屏幕时一定要调：说明卡在哪。", "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "end_day", "description": "今天结束（目标做完了，或者耐心用完了）。可附一句总结。", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}}},
]

INSTRUCTIONS = ("你是一个来用「启研」的学生，不是助手。先 start_as_student 拿到人设和目标；之后只根据屏幕文字行动：reply 说话，"
                "press 按按钮，open_paper 打开论文。看不懂就 stuck 说明原因；目标做完或耐心用完就 end_day。不要猜接口，不要编造屏幕上没有的东西。")


def _pick(a: dict, *keys: str, default: Any = "") -> Any:
    """agent 常把参数名写歪（label 写成 text/button，code 写成 direction）：认下来，别把它算成产品的卡点。"""
    for k in keys:
        if k in a and a[k] not in (None, ""):
            return a[k]
    return default


def dispatch(tools: Tools, name: str, args: dict) -> dict:
    a = args or {}
    if name == "start_as_student":
        return tools.start_as_student(str(a.get("persona") or ""), str(a.get("agent_model") or ""))
    if name == "look":
        return tools.look()
    if name == "reply":
        return tools.reply(str(_pick(a, "text", "message", "msg", "content", "answer")))
    if name == "press":
        return tools.press(str(_pick(a, "label", "button", "text", "name")))
    if name == "open_paper":
        return tools.open_paper(int(_pick(a, "n", "index", "number", "i", default=1)))
    if name == "start_tutor":
        return tools.start_tutor()
    if name == "answer_tutor":
        return tools.answer_tutor(str(_pick(a, "text", "answer", "message")), bool(a.get("skip")))
    if name == "finish_tutor":
        return tools.finish_tutor()
    if name == "choose_direction":
        return tools.choose_direction(str(_pick(a, "code", "direction", "id", "label", "name")))
    if name == "search_projects":
        return tools.search_projects(str(_pick(a, "keywords", "query", "q", "text")))
    if name == "pick_project":
        return tools.pick_project(int(_pick(a, "n", "index", "number", "i", default=1)))
    if name == "submit_project":
        return tools.submit_project()
    if name == "stuck":
        return tools.stuck(str(_pick(a, "reason", "why", "text", "message")))
    if name == "end_day":
        return tools.end_day(str(_pick(a, "summary", "text", "note")))
    raise KeyError(name)


def call_tool(tools: Tools, name: str, args: dict | None) -> dict:
    try:
        data = dispatch(tools, name, args or {})
    except KeyError:
        return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}
    except ApiError as exc:
        return {"content": [{"type": "text", "text": f"出错了：{exc.detail}"}], "isError": True}
    except Exception as exc:                                            # noqa: BLE001
        return {"content": [{"type": "text", "text": f"{name} 执行失败：{exc!r}"}], "isError": True}
    text = data.get("text") if isinstance(data, dict) else json.dumps(data, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text or json.dumps(data, ensure_ascii=False)}], "structuredContent": data}


# ---------------- JSON-RPC ----------------

def handle(tools: Tools, msg: dict) -> dict | None:
    if not isinstance(msg, dict):
        return _err(None, -32600, "invalid request")
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        return _ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                         "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        return _ok(mid, call_tool(tools, params.get("name", ""), params.get("arguments")))
    if method in ("resources/list", "resources/templates/list"):
        return _ok(mid, {"resources": [], "resourceTemplates": []})
    if method == "prompts/list":
        return _ok(mid, {"prompts": []})
    return _err(mid, -32601, f"method not found: {method}")


def http_handle(tools: Tools, payload) -> dict | list | None:
    if isinstance(payload, list):
        out = [r for r in (handle(tools, m) for m in payload) if r is not None]
        return out or None
    return handle(tools, payload)


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ---------------- 远程模式（挂在 /mcp/student）：每个 operator+会话 一位学生 ----------------

_SESSIONS: dict[str, Tools] = {}


def remote_handle(payload, operator: str, session_key: str) -> dict | list | None:
    now = time.time()
    for k in [k for k, t in _SESSIONS.items() if now - t.s.touched > SESSION_TTL]:
        _SESSIONS.pop(k, None)
    tools = _SESSIONS.get(session_key)
    if tools is None:
        tools = Tools(Api(os.environ.get("QIYAN_SELF_URL") or "http://127.0.0.1:8100", token_for(operator), operator))
        _SESSIONS[session_key] = tools
    return http_handle(tools, payload)


# ---------------- stdio ----------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="启研人工学生 MCP（stdio）")
    ap.add_argument("--base", default=os.environ.get("QIYAN_URL") or "http://127.0.0.1:8100")
    ap.add_argument("--token", default=os.environ.get("QIYAN_AGENT_TOKEN") or "")
    ap.add_argument("--operator", default="")
    ap.add_argument("--no-stream", action="store_true", help="对话用 /api/dialogue/turn 而不是 SSE 流")
    a = ap.parse_args(argv)
    if not a.token:
        sys.stderr.write("缺 operator 令牌：--token 或 QIYAN_AGENT_TOKEN\n")
        sys.exit(2)
    api = Api(a.base, a.token, a.operator or operator_for(a.token) or "agent")
    api.prefer_stream = not a.no_stream
    tools = Tools(api)
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_err(None, -32700, "parse error")) + "\n")
            continue
        resp = http_handle(tools, msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
