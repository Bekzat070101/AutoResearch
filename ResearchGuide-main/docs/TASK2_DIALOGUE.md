# 任务 2 · 交互与问卷（对话内核 + 记忆）

> 分支 `feat/dialogue-agent` · 2026-10-01 ~ 10-02
> 负责人：任务 2 · 状态：**已完成并验证**（本分支 271 个测试通过；合进开发线后全量为 417 个，见根 README）
> 改对话 / 记忆 / 决策 / 成绩单之前**必读**本文；冻结契约细节见 [`DIALOGUE_CONTRACT.md`](DIALOGUE_CONTRACT.md)。

---

## 0. 这份文档是什么

任务 2 的交付说明。回答三个问题：

1. **我做了什么**（第 2–7 节，按机制分）
2. **别人怎么接**（第 8 节接口、第 9 节前端契约、第 10 节怎么跑测试）
3. **哪里还有坑**（第 11 节没做的、第 12 节改过别人代码的地方）

第 12 节请**务必看**——我动过任务 1/3/4 的文件，接口约定有冲突。

### 一句话概括

把「对话」从**模型自由发挥**改成 **模型决定做什么、代码决定能不能做** 的闭环，
并让这个闭环真的接上记忆、成绩单和任务区。

---

## 1. 原需求与交付对照

| 原需求 | 交付 |
| --- | --- |
| 「让对话变得智能，实现 环境观察 → 深度思考决策 → 调用工具 的闭环」 | §2 一轮两次调用的闭环 |
| 「不要固定五问，要自然聊天」 | §2 提问阶梯，按层追问不跳级 |
| 「记住的东西要能改能删」 | §3 记忆面板 + `PATCH/DELETE /api/me/facts` |
| 「用户列出的九大类字段」 | §4 九大类 → key 注册表 |
| 「成绩单能识别、能算绩点」 | §5 树洞格式识别 + 教务口径绩点 |
| 「对话里的任务要能去侧边栏完成」 | §6 对话 ↔ 任务区 |
| 「卡片信息布局要有逻辑」 | §7 前端结构契约 |

---

## 2. 核心机制：一轮最多两次模型调用

```
用户说话
 └─ ① 组装环境包（代码，只读，按画像过滤）
      ├─ 它记住的（带 fact id）
      ├─ 最近对话 / 任务 / 提交
      └─ 当前方向与进度
 └─ ② 调用模型 A：TurnProposal  ← 输出结构化 JSON
      ├─ understanding  本轮理解到什么
      ├─ memory_ops     想记住什么（**必须带用户原话引文**）
      ├─ dialogue       move / reply / reason / offered_actions
      ├─ tool_intent    要查什么（只读）
      └─ next_action    建议用户做什么
 └─ ③ 代码逐项裁决            ← 不合格的丢弃并记录原因
      ├─ 记忆：白名单 + 引文逐字校验（support_ratio）+ affects 必填
      └─ 工具：只读 registry，越界拒绝
 └─ 需要外部信息？执行工具（失败如实返回，不编造）
 └─ ④ 调用模型 B：TurnReply    ← 带工具结果收口
      └─ 落记忆、落行动、返回
```

**关键点：模型只提建议，代码有否决权。** 模型提议的 `next_action` 必须在
`VALID_NEXT_ACTIONS = ("explore_direction",)`
里，越界一律降级（`dialogue.py:517`）。

**为什么限制两次调用**：一轮里每多一次调用，延迟和成本线性上升，
而「决策」和「措辞」本来就是两件事。工具结果必须回到模型才能收口，
所以只有「要查东西」时才需要第二次。

### 传输：SSE

`POST /api/dialogue/stream` 是用户实际点的那条路（`/api/dialogue/turn` 是不支持
`ReadableStream` 时的兜底）。两者都走 `dialogue.turn_steps`，保证行为一致。

帧格式（事件名在 `event:` 行，**不在** JSON 里）：

