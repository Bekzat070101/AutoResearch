import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "web" / "js" / "app.js"
NODE = shutil.which("node")


def test_web_copy_r3_strings():
    js = APP_JS.read_text(encoding="utf-8")
    for s in ("推荐论文", "我的阅读卡", "对比表", "有全文", "译名待校", "splitZhTitle", "挑几篇新论文",
              "先选一个方向，才知道给你推哪些论文。", "聊过了", "今天这一步", "搜论文（英文关键词）"):
        assert s in js, s
    for s in ("分拣最近的新论文", "还没有能用来检索的兴趣话题", "arXiv 只认英文关键词", '"库内"'):
        assert s not in js, s


def test_split_zh_title_regex():
    if NODE is None:
        pytest.skip("没有 node")
    js = APP_JS.read_text(encoding="utf-8")
    src = re.search(r"function splitZhTitle\(t\) \{.*?\n\}", js, re.S).group(0)
    code = src + '\nconsole.log(JSON.stringify([splitZhTitle("大规模多任务语言理解评测（机器待核）"), splitZhTitle("大规模多任务语言理解评测")]));'
    proc = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    a, b = json.loads(proc.stdout)
    assert a == {"zh": "大规模多任务语言理解评测", "unverified": True}
    assert b["unverified"] is False
