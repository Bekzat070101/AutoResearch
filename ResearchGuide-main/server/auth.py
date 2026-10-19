# -*- coding: utf-8 -*-
"""账号：微信登录（关注公众号、发网页上的 6 位数字）；访客可以先用，之后绑微信把数据带走。

- 为什么是公众号消息：网站扫码登录要微信开放平台的企业认证和备案域名，个人拿不到；
  个人订阅号能收用户发来的消息，消息里带发信人的 openid，够认出是谁（细节见 wechat.py）。
  不存手机号、不用密码；openid 只对我们的公众号有效，拿不到微信号和微信资料。
- 流程：网页 start 拿到 ticket（只在这个浏览器里）和 6 位数字 → 学生在公众号里发这个数字
  → 消息回调把 openid 记到这次请求上 → 网页轮询 poll，拿 ticket 换成会话。一次请求只能换一次。
- 令牌放在请求头 Authorization: Bearer，不用 cookie：页面在 github.io、接口在 fcapp.run，跨站 cookie 会被浏览器拦。
- 所有接口默认要登录，PUBLIC 里的才放行；请求里带的 uid（查询串、路径、JSON 体）必须就是登录的这个人。
  原来 uid 就是唯一凭证：知道别人的 uid 就能读他的成绩单和对话。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import budget
import store
import wechat

SESSION_TTL = 30 * 86400   # 三十天不用才过期；用着的一天续一次
TICKET_TTL = 300           # 网页上的数字五分钟有效
# 同一来源一小时最多开几次微信登录、几个访客号。学校机房、宿舍的出口常常是同一个公网地址、几百人共用：
# 原来 30，一个班两百人同时开访客号 170 个被拦（Codex 第五轮压测）。这里只挡脚本刷号；
# 访客能花的模型钱另有全体访客合计的上限（budget.guests_cap），刷再多访客号也花不到绑了微信的人头上。
PER_IP_HOUR = int(os.environ.get("AUTH_PER_IP_HOUR") or 600)
SIGNIN_PER_IP_HOUR = int(os.environ.get("AUTH_SIGNIN_PER_IP_HOUR") or 3000)
SIGNIN_USER_LIMIT = int(os.environ.get("AUTH_SIGNIN_USER_LIMIT") or 10)  # 同一用户名窗口内最多失败这么多次
SIGNIN_USER_WINDOW = int(os.environ.get("AUTH_SIGNIN_USER_WINDOW") or 600)
PER_OPENID_HOUR = 10       # 同一个微信一小时最多发错十次数字（防止乱发数字把别人的网页登进自己的号）

# 隐私说明改了就改这个日期：老用户下次打开会再看到一次说明并重新同意。说明正文在 web/js/app.js 的 PRIVACY_HTML。
PRIVACY_VERSION = "2026-10-15"

# 不登录也能用的接口（按路由模板写）。新加的接口默认要登录：要么带 uid，要么加到这里并说明为什么可以公开。
PUBLIC = frozenset({
    "/", "/api/health", "/metrics",
    "/api/auth/login", "/api/auth/register", "/api/auth/signin", "/api/auth/legacy",
    "/api/auth/wechat/start", "/api/auth/wechat/poll", "/api/auth/wechat/dev-send",
    "/api/wechat",  # 微信服务器回调，自己验签
    # 公开知识：课程、老师、专业、培养方案、方向路径、阅读工具包、论文原文、给学生自己 agent 的简报
    "/api/explore/courses", "/api/explore/teachers", "/api/explore/majors", "/api/explore/major",
    "/api/explore/minor", "/api/explore/course", "/api/curriculum/match", "/api/curriculum/stats",
    "/mcp", "/api/projects/sources", "/api/paths", "/api/kits", "/api/kits/{kit_id}",
    "/api/brief",
    "/api/llm/connect",  # 自己核对管理员口令
    "/api/admin/backup", "/api/admin/restore", "/api/admin/stats",
    "/api/admin/maintenance", "/api/admin/user/delete", "/api/admin/rewrap",
    "/api/admin/papers/warm", "/api/admin/papers/collect",
    "/api/admin/feedback", "/api/admin/feedback/status",  # 上游 w97 的反馈管理：同样自己核对管理员口令
})

_ARXIV_PAPER_PATH = re.compile(r"^/api/papers/\d{4}\.\d{4,5}$")


def _public_arxiv_paper(path: str) -> bool:
    return bool(_ARXIV_PAPER_PATH.match(path or ""))

router = APIRouter()


# ---------- 每个请求：认人 ----------

def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


# 续期要写库、要拿写锁；放到这个线程里做，guard 那一侧只读
_TOUCH = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-touch")


def _session_uid(request: Request) -> str | None:
    token = _bearer(request)
    if not token:
        return None
    uid, stale = store.session_lookup(token)
    if stale:
        _TOUCH.submit(store.touch_session, token, SESSION_TTL)
    return uid


async def guard(request: Request) -> None:
    """挂在整个 app 上的依赖。公开接口直接放行；其余的先认会话，再核对请求里自称的 uid。"""
    route = request.scope.get("route")
    budget.bind(None)  # 先当匿名：公开接口不许调模型
    path = request.url.path or ""
    route_path = getattr(route, "path", None)
    if path.startswith("/static") or path.startswith("/api/admin") or route_path in PUBLIC or path in PUBLIC:
        return
    # 路由还没绑上时，公开知识仍按路径放行，避免工具包被过期会话拖成 401。
    # 论文原文 /api/papers/ 不在这里：没登录也能让服务器去 arXiv 取任意论文，会占满限速的抓取队列和磁盘缓存
    if path.startswith("/api/kits/"):
        return
    # 查会话是一次 SQLite 读：放线程池，别在事件循环上做。每个请求都经过这里，库在网络盘上时一次就是几毫秒，
    # 在循环上排队会拖慢所有人，包括正在流式输出的对话。
    uid = await run_in_threadpool(_session_uid, request)
    if not uid:
        raise HTTPException(401, "请先登录", headers={"WWW-Authenticate": "Bearer"})
    request.state.uid = uid
    budget.bind(uid)  # 这个请求里的模型调用算在这个人头上
    claimed = [*request.query_params.getlist("uid"), request.path_params.get("uid")]
    # 有请求体的接口：不看 Content-Type，只要体是 JSON 对象就核对里面的 uid。
    # FastAPI 对没有 Content-Type、application/JSON、application/xxx+json 都照样当 JSON 解析；
    # 原来只认小写子串 "json"，把头写成 application/JSON 就能改别人的数据。
    # 体在跑依赖之前已经被 FastAPI 读进内存，这里取的是缓存，不会把上传流再读一遍（上传接口没有 body_field）。
    if getattr(route, "body_field", None) is not None:
        try:
            raw = await request.body()
        except RuntimeError:  # 表单接口的流已被表单解析读走（目前没有这种接口）
            raw = b""
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None  # 坏 JSON 交给接口自己报 422
        if isinstance(body, dict):
            claimed.append(body.get("uid"))
    if any(c not in (None, "") and c != uid for c in claimed):
        raise HTTPException(403, "这不是你的账号")


def me(request: Request) -> str | None:
    """登录的这个人。只在不带 uid、按对象 id 取东西的接口里用来核对归属。"""
    return getattr(request.state, "uid", None)


# ---------- 频率 ----------

_HITS: dict[str, list[float]] = {}
_HITS_LOCK = threading.Lock()


def _client_ip(request: Request) -> str | None:
    """限频用的来源地址。在代理后面（TRUST_PROXY=1）取 X-Forwarded-For 第一个；
    否则只认公网地址——代理的内网地址是所有人共用的，按它限频会把所有人一起拦住。"""
    if os.environ.get("TRUST_PROXY") == "1":
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if first:
            return first
    host = request.client.host if request.client else ""
    try:
        return host if ipaddress.ip_address(host).is_global else None
    except ValueError:
        return None


_HITS_CAP = 10000
_NEXT_SWEEP = [0.0]


def _ttl(key: str) -> float:
    return 600 if key.startswith("signin-user:") else 3600


def _sweep_expired(now: float) -> None:
    for k in [k for k, v in _HITS.items() if not v or now - v[-1] >= _ttl(k)]:
        del _HITS[k]


def _recent(key: str, now: float, window: float = 3600) -> list[float] | None:
    """这个 key 在 window 秒内的记录；容量满且是新 key 时返回 None。调用方持有 _HITS_LOCK。"""
    if now >= _NEXT_SWEEP[0]:
        _sweep_expired(now)
        _NEXT_SWEEP[0] = now + 1
    if key not in _HITS and len(_HITS) >= _HITS_CAP:
        return None
    hits = [t for t in _HITS.get(key, []) if now - t < window]
    _HITS[key] = hits
    return hits


def _allow(key: str, limit: int, window: float = 3600) -> bool:
    """window 秒内同一个 key 最多 limit 次：查一次、记一次。内存计数，有界。"""
    now = time.time()
    with _HITS_LOCK:
        hits = _recent(key, now, window)
        if hits is None or len(hits) >= limit:
            return False
        hits.append(now)
    return True


def _used_up(key: str, limit: int, window: float = 3600) -> bool:
    """只查不记。"""
    with _HITS_LOCK:
        hits = _recent(key, time.time(), window)
        return hits is None or len(hits) >= limit


def _note(key: str, window: float = 3600) -> None:
    """只记不查。"""
    now = time.time()
    with _HITS_LOCK:
        hits = _recent(key, now, window)
        if hits is not None:
            hits.append(now)


def _clear(key: str) -> None:
    with _HITS_LOCK:
        _HITS.pop(key, None)


def _ip_allows(request: Request, kind: str) -> bool:
    return allow_ip(request, kind, PER_IP_HOUR)


def allow_ip(request: Request, kind: str, limit: int) -> bool:
    """同一来源一小时最多 limit 次（来源认不出时不限，见 _client_ip）。"""
    ip = _client_ip(request)
    return True if ip is None else _allow(f"{kind}:{ip}", limit)


def _dev() -> bool:
    """本机开发、没配公众号：用 /api/auth/wechat/dev-send 假装从微信发数字。线上不要设 AUTH_DEV_CODES。"""
    return os.environ.get("AUTH_DEV_CODES") == "1" and not wechat.configured()


ENTER_GATE_SINCE = "2026-10-13"  # 这天起新号默认未进入；更早的号视为已进入
_CHINA = timezone(timedelta(hours=8))


def _china_day(raw: str) -> str:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CHINA).date().isoformat()
    except ValueError:
        return raw[:10]


def user_entered(uid: str, u: dict[str, Any] | None = None) -> bool:
    """进入按人记；新号必须显式进入，发消息或刷新不会提前结束首次引导。"""
    u = u if u is not None else (store.get_user(uid) or {})
    prefs = store.get_prefs(uid)
    if prefs.get("entered_at"):
        return True
    created = str(u.get("created_at") or "")
    if created and _china_day(created) < ENTER_GATE_SINCE:
        return True
    return False


def account(uid: str) -> dict[str, Any]:
    u = store.get_user(uid) or {}
    username = u.get("username") or ""
    meta = store.llm_secret_meta(uid)
    return {
        "uid": uid,
        "nickname": u.get("nickname", ""),
        "username": username,
        "wechat": bool(u.get("wechat_openid")),
        "guest": not bool(u.get("wechat_openid") or username),
        "consent_ok": u.get("consent_version") == PRIVACY_VERSION,
        "privacy_version": PRIVACY_VERSION,
        "llm_left": budget.left(uid) if u else 0,  # 今天还能让服务器的模型回答几次；用自己密钥的人不受这个数限制
        "has_llm_key": meta["has_key"],
        "llm_key_tail": meta["tail"],
        "llm_key_invalid": store.llm_secret_invalid(uid) if meta["has_key"] else False,
        "entered": user_entered(uid, u),
    }


def status() -> dict[str, Any]:
    """给 /api/health：页面据此决定显示「微信登录」还是只给访客入口，以及公众号二维码。"""
    st = os.environ.get("QIYAN_RECOVERY_STATE")
    if st not in ("normal", "recovering", "accounts_restored", "lost"):
        st = "normal"
    return {"wechat_login": wechat.configured() or _dev(), "dev": _dev(), "qr_url": wechat.qr_url(),
            "account_name": wechat.account_name(), "privacy_version": PRIVACY_VERSION,
            "recovery": {"event_id": os.environ.get("QIYAN_RECOVERY_EVENT") or "", "state": st}}


# ---------- 接口 ----------

class GuestReq(BaseModel):
    nickname: str
    consent: bool = False


class RegisterReq(BaseModel):
    username: str
    password: str
    nickname: str = ""
    consent: bool = False


class SigninReq(BaseModel):
    username: str
    password: str


class UpgradeReq(BaseModel):
    username: str
    password: str


class WechatStartReq(BaseModel):
    nickname: str = ""
    consent: bool = False


class WechatPollReq(BaseModel):
    ticket: str


class DevSendReq(BaseModel):
    code: str
    openid: str = "dev-openid"


class LegacyReq(BaseModel):
    uid: str


class ConsentReq(BaseModel):
    version: str


@router.post("/api/auth/login")
def guest_login(req: GuestReq, request: Request):
    """访客：只要昵称。数据只能凭这台浏览器里的令牌找回，之后绑微信就能换设备。"""
    nickname = req.nickname.strip()[:24]
    if not nickname:
        raise HTTPException(400, "nickname is required")
    if not _ip_allows(request, "guest"):
        raise HTTPException(429, "开的访客号太多了，过一会儿再试")
    uid = store.create_user(nickname)["uid"]
    if req.consent:
        store.record_consent(uid, PRIVACY_VERSION)
    return {**account(uid), "token": store.create_session(uid, SESSION_TTL)}


def _password_ok(password: str) -> str:
    if len(password or "") < 8:
        raise HTTPException(400, "密码至少 8 个字符")
    return password


def _username_ok(username: str) -> str:
    if not store.valid_username(username):
        raise HTTPException(400, "用户名 2–24 个字符，只能用字母、数字、下划线")
    return store.normalize_username(username)


@router.post("/api/auth/register")
def register(req: RegisterReq, request: Request):
    if not req.consent:
        raise HTTPException(400, "请先看过并同意隐私说明")
    username = _username_ok(req.username)
    password = _password_ok(req.password)
    if store.user_by_username(username):
        raise HTTPException(409, "这个用户名已经有人用了")
    if not _ip_allows(request, "guest"):
        raise HTTPException(429, "开的号太多了，过一会儿再试")
    row = store.register_user(username, password, req.nickname)
    store.record_consent(row["uid"], PRIVACY_VERSION)
    return {**account(row["uid"]), "token": store.create_session(row["uid"], SESSION_TTL)}


@router.post("/api/auth/signin")
def signin(req: SigninReq, request: Request):
    username = store.normalize_username(req.username)
    if not store.valid_username(username):  # 形状不对的用户名直接 401，不给它建桶（R05：桶总数有上限，别让垃圾名占位）
        raise HTTPException(401, "用户名或密码不对")
    if not allow_ip(request, "signin", SIGNIN_PER_IP_HOUR):
        raise HTTPException(429, "试得太频繁了，过一会儿再试")
    key = f"signin-user:{username}"
    if username and _used_up(key, SIGNIN_USER_LIMIT, SIGNIN_USER_WINDOW):
        raise HTTPException(429, "试得太频繁了，过一会儿再试")
    user = store.user_by_username(username)
    if not user or not store.password_ok(req.password, user.get("password_salt"), user.get("password_hash")):
        _note(key, SIGNIN_USER_WINDOW)
        raise HTTPException(401, "用户名或密码不对")
    _clear(key)
    return {**account(user["id"]), "token": store.create_session(user["id"], SESSION_TTL)}


@router.post("/api/auth/upgrade")
def upgrade(req: UpgradeReq, request: Request):
    uid = me(request)
    username = _username_ok(req.username)
    password = _password_ok(req.password)
    if store.user_by_username(username):
        raise HTTPException(409, "这个用户名已经有人用了")
    try:
        store.upgrade_user(uid, username, password)
    except KeyError:
        raise HTTPException(404, "user not found") from None
    except ValueError as exc:
        if str(exc) == "taken":
            raise HTTPException(409, "这个用户名已经有人用了") from exc
        raise HTTPException(409, "这个账号已经有用户名了") from exc
    return account(uid)


@router.post("/api/auth/wechat/start")
def wechat_start(req: WechatStartReq, request: Request):
    """开一次微信登录：给网页一个 ticket（留在浏览器里换会话用）和一个 6 位数字（让学生发到公众号）。
    带着访客会话来的，登录成功时把微信绑到这个访客号上，记录跟着走。"""
    if not (wechat.configured() or _dev()):
        raise HTTPException(503, "微信登录还没开通，暂时只能用访客进入")
    if not _ip_allows(request, "wechat"):
        raise HTTPException(429, "试得太频繁了，过一会儿再试")
    guest_uid = None
    token = _bearer(request)
    if token:
        uid = store.session_user(token, SESSION_TTL)
        if uid and account(uid)["guest"]:
            guest_uid = uid
    ticket = secrets.token_urlsafe(32)
    for _ in range(20):  # 同一时刻在等的数字不能重
        code = f"{secrets.randbelow(10 ** 6):06d}"
        if store.wechat_ticket_new(ticket, code, time.time() + TICKET_TTL, guest_uid,
                                   req.nickname.strip()[:24], req.consent):
            break
    else:
        raise HTTPException(503, "登录的人太多了，过一会儿再试")
    return {"ticket": ticket, "code": code, "expires_in": TICKET_TTL,
            "qr_url": wechat.qr_url(), "account_name": wechat.account_name()}


@router.post("/api/auth/wechat/poll")
def wechat_poll(req: WechatPollReq, request: Request):
    """网页每两秒问一次：学生在公众号里发了数字没有。发了就把这次请求换成会话（只能换一次）。
    ticket 放在请求体里，不放 URL，免得进访问日志。"""
    row = store.wechat_ticket(req.ticket)
    if not row or row["used"]:
        raise HTTPException(410, "这次登录已经结束了，请重新开始")
    left = row["expires_at"] - time.time()
    if left <= 0:
        raise HTTPException(410, "数字过期了，请重新开始")
    openid = row["openid"]
    if not openid:
        return {"pending": True, "expires_in": int(left)}
    if not store.wechat_use_ticket(req.ticket):
        raise HTTPException(410, "这次登录已经结束了，请重新开始")

    guest_uid = row["guest_uid"]
    user = store.user_by_openid(openid)
    created = bound = False
    if user:
        uid = user["id"]
    else:
        try:
            if guest_uid and store.bind_wechat(guest_uid, openid):
                uid, bound = guest_uid, True
            else:
                uid, created = store.create_wechat_user(row["nickname"] or "微信用户", openid), True
        except sqlite3.IntegrityError:  # 同一个微信同时登两次：后到的登进先开的那个
            uid = store.user_by_openid(openid)["id"]
    if row["consent"]:
        store.record_consent(uid, PRIVACY_VERSION)
    # 不在这里吊销浏览器原来的令牌：这个结果可能被浏览器当成过期的尝试丢掉（用户已经换了数字或走了访客），
    # 那时原令牌还得能用。前端真正采用新令牌之后，自己调 /api/auth/logout 吊销旧的。
    return {**account(uid), "token": store.create_session(uid, SESSION_TTL),
            "created": created, "bound": bound, "left_guest": bool(guest_uid and guest_uid != uid)}


WELCOME = "欢迎关注启研。登录网页时，把网页上显示的 6 位数字发到这里就行。"


def _on_message(msg: dict[str, str]) -> str | None:
    """公众号收到一条消息该回什么。返回 None 表示不回。"""
    openid = msg.get("FromUserName", "")
    if not openid:
        return None
    if msg.get("MsgType") == "event":
        return WELCOME if msg.get("Event") == "subscribe" else None
    if msg.get("MsgType") != "text":
        return "要登录启研，把网页上显示的 6 位数字发过来就行。"
    digits = re.sub(r"\D", "", unicodedata.normalize("NFKC", msg.get("Content", "")))
    if len(digits) != 6:
        return "要登录启研，把网页上显示的 6 位数字发过来就行。"
    # 先查限额再去配：原来是配完才看限额，超限之后照样能一直猜，限额只换了一句提示。
    # 只记发错的；发对了（包括微信超时重发同一条）不算次数。
    key = f"openid:{openid}"
    if _used_up(key, PER_OPENID_HOUR):
        return "发错的次数太多了，过一会儿再试。"
    if store.wechat_claim_code(digits, openid, msg.get("MsgId", "")):
        return "登录成功，回到网页就好，网页会自己跳转。"
    _note(key)
    return "没找到这个数字。请看网页上显示的 6 位数字，5 分钟内有效。"


@router.get("/api/wechat")
def wechat_verify(signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""):
    """公众号后台「启用服务器配置」时微信来验一次：签名对就原样回 echostr。"""
    if not wechat.check_signature(signature, timestamp, nonce):
        raise HTTPException(403, "签名不对")
    return PlainTextResponse(echostr)


@router.post("/api/wechat")
async def wechat_message(request: Request, signature: str = "", timestamp: str = "", nonce: str = "",
                         msg_signature: str = ""):
    """公众号收到的消息。微信要求五秒内回，所以这里只做一次查库和一次写库。
    安全模式下 parse 会核对签了密文的 msg_signature 并解密；明文模式只核 URL 签名，消息体是不可信的（只给本机调试）。"""
    if not wechat.check_signature(signature, timestamp, nonce):
        raise HTTPException(403, "签名不对")
    if not wechat.configured():  # 只设了 Token、没开安全模式：消息体没签名，不收
        raise HTTPException(403, "公众号要用安全模式（设 WECHAT_AES_KEY）")
    body = await request.body()
    try:
        msg = wechat.parse(body, msg_signature=msg_signature, timestamp=timestamp, nonce=nonce)
    except wechat.SignatureError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    text = await run_in_threadpool(_on_message, msg)  # 要写库、拿写锁，不在事件循环上做
    if text is None:
        return PlainTextResponse("success")  # 微信约定：回 success 表示收到、不回复
    return Response(wechat.reply_text(msg, text), media_type="application/xml")


@router.post("/api/auth/wechat/dev-send")
def wechat_dev_send(req: DevSendReq):
    """本机开发没有公众号：假装某个微信发了这个数字。只有 AUTH_DEV_CODES=1 且没配公众号时存在。"""
    if not _dev():
        raise HTTPException(404, "Not Found")
    return {"reply": _on_message({"FromUserName": req.openid, "ToUserName": "dev", "MsgType": "text",
                                  "Content": req.code, "MsgId": "dev-" + secrets.token_hex(8)})}


@router.post("/api/auth/legacy")
def claim_legacy(req: LegacyReq):
    """有账号之前，浏览器里只存了 uid。这样的老用户凭 uid 认领一次，之后就要凭会话。"""
    if not store.claim_legacy(req.uid):
        raise HTTPException(401, "这个旧账号已经认领过或不存在，请重新登录")
    return {**account(req.uid), "token": store.create_session(req.uid, SESSION_TTL)}


@router.get("/api/auth/me")
def whoami(request: Request):
    return account(me(request))


@router.post("/api/auth/consent")
def consent(req: ConsentReq, request: Request):
    if req.version != PRIVACY_VERSION:
        raise HTTPException(409, "隐私说明更新了，请刷新页面再看一遍")
    store.record_consent(me(request), PRIVACY_VERSION)
    return account(me(request))


@router.post("/api/auth/logout")
def logout(request: Request):
    store.drop_session(_bearer(request))
    return {"ok": True}


@router.get("/api/me/export")
def export_mine(request: Request):
    data = store.export_user(me(request))
    return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=qiyan-my-data.json"})


@router.delete("/api/me")
def delete_mine(request: Request):
    """真删：每张表里这个人的行、所有会话、账号本身。备份按 tools/backup_db.py 的保留期自然过期。"""
    return {"ok": True, "deleted": store.delete_user(me(request))}
