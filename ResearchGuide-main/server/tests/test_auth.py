# -*- coding: utf-8 -*-
"""账号：默认要登录、uid 必须是本人、微信登录（公众号消息）、访客绑微信、老用户认领、删号与导出。

这里用真的 auth.guard（其余测试把它换成「信请求里的 uid」）。不联网：
微信服务器那一侧由测试自己签名、拼 XML，直接打 /api/wechat。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth
SERVER = Path(__file__).resolve().parent.parent
PRIVACY_DIGEST = "ac4b619a78fa"  # web/js/app.js 里 PRIVACY_HTML 的指纹，见最后一个测试
TOKEN = "test-token"
OPENID = "o_test_user_1"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "auth.db")
    store.init_db()
    auth._HITS.clear()
    for k in ("AUTH_DEV_CODES", "TRUST_PROXY", "WECHAT_TOKEN", "WECHAT_ACCOUNT_ID", "WECHAT_QR_URL",
              "WECHAT_ACCOUNT_NAME"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def wx(monkeypatch):
    """开通了公众号：消息接口用 TOKEN 验签。明文模式要显式允许（只给本机调试）；安全模式见 wx_secure。"""
    monkeypatch.setenv("WECHAT_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_ALLOW_PLAINTEXT", "1")
    monkeypatch.setenv("WECHAT_ACCOUNT_ID", "gh_test")
    monkeypatch.setenv("WECHAT_ACCOUNT_NAME", "启研")


def _signed(ts: str | None = None, nonce: str = "n1", token: str = TOKEN) -> str:
    ts = str(int(time.time())) if ts is None else ts
    sig = hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()
    return f"signature={sig}&timestamp={ts}&nonce={nonce}"


_MSG_SEQ = iter(range(10 ** 9, 2 * 10 ** 9))


def _xml(openid: str, content: str | None = None, event: str | None = None, msg_id: str | None = None) -> bytes:
    """一条用户消息。每条默认一个新 MsgId；要模拟微信重发同一条，就传同一个 msg_id。"""
    head = (f"<xml><ToUserName><![CDATA[gh_test]]></ToUserName><FromUserName><![CDATA[{openid}]]></FromUserName>"
            "<CreateTime>1700000000</CreateTime>")
    if event:
        return (head + f"<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[{event}]]></Event></xml>").encode()
    mid = msg_id or str(next(_MSG_SEQ))
    return (head + f"<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[{content}]]></Content>"
            f"<MsgId>{mid}</MsgId></xml>").encode()


def _send(client, openid: str, content: str, query: str | None = None, msg_id: str | None = None):
    """假装微信服务器把一条用户消息推给我们。"""
    return client.post(f"/api/wechat?{query or _signed()}", content=_xml(openid, content, msg_id=msg_id),
                       headers={"Content-Type": "text/xml"})


def _reply(r) -> dict[str, str]:
    return {c.tag: c.text or "" for c in ET.fromstring(r.content)}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _guest(client, nick="小北") -> tuple[str, str]:
    r = client.post("/api/auth/login", json={"nickname": nick, "consent": True}).json()
    return r["uid"], r["token"]


def _wechat_login(client, openid=OPENID, headers=None, **start):
    s = client.post("/api/auth/wechat/start", json={"consent": True, **start}, headers=headers or {})
    assert s.status_code == 200, s.text
    s = s.json()
    assert "登录成功" in _reply(_send(client, openid, s["code"]))["Content"]
    r = client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 默认要登录 ----------

def _routes() -> dict[str, set[str]]:
    """所有接口：路由模板 → 方法。用 OpenAPI 列表而不是 app.routes——新版 FastAPI 把 include_router 的路由包了一层。"""
    return {path: {m.upper() for m in ops} for path, ops in main.app.openapi()["paths"].items()}


def test_every_private_route_refuses_requests_without_a_session(client):
    checked = 0
    for path, methods in _routes().items():
        if path in auth.PUBLIC:
            continue
        url = path.replace("{", "").replace("}", "")  # 路径参数随便填
        for method in methods:
            r = client.request(method, url)
            assert r.status_code == 401, f"{method} {path} 没登录也进去了：{r.status_code}"
            checked += 1
    assert checked > 50


def test_public_list_only_names_routes_that_exist():
    assert auth.PUBLIC <= set(_routes()), sorted(auth.PUBLIC - set(_routes()))


def test_uid_in_query_body_or_path_must_be_the_session_owner(client):
    a, ta = _guest(client, "甲")
    b, _ = _guest(client, "乙")
    assert client.get(f"/api/me/facts?uid={a}", headers=_h(ta)).status_code == 200
    assert client.get(f"/api/me/facts?uid={b}", headers=_h(ta)).status_code == 403
    assert client.get(f"/api/me/facts?uid={a}").status_code == 401
    assert client.get(f"/api/me/facts?uid={a}", headers=_h("qy_not-a-session")).status_code == 401
    edge = {"uid": b, "kind": "language", "text": "粤语母语", "evidence_url": ""}
    assert client.post("/api/edges", json=edge, headers=_h(ta)).status_code == 403
    assert client.post("/api/edges", json={**edge, "uid": a}, headers=_h(ta)).status_code == 200


def test_task_detail_is_only_visible_to_its_owner(client):
    a, ta = _guest(client, "甲")
    _, tb = _guest(client, "乙")
    t = client.post("/api/tasks/generate", json={"uid": a, "direction": "ai", "level": 1}, headers=_h(ta)).json()
    assert client.get(f"/api/tasks/{t['id']}", headers=_h(ta)).status_code == 200
    assert client.get(f"/api/tasks/{t['id']}", headers=_h(tb)).status_code == 404


def test_upload_route_is_not_read_by_the_guard(client):
    a, ta = _guest(client)
    r = client.post(f"/api/projects/nope/submit?uid={a}", content=b"PK\x03\x04" + b"x" * 1000,
                    headers={**_h(ta), "Content-Type": "application/zip"})
    assert r.status_code == 404  # 过了登录，接口自己说项目不存在


# ---------- 微信登录 ----------

def test_server_verification_echoes_only_with_a_valid_signature(client, wx):
    assert client.get(f"/api/wechat?{_signed()}&echostr=hello").text == "hello"
    assert client.get(f"/api/wechat?{_signed(token='wrong')}&echostr=hello").status_code == 403
    stale = str(int(time.time()) - 3600)
    assert client.get(f"/api/wechat?{_signed(ts=stale)}&echostr=hello").status_code == 403
    assert client.get("/api/wechat?echostr=hello").status_code == 403


def test_wechat_login_end_to_end_and_a_second_device_gets_the_same_account(client, wx):
    s = client.post("/api/auth/wechat/start", json={"consent": True, "nickname": "小北"}).json()
    assert re.fullmatch(r"\d{6}", s["code"]) and s["expires_in"] == auth.TICKET_TTL
    assert s["qr_url"] == "https://open.weixin.qq.com/qr/code?username=gh_test" and s["account_name"] == "启研"
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["pending"] is True
    reply = _reply(_send(client, OPENID, f" {s['code'][:3]} {s['code'][3:]} "))  # 中间带空格也认
    assert reply["ToUserName"] == OPENID and reply["FromUserName"] == "gh_test" and "登录成功" in reply["Content"]
    first = client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()
    assert first["created"] and first["wechat"] and not first["guest"] and first["nickname"] == "小北"
    assert first["consent_ok"]
    again = _wechat_login(client)
    assert again["uid"] == first["uid"] and not again["created"] and again["token"] != first["token"]


def test_full_width_digits_are_accepted(client, wx):
    s = client.post("/api/auth/wechat/start", json={}).json()
    full = s["code"].translate({ord(c): ord(c) + 0xFEE0 for c in "0123456789"})
    assert "登录成功" in _reply(_send(client, OPENID, full))["Content"]


def test_unsigned_stale_or_hostile_messages_change_nothing(client, wx):
    s = client.post("/api/auth/wechat/start", json={}).json()
    assert _send(client, OPENID, s["code"], query=_signed(token="wrong")).status_code == 403
    assert _send(client, OPENID, s["code"], query=_signed(ts=str(int(time.time()) - 3600))).status_code == 403
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xml><FromUserName>&a;</FromUserName></xml>'
    assert client.post(f"/api/wechat?{_signed()}", content=evil).status_code == 400
    assert client.post(f"/api/wechat?{_signed()}", content=b"<xml><oops").status_code == 400
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["pending"] is True


def test_a_wrong_number_logs_nobody_in_and_guessing_is_limited(client, wx, monkeypatch):
    monkeypatch.setattr(auth, "PER_OPENID_HOUR", 3)
    s = client.post("/api/auth/wechat/start", json={}).json()
    wrong = "000000" if s["code"] != "000000" else "111111"
    replies = [_reply(_send(client, "o_attacker", wrong))["Content"] for _ in range(4)]
    assert all("没找到" in r for r in replies[:3]) and "太多" in replies[3]
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["pending"] is True
    assert "6 位数字" in _reply(_send(client, OPENID, "你好"))["Content"]


def test_ticket_is_single_use_and_expires(client, wx):
    s = client.post("/api/auth/wechat/start", json={}).json()
    _send(client, OPENID, s["code"])
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).status_code == 200
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).status_code == 410
    assert "没找到" in _reply(_send(client, OPENID, s["code"]))["Content"]  # 用过的数字不能再配

    s = client.post("/api/auth/wechat/start", json={}).json()
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("UPDATE wechat_tickets SET expires_at=?", (time.time() - 1,))
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).status_code == 410
    assert "没找到" in _reply(_send(client, OPENID, s["code"]))["Content"]
    assert client.post("/api/auth/wechat/poll", json={"ticket": "made-up"}).status_code == 410

    s = client.post("/api/auth/wechat/start", json={}).json()
    _send(client, OPENID, s["code"])
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("UPDATE wechat_tickets SET expires_at=?", (time.time() - 1,))
        c.commit()
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).status_code == 410


def test_wechat_retries_are_idempotent_but_another_sender_cannot_take_over(client, wx):
    s = client.post("/api/auth/wechat/start", json={}).json()
    assert "登录成功" in _reply(_send(client, OPENID, s["code"], msg_id="777"))["Content"]
    assert "登录成功" in _reply(_send(client, OPENID, s["code"], msg_id="777"))["Content"]  # 微信超时重发同一条
    assert "没找到" in _reply(_send(client, "o_someone_else", s["code"]))["Content"]
    r = client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()
    assert store.user_by_openid(OPENID)["id"] == r["uid"]


def test_follow_event_gets_a_welcome_and_other_events_are_silent(client, wx):
    r = client.post(f"/api/wechat?{_signed()}", content=_xml(OPENID, event="subscribe"))
    assert "6 位数字" in _reply(r)["Content"]
    r = client.post(f"/api/wechat?{_signed()}", content=_xml(OPENID, event="unsubscribe"))
    assert r.text == "success"


def test_waiting_codes_never_collide(client, wx, monkeypatch):
    seq = iter([42, 42, 43])
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: next(seq))
    a = client.post("/api/auth/wechat/start", json={}).json()
    b = client.post("/api/auth/wechat/start", json={}).json()
    assert (a["code"], b["code"]) == ("000042", "000043")


def test_per_source_limit_uses_forwarded_address_only_behind_a_proxy(client, monkeypatch):
    monkeypatch.setattr(auth, "PER_IP_HOUR", 2)
    fwd = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    for _ in range(3):  # 没开 TRUST_PROXY：不认转发头，测试客户端不是公网地址，不限
        assert client.post("/api/auth/login", json={"nickname": "x"}, headers=fwd).status_code == 200
    monkeypatch.setenv("TRUST_PROXY", "1")
    assert client.post("/api/auth/login", json={"nickname": "x"}, headers=fwd).status_code == 200
    assert client.post("/api/auth/login", json={"nickname": "x"}, headers=fwd).status_code == 200
    assert client.post("/api/auth/login", json={"nickname": "x"}, headers=fwd).status_code == 429
    other = {"X-Forwarded-For": "198.51.100.9"}
    assert client.post("/api/auth/login", json={"nickname": "x"}, headers=other).status_code == 200


def test_without_a_wechat_account_login_is_refused_and_dev_mode_can_simulate(client, monkeypatch):
    assert client.post("/api/auth/wechat/start", json={}).status_code == 503
    assert client.get("/api/health").json()["auth"]["wechat_login"] is False
    assert client.post("/api/auth/wechat/dev-send", json={"code": "123456"}).status_code == 404

    monkeypatch.setenv("AUTH_DEV_CODES", "1")
    assert client.get("/api/health").json()["auth"]["wechat_login"] is True
    s = client.post("/api/auth/wechat/start", json={}).json()
    assert "登录成功" in client.post("/api/auth/wechat/dev-send", json={"code": s["code"]}).json()["reply"]
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["wechat"] is True

    monkeypatch.setenv("WECHAT_TOKEN", TOKEN)  # 配了公众号（安全模式），开发捷径自动关掉
    monkeypatch.setenv("WECHAT_AES_KEY", AES_KEY)
    monkeypatch.setenv("WECHAT_APP_ID", APP_ID)
    assert client.post("/api/auth/wechat/dev-send", json={"code": "123456"}).status_code == 404


# ---------- 安全模式：消息体加密并签名 ----------

AES_KEY = base64.b64encode(bytes(range(32))).decode().rstrip("=")  # 43 位，和后台的 EncodingAESKey 一样
APP_ID = "wx_test_app"


@pytest.fixture
def wx_secure(wx, monkeypatch):
    monkeypatch.setenv("WECHAT_AES_KEY", AES_KEY)
    monkeypatch.setenv("WECHAT_APP_ID", APP_ID)


def _seal(xml: bytes, appid: str = APP_ID, key_b64: str = AES_KEY) -> str:
    """按微信文档自己拼一遍密文（不用 wechat.encrypt），好让测试独立于被测代码。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = base64.b64decode(key_b64 + "=")
    plain = b"\x01" * 16 + struct.pack("!I", len(xml)) + xml + appid.encode()
    pad = 32 - len(plain) % 32
    plain += bytes([pad]) * pad
    e = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(e.update(plain) + e.finalize()).decode()


