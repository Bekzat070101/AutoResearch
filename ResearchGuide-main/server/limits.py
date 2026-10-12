# -*- coding: utf-8 -*-
"""两个上限工具：读外部响应有总时限和字节上限；内存缓存有过期和条数上限。

- urlopen 的 timeout 只管「多久没收到数据」，一个慢慢滴数据的服务器能让连接、响应头、正文一直拖下去。
  fetch 给整个请求（域名解析、连接、TLS、响应头、正文）一个总截止时间：解析限时等，连接前就登记 socket，
  到点由看门狗关掉；正文另有字节上限。
- 原来的缓存是普通 dict：过期的只是不再命中，但一直留在内存里；TTLCache 过期即删，超过条数先删最旧的。
"""
from __future__ import annotations

import heapq
import http.client
import itertools
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from collections import OrderedDict
from typing import Any, Hashable, Iterator, NoReturn

CHUNK = 64 * 1024


class ReadLimitError(OSError):
    """超过总时限或字节上限。继承 OSError，调用方原有的网络错误处理照样接得住。"""


def _socket_of(resp: Any) -> Any:
    try:
        return resp.fp.raw._sock  # CPython http.client 的底层 socket；拿不到就只靠两次读之间检查
    except AttributeError:
        return None


def read_limited(resp: Any, max_bytes: int, deadline: float, idle: float | None = None) -> bytes:
    """deadline 是 time.monotonic() 的绝对时刻。每次读之前把 socket 超时收紧到剩余时间；
    给了 idle 时，再收紧到「最多这么久没有数据」。"""
    buf = bytearray()
    sock = _socket_of(resp)
    read = getattr(resp, "read1", None) or resp.read
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ReadLimitError("超过总时限")
        if sock is not None:
            try:
                sock.settimeout(min(left, idle) if idle else left)
            except OSError:
                pass
        try:
            chunk = read(CHUNK)
        except TimeoutError as exc:
            raise ReadLimitError("超过总时限" if not idle or deadline - time.monotonic() <= 0 else f"{idle:g} 秒没有收到数据") from exc
        if not chunk:
            declared = (getattr(resp, "headers", None) or {}).get("Content-Length", "")
            if (declared.isdigit() and len(buf) < int(declared)) or (getattr(resp, "length", None) or 0) > 0:
                raise OSError("响应不完整：连接提前断开")  # 当作网络错误，调用方不会把半份内容当成完整结果缓存
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            raise ReadLimitError(f"响应超过 {max_bytes // 1024 // 1024} MB")


_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")


# 同一主机正在解析就共用那一次：一个解析很慢的主机最多占一个解析线程，不会把四个都占满、连累别的主机。
# 记着有几个请求在等；最后一个等的人超时走了，还在排队没开始的解析就取消，不让过期的活占住线程。
class _Lookup:
    __slots__ = ("fut", "waiters")

    def __init__(self, fut: Future) -> None:
        self.fut = fut
        self.waiters = 0


_DNS_INFLIGHT: dict[tuple[str, int], _Lookup] = {}
_DNS_LOCK = threading.RLock()  # 取消会在本线程里立刻触发完成回调，回调要拿同一把锁


def _forget_dns(key: tuple[str, int], lk: _Lookup) -> None:
    with _DNS_LOCK:
        if _DNS_INFLIGHT.get(key) is lk:
            del _DNS_INFLIGHT[key]


def _resolve(host: str, port: int, left: float) -> list:
    key = (host, port)
    with _DNS_LOCK:
        lk = _DNS_INFLIGHT.get(key)
        created = lk is None
        if created:
            lk = _DNS_INFLIGHT[key] = _Lookup(_DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM))
        lk.waiters += 1
    if created:
        lk.fut.add_done_callback(lambda _done, k=key, me=lk: _forget_dns(k, me))
    try:
        return lk.fut.result(timeout=left)
    except (FutureTimeout, CancelledError) as exc:
        raise ReadLimitError("超过总时限（域名解析）") from exc
    finally:
        with _DNS_LOCK:
            lk.waiters -= 1
            if lk.waiters == 0 and not lk.fut.done():
                lk.fut.cancel()  # 已经开始跑的取消不了（getaddrinfo 打断不了），排队中的就此作罢


