/* 决定性验证：**在渲染层**上，对话给的任务会不会出现在侧边栏的任务区。
 *
 * 用户的追问（原话）：
 *   「为什么对话里的任务不能和整个系统产生联系！！！
 *     正确流程不应该是对话里的任务接受后去侧边栏里的任务区完成吗？！！」
 *
 * 之前我只用 API 验过（verify_task_loop.py），那证明的是**数据通了**，
 * 不能证明**界面上看得见**。这两件事是分开的：
 *   renderWorkbench 以前在没有方向时整页 return，数据再对也是一片空白。
 *
 * 这里把 app.js 里真正的那几个渲染函数抽出来跑，其余依赖给最小桩：
 *   renderWorkbench / dialogueTaskSection / taskPanel / taskPanelDone
 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(path.resolve(__dirname, "..", ".."), "web", "js", "app.js"),
  "utf8");

let fails = 0;
const check = (c, l, e = "") => { console.log(`  [${c ? "OK " : "!! "}] ${l} ${e}`); if (!c) fails++; };

/* ---------- 最小 DOM ---------- */
function mkEl(tag) {
  const e = {
    tagName: String(tag || "div").toUpperCase(), children: [], attrs: {}, style: {},
    _text: "", _html: "", parent: null, className: "", type: "", disabled: false,
    hidden: false, dataset: {},
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    prepend(c) { c.parent = this; this.children.unshift(c); return c; },
    insertBefore(c, ref) {
      c.parent = this;
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c);
      return c;
    },
    remove() { const p = this.parent; if (p) { const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); } },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    addEventListener() {}, focus() {}, blur() {}, click() { if (this.onclick) this.onclick(); },
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  return e;
}
const walk = (r, o = []) => { o.push(r); (r.children || []).forEach((c) => walk(c, o)); return o; };
const allText = (r) => walk(r).map((c) => c.textContent).join(" ");
const byClass = (r, cls) => walk(r).filter((c) => (c.className || "").split(/\s+/).includes(cls));

function extract(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) throw new Error("找不到函数: " + name);
  let i = src.indexOf("{", m.index + m[0].length - 1), d = 0, j = i;
  for (; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) break; }
  }
  return src.slice(m.index, j + 1);
}

/* ---------- 可变的桩配置 ---------- */
const CFG = {
  tasks: [],
  facts: [],
  field: null,          // null = 还没选方向
  app: null,            // 渲染目标，每次跑之前设置
  view: "workbench",
  step: null,           // 主干步骤说明（stepOf 的桩）
};
const getCfg = () => CFG;