def _open(enc: str) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = base64.b64decode(AES_KEY + "=")
    d = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = d.update(base64.b64decode(enc)) + d.finalize()
    plain = plain[:-plain[-1]]
    (n,) = struct.unpack("!I", plain[16:20])
    assert plain[20 + n:] == APP_ID.encode()
    return {c.tag: c.text or "" for c in ET.fromstring(plain[20:20 + n])}


def _send_secure(client, openid: str, content: str, *, enc: str | None = None, msg_sig: str | None = None,
                 nonce: str = "n2"):
    ts = str(int(time.time()))
    enc = enc if enc is not None else _seal(_xml(openid, content))
    msg_sig = msg_sig if msg_sig is not None else hashlib.sha1("".join(sorted([TOKEN, ts, nonce, enc])).encode()).hexdigest()
    body = f"<xml><ToUserName><![CDATA[gh_test]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()
    return client.post(f"/api/wechat?{_signed(ts=ts, nonce=nonce)}&encrypt_type=aes&msg_signature={msg_sig}",
                       content=body, headers={"Content-Type": "text/xml"})


def test_secure_mode_logs_in_and_replies_encrypted(client, wx_secure):
    s = client.post("/api/auth/wechat/start", json={"consent": True}).json()
    r = _send_secure(client, OPENID, s["code"])
    assert r.status_code == 200, r.text
    outer = {c.tag: c.text or "" for c in ET.fromstring(r.content)}
    assert set(outer) == {"Encrypt", "MsgSignature", "TimeStamp", "Nonce"}  # 回复里没有明文
    expect = hashlib.sha1("".join(sorted([TOKEN, outer["TimeStamp"], outer["Nonce"], outer["Encrypt"]])).encode()).hexdigest()
    assert outer["MsgSignature"] == expect
    inner = _open(outer["Encrypt"])
    assert inner["ToUserName"] == OPENID and "登录成功" in inner["Content"]
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["wechat"] is True


