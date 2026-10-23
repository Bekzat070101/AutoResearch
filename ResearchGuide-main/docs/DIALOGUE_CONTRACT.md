# DIALOGUE_CONTRACT · 对话内核契约 v2（2026-10-18 本地修改）

> 本文冻结「环境观察 → 决策 → 工具调用」闭环的接口与语义。实现见 `server/dialogue.py`、`server/tools.py`、`server/memory.py`。
> 与本文冲突时以本文为准；改动须同步 [CHANGELOG](CHANGELOG.md) 与 [ARCHITECTURE](ARCHITECTURE.md) §5。
>
> 设计来源：[02-任务2方案草案](../../../审计与方案/02-任务2方案草案.md)、[03-Agent环境与召回](../../../审计与方案/03-Agent环境与召回.md)。

---

## 本轮职责与首次使用规则

对话只获取和维护用户背景、回答疑问；具体论文带读、出题和判答由 `server/tutor.py` 的独立 Agent 负责。
对话不作为软件主入口，不布置任务，不增加当前论文读取工具；原固定信息栏保留。首次推荐进入条件已按用户新要求更新为下述 w100 规则。
首次开场介绍可选的树洞成绩单全选、复制、粘贴方法，明确不提供也能继续；已介绍或用户拒绝后不反复索要。
`guidance` 带上跨近期窗口的介绍状态、全部有效交流偏好和既有进入判定。用户直接提问先答，按上下文最多补问一项，允许不追问。
前端满足现有条件后给一次进入邀请（同浏览器、同用户、同画像），置于回执前；「再聊聊」关闭后不重复提示，页头跳过按钮保留。
第二次生成看到实际 `memory_write`，不能把被拒的写入说成已保存。成绩单支持明确「只预览」不落课程与能力记录，以及「追加」保留原有课程。

### w99：成绩单导入后继续同一轮对话

确定性解析负责课程写入、统计和准确回执；成功导入不再提前结束本轮。解析结果以 `transcript_import` 注入环境，随后继续正常模型决策和回复，并与短回执合为同一条助手消息。SSE 的 delta 拼接须与最终 reply 相同；模型不可用时也保留已完成的导入，并给出简短接话。

模型只补充 1–3 句：纯粘贴且未拒绝交流时，依据已知信息提出一个轻问题；资料已有则不重复盘问；用户只想预览或直接提问时尊重其意图。导入原文在模型近期上下文中替换为处理摘要，保留学期标题前的用户说明。课程仅说明接触过的内容，不证明熟练掌握。

预览仅指不写课程和派生能力，原始对话仍按正常聊天保存，禁止承诺“不留痕”。前端将课程/GPA/依据放在默认折叠的回执与“我的”课程区，主回复先让用户看到接话。“我的”保留原固定字段和进入规则，背景字段与账号/画像设置默认折叠，不显示填表缺项压力。

### w100：首次约五分钟引导与可跳过的推荐进入

用户本轮已明确更新此前 D5 的首次引导判定，其他模块职责不变。首次使用 `guide.assess` 的五方面：学习背景、关注主题/阅读目的、课程/实践基础、论文阅读经历/困难、希望怎样获得帮助。只按有效且有依据的自述或实际课程底稿判断覆盖，不按事实总条数、GPA、字数、轮数或经过五分钟判断。明确主题/目的且至少覆盖四方面时 `ready=true`。专业、成绩单均非必需，拒绝/未知不虚构为已了解，也不推断能力弱。

`onboard_done` 兼容字段现在表示推荐开始，等于 `guide.ready`。仅有身份+兴趣、旧 phase=done、单独选择方向都不再触发推荐。`GET /api/onboard/readiness` 返回可解释的各方面摘要，history 与 turn 同时带 `guide`。新账号仅显式进入后标记 entered，发送消息或刷新仍留在首次引导；既有日期之前的老账号兼容保留。

首次开场说明预计约五分钟、Agent 会引导、用户可主动补充重要信息、可跳过。一次补一个未了解且未拒绝的方面，具体提问先回答；只拒绝某方面不等于拒绝全部引导。达到推荐条件且本轮无明确问题时，由已提交事实生成一次完成回顾，不继续采集；同轮有问题则先答，由页面给推荐邀请。回顾不验证或声称掌握能力。常规后续对话不重做首次访谈。

信息不足时点击任意进入入口，前端获取最新覆盖度并显示可访问确认弹窗：再聊一聊 / 直接进入。取消不改变 entered 或当前输入，确认后才提交进入；条件满足则直接进入。`/api/onboard/enter` 不设硬门槛，始终允许提前进入，不挂对话串行锁。五方面不是强制表格。长聊环境保留准确回执的预览/导入历史摘要，预览原文仍为正常聊天记录，不新增课程。