class _Watch:
    """这次请求的截止时间和它用到的连接。DNS 解析在小线程池里限时等；socket 在连接之前就登记，
    到点 shutdown，卡在连接、TLS 握手、响应头、正文任何一步的线程都会立刻出错返回。"""

    def __init__(self, deadline: float, idle: float | None = None) -> None:
        self.deadline = deadline
        self.idle = idle
        self.killed = False
        self.done = False
        self._dups: list[socket.socket] = []
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def resolve(self, host: str, port: int) -> list:
        """getaddrinfo 没有超时参数，也打断不了：放进线程池，等到截止时间就放弃（解析线程自己会结束）。"""
        left = self.left()
        if left <= 0:
            raise ReadLimitError("超过总时限")
        return _resolve(host, port, min(left, self.idle) if self.idle else left)

    def adopt(self, sock: socket.socket) -> None:
        """登记原 socket 和副本。TLS 包装会摘走原对象，副本指向同一条连接；
        Windows 上只 shutdown 副本拦不住正在读响应头的线程，所以原件也要关。"""
        try:
            dup = sock.dup()
        except OSError:
            dup = None
        with self._lock:
            self._socks.append(sock)
            if dup is not None:
                self._dups.append(dup)
            killed = self.killed
        if killed:
            _abort(sock)
            if dup is not None:
                _abort(dup)
        left = max(0.01, self.left())
        sock.settimeout(min(left, self.idle) if self.idle else left)  # 每次收发最多等这么久；总时限由看门狗管

    def kill(self) -> None:
        with self._lock:
            self.killed = True
            socks = list(self._socks) + list(self._dups)
        for s in socks:
            _abort(s)

    def close(self) -> None:
        with self._lock:
            dups, self._dups = self._dups, []
            self._socks = []
        for dup in dups:
            try:
                dup.close()
            except OSError:
                pass


def _shut(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # 连接已经断了


def _abort(sock: socket.socket) -> None:
    """shutdown 之后再 close：Windows 上只关副本、不关原件时，慢响应头会把线程挂满整个滴完。"""
    _shut(sock)
    try:
        sock.close()
    except OSError:
        pass


def _connect(watch: _Watch, address: tuple, timeout: Any = None, source_address: Any = None, **_: Any) -> socket.socket:
    """代替 socket.create_connection：解析限时；每个 socket 先登记再连接，连接本身受剩余时间约束。"""
    host, port = address
    err: OSError | None = None
    for family, socktype, proto, _canon, addr in watch.resolve(host, port):
        sock = socket.socket(family, socktype, proto)
        try:
            watch.adopt(sock)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            return sock
        except OSError as exc:
            err = exc
            sock.close()
            if watch.left() <= 0:
                break
    raise err or OSError("连不上")


def _watched(base: type, watch: _Watch) -> type:
    class Conn(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = lambda address, timeout=None, source_address=None, **kw: \
                _connect(watch, address, timeout, source_address)

    return Conn


class _HTTP(urllib.request.HTTPHandler):
    def __init__(self, watch: _Watch) -> None:
        super().__init__()
        self.watch = watch

    def http_open(self, req):
        return self.do_open(_watched(http.client.HTTPConnection, self.watch), req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, watch: _Watch) -> None:
        super().__init__()
        self.watch = watch

    def https_open(self, req):
        return self.do_open(_watched(http.client.HTTPSConnection, self.watch), req, context=self._context)


class _Watchdog:
    """一个线程看所有请求的截止时间，到点关掉还没结束的请求（原来每个请求单开一个 Timer 线程）。"""

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, _Watch]] = []
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None

    def add(self, watch: _Watch) -> None:
        with self._cv:
            heapq.heappush(self._heap, (watch.deadline, next(self._seq), watch))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="fetch-watchdog", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                deadline, _, watch = self._heap[0]
                left = deadline - time.monotonic()
                if left > 0:
                    self._cv.wait(left)
                    continue
                heapq.heappop(self._heap)
            if not watch.done:
                watch.kill()


_WATCHDOG = _Watchdog()


