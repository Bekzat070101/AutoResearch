# 全局对话、多会话与已读文献

## 使用与边界

- 右下角气泡属于页面外壳。已建立身份后在所有应用页面可用，登录页隐藏；展开默认 440px，扩大为 720px，手机全屏。
- 首次登录的信息采集保留完整主页面对话，桌面采用聊天与画像摘要双栏、手机单栏；此时隐藏气泡。进入应用后将同一个聊天组件移回气泡，原会话与用户事实保留。
- 会话列表可新建、切换；标题取第一条用户消息，按最近活动排序。第一版没有删除、改名或跨会话原文搜索。
- 同画像共享有效事实与已读文献，近期对话及记忆原话证据只取本会话。生成时可以切页、换会话和编辑草稿，一次仅生成一条回复。
- 草稿、滚动位置、最近会话按账号、画像、会话隔离；个人阅读草稿也带画像 ID。旧的无画像草稿不自动导入。

## 接口

- `GET /api/dialogue/conversations?uid=…&portrait_id=…` → `conversations, portrait_id, busy`。
- `POST /api/dialogue/conversations`，JSON 为 `uid, portrait_id` → `conversation, portrait_id`；始终创建新 ID。
- 历史接口接受 `conversation_id, portrait_id`，返回选中的 `conversation_id`。未指定会话时仅取最近一个会话，绝不合并所有历史。
- 普通回复和 SSE 请求携带 `uid, portrait_id, conversation_id, message`。在用户锁内核对画像与会话；画像变化返回 409，会话不属于当前画像返回 404。旧客户端的可选参数保留兼容。
- 回复及历史消息新增 `reading_sources`，每条包含论文 ID、工具包、标题、来源链接和通过卡片的 ID／版本。前端可跳回研读。
- `GET /api/reading/completed?uid=…&portrait_id=…&query=…` 查看当前画像已读库。
- 对话新增只读工具 `reading.search(query?, limit?)`、`reading.detail(paper_id)`；查询使用关键词和中文双字片段，默认最多 5 条、上限 20 条。环境包最多附 3 条相关摘要。

## 存储与迁移

沿用现有 live 表＋画像快照架构：cards、triage、tutor_sessions、user_papers、read_documents 及阅读相关 edges/statements/bets 一起保存、清空与恢复。账号和当前画像共同决定 live 表的归属；不另建一套与画像快照竞争的所有权机制。公开论文正文及通用题目缓存不属于个人阅读经历。

`read_documents` 以当前 live 画像中的 `(user_id,paper_id)` 唯一；不同画像的版本保存在各自快照中。只有阅读卡通过才能写入，且卡片、索引、行为事实、memory_version 同事务提交。后续失败修订不覆盖最近通过的记录。笔记、摘要与原文引文保持来源区别，不能把完成阅读卡称为掌握论文。

首次启动执行有版本标记的事务迁移：

1. 无会话 ID 的旧消息归入专门历史会话；失去会话行的消息恢复对应会话。包括非活跃画像快照。
2. 只按各画像事实中的明确 `reading_card.card_id` 引用分配旧阅读卡，不按相同论文猜测另一版本笔记的归属。
3. 私有文献有被归属卡片引用时随其归属；无法归属的卡片、带读进度、分拣等保存到 `reading_unassigned`，不进入聊天或研读查询。
4. 从已归属的通过卡片回填已读索引。标记在同一事务提交，重复启动不会重复迁移。

`reading_unassigned` 随账号导出、删除；不提供自动认领界面。上线前须在备份副本上演练并审阅归属报告，不能将未知数据批量复制给所有画像。

只读报告命令：

```text
python tools/verify/report_reading_migration.py --db /path/to/migrated-copy.db
```

报告包含迁移是否执行、已分配卡片数、各类待归属数量和 live 已读数，不包含论文正文或密钥。生产数据库尚未在本次开发中迁移；部署仍须遵守 README 的 NAS、备份及 main 发版规则。

## 验证

新增 `server/tests/test_dialogue_workspace.py` 覆盖会话上下文、共享事实、拒绝错画像／错会话、通过卡片索引、重复与失败修订、事务回滚、画像往返、历史迁移与待归属数据。

本地界面集成验收脚本 `tools/verify/verify_chat_widget.js` 使用 Playwright 与独立的本地 QA 账号／数据库，覆盖常驻组件、草稿、生成期间切换、桌面尺寸及手机全屏，保存截图，并运行三个真实模型场景。仅允许 `127.0.0.1`，不会操作生产站点；需要本地 QA 种子数据和模型配置。
