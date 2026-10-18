/* 用实际渲染函数验“我的”：课程不丢、可核对修改、可选信息不变成任务。 */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "../../web/js/app.js"), "utf8");
let fails = 0;
const check = (ok, name) => { console.log(`  [${ok ? "OK " : "!! "}] ${name}`); if (!ok) fails++; };
function extract(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src);
  if (!m) throw new Error(name);
  const start = src.indexOf("{", m.index + m[0].length - 1); let depth = 0, end = start;
  for (; end < src.length; end++) { if (src[end] === "{") depth++; else if (src[end] === "}" && --depth === 0) break; }
  return src.slice(m.index, end + 1);
}
const walk = (r) => [r, ...(r.children || []).flatMap(walk)];
const byClass = (r, name) => walk(r).filter(e => e.className.split(/\s+/).includes(name));
function el(tag, cls = "", text = "") {
  const n = {tagName:tag.toUpperCase(), className:cls, children:[], attrs:{}, dataset:{}, events:{}, _text:String(text),
    set textContent(v) {this._text=String(v); this.children=[];},
    get textContent() {return this._text + this.children.map(e=>e.textContent).join("");},
    set innerHTML(v) {this._text=String(v); this.children=[];},
    appendChild(c) {c.parent=this; this.children.push(c); return c;},
    append(...items) {items.forEach(c=>this.appendChild(c));},
    setAttribute(k,v) {this.attrs[k]=v;},
    addEventListener(k,v) {this.events[k]=v;}, focus(){}, scrollIntoView(){},
    querySelector(selector) {return walk(this).slice(1).find(e=>selector.startsWith(".") ? e.className.split(/\s+/).includes(selector.slice(1)) : e.tagName===selector.toUpperCase()) || null;},
    remove() {if(this.parent) this.parent.children=this.parent.children.filter(e=>e!==this);},
    classList:{add(c) {if (!n.className.split(/\s+/).includes(c)) n.className+=" "+c;}},
  };
  return n;
}
const esc = s => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const S = {uid:"synthetic", renderSeq:1, entered:false, portraitName:""};
const $app = el("main"); const calls=[];
const derived = {id:"d1", key:"transcript:math", source:"derived", value:"修过 2 门数学类课程，学分加权平均 82.0：示例数学一 80、示例数学二 84", evidence:[{type:"transcript",course:"示例数学一",grade:"80"}], affects_label:"任务难度"};
const grade = {id:"g1", key:"grade", value:"大二", source:"declared", status:"confirmed", evidence:[{quote:"我是大二"}]};
const courses = Array.from({length:22}, (_, i) => ({course:`示例课程 ${i+1}`, grade:"80", credits:2, term:"25-26学年度1学期"}));
const api = async (method, url, body) => {
  calls.push({method,url,body});
  if (url.startsWith("/api/memory?")) return {layers:[{layer:"identity",label:"身份",facts:[grade]},{layer:"capability",label:"能力",facts:[derived]}]};
  if (url.startsWith("/api/me/facts?")) return {facts:[grade,derived]};
  if (url.startsWith("/api/me/transcript?")) return {courses,summary:{passed_credits:44,gpa:3.36,gpa_credits:44,in_progress:[]}};
  if (url.startsWith("/api/me/coverage?")) return {filled:1,total:15,missing:14,groups:[{group:"identity",label:"身份",filled:1,total:3,slots:[{key:"grade",label:"年级",filled:true,value:"大二"},{key:"department",label:"院系",filled:false}]}]};
  return {};
};
const portraitSection = async () => {const p=el("section","panel");p.dataset.activeLabel="合成测试画像";return p;};
const workspaceHead = (title, sub) => {const h=el("header","ws-head");h.append(el("h2","",title),el("p","",sub));return h;};
const accountPanel = () => el("section","panel account-panel","账号管理");
const names = ["meFold","memoryCard","layerBlock","transcriptPanel","transcriptImportPanel","coverageBoard","renderMe"];
const code = names.map(extract).join("\n") + "\nreturn {renderMe,memoryCard,transcriptPanel};";
const ui = new Function("el","esc","api","S","$app","portraitSection","workspaceHead","accountPanel","stale","toast","setView","enterApp","document","confirmDrafts","projectFolders", code)(
  el,esc,api,S,$app,portraitSection,workspaceHead,accountPanel,()=>false,()=>{},()=>{},()=>{}, {getElementById:()=>null},()=>el("div"),()=>el("div"));
(async () => {
  await ui.renderMe();
  await new Promise(setImmediate);
  check(byClass($app,"me-course-table")[0].querySelector("tbody").children.length===22,"全部 22 门课程仍可在明细中查看");
  check(byClass($app,"me-transcript-panel")[0].querySelector(".mem-value").textContent.includes("2 门数学相关课程"),"课程依据收纳在学习背景中，不丢失派生记录");
  check(!byClass($app,"me-memory-panel")[0].textContent.includes("2 门数学"),"同一条课程理解不在两个区重复显示");
  check(!byClass($app,"me-page")[0].textContent.includes("还差 14"),"不把未提供背景显示成待完成清单");
  check(!byClass($app,"me-transcript-panel")[0].textContent.includes("影响任务难度"),"不再声称课程用于对话派任务");
  check(byClass($app,"me-settings-fold").every(n=>!n.open),"账号、方向和选填字段按需展开");
  const card = ui.memoryCard(grade);
  walk(card).find(e=>e.tagName==="BUTTON" && e.textContent==="修改").onclick();
  const input=card.querySelector("input");input.value="大一";
  await input.events.blur();
  check(calls.some(c=>c.method==="PATCH" && c.body.value==="大一" && c.body.uid==="synthetic"),"记忆修改仍提交正确内容和用户，不只是换了样式");
  const d = ui.memoryCard(derived);
  await walk(d).find(e=>e.tagName==="BUTTON" && e.textContent==="暂不采用").onclick();
  check(calls.some(c=>c.method==="DELETE" && c.url==="/api/me/facts/d1?uid=synthetic"),"不采用只移除相应理解");
  check(!calls.some(c=>c.method==="DELETE" && c.url.includes("enrollments")),"移除理解不会删除原始成绩单");
  console.log(fails ? `失败 ${fails} 项` : "全部通过"); process.exit(fails ? 1 : 0);
})().catch(e=>{console.error(e);process.exit(1);});
