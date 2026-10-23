# -*- coding: utf-8 -*-
"""日志出口：一行 JSON、写完就刷新、任何异常都不往外抛。钉住三件出过事的东西：

1. 中文日志写到 gbk stdout 不许抛异常、不许丢行（原来 UnicodeEncodeError 一抛，跑在请求中间件里那次请求跟着变 500）；
2. 编不出来的字符退成 \\uXXXX，行还在、JSON 还能读回原字符，也不会把这一行变成另一种编码的乱码；
3. 写 stdout 失败绝不让调用方跟着挂。

不联网、不起服务。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logs  # noqa: E402

WARN = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"  # gbk 编不出来，正是线上会把整条日志丢掉的那种字符


def test_line_keeps_chinese_and_adds_beijing_ts():
    text = logs.line({"event": "request", "msg": "库在临时盘上"})
    assert "\n" not in text  # 一行一条，交给 emit 补换行
    got = json.loads(text)
    assert got["msg"] == "库在临时盘上"  # 不转义成 \uXXXX：SLS 里直接能读
    assert got["event"] == "request"
    assert got["ts"][4] == "-" and got["ts"][7] == "-"  # 缺 ts 时补北京时间


def test_line_keeps_an_explicit_ts():
    assert json.loads(logs.line({"ts": "2026-10-23T09:00:00+08:00"}))["ts"] == "2026-10-23T09:00:00+08:00"


def test_emit_keeps_one_readable_line_on_a_gbk_stdout(monkeypatch):
    """本机（和部分容器）stdout 是 gbk：中文加 ⚠️ 原来直接抛 UnicodeEncodeError，那条日志就没了。"""
    raw = io.BytesIO()
    monkeypatch.setattr(logs.sys, "stdout", io.TextIOWrapper(raw, encoding="gbk"))
    logs.emit({"event": "warn", "msg": f"{WARN} 数据库还在临时盘"})  # 不许抛
    written = raw.getvalue()
    assert written.endswith(b"\n") and written.count(b"\n") == 1  # 行没丢，也没多出来半行
    got = json.loads(written.decode("gbk"))  # 流的编码没被污染：整行还是 gbk
    assert got["msg"] == f"{WARN} 数据库还在临时盘"  # 字符也没丢，JSON 读回来是原字符
    assert got["event"] == "warn"


def test_emit_swallows_a_broken_stdout(monkeypatch):
    class Boom:
        def write(self, _text):
            raise OSError("stdout 挂了")

    monkeypatch.setattr(logs.sys, "stdout", Boom())
    logs.emit({"event": "request"})  # 不许抛：日志失败不能影响请求