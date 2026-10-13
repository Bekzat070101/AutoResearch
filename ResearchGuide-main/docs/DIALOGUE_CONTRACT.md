# DIALOGUE_CONTRACT · 对话内核契约 v1

> 本文冻结「环境观察 → 决策 → 工具调用」闭环的接口与语义。实现见 `server/dialogue.py`、`server/tools.py`、`server/memory.py`。
> 与本文冲突时以本文为准；改动须同步 [CHANGELOG](CHANGELOG.md) 与 [ARCHITECTURE](ARCHITECTURE.md) §5。
>
> 设计来源：[02-任务2方案草案](../../../审计与方案/02-任务2方案草案.md)、[03-Agent环境与召回](../../../审计与方案/03-Agent环境与召回.md)。

---

## 0. 一句话架构

**模型决定做什么，代码决定能不能做。**

旧的 W0 形态是「代码用规则选好动作 → 模型只改措辞」（见 `planner.py` 的 S0/S1/S2）。本契约反过来：模型在**一次结构化调用**里输出本轮理解、记忆操作、对话动作与工具意图；代码对每一项做**确定性校验**，合格的才执行，不合格的丢弃并记录。模型永远不直接写库。

一轮最多两次模型调用：

```text
请求
 └─ 组装环境包（代码，只读，按画像过滤）
     └─ 调用①  TurnProposal
         ├─ 校验 memory_ops → 提交（代码）
         ├─ tool_intent == null ? 用 dialogue.reply 结束
         └─ tool_intent != null
             ├─ 执行工具（代码，只读）
             ├─ 调用②  TurnReply（带工具结果）
             └─ 兜底：检索失败但正文点名了具体课程/书/论文
                 └─ 调用③  定向重写（见 §9.1，只在命中风险时发生）
```

**调用③ 是唯一的例外，且只在一种情况下发生**：工具返回 `ok=false`，而模型仍在正文里写了
《…》形式的具体名称。这时「不编造事实」这条产品原则已经被违反，不能把正文直接发给用户。
调用③ 只做一件事：保留原意与结构，删掉所有具体名称，改成说明该去哪里查。
两次调用是**正常路径**的上限，不是绝对上限；这一点在 §9.1 有完整语义，别把它当常态。

---

## 1. TurnProposal（调用①的输出契约）

```json
{
  "understanding": {
    "gist": "一句话复述用户这轮在说什么",
    "signals": [
      { "kind": "goal|interest|capability|constraint|preference|correction|meta",
        "value": "这条信息是什么",
        "evidence_quote": "用户原话里的连续片段" }
    ],
    "corrections": [
      { "targets_fact_id": "f_xxx", "why": "为什么这是纠正" }
    ]
  },
  "memory_ops": [
    { "op": "add|replace|retract|support",
      "key": "见 §3 registry",
      "category": "background|interest|capability|preference|experience",
      "value": "人可读的一句话",
      "evidence_quote": "必须能在本画像近期消息里逐字找到",
      "target_fact_id": "replace/retract/support 时必填",
      "valid_until": "constraint:* 必填，ISO 日期或 null" }
  ],
  "tool_intent": null,
  "dialogue": {
    "move": "answer|clarify|propose_action|execute_action|correct|acknowledge_stop",
    "reason": "为什么选这个动作（写进 decision log，不给用户看）",
    "reply": "给用户的回复正文；tool_intent != null 时必须为 null",
    "offered_actions": [ { "id": "a1", "label": "用户可点的下一步" } ]
  },
  "next_action": null
}
```

约束：

- `tool_intent != null` ⟹ `dialogue.reply` 必须为 `null`，且 `next_action` 必须为 `null`（两者都由调用②给出）。
- `tool_intent == null` ⟹ `dialogue.reply` 必须有内容，`next_action` 可以在调用①里给。
  理由：不调工具时不会再有第二批信息进来，本轮就可以收口到具体行动。
- `memory_ops` 可以为空数组。**允许零条**——寒暄、假设情境、引用他人不写入长期记忆。

## 2. TurnReply（调用②的输出契约）

只在执行了工具时调用。

```json
{
  "reply": "结合工具结果的回复正文",
  "next_action": null
}
```

`next_action` 形状：

```json
{ "action": "micro_task|explore_direction|review_progress|course_action",
  "title": "给用户看的一句话",
  "direction": "ai",
  "node_id": null,
  "payload": {},
  "based_on": ["f_ab12cd34"] }
```

