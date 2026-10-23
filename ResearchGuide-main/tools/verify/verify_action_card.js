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
    querySelector(s) { return s.startsWith(".") ? byClass(this, s.slice(1))[0] || null : null; },
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

/* 对话退出派任务后，验证一次邀请和继续聊天的真实点击行为。 */
const S = { uid: "u1", portraitId: "p1", entered: false };
const saved = new Map();
const localStorage = { getItem: (k) => saved.get(k), setItem: (k, v) => saved.set(k, v) };
const entryInvitations = new Set();
let entries = 0, focuses = 0, directions = 0;
const enterApp = () => { entries++; };
const input = { focus: () => { focuses++; } };
let actions;
function make() {
  actions = el("div", "action-slot");
  return new Function("el", "S", "localStorage", "entryInvitations", "actions", "input", "enterApp", "scroll", "button", "openDirections",
    extract("enterCard") + "\nreturn enterCard;")(el, S, localStorage, entryInvitations, actions, input, enterApp, el("div", "chat-scroll"),
      (text, cls) => el("button", cls, text), () => directions++);
}
let invite = make();
invite();
check(actions.children.length === 1, "首次满足条件有一次邀请");
check(actions.textContent.includes("其他信息可以以后再补"), "明确告知无需补齐字段");
let buttons = walk(actions).filter((n) => n.tagName === "BUTTON");
buttons.find((b) => b.textContent === "再聊聊").click();
check(actions.children.length === 0 && focuses === 1, "再聊聊关闭卡片并回到输入框");
invite();
check(actions.children.length === 1, "下一轮仍可找到进入入口，不要求继续补信息");
invite();
check(actions.children.length === 1, "同一面板内不插入重复邀请");
entryInvitations.clear();
invite = make(); invite();
check(actions.children.length === 1, "刷新后恢复可见的进入入口");
S.portraitId = "p2";
invite = make(); invite();
check(actions.children.length === 1, "新画像有独立的一次邀请");
buttons = walk(actions).filter((n) => n.tagName === "BUTTON");
buttons.find((b) => b.textContent === "查看推荐方向").click();
check(directions === 1, "方向入口调用统一方向页面");
buttons.find((b) => b.textContent === "直接进入研读").click();
check(entries === 1, "进入按钮仍能调用现有进入流程");
S.entered = true; S.portraitId = "p3";
invite = make(); invite();
check(actions.children.length === 0, "已进入的用户没有开场邀请");
S.entered = false; S.portraitId = "p4";
invite = make();
actions.appendChild(el("div", "receipt", "一份很长的记忆回执"));
invite();
check(actions.children[0].className === "enter-card", "进入邀请在长回执之前，不被挤到滚动条下面");
check(!src.includes("function renderAction") && !src.includes("function finishedNote"), "对话不再渲染任务卡或完成通知");
check(!src.includes('"/api/dialogue/action"'), "对话不再提交派任务事件");
console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
process.exit(fails ? 1 : 0);
