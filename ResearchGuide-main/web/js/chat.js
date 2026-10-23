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
  let busy = false;      // 正在生成，阻止重复发送
  let abort = null;      // AbortController
  let seq = 0;           // 渲染序号，切走视图后不再写 DOM
  let root, panel, content, list, launcher, caption, newButton, portraitCaption;
  let scope = "", activeCid = "", conversations = [], pending = null;
  let opened = false, loading = 0, updateSend = () => {};
  let remoteWatch = null;
  let embedded = false;

  const scopeOf = () => `${S.uid}:${S.portraitId}`;
  const storageKey = (kind, cid = activeCid) => `rg_chat_${kind}_${S.uid}_${S.portraitId}_${cid || "new"}`;
  const readLocal = (key) => { try { return sessionStorage.getItem(key); } catch (_) { return null; } };
  const writeLocal = (key, value) => { try { sessionStorage.setItem(key, value); } catch (_) { /* private mode */ } };

  function button(label, cls = "btn small secondary") {
    const b = el("button", cls, esc(label)); b.type = "button"; return b;
  }

  function mount() {
    if (root) return;
    root = el("div", "chat-widget"); root.id = "chatWidget";
    launcher = button("", "chat-launcher");
    launcher.innerHTML = `<span class="chat-launcher-caption" aria-hidden="true"><strong>启研助手</strong><span>说问题 · 补背景</span></span><svg viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d="M21 13c5 0 9 3.1 9 7 0 2-1.1 3.9-3 5.2L28 29l-5-2c-.7.1-1.3.2-2 .2-5 0-9-3.2-9-7.2s4-7 9-7Z" fill="currentColor" opacity=".65"/><path d="M13 3C6.4 3 1 7.1 1 12.2c0 2.8 1.6 5.3 4.1 7L4 24l6-2.8c1 .2 2 .3 3 .3 6.6 0 12-4.1 12-9.3S19.6 3 13 3Z" fill="currentColor"/><circle cx="9" cy="12" r="1.4" fill="var(--teal-ink)"/><circle cx="17" cy="12" r="1.4" fill="var(--teal-ink)"/></svg>`;
    launcher.title = "向启研提问，补充背景或讨论阅读";
    launcher.setAttribute("aria-label", "打开启研对话");
    launcher.setAttribute("aria-controls", "chatDialog");
    launcher.setAttribute("aria-expanded", "false");
    launcher.onclick = () => opened ? close() : open();
    panel = el("section", "chat-dialog"); panel.id = "chatDialog";
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "启研对话"); panel.hidden = true;
    const head = el("div", "chat-widget-head");
    const identity = el("div", "chat-widget-identity");
    caption = button("新对话", "chat-widget-caption");
    caption.setAttribute("aria-label", "切换会话"); caption.title = "切换会话";
    caption.setAttribute("aria-controls", "chatConversationList");
    portraitCaption = el("span", "chat-widget-portrait", "当前画像");
    identity.append(caption, portraitCaption);
    const showList = caption; showList.setAttribute("aria-expanded", "false");
    showList.onclick = () => { list.hidden = !list.hidden; showList.setAttribute("aria-expanded", String(!list.hidden)); };
    newButton = button("＋ 新对话", "chat-toolbar-new");
    newButton.setAttribute("aria-label", "新对话"); newButton.title = "新建对话";
    newButton.onclick = async () => {
      if (busy) { toast("上一句还在生成，完成后可以新建"); return; }
      const origin = scope;
      newButton.disabled = true;
      try {
        const r = await api("POST", "/api/dialogue/conversations", { uid: S.uid, portrait_id: S.portraitId });
        if (origin !== scope) return;
        conversations.unshift(r.conversation); await select(r.conversation.id);
      } catch (e) { toast(e.message); }
      finally { newButton.disabled = busy; }
    };
    const expand = button("⤢", "chat-toolbar-icon"); expand.setAttribute("aria-pressed", "false");
    expand.setAttribute("aria-label", "扩大"); expand.title = "扩大对话框";
    expand.onclick = () => { const wide = panel.classList.toggle("is-wide"); expand.textContent = wide ? "⤡" : "⤢"; expand.setAttribute("aria-label", wide ? "还原" : "扩大"); expand.title = wide ? "还原对话框" : "扩大对话框"; expand.setAttribute("aria-pressed", String(wide)); };
    const hide = button("—", "chat-toolbar-icon"); hide.setAttribute("aria-label", "收起"); hide.title = "收起对话框"; hide.onclick = close;
    head.append(identity, newButton, expand, hide);
    list = el("nav", "chat-conversations"); list.id = "chatConversationList"; list.hidden = true; list.setAttribute("aria-label", "聊天会话");
    content = el("div", "chat-widget-content");
    panel.append(head, list, content); root.append(panel, launcher); document.body.appendChild(root);
    panel.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); if (!list.hidden) { list.hidden = true; caption.setAttribute("aria-expanded", "false"); caption.focus(); } else close(); } });
    document.addEventListener("click", (e) => {
      if (!head.contains(e.target) && !list.contains(e.target)) { list.hidden = true; caption.setAttribute("aria-expanded", "false"); }
    });
  }

  function close() {
    opened = false; if (!panel) return;
    list.hidden = true; caption.setAttribute("aria-expanded", "false");
    panel.hidden = true; launcher.setAttribute("aria-expanded", "false"); launcher.focus();
  }

  function reset() { scope = ""; ++seq; ++loading; if (content) content.replaceChildren(); }

  function paintList() {
    if (!list) return;
    list.replaceChildren();
    const selected = conversations.find((c) => c.id === activeCid);
    caption.textContent = (selected?.topic_hint || "新对话") + " ▾";
    caption.title = selected?.topic_hint ? `切换会话：${selected.topic_hint}` : "切换会话";
    portraitCaption.textContent = S.portraitName || "当前画像";
    conversations.forEach((c) => {
      const b = button((c.topic_hint || "新对话") + (pending?.cid === c.id ? " · 生成中" : ""), "chat-conversation");
      b.classList.toggle("active", c.id === activeCid);
      b.setAttribute("aria-current", c.id === activeCid ? "true" : "false");
      b.onclick = () => select(c.id).catch((e) => toast(e.message)); list.appendChild(b);
    });
    newButton.disabled = busy;
    launcher.classList.toggle("is-generating", busy);
    updateSend();
  }

  async function select(cid) {
    activeCid = cid || ""; writeLocal(storageKey("last", "scope"), activeCid);
    list.hidden = true; caption.setAttribute("aria-expanded", "false"); paintList();
    await renderContent(content, activeCid);
  }

  async function sync() {
    mount();
    const onboarding = S.view === "dialogue" && !S.entered && !!S.uid;
    if (embedded && !onboarding) {
      embedded = false; content.classList.remove("chat-onboarding-content");
      panel.appendChild(content); opened = false;
      await renderContent(content, activeCid);
    }
    root.hidden = !S.uid || S.view === "login";
    if (root.hidden) { panel.hidden = true; opened = false; return; }
    await ensurePortrait();
    const next = scopeOf();
    if (scope !== next) {
      scope = next; ++loading; ++seq; activeCid = ""; conversations = [];
      content.replaceChildren();
      if (opened) await loadScope();
    }
    portraitCaption.textContent = S.portraitName || "当前画像";
    panel.hidden = !opened;
    launcher.setAttribute("aria-expanded", String(opened));
    if (onboarding) root.hidden = true;
  }

  async function renderOnboarding(host) {
    await sync();
    embedded = true; opened = false; root.hidden = true; panel.hidden = true;
    content.classList.add("chat-onboarding-content");
    host.replaceChildren(content);
    await loadScope();
  }

  async function loadScope() {
    const ticket = ++loading, origin = scope;
    const r = await api("GET", `/api/dialogue/conversations?uid=${encodeURIComponent(S.uid)}&portrait_id=${encodeURIComponent(S.portraitId)}`);
    if (ticket !== loading || origin !== scope) return;
    conversations = r.conversations || [];
    const last = readLocal(storageKey("last", "scope"));
    await select(conversations.some((c) => c.id === last) ? last : (conversations[0]?.id || ""));
    if (r.busy && !busy) watchRemote(S.uid, S.portraitId, origin);
  }

  async function watchRemote(uid, portrait, origin) {
    if (remoteWatch) return;
    const watcher = remoteWatch = {};
    busy = true; paintList();
    try {
      while (origin === scope) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        try {
          const r = await api("GET", `/api/dialogue/conversations?uid=${encodeURIComponent(uid)}&portrait_id=${encodeURIComponent(portrait)}`);
          if (origin !== scope) return;
          if (!r.busy) {
            conversations = r.conversations || []; remoteWatch = null; busy = false; paintList();
            if (opened) await renderContent(content, activeCid); else content.replaceChildren();
            return;
          }
        } catch (e) {
          if ([401, 404, 409, 410].includes(e.status)) { toast(e.message); return; }
          // Keep drafts and poll after a network interruption. Never resend a turn.
        }
      }
    } finally {
      if (remoteWatch === watcher) { remoteWatch = null; busy = false; paintList(); }
    }
  }

  async function open() {
    try {
      await sync(); if (root.hidden) return;
      const wasOpen = opened; opened = true; panel.hidden = false; launcher.setAttribute("aria-expanded", "true");
      if (!wasOpen && !content.querySelector("textarea")) await loadScope();
      content.querySelector("textarea")?.focus({ preventScroll: true });
    } catch (e) { toast(e.message); }
  }

  function renderSources(container, sources) {
    if (!sources?.length) return;
    const box = el("details", "chat-reading-sources");
    box.appendChild(el("summary", "", "参考的已读文献"));
    sources.forEach((s) => {
      const b = button(`${s.title || s.paper_id} · 阅读卡 v${s.card_version}`, "linkish");
      b.onclick = () => { S.cardFrom = "read"; openCard(s.paper_id, s.kit_id || DEFAULT_KIT); close(); };
      box.appendChild(b);
    });
    container.appendChild(box);
  }

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
    // Decide fallback before sending anything: never POST the same turn twice.
    if (typeof ReadableStream === "undefined") {
      const data = await api("POST", "/api/dialogue/turn", payload);
      onEvent("result", data); return data;
    }
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
      throw new Error("浏览器无法接收流式回复，请稍后重开会话查看结果");
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
    if (embedded) {
      head.classList.add("onboarding-head");
      const intro = el("div", "onboarding-intro");
      intro.append(el("p", "onboarding-kicker", "欢迎来到启研"), el("h2", "", "先认识一下"),
        el("p", "onboarding-lead", "聊聊你的学习背景和好奇的问题，让之后的阅读建议更贴近你。"));
      const next = el("div", "onboarding-next");
      next.appendChild(el("p", "form-note", "不必一次说完，之后随时可以补充或更正。"));
      const go = button("开始今天的一件事", "btn secondary"); go.onclick = () => enterApp();
      const read = button("先挑一篇论文读", "btn"); read.onclick = () => enterApp("read");
      const row = el("div", "onboarding-actions"); row.append(read, go); next.appendChild(row);
      head.append(intro, next); return head;
    }
    head.appendChild(el("h2", "", S.entered ? "对话" : "先认识一下"));
    head.appendChild(el("p", "", S.entered
      ? "继续和启研聊一聊你的问题和阅读近况。"
      : "和启研聊一聊你的大概情况（基本信息，兴趣点，未来规划...）也可以随时进入使用。"));
    if (!S.entered) {
      const bar = el("div", "enter-bar");
      const go = el("button", "btn", "开始今天的一件事");  // 第四轮第三遍吐槽 3/3：「进入启研」不知道进去干嘛——按钮直接说去向（今日页）
      go.type = "button";
      go.onclick = () => enterApp();
      bar.appendChild(go);
      const read = el("button", "btn ghost", "先挑一篇论文读"); read.type = "button"; read.onclick = () => enterApp("read"); bar.appendChild(read);
      head.appendChild(bar);
    }
    return head;
  }

  async function renderContent($app, requestedCid = activeCid) {
    const origin = scope;
    let draftKey = storageKey("draft", requestedCid);
    let scrollKey = storageKey("scroll", requestedCid);
    const mySeq = ++seq;
    if (mySeq !== seq || origin !== scope) return;  // 等待期间切到了别的页：不能再往页面上写

    $app.innerHTML = "";
    if (embedded) $app.appendChild(dialogueHead());

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
    input.placeholder = S.entered ? "想聊什么，直接说…" : "说说你的院系、年级，或直接问我…";
    input.setAttribute("aria-label", "对话输入");
    const sendBtn = el("button", "btn small", "发送");
    sendBtn.type = "button";
    sendBtn.disabled = true; // History and handlers must be ready before sending.
    const stopBtn = el("button", "btn ghost small", "停止");
    stopBtn.type = "button";
    stopBtn.hidden = true;
    inputRow.append(input, sendBtn, stopBtn);
    // 状态行（正在观察 / 正在查资料）说的是「输入之后会发生什么」，
    // 所以它归输入区，不归行动区——否则它会跟着行动卡一起滚走。
    inputZone.append(suggestions, stageLine, inputRow);
    if (embedded) inputZone.appendChild(el("p", "composer-hint", "Enter 发送 · Shift+Enter 换行"));
    const extra = el("details", "composer-extra");
    extra.appendChild(el("summary", "", embedded ? "可选：粘贴成绩单，让我更了解你的基础" : "更多"));
    extra.appendChild(el("p", "", "打开树洞的成绩单页，全选复制后粘贴到上面的输入框；会存到你的资料里，先去掉不想分享的信息。"));
    extra.hidden = true;  // 第一屏不提成绩单（面板三轮 3/3 被它吓到）；发过一句话或已进入后才露出
    if (embedded) inputZone.appendChild(extra);
    else {
      const footer = el("div", "chat-composer-footer");
      const mine = button("查看我的画像", "linkish chat-profile-link");
      mine.onclick = () => { close(); setView("me"); };
      footer.append(mine, extra); inputZone.appendChild(footer);
    }
    chat.append(scroll, inputZone);

    const side = el("div", "panel side-panel mem-sum");
    const sideHead = el("div", "rank-head");
    sideHead.appendChild(el("p", "side-title", S.entered ? "可能适合你的方向" : "目前对你的了解"));
    const treeBtn = el("button", "linkish", "查看方向树");
    treeBtn.type = "button";
    treeBtn.hidden = !S.entered;
    treeBtn.onclick = openDirections;
    sideHead.appendChild(treeBtn);
    side.appendChild(sideHead);
    side.appendChild(el("p", "side-sub rank-badge", S.entered ? "规则推测" : "从学习背景聊起，其他信息慢慢补充。"));
    const sideList = el("div", "rank-list");
    const sideEmpty = el("p", "side-empty", embedded
      ? "从院系、年级或一个感兴趣的问题开始。聊过的背景会整理在这里，你可以随时核对。"
      : "再聊几句兴趣或目标，这里会按你说过的排方向。");
    const sideFoot = el("p", "rank-foot", "");
    side.append(sideList, sideEmpty, sideFoot);
    if (embedded) wrap.append(chat, side);
    else wrap.append(chat);
    $app.appendChild(wrap);

    autoGrow();
    input.focus({ preventScroll: true });
    try {
      const draft = sessionStorage.getItem(draftKey);
      if (draft) { input.value = draft; autoGrow(); }
    } catch (_) { /* ignore */ }
    input.addEventListener("input", () => {
      try { sessionStorage.setItem(draftKey, input.value); } catch (_) { /* ignore */ }
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
      history = await api("GET", `/api/dialogue/history?uid=${encodeURIComponent(S.uid)}&portrait_id=${encodeURIComponent(S.portraitId)}${requestedCid ? "&conversation_id=" + encodeURIComponent(requestedCid) : ""}`);
    } catch (e) { toast(e.message); }
    if (mySeq !== seq || origin !== scope) return;  // 等待期间切到了别的页：不能再往页面上写
    history.messages.forEach((m) => { addBubble(m.role, m.text); renderSources(scroll, m.reading_sources); });
    scroll.addEventListener("scroll", () => writeLocal(scrollKey, String(scroll.scrollTop)));
    const position = readLocal(scrollKey);
    if (position !== null) scroll.scrollTop = Number(position);
    extra.hidden = !(S.entered || history.messages.some((m) => m.role === "user"));
    await refreshFacts();
    if (mySeq !== seq || origin !== scope) return;  // 等待期间切到了别的页：不能再往页面上写
    if (history.guide && !S.entered) renderGuide(history.guide);
    if (history.onboard_done && !S.entered) enterCard(history.guide);
    if (!history.messages.length) {
      addBubble("assistant", history.welcome_message || "你好，我是启研。你是哪个院系、几年级？也可以直接问我；不想聊就点上面的按钮。");
    }

    /* --- 发送 --- */

    async function send(text) {
      const msg = (text || "").trim();
      if (!msg) return;
      if (busy) { toast("上一句还在生成，等它好了再发"); return; }
      const uid = S.uid, portrait = S.portraitId;
      const startedIn = scope;
      let cid = history.conversation_id;
      let sentKey = draftKey;
      writeLocal(sentKey, msg);
      busy = true;
      paintList();
      input.value = ""; autoGrow();
      sendBtn.disabled = true; stopBtn.hidden = true;
      actions.innerHTML = "";
      actions.remove();
      suggestions.innerHTML = "";
      addBubble("user", msg);
      extra.hidden = false;

      const bubble = addBubble("assistant", "");
      bubble.classList.add("streaming");
      let acc = "";
      let firstChunk = true;
      let recoveryNeeded = false;
      setStage("observe");

      try {
        if (!cid) {
          const created = await api("POST", "/api/dialogue/conversations", { uid, portrait_id: portrait });
          if (startedIn !== scope) return;
          cid = created.conversation.id; conversations.unshift(created.conversation);
          history.conversation_id = cid;
          const oldDraft = readLocal(draftKey);
          draftKey = storageKey("draft", cid); scrollKey = storageKey("scroll", cid);
          writeLocal(draftKey, oldDraft || msg); writeLocal(sentKey, ""); sentKey = draftKey;
          if (mySeq === seq) activeCid = cid;
          writeLocal(storageKey("last", "scope"), activeCid);
        }
        pending = { cid, scope: startedIn };
        paintList();
        const result = await streamTurn(
          { uid, portrait_id: portrait, message: msg, conversation_id: cid },
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
        if (result && readLocal(sentKey) === msg) writeLocal(sentKey, "");
        if (result && startedIn === scope) {
          const c = conversations.find((v) => v.id === cid);
          if (c) { c.topic_hint = c.topic_hint || msg.slice(0, 60); c.updated_at = new Date().toISOString(); }
          conversations.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
        }
        if (mySeq !== seq || origin !== scope) return;

        if (result) {
          history.conversation_id = result.conversation_id;
          bubble.innerHTML = mdLite(result.reply);
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
        if (startedIn === scope) {
          // Disconnecting the stream does not cancel the server's worker.
          // Wait for that worker before enabling another send.
          const r = await api("GET", `/api/dialogue/conversations?uid=${encodeURIComponent(uid)}&portrait_id=${encodeURIComponent(portrait)}`).catch(() => null);
          recoveryNeeded = !r || r.busy;
        }
      } finally {
        bubble.classList.remove("streaming");
        busy = false; abort = null; pending = null;
        paintList();
        if (mySeq !== seq && startedIn === scope && activeCid === cid) {
          renderContent(content, activeCid).catch((e) => toast(e.message));
        }
        if (mySeq === seq) {
          input.disabled = false; sendBtn.disabled = false; stopBtn.hidden = true;
          setStage("");
          input.focus({ preventScroll: true });
        }
        if (recoveryNeeded) watchRemote(uid, portrait, startedIn);
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
      renderSources(actions, result.reading_sources);
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
      if (S.entered || actions.querySelector(".enter-card")) return;
      const card = el("div", "enter-card");
      card.appendChild(el("p", "enter-card-title", "可以进入启研了"));
      card.appendChild(el("p", "form-note", "已了解你关注的主题和几方面阅读背景。其他信息可以以后再补；也可以继续聊。"));
      const row = el("div", "submit-actions");
      const go = el("button", "btn", "查看推荐方向");
      go.type = "button";
      go.onclick = () => openDirections();
      const read = button("直接进入研读", "btn secondary");
      read.onclick = () => enterApp("read");
      const more = el("button", "btn ghost", "再聊聊");
      more.type = "button";
      more.onclick = () => { card.remove(); input.focus({ preventScroll: true }); };
      row.append(go, read, more);
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
      side.hidden = !embedded && !S.entered && !(profile.areas || []).some((a) => a.known);
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
        : "聊到足够了解你的主题和阅读需要时，我会回顾一下并邀请你开始，也可以随时提前进入。"));
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
      if (!embedded) return;
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

    updateSend = () => { sendBtn.disabled = busy; stopBtn.hidden = true; };
    updateSend();
    if (busy) setStage("compose");
  }

  return { open, close, sync, reset, renderOnboarding, mdLite, isBusy: () => busy };
})();