def test_secure_mode_rejects_plaintext_forged_and_foreign_messages(client, wx_secure):
    s = client.post("/api/auth/wechat/start", json={}).json()
    # 明文消息：安全模式下一律不收（这正是明文模式的漏洞：消息体没签名）
    assert _send(client, OPENID, s["code"]).status_code == 400
    # 密文签名不对
    assert _send_secure(client, OPENID, s["code"], msg_sig="0" * 40).status_code == 403
    # 没带 msg_signature
    assert _send_secure(client, OPENID, s["code"], msg_sig="").status_code == 403
    # 用别的 AppID 加密的（发给别的公众号的消息）
    other = _seal(_xml(OPENID, s["code"]), appid="wx_other")
    assert _send_secure(client, OPENID, s["code"], enc=other).status_code == 400
    # 用别的密钥加密的
    wrong_key = base64.b64encode(bytes(32)).decode().rstrip("=")
    bad = _seal(_xml(OPENID, s["code"]), key_b64=wrong_key)
    assert _send_secure(client, OPENID, s["code"], enc=bad).status_code == 400
    assert _send_secure(client, OPENID, s["code"], enc="not base64!").status_code == 400
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["pending"] is True


def test_secure_mode_round_trip_and_key_validation(wx_secure, monkeypatch):
    import wechat
    assert wechat.decrypt(wechat.encrypt(b"<xml><a>1</a></xml>")) == b"<xml><a>1</a></xml>"
    monkeypatch.setenv("WECHAT_AES_KEY", "too-short")
    with pytest.raises(ValueError):
        wechat.decrypt(_seal(b"<xml/>"))