代码在写库前会校验两条：

1. `direction` 必须属于当前画像的生效方向（§4），否则拉回到生效方向；
   没有生效方向时 `micro_task` / `course_action` 降级为 `explore_direction`。
2. **个性化闸门**：`based_on` 里至少要有 1 个本画像**生效事实**的 id。
   一个都引不到 ⟹ 整个 `next_action` 丢掉，本轮不给落点。

第 2 条是产品原则「个性化必须有依据」的代码化，也是实测逼出来的：
早期模型在只知道「信管大二、想走大数据」时就派「关掉讲义重写一段代码」——
这句话对任何一个信管大二学生都成立，等于没了解这个人。用户明确反馈过这一点。

**为什么放在代码里而不是只写在 prompt**：prompt 只能降低概率，而这个失败模式直接伤信任
（用户照做了才发现这事跟谁都能说）。放进代码后，模型必须**说出依据**才拿得到落点，
`based_on` 同时会带给前端显示成「依据你说的：…」——也就是把「凭什么给我这个」摊开给用户看。

**已知取舍**：闸门只能保证「有依据」，不能保证「依据够强」。模型仍可能引一条弱事实
（例如用 `grade` 去支撑一个其实是关于 Python 的任务）。这属于回答质量问题，
由 `eval/` 的 `task_carries_its_basis` 用例盯着，不靠代码硬判。

## 3. Memory Key Registry（写入白名单）

**只有命中 registry 的 key 才能写入。** 未命中 ⟹ 丢弃该条 op，计入 `rejected_ops`。

### 3.1 按「时间跨度」分层

画像要回答的不只是「他是谁」，还有「这条多久会变、什么时候该重新问」。
所以每个 key 都归属一层。层同时管三件事：**提问顺序、注入预算、什么时候重新问**。

| 层 | 中文 | 变化速度 | 里的 key | 注入预算 |
| --- | --- | --- | --- | --- |
| `identity` | 身份 | 几乎不变 | `grade` `school` `major` | 6 |
| `practice` | 当前实践 | **最快，约 90 天过期** | `current:course` `current:project` | 6 |
| `capability` | 能力起点 | 每学期 | `base:*` `capability:<slug>` | 4 |
| `experience` | 经历积累 | 只增 | `experience:<slug>`（**行为写入**） | 3 |
| `interest` | 倾向 | 慢，但会变 | `goal:*` `interest:*` `direction:*` `style:*` | 4 |
| `constraint` | 临时约束 | **到点即失效** | `constraint:<slug>` | 4 |

层是**有序**的，顺序就是提问阶梯：身份 → 当前实践 → 能力起点 → 经历积累 → 倾向 → 临时约束。
设计取向：**当前实践排在很前面**——它最便宜（一句话能答）、最可靠（是行为不是自述），
而且任务必须挂在「他手头已经在做的事」上。抽象偏好最难问也最不可靠，所以放后面，能推就推。

`practice:*` 不填 `valid_until` 时自动补 `default_ttl_days=90`：半年前的项目不该被当成「他正在做的事」。

### 3.1.1 提问阶梯怎么落地

`memory.ladder_state(uid)` 算出六层各自的「有数了没有」（`LADDER_PROBE` 列出每层的判据 key），
以及最早的空白层 `next_layer`，随环境包一起给模型。

**这里是刻意的分工，不是没做完**：代码给真相（哪层空着是确定性的），模型定措辞
（问题是一句自由文本，靠关键词判它「在问哪一层」很脆），评测管结果
（`ladder_asks_earliest_gap` / `ladder_no_skipping_to_capability`）。
本轮只有两次模型调用，没有余量再加一次「问题分类」。

阶梯只管「你想了解他时问什么」，**不是每轮都必须问**。上下文在别处就顺着走，
否则会变成一张问不完的表。已经填过的层不重问；能推出来的不问。

两个已经修掉的实证问题：

- `identity` 的判据里**没有 `department`**：用户说「我是信管的大二学生」（年级 + 院系都已给出）
  这层仍被判成空着，模型就接着问了一个已经答过的问题。现在判据是
  `grade` / `department` / `major` / `enroll_year`。