const el = (tag, cls, text) => {
  const e = mkEl(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const S = { uid: "u1", renderSeq: 1, view: "workbench", openTaskId: "", lastFeedback: null, lastFeedbackTaskId: "" };

const api = async (method, url, body) => {
  if (url.startsWith("/api/tasks?")) return { tasks: CFG.tasks };
  if (url.startsWith("/api/onboard/result")) return { facts: CFG.facts, result: null };
  if (url.indexOf("/api/tasks/generate") >= 0) {
    // 真实接口会按请求里的 title 建任务；桩必须照做，否则 renderWorkbench
    // 那句「任务服务还在用旧题目」的保护会误触发，测出来就是假失败。
    const req = body || {};
    return Object.assign({}, DIALOGUE_TASK, {
      id: "t-tree-1", title: req.title || "", direction: req.direction || "",
      origin: "tree", action_id: "", node_path: req.direction || "",
      deliverable: "一段文字：这个节点在问什么 + 一个能核对的例子 + 你还卡在哪",
    });
  }
  return {};
};
const FIELD_TREES = {};
const toast = () => {};
const saveTrail = () => {};
const adoptDirection = () => ({ code: CFG.field ? "ai" : "", done: [], tasks: {} });
const mergeTrail = (c, l) => ({ code: c || (CFG.field ? "ai" : ""), done: [], tasks: {} });
const currentOnPath = (f) => (f && f.nodes ? f.nodes[0] : null);
const pathNodes = (f) => (f && f.nodes ? f.nodes : []);
const renderFeedbackInto = () => {};
const readDraft = () => "";
const writeDraft = () => {};
const hydrateDraft = async () => {};
const ensurePortrait = async () => {};
const stale = (seq) => seq !== S.renderSeq;
const workspaceHead = (t) => el("div", "ws-head", t);
const emptyPanel = (msg) => el("div", "empty", msg);
const roomGuide = () => el("aside", "room-guide");
const setView = (v) => { CFG.view = v; S.view = v; };
/* 合并两条开发线之后，renderWorkbench 还用到这几样（原来那支用的是 FIELD_TREES[code]，
   现在统一走 activeField/stepOf——每个画像各自记住方向与进度、以及「这是主干第几步」）。 */
const activeField = (t) => FIELD_TREES[(t && t.code) || ""] || null;
const dirTitle = (dirId, field) => (field && field.name) || dirId || "";
const stepOf = () => CFG.step || null;
const findProjectsForStep = () => {};
const goFindProjects = () => {};
const PROJECT_AFTER_TASKS = 3;
const sessionStorage = { getItem: () => null, setItem: () => {} };
const document = {
  createTextNode: (t) => { const e = mkEl("#text"); e._text = String(t); return e; },
  createElement: (t) => mkEl(t),
  getElementById: () => null,
  querySelectorAll: () => [],
  body: mkEl("body"),
};
const setTimeout = () => {};

/* $app 是通过闭包读的：让它每次返回当前渲染目标 */
const appRef = { get innerHTML() { return CFG.app ? CFG.app.innerHTML : ""; },
                 set innerHTML(v) { if (CFG.app) CFG.app.innerHTML = v; },
                 appendChild(c) { return CFG.app.appendChild(c); } };
const ensureAiTree = () => {
  if (!FIELD_TREES.ai) {
    FIELD_TREES.ai = { name: "人工智能", nodes: [
      { id: "n1", label: "特征是什么", intro: "先弄清特征" },
      { id: "n2", label: "线性模型", intro: "" },
    ] };
  }
};

const body = [
  "taskOriginLine", "taskStemBlock", "taskHowNode", "taskRubricNode",
  "taskFolds", "taskHintBox",
  "dialogueTaskSection", "taskPanelDone", "taskPanel", "renderWorkbench",
].map(extract).join("\n");
const make = () => new Function(
  "el", "esc", "api", "S", "stale", "$app", "document", "sessionStorage", "setTimeout",
  "toast", "saveTrail", "adoptDirection", "mergeTrail", "currentOnPath", "pathNodes",
  "renderFeedbackInto", "readDraft", "writeDraft", "hydrateDraft", "ensurePortrait", "workspaceHead",
  "emptyPanel", "roomGuide", "setView", "FIELD_TREES", "activeField", "dirTitle", "stepOf",
  "findProjectsForStep", "goFindProjects", "PROJECT_AFTER_TASKS",
  body + "\nreturn { renderWorkbench, dialogueTaskSection };")(
  el, esc, api, S, stale, appRef, document, sessionStorage, setTimeout,
  toast, saveTrail, adoptDirection, mergeTrail, currentOnPath, pathNodes,
  renderFeedbackInto, readDraft, writeDraft, hydrateDraft, ensurePortrait, workspaceHead,
  emptyPanel, roomGuide, setView, FIELD_TREES, activeField, dirTitle, stepOf,
  findProjectsForStep, goFindProjects, PROJECT_AFTER_TASKS);

const DIALOGUE_TASK = {
  id: "t-dlg-1", user_id: "u1", direction: "", title: "挑 KotobaAI 里最小的一个函数自己重写一遍",
  brief: "", steps: ["关掉 AI 生成", "自己把那个函数写出来", "记下卡在哪一步"],
  deliverable: "一段文字：重写后的代码 + 你卡住的那一步 + 原因",
  rubric: [{ criterion: "说清卡在哪一步" }, { criterion: "写了原因，不是只描述现象" }],
  time_budget_min: 40, status: "open", origin: "dialogue", action_id: "a-1", node_path: "",
};

(async () => {
  const m = make();

  console.log("场景 A：用户在对话里接了任务，但**还没选知识树方向**\n");
  CFG.app = mkEl("div");
  CFG.tasks = [DIALOGUE_TASK];
  CFG.field = null;
  await m.renderWorkbench();

  const box = CFG.app;
  const txt = allText(box);

  console.log("=== ① 对话任务必须渲染出来（这是过去失败的那一种） ===");
  check(byClass(box, "dialogue-tasks").length === 1, "作业区里有「对话给你的这一步」这一块");
  check(txt.includes("挑 KotobaAI"), "作业的标题渲染出来了");
  check(txt.includes("对话给你的这一步"), "分组标题在");
  check(byClass(box, "task-panel").length === 1, "渲染成了一个任务面板");

  console.log("\n=== ② 三问齐全 ===");
  const clickNamed = (root, name) => {
    const b = walk(root).find((c) => c.tagName === "BUTTON" && c._text === name);
    if (b) b.click();
    return b;
  };
  check(txt.includes("已知") && txt.includes("求") && txt.includes("交"), "题干有已知 / 求 / 交");
  check(txt.includes("题目说明") && txt.includes("怎样算做到"), "细则入口在（点开才展开）");
  const dv = byClass(box, "deliverable")[0];
  check(!!dv && dv.textContent.includes("重写后的代码"),
    "交付物内容渲染出来了", dv ? JSON.stringify(dv.textContent) : "(没有 .deliverable)");
  check(!!clickNamed(box, "题目说明") && allText(box).includes("关掉 AI 生成"), "点开题目说明能看到步骤");
  check(!!clickNamed(box, "怎样算做到") && allText(box).includes("说清卡在哪一步"), "点开怎样算做到能看到标准");
  check(txt.includes("来自对话"), "标明了任务来自对话");

  console.log("\n=== ③ 有提交框，能在这里完成 ===");
  check(byClass(box, "submit-box").length === 1, `有一个提交框（${byClass(box, "submit-box").length}）`);
  check(walk(box).filter((c) => c.tagName === "TEXTAREA").length === 1, "有一个输入框");
  const submitBtns = walk(box).filter((c) => c.tagName === "BUTTON" && c._text === "提交");
  check(submitBtns.length === 1, "有「提交」按钮");

  console.log("\n=== ④ 不该被「还没有方向」挡在外面 ===");
  check(!txt.includes("还没有方向。先在方向区"), "没有出现「还没有方向」的空面板");
  check(!txt.includes("去方向区"), "没有出现「去方向区」按钮");

  console.log("\n=== ⑤ 有方向时，两块都在、各归各位 ===");
  ensureAiTree();
  CFG.app = mkEl("div");
  CFG.field = "ai";
  CFG.step = { stage: 1, label: "特征与标签", done_when: "一份能跑的小例子" };
  const m2 = make();
  await m2.renderWorkbench();
  const box2 = CFG.app;
  const txt2 = allText(box2);
  check(txt2.includes("对话给你的这一步"), "对话作业那一块还在");
  check(txt2.includes("挑 KotobaAI"), "对话任务的标题仍在");
  check(txt2.includes("人工智能"), "方向路径那一段也在");
  check(byClass(box2, "dialogue-tasks").length === 1, "对话任务独立成块（没有被树挤掉）");
  check(byClass(box2, "submit-box").length === 2,
    `两块各有一个提交框（${byClass(box2, "submit-box").length}）`);

  console.log("\n=== ⑤b 树上任务保留了方向进度那一支的能力（合并时不能丢） ===");
  const pathBtn = walk(box2).find((c) => c.tagName === "BUTTON" && c._text === "路径说明");
  check(!!pathBtn, "树任务面板里有「路径说明」入口");
  if (pathBtn) pathBtn.click();
  const note = byClass(box2, "step-note")[0];
  check(!!note, "点开后出现「这是路径第几步」的说明");
  check(!!note && note.innerHTML.includes("路径第 1 步"), "说明里写明了第几步",
    note ? JSON.stringify(note.innerHTML.slice(0, 60)) : "(没有 .step-note)");
  check(!!note && note.innerHTML.includes("特征与标签"), "说明里带上了这一步的名字");
  check(!!note && note.innerHTML.includes("一份能跑的小例子"), "说明里写清了这一步要交什么");
  check(!allText(box2).includes("找能交出它的项目"), "任务页不再出现找项目入口");
  check(txt2.includes("填入演示示例"), "树任务有「填入演示示例」按钮（对话任务没有）");
  check(txt2.includes("来自方向"), "树任务标明它来自方向（不是「来自对话」）");

  console.log("\n=== ⑥ 只有树任务时，不该冒出「对话给你的任务」那块 ===");
  CFG.app = mkEl("div");
  CFG.tasks = [Object.assign({}, DIALOGUE_TASK, { origin: "tree", action_id: "", title: "特征是什么" })];
  const m3 = make();
  await m3.renderWorkbench();
  const box3 = CFG.app;
  const dlg3 = byClass(box3, "dialogue-tasks")[0];
  check(!!dlg3 && allText(dlg3).includes("还没有"),
    "显示了引导语而不是空壳", dlg3 ? JSON.stringify(allText(dlg3).slice(0, 40)) : "");

  console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
  process.exit(fails ? 1 : 0);
})();
