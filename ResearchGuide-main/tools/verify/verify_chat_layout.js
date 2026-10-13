/* 验对话页的整体布局：页头一块、三个区、顺序固定。
 *
 * 用户的原话：
 *   「正确流程不应该是对话里的任务接受后去侧边栏里的任务区完成吗？！」
 *   「对话部分的卡片信息布局，能不能规整一点，信息组织有点逻辑？」
 *
 * 之前的结构问题（不是样式问题，美化救不了）：
 *   1) 页头竖着叠三层：标题 / 画像 chip / 对话|核对 标签
 *      —— 画像管理是次要设置，却占了一整行夹在标题和主标签中间
 *   2) 对话流、行动卡、输入框全塞在一个 .chat-foot 里
 *      —— 行动卡一高就把输入框顶出屏幕，而输入框该是位置最稳的那个
 *
 * 这里**真的执行** render 里构造布局的那段源码（不是拿正则去猜），
 * 然后在生成的节点树上断言结构。这是没有无头浏览器时的既定替代做法。
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
    _text: "", _html: "", parent: null, className: "", type: "", hidden: false, rows: 0,
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
  Object.defineProperty(e, "parentNode", { get() { return e.parent; } });
  Object.defineProperty(e, "parentElement", { get() { return e.parent; } });
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
const el = (tag, cls, text) => {
  const e = mkEl(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

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

/* 从 render 里切出「构造布局」那一段真实源码。
   它只依赖 el，所以能干净地单独执行。 */
function layoutBlock() {
  const start = src.indexOf('const wrap = el("div", "two-col two-col-chat");');
  const endMark = "$app.appendChild(wrap);";
  const end = src.indexOf(endMark, start);
  if (start < 0 || end < 0) throw new Error("找不到布局构造段（render 被改动了？）");
  return src.slice(start, end + endMark.length);
}

/* ---- ① 页头：一块；核对已去「我的」，未进入时有进入启研 ---- */
console.log("=== ① 页头是一块，核对不在对话页 ===");
const mkHead = new Function("el", "S",
  extract("dialogueHead") + "\nreturn dialogueHead;")(el, { entered: true });
const head = mkHead();

const $app = el("main", "page");
$app.appendChild(head);
check(byClass($app, "ws-head").length === 1, "只有一块页头",
  `实际 ${byClass($app, "ws-head").length} 块`);
check(byClass($app, "ws-tabs").length === 0, "对话页不再放对话|核对标签");
check(byClass($app, "portrait-bar").length === 0, "画像管理不在对话页头");
check(byClass($app, "enter-bar").length === 0, "已进入的人不显示进入条");
const mkOpen = new Function("el", "S",
  extract("dialogueHead") + "\nreturn dialogueHead;")(el, { entered: false });
check(byClass(mkOpen(), "enter-bar").length === 1, "未进入时页头有「进入启研」");

/* ---- ② 三个区，顺序固定 ---- */
console.log("\n=== ② 对话面板是三个有名字的区，顺序固定 ===");
const block = layoutBlock();
const built = new Function("el", "portraitTabs", "portraitBar", "$app",
  block + "\nreturn { wrap, chat, scroll, actions, inputZone, inputRow, input, stageLine, side };")(
  el, () => el("nav", "ws-tabs"), () => el("div", "portrait-bar"), el("main", "page"));

const chat = built.chat;
const zoneNames = chat.children.map((c) => c.className);
console.log("        chat 的直接孩子：" + JSON.stringify(zoneNames));
check(chat.children.length === 3, "对话面板正好三个区", `实际 ${chat.children.length}`);
check(chat.children[0] === built.scroll, "第 1 个区是对话流（chat-scroll）");
check(chat.children[1] === built.actions, "第 2 个区是下一步（chat-next）");
check(chat.children[2] === built.inputZone, "第 3 个区是输入（chat-input）");
check(byClass(built.actions, "chat-next").length === 1, "行动区自己就是 .chat-next");
check(built.actions.className.includes("action-slot"), "行动区仍带 .action-slot（空则隐藏）");

/* ---- ③ 输入永远在最下面 ---- */
console.log("\n=== ③ 输入框永远在最下面，不会被行动卡顶走 ===");
check(chat.children[chat.children.length - 1] === built.inputZone,
  "最后一个区是输入区");
check(walk(built.inputZone).includes(built.input), "输入框在输入区里");
check(walk(built.inputZone).includes(built.inputRow), "输入行在输入区里");
check(!walk(built.scroll).includes(built.input), "输入框不在对话流里");
check(walk(built.inputZone).includes(built.stageLine),
  "状态行归输入区（说的是「输入之后会发生什么」）");
check(!walk(built.actions).includes(built.stageLine), "状态行不在行动区里");

/* ---- ④ 行动卡落进下一步区，输入位置不变 ---- */
console.log("\n=== ④ 来了行动卡之后，输入仍在最下面 ===");
const card = el("div", "action-card");
built.actions.appendChild(card);          // 等价于 renderAction 往 actions 里塞卡片
check(walk(built.actions).includes(card), "卡片在 .chat-next 里");
check(chat.children[chat.children.length - 1] === built.inputZone,
  "卡片进来后输入区仍是最下面那个");

// 一大堆内容（长回执 + 卡片）也不该改变结构
for (let i = 0; i < 40; i++) built.actions.appendChild(el("p", "receipt"));
check(chat.children[chat.children.length - 1] === built.inputZone,
  "行动区塞满 40 个块之后，输入区还在最下面");
check(built.actions.children.length === 41, "行动区自己滚（结构上就是它的孩子）");

/* ---- ⑤ 刚交完任务的落点 ---- */
console.log("\n=== ⑤ 刚交完任务：finished-note 插在行动区前面 ===");
const note = el("div", "finished-note");
built.actions.parentNode.insertBefore(note, built.actions);   // finishedNote 的做法
check(chat.children.indexOf(note) < chat.children.indexOf(built.actions),
  "落点排在行动区前面");
check(chat.children[chat.children.length - 1] === built.inputZone,
  "落点插进来之后，输入区还是在最下面");
const css = fs.readFileSync(
  path.join(path.resolve(__dirname, "..", ".."), "web", "css", "styles.css"),
  "utf8");
check(/\.chat-panel > \.finished-note\s*\{/.test(css),
  "落点的父节点是 .chat-panel，所以必须有对应规则给它对齐边距");

/* ---- ⑥ 三列/两列对齐 ---- */
console.log("\n=== ⑥ 主区两列 ===");
check(built.wrap.className.includes("two-col"), "主区是 two-col");
check(chat.className.includes("chat-wide"), "对话列带 chat-wide");
check(built.side.className.includes("side-panel"), "另一列是记忆侧栏");
check(/\.two-col-chat > \.side-panel\s*\{/.test(css),
  "侧栏有自己的高度上限（两列底边才对得上）");

console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
process.exit(fails ? 1 : 0);