- **不问学校**。本产品只服务北大学生，学校是默认前提（`Slot(presumed=...)`，
  核对页显示为「北京大学」且不算空格）。之前 `LADDER_PROBE` 里有 `school`、
  身份层的说明也是「年级、学校、专业」，模型就真的问出了「你是哪个学校的」——
  对用户来说这是在问一件已经知道的事。他若主动说起别的学校，那一轮如实说
  知识库只覆盖北大培养方案。

### 3.2 key 与规则

| key 模式 | 层 | category | 基数 | 来源要求 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `grade` | identity | background | 单值 | declared | 年级 |
| `school` | identity | background | 单值 | declared | 学校 |
| `major` | identity | background | 单值 | declared | 专业/方向 |
| `current:course` | practice | background | 多值 | declared/inferred | 在上的课（自动 90 天过期） |
| `current:project` | practice | background | 多值 | declared/inferred | 在做的项目（自动 90 天过期） |
| `base:math` | capability | capability | 单值 | declared | 数学基础 |
| `base:code` | capability | capability | 单值 | declared | 编程基础 |
| `base:math_code` | capability | capability | 单值 | declared | 两者都有 |
| `base:none` | capability | capability | 单值 | declared | 都还浅 |
| `capability:<slug>` | capability | capability | 多值 | declared | 具体已学 |
| `goal:<slug>` | interest | interest | 多值 | declared | 明确目标 |
| `interest:<slug>` | interest | interest | 多值 | declared/inferred | 兴趣主题 |
| `direction:<code>` | interest | interest | **互斥单值** | declared | 六方向之一 |
| `style:<slug>` | interest | preference | 单值 | declared | 学习节奏 |
| `constraint:<slug>` | constraint | preference | 多值 | declared | **必须带 `valid_until`** |
| `experience:<slug>` | experience | experience | 多值 | **behavior** | 只能由行为证据产生，模型不能写 |

规则：

1. `direction:*` **互斥**：写入新的自动把旧的置 `superseded`。这一条从根上修掉「NBA 取第一条、项目取最后一条」的分歧（archive 中的 M3）。
2. `constraint:*` 无 `valid_until` ⟹ 拒绝。临时约束不得变成永久设定。
3. `experience:*` 若 `source != behavior` ⟹ 拒绝。用户自述"我学过"进 `capability:*`（declared），不进 experience。
4. `capability:*` 若声称**已掌握**但只有 declared 证据 ⟹ 降级写入并在 `notes` 标注 `needs_behavior_evidence`，不得升级任务难度。
5. key 一律小写、`<namespace>:<slug>`。slug 允许 `[a-z0-9\u4e00-\u9fff_.-]`。
   **中文 slug 必须放行**——模型写 `current:选课数据大作业` 是很自然的，
   只放行 `[a-z0-9_]` 会把真实信息整条丢掉（这个错犯过两次，第二次是中文）。
   空格与下划线由 `canon_key` 统一成连字符、削掉首尾分隔符，让同一个东西尽量只落一个 key。
   （注意：ASCII-only 从来没真正解决同义重复——`ai`/`AI`/`人工智能` 照样是三条；
   同义不同词那部分靠 single 基数的 supersede 和幂等 add 兜。）

### 3.3 写入的三道闸门

「写错」和「漏写」的代价**不对称**：漏写一条，下次再问一遍就行；
写错一条，它会**静默污染之后的每一个决策**，而且没人会发现。
所以默认不写，要写就举证。三道闸门任一不过即拒（见 §7）：

1. **闸门① 在册**：key 命中 registry，且该 key 允许这个 `source`。
2. **闸门② 引文撑得住值**：`evidence_quote` 必须是本画像内用户原话的连续片段，
   且其内容要**支撑**写进去的 value。代码算「value 的内容有多少能在引文里找到」：
   - `≥ 0.8` → 收
   - `0.5 ~ 0.8` → 收，但**降级为 `inferred`**，`notes` 标 `partially_inferred`
   - `< 0.5` → **拒**（`value_exceeds_evidence`）
   - value 内容不足 8 字 → 跳过本检查（`AI`、`大二` 是规范化缩写，逐字要求会误杀）

   阈值是拿真实例子校准的（`审计与方案/calibrate_support.py`，9/9 符合预期）：
   真实事故那条是 **0.29**，而正常的值落在 **0.67–1.00**，分得开。
