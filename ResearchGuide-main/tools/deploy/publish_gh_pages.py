"""Publish a browsable copy of web/ to GitHub Pages via the Git Data API."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

STATE = Path(__file__).resolve().parent / ".deploy.env"

WEB = Path(__file__).resolve().parents[2] / "web"
ROOT = Path(__file__).resolve().parents[4] / "qiyan-gh-pages"
API_FALLBACK = "https://qiyan-caxplsowco.cn-hangzhou.fcapp.run"
REPO = "repos/ChengcanWu/AutoResearch"


def gh(method: str, path: str, payload: dict | None = None) -> dict:
    cmd = ["gh", "api", "--method", method, path]
    if payload is not None:
        p = subprocess.run(cmd + ["--input", "-"], input=json.dumps(payload),
                           text=True, encoding="utf-8", capture_output=True)
    else:
        p = subprocess.run(cmd, text=True, encoding="utf-8", capture_output=True)
    if p.returncode != 0:
        raise SystemExit((p.stderr or p.stdout)[:800])
    return json.loads(p.stdout) if p.stdout.strip() else {}


def api_url() -> str:
    if STATE.exists():
        for line in STATE.read_text(encoding="utf-8").splitlines():
            if line.startswith("PUBLIC_URL="):
                return line.split("=", 1)[1].strip()
    return API_FALLBACK


def _static_files(html: str) -> list[str]:
    import re
    return re.findall(r"static/[\w./-]+\.(?:css|js)", html)


def assemble() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    html = (WEB / "index.html").read_text(encoding="utf-8").replace(
        'window.QIYAN_API = window.QIYAN_API || "";',
        f'window.QIYAN_API = "{api_url()}";',
    )
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    for rel in _static_files(html):
        src = WEB / rel.replace("static/", "", 1)
        dest = ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not src.exists():
            raise SystemExit(f"index 引用了不存在的 {rel}")
        shutil.copy2(src, dest)
    (ROOT / ".nojekyll").write_text("", encoding="ascii")
    if "启研" not in html or "static/css" not in html:
        raise SystemExit("assembled index looks wrong")


def main() -> None:
    assemble()
    parent = gh("GET", f"{REPO}/git/refs/heads/gh-pages")["object"]["sha"]
    tree = []
    for pth in ROOT.rglob("*"):
        if not pth.is_file() or ".git" in pth.parts:
            continue
        rel = pth.relative_to(ROOT).as_posix()
        blob = gh("POST", f"{REPO}/git/blobs", {
            "content": base64.b64encode(pth.read_bytes()).decode(),
            "encoding": "base64",
        })
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print("blob", rel)
    tr = gh("POST", f"{REPO}/git/trees", {"tree": tree})
    import re
    html = (WEB / "index.html").read_text(encoding="utf-8")
    stamp = (re.search(r"<b>(w\d+)</b>", html) or type("m", (), {"group": lambda n: ""})()).group(1)
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=WEB.parent, capture_output=True, text=True)
    src = (sha.stdout or "").strip() or "unknown"
    commit = gh("POST", f"{REPO}/git/commits", {
        "message": f"pages {stamp} {src}",
        "tree": tr["sha"],
        "parents": [parent],
    })
    gh("PATCH", f"{REPO}/git/refs/heads/gh-pages", {"sha": commit["sha"]})
    print("commit", commit["sha"])
    built = gh("POST", f"{REPO}/pages/builds")
    print("build", built.get("status"))


if __name__ == "__main__":
    main()
