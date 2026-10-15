# -*- coding: utf-8 -*-
"""把「渲染层」的验收脚本接进 pytest。

为什么需要这一层：API 通了不等于界面看得见。用户连着问了好几次
「为什么对话里的任务不能和整个系统产生联系」「点进任务区没东西」——
数据一直是通的，断在渲染上（`renderWorkbench` 没选方向时整页 return）。

`test_web_assets.py` 只能做结构检查（某个字符串在不在）。真正的
「渲染出来没有」要靠跑一遍源码。这两个脚本用最小 DOM 把 app.js / chat.js
里真正的渲染函数抽出来执行：

- verify_workbench_render.js  任务区会不会显示对话给的任务（含没选方向那种）
- verify_action_card.js       「就做这个」→「去任务区完成」这条点击路径

脚本放在 `tools/verify/`（仓库内，随仓库一起发出去）。早先它们在工作区
另一个目录里（`审计与方案/`，不在仓库内），于是别人 clone 下来永远跳过——
那等于没有这层保护。现在搬进来了，找不到才会跳过。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

SERVER = pathlib.Path(__file__).resolve().parent.parent
REPO = SERVER.parent                      # ResearchGuide-main
SCRIPTS = REPO / "tools" / "verify"       # 验收脚本随仓库走

NODE = shutil.which("node")

CASES = [
    ("verify_workbench_render.js", "任务区会显示对话给的任务"),
    ("verify_action_card.js", "行动卡的去向按钮"),
    ("verify_receipt_render.js", "结果区的信息组织（分组、顺序、空则不留壳）"),
    ("verify_chat_layout.js", "对话页整体布局（页头一块 + 三个区，输入永远在最下）"),
    ("verify_tutor_render.js", "带读面板作答框、页签模式、预填只填空栏"),
]


@pytest.mark.parametrize("name,label", CASES)
def test_render_layer_verification(name, label):
    if NODE is None:
        pytest.skip("没有 node，跳过渲染层验收")
    script = SCRIPTS / name
    if not script.exists():
        pytest.skip(f"找不到 {script}")

    proc = subprocess.run(
        [NODE, str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # 脚本自己打印「全部通过」/「失败 N 项」，退出码也一致
    assert proc.returncode == 0, f"{label} 失败：\n{out}"
    assert "全部通过" in out, f"{label} 没有报告通过：\n{out}"
    assert "[!! ]" not in out, f"{label} 里有失败项：\n{out}"