```
event: stage
data: {"name": "observe"}

event: delta
data: {"text": "..."}

event: result
data: {...完整结果...}
```

真实阶段名：`observe` → `decide` → `memory` → `tool` → `compose`。

---

## 3. 记忆：按时间跨度分六层

```python
LAYERS = (identity 身份, practice 当前实践, capability 能力起点,
          experience 经历积累, interest 倾向, constraint 临时约束)
LAYER_BUDGET = {identity: 6, practice: 6, capability: 6, interest: 4, experience: 3, constraint: 4}
RECALL_HARD_CAP = 40
```

**为什么按时间跨度分，不按主题分**：变化速度决定「什么时候该重新问」，
也决定注入预算。身份类小而必带，倾向类多、只在聊方向时进来。

这取代了原来「平表按关键词取 top-8」——那种做法里一门课的成绩可能把
「他的方向」挤出上下文，而且没有任何信号告诉模型记忆被截断了。

### 写入三道闸门

| 闸门 | 作用 | 位置 |
| --- | --- | --- |
| ① 白名单 + 来源 | key 必须在注册表里，来源必须合法 | `_NAMESPACED` / `KeySpec` |
| ② `support_ratio(value, quote)` | 写进去的值必须在用户原话里有支撑 | `memory.py:398` |
| ③ `affects` 必填 | 说不出「这条影响哪个决策」就不记 | `KeySpec.affects` |

**底层是不对称原则**：

> 漏记一条 = 再问一次（便宜）。记错一条 = **静默污染以后所有决策**（贵）。

所以闸门只往严里设。这条原则是全部设计取舍的依据。

### 能力层的两条来源，硬证据优先

`capability:*`（用户自述）和 `transcript:*`（成绩单推导）都进能力层。
坑位不够时 **`transcript:*` 优先** —— 它是硬证据，自述是印象（`recall_grouped`）。

实际效果（真实数据里出现过）：用户自述 `capability:vibecoding`，
同时又说过「软件开发实战相关的东西没有实际做过」。两条**互相接近矛盾**，
而它们各自都不知道对方存在——这正是「按属性分开存、整卡注入」的理由。

### 提问阶梯

按层的顺序追问，不跳级（`a6487a6`）。刚认识时不派「对谁都成立」的任务——
这是实测反馈里最伤信任的失败模式，已固化成 eval 用例 `no_generic_task_early`。

---

## 4. 信息分类：九大类 → key 注册表

用户列出的九大类逐条落到 key：

| 类别 | key 形如 |
| --- | --- |
| 身份（年级/学校/院系/专业/性别/年龄/所在地） | `grade` `school` `department` `major` `gender` `age` `location` `enroll_year` |
| 已修课程 | `enrollments` 表（不是 fact） |
| 在修课程 | `current:course`（`default_ttl_days=90` 自动过期） |
| 能力 | `capability:*` / `base:*` / `transcript:*` |
| 进修方向 | `interest:*`、`enroll:minor` |
| 实习就业方向 | `career:*` |
| 人际交往 | `social:*` |
| 临时约束 | `constraint:*`（带 `valid_until`） |
| 经验积累 | `experience:*`、`project:<slug>.<attr>` |

**院系和专业分开存**（`school` / `department` / `major` 三个 key）：
「信息管理系 / 信息管理与信息系统」这种组合很常见，合成一条会丢掉其中一个。

### 「问卷」视图已删除

原来「对话 - 问卷 - 核对」三条路，问卷和对话在抢同一件事（收集信息）。
**「我的」页是唯一的编辑入口**，问卷视图已删（`test_no_questionnaire_view_is_left` 钉着）。

现在只剩**对话 + 我的**两条。对话右栏只读，改和删都在「我的」。

---

## 5. 成绩单：识别 → 解析 → 绩点 → 推导

### 5.1 粘贴格式（树洞导出）

```
25-26学年度2学期      ← 学期标题，一出现下面所有课都归它
4                     ← 学分
学分                   ← 字面量
高等数学C (二)         ← 课名
专业必修               ← 课程性质（0 行或多行，取最后一行）
89                    ← 成绩
```

