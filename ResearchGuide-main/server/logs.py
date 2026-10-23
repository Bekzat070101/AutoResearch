# -*- coding: utf-8 -*-
"""统一日志出口：一行 JSON、写完就刷新、任何异常都不往外抛。

为什么不用裸 `print(json.dumps(..., ensure_ascii=False))`：

1. 编码失败会连行一起丢。stdout 是 gbk 时遇到编不出来的字符（例子：「⚠️」），
   UnicodeEncodeError 直接把那条日志扔掉——它还跑在请求中间件里，异常会把这次请求变成 500。
2. print 默认块缓冲。函数实例被冻结或回收时，缓冲区里的日志根本不会出现。
3. 原来每条日志各写各的，没有时间戳，SLS 里只能靠顺序猜。

emit() 给所有日志用：一行 JSON、写完 flush、编不出来的字符退成 \\uXXXX 转义写法
（行不丢，也不把别的行带成另一种编码）、任何异常都吞掉——日志失败绝不能影响请求。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

CN = timezone(timedelta(hours=8))


def line(obj: dict[str, Any]) -> str:
    """一行日志文本（不含换行）。缺 ts 时补北京时间，方便 SLS 排序对账。"""
    if "ts" not in obj:
        obj = {"ts": datetime.now(CN).isoformat(timespec="seconds"), **obj}
    return json.dumps(obj, ensure_ascii=False)


def emit(obj: dict[str, Any]) -> None:
    """打一行日志。写不出去就算了：日志失败绝不能影响请求。"""
    text = line(obj) + "\n"
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        # 先按 stdout 自己的编码转一遍：编不出来的字符变 \uXXXX，JSON 读回来还是原字符，
        # 这样这一行跟前后行是同一种编码，不会在日志里插进半行乱码。
        safe = text.encode(enc, "backslashreplace").decode(enc, "replace")
    except Exception:
        safe = text
    for chunk in (safe, text):
        try:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            return
        except Exception:
            continue