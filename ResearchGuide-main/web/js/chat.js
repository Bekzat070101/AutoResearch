/* 启研 · 对话视图（无构建、经典脚本）
 *
 * 走 /api/dialogue/stream：真实阶段推进（观察 → 决策 → 记忆 → 工具 → 组织回复）
 * + 回复正文增量，前端边收边渲染。
 *
 * 与 app.js 的关系：复用它的 api/el/esc/toast/setView/portraitBar 等全局函数，
 * 只把「对话」这一个视图的渲染与交互放在这里，避免继续膨胀 app.js。
 */
"use strict";

const ChatView = (() => {
  const entryInvitations = new Set();
  let busy = false;      // 正在生成，阻止重复发送
  let abort = null;      // AbortController
  let seq = 0;           // 渲染序号，切走视图后不再写 DOM
  let turnDone = null;   // 正在生成的那一轮做完时兑现：切走再回来的新视图靠它接上

  const STAGE_CN = {
    observe: "正在看你的近况",
    decide: "正在判断这一步该做什么",
    memory: "正在核对要不要记下来",
    tool: "正在查外部资料",
    compose: "正在组织回复",
  };

  /* ---------- Markdown（先转义再套格式，避免注入）---------- */

  function mdLite(src) {
    let t = esc(String(src || ""));
    t = t.replace(/```([\s\S]*?)```/g, (_, code) => `<pre class="md-pre">${code.replace(/^\n/, "")}</pre>`);
    t = t.replace(/^###\s+(.+)$/gm, "<h4>$1</h4>");
    t = t.replace(/^##\s+(.+)$/gm, "<h3>$1</h3>");
    t = t.replace(/^&gt;\s?(.+)$/gm, "<blockquote>$1</blockquote>");
    t = t.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
    t = t.replace(/^\s*\d+[.)]\s+(.+)$/gm, "<li>$1</li>");
    t = t.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    // 只放行 http/https，其它协议一律当普通文字
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // 段落：空行分段，段内单换行保留为 <br>
    return t.split(/\n{2,}/).map((block) => {
      const b = block.trim();
      if (!b) return "";
      if (/^<(h[34]|ul|pre|blockquote)/.test(b)) return b;
      return `<p>${b.replace(/\n/g, "<br>")}</p>`;
    }).join("");
  }

  /* ---------- SSE ---------- */

  async function streamTurn(payload, onEvent) {
    abort = new AbortController();
    let res;
    try {
      res = await apiFetch("/api/dialogue/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: abort.signal,
      });
    } catch (e) {
      if (e.name === "AbortError") return null;
      throw e;
    }
    if (!res.ok) {
      let detail = `请求失败 (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* 无响应体 */ }
      throw new Error(detail);
    }
    // 老浏览器没有 ReadableStream 时退回一次性接口
    if (!res.body || !res.body.getReader) {
      const data = await api("POST", "/api/dialogue/turn", payload);
      onEvent("result", data);
      return data;
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        let name = null;
        for (const line of raw.split("\n")) {
          if (line.startsWith("event: ")) name = line.slice(7).trim();
          else if (line.startsWith("data: ") && name) {
            let data = {};
            try { data = JSON.parse(line.slice(6)); } catch (_) { /* 跳过坏帧 */ }
            if (name === "result") result = data;
            if (name === "error") throw new Error(data.error || "生成失败");
            onEvent(name, data);
          }
        }
      }
    }
    return result;
  }

  /* ---------- 视图 ---------- */

  /* 对话页的页头。以前是三层竖着叠：
       ws-head（标题 + 说明）/ portrait-bar（画像 chip）/ ws-tabs（对话|核对）
     ——标题和主标签之间夹着一个**画像管理**控件，那是次要设置，
     却占了一整行，把真正入口挤下去了。

     现在合成一块，两行：
       对话        [对话|核对]          画像 [默认] [新建]
       直接说话就行。它会先看你的近况…（一行说明）
     标题和标签同行，画像管理退到右侧。 */
  function dialogueHead() {
    const head = el("header", "ws-head");
    head.appendChild(el("h2", "", S.entered ? "对话" : "先认识一下"));
    head.appendChild(el("p", "", S.entered
      ? "继续聊你的问题和阅读近况。"
      : "先聊院系、专业、年级和课程，再顺着你的想法慢慢了解。随时可以先进入使用。"));
    if (!S.entered) {
      const bar = el("div", "enter-bar");
      const go = el("button", "btn", "进入启研");
      go.type = "button";
      go.onclick = () => enterApp();
      bar.appendChild(go);
      const read = el("button", "btn ghost", "直接去读论文"); read.type = "button"; read.onclick = () => enterApp("read"); bar.appendChild(read);
      bar.appendChild(el("span", "form-note", "不用聊完，随时可以进去。"));
      head.appendChild(bar);
    }
    return head;
  }

  async function render($app) {
    const mySeq = ++seq;
    const resumeFrom = turnDone;  // 打开这一页时上一轮还在生成（在别的页等过）：见最后
    if (mySeq !== seq || S.view !== "dialogue") return;  // 等待期间切到了别的页：不能再往页面上写

    $app.innerHTML = "";
    $app.appendChild(dialogueHead());

    // 只有对话流和输入区两块。回执/进入邀请跟随本轮回复，不再挤进中间的独立滚动带。
    const wrap = el("div", "two-col two-col-chat");
    const chat = el("div", "panel chat-panel chat-wide");
    const scroll = el("div", "chat-scroll");
    scroll.setAttribute("aria-live", "polite");

    // 行动区本身就是「下一步」这个区。.action-slot:empty 已经会隐藏它，
    // 所以没有卡片时不会留一条空带。
    const actions = el("div", "chat-followup");
    const inputZone = el("div", "chat-input");
    const suggestions = el("div", "chat-suggestions");
    const stageLine = el("p", "stage-line");
    const inputRow = el("div", "chat-input-row");
    const input = el("textarea");
    input.rows = 1;
    input.placeholder = S.entered ? "想聊什么，直接说…" : "说说你的想法，也可以粘贴成绩单…";
    input.setAttribute("aria-label", "对话输入");
    const sendBtn = el("button", "btn small", "发送");
    sendBtn.type = "button";
    const stopBtn = el("button", "btn ghost small", "停止");
    stopBtn.type = "button";
    stopBtn.hidden = true;
    inputRow.append(input, sendBtn, stopBtn);
    // 状态行（正在观察 / 正在查资料）说的是「输入之后会发生什么」，
    // 所以它归输入区，不归行动区——否则它会跟着行动卡一起滚走。
    inputZone.append(suggestions, stageLine, inputRow);
    inputZone.appendChild(el("p", "composer-hint", "Enter 发送 · Shift+Enter 换行"));
    chat.append(scroll, inputZone);

    const side = el("div", "panel side-panel mem-sum");
    const sideHead = el("div", "rank-head");
    sideHead.appendChild(el("p", "side-title", S.entered ? "按你说过的，可能适合的方向" : "目前对你的了解"));
    const treeBtn = el("button", "linkish", "查看方向树");
    treeBtn.type = "button";
    treeBtn.hidden = !S.entered;
    treeBtn.onclick = () => (S.entered ? setView("cards") : enterApp("cards"));
    sideHead.appendChild(treeBtn);
    side.appendChild(sideHead);
    side.appendChild(el("p", "side-sub rank-badge", S.entered ? "规则推测" : "从学习背景聊起，其他信息慢慢补充。"));
    const sideList = el("div", "rank-list");
    const sideEmpty = el("p", "side-empty", "再聊几句兴趣或目标，这里会按你说过的排方向。");
    const sideFoot = el("p", "rank-foot", "");
    side.append(sideList, sideEmpty, sideFoot);
    wrap.append(chat, side);
    $app.appendChild(wrap);

    autoGrow();
    input.focus({ preventScroll: true });
    try {
      const draft = sessionStorage.getItem("rg_chat_draft_" + S.uid);
      if (draft) { input.value = draft; autoGrow(); }
    } catch (_) { /* ignore */ }
    input.addEventListener("input", () => {
      try { sessionStorage.setItem("rg_chat_draft_" + S.uid, input.value); } catch (_) { /* ignore */ }
    });

    /* --- 历史 --- */
    // 有一张记忆卡正在编辑时，refreshFacts 不能把面板清掉重建。
    // 编辑框的唯一保存路径是 blur；元素被 innerHTML = "" 拔掉之后
    // blur 不保证触发，用户敲进去的改动会**无声地丢掉**。
    // 所以编辑期间挂起刷新，编辑结束再补一次。
    let editingCard = false;
    let refreshQueued = false;
    let redrawQueued = false;  // 进行中的那一轮做完了要重画，但正在改记忆：等改完（见最后）
    let history = { messages: [], pending_action: null };
    try {
      history = await api("GET", `/api/dialogue/history?uid=${encodeURIComponent(S.uid)}`);
    } catch (e) { toast(e.message); }
    if (mySeq !== seq || S.view !== "dialogue") return;  // 等待期间切到了别的页：不能再往页面上写
    history.messages.forEach((m) => addBubble(m.role, m.text));
    await refreshFacts();
    if (mySeq !== seq || S.view !== "dialogue") return;  // 等待期间切到了别的页：不能再往页面上写
    if (history.guide && !S.entered) renderGuide(history.guide);
    if (history.onboard_done && !S.entered) enterCard(history.guide);
    if (!history.messages.length) {
      addBubble("assistant", history.welcome_message || "你好，我是启研。你可以直接问问题，不用先把背景填齐。也可以在树洞成绩单页全选、复制，再粘贴到这里，帮助我了解你的学习基础；这完全可选，粘贴后会保存到当前画像。");
    }

    /* --- 发送 --- */

    async function send(text) {
      const msg = (text || "").trim();
      if (!msg) return;
      if (busy) { toast("上一句还在生成，等它好了再发"); return; }
      try { sessionStorage.setItem("rg_chat_draft_" + S.uid, msg); } catch (_) { /* 无痕模式等 */ }
      busy = true;
      let finish;
      turnDone = new Promise((resolve) => { finish = resolve; });
      input.value = ""; autoGrow();
      input.disabled = true; sendBtn.disabled = true; stopBtn.hidden = false;
      actions.innerHTML = "";
      actions.remove();
      suggestions.innerHTML = "";
      addBubble("user", msg);

      const bubble = addBubble("assistant", "");
      bubble.classList.add("streaming");
      let acc = "";
      let firstChunk = true;
      setStage("observe");

      try {
        const result = await streamTurn(
          { uid: S.uid, message: msg, conversation_id: history.conversation_id },
          (name, data) => {
            if (name === "stage") { setStage(data.name, data.tool); return; }
            if (name === "delta") {
              acc += data.text || "";
              if (firstChunk) { bubble.innerHTML = ""; firstChunk = false; }
              bubble.innerHTML = mdLite(acc);
              scroll.scrollTop = scroll.scrollHeight;
            }
          },
        );
        if (mySeq !== seq || S.view !== "dialogue") return;  // 等待期间切到了别的页：不能再往页面上写

        if (result) {
          history.conversation_id = result.conversation_id;
          bubble.innerHTML = mdLite(result.reply);
          try { sessionStorage.removeItem("rg_chat_draft_" + S.uid); } catch (_) { /* ignore */ }
          afterTurn(result);
        } else if (!acc) {
          bubble.remove();
        }
      } catch (e) {
        if (mySeq === seq) {
          if (acc) { bubble.innerHTML = mdLite(acc); toast("生成中断：" + e.message); }
          else { bubble.remove(); toast(e.message); }
        } else if (e.name !== "AbortError") {
          toast("对话：上一句没生成完（" + e.message + "）");  // 人在别的页：页面上没地方显示，至少说一声
        }
      } finally {
        bubble.classList.remove("streaming");
        busy = false; abort = null; turnDone = null;
        finish();
        if (mySeq === seq) {
          input.disabled = false; sendBtn.disabled = false; stopBtn.hidden = true;
          setStage("");
          input.focus({ preventScroll: true });
        }
      }
    }

    /* 一轮结束后，把「系统做了什么」收进**一个**结构化块。
    
       以前是五种单行 <p> 直接塞进对话流里（记下了/更新了/已查/没记下来/未连接模型），
       顺序还跟着代码走，谁先谁后看运气。用户的原话是
       「信息组织有点逻辑？现在感觉是想到哪里放到哪里」。
    
       现在的规矩很简单，也是这个页面该有的框架：
         · 对话流里**只有对话**（用户和助手说的话）
         · 系统这一轮的副作用全部进这个块，按固定顺序分组
         · 记忆回执与可选说法帮助用户核对信息
    */
    function afterTurn(result) {
      scroll.appendChild(actions);
      renderOffers(result.offered_actions);
      if (result.transcript_import) {
        renderTranscriptReceipt(result.transcript_import);
      }
      const receipt = turnReceipt(result);
      if (receipt) actions.appendChild(receipt);
      if (result.degraded) {
        const d = el("p", "rc-warn");
        d.appendChild(document.createTextNode("当前未连接模型，这一轮是规则回复。"));
        const link = el("button", "linkish", "连接模型");
        link.type = "button";
        link.onclick = () => openConnect();
        d.appendChild(link);
        actions.appendChild(d);
      }
      if (result.guide && !S.entered) renderGuide(result.guide);
      if (result.onboard_done && !S.entered) enterCard(result.guide);
      scroll.scrollTop = scroll.scrollHeight;
    }

    function enterCard(profile) {
      const key = "rg_entry_invited_v2_" + S.uid + "_" + (S.portraitId || "default");
      if (S.entered || entryInvitations.has(key) || actions.querySelector(".enter-card")) return;
      try { if (localStorage.getItem(key)) return; } catch (_) { /* 仅使用本次会话状态 */ }
      entryInvitations.add(key);
      try { localStorage.setItem(key, "1"); } catch (_) { /* 仅使用本次会话状态 */ }
      const card = el("div", "enter-card");
      card.appendChild(el("p", "enter-card-title", "可以进入启研了"));
      card.appendChild(el("p", "form-note", "已了解你关注的主题和几方面阅读背景。其他信息可以以后再补；也可以继续聊。"));
      const row = el("div", "submit-actions");
      const go = el("button", "btn", "进入启研");
      go.type = "button";
      go.onclick = () => enterApp();
      const more = el("button", "btn ghost", "再聊聊");
      more.type = "button";
      more.onclick = () => { card.remove(); input.focus({ preventScroll: true }); };
      row.append(go, more);
      card.appendChild(row);
      // 邀请放在回执前面，不能被长记忆清单挤到结果区滚动条下面。
      actions.insertBefore(card, actions.firstChild);
      if (!actions.parentNode) scroll.appendChild(actions);
    }

    /* 系统这一轮的副作用，收成一个块。没有可说的就不渲染。 */
    function turnReceipt(result) {
      const added = result.facts_added || [];
      const changed = result.facts_changed || [];
      const rejected = result.rejected_ops || [];
      const tools = result.tool_results || [];
      const groups = [];

      if (added.length || changed.length) {
        const items = [];
        added.forEach((f) => items.push("记下：" + f.value));
        changed.forEach((f) => items.push("更新：" + f.value));
        groups.push({ label: "记忆", note: `${added.length + changed.length} 条`, items });
      }
      refreshFacts();
      if (tools.length) {
        const items = tools.map((t) => (t.ok ? `已查 ${t.tool}` : `查询失败（${t.tool}）：${t.error || "没有结果"}`));
        groups.push({ label: "查询", note: "", items });
      }
      if (rejected.length) {
        // 没有依据的说法不会被记下来，如实告诉用户，不装作记住了
        groups.push({
          label: "没采纳", note: `${rejected.length} 条`,
          items: rejected.map((r) => r.reason || "没有依据"),
        });
      }
      if (!groups.length) return null;

      const box = el("details", "receipt");
      box.appendChild(el("summary", "rc-head", "本轮记录 · 展开核对"));
      groups.forEach((g) => {
        const row = el("div", "rc-group");
        const head = el("p", "rc-label", esc(g.label));
        if (g.note) head.appendChild(el("span", "rc-count", esc(g.note)));
        row.appendChild(head);
        const ul = el("ul", "rc-list");
        g.items.forEach((t) => ul.appendChild(el("li", "", esc(t))));
        row.appendChild(ul);
        box.appendChild(row);
      });
      return box;
    }

    /* 成绩单导入的回执。它不是「聊了一句」，是一次数据录入，
       所以要说清：读进去几门、绩点怎么算的、哪些没算进去、推出什么结论。 */
    function renderTranscriptReceipt(imp) {
      const box = el("details", "receipt receipt-import");
      const preview = imp.mode === "preview";
      box.appendChild(el("summary", "rc-head", (preview ? "预览结果（未保存）" : (imp.written ? "课程记录已更新" : "课程记录未新增")) + ` · ${(imp.summary || {}).total || 0} 门 · 展开详情`));
      const s = imp.summary || {};
      const facts = el("div", "ri-facts");
      const cell = (k, v) => {
        const c = el("div", "ri-cell");
        c.appendChild(el("span", "ri-k", esc(k)));
        c.appendChild(el("span", "ri-v", esc(v)));
        facts.appendChild(c);
      };
      cell(preview ? "识别课程" : "写入课程", `${preview ? (s.total || 0) : imp.written} 门`);
      cell("通过学分", String(s.passed_credits != null ? s.passed_credits : "—"));
      cell("参考 GPA", s.gpa != null ? s.gpa.toFixed(4) : "—");
      if (s.gpa_credits != null) cell("计入绩点的学分", String(s.gpa_credits));
      box.appendChild(facts);

      const un = s.ungraded || [];
      if (un.length) {
        box.appendChild(el("p", "rc-warn",
          `${un.length} 门是字母等级/五级制，没有算进绩点（教务换算口径未定，不敢替你定）：`
          + un.map((u) => `${esc(u.course)}（${esc(u.grade)}）`).join("、")));
      }
      const cur = s.in_progress || [];
      if (cur.length) {
        box.appendChild(el("p", "rc-note",
          `${cur.length} 门还在修：` + cur.map((c) => esc(c.course)).join("、")));
      }
      const d = imp.derived || [];
      if (d.length) {
        const ul = el("ul", "rc-list");
        d.forEach((f) => ul.appendChild(el("li", "", esc(f.value))));
        box.appendChild(el("p", "rc-label", "从课程记录了解的背景（不代表熟练掌握）"));
        box.appendChild(ul);
      }
      const w = imp.warnings || [];
      if (w.length) {
        const ul = el("ul", "rc-list rc-list-warn");
        w.forEach((x) => ul.appendChild(el("li", "", esc(x))));
        box.appendChild(el("p", "rc-label", `没看懂的 ${w.length} 处（不替你补）`));
        box.appendChild(ul);
      }
      actions.appendChild(box);
    }

    /* 模型给的可选说法，点一下就等于把这句话发给它，省去用户组织语言 */
    function renderOffers(list) {
      if (!list || !list.length) return;
      const row = el("div", "offer-row");
      list.forEach((a) => {
        const b = el("button", "chip", esc(a.label));
        b.type = "button";
        b.onclick = () => send(a.label);
        row.appendChild(b);
      });
      suggestions.appendChild(row);
    }

    function renderGuide(profile) {
      sideList.innerHTML = "";
      sideEmpty.style.display = "none";
      (profile.areas || []).forEach((area) => {
        const row = el("div", "guide-area" + (area.known ? " is-known" : ""));
        const label = el("p", "guide-area-label", esc(area.label));
        label.appendChild(el("span", "guide-area-state", area.known ? "已了解" : "可以聊聊"));
        row.append(label, el("p", "guide-area-note", esc(area.summary || area.hint)));
        sideList.appendChild(row);
      });
      sideFoot.innerHTML = "";
      sideFoot.appendChild(el("p", "guide-note", profile.ready
        ? "已有适合开始的初步了解，剩下的可以在使用中慢慢补充。"
        : "聊到足够了解你的主题和阅读需要时，我会回顾一下，再邀请你开始。也可以随时提前进入。"));
      const go = el("button", "linkish", "查看或修改已记录的信息");
      go.onclick = () => setView("me");
      sideFoot.appendChild(go);
    }

    /* 按层分列展示记忆。每层的顺序就是提问顺序（身份→当前实践→能力→经历→倾向→约束），
       用户能一眼看出「它把哪些事算成同一类」，也能直接改。

       项目单独做成一叠「文件夹」放在最前面：一个项目一张卡，属性是它的子行。
       存储上 project:kotoba.what / .blocker 仍是各自独立的事实（证据、TTL、
       单独改删都在），这里只是把它们聚起来看——因为用户的原话是
       「识别为大项目的可以创建为大文件夹存储相关记忆」。 */
    async function refreshFacts() {
      try {
        if (!S.entered) {
          const profile = await api("GET", `/api/onboard/readiness?uid=${encodeURIComponent(S.uid)}`);
          if (mySeq === seq) renderGuide(profile);
          return;
        }
        const [rank, mem, mine] = await Promise.all([
          api("GET", `/api/directions/ranking?uid=${encodeURIComponent(S.uid)}`).catch(() => ({ items: [] })),
          api("GET", `/api/memory?uid=${encodeURIComponent(S.uid)}`).catch(() => ({ layers: [] })),
          api("GET", `/api/me/facts?uid=${encodeURIComponent(S.uid)}`).catch(() => ({ facts: [] })),
        ]);
        const items = rank.items || [];
        const layers = (mem.layers || []).filter((L) => (L.facts || []).length);
        const nFacts = layers.reduce((n, L) => n + (L.facts || []).length, 0);
        const drafts = (mine.facts || []).filter((f) => f.status === "draft");
        sideList.innerHTML = "";
        items.slice(0, 5).forEach((it) => {
          const row = el("div", "rank-row");
          const name = (window.RG_DIR && RG_DIR.displayName(it.node)) || it.node;
          // 和方向树说明浮层同一套状态：加过就带 ✓，按钮变成取消添加
          const added = trail().dirs.includes(it.node);
          const nameP = el("p", "rank-name", esc(name));
          if (added) nameP.appendChild(el("span", "added-tag", "✓ 已添加"));
          row.appendChild(nameP);
          (it.why || []).slice(0, 2).forEach((w) => row.appendChild(el("p", "rank-why", esc(w))));
          const acts = el("div", "rank-acts");
          const pick = el("button", "btn small", "选这个方向");
          pick.type = "button";
          pick.onclick = async () => {
            await chooseDirection(it.node);
            refreshFacts();
          };
          const add = el("button", "btn small secondary", added ? "取消添加" : "添加到我的方向");
          add.type = "button";
          add.onclick = async () => {
            if (added) await removeMyDirection(it.node);
            else await addMyDirection(it.node);
            refreshFacts();
          };
          acts.append(pick, add);
          row.appendChild(acts);
          sideList.appendChild(row);
        });
        sideFoot.innerHTML = "";
        const go = el("button", "linkish",
          drafts.length ? `先核对 ${drafts.length} 条 · 它记下 ${nFacts} 条 · 去「我的」页核对`
            : `它记下 ${nFacts} 条 · 去「我的」页核对`);
        go.type = "button";
        go.onclick = () => setView("me");
        sideFoot.appendChild(go);
        sideEmpty.style.display = items.length ? "none" : "block";
      } catch (_) { /* 侧栏失败不影响对话 */ }
    }

    function setStage(name, tool) {
      if (!name) { stageLine.textContent = ""; stageLine.classList.remove("on"); return; }
      let text = STAGE_CN[name] || "处理中";
      if (name === "tool" && tool) text = `正在查：${tool}`;
      stageLine.textContent = text;
      stageLine.classList.add("on");
    }

    function addBubble(role, text) {
      const kind = role === "user" ? "user" : "ai";
      const b = el("div", `bubble ${kind}${kind === "ai" ? " md" : ""}`);
      if (kind === "ai") b.innerHTML = mdLite(text);
      else if (text.length > 400 && /\d{2}-\d{2}学年度/.test(text) && text.includes("学分")) {
        b.classList.add("bubble-transcript");
        const fold = el("details", "pasted-transcript");
        fold.appendChild(el("summary", "", "已粘贴成绩单 · 展开原文"));
        const raw = el("div", "pasted-text");
        raw.textContent = text;
        fold.appendChild(raw);
        b.appendChild(fold);
      } else b.textContent = text;
      scroll.appendChild(b);
      scroll.scrollTop = scroll.scrollHeight;
      return b;
    }

    function autoGrow() {
      input.style.height = "auto";
      const height = input.scrollHeight + 2;
      input.style.height = Math.max(48, Math.min(height, 160)) + "px";
      input.style.overflowY = height > 160 ? "auto" : "hidden";
    }

    input.addEventListener("input", autoGrow);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); }
    });
    sendBtn.onclick = () => send(input.value);
    stopBtn.onclick = () => { if (abort) abort.abort(); };

    // 上一轮在生成时切走又回来：这个新视图里没有那一轮的气泡和进度，旧视图收到的回复也不会画到这里。
    // 还在生成就锁住输入、给停止键；做完（不管在这之前还是之后）从服务器重取一遍历史重画。
    // 原来发送键看着能点、点了没反应，做完的回复也不显示，要手动刷新（Codex 复现）。
    if (resumeFrom) {
      if (busy) {
        input.disabled = true; sendBtn.disabled = true; stopBtn.hidden = false;
        stageLine.textContent = "上一句还在生成，好了会自动显示…";
        stageLine.classList.add("on");
      }
      resumeFrom.then(() => {
        if (mySeq !== seq || S.view !== "dialogue") return;
        // 正在改一条记忆：重画会把输入框拔掉，失焦保存不一定触发，改动就没了（Codex 复现）。改完再画
        if (editingCard) { redrawQueued = true; return; }
        render($app);
      });
    }
  }

  return { render, mdLite };
})();
