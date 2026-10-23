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

// Execute the real layout builder: receipts belong to the conversation,
// suggestions belong to the composer. There is no third scroll strip.
const mkHead = (entered) => new Function("el", "S", extract("dialogueHead") + "\nreturn dialogueHead();")(el, {entered});
check(byClass(mkHead(false), "enter-bar").length === 1, "首次引导有提前进入入口");
check(mkHead(false).textContent.includes("院系和年级"), "首次引导先邀请提供明确的学习背景");  // 第四轮副标题改成「说说你的院系和年级」
check(byClass(mkHead(true), "enter-bar").length === 0, "普通对话不重做引导");
const built = new Function("el", "$app", "S", layoutBlock() +
  "\nreturn {wrap,chat,scroll,actions,inputZone,inputRow,input,stageLine,side,suggestions};")(
    el, el("main"), {entered:false});
check(built.chat.children.length === 2, "对话流和输入区两块，不存在中间滚动带");
check(built.chat.children[0] === built.scroll, "对话流在上方");
check(built.chat.children[1] === built.inputZone, "输入区固定在下方");
check(walk(built.inputZone).includes(built.suggestions), "可选回答归输入区");
check(walk(built.inputZone).includes(built.stageLine), "实际生成状态归输入区");
check(!built.chat.children.includes(built.actions), "回执不再作为中间独立面板");
check(!src.includes('"action-slot chat-next"'), "移除旧滚动带");
check(src.includes('scroll.appendChild(actions)'), "本轮记录与邀请随对话滚动");
check(src.includes('bubble-transcript'), "长成绩单原文可折叠");
console.log("\n" + (fails ? `失败 ${fails} 项` : "全部通过"));
process.exit(fails ? 1 : 0);