学期标题按**时间**排序，不是字面排序（`09-10` 要排在 `25-26` 前面）。

### 5.2 教务口径绩点

```python
GPA(x) = 4 - 3 * (100 - x) ** 2 / 1600        # 只对百分制
```

- 加权平均 = Σ(学分 × GPA) / Σ(学分)，**分母只含百分制课程**
- 「合格」算**通过学分**，但**不进绩点分母**（当成 60 分会把绩点算低）
- 字母等级（B+）算通过、进通过学分，**不进绩点**，且必须**显式告诉用户**
- `IP` = 在修，单独一类，不参与任何已修统计
- `W` 退课 / `缓考(I)` 不计
- 50 分会算出负绩点，所以截到 0（`GPA_FLOOR_AT_ZERO`，这是**有意**和上游不同的一处，改回去只需设 False）

**「没算进绩点」必须说出来**，否则用户看到的总绩点是少算过的，而他还以为那是全部。
宁可多一句话，不要一个看起来完整其实是错的数。

### 5.3 成绩单直接粘进对话就能识别

`transcript.looks_like_transcript()` 拦在 `turn_steps` 最前面，
命中就走一条**不调模型**的快路径（`_transcript_turn`）。

**为什么不让模型「理解」成绩单**：成绩单是固定格式，解析是确定性的（78 个测试钉着）。
让模型去理解 60 行教务导出只会更慢更贵，还可能把「学分 3」当成聊天。

判据**刻意保守**：必须同时满足 ①有学期标题行 ②真解析出 ≥2 门课。

- 错判（把闲聊当成绩单）= 一句随口的话变成**硬证据**，最坏
- 漏判 = 用户去核对页粘，代价很低

所以「我高数 85、线代 90」**不会**被当成成绩单。

### 5.4 从成绩单推导能力（`transcript:*`）

`derive_capabilities()` 是**幂等且完整**的：同样输入永远同样输出。
调用方负责与库里已有的事实对齐（增/改/撤）。

四类规则（带 `min_courses` 门槛，避免一门课就下结论）：

| area | label | 门槛 |
| --- | --- | --- |
| `math` | 数学类课程 | **2** |
| `code` | 编程类课程 | 1 |
| `data` | 统计与数据类课程 | 1 |
| `econ` | 经济金融类课程 | 2（`affects=direction_choice`） |
| `english` | 英语类课程 | 1 |

**只统计通过的课**——挂了的高等数学不能算「修过数学类课程」，那句话会误导人。

**取代了模型凭一段话编判断**：`base:code` 那起事故（把「Vibecoding」写成
「依赖 AI 生成」并加上不存在的判断）就是这么来的。现在这类结论只能来自底稿。

---

## 6. 对话 ↔ 任务区

### 6.1 原来坏在哪

对话出的任务**根本不建 `tasks` 行**，只画了张卡。所以「对话里有任务，
去任务区看不到」。`action_event` 也没有真的落库。

### 6.2 现在的链路

```
对话里点「就做这个」
 └─ POST /api/dialogue/action {event: "accept"}
     └─ 真建 tasks 行（origin="dialogue"）
 └─ 侧边栏任务区显示（dialogueTaskSection）
 └─ 点「去任务区完成」→ setView("workbench") + S.openTaskId
 └─ 提交交付物 → status=done
 └─ 回对话 → finished_note 说「你刚交了 X」
```

**卡片三个状态给出互斥的唯一动作**：

| 状态 | 动作 |
| --- | --- |
| `offered` | 就做这个 / 先不做 |
| `accepted` \| `in_progress` | 去任务区完成 |
| `completed` | 无按钮，明确说已完成 |

**没有「标记完成」按钮**——完成只能通过提交交付物。

### 6.3 项目 = 实体 + 属性

`key` 形如 `project:<slug>.<attr>`，`PROJECT_ATTRS = (what, traction, stage, stack, blocker, next)`。