# ---------- 访客、老用户、会话 ----------

def test_guest_binding_wechat_keeps_the_guest_data(client, wx):
    uid, token = _guest(client)
    client.post("/api/edges", json={"uid": uid, "kind": "language", "text": "粤语母语", "evidence_url": ""},
                headers=_h(token))
    r = _wechat_login(client, headers=_h(token))
    assert r["bound"] and r["uid"] == uid and not r["left_guest"]
    # 轮询不吊销原来的令牌：结果可能被浏览器当成过期尝试丢掉，那时原令牌还得能用
    assert client.get("/api/auth/me", headers=_h(token)).status_code == 200
    # 浏览器真正采用新令牌之后自己吊销旧的（app.js 的 revokeToken）
    assert client.post("/api/auth/logout", headers=_h(token)).status_code == 200
    assert client.get("/api/auth/me", headers=_h(token)).status_code == 401
    assert client.get("/api/auth/me", headers=_h(r["token"])).status_code == 200
    edges = client.get(f"/api/edges?uid={uid}", headers=_h(r["token"])).json()
    assert "粤语母语" in str(edges)


def test_guest_logging_into_an_existing_wechat_switches_account_and_says_so(client, wx):
    owner = _wechat_login(client)
    _, guest_token = _guest(client)
    r = _wechat_login(client, headers=_h(guest_token))
    assert r["uid"] == owner["uid"] and r["left_guest"] and not r["bound"]