3. **闸门③ 说得清改变哪个决策**：`affects` 必填，且必须命中白名单：
   `task_difficulty` `task_kind` `direction_choice` `question_next` `course_pick` `pace` `feedback`。
   **填不出来就说明它没资格进记忆。** `retract` / `support` 不要求重新举证。

这三道闸门解决的是真实事故：模型把用户原话里的 "Vibecoding" 自己解读成
「依赖 AI 生成、代码不是他写的」写进 `base:code`，之后所有任务都建立在它自己编的前提上。


## 4. 环境包（Env Packet）

每轮由代码组装，**先按 portrait_id 过滤再组装**，不全量读库。

```json
{
  "portrait_id": "p_xxx",
  "memory_version": 12,
  "time": "2026-10-01T16:00:00+08:00",
  "current_message": "用户这轮的完整消息（不截断）",
  "recent_context": [ { "role": "user|assistant", "text": "...", "at": "..." } ],
  "active_plan": {
    "direction": "ai",
    "direction_source": "direction:ai",
    "node_id": null,
    "pending_action": { "action_id": "act_x", "title": "...", "status": "offered" }
  },
  "recalled_memory": [
    { "id": "f_x", "key": "interest:attention", "value": "...", "source": "declared",
      "confidence": 0.6, "layer": "倾向", "affects": "direction_choice",
      "evidence_quote": "...", "valid_until": null }
  ],
  "world_knowledge": [],
  "lib_facts": [
    { "用户说到的词": "信管", "库里认成": "院系（3 个专业）", "院系": "信息管理系",
      "命中": [ { "专业": "图书馆学专业", "毕业总学分": "129～135学分",
                  "核心必修课": ["…"], "来源页": "书内 p387" } ],
      "说明": "分流名额与当年方案以院系教务为准。" }
  ],
  "tool_results": [],
  "degraded": false
}
```

召回顺序（**严格按此顺序**，预算从前往后消耗）：

1. 当前消息（完整）
2. 近期对话（本 conversation 最近 N 轮，按 token 预算截取，**保留最新纠正**）
3. 当前行动状态（`active_plan`）
4. 按本轮主题召回的持续记忆。**不是平表取 top-8，是按层给预算**（`LAYER_BUDGET`，见 §3.1）：
   每层先取够自己的名额，层内再按相关度排序，新近度只在平局时起作用。

   为什么改：全局抢坑位时，一条课程成绩可能把「他的方向」挤出去，
   而且没有任何信号告诉模型「记忆被截断了」。分层之后，身份 / 当前实践 / 约束
   这几层小而必带，倾向 / 经历按相关度填——这才叫「不是全量注入，是每层给预算」。

5. `lib_facts`：**这一轮用户说到的院系/专业，代码先把知识库的原文事实查好**（只读、
   有界、带来源页：最多 2 个名字 × 4 张卡 × 6 门课）。和按层召回记忆是同一个模式——
   代码组装环境包，模型决定怎么用；不是"代码替模型决策"。

   为什么不能等模型自己调工具：用户第一句是「我是信管的大二学生」时，「信管」是个
   **简称**，不是一个规范的院系名。实测模型在这一轮既不查库、又反问「你是哪个学校？」
   （默认北大，本就该知道）。把库里的名单先摆上桌之后，同一句话的回复变成
   「你们系三个方向（信息管理与信息系统、图书馆学、大数据管理与应用）……」，并直接
   落到「你这学期在上的课里，哪一门你会额外花时间」，不再问一个已经知道的事。
   实测还发现另一面的风险：模型引用这些事实时会顺手加戏（把只出现在两个专业清单里的
   《数据库系统》说成"三个方向都有"），所以提示词里加了一条「只写它真的写了的东西」。

   **这里不能留全局条数上限。** 曾经留了 `limit=10`，而拉平是按层序的，
   于是靠后的「倾向 / 临时约束」会被这个总数截掉——等于把平表抢坑位换了个地方犯。
   实测：9 条能力 + 9 条当前实践的情况下，`limit=10` 让倾向和临时约束**双双消失**。
   现在总上限由各层名额之和决定，只保留一个很大的 `RECALL_HARD_CAP` 防异常数据撑爆上下文。

   层内相关度只认**多字词**命中。早期那个「单字命中 0.05/字」在中文里等于噪声：
   一句话的每个字都会命中一堆无关事实，排序基本是随机的。
