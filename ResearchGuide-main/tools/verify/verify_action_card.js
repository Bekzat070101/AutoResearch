/* 验对话里那张行动卡的按钮：从「就做这个」到「去任务区完成」。
 *
 * 用户说的流程：
 *   「正确流程不应该是对话里的任务接受后去侧边栏里的任务区完成吗？」
 *
 * 所以卡片在三个状态下必须给出正确的唯一动作：
 *   offered   → 「就做这个」（accept，服务端会建任务）
 *   accepted  → 「去任务区完成」（跳到 workbench，并带上 task_id）
 *   completed → 明确说已完成，不再给动作
 * 并且「标记完成」这个按钮不能存在。
 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(path.resolve(__dirname, "..", ".."), "web", "js", "chat.js"),
  "utf8");

let fails = 0;
const check = (c, l, e = "") => { console.log(`  [${c ? "OK " : "!! "}] ${l} ${e}`); if (!c) fails++; };

function mkEl(tag) {
  const e = {
    tagName: String(tag || "div").toUpperCase(), children: [], attrs: {}, style: {},
    _text: "", _html: "", parent: null, className: "", type: "", disabled: false,
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    insertBefore(c) { c.parent = this; this.children.unshift(c); return c; },
    remove() { const p = this.parent; if (p) { const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); } },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {}, focus() {}, blur() {}, click() { if (this.onclick) this.onclick(); },
    querySelector() { return null; },
  };
  // 真 DOM 里 .parentNode 和 .parentElement 都在。渲染代码用的是
  // actions.parentNode.insertBefore(...)，只给 .parent 会误报成产品 bug。
  Object.defineProperty(e, "parentNode", { get() { return e.parent; } });
  Object.defineProperty(e, "parentElement", { get() { return e.parent; } });
  // 真 DOM 元素有 classList；渲染代码用 card.classList.add("done")，
  // 补上它，否则是桩的缺口而不是产品的 bug。
  e.classList = {
    add: (c) => { if (!e.className.split(/\s+/).includes(c)) e.className = (e.className + " " + c).trim(); },
    remove: (c) => { e.className = e.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
    contains: (c) => e.className.split(/\s+/).includes(c),
    toggle: (c, on) => { on ? e.classList.add(c) : e.classList.remove(c); },
  };
  return e;
}
const walk = (r, o = []) => { o.push(r); (r.children || []).forEach((c) => walk(c, o)); return o; };
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

const el = (tag, cls, text) => {
  const e = mkEl(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const document = { createTextNode: (t) => { const e = mkEl("#text"); e._text = String(t); return e; } };

/* ---- 可变桩 ---- */
const CFG = { created: null, views: [], bubbles: [], factsRefreshed: 0 };
const S = { uid: "u1", openTaskId: "" };
const api = async (m, url, body) => {
  if (url === "/api/dialogue/action") {
    CFG.created = body;
    // 服务端 accept 之后返回的卡（带 task_id）——这正是「去任务区完成」要用的
    return { action: Object.assign({}, CARD, { status: "accepted", task_id: "t-9" }) };
  }
  return {};
};
const addBubble = (role, text) => { CFG.bubbles.push({ role, text }); };
const toast = () => {};
const refreshFacts = async () => { CFG.factsRefreshed++; };
const setView = (v) => { CFG.views.push(v); };

let actions, slotParent;
const make = () => {
  actions = el("div", "action-slot");
  // finishedNote 会往 actions 前面插东西，所以它必须有父节点
  slotParent = el("div", "chat-foot");
  slotParent.appendChild(actions);
  return new Function(
    "el", "esc", "api", "S", "document", "addBubble", "toast", "refreshFacts", "setView", "actions",
    extract("renderAction") + "\n" + extract("finishedNote")
      + "\nreturn { renderAction, finishedNote };")(
    el, esc, api, S, document, addBubble, toast, refreshFacts, setView, actions);
};

const CARD = {
  action_id: "a-1", action: "micro_task", title: "给 KotobaAI 加一个最小的数据记录",
  direction: "", status: "offered", task_id: "",
  based_on: [{ id: "f1", value: "KotobaAI，全平台累计下载 400+" }],
};
const btns = () => walk(actions).filter((c) => c.tagName === "BUTTON");
const labels = () => btns().map((b) => b._text);
const txt = () => actions.textContent;

