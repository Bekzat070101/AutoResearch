/* 验对话结果区的信息组织：系统副作用收成一个结构化的块，而不是散在对话流里。
 *
 * 用户的抱怨：「信息组织有点逻辑？现在感觉是想到哪里放到哪里」
 *
 * 所以这里钉住的不只是「有没有渲染出来」，而是**组织方式**：
 *   · 三种副作用各归一组，组名固定（记忆 / 查询 / 没采纳）
 *   · 组顺序固定，不随代码顺序变
 *   · 三条记忆是**一个**「记忆 3 条」组，不是三个胶囊
 *   · 什么都没发生时整块不渲染（不留空壳）
 *   · 成绩单回执用数字格，并**显式**写出「没算进绩点」
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
    remove() { const p = this.parent; if (p) { const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); } },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {}, focus() {}, click() { if (this.onclick) this.onclick(); },
    querySelector() { return null; },
  };
  e.classList = {
    add: (c) => { if (!e.className.split(/\s+/).includes(c)) e.className = (e.className + " " + c).trim(); },
    remove: (c) => { e.className = e.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
    contains: (c) => e.className.split(/\s+/).includes(c),
    toggle: (c, on) => { on ? e.classList.add(c) : e.classList.remove(c); },
  };
  Object.defineProperty(e, "parentNode", { get() { return e.parent; } });
  Object.defineProperty(e, "parentElement", { get() { return e.parent; } });
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

const el = (tag, cls, text) => {
  const e = mkEl(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const document = { createTextNode: (t) => { const e = mkEl("#text"); e._text = String(t); return e; } };

let actions, factsRefreshed;
const body = extract("turnReceipt") + "\n" + extract("renderTranscriptReceipt");
const make = () => {
  actions = mkEl("div");
  factsRefreshed = 0;
  const refreshFacts = async () => { factsRefreshed++; };
  return new Function(
    "el", "esc", "document", "actions", "refreshFacts",
    body + "\nreturn { turnReceipt, renderTranscriptReceipt };")(
    el, esc, document, actions, refreshFacts);
};

(async () => {
  let m = make();

  console.log("=== ① 三种副作用各归一组，顺序固定 ===");
  // turnReceipt 只**返回**块，挂到哪里由调用方决定（afterTurn 负责 append）
  actions.appendChild(m.turnReceipt({
    facts_added: [{ value: "Agent 日语学习项目 KotobaAI" }],
    facts_changed: [{ value: "全平台累计下载 400+" }],
    tool_results: [{ ok: true, tool: "pku_course" }],
    rejected_ops: [{ reason: "找不到原话依据" }],
  }));
  const groups = byClass(actions, "rc-group").map((g) => g.textContent);
  console.log("        分组：" + JSON.stringify(groups.map((g) => g.slice(0, 12))));
  const labels = byClass(actions, "rc-label").map((l) => l.textContent);
  check(labels.length === 3, `三组（${labels.length}）`);
  check(labels[0].startsWith("记忆"), "第一组是「记忆」", labels[0]);
  check(labels[1].startsWith("查询"), "第二组是「查询」", labels[1]);
  check(labels[2].startsWith("没采纳"), "第三组是「没采纳」", labels[2]);

  console.log("\n=== ② 两条记忆是一个组，不是两个胶囊 ===");
  const memGroup = byClass(actions, "rc-group")[0];
  check(byClass(memGroup, "rc-list").length === 1, "记忆只有一个列表");
  check(walk(memGroup).filter((c) => c.tagName === "LI").length === 2,
    "记忆列表里两条", String(walk(memGroup).filter((c) => c.tagName === "LI").length));
  check(allText(memGroup).includes("2 条"), "组头带条数「2 条」");
  check(factsRefreshed === 1, "刷新了一次记忆面板", String(factsRefreshed));

  console.log("\n=== ③ 空的时候整块不渲染 ===");
  m = make();
  const none = m.turnReceipt({ facts_added: [], facts_changed: [], tool_results: [], rejected_ops: [] });
  check(none === null, "什么都没发生时不返回块");
  check(actions.children.length === 0, "也没有往结果区塞东西");

  console.log("\n=== ④ 只有记忆时，查询/没采纳两组不出现 ===");
  m = make();
  actions.appendChild(m.turnReceipt({ facts_added: [{ value: "大二" }] }));
  check(byClass(actions, "rc-group").length === 1, "只有一组");
  check(!allText(actions).includes("查询"), "没有空的「查询」组");
  check(!allText(actions).includes("没采纳"), "没有空的「没采纳」组");

  console.log("\n=== ⑤ 成绩单回执：数字格 + 显式说出没算进绩点的课 ===");
  m = make();
  m.renderTranscriptReceipt({
    written: 8,
    summary: {
      passed_credits: 10, gpa: 3.801953125, gpa_credits: 8,
      ungraded: [{ course: "中国美术简史", grade: "B+", credits: 2 }],
      in_progress: [{ course: "军事理论（上）" }, { course: "军事理论（下）" }],
    },
    derived: [{ value: "修过 4 门编程类课程，学分加权平均 91.2" }],
    warnings: ["神秘课程 待定"],
  });
  const t = allText(actions);
  check(byClass(actions, "receipt-import").length === 1, "是导入回执样式");
  check(byClass(actions, "receipt-import")[0].tagName === "DETAILS", "长导入回执默认折叠，给接话留出空间");
  check(!byClass(actions, "receipt-import")[0].open, "不会默认展开整份统计");
  check(byClass(actions, "ri-cell").length === 4, "四个数字格", String(byClass(actions, "ri-cell").length));
  // 数字来自合成课表（4 门百分制，8 学分），不是任何人的真实成绩单
  check(t.includes("3.8020"), "绩点按四位小数显示", "(避免 3.80 那种看起来像整数的写法)");
  check(t.includes("8 门"), "说了几门");
  check(t.includes("没有算进绩点") && t.includes("中国美术简史"),
    "显式说了哪几门没算进绩点");
  check(t.includes("还在修") && t.includes("军事理论（上）"), "说了哪些还在修");
  check(t.includes("修过 4 门编程类课程"), "列出了推出的能力结论");
  check(t.includes("神秘课程"), "列出没看懂的地方");
  check(t.includes("不替你补"), "并说明不替用户补");

  console.log("\n=== ⑥ 用户粘的内容当成文本，不当 HTML ===");
  m = make();
  m.renderTranscriptReceipt({
    written: 1,
    summary: { ungraded: [{ course: "<b>AI</b>导论", grade: "B+", credits: 2 }] },
    derived: [], warnings: [],
  });
  const raw = JSON.stringify(walk(actions).map((c) => c._text));
  check(raw.includes("<b>AI</b>导论"), "尖括号原样保留在文本节点里（不会被当标签）");
  check(!byClass(actions, "ri-v").some((c) => c._html),
    "没有用 innerHTML");

  console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
  process.exit(fails ? 1 : 0);
})();