def test_pre_account_users_can_claim_their_uid_exactly_once(client):
    old = store.create_user("老用户")["uid"]
    with sqlite3.connect(store.DB_PATH) as c:  # 加账号之前建的号没有 claimed_at
        c.execute("UPDATE users SET claimed_at=NULL WHERE id=?", (old,))
    r = client.post("/api/auth/legacy", json={"uid": old})
    assert r.status_code == 200 and r.json()["uid"] == old
    assert client.get(f"/api/me/facts?uid={old}", headers=_h(r.json()["token"])).status_code == 200
    assert client.post("/api/auth/legacy", json={"uid": old}).status_code == 401
    fresh, _ = _guest(client)  # 新号一出生就算认领过，知道 uid 也认领不了
    assert client.post("/api/auth/legacy", json={"uid": fresh}).status_code == 401


def test_logout_and_expiry_end_the_session(client):
    uid, token = _guest(client)
    assert client.get("/api/auth/me", headers=_h(token)).status_code == 200
    client.post("/api/auth/logout", headers=_h(token))
    assert client.get("/api/auth/me", headers=_h(token)).status_code == 401
    _, token = _guest(client)
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert client.get("/api/auth/me", headers=_h(token)).status_code == 401


def test_sessions_store_only_a_hash_of_the_token(client):
    _, token = _guest(client)
    with sqlite3.connect(store.DB_PATH) as c:
        dump = "\n".join(c.iterdump())
    assert token not in dump


