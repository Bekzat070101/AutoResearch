# DESIGN_SPEC · 启研设计规范 v1.1

> 从旧 Demo「学科瞭望」视觉体系提炼，是本项目**唯一的 UI 视觉权威**。
> 所有新页面/组件必须遵循本规范；规范未覆盖的，参照现有页面类推并回填本文档。
> 实装参照：[web/css/styles.css](../web/css/styles.css)（`:root` 变量区为唯一色值/字体来源）。

---

## 1. 设计立场

面向大一大二的学术工具，气质 = **安静、可信、有书卷气**：

- 低饱和底色 + 一个主色（青绿 teal），信息密度友好；
- 克制的圆角与阴影，不用毛玻璃、不用重渐变、不做暗色模式（V1）；
- 事实与证据永远高亮可辨（引用、来源、置信度是产品核心，必须醒目但不刺眼）。

## 2. 色彩（token 与代码一一对应，唯一来源是 styles.css 的 `:root`）

| Token | 值 | 用途 |
| --- | --- | --- |
| `--bg` | `#f5f2ea` | 页面底色（纸面） |
| `--surface` | `#fffdf8` | 卡片 / 面板 / 输入框 |
| `--sunken` | `#eeeadf` | 悬停底、AI 气泡、提示块 |
| `--ink` / `--ink-2` / `--ink-3` | `#1c2823` / `#4d5a54` / `#85908a` | 正文 / 次要 / 辅助文字，只用这三级 |
| `--line` / `--line-strong` | `#e4dfd2` / `#d2cbba` | 分隔线 / 可交互元素描边 |
| `--teal` / `--teal-soft` | `#0f6e65` / `#e2efea` | **主色**：主按钮、当前节点、用户气泡、做到 ✓ |
| `--coral` / `--coral-soft` | `#b4532f` / `#f7ebe4` | 只给「未做到 / 删除 / 新写入的行为证据」 |
| `--gold` / `--blue` / `--violet` | `#a07424` / `#2f5aa8` / `#6b4fb0` | 事实类别圆点，不做大面积底色 |
| `--night*` | `#0c0f0e` 等 | 仅首页夜色；`--night-accent` 为点线图主色，`--night-mark` 为序号游标 |

**配色纪律**：一屏只有一个视觉重点（今日=建议卡，方向=树上「你在这里」，任务=提交按钮）；主色面积 ≤ 20%；不加渐变底、不加光晕。

## 3. 字体

| 层级 | 字体 | 用法 |
| --- | --- | --- |
| 标题/数字 | `--serif`：Source Serif 4 / Noto Serif SC → 宋体回退 | 工作区标题 30px、卡片标题 20–26px、分数 |
| 正文/UI | `--font`：系统字体（PingFang SC / 微软雅黑） | 正文 15px/1.7，辅助 13px，标注 12px |

- 衬线字体走 `fonts.googleapis.cn` 镜像且不阻塞首屏；正文不加载网络字体（国内网络下 Google Fonts 会卡住首屏）；
- 数字用 `tabular-nums`。

## 4. 形状 / 间距 / 动效

- 圆角：`--r-lg 14px`（面板）/ `--r-md 10px`（按钮、输入）/ `--pill`（chip、节点）；
- 间距只用 `--gap-1..7`（4/8/12/16/24/32/48）；页面统一左对齐，左边距 48px（窄屏 20px）；
- 不用投影区分层级，用 1px 描边；只有抽屉和弹窗有投影；
- 动效：进入 `riseIn` 0.35s、新事实 `factIn` 珊瑚底淡出、树 `linkIn` 连线生长；过渡统一 `0.15s`；`prefers-reduced-motion` 时全部关闭。

## 5. 组件清单（新页面直接复用）