(async () => {
  console.log("=== ① offered：唯一动作是「就做这个」 ===");
  let m = make();
  let render = m.renderAction;
  render(Object.assign({}, CARD, { status: "offered" }));
  console.log("        按钮：" + labels().join(" / "));
  check(labels().includes("就做这个"), "有「就做这个」");
  check(!labels().includes("标记完成"), "没有「标记完成」");
  check(txt().includes("KotobaAI"), "显示了依据（为什么是给你的）");

  console.log("\n=== 点「就做这个」→ 真的去 accept ===");
  btns().find((b) => b._text === "就做这个").click();
  await new Promise((r) => setImmediate(r));
  check(CFG.created && CFG.created.event === "accept", "发的是 accept 事件",
    JSON.stringify(CFG.created));
  check(CFG.created && CFG.created.action_id === "a-1", "带上了 action_id");
  check(CFG.factsRefreshed > 0, "刷新了记忆面板");
  console.log("        按钮变成：" + labels().join(" / "));

  console.log("\n=== ② accepted：唯一动作是「去任务区完成」 ===");
  check(labels().includes("去做这一步"), "有「去做这一步」");
  check(!labels().includes("就做这个"), "不再显示「就做这个」（已经接过了）");
  check(!labels().includes("标记完成"), "没有「标记完成」");
  check(!labels().includes("先不做"), "已接的任务不再给「先不做」（要放弃得另有出口）");

  console.log("\n=== 点「去任务区完成」→ 跳到任务区并带上 task_id ===");
  CFG.views.length = 0;
  S.openTaskId = "";
  btns().find((b) => b._text === "去做这一步").click();
  check(CFG.views.includes("workbench"), "切到了任务区（workbench）",
    JSON.stringify(CFG.views));
  check(S.openTaskId === "t-9", "带上了 task_id，任务区会直接打开那一条",
    JSON.stringify(S.openTaskId));

  console.log("\n=== ③ completed：说清已完成，不再给动作 ===");
  m = make();
  render = m.renderAction;
  render(Object.assign({}, CARD, { status: "completed", task_id: "t-9" }));
  console.log("        按钮：" + (labels().join(" / ") || "（没有按钮）"));
  check(txt().includes("已完成"), "明确写了「这一步已完成」");
  check(!labels().includes("标记完成"), "没有「标记完成」");
  check(!labels().includes("就做这个"), "没有「就做这个」");
  check(actions.children.some((c) => (c.className || "").includes("done")),
    "卡片带上了 done 样式");

  console.log("\n=== ④ 刚交完任务：一张说明，不再叠一张「已完成」的卡 ===");
  m = make();
  CFG.bubbles.length = 0;
  m.finishedNote({
    action_id: "a-1", title: "给 KotobaAI 加一个最小的数据记录",
    payload: { title: "给 KotobaAI 加一个最小的数据记录", chars: 87, score: 4 },
  });
  const note = byClass(slotParent, "finished-note")[0];
  check(!!note, "画出了「你刚交了 X」");
  const nt = note ? note.textContent : "";
  check(nt.includes("给 KotobaAI"), "带了任务标题", nt.slice(0, 40));
  check(nt.includes("87 字"), "带了写了多少字");
  check(nt.includes("4 分"), "带了得分");
  check(nt.includes("经验积累"), "说清写进哪一层记忆了");
  // 这一条是重点：just_finished 那条分支**只调 finishedNote**，
  // 如果再叠一张 renderAction(last_action) 就是两张卡说同一件事。
  check(byClass(slotParent, "action-card").length === 0,
    "finishedNote 自己没有画行动卡（配对的事由调用方保证）");
  check(CFG.bubbles.some((b) => b.role === "assistant" && b.text.includes("你把")),
    "助手也说了一句", JSON.stringify(CFG.bubbles));
  check(slotParent.children[0] === note, "插在行动区之前，不挤进卡片位置");

  console.log("\n=== ⑤ 结构上不可能再发 complete 事件 ===");
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  check(!/"complete"/.test(code), "chat.js 里没有裸的 complete 事件");
  check(!/标记完成/.test(code), "chat.js 里没有「标记完成」按钮");
  // just_finished 分支必须**单独**收口，不能再跟一张 renderAction
  const justBlock = code.slice(code.indexOf("history.just_finished"));
  const branch = justBlock.slice(0, justBlock.indexOf("} else if"));
  check(!/renderAction/.test(branch),
    "刚交完那条分支里没有 renderAction（否则会画两张）", branch.trim().slice(0, 60));

  console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
  process.exit(fails ? 1 : 0);
})();