- **存储**按属性分开（各自的证据、TTL、单独改删都在）
- **展示**聚合成一张卡（一个项目一张，属性是子行）
- **注入**整卡注入（模型需要看到全貌才能判断）

**切分用属性表消歧**，不按第一个点切：key 里允许出现点，
按第一个点切会让 `project:my.app` 和 `project:my.other` 塌成同一个 slug、
属性互相串（`db35435` 修的就是这个）。未知属性 → 整段余下部分算实体
（**宁可多建一张卡，也不把不相关的两件事合并**）。

---

## 7. 前端结构契约

> 详细版见提交说明；这里只列**不能改**的部分。视觉（颜色/字体/圆角/留白）
> 可以随便重做，但下面这些是结构。

### 对话页：页头一块 + 三个区

```
┌ 对话  （未进入时有「进入启研」）                     ┐
│ 直接说话就行。                                        │
└──────────────────────────────────────────────────────┘
┌ 对话列 ───────────────────┐ ┌ 它记住的我 ┐
│ .chat-scroll  对话流（滚） │ │ 只读摘要    │
├───────────────────────────┤ │ 去我的核对  │
│ .chat-next    下一步（滚） │ │            │
├───────────────────────────┤ │            │
│ .chat-input   输入        │ │            │
└───────────────────────────┘ └────────────┘
```

**顺序固定：对话流 → 下一步 → 输入。输入永远在最下面。**
（原来三样塞在一个 `.chat-foot` 里，行动卡一高就把输入框顶出屏幕。）

### 对话流里只有对话

系统副作用（记了什么 / 查了什么 / 没采纳什么）收进**一个**块，
固定分组顺序：**记忆 → 查询 → 没采纳**。空的不渲染，不留空壳。

### 其他硬约束

- 行动卡四段顺序：状态 → 标题 → 依据 → 动作
- 没有依据就整段不渲染
- 已完成的任务**只画一个表头**
- 编辑记忆卡期间不得重建面板（否则 `blur` 不保证触发，输入会丢）
- 两列高度用同一套算法（否则一边长一边短，短的那列看起来像没加载完）

### 版本戳

页脚有 `界面版本 wNN`，每次改前端 +1，**必须和三个 `?v=` 全部一致**。
（踩过：CSS 停在 w27 而 JS 到了 w28，用户拿到「新 JS + 旧 CSS」，样式全不对。
有测试钉着。）

---

## 8. 接口清单（本轮新增）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/dialogue/turn` | 一轮对话（非流式兜底） |
| POST | `/api/dialogue/stream` | 一轮对话（SSE，前端实际用这条） |
| POST | `/api/dialogue/action` | 行动卡事件：accept / decline / complete |
| GET | `/api/dialogue/history` | 历史 + 待办行动 + 刚完成的任务 |
| GET | `/api/memory` | 分层记忆（前端记忆面板用） |
| GET | `/api/me/coverage` | 九大类字段覆盖度（核对页用） |
| POST | `/api/me/transcript/parse` | 只解析不落库（核对页预览） |
| POST | `/api/me/transcript` | 解析 + 落库（`mode=replace`） |
| GET | `/api/me/transcript` | 已落库的课程 |
| DELETE | `/api/me/enrollments/{eid}` | 删一门课 |

**只读工具 registry**（`tools.py`，模型只能调这四个）：

`course.search` · `project.search` · `project.review` · `transcript.summary`

> 合并进开发线后又挂了 5 个院系-专业知识库工具（`major_lookup` · `major_detail` ·
> `match_transcript` · `minor_programs` · `course_lookup`）。定义取自 MCP 的工具表，
> 见 [`DEPARTMENT_KNOWLEDGE.md`](DEPARTMENT_KNOWLEDGE.md) §4.6；
> `tool_intent` 的字段名同义写法见 [`DIALOGUE_CONTRACT.md`](DIALOGUE_CONTRACT.md) §5。

