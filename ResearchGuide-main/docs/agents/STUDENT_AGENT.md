# 让你的 AI agent 来当启研的学生

- 目的：任何 MCP 客户端（Codex、Claude Code/Desktop、Cursor、DSH）都能以「学生」身份用启研：注册、回答首次引导、看今日、读论文做带读、找项目交作品。网站记下每个 agent 做了什么、在哪卡住，给出按屏幕聚合的使用统计（`/api/admin/agent-runs`，Grafana 的 `qiyan_agent_*`）。
- 代码：`server/mcp_student.py`（工具层 + stdio + 远程挂载）、`server/personas.py`（人设卡与今日目标）、`tools/synthetic/run_student.py`（我们自己用 DeepSeek 当学生脑子的跑法）。测试 `server/tests/test_mcp_student.py`。
- 这些统计说明的是「学生能不能走通、在哪停下来」，不是学习效果；不同模型表现不同，比较时按 `agent_model` 和人设分组。

## 服务端怎么开（运维）

- 设 `AGENT_TOKENS="组名:令牌,组名2:令牌2"`（令牌 ≥ 8 字符，组名就是 operator，建议一人一条）。没设 = 功能关闭。
- 远程模式再设 `QIYAN_SELF_URL`（服务自己的回环地址，本机 `http://127.0.0.1:8100`；函数计算用运行时端口）。不设时只能用 stdio（agent 在本机起 `server/mcp_student.py`）。
- 预算：人工学生按注册用户计（`LLM_DAILY_USER`），另有同一 operator 名下合计 `LLM_DAILY_AGENT`（默认 200/天）；`/api/agent/register` 每来源每小时最多 200 个号。用的是服务器自己的 DeepSeek 额度。
- 人工学生的账号用户名是 `bot_<operator>_xxxxxx`，`agent_accounts` 表标记；`qiyan_users{kind="synthetic"}` 已含在总数里，统计真人时减掉它。清理：`POST /api/admin/user/delete`（逐个）或直接删 `bot_` 前缀账号。

## 客户端怎么接

- Codex（`~/.codex/config.toml`）：
  ```toml
  [mcp_servers.qiyan_student]
  url = "https://<API 域名>/mcp/student"
  bearer_token_env_var = "QIYAN_AGENT_TOKEN"
  ```
  本机 stdio 写法：`command = "python"`，`args = ["<仓库>/ResearchGuide-main/server/mcp_student.py", "--base", "http://127.0.0.1:8100", "--token", "<令牌>"]`。
- Claude Code：`claude mcp add --transport http qiyan_student https://<API 域名>/mcp/student --header "Authorization: Bearer <令牌>"`。
- DSH：`docs/DEPARTMENT_KNOWLEDGE.md` §4.5 的 `dsh-mcp-client` 形状，`url` 换成 `/mcp/student`，加 Authorization 头。
- 任何会发 HTTP 的 agent：`POST /mcp/student`，JSON-RPC `initialize` → `tools/list` → `tools/call`，`Authorization: Bearer <令牌>`；多个学生并行时每个会话带不同的 `Mcp-Session-Id` 头。

## 给 agent 的开场提示（system prompt，整段复制）

```text
你是一个来用「启研」网站的北大学生，不是助手。先调 start_as_student（可指定 persona：diligent_y1 / bored_y3 / non_cs / knows_direction / careless），拿到人设卡和今天的目标。
之后只根据屏幕上的文字行动：reply 在输入框说一句话，press 按「可以按的」里的按钮，open_paper 打开第几篇论文，start_tutor / answer_tutor / finish_tutor 做带读，search_projects / pick_project / submit_project 做项目。
说话要像人设：长度、语气、懂多少都按卡片来。看不懂页面、不知道该做什么时，必须调 stuck 说明卡在哪。
每遇到一个看不懂的页面耐心减一；耐心到 0、或今天的目标都做完了，调 end_day 并写一句总结。每一步只调一个工具。
```

## 工具一览

| 工具 | 做什么 |
| --- | --- |
| `start_as_student(persona?, agent_model?)` | 注册一个人工学生并上线；返回人设卡、今日目标、第一个屏幕 |
| `look()` | 当前屏幕：文字、可按的按钮、输入框、目标进度 |
| `reply(text)` | 在当前屏幕输入框说一句（对话 / 带读作答 / 项目问答） |
| `press(label)` | 按屏幕上列出的按钮；导航「今日 / 研读 / 项目 / 我的 / 对话」也用它 |
| `open_paper(n)` | 打开列表里第 n 篇 |
| `start_tutor()` / `answer_tutor(text, skip?)` / `finish_tutor()` | 带读 |
| `choose_direction(code)` | 选方向（屏幕上「选方向：xx」里的代码） |
| `search_projects(keywords?)` / `pick_project(n)` / `submit_project()` | 项目：搜、选、交示例作品 |
| `stuck(reason)` | 不知道怎么办时必须调 |
| `end_day(summary?)` | 今天结束；返回步数、卡住次数、完成的目标 |

## 看统计

- `GET /api/admin/agent-runs`（`X-Admin-Token`）：每次上线一行（operator、模型、人设、步数、卡住次数、完成的目标）、按 operator/模型/人设 的汇总、卡住最多的屏幕。
- Grafana：`qiyan_users{kind="synthetic"}`、`qiyan_agent_runs_total`、`qiyan_agent_stuck_total`、`qiyan_agent_plan_done_total`、`qiyan_agent_http_requests_total{path,code}`（人工学生的请求另计，真人面板不受影响）。
- 我们自己跑：`LLM_API_KEY=<key> uv run --no-project --offline python tools/synthetic/run_student.py --token <令牌> --persona bored_y3`，默认模型 `AGENT_MODEL=deepseek-v4.1`。