5. 依附于已召回记忆的证据原文（少量）
6. 世界知识（只在需要时取，带来源与检索时间）
7. 过期项过滤：`valid_until < now` 或 `status in (retracted, deleted, superseded)` 一律不进包

**调试可观测**：响应里带 `trace` 字段，列出召回的 fact_id 列表、过滤掉的 fact_id、基于哪个 `memory_version` 决策。不记录消息正文到普通日志。

## 5. 工具 registry

**所有工具只读。** 任何状态变更走 `memory_ops`（记忆）或 `next_action`（行动），由代码执行。

| 工具 | 参数 | 返回 | 失败语义 |
| --- | --- | --- | --- |
| `course.search` | `{query, limit?}` | `{ok, items[], term, retrieved_at}` | 返回 `{ok: false, error}`，如实展示，**绝不编造课程** |
| `project.search` | `{direction, stage, keywords?}` | `{ok, items[], sources, empty_reason}` | 查不到就空着，写 `empty_reason` |
| `project.review` | `{project_id}` | `{ok, review}` | 非本画像的 project_id ⟹ `ok: false` |
| `transcript.summary` | `{keyword?, limit?}` | 无 keyword 只回 `{summary, by_term}`；带 keyword 另回 `{matched[], matched_count}` | 没录成绩单 ⟹ `has_transcript: false`，如实说没有 |
| `major_lookup` | `{query, minor?, limit?}` | `{ok, kind, 命中[], 院系?, 近名[], 说明?}` | 未命中 ⟹ `命中: []` + 近名 + 覆盖说明，**绝不编专业名** |
| `major_detail` | `{name}` | `{ok, 卡片, 学分结构, 必修课[], 兄弟专业[]}` | 没查到 ⟹ `ok: false` + 覆盖说明 |
| `match_transcript` | `{codes, low_only?, top?}` | `{ok, 院系排名[], 专业排名[], 识别到课号, source, note?}` | 没成绩单 ⟹ `has_transcript: false`；课名对不上 ⟹ 空排名 + 如实说明 |
| `minor_programs` | `{query?, dept?, limit?}` | `{ok, 命中[]}` | 查不到就空着 |
| `course_lookup` | `{code}` | `{ok, 课号, 课程名, 必修它的专业[]}` | 课号不合法 ⟹ `ok: false, error` |

后 5 个是院系-专业知识库（`server/curriculum.py`）的工具，定义在
`server/mcp_curriculum.py` 的 `TOOLS` 里，MCP 客户端拿到的就是这一份——**不给同一个能力
维护两套说明**。对话侧（`server/tools.py`）只多做三件代码该做的事：

- **院系就是「有哪些分流」的答案**：`major_lookup("信管")` 或官方名 `信息管理系`
  返回该院全部专业（用户问的就是这个）；简称表 `curriculum.DEPT_ALIAS` 只登记能对上
  库内院系的词，写错的简称会让答案「像是查到了」。
- **整句当 query 时从原话里抠名字**：模型常把「信管有哪些专业分流」整句塞进 `query`，
  未命中就按 `curriculum.mentions()` 在原话里找库内名字再查一次，`query` 里如实写实际用了什么。
- **课名 → 课号由代码查索引**：北大成绩单只有课名，认专业要课号。模型给课名、或画像里
  已录成绩单时，用 `curriculum.codes_for_names()` 做**确定性反查**（归一后完全相同才算），
  认不出的列进 `note`，不模糊猜——猜错一门课，认出来的院系就是错的。

规则：

- 模型只能调用表内工具。未登记的工具名 ⟹ 丢弃 intent，按无工具处理。
- 工具结果写入 `tool_results` 并作为调用②的输入。
- **工具失败不得写成已完成**。失败结果原样进入下一轮观察，并在回复里如实说明。
- 单轮只执行一个工具（v1）。模型需要多次则在后续轮次继续。
- **工具名写歪了要收敛，不能整轮降级**：实测模型会把 `tool_intent` 写成
  `{"name": ...}`（契约里是 `{"tool": ...}`），参数也会写成 MCP 的 `arguments`。
  `_clean_proposal` 把 `tool` / `name` / `tool_name` 和 `args` / `arguments` 都认下来，
  再规整成契约形状；否则 `tool_intent` 被判为空、`dialogue.reply` 又是 `null`，
  整轮被当成无效输出降级——用户看到的就是「它明明有工具却说没工具」。
