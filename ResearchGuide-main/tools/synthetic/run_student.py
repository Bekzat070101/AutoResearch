# -*- coding: utf-8 -*-
"""我们自己的人工学生跑法：用一个 OpenAI 兼容的模型（默认 DeepSeek）当学生脑子，调 server/mcp_student.py 的工具当手脚。

用法（API 已经在跑，且设了 AGENT_TOKENS）：
  LLM_API_KEY=<DeepSeek key> uv run --no-project --offline python tools/synthetic/run_student.py \
      --base http://127.0.0.1:8100 --token <AGENT_TOKENS 里的令牌> --persona bored_y3 --max-steps 40
环境变量：LLM_BASE_URL（默认 https://api.deepseek.com/v1）、AGENT_MODEL（默认 deepseek-v4.1）、LLM_API_KEY。
每一步打印工具调用与屏幕；结束时打印 /api/agent/run/end 的统计。不改仓库、不碰生产。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "server"))

import mcp_student  # noqa: E402

SYSTEM = ("你是一个来用「启研」网站的北大学生，不是助手。你只能通过工具行动：看屏幕（look）、在输入框说话（reply）、"
          "按屏幕上列出的按钮（press）、打开第几篇论文（open_paper）等。规则：\n"
          "1. 只根据屏幕上的文字决定下一步；屏幕上没有的东西不要假设存在。\n"
          "2. 说话要像你的人设：长度、语气、懂多少都按人设卡来；不要替网站解释，不要总结。\n"
          "3. 看不懂页面、不知道该做什么时，必须调 stuck 说明卡在哪，然后再试一次别的按钮。\n"
          "4. 每遇到一个看不懂的页面，耐心减一；耐心到 0、或今天的目标都做完了，调 end_day 并写一句总结。\n"
          "5. 每一步只调一个工具。")


def _chat(base: str, key: str, model: str, messages: list[dict], tools: list[dict]) -> dict:
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", method="POST",
                                 data=json.dumps({"model": model, "messages": messages, "tools": tools,
                                                  "tool_choice": "required", "temperature": 0.7}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("QIYAN_URL") or "http://127.0.0.1:8100")
    ap.add_argument("--token", default=os.environ.get("QIYAN_AGENT_TOKEN") or "")
    ap.add_argument("--persona", default="diligent_y1")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL") or "deepseek-v4.1")
    ap.add_argument("--llm-base", default=os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1")
    ap.add_argument("--no-stream", action="store_true")
    a = ap.parse_args(argv)
    key = os.environ.get("LLM_API_KEY") or ""
    if not key or not a.token:
        print("需要 LLM_API_KEY（学生脑子用的模型）和 --token（AGENT_TOKENS 里的 operator 令牌）", file=sys.stderr)
        return 2
    api = mcp_student.Api(a.base, a.token)
    api.prefer_stream = not a.no_stream
    tools = mcp_student.Tools(api)
    oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
                for t in mcp_student.TOOLS]
    first = tools.start_as_student(a.persona, a.model)
    print("== start ==\n" + first["text"])
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": first["text"]}]
    for step in range(1, a.max_steps + 1):
        out = _chat(a.llm_base, key, a.model, messages, oa_tools)
        msg = out["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": "你必须调用一个工具。看不懂就调 stuck，做完了就调 end_day。"})
            continue
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls[:1]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            res = mcp_student.call_tool(tools, name, args)
            text = res["content"][0]["text"]
            print(f"\n== step {step}: {name}({json.dumps(args, ensure_ascii=False)}) ==\n{text}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            if name == "end_day" and not res.get("isError"):
                print("\n== run ==", json.dumps((res.get("structuredContent") or {}).get("run"), ensure_ascii=False))
                return 0
    print("\n到了最大步数，强制 end_day")
    res = tools.end_day("到了最大步数")
    print(json.dumps(res.get("run"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