def fetch(req: urllib.request.Request, *, timeout: float, max_bytes: int, idle: float | None = None) -> bytes:
    """整个请求的总时限是 timeout 秒；给了 idle 时，任何一步最多 idle 秒没有数据就放弃。
    HTTPError 原样抛出；超时或超大抛 ReadLimitError；其他网络错误抛 OSError。"""
    deadline = time.monotonic() + timeout
    watch = _Watch(deadline, idle)
    opener = urllib.request.build_opener(_HTTP(watch), _HTTPS(watch))
    _WATCHDOG.add(watch)
    try:
        with opener.open(req, timeout=min(timeout, idle) if idle else timeout) as resp:
            body = read_limited(resp, max_bytes, deadline, idle)
        if watch.killed:
            # 看门狗关掉 socket 后，读的一方看到的是正常的「连接结束」；没声明长度的响应会像是读完了，其实只是半份
            raise ReadLimitError("超过总时限")
        return body
    except (urllib.error.HTTPError, ReadLimitError):
        raise
    except (OSError, http.client.HTTPException) as exc:
        _translate(exc, watch, deadline)
    finally:
        watch.done = True
        watch.close()


def _translate(exc: BaseException, watch: _Watch, deadline: float) -> NoReturn:
    """连接或读取阶段的错误 → 调用方认得的三种：HTTPError 原样、超时 ReadLimitError、其余 OSError。"""
    if isinstance(getattr(exc, "reason", None), ReadLimitError):
        raise exc.reason from exc  # urllib 把连接阶段的错误包成 URLError
    if watch.killed or time.monotonic() >= deadline:
        raise ReadLimitError("超过总时限") from exc
    if isinstance(exc, OSError):
        raise exc
    raise OSError(f"响应不完整（{type(exc).__name__}）") from exc


def stream(req: urllib.request.Request, *, timeout: float, max_bytes: int, idle: float | None = None) -> Iterator[bytes]:
    """逐行产出响应正文（给 SSE 这类边收边用的响应）。约束和 fetch 一样：总时限 timeout 秒、idle 秒没数据就放弃、
    正文累计超过 max_bytes 就停。原来的流式模型调用直接 urlopen，只有「多久没数据」的限制：
    对方每隔几十秒发个空行，一个线程就能被占住一整天。"""
    deadline = time.monotonic() + timeout
    watch = _Watch(deadline, idle)
    opener = urllib.request.build_opener(_HTTP(watch), _HTTPS(watch))
    _WATCHDOG.add(watch)
    try:
        with opener.open(req, timeout=min(timeout, idle) if idle else timeout) as resp:
            sock = _socket_of(resp)
            read = getattr(resp, "read1", None) or resp.read
            buf, total = bytearray(), 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ReadLimitError("超过总时限")
                if sock is not None:
                    try:
                        sock.settimeout(min(left, idle) if idle else left)
                    except OSError:
                        pass
                try:
                    chunk = read(CHUNK)
                except TimeoutError as exc:
                    raise ReadLimitError("超过总时限" if not idle or deadline - time.monotonic() <= 0 else f"{idle:g} 秒没有收到数据") from exc
                if not chunk:
                    if watch.killed:
                        raise ReadLimitError("超过总时限")  # 看门狗关了 socket，读到的「结束」是假的
                    if buf:
                        yield bytes(buf)
                    return
                total += len(chunk)
                if total > max_bytes:
                    raise ReadLimitError(f"响应超过 {max_bytes // 1024 // 1024} MB")
                buf += chunk
                while True:
                    i = buf.find(b"\n")
                    if i < 0:
                        break
                    line = bytes(buf[:i])
                    del buf[:i + 1]
                    yield line
    except (urllib.error.HTTPError, ReadLimitError):
        raise
    except (OSError, http.client.HTTPException) as exc:
        _translate(exc, watch, deadline)
    finally:
        watch.done = True
        watch.close()


class TTLCache:
    """线程安全。lookup 返回 (命中?, 值)，值可以是空列表这类假值。"""

    def __init__(self, ttl: float, maxsize: int) -> None:
        self.ttl, self.maxsize = ttl, maxsize
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        # 按写入时间排序（set 时移到末尾），过期的都在最前面：从头删到第一个没过期的为止
        while self._data:
            first = next(iter(self._data.values()))
            if now - first[0] < self.ttl:
                break
            self._data.popitem(last=False)

    def lookup(self, key: Hashable) -> tuple[bool, Any]:
        with self._lock:
            self._sweep(time.monotonic())
            item = self._data.get(key)
            return (True, item[1]) if item is not None else (False, None)

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            now = time.monotonic()
            self._sweep(now)
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