| 组件 | 类名 | 说明 |
| --- | --- | --- |
| 侧栏 | `.side-nav` + `.side-brand` + `.nav-toggle` + `.nav-btn` + `.nav-soon` + `.side-foot` | 今日 / 对话 / 研读 / 定位（待开发） / 项目（待开发）。作业不占栏目。对话按钮在进入启研后才出现。侧栏可收起（`rg_nav_collapsed`）。窄屏变顶栏，「待开发」用 `.nav-soon` 仍可见 |
| 工作区标题 | `.ws-head` + `.ws-status` | 标题下一行灰字写「方向 · 第几个节点」 |
| 分段切换 | `.ws-tabs .ws-tab` | 研读：经典文献推荐 / 阅读卡 / 矩阵。论文页：AI 带读 / 写阅读卡 |
| 面板 | `.panel` | 一切内容容器；面板里不再套带边框的卡片，用分隔线分组 |
| 小标题 | `.section-label` | 12px 灰色，面板内分段 |
| 按钮 | `.btn` / `.secondary` / `.ghost` / `.small` | 一屏只有一个 `.btn` 主按钮 |
| 选项 | `.chip` | 对话选项、方向切换同形 |
| 事实行 | `.fact-card` + `.badge.cat-*` | 行内：值 → 类别圆点 · 来源 · 待核对 → 依据 → 修改/删除 |
| 方向树 | `.frontier-node`（`is-now` / `is-past` / `is-path` / `is-pick`）+ `.frontier-link` | 整齐树布局，连线不交叉；图例 `.tree-legend` |
| 路径主干 | `.frontier-node.is-main` + `.step-no` + `.frontier-trunk` | 有任务 4 路径的方向：六步竖排成一条粗线（序号圆点），原有节点作为岔路挂在右侧；主干讲「这一学期走到哪」，节点讲「二十分钟一件事」，两种尺度一眼可分 |
| 节点抽屉 | `.node-sheet` | 桌面右侧、窄屏底部 |
| 任务 | `.task-head` + `.step-list` + `.criteria` + `.submit-box` | 步骤是可勾选的行，标准是圆点列表 |
| 反馈 | `.feedback` + `.rubric-item.pass/.fail` + `.why-box` | 分数写成「3 / 3 条做到」；下一步只指一处 |
| 建议卡 | `.nba-card` | 今日唯一重点 |
| 首页 | `.land` + `.snap-inner` + `.land-steps` + `.land-skip` | 每屏一色的发光粒子（`TONES` / `FIG_ANIMS`），左右对调，钉住文字淡入。遮罩撤后聚形 900ms，换屏 `SWAP_MS` 1400ms，能排队连翻 |
| 房间引导 | `.room-guide` | 页头下 3 步，可关掉；不是弹窗 |
| 作业题干 | `.task-stem` + `.task-ask` + `.task-fold` | 先看见已知 / 求 / 交；细则点开 |
| 论文表 | `.paper-table` | 阅读区里的真表格，窄屏可横滑 |
| 定位说明 | `.pos-lead` | 每个子页一句人话 + 例子 + 做完去哪 |
| 竞争地图卡 | `.map-card` | 一条开放问题一张卡，不要五列表格 |
| 登录页签 | `.login-tabs` + `.login-tab` + `.login-body` | 首页进入：登录 / 注册。打开页面不沿用上次登录 |
| 我的密钥 | `.connect-box`（复用） | 学生填自己的 DeepSeek；管理员「服务器共用模型」另走 `/api/llm/connect` |

**事实类别色**：background 蓝 · interest 主色 · capability 金 · preference 紫 · experience 珊瑚（只用作圆点）。

## 6. 文案与内容规范

- 产品语气：像一位**熟悉又克制的学长**——陈述事实、给出理由、不煽情不卖课；
- 所有 AI 产出必须带理由，UI 上对应「为什么是你 / 证据」区块，禁止无来源断言；
- 检索失败必须如实展示失败（`.course-status` 文案模板：「检索失败（如实说明）：…」），**不许编造兜底数据**；
- 「不知道」选项永远合法且不加负面措辞；
- 数字/时间用半角；中文标点为主。

## 7. 布局

- 侧栏 216px + 主栏；主栏内容宽 ≤ 880px（方向页、研读 / 论文页放宽到 1240px），研读和论文页居中；<920px 侧栏变顶栏、单列；研读 kit 页 `.read-grid` 两栏三窗各自内滚；
- 主工作区两栏 `.two-col`（1.6fr : 1fr）：左主内容、右辅栏（实时记录/治理原则）；移动端叠放；
- 列表卡片网格 `.dir-cards`：`repeat(auto-fit, minmax(300px, 1fr))`。

## 8. 变更流程

改本规范 = PR + 在 [CHANGELOG.md](CHANGELOG.md) 登记。新增颜色/组件必须先在 styles.css 里以 token 形式落地，再回填 §2/§5。