界面仅“对话流 + 输入区”两块：回执与进入邀请随本轮回复滚动，建议按钮归输入区，无中间独立滚动带；长成绩单原文默认折叠。首次侧栏展示五方面的已知摘要，普通对话保留原方向侧栏。

## 0. 一句话架构

### w101：按现有流程，先邀请提供基本背景

用户本轮决定保留现有交流与进入流程，只调整首次认识的起点：先邀请院系、专业、年级及可选成绩单，再可选补充本学期在修课程。未分专业不要求定专业；成绩单可在树洞全选复制粘贴，不提供也可继续。开场不再问研究主题或阅读目标。仅贴成绩单时先问尚未知的院系/年级/专业，身份已知则不重复，可轻问一次未提过的在修课程。

之后回应用户当前的话，顺着自然出现的线索继续交流，不按五方面逐项采访，也不为了覆盖字段执着追问抽象目标、阅读经历或实践。专业知识库用于标准名称与背景理解，没问培养方案时不主动列分流和学分。在修自述用 current:* 保存，不擅自修改或声称修改成绩单。现有推荐覆盖规则、低信息确认、可直接进入和一次邀请保留；用户亲自修改的信息仍计为已知。原有历史开场不回写，新开场用于首次新会话。

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
      "value": "人可读的一句话；grade/age/enroll_year/pace 写成对象，见 §3.3 闸门②",
      "evidence_quote": "必须能在本画像近期消息里逐字找到",
      "target_fact_id": "replace/retract/support 时必填",
      "valid_until": "constraint:* 必填，ISO 日期或 null" }
  ],
  "tool_intent": null,
  "dialogue": {
    "move": "answer|clarify|correct|acknowledge_stop",
    "reason": "为什么选这个动作（写进 decision log，不给用户看）",
    "reply": "给用户的回复正文；tool_intent != null 时必须为 null",
    "offered_actions": [ { "id": "a1", "label": "用户可点的下一步" } ]
  },
  "next_action": null
}
```

约束：

- `tool_intent != null` ⟹ `dialogue.reply` 必须为 `null`，且 `next_action` 必须为 `null`（两者都由调用②给出）。
- `tool_intent == null` ⟹ `dialogue.reply` 必须有内容，`next_action` 仍必须为 `null`，不产生行动。
- `memory_ops` 可以为空数组。**允许零条**——寒暄、假设情境、引用他人不写入长期记忆。

## 2. TurnReply（调用②的输出契约）

只在执行了工具时调用。

```json
{
  "reply": "结合工具结果的回复正文",
  "next_action": null
}
```

`next_action` 始终为 `null`。旧模型返回行动也会被代码丢弃，不写 actions、不建 tasks。
`offered_actions` 只作可选回复短句，不承载任务。工具回复继续使用 JSON 以保留接口兼容。

## 3. Memory Key Registry（写入白名单）

**只有命中 registry 的 key 才能写入。** 未命中 ⟹ 丢弃该条 op，计入 `rejected_ops`。

### 3.1 按「时间跨度」分层

画像要回答的不只是「他是谁」，还有「这条多久会变、什么时候该重新问」。
所以每个 key 都归属一层。层用于组织记忆和分配注入预算，不要求逐层采集。

| 层 | 中文 | 变化速度 | 里的 key | 注入预算 |
| --- | --- | --- | --- | --- |
| `identity` | 身份 | 几乎不变 | `grade` `school` `major` | 6 |
| `practice` | 当前实践 | **最快，约 90 天过期** | `current:course` `current:project` | 6 |
| `capability` | 能力起点 | 每学期 | `base:*` `capability:<slug>` | 4 |
| `experience` | 经历积累 | 只增 | `experience:<slug>`（**行为写入**） | 3 |
| `interest` | 倾向 | 慢，但会变 | `goal:*` `interest:*` `direction:*` `style:*` | 4 |
| `constraint` | 临时约束 | **到点即失效** | `constraint:<slug>` | 4 |

记忆层展示顺序与提问顺序分开。现有 `LADDER_ORDER` 是身份 → 倾向 → 能力起点 → 当前实践，
仅作已知信息参考；问不问由用户当前问题决定，不主动派任务，也不要求填满当前实践。

`practice:*` 不填 `valid_until` 时自动补 `default_ttl_days=90`：半年前的项目不该被当成「他正在做的事」。

### 3.1.1 提问阶梯怎么落地

`memory.ladder_state(uid)` 算出六层各自的「有数了没有」（`LADDER_PROBE` 列出每层的判据 key），
以及最早的空白层 `next_layer`，随环境包一起给模型。

**这里是刻意的分工，不是没做完**：代码给真相（哪层空着是确定性的），模型定措辞
（问题是一句自由文本，靠关键词判它「在问哪一层」很脆），评测管结果
（首次对话及长聊回归用例）。
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
| `reading_experience` | practice | background | 单值 | declared | 用户自述读过/未读过论文，不是行为提交事件 |
| `reading_barrier` | interest | preference | 单值 | declared | 阅读时的具体困难 |
| `reading_support` | interest | preference | 单值 | declared | 希望怎样解释或帮助 |
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
   - value 内容不足 8 字 → 不算比例，但至少一个内容字（含常见缩写）在引文里，且不能只出现在被否定的地方

   下面几种 key 不按比例，按字段核对（`server/evidence.py`；模型抽、代码对，不让模型自检）：

   | key | value 形状 | 引文里要有 | 拒绝理由 |
   | --- | --- | --- | --- |
   | `grade` | `{"stage": 本科/硕士/博士/高中/null, "year": 1–6/null}`，或整串就是年级的字符串 | 按固定表认出的同一个（阶段, 第几年）；没说阶段的只撑本科或不写阶段 | `grade_not_in_evidence` |
   | `age` `enroll_year` `pace` | `{"value": 数或[低,高], "unit", "per"}`，或带数的字符串 | 同一个数、同量纲单位（时分秒、天周、月年可换算），per 都写了就得一样 | `number_not_in_evidence` |
   | `school` `department` `major` | 名字 | 别名表里同一实体的叫法（学校表、`curriculum.DEPT_ALIAS`、培养方案卡片）；库里没有的要原样出现；「数学」这类学科词要跟着系/院/专业 | `short_value_not_in_evidence` |
   | `interest:*` `goal:*` `field:*` | 原话片段 | 值（或去掉「对…感兴趣」、换成同组缩写）原样出现；不是原样就要有四字以上的公共片段当锚，收下也降成 `inferred` | `short_value_not_in_evidence` |

   范围按引文所在的**整条消息**算（模型只引「大四」，「我不是大四」的「不是」照样算），只认落在引文里的那一处说法。范围有这些：
   否定（含后置的「没兴趣」「没学过😂」「不是我的专业」，双重否定不算，并列的下一分句接着管，问出来的话题被下一句否定也算）、
   过去（以前、去年、「大一的时候」「是去年的兴趣」）、打算（想、以后）、假设（如果、假如）、改口（「打错了」、分句开头的「不对」，越过「等下」「抱歉」这种插话）、
   别人的事（室友、家人、老师、「别人给的建议」「替同学问的」「大四的学长」；隔着逗号接着管，到句末或他自己又当主语「而我…」为止；
   「我和同学都…」「在张老师组里」不算）、问句（问句里「我这个大四…」这种自我介绍除外）、只是提到（「是讲座的题目」「这个词」）、
   顺带提到（「看…的教程」「班长给我发了通知」，兴趣目标不查这一种）。条件句的后件（「如果…，我才会…」）和「只是理想状态」也算假设。
   年级、学校院系、年龄、每周时间全都不能沾；入学年份可以是过去；兴趣目标可以是打算。沾了就拒，`negated_in_evidence`。
   范围只管到分句（标点、但是）为止，「现在」只截断过去，否定管不到「的」后面被修饰的词（后面跟着数的除外）。

   字段各自另外要求：学校院系专业要作为学籍出现——名字前紧挨着「是、在、读、属于、同属、专业是、院系：」，或后面紧跟「大二、学生、在读、专业、的。」，
   或整句就是这个名字（回答「哪个院的」）；后面跟着「附近、门口、出版、的同学、教材」，被更长的名字包着，「法院」，都不算。
   入学年份要紧挨着入学说（「2024年入学」「24年进的北大」「入学年份2023」）或写成「24级」；每周时间不能是上课、通勤占掉的（「课余」「课题组」不算课）；
   结构化的数量和带单位的数量字符串一样严格：不认上下限（「不到五小时」「上限八小时」）、不认范围里挑一头、值写了「每周」原话也得说了。
   数量字符串只能是这一个量，夹带别的内容按 `number_not_in_evidence` 拒；只有一个光秃秃的数（「19」）照旧只比数。

   其余字段：英文词要整个出现（Java ≠ JavaScript）；短值也算支撑度；和原话共有的片段上说法要一致（原话否定/过去/打算/假设/别人/改口，值不能当成现在的事；
   值有否定原话没有也不行；「学过、接触过、完成了」本来就是过去；英文词或三个字以上的片段一处对不上就拒，更短的要全对不上）；
   除 base/capability/direction 外不许多出原话没有的两个字以上的内容。

   做不到的（要读懂句子，字面规则管不了）：反义词（「时间很少」撑住「时间很多」）、谓语归属（「电脑可以用，校园网不能用」撑住「电脑不能用」）、
   能力字段里没有依据的「精通、独立完成、掌握」（按契约降成 inferred 并标 needs_behavior_evidence，不升任务难度）。

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
    "trail_dir": "ai"
  },
  "guidance": {
    "entry_ready": true,
    "transcript_available": false,
    "transcript_introduced": true,
    "preferences": [{"key": "style:transcript", "value": "不想分享成绩单"}]
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
3. 方向背景（`active_plan`）、引导状态和交流偏好（`guidance`）；不注入待办任务
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

**所有工具只读。** 记忆变更走 `memory_ops` 并由代码校验；成绩单由独立的确定性解析入口更新。没有对话行动写入。

| 工具 | 参数 | 返回 | 失败语义 |
| --- | --- | --- | --- |
| `course.search` | `{query, limit?}` | `{ok, items[], term, retrieved_at}` | 返回 `{ok: false, error}`，如实展示，**绝不编造课程** |
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

## 6. 会话与历史行动兼容

`conversation_id` 缺省时沿用本画像的 open 会话，没有则新建。长聊的偏好和成绩单介绍状态不依赖最近 12 条消息。
对话不再创建行动、不注入项目实践阶段或待办任务，不展示任务卡及完成通知。
`next_action`、`pending_action` 始终为 null，history 的 `last_action`、`just_finished` 也为 null；保留字段以兼容旧客户端。
历史 actions/tasks 不删除，任务模块仍可读取已有任务。`/api/dialogue/action` 只允许 decline 关闭历史卡片；
对已有卡片 accept/complete 返回 400，跨画像或不存在返回 404，不再生成任务或伪造提交事件。

## 7. 确定性校验（代码侧，逐条）

对每个 `memory_op`，按顺序检查，任一不过即丢弃并记入 `rejected_ops`：

| # | 检查 | 不过的处理 |
| --- | --- | --- |
| 1 | `op` ∈ {add, replace, retract, support} | 丢弃 |
| 2 | `key` 命中 §3 registry 模式 | 丢弃 |
| 3 | `category` 与 registry 一致 | 用 registry 的值覆盖 |
| 4 | `evidence_quote` 非空，且是**本画像**近期消息的连续子串 | 丢弃 |
| 4a | value 形状对（结构化字段的对象只能有约定的键、合法的值） | 丢弃，`bad_type:value` |
| 4b | 引文撑得住 value（§3.3 闸门②：结构化字段逐项核对，其余按支撑度、数字、否定） | 丢弃或降级 |
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

  next_action / pending_action = 始终 null；不再产生对话行动。
  fabrication_risk = true 表示：检索失败、正文点名了具体名称，且定向重写也没修掉。
                     不该出现在正常流程里；出现即为产品原则被违反，需人工看这条对话（§9.1）。

POST /api/dialogue/stream              # 同一份内核，SSE
  { uid, conversation_id?, message }
  → event: stage   {"name": "observe|decide|memory|tool|compose", "tool"?: str}
    event: delta   {"text": "..."}      # 回复正文增量
    event: result  {…与 /turn 完全相同的响应体…}
    event: error   {"error": "..."}     # 流已开始后只能这样报错

POST /api/dialogue/action
  { uid, action_id, event: decline }  # accept/complete 对已有行动返回 400
  → { action }

GET  /api/dialogue/history?uid&conversation_id?
  → { messages[], conversations[], pending_action, onboard_done }

POST /api/onboard/enter
  { uid } → account（含 entered）。entered_at 只写第一次。
```