- 系统提示词里**要写出 `tool_intent` 的形状**（字段名 `tool`、参数放 `args`）。
  契约只给 `"tool_intent": null` 的模板时，模型会自己猜字段名，这就是上面那条的成因。

### 5.1 为什么 `transcript.summary` 默认只给汇总

成绩单是 §4 优先级里的 **P3：不进上下文，需要时用工具查**。

一个人 20~40 门课。整份塞进环境包有两个后果：
一是挤掉真正决定判断的那几条（身份、在做的事、方向）；
二是**逼模型自己在几十条里找**，而它找错一条就会说出「你没修过这门课」这种
用户一眼就能看穿的错话。

所以默认只回汇总（总绩点 / 学分 / 分学期），要具体课程必须带 `keyword`。
这和「课程成绩单不该塞进 `facts` 的 key-value」是同一个决定的两面：
**底稿留在表里，结论才进上下文。**

## 5.2 成绩单（enrollments）

底稿单独一张表，一门课一条记录：`course / grade / credits / term / kind / status`。

**为什么不塞进 `facts`**：一个人 20~40 门课、每门四个字段，塞进 key-value 的
`value` 会变成一坨文本——**正是 `base:code` 那起事故的形态**
（`'修过计算概论B(Python)93、程序与算法(Python)92…课程基础扎实'`）。
一坨文本既撑不住引文校验（闸门②无法判断哪部分有依据），
也没法按学期 / 成绩 / 关键词查询。

`facts` 里只放**从成绩单推出来的能力结论**（如 `capability:python`），
并写明依据了哪几门课。底稿和能力是两层：能力可推翻、可追溯，成绩单是事实。

**粘贴格式**（树洞 / 教务导出，一行一项，顺序固定）：

```text
25-26学年度2学期      ← 学期标题，决定下面这些课属于哪个学期
3                      ← 学分
学分                   ← 字面量，用来锚定一条记录的起点
金融会计               ← 课程名
任选                   ← 课程性质（可缺省，可多行，取最后一行）
W                      ← 成绩
```

