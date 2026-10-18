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
    head.appendChild(el("h2", "", "对话"));
    head.appendChild(el("p", "", "有什么想搞清楚的，直接说。年级、专业、卡在哪，都行。"));
    if (!S.entered) {
      const bar = el("div", "enter-bar");
      const go = el("button", "btn", "进入启研");
      go.type = "button";
      go.onclick = () => enterApp();
      bar.appendChild(go);
      bar.appendChild(el("span", "form-note", "随时可以先做事，不必聊完。"));
      head.appendChild(bar);
    }
    return head;
  }

  async function render($app) {
    const mySeq = ++seq;

    $app.innerHTML = "";
    $app.appendChild(dialogueHead());

    /* 对话页分成**三个有名字的区**，顺序固定，各自管一件事：
         .chat-scroll  对话流     —— 只有气泡，它自己滚
         .chat-next    下一步     —— 行动卡 / 回执 / 可选说法，给最大高度，自己滚
         .chat-input   输入        —— 状态行 + 输入框，永远在最下面
       以前这三样都塞在一个 .chat-foot 里：行动卡和输入框抢同一块地方，
       行动卡一高就把输入框顶出屏幕，而输入框本来该是位置最稳定的那个。 */
    const wrap = el("div", "two-col two-col-chat");
    const chat = el("div", "panel chat-panel chat-wide");
    const scroll = el("div", "chat-scroll");
    scroll.setAttribute("aria-live", "polite");

    // 行动区本身就是「下一步」这个区。.action-slot:empty 已经会隐藏它，
    // 所以没有卡片时不会留一条空带。
    const actions = el("div", "action-slot chat-next");
    const inputZone = el("div", "chat-input");
    const stageLine = el("p", "stage-line");
    const inputRow = el("div", "chat-input-row");
    const input = el("textarea");
    input.rows = 1;
    input.placeholder = "说说你现在的情况，或者你想弄清楚的问题…（Shift + 回车换行）";
    input.setAttribute("aria-label", "对话输入");
    const sendBtn = el("button", "btn small", "发送");
    sendBtn.type = "button";
    const stopBtn = el("button", "btn ghost small", "停止");
    stopBtn.type = "button";
    stopBtn.hidden = true;
    inputRow.append(input, sendBtn, stopBtn);
    // 状态行（正在观察 / 正在查资料）说的是「输入之后会发生什么」，
    // 所以它归输入区，不归行动区——否则它会跟着行动卡一起滚走。
    inputZone.append(stageLine, inputRow);
    chat.append(scroll, actions, inputZone);

    const side = el("div", "panel side-panel mem-sum");
    const sideHead = el("div", "rank-head");
    sideHead.appendChild(el("p", "side-title", "按你说过的，可能适合的方向"));
    const treeBtn = el("button", "linkish", "查看方向树");
    treeBtn.type = "button";
    treeBtn.onclick = () => (S.entered ? setView("cards") : enterApp("cards"));
    sideHead.appendChild(treeBtn);
    side.appendChild(sideHead);
    side.appendChild(el("p", "side-sub rank-badge", "规则推测"));
    const sideList = el("div", "rank-list");
    const sideEmpty = el("p", "side-empty", "再聊几句兴趣或目标，这里会按你说过的排方向。");
    const sideFoot = el("p", "rank-foot", "");
    side.append(sideList, sideEmpty, sideFoot);
    wrap.append(chat, side);
    $app.appendChild(wrap);

    autoGrow();
    input.focus();
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
    let history = { messages: [], pending_action: null };
    try {
      history = await api("GET", `/api/dialogue/history?uid=${encodeURIComponent(S.uid)}`);
    } catch (e) { toast(e.message); }
    if (mySeq !== seq) return;
    history.messages.forEach((m) => addBubble(m.role, m.text));
    if (history.pending_action) {
      renderAction(history.pending_action);
    } else if (history.just_finished) {
      // 刚在任务区把任务交掉了。这时候 pending_action 已经是空的（那一步终结了），
      // 如果什么都不画，用户回到对话就看不到自己刚做完的事——那张卡会凭空消失。
      // 但**只画一张**：再叠一张「这一步已完成」的卡就是在说同一件事，
      // 看起来像有两个任务。just_finished 里已经有标题、字数、得分，信息是全的。
      finishedNote(history.just_finished);
    } else if (history.last_action && history.last_action.status === "completed") {
      // 不是刚交的（比如隔了很久再打开），就只显示那张已完成状态的卡
      renderAction(history.last_action);
    }
    await refreshFacts();
    if (mySeq !== seq) return;
    if (history.onboard_done && !S.entered) enterCard();
    if (!history.messages.length) {
      addBubble("assistant", "你好。你不用说得很完整——先告诉我你现在最想弄清楚的一件事，或者你手上的情况。");
    }

    /* --- 发送 --- */

    async function send(text) {
      const msg = (text || "").trim();
      if (!msg || busy) return;
      try { sessionStorage.setItem("rg_chat_draft_" + S.uid, msg); } catch (_) { /* ignore */ }
      busy = true;
      input.value = ""; autoGrow();
      input.disabled = true; sendBtn.disabled = true; stopBtn.hidden = false;
      actions.innerHTML = "";
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
        if (mySeq !== seq) return;

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
        }
      } finally {
        bubble.classList.remove("streaming");
        busy = false; abort = null;
        if (mySeq === seq) {
          input.disabled = false; sendBtn.disabled = false; stopBtn.hidden = true;
          setStage("");
          input.focus();
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
         · 行动卡永远在最上面——它才是用户要动手的东西
    */
    function afterTurn(result) {
      // pending_action 是当前还开着的行动（界面状态）；next_action 只是这一轮决定给的那个
      renderAction(result.pending_action || null);
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
      if (result.onboard_done && !S.entered) enterCard();
      scroll.scrollTop = scroll.scrollHeight;
    }

    function enterCard() {
      if (actions.querySelector(".enter-card")) return;
      const card = el("div", "enter-card");
      card.appendChild(el("p", "enter-card-title", "可以进入启研了"));
      const row = el("div", "submit-actions");
      const go = el("button", "btn", "进入启研");
      go.type = "button";
      go.onclick = () => enterApp();
      const more = el("button", "btn ghost", "再聊聊");
      more.type = "button";
      more.onclick = () => { card.remove(); input.focus(); };
      row.append(go, more);
      card.appendChild(row);
      actions.appendChild(card);
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

      const box = el("div", "receipt");
      box.appendChild(el("p", "rc-head", "这一轮发生了什么"));
      groups.forEach((g) => {
        const row = el("div", "rc-group");
        const head = el("p", "rc-label", g.label);
        if (g.note) head.appendChild(el("span", "rc-count", g.note));
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
      const box = el("div", "receipt receipt-import");
      box.appendChild(el("p", "rc-head", "成绩单已导入"));
      const s = imp.summary || {};
      const facts = el("div", "ri-facts");
      const cell = (k, v) => {
        const c = el("div", "ri-cell");
        c.appendChild(el("span", "ri-k", k));
        c.appendChild(el("span", "ri-v", v));
        facts.appendChild(c);
      };
      cell("课程", `${imp.written} 门`);
      cell("通过学分", String(s.passed_credits != null ? s.passed_credits : "—"));
      cell("绩点", s.gpa != null ? s.gpa.toFixed(4) : "—");
      if (s.gpa_credits != null) cell("计入绩点的学分", String(s.gpa_credits));
      box.appendChild(facts);

      const un = s.ungraded || [];
      if (un.length) {
        box.appendChild(el("p", "rc-warn",
          `${un.length} 门是字母等级/五级制，没有算进绩点（教务换算口径未定，不敢替你定）：`
          + un.map((u) => `${u.course}（${u.grade}）`).join("、")));
      }
      const cur = s.in_progress || [];
      if (cur.length) {
        box.appendChild(el("p", "rc-note",
          `${cur.length} 门还在修：` + cur.map((c) => c.course).join("、")));
      }
      const d = imp.derived || [];
      if (d.length) {
        const ul = el("ul", "rc-list");
        d.forEach((f) => ul.appendChild(el("li", "", esc(f.value))));
        box.appendChild(el("p", "rc-label", "推出的能力结论"));
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
      actions.appendChild(row);
    }

    /* 刚在任务区交完任务、回到对话时的一句话。
       它是「任务区做完 → 回到对话」这条闭环的可见落点。 */
    function finishedNote(jf) {
      if (!jf) return;
      const p = (jf.payload || {});
      const box = el("div", "finished-note");
      box.appendChild(el("p", "fn-title", `你刚交了「${esc(jf.title || "")}」`));
      const bits = [];
      if (p.chars) bits.push(`写了 ${p.chars} 字`);
      if (typeof p.score === "number") bits.push(`按标准 ${p.score} 分`);
      if (bits.length) box.appendChild(el("p", "fn-sub", bits.join(" · ")));
      box.appendChild(el("p", "fn-sub", "它已经写进你的「经验积累」了。接着说下一步就行。"));
      addBubble("assistant", `看到了，你把「${jf.title || "那一步"}」交了。我按结果想想下一步。`);
      actions.parentNode.insertBefore(box, actions);
    }

    /* 行动卡。结构固定成四段，位置不随内容变：
         ① 状态（这一步是什么状态）
         ② 标题（要做什么）
         ③ 依据（为什么是给你的）—— 每条一行，不再挤成一句话
         ④ 动作（唯一该做的那件事）
       任务必须说得出依据，所以③不是装饰；没有依据就整段不出现，
       而不是留一个空标题。 */
    function renderAction(a) {
      actions.innerHTML = "";
      if (!a) return;
      const card = el("div", "action-card");
      card.appendChild(el("p", "action-kicker", a.status === "completed" ? "这一步已完成" : "下一步"));
      card.appendChild(el("p", "action-title", esc(a.title)));

      const why = (a.based_on || []).filter((x) => x && x.value);
      if (why.length) {
        const box = el("div", "action-why");
        box.appendChild(el("p", "aw-label", "为什么是给你的"));
        const ul = el("ul", "aw-list");
        why.forEach((x) => ul.appendChild(el("li", "", esc(x.value))));
        box.appendChild(ul);
        card.appendChild(box);
      }

      const row = el("div", "action-btns");

      // 「就做这个」→ 真的建一个任务（服务端 accept 时建），然后去任务区做。
      // 以前这里是「标记完成」，点一下就把这一步算走完了——没有交付物、
      // 没有反馈、也不写回记忆。用户问过「我在对话里出现的任务是要我怎么完成」，
      // 根因就是那个按钮什么都没要求。
      if (a.status === "offered" && a.action === "explore_direction") {
        const go = el("button", "btn small", "看方向排序");
        go.type = "button";
        go.onclick = () => refreshFacts();
        const tree = el("button", "btn small secondary", "去方向树");
        tree.type = "button";
        tree.onclick = () => (S.entered ? setView("cards") : enterApp("cards"));
        row.append(go, tree);
      } else if (a.status === "offered" && a.action === "micro_task") {
        /* 老卡只给「先不做」 */
      } else if (a.status === "offered") {
        const go = el("button", "btn small", "就做这个");
        go.type = "button";
        go.onclick = async () => {
          go.disabled = true;
          try {
            const r = await api("POST", "/api/dialogue/action", {
              uid: S.uid, action_id: a.action_id, event: "accept",
            });
            renderAction(r.action);
            actions.appendChild(el("p", "action-hint",
              "这一步已建好：说清「做什么 / 交什么 / 怎样算做到」。点下面的按钮就能去做，做完回来接着聊。"));
            addBubble("assistant", "好。点下面的按钮就能去做，做完回来接着聊。");
            await refreshFacts();
          } catch (e) { toast(e.message); go.disabled = false; }
        };
        row.appendChild(go);
      } else if (a.status === "accepted" || a.status === "in_progress") {
        // 已接：不再提供「标记完成」，只提供「去做」。完成只能通过提交交付物。
        const go = el("button", "btn small", "去做这一步");
        go.type = "button";
        go.onclick = () => {
          S.openTaskId = a.task_id || "";
          setView("workbench");
        };
        row.appendChild(go);
      }

      if (a.status !== "completed" && a.status !== "accepted" && a.status !== "in_progress") {
        const no = el("button", "btn ghost small", "先不做");
        no.type = "button";
        no.onclick = async () => {
          no.disabled = true;
          try {
            const r = await api("POST", "/api/dialogue/action",
              { uid: S.uid, action_id: a.action_id, event: "decline" });
            renderAction(r.action);
          } catch (e) { toast(e.message); }
        };
        row.appendChild(no);
      }
      if (a.status === "completed") card.classList.add("done");
      card.appendChild(row);
      actions.appendChild(card);
    }

    /* 按层分列展示记忆。每层的顺序就是提问顺序（身份→当前实践→能力→经历→倾向→约束），
       用户能一眼看出「它把哪些事算成同一类」，也能直接改。

       项目单独做成一叠「文件夹」放在最前面：一个项目一张卡，属性是它的子行。
       存储上 project:kotoba.what / .blocker 仍是各自独立的事实（证据、TTL、
       单独改删都在），这里只是把它们聚起来看——因为用户的原话是
       「识别为大项目的可以创建为大文件夹存储相关记忆」。 */
    async function refreshFacts() {
      try {
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
      if (kind === "ai") b.innerHTML = mdLite(text); else b.textContent = text;
      scroll.appendChild(b);
      scroll.scrollTop = scroll.scrollHeight;
      return b;
    }

    function autoGrow() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 160) + "px";
    }

    input.addEventListener("input", autoGrow);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); }
    });
    sendBtn.onclick = () => send(input.value);
    stopBtn.onclick = () => { if (abort) abort.abort(); };
  }

  return { render, mdLite };
})();
