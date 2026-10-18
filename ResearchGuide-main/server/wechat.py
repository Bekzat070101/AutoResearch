# -*- coding: utf-8 -*-
"""公众号消息接口：验签、解密、解析、被动回复。微信登录的流程在 auth.py。

为什么走公众号消息而不是「微信扫码登录」：网站扫码登录要微信开放平台的企业认证和备案域名，
公众号网页授权要认证服务号，个人都拿不到；个人订阅号可以开「服务器配置」收用户发来的消息，
消息里带发信人的 openid（只对这个公众号有效的编号），够我们认出是谁。

两种模式：
- 安全模式（线上必须用）：消息体是 AES 密文，msg_signature 把密文也签进去，所以消息体是可信的。
  要设 WECHAT_AES_KEY（后台的 EncodingAESKey，43 位）和 WECHAT_APP_ID。回复也要加密。
- 明文模式（只给本机调试）：微信的 signature 只签 token、timestamp、nonce，不签消息体。
  谁拿到一条签过名的回调 URL（访问日志里就有），五分钟内就能伪造「某个 openid 发了某个数字」。

公众号后台：设置与开发 → 基本配置 → 服务器配置。URL 填 <接口地址>/api/wechat，Token 填 WECHAT_TOKEN，
EncodingAESKey 点「随机生成」后抄到 WECHAT_AES_KEY，消息加解密方式选「安全模式」，启用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

MAX_BODY = 16 * 1024   # 文本消息几百字节；再大就不是微信发来的
MAX_SKEW = 300         # 时间戳差五分钟以上的当作重放


class SignatureError(ValueError):
    """签名不对：回 403，而不是 400。"""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def token() -> str:
    return _env("WECHAT_TOKEN")


def plaintext_allowed() -> bool:
    """明文模式消息体没签名、能伪造，只给本机调试：要显式设 WECHAT_ALLOW_PLAINTEXT=1。"""
    return _env("WECHAT_ALLOW_PLAINTEXT") == "1"


def configured() -> bool:
    """能收微信登录：有 Token，并且是安全模式（或者显式允许了明文）。只设了 Token 不算开通——那样谁都能伪造消息。"""
    return bool(token()) and (secure() or plaintext_allowed())


def secure() -> bool:
    return bool(_env("WECHAT_AES_KEY"))


def _aes_key() -> bytes:
    raw = _env("WECHAT_AES_KEY")
    try:
        key = base64.b64decode(raw + "=", validate=True)
    except ValueError as exc:
        raise ValueError("WECHAT_AES_KEY 不是后台给的 43 位 EncodingAESKey") from exc
    if len(key) != 32:
        raise ValueError("WECHAT_AES_KEY 不是后台给的 43 位 EncodingAESKey")
    return key


def _cipher(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # 安全模式要 cryptography；requirements.txt 里有，部署包别漏
        raise RuntimeError("安全模式需要 cryptography 包：pip install cryptography") from exc
    return Cipher(algorithms.AES(key), modes.CBC(key[:16]))


def check_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """微信的 URL 签名：token、timestamp、nonce 三个字符串排序后拼起来取 SHA1。另外拒绝太旧的时间戳。"""
    tok = token()
    if not tok or not signature or not timestamp.isdigit():
        return False
    if abs(time.time() - int(timestamp)) > MAX_SKEW:
        return False
    expect = hashlib.sha1("".join(sorted([tok, timestamp, nonce])).encode()).hexdigest()
    return hmac.compare_digest(expect, signature)


def _msg_signature(timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([token(), timestamp, nonce, encrypt])).encode()).hexdigest()


def decrypt(encrypt: str) -> bytes:
    """密文 → 里面的消息 XML。格式：16 字节随机 + 4 字节长度（大端）+ 消息 + AppID，PKCS#7 补到 32 的倍数。"""
    key = _aes_key()
    try:
        data = base64.b64decode(encrypt, validate=True)
    except ValueError as exc:
        raise ValueError("密文不是 base64") from exc
    if not data or len(data) % 16:
        raise ValueError("密文长度不对")
    d = _cipher(key).decryptor()
    plain = d.update(data) + d.finalize()
    pad = plain[-1]
    if not 1 <= pad <= 32 or plain[-pad:] != bytes([pad]) * pad:
        raise ValueError("解密失败")
    plain = plain[:-pad]
    if len(plain) < 20:
        raise ValueError("解密失败")
    (length,) = struct.unpack("!I", plain[16:20])
    xml, appid = plain[20:20 + length], plain[20 + length:].decode("utf-8", "replace")
    if len(xml) != length or not hmac.compare_digest(appid, _env("WECHAT_APP_ID")):
        raise ValueError("不是发给这个公众号的消息")
    return xml


