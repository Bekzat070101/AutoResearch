"""Download manylinux cp311 wheels for the FC custom.debian12 image."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DEST = Path(__file__).resolve().parent / "vendor_wheels"
# 和 server/requirements.txt 保持一致：少了 cryptography，公众号安全模式解不了密，微信登录整个不能用；pypdf 是导入 PDF 用的
PKGS = ["fastapi>=0.110", "uvicorn>=0.29", "pydantic>=2.6", "cryptography>=42", "pypdf>=6.1"]


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        *PKGS,
        "-d",
        str(DEST),
        "--platform",
        "manylinux2014_x86_64",
        "--python-version",
        "311",
        "--implementation",
        "cp",
        "--abi",
        "cp311",
        "--only-binary=:all:",
    ]
    print(" ".join(cmd))
    subprocess.check_call(cmd)
    print("wheels", len(list(DEST.glob("*.whl"))))


if __name__ == "__main__":
    main()
