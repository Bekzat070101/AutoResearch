# -*- coding: utf-8 -*-
"""R4-12：逐个在独立 pytest 子进程里跑曾经外联的测试，低层 DNS/connect 守卫记录非 loopback 目标并拒绝。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2]

GUARD = r'''
import ipaddress
import json
import os
import socket

_OUT = os.environ["NETGUARD_OUT"]


def _record(kind, target):
    with open(_OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "target": repr(target)}) + "\n")


def _loopback(host):
    try:
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError, UnicodeError):
        return False


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def getaddrinfo(host, *a, **k):
    if not _loopback(host):
        _record("dns", host)
        raise socket.gaierror(socket.EAI_NONAME, "netguard: external DNS refused")
    return _real_getaddrinfo(host, *a, **k)


def _check(addr):
    if isinstance(addr, tuple) and addr and not _loopback(addr[0]):
        _record("connect", addr)
        raise OSError("netguard: external connect refused")


def connect(self, addr):
    _check(addr)
    return _real_connect(self, addr)


def connect_ex(self, addr):
    _check(addr)
    return _real_connect_ex(self, addr)


socket.getaddrinfo = getaddrinfo
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
'''

OSS_ENV = {"BACKUP_OSS_BUCKET": "netguard-bucket", "OSS_ACCESS_KEY_ID": "netguard-id",
           "OSS_ACCESS_KEY_SECRET": "netguard-secret"}

CASES = [
    ("server/tests/test_admin.py::test_collect_lock_blocks_second", False),
    ("server/tests/test_perf.py::test_cached_papers_do_not_queue_behind_slow_fetches", False),
    ("server/tests/test_perf.py::test_slow_dns_counts_against_the_deadline", False),
    ("server/tests/test_admin.py::test_backup_vacuum_and_restore", True),
    ("server/tests/test_admin.py::test_restore_write_failure_keeps_original_db", True),
    ("server/tests/test_admin.py::test_concurrent_restore_partial_write_keeps_validated_database", True),
    ("server/tests/test_admin.py::test_restore_cleanup_error_reports_applied", True),
    ("server/tests/test_admin.py::test_restore_waits_for_open_connections", True),
]


def _run_guarded(tmp_path: Path, target: str, oss: bool = False, cwd: Path = APP):
    plug = tmp_path / "plug"
    plug.mkdir(exist_ok=True)
    (plug / "netguard_plugin.py").write_text(GUARD, encoding="utf-8")
    out = tmp_path / "net.jsonl"
    out.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(plug) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["NETGUARD_OUT"] = str(out)
    env["LLM_API_KEY"] = ""
    for k in ("BACKUP_OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
        env.pop(k, None)
    if oss:
        env.update(OSS_ENV)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "netguard_plugin", "-p", "no:cacheprovider",
                           "-q", target], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    return proc, records


@pytest.mark.parametrize("node,oss", CASES, ids=[c[0].rsplit("::", 1)[1] for c in CASES])
def test_repository_cases_do_not_reach_external_boundaries(tmp_path, node, oss):
    proc, records = _run_guarded(tmp_path, node, oss)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert records == []


def test_guard_records_and_refuses_external_targets(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_probe.py").write_text(
        "import socket\n"
        "import pytest\n\n"
        "def test_probe():\n"
        "    with pytest.raises(socket.gaierror, match='netguard'):\n"
        "        socket.getaddrinfo('netguard-probe.invalid', 80)\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    try:\n"
        "        with pytest.raises(OSError, match='netguard'):\n"
        "            s.connect(('203.0.113.7', 9))\n"
        "    finally:\n"
        "        s.close()\n"
        "    assert socket.getaddrinfo('127.0.0.1', 80)\n", encoding="utf-8")
    proc, records = _run_guarded(tmp_path, "test_probe.py", cwd=probe)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert records == [{"kind": "dns", "target": "'netguard-probe.invalid'"},
                       {"kind": "connect", "target": "('203.0.113.7', 9)"}]


_NON_NUMERIC = ["127.review.invalid", "localhost", "0.0.0.0", "::"]


@pytest.mark.parametrize("raw", [False, True], ids=["str", "bytes"])
@pytest.mark.parametrize("host", _NON_NUMERIC)
@pytest.mark.parametrize("kind", ["dns", "connect", "connect_ex"])
def test_guard_rejects_non_numeric_loopback_targets(tmp_path, monkeypatch, kind, host, raw):
    import socket
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: calls.append(("dns", a)))
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: calls.append(("connect", addr)))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, addr: calls.append(("connect_ex", addr)))
    out = tmp_path / "net.jsonl"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("NETGUARD_OUT", str(out))
    exec(compile(GUARD, "netguard_plugin", "exec"), {"__name__": "netguard_plugin"})
    target = host.encode("ascii") if raw else host
    if kind == "dns":
        with pytest.raises(socket.gaierror) as e:
            socket.getaddrinfo(target, 80)
        assert e.value.args == (socket.EAI_NONAME, "netguard: external DNS refused")
        want = {"kind": "dns", "target": repr(target)}
    else:
        with pytest.raises(OSError) as e:
            getattr(socket.socket, kind)(object(), (target, 80))
        assert type(e.value) is OSError and e.value.args == ("netguard: external connect refused",)
        want = {"kind": "connect", "target": repr((target, 80))}
    assert calls == []
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records == [want]


def test_guard_subprocess_refuses_non_numeric_loopback_dns(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_probe.py").write_text(
        "import socket\n"
        "import pytest\n\n"
        "def test_probe():\n"
        "    with pytest.raises(socket.gaierror, match='netguard: external DNS refused'):\n"
        "        socket.getaddrinfo('127.review.invalid', 80)\n"
        "    assert socket.getaddrinfo('127.0.0.1', 80)\n"
        "    assert socket.getaddrinfo('127.0.0.2', 80)\n"
        "    assert socket.getaddrinfo('::1', 80)\n", encoding="utf-8")
    proc, records = _run_guarded(tmp_path, "test_probe.py", cwd=probe)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert records == [{"kind": "dns", "target": "'127.review.invalid'"}]