def encrypt(xml: bytes) -> str:
    key = _aes_key()
    plain = secrets.token_bytes(16) + struct.pack("!I", len(xml)) + xml + _env("WECHAT_APP_ID").encode()
    pad = 32 - len(plain) % 32
    plain += bytes([pad]) * pad
    e = _cipher(key).encryptor()
    return base64.b64encode(e.update(plain) + e.finalize()).decode()


def _fields(body: bytes) -> dict[str, str]:
    """带 DOCTYPE / ENTITY 的一律不解析（防实体展开）；解析失败抛 ValueError。"""
    if len(body) > MAX_BODY or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("不像微信的消息")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("XML 解析失败") from exc
    return {child.tag: (child.text or "") for child in root}


def parse(body: bytes, *, msg_signature: str = "", timestamp: str = "", nonce: str = "") -> dict[str, str]:
    """消息 XML → {ToUserName, FromUserName, MsgType, Content, Event, MsgId…}。
    安全模式下只认密文：先核 msg_signature（签了密文），再解密；明文字段一概不信。"""
    outer = _fields(body)
    if not secure():
        return outer
    encrypted = outer.get("Encrypt", "")
    if not encrypted:
        raise ValueError("只收安全模式的加密消息，请在公众号后台把消息加解密方式改成「安全模式」")
    if not msg_signature or not hmac.compare_digest(_msg_signature(timestamp, nonce, encrypted), msg_signature):
        raise SignatureError("消息签名不对")
    return _fields(decrypt(encrypted))


def _cdata(s: str) -> str:
    return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def reply_text(msg: dict[str, str], content: str) -> str:
    """被动回复一条文本：收发人对调。安全模式下整条回复加密，再签名。"""
    plain = ("<xml>"
             f"<ToUserName>{_cdata(msg.get('FromUserName', ''))}</ToUserName>"
             f"<FromUserName>{_cdata(msg.get('ToUserName', ''))}</FromUserName>"
             f"<CreateTime>{int(time.time())}</CreateTime>"
             f"<MsgType>{_cdata('text')}</MsgType>"
             f"<Content>{_cdata(content)}</Content>"
             "</xml>")
    if not secure():
        return plain
    enc = encrypt(plain.encode("utf-8"))
    ts, nonce = str(int(time.time())), secrets.token_hex(8)
    return ("<xml>"
            f"<Encrypt>{_cdata(enc)}</Encrypt>"
            f"<MsgSignature>{_cdata(_msg_signature(ts, nonce, enc))}</MsgSignature>"
            f"<TimeStamp>{ts}</TimeStamp>"
            f"<Nonce>{_cdata(nonce)}</Nonce>"
            "</xml>")


def qr_url() -> str:
    """关注公众号的二维码。WECHAT_QR_URL 直接给图片地址；或者只给原始 ID（gh_ 开头），用微信的公开二维码地址。"""
    url = _env("WECHAT_QR_URL")
    if url:
        return url
    gh = _env("WECHAT_ACCOUNT_ID")
    return f"https://open.weixin.qq.com/qr/code?username={escape(gh)}" if gh else ""


def account_name() -> str:
    return _env("WECHAT_ACCOUNT_NAME")
