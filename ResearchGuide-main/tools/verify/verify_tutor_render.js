/* 带读面板：题面走 textContent，规则版可见，预填只填空栏。 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(path.resolve(__dirname, "..", ".."), "web", "js", "app.js"),
  "utf8");

let fails = 0;
const check = (c, l, e = "") => { console.log(`  [${c ? "OK " : "!! "}] ${l} ${e}`); if (!c) fails++; };

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
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  return e;
}
const walk = (r, o = []) => { o.push(r); (r.children || []).forEach((c) => walk(c, o)); return o; };
const byClass = (r, cls) => walk(r).filter((c) => (c.className || "").split(/\s+/).includes(cls));

function extract(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) throw new Error("找不到函数: " + name);
  let d = 0, j = m.index + m[0].length - 1;
  for (; j < src.length; j++) {
    if (src[j] === "(") d++;
    else if (src[j] === ")") { d--; if (d === 0) { j++; break; } }
  }
  const i = src.indexOf("{", j);
  d = 0;
  for (j = i; j < src.length; j++) {
    if (src[j] === "{") d++;
    else if (src[j] === "}") { d--; if (d === 0) break; }
  }
  return src.slice(m.index, j + 1);
}

const el = (tag, cls, html) => {
  const e = mkEl(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = String(html);
  return e;
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const toast = () => {};
const S = { uid: "u1" };
function api() { return Promise.resolve({}); }

eval(extract("applyTutorPrefill"));
eval(extract("tutorPanel"));

const fields = { claim: "已有主张", method: "" };
const alts = applyTutorPrefill(fields, { claim: "带读里的主张", method: "他们怎么做", change: "改一处" });
check(fields.claim === "已有主张", "onPrefill 不覆盖已填栏");
check(fields.method === "他们怎么做" && fields.change === "改一处", "onPrefill 只填空栏");
check(alts.length === 1 && alts[0].key === "claim", "已填栏列出可替换句");

const sess = {
  status: "active", rev: 1, voice: "rules", notice: "",
  cur: 0, total: 8,
  slots: [{
    qid: "q_claim_quote", station: "abstract", type: "find_quote", field: "claim_quote",
    question: "<b>leak</b>", hint: "提示", voice: "rules", attempts: 0, status: "open", answer: "", note: "",
  }],
  stations: [{ key: "abstract", label: "摘要" }],
};
const box = tutorPanel({
  aid: "2009.03300", kitId: "llm-eval",
  scrollTo() {}, selection() { return ""; },
  onPrefill() { return []; },
  session: sess,
});
const q = byClass(box, "tutor-q")[0];
const voice = byClass(box, "tutor-voice")[0];
check(!!q, "面板画出题面");
check(q && q.textContent === "<b>leak</b>", "题面走 textContent", q && q.innerHTML);
check(q && q.innerHTML === "", "题面不进 innerHTML");
check(voice && /规则版/.test(voice.textContent), "标规则版");

if (fails) {
  console.log("失败 " + fails + " 项");
  process.exit(1);
}
console.log("全部通过");