**流式的边界，别误解**：调用① 必须拿到完整 JSON 才能做校验，所以它不能流式。
能真正逐字产出的是**回复正文**——只在「本轮不需要调工具」时，由调用②（`llm.chat_stream`）
按 token 推送。需要调工具的那一轮，调用② 保留结构化回复协议，因此仍走 JSON（next_action 固定 null），
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

## 12. 明确不做（v2 范围外）

- 向量检索 / embedding（先用 SQL + 关键词，见下）
- 原生 function-calling（走 §1 的 `tool_intent` 字段；接口按可替换设计）
- 多工具并行调用
- 对话派任务、作业催交或接管研读流程
- 将完整固定字段、成绩单或定方向作为进入软件的前置条件
- 自动替用户决定终身方向
# 2026-10-23 接口补充：全局对话与已读文献

当前接口新增明确创建／列出会话；历史默认仅返回最近会话，并返回 conversation_id。
客户端发送 portrait_id 与 conversation_id，后端在现有用户锁中校验归属，分别以 409／404 拒绝错画像或不存在的会话。
同画像共享校验后的事实，近期上下文和原话证据池只取本会话。
新增 reading.search、reading.detail 只读工具；回复和历史可带 reading_sources。
只有通过阅读卡的文献进入已读索引，个人阅读数据随画像快照隔离。
完整字段、迁移与兼容说明见 [DIALOGUE_WORKSPACE.md](DIALOGUE_WORKSPACE.md)。