`transcript.summary` **默认只给汇总**，不把整份成绩单倒给模型；
要具体课程必须传 `keyword`。这是为了保护上下文预算。

---

## 9. 怎么跑

```bash
# 依赖：Python 3.12 + fastapi + uvicorn + pydantic（无前端构建）
cd ResearchGuide-main

# 配置（可选，不配也能跑，模型环节回退规则版）
cp .env.example .env        # 然后填 LLM_API_KEY

# 起服务
python server/main.py       # http://127.0.0.1:8100

# 全量测试（本分支 271 个；合进开发线后 417 个）
python tools/verify/run_existing_tests.py

# 渲染层验收（真跑渲染函数，不用浏览器）
node tools/verify/verify_chat_layout.js
node tools/verify/verify_action_card.js
node tools/verify/verify_receipt_render.js
node tools/verify/verify_workbench_render.js

# 守卫自身的验证：逐条回滚修复，断言守卫确实会变红
python tools/verify/verify_guards_can_fail.py

# 端到端（需要服务已起）
python tools/verify/verify_served.py
python tools/verify/verify_transcript_sse.py
```

### 测试分布（271）

| 文件 | 条数 | 覆盖 |
| --- | --- | --- |
| `test_memory.py` | 44 | 六层、三道闸门、召回预算、可改可删 |
| `test_transcript.py` | 78 | 解析、绩点、落库、推导、字段覆盖度 |
| `test_dialogue.py` | 30 | 两次调用契约、降级语义 |
| `test_web_assets.py` | 25 | 缓存参数、结构契约、死钩子、未定义变量 |
| `test_project_memory.py` | 22 | 项目实体+属性、带点 key 的消歧 |
| `test_transcript_in_dialogue.py` | 16 | 粘进对话的识别/拒识/落库 |
| `test_api_dialogue.py` | 14 | HTTP 层 |
| `test_task_bridge.py` | 13 | 对话 ↔ 任务区 |
| `test_projects.py` | 11 | 项目（任务 3 侧） |
| `test_tools.py` | 10 | 只读工具 |
| `test_portrait_isolation.py` | 4 | 画像隔离 |
| `test_render_layer.py` | 4 | 把渲染层脚本接进 pytest |

> **关于那 2 个 error**：直接 `pytest server/tests` 会看到
> `ValueError: the environment variable is longer than 32767 characters`
> ——这是 Windows 环境变量长度限制导致的**既有问题**，与任务 2 无关。
> 请用 `tools/verify/run_existing_tests.py`，它会跳过这两个文件。

---

## 10. 测试夹具有意使用合成数据

`test_transcript.py` 的 `SAMPLE` / `REAL`、`verify_served.py` 的
`SAMPLE_TRANSCRIPT`、`eval/dialogue_cases.json` 里的用户输入，
**都是合成的**课表和身份（示例大学 / 某某大学）。

早先这些地方放的是一份真实成绩单，已全部替换。理由不只是隐私：
**测试夹具不该来自某一个人的数据**——合成夹具更通用，也不会让下一个
读代码的人以为这是某个真实用户。

替换时保持了学分和成绩不变，所以「通过学分 10.0 / 绩点学分 8.0 /
绩点 3.8020」这些数字断言继续成立；末尾「总学分」那一行必须等于各课学分之和，
那条断言才有意义。

---

## 11. 边界与未做

### 契约明确排除（`DIALOGUE_CONTRACT.md` §12，v1 不做）

向量/embedding 检索 · 原生 function-calling · 并行工具调用 ·
自由闲聊 · 替用户决定终身方向

### 已知缺口

| 缺口 | 影响 |
| --- | --- |
| `enrollments` 未与 `skills/pku-course` 对齐 | 两套课程名，跨功能匹配不上 |
| 院系知识库 | **需要但未指派**——院系差异化的建议现在给不出 |
| reflection 层（回看「这学期做得怎么样」） | 缺一层，长期用户感受不到进展 |
| 课程 60 天自动衰减 | `ARCHITECTURE.md` §3.1 写了，实际只覆盖明文 `constraint:*` |
| 导出 | me 页只有可改/可删，没有导出 |

