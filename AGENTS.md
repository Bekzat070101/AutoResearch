# 给 AI agent 的仓库规矩（Codex / Claude Code / Grok 等先读这页）

1. 应用在 `ResearchGuide-main/`（FastAPI + SQLite 后端 `server/`，无构建静态前端 `web/`，给模型的工作说明 `skills/`）。改代码先读 `ResearchGuide-main/README.md`，部署只按它的「部署」一节和 `ResearchGuide-main/docs/DEPLOY.md` 做。
2. **发版四条硬规矩**：生产 `QIYAN_DB` 必须在 NAS（`/mnt/nas/qiyan.db`）；只用 `ResearchGuide-main/tools/deploy/deploy_fc.py` 发版，不在控制台部署，不给生产加 `--allow-ephemeral`；换库路径必须 `--db ... --migrate-from-backup ...`；从 `main` 发版，发完核对 `/api/health` 的 `db.ephemeral=false`、`db.blocked=null` 和 `checks.build`。服务端自己也守门：库不在持久盘就不开业（503 `db_not_ready`），不要用 `QIYAN_ALLOW_EPHEMERAL=1` 绕过它发生产。做不到就停下来问人，不要「先发了再说」。
3. 测试：`cd ResearchGuide-main && LLM_API_KEY= BACKUP_OSS_BUCKET= uv run --no-project --with pytest --with fastapi --with pydantic --with httpx --with cryptography --with pypdf pytest server/tests -q`，前端 `node --check web/js/*.js` 加 `node tools/verify/verify_frontend_state.js all`。线上 Python 是 3.11，别用 3.12 才有的语法。
4. 不提交 `.env`、密钥、数据库文件、图片等二进制；不改 `ResearchGuide-main/docs/tasks/`（本地工作单，不进 PR）；SKILL.md 只写会改变模型行为的内容，一条一行。
5. PR 标题和正文用中文，说清改了什么、怎么自测的、上线前要配什么；没有人确认不要合并，不要动别人的分支。