def test_consent_is_recorded_against_the_current_privacy_version(client):
    r = client.post("/api/auth/login", json={"nickname": "x"}).json()
    assert r["consent_ok"] is False
    assert client.post("/api/auth/consent", json={"version": "2000-01-01"}, headers=_h(r["token"])).status_code == 409
    ok = client.post("/api/auth/consent", json={"version": auth.PRIVACY_VERSION}, headers=_h(r["token"])).json()
    assert ok["consent_ok"] is True


# ---------- 删号与导出 ----------

def test_delete_account_removes_every_row_and_the_session(client, wx, capsys):
    r = _wechat_login(client)
    uid, h = r["uid"], _h(r["token"])
    client.post("/api/edges", json={"uid": uid, "kind": "language", "text": "粤语母语", "evidence_url": ""}, headers=h)
    client.post("/api/tasks/generate", json={"uid": uid, "direction": "ai", "level": 1}, headers=h)
    client.post("/api/onboard/start", json={"uid": uid}, headers=h)
    store.save_tutor(uid, "llm-eval", "2009.03300", "active", {"rev": 1, "slots": []}, None)
    store.put_tutor_questions("2009.03300", "abstract", "sha", 1, "sk", [{"qid": "q"}])
    other, _ = _guest(client, "别人")

    exported = client.get("/api/me/export", headers=h)
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    data = exported.json()
    assert data["user"]["wechat_openid"] == OPENID and data["edges"] and data["tasks"]
    assert data["tutor_sessions"] and "tutor_questions" not in data
    assert "sessions" not in data and "token" not in data["user"]

    out = client.delete("/api/me", headers=h).json()
    assert out["ok"] and out["deleted"]["users"] == 1
    logged = capsys.readouterr().out
    assert '"event": "user_deleted"' in logged and uid in logged
    with sqlite3.connect(store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        for t in store._user_tables(c):
            assert c.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (uid,)).fetchone()[0] == 0, t
        assert c.execute("SELECT COUNT(*) FROM wechat_tickets WHERE openid=?", (OPENID,)).fetchone()[0] == 0
    assert store.get_user(other)  # 别人不受影响
    assert client.get("/api/auth/me", headers=h).status_code == 401
    again = _wechat_login(client)  # 同一个微信可以重新注册，是个新号
    assert again["created"] and again["uid"] != uid


# ---------- 库文件的位置 ----------