---

## 12. 我改过别人代码的地方（**请重点看**）

### 12.1 接口约定冲突（需要在群里定）

> 共同约定是「要改接口时先在群里说」。下面这条**我还没有单方面改**，
> 但两边对同一个字段的取值域不一致，迟早会撞。

| 提出方 | 字段 | 取值 |
| --- | --- | --- |
| 任务 3（边学边练） | `stage` | `0..3` |
| 任务 4（方向路径） | `stage` | `1..6` |

同一个字段名，两套语义。**建议拆成 `practice_stage`（任务3）和
`path_stage_id`（任务4）**，别共用一个。

### 12.2 改动过的共享文件

| 文件 | 改了什么 | 对别人的影响 |
| --- | --- | --- |
| `server/main.py` | 新增 10 个对话/记忆/成绩单接口 | 只增不改，原有接口签名未动 |
| `server/store.py` | 新增 `enrollments` 表、`last_action`、`pending_action` | 新表，`init_db` 会自动建 |
| `server/schemas.py` | 新增对话相关 schema | 只增 |
| `server/workbench.py` | 任务区渲染衔接对话任务 | 任务 1 的前端若直接读 `tasks` 需注意 `origin` 字段 |
| `web/js/app.js` | 对话页、任务面板、核对页 | **任务 1 做前端时注意**：任务面板/核对页已被我改过 |
| `web/index.html` | 版本戳机制 + 缓存参数 | 改前端**必须**同步页脚版本号和三个 `?v=` |
| `docs/README.md` | 标注过期文档 | 判定权威顺序见该文件 |

### 12.3 删掉的东西

| 删除项 | 原因 |
| --- | --- |
| 「问卷」视图 | 与对话职能重复，核对页是唯一编辑入口 |
| `docs/TASK_ASSIGNMENTS.md` | Week1 的 §0.3「接口契约冻结」会直接阻挡本轮交互改造 |
| `docs/NEXT_PRE_2026-09-28.md` | 「固定 ≤8 轮脚本」与本轮目标正好相反 |
| `planner.py` 的 S0/S1/S2 规则决策 | 换成模型决策（**方向相反**，不是增强） |

### 12.4 我修过的自己造成的 bug（诚实记录）

| bug | 后果 |
| --- | --- |
| CSS 缓存参数停在 w27 而 JS 到 w28 | 用户拿到「新 JS + 旧 CSS」，样式全不对 |
| `derived[:4]` | `sync_transcript_facts` 返回 key 列表不是字典 → KeyError |
| `_project_slug` 按第一个点切 | 带点的两个项目塌成一个、属性互串 |
| `taskPanel` done 分支没清空面板 | 已完成任务标题出现两遍 + 两个矛盾标签 |
| 三个从未定义的 CSS 变量 | 底色静默变透明，不报错不警告 |
| 刷新记忆面板拔掉编辑框 | `blur` 不保证触发，用户输入无声丢失 |
| 打分层 `limit` 把预算架空 | 分层等于没分 |
| `layer_hint` 挂错对象 | 提示落到错误的层 |
| `index.html` 编码损坏 | 中文乱码 |

---

## 13. 相关文档

| 文档 | 什么时候读 |
| --- | --- |
| [`DIALOGUE_CONTRACT.md`](DIALOGUE_CONTRACT.md) | 改对话/记忆/决策前**必读**（冻结契约） |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 数据契约与 REST（注意 `docs/README.md` 标的过期条目） |
| [`DESIGN_SPEC.md`](DESIGN_SPEC.md) | 做界面前（视觉唯一权威） |
| [`CHANGELOG.md`](CHANGELOG.md) | 每次开工前扫一眼 |
| `../任务表.md` | 分工与共同约定（**以仓库上一级那份为准**） |