解析器在 `server/transcript.py`，规则移植自开源计算器
[PKUMuZi/pku-gpa-calculator](https://github.com/PKUMuZi/pku-gpa-calculator)（MIT）。
对照验证见 `审计与方案/verify_transcript_vs_reference.js` + `verify_transcript_my.py`：
拿上游**自己的示例成绩单**跑上游 JS 和我们的 Python，50 条逐条对齐。

**绩点**：`GPA(x) = 4 − 3(100−x)²/1600`。

一处**有意**与上游不同：低于 60 分我们记 0，上游不设下限。
上游的写法在 50 分时会算出 `-0.6875` 的负绩点，累加进平均会把别的课拖下去；
负绩点没有意义，北大教务口径也是不及格记 0。
要改回上游行为，把 `transcript.GPA_FLOOR_AT_ZERO` 设成 `False`。

**汇总口径**（与上游一致）：

- 通过学分 = 所有通过（含「合格」）的课，不要求有分数
- 计 GPA 学分 = **只有百分制分数**的课。`W`/`I`/`合格`/`不合格` 不进分母

这条区分容易写错：把「合格」当成 60 分算进去，会把总绩点算低。

## 5.3 能力由代码从成绩单推导（`transcript:*`）

`memory.sync_transcript_facts(uid)` 在成绩单变动后重新推导，幂等。

| key | 来源 | 谁写 | 例 |
| --- | --- | --- | --- |
| `capability:<slug>` | declared / inferred | 模型或用户 | 「Python 只会照着教程抄」 |
| `transcript:<area>` | **derived** | **只有代码** | 「修过 3 门数学类课程，学分加权平均 89.5：概率统计 (B) 96、线性代数 (B) 88…」 |

**为什么分成两个 namespace**：混在一个 key 上就分不清哪部分是硬证据、哪部分是自述。
`base:code` 那起事故就是把两者搅在一起写出来的。

**两条红线**：

1. **只写事实，不写判断。** value 里只准出现「修过哪几门、考了多少、加权平均多少」，
   不出现「基础扎实」「能力偏弱」——那是推断，写进事实就再也分不清了。
   难度判断交给读到这条事实的模型自己做。
2. **只统计通过的课。** 挂了的高等数学不能算「修过数学类课程」。

**模型写不了 `transcript:*`**（registry 里 `sources=("derived",)`，写了返回 `derived_only`）。
`_registry_doc()` 也不再把它列进「可写 key」——列了就是叫模型去撞墙，白花一次调用。

**用户是最终权威，sync 必须让路**（三种情况都不加、不改、也不撤）：

- 用户改过（`source=user_edit`）
- 用户删过（revision 里 `operation=user_reject`，**按 key 记**——
  按 fact id 记会失效，因为重新推导可能新建一行）
- 成绩单里推不出来了（正常过期，此时**要**撤掉）

撤除这一步最要紧：过期的硬证据比没有更糟，因为它看起来是最可信的那类东西。

注入优先级：`transcript:*` 属 capability 层，层内排序有 0.9 权重加成——
坑位不够时硬证据压过自述（0.9 < 1.0，所以相关度仍能压过它）。

**成绩单明细本身仍是 P3**（不进上下文，走 `transcript.summary` 工具查）。
进上下文的只有这几条推导出来的结论。

## 6. 对话与行动生命周期

```text
conversation: open ──────────────► closed
                 （同一画像同一时刻只有一个 open）

action: offered ─► accepted ─► in_progress ─► completed
           │            │              │
           └────────────┴──────────────┴──► abandoned（用户换话题/明确不做）
           └──────────────────────────────► expired（7 天未动，不再追问）
```

- `conversation_id` 缺省时，取本画像 `status=open` 的会话；没有就新建。
- 上一轮的 `next_action` 写入 `actions` 表，状态 `offered`。
- 下一轮：用户接受/开始做 ⟹ `accepted`；用户明确拒绝或换方向 ⟹ `abandoned`；两者都由模型在 `understanding.signals` 里表达，**代码落状态**。
- `active_plan.pending_action` 只填**非终态**的 action。终态的不进环境包，避免反复追问。
- 同一画像同时最多一个非终态 action；新的 `next_action` 生成时，旧的 `offered` 置 `abandoned`。

## 7. 确定性校验（代码侧，逐条）

对每个 `memory_op`，按顺序检查，任一不过即丢弃并记入 `rejected_ops`：

| # | 检查 | 不过的处理 |
| --- | --- | --- |
| 1 | `op` ∈ {add, replace, retract, support} | 丢弃 |
| 2 | `key` 命中 §3 registry 模式 | 丢弃 |
| 3 | `category` 与 registry 一致 | 用 registry 的值覆盖 |
| 4 | `evidence_quote` 非空，且是**本画像**近期消息的连续子串 | 丢弃 |
| 5 | `replace/retract/support` 的 `target_fact_id` 存在且属于本画像 | 丢弃 |
| 6 | `constraint:*` 有 `valid_until` | 丢弃 |
| 7 | `experience:*` 的 source 为 behavior | 丢弃 |
| 8 | 声称掌握但无行为证据的 `capability:*` | 降级 + 标注 |
| 9 | `direction:*` 互斥 | 写入新的，旧置 superseded |

`confidence` **由代码按 source 赋值**（declared 0.6 / inferred 0.4 / behavior 0.8），模型不得指定。

## 8. Decision Log

每次决策落一条，用于回答「因为你刚说了 X，我把 Y 改了，所以下一步变成 Z」。

```text
decisions(decision_id, portrait_id, conversation_id, memory_version,
          move, reason, rationale_refs[], tool_calls[], rejected_ops[],
          degraded, created_at)
```

## 9. 降级语义

| 情况 | 行为 |
| --- | --- |
| 未配置 LLM key | 走规则版 turn（等价 W0 行为），响应 `degraded: true` |
| 调用①两次解析失败 | 同上；本轮消息仍保存，不写任何记忆 |
| 调用②失败 | 用工具结果的模板化摘要作为回复，`degraded: true` |
| 工具失败 | 结果进 `tool_results`，`ok: false`，回复如实说明 |
| 模型声称"我记住了"但 op 被拒 | 只报告实际提交成功的变化；回复里的承诺以提交结果为准 |

### 9.1 不编造事实的代码侧兜底

结构化路径（写进记忆的事实）由 `memory.py` 的引文逐字校验兜住，**正文里的具体名称没有对应约束**，
所以单独加一道：

```text
工具 ok=false
 且 reply 命中 《…》(2–40 字)
   ├─ 调用③ 定向重写（只删名称、保留原意）→ 重写后无名称 ⟹ 用重写稿
   └─ 重写仍带名称 / 调用失败 / 抛异常 ⟹ 保留原文，fabrication_risk = true
```

设计取舍：

- **为什么是重写而不是删除**：直接正则删掉《…》会把句子打碎（「先看，再上」），比不给名字更糟。
- **为什么保留原文而不拦截**：宁可给出一段带风险标注的回复并让用户看到内容，也不要因为模型不听话
  就让这一轮彻底失败。风险进日志、进响应字段，可被统计——这比假装没发生更诚实。
- **为什么只在工具失败时触发**：工具成功时名称来自真实检索结果，动它反而是在删真数据。
- **实测结论**：这条规则靠 prompt 只能压到偶发，不能压到零。`eval/` 里
  `tool_failure_is_honest` 用例就是专门盯它的。

## 10. 接口

```text
POST /api/dialogue/turn
  { uid, conversation_id?, message }
  → { reply, conversation_id, memory_version, facts_added[], facts_changed[],
      rejected_ops[], tool_results[], next_action, pending_action, offered_actions[],
      degraded, fabrication_risk, trace }

  next_action      = 这一轮决定给的落点；模型没给就是 null。
                     不要用「当前还挂着的行动」回填这个字段，否则「这轮该不该派任务」从响应里看不出来。
  pending_action   = 这一轮结束后仍开着的行动（界面状态）。同一画像最多一个。
                     用户说停（move=acknowledge_stop）时，开着的行动会被置 abandoned。
  fabrication_risk = true 表示：检索失败、正文点名了具体名称，且定向重写也没修掉。
                     不该出现在正常流程里；出现即为产品原则被违反，需人工看这条对话（§9.1）。

POST /api/dialogue/stream              # 同一份内核，SSE
  { uid, conversation_id?, message }
  → event: stage   {"name": "observe|decide|memory|tool|compose", "tool"?: str}
    event: delta   {"text": "..."}      # 回复正文增量
    event: result  {…与 /turn 完全相同的响应体…}
    event: error   {"error": "..."}     # 流已开始后只能这样报错

POST /api/dialogue/action
  { uid, action_id, event: accept|decline|complete }
  → { action }

GET  /api/dialogue/history?uid&conversation_id?
  → { messages[], conversations[], pending_action, onboard_done }

POST /api/onboard/enter
  { uid } → account（含 entered）。entered_at 只写第一次。
```

**流式的边界，别误解**：调用① 必须拿到完整 JSON 才能做校验，所以它不能流式。
能真正逐字产出的是**回复正文**——只在「本轮不需要调工具」时，由调用②（`llm.chat_stream`）
按 token 推送。需要调工具的那一轮，调用② 同时要产出 `next_action`，因此仍走 JSON，
`delta` 事件不会出现，前端只显示阶段行。裸 SSE 的 `stage` 事件在所有情况下都是真实阶段，
不是假进度条。

兼容：`POST /api/onboard/message` 保留签名。配了模型时内部转调对话内核并把结果翻译回旧响应形状
（`onboard_state` 由内核同步维护，额外字段挂在 `dialogue` 上）；**没配模型时仍走原向导**，
保证离线 demo 与既有测试不受影响。旧端点不删除。

## 11. 验收（对应 archive 03 §8，逐条可测）

1. 画像 A 讲过项目经历，切到空画像 B 问同样的问题 ⟹ B 的环境包里**不含** A 的任何 fact/message/task/project。
2. 同一画像隔天继续 ⟹ 能召回未完成问题；无关旧事不强行插入。
3. 用户纠正「本周只有十分钟」「并非不喜欢 AI」⟹ 下一轮行动与表述改变；`valid_until` 过后不再生效。
4. 用户删除一条记忆 ⟹ 该条、其旧摘要、派生建议均不再进环境包。
5. 同一提交重试、模型超时、检索失败 ⟹ 不产生重复经验、不虚假达标、不跨画像混入。
6. 每轮 `trace` 可回答：召回了哪些 fact_id、过滤了哪些过期项、基于哪个 memory_version。

## 12. 明确不做（v1 范围外）

- 向量检索 / embedding（先用 SQL + 关键词，见下）
- 原生 function-calling（走 §1 的 `tool_intent` 字段；接口按可替换设计）
- 多工具并行调用
- 自由闲聊本身（每次交互都要落到一件具体的事，这是产品铁律）
- 自动替用户决定终身方向