def test_db_path_follows_qiyan_db(tmp_path):
    target = tmp_path / "vol" / "qiyan.db"
    out = subprocess.run([sys.executable, "-c", "import store; print(store.DB_PATH)"], cwd=SERVER,
                         env={**os.environ, "QIYAN_DB": str(target)}, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(target)


# ---------- 备份 ----------

def test_backup_copies_a_consistent_snapshot_and_prunes_old_ones(tmp_path):
    uid = store.create_user("备份")["uid"]
    out = tmp_path / "bk"
    out.mkdir()
    old = out / "qiyan-20000101-000000.db"
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    sys.path.insert(0, str(SERVER.parent / "tools"))
    import backup_db
    dest = backup_db.backup(store.DB_PATH, out, 7)
    assert not old.exists() and dest.exists() and not list(out.glob("*.part"))
    with sqlite3.connect(dest) as c:
        assert c.execute("SELECT nickname FROM users WHERE id=?", (uid,)).fetchone()[0] == "备份"


# ---------- 前端 ----------

WEB = SERVER.parent / "web"


def test_every_frontend_request_goes_through_the_authenticated_helper():
    for name in ("app.js", "chat.js", "directions.js"):
        js = (WEB / "js" / name).read_text(encoding="utf-8")
        raw = re.findall(r"(?<![\w.])fetch\(", js)
        # 只有 apiFetch 自己里面那一处直接 fetch；别处直接 fetch 会漏掉会话令牌和接口地址
        assert len(raw) == (1 if name == "app.js" else 0), f"{name} 里有绕过 apiFetch 的请求"
        assert "/api/" not in "".join(re.findall(r"(?:href|src)=[\"'`][^\"'`]*", js)), f"{name} 用链接直接打接口，带不上令牌"


def test_new_account_classes_have_rules():
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    for cls in ("login-step", "login-nick", "login-note", "consent-row", "privacy-box", "privacy-body",
                "account-panel", "account-actions", "wx-box", "wx-qr", "wx-steps", "wx-code", "wx-status"):
        assert re.search(rf"\.{re.escape(cls)}(?![\w-])", css), cls


def test_privacy_text_and_version_change_together():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    text = re.search(r"const PRIVACY_HTML = `(.*?)`;", js, re.S).group(1)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    # 改了隐私说明：把 auth.PRIVACY_VERSION 换成当天日期（老用户会再同意一次），再把这里的指纹和日期一起更新
    assert (auth.PRIVACY_VERSION, digest) == ("2026-10-15", PRIVACY_DIGEST), digest


def test_sessions_slide_forward_when_used_after_a_day(client):
    _, token = _guest(client)
    with sqlite3.connect(store.DB_PATH) as c:
        c.execute("UPDATE sessions SET last_seen=?, expires_at=?", (time.time() - 2 * 86400, time.time() + 3600))
    assert client.get("/api/auth/me", headers=_h(token)).status_code == 200
    auth._TOUCH.submit(lambda: None).result()  # 等续期那个线程做完
    with sqlite3.connect(store.DB_PATH) as c:
        expires = c.execute("SELECT expires_at FROM sessions").fetchone()[0]
    assert expires > time.time() + auth.SESSION_TTL - 60


# ---------- Codex 复查第一轮 ----------

def test_uid_check_does_not_depend_on_the_content_type_header(client):
    a, ta = _guest(client, "甲")
    b, _ = _guest(client, "乙")
    edge = json.dumps({"uid": b, "kind": "language", "text": "冒充", "evidence_url": ""}).encode()
    for ctype in ("application/JSON", "Application/Json; charset=utf-8", "application/merge-patch+json", None):
        headers = {**_h(ta), **({"Content-Type": ctype} if ctype else {})}
        r = client.post("/api/edges", content=edge, headers=headers)
        assert r.status_code == 403, (ctype, r.status_code, r.text)
    with sqlite3.connect(store.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM edges WHERE user_id=?", (b,)).fetchone()[0] == 0
    # 查询串里同名参数写两遍：每一个都要核
    assert client.get(f"/api/me/facts?uid={a}&uid={b}", headers=_h(ta)).status_code == 403
    assert client.get(f"/api/me/facts?uid={b}&uid={a}", headers=_h(ta)).status_code == 403


def test_guessing_limit_blocks_even_a_correct_code(client, wx, monkeypatch):
    monkeypatch.setattr(auth, "PER_OPENID_HOUR", 3)
    s = client.post("/api/auth/wechat/start", json={}).json()
    wrong = "000000" if s["code"] != "000000" else "111111"
    for _ in range(3):
        assert "没找到" in _reply(_send(client, "o_guesser", wrong))["Content"]
    # 超限之后，就算猜中了也不能配上（原来是先配再看限额）
    assert "太多" in _reply(_send(client, "o_guesser", s["code"]))["Content"]
    assert client.post("/api/auth/wechat/poll", json={"ticket": s["ticket"]}).json()["pending"] is True
    # 限额只针对这个发送者；真正的学生照样能登
    assert "登录成功" in _reply(_send(client, OPENID, s["code"], msg_id="555"))["Content"]
    # 发对了不计次数：同一条消息微信重发也还是成功
    assert "登录成功" in _reply(_send(client, OPENID, s["code"], msg_id="555"))["Content"]


def test_writes_for_a_deleted_account_are_refused(client, monkeypatch):
    uid, token = _guest(client)
    with sqlite3.connect(store.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        tables = store._user_tables(c)
        triggers = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {f"trg_{t}_not_deleted" for t in tables} <= triggers  # 每张带 user_id 的表都有
    assert client.delete("/api/me", headers=_h(token)).json()["ok"]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_session(uid, 60)
    # 删号那一刻已经过了登录校验和「用户存在」检查、还在路上的请求（比如在等模型回话的对话）：
    # 写库被拒，回 410，数据不会被写回来。把两道检查替换成「已经通过」来模拟它。
    monkeypatch.setattr(auth, "_session_uid", lambda request: uid)
    monkeypatch.setattr(main, "_user_or_404", lambda u: {"id": u})
    r = client.post("/api/edges", json={"uid": uid, "kind": "language", "text": "迟到的写入", "evidence_url": ""},
                    headers=_h("qy_anything"))
    assert r.status_code == 410
    with sqlite3.connect(store.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM edges WHERE user_id=?", (uid,)).fetchone()[0] == 0
    # 别人照常能写
    other, to = _guest(client, "别人")
    monkeypatch.setattr(auth, "_session_uid", lambda request: other)
    r = client.post("/api/edges", json={"uid": other, "kind": "language", "text": "正常", "evidence_url": ""},
                    headers=_h(to))
    assert r.status_code == 200


def test_qiyan_db_in_the_env_file_is_honoured(tmp_path):
    target = tmp_path / "vol" / "from-env-file.db"
    envf = tmp_path / ".env"
    envf.write_text(f"# 注释\nQIYAN_DB={target}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "QIYAN_DB"}
    env["QIYAN_ENV_FILE"] = str(envf)
    # 按 main 的 import 顺序（auth → store 在 llm 之前）
    out = subprocess.run([sys.executable, "-c", "import main, store; print(store.DB_PATH)"], cwd=SERVER,
                         env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == str(target)


def test_backup_cli_uses_qiyan_db_from_the_env_file(tmp_path):
    db = tmp_path / "real.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (42)")
    envf = tmp_path / ".env"
    envf.write_text(f"QIYAN_DB={db}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "QIYAN_DB"}
    env["QIYAN_ENV_FILE"] = str(envf)
    out = subprocess.run([sys.executable, str(SERVER.parent / "tools" / "backup_db.py"), "--out", str(tmp_path / "bk")],
                         env=env, capture_output=True, text=True, check=True)
    dest = Path(out.stdout.strip().splitlines()[-1])
    with sqlite3.connect(dest) as c:
        assert c.execute("SELECT x FROM t").fetchone()[0] == 42  # 备份的是 .env 指的那个库，不是默认的演示库


def test_token_alone_is_not_enough_for_wechat_login(client, monkeypatch):
    monkeypatch.setenv("WECHAT_TOKEN", TOKEN)  # 只设了 Token：明文模式，消息体能伪造
    assert client.get("/api/health").json()["auth"]["wechat_login"] is False
    assert client.post("/api/auth/wechat/start", json={}).status_code == 503
    assert _send(client, OPENID, "123456").status_code == 403


def test_a_replayed_message_cannot_claim_a_new_login(client, wx, monkeypatch):
    seq = iter([42, 42])
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: next(seq))
    first = client.post("/api/auth/wechat/start", json={}).json()
    assert "登录成功" in _reply(_send(client, "o_victim", first["code"], msg_id="9001"))["Content"]
    assert client.post("/api/auth/wechat/poll", json={"ticket": first["ticket"]}).status_code == 200
    # 攻击者拿到那条签过名的消息，开一个同号的新请求，然后原样重放
    second = client.post("/api/auth/wechat/start", json={}).json()
    assert second["code"] == first["code"]
    assert "没找到" in _reply(_send(client, "o_victim", second["code"], msg_id="9001"))["Content"]
    assert client.post("/api/auth/wechat/poll", json={"ticket": second["ticket"]}).json()["pending"] is True
