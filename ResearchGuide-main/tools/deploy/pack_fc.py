"""Zip ResearchGuide-main for Function Compute custom runtime."""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "qiyan-fc.zip"
SKIP_DIR = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ptmp", "node_modules", "data"}
SKIP_FILE = {".env", ".env.local", "demo.db", ".deploy.env"}
SKIP_SUF = {".pyc", ".db", ".log", ".zip"}
ALLOW_TOP = {"server", "web", "knowledge", "skills"}


def _git_root() -> Path | None:
    here = APP
    for _ in range(4):
        if (here / ".git").exists():
            return here
        here = here.parent
    return None


def _dirty() -> str:
    root = _git_root()
    if root is None:
        return ""
    p = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    if p.returncode != 0:
        return ""
    rel = APP.resolve()
    dirty = []
    for line in p.stdout.splitlines():
        path = line[3:].strip()
        full = (root / path).resolve()
        try:
            full.relative_to(rel)
        except ValueError:
            continue
        dirty.append(path)
    return "\n".join(dirty)


def _write_build() -> None:
    import re
    root = _git_root()
    sha = "unknown"
    if root:
        p = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True)
        if p.returncode == 0:
            sha = p.stdout.strip()
    html = (APP / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"<b>(w\d+)</b>", html)
    stamp = m.group(1) if m else ""
    return f"{sha} {stamp}\n"


def main() -> None:
    dirty = _dirty()
    if dirty:
        raise SystemExit("工作区不干净，拒绝打包：\n" + dirty[:800])
    if OUT.exists():
        OUT.unlink()
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        boot = (Path(__file__).resolve().parent / "bootstrap").read_bytes().replace(b"\r\n", b"\n")
        info = zipfile.ZipInfo("bootstrap")
        info.create_system = 3
        info.external_attr = 0o755 << 16
        z.writestr(info, boot)
        z.writestr("BUILD", _write_build())
        n += 1
        wheels = Path(__file__).resolve().parent / "vendor_wheels"
        if not wheels.exists() or not any(wheels.glob("*.whl")):
            raise SystemExit("missing vendor_wheels; run download_wheels.py")
        for w in sorted(wheels.glob("*.whl")):
            z.write(w, f"vendor_wheels/{w.name}")
            n += 1
        for p in APP.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(APP)
            if rel.parts and rel.parts[0] not in ALLOW_TOP:
                continue
            if any(part in SKIP_DIR for part in rel.parts):
                continue
            if p.name in SKIP_FILE or p.suffix in SKIP_SUF:
                continue
            z.write(p, rel.as_posix())
            n += 1
    print("files", n, "bytes", OUT.stat().st_size, "out", OUT)


if __name__ == "__main__":
    main()
