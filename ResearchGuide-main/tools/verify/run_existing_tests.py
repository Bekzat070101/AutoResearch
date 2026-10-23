"""Run tests with short parameter IDs to fit Windows environment limits."""
from pathlib import Path
import hashlib
import sys
import pytest


class ShortByteIds:
    def pytest_make_parametrize_id(self, config, val, argname):
        if isinstance(val, bytes):
            return f'{argname}-{len(val)}-bytes'
        if isinstance(val, str) and len(val) > 200:
            digest = hashlib.sha256(val.encode('utf-8')).hexdigest()[:10]
            return f'{argname}-{len(val)}-chars-{digest}'


root = Path(__file__).resolve().parents[2]   # tools/verify -> 仓库根
raise SystemExit(pytest.main([
    '-q', '-p', 'no:cacheprovider', '--tb=short',
    *(sys.argv[1:] or [str(root / 'server/tests')]),
], plugins=[ShortByteIds()]))
