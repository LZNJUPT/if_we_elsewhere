const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const DIMS = [
  ["closeness", "亲密度"], ["conflict", "冲突"], ["trust", "信任"],
  ["emotional_safety", "情绪安全"], ["comm_quality", "沟通"],
];

const S = {
  lines: [], line: null, messages: [], track: [], anchors: null, busy: false, curDay: "",
  history: [], histTotal: 0, histFirst: null, histVisible: true, histLoading: false,
  lineStart: "", histInfo: null,
};

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove("on"), 2800);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let d = "";
    try { d = (await r.json()).detail || ""; } catch (e) { }
    throw new Error(d || ("HTTP " + r.status));
  }
  return r.json();
}

const esc = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mediumPrefix = m => m === "image" ? "[图片] " : m === "emoji" ? "[表情包] " : m === "voice" ? "[语音] " : "";

/* ---------------------------------------------------------------- 渲染 */
function curLine() { return S.lines.find(l => l.sim_id === S.line) || null; }

function renderHead() {
  const l = curLine();
  $("#mTitle").textContent = l ? l.name : "还没有线";
  $("#mDay").textContent = l ? l.cur_day : "—";
  $("#mMsg").textContent = l ? `${l.n_msg} 条` : "—";
  $("#mTitle").title = l && l.rewrite ? "改写：" + l.rewrite : "";
  $("#btnDay").disabled = !l || S.busy;
}

function renderLines() {
  const box = $("#lines");
  box.innerHTML = "";
  if (!S.lines.length) {
    box.innerHTML = '<div class="empty">还没有任何线。<br>点右上角开一条。</div>';
    return;
  }
  for (const l of S.lines) {
    const el = document.createElement("div");
    el.className = "line-item" + (l.sim_id === S.line ? " active" : "");
    const close = l.rel ? Math.round(l.rel.closeness * 10) / 10 : "—";
    el.innerHTML =
      `<div class="line-name">${esc(l.name)}</div>
       <div class="line-meta"><span>${l.cur_day}</span><span>${l.n_msg} 条</span><span>亲密 ${close}</span></div>
       ${l.rewrite ? `<div class="line-rw" title="${esc(l.rewrite)}">改写：${esc(l.rewrite)}</div>` : ""}
       <div class="line-del" title="删除这条线">×</div>`;
    el.onclick = e => {
      if (e.target.classList.contains("line-del")) return;
      if (l.sim_id !== S.line) { S.line = l.sim_id; refresh(l.sim_id); }
    };
    $(".line-del", el).onclick = async e => {
      e.stopPropagation();
      if (!confirm(`删除「${l.name}」？\n只删这条模拟线，真实聊天记录不受影响。`)) return;
      try {
        await api("/api/lines/" + l.sim_id, { method: "DELETE" });
        if (S.line === l.sim_id) S.line = null;
        await refresh();
        toast("已删除");
      } catch (err) { toast("删除失败：" + err.message); }
    };
    box.appendChild(el);
  }
}

function daySep(text, hist) {
  const d = document.createElement("div");
  d.className = "day-sep" + (hist ? " hist" : "");
  d.textContent = text;
  return d;
}

function msgNode(m, hist) {
  // 真实记录用 sender('A'/'B') + text；本条线的对话用 human(bool) + content
  const isMe = hist ? m.sender === "A" : !!m.human;
  const text = hist ? m.text : m.content;
  const wrap = document.createElement("div");
  wrap.className = "msg " + (isMe ? "me" : "her") + (hist ? " hist" : "");
  const av = document.createElement("div");
  av.className = "avatar";
  av.textContent = isMe ? "你" : "TA";
  const body = document.createElement("div");
  body.className = "body";
  const sticker = hist ? m.media : m.sticker;      // 真实表情包文件名（有则直接显示真图）
  if (sticker) {
    const img = document.createElement("img");
    img.className = "sticker";
    img.src = "/api/media/" + sticker;
    img.alt = "表情包";
    img.loading = "lazy";
    img.title = "点击看大图";
    img.onclick = () => window.open(img.src, "_blank");
    img.onerror = () => {                          // 文件缺失时优雅降级为占位
      const fb = document.createElement("div");
      fb.className = "bubble";
      fb.textContent = "[表情包]";
      img.replaceWith(fb);
    };
    body.appendChild(img);
  } else {
    const b = document.createElement("div");
    b.className = "bubble";
    // 真实记录的媒介占位符已在后端生成（text 里带了 [图片]/[表情包]），此处不再重复加前缀
    b.textContent = (hist ? "" : mediumPrefix(m.medium)) + (text || "");
    body.appendChild(b);
  }
  if (!isMe && m.action) {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = m.action;
    body.appendChild(meta);
  }
  wrap.appendChild(av);
  wrap.appendChild(body);
  return wrap;
}

function renderChat() {
  const box = $("#chat");
  box.innerHTML = "";
  if (!S.line) {
    box.innerHTML = '<div class="hint">左侧还没有线。<br>点「＋ 新的一条线」开始。</div>';
    return;
  }

  // ① 真实聊天记录（这条线起点之前的）
  if (S.histVisible) {
    if (S.history.length) {
      const more = document.createElement("div");
      more.className = "hist-more";
      more.innerHTML = S.history.length < S.histTotal
        ? `<button class="btn btn-sm" id="btnMore">↑ 更早的记录（还有 ${S.histTotal - S.history.length} 条）</button>`
        : `<span class="muted">已翻到最早 —— ${S.histFirst || ""}</span>`;
      box.appendChild(more);
      let hd = null;
      for (const m of S.history) {
        if (m.day !== hd) { hd = m.day; box.appendChild(daySep(hd, true)); }
        box.appendChild(msgNode(m, true));
      }
    } else {
      const tip = document.createElement("div");
      tip.className = "hist-tip";
      tip.textContent = "（这条线之前没有更早的真实记录）";
      box.appendChild(tip);
    }
    // ② 起点分隔
    const ls = document.createElement("div");
    ls.className = "line-start";
    const inner = document.createElement("div");
    inner.className = "ls";
    const sp = document.createElement("span");
    sp.textContent = "真实记录到此为止";
    const b = document.createElement("b");
    b.textContent = `以下是你从 ${S.lineStart || ""} 开始的时间线`;
    inner.appendChild(sp);
    inner.appendChild(b);
    ls.appendChild(inner);
    box.appendChild(ls);
  }

  // ③ 本条线的对话
  if (!S.messages.length) {
    const h = document.createElement("div");
    h.className = "hint";
    h.textContent = "这条线还没有对话。在下面说第一句话。";
    box.appendChild(h);
  }
  let day = null;
  for (const m of S.messages) {
    if (m.day !== day) { day = m.day; box.appendChild(daySep(day, false)); }
    box.appendChild(msgNode(m, false));
  }

  const t = document.createElement("div");
  t.className = "msg her typing";
  t.id = "typing";
  t.innerHTML = '<div class="avatar">TA</div><div class="body"><div class="bubble"><i></i><i></i><i></i></div></div>';
  box.appendChild(t);

  const mb = $("#btnMore");
  if (mb) mb.onclick = loadMore;
}

/* ---------------------------------------------------------------- 真实记录 */
async function loadHistory(reset) {
  if (!S.line || S.histLoading) return;
  S.histLoading = true;
  try {
    if (reset) S.history = [];
    const d = await api(`/api/history?line=${encodeURIComponent(S.line)}`
      + `&limit=40&offset=${S.history.length}`);
    S.history = d.items.concat(S.history);
    S.histTotal = d.total;
    S.histFirst = d.first_day;
    S.lineStart = d.start_day;
    S.histInfo = { divergence: d.divergence, rewrite: d.rewrite };
  } catch (e) {
    toast("加载真实记录失败：" + e.message);
  } finally {
    S.histLoading = false;
  }
}

async function loadMore() {
  const c = $("#chat");
  const before = c.scrollHeight;
  await loadHistory(false);
  renderChat();
  c.scrollTop = c.scrollHeight - before;   // 保持视觉位置，不跳走
}

async function loadRecall(month) {
  const box = $("#pastRecall");
  box.className = "recall on";
  box.innerHTML = '<div class="recall-h">正在翻那段时间…</div>';
  try {
    const d = await api(`/api/recall?month=${month}`);
    if (!d.items.length) { box.className = "recall"; return; }
    const rows = d.items.map(m =>
      `<div class="recall-row${m.sender === "A" ? " me" : ""}">`
      + `<span class="who">${m.sender === "A" ? "你" : "TA"}</span>`
      + `<span class="tx">${esc(m.text)}</span></div>`).join("");
    box.innerHTML = `<div class="recall-h">${month} · 当月 ${d.n} 条 · 先看看那时你们说了什么</div>${rows}`;
  } catch (e) {
    box.className = "recall";
  }
}

function renderRel() {
  const l = curLine();
  const box = $("#relBody");
  box.innerHTML = "";
  if (!l || !l.rel) return;
  for (const [k, label] of DIMS) {
    const v = l.rel[k] == null ? 0 : l.rel[k];
    const row = document.createElement("div");
    row.className = "rel-row";
    row.innerHTML = `<span>${label}</span><span class="bar"><i style="width:${Math.min(100, v * 10)}%"></i></span><b>${v}</b>`;
    box.appendChild(row);
  }
  const d = document.createElement("div");
  d.className = "hint";
  d.style.padding = "2px 0 0";
  d.textContent = "模拟估计量 · 非事实（" + (l.rel.day || "") + "）";
  box.appendChild(d);
}

function scrollBottom() {
  const c = $("#chat");
  c.scrollTop = c.scrollHeight;
}

function setTyping(on) {
  const t = $("#typing");
  if (t) t.classList.toggle("on", on);
}

function updateBusy() {
  $("#input").disabled = S.busy || !S.line;
  $("#btnSend").disabled = S.busy || !S.line;
  $("#btnDay").disabled = S.busy || !S.line;
}

/* ---------------------------------------------------------------- 数据 */
async function refresh(lineId) {
  const id = lineId || S.line;
  const url = "/api/state" + (id ? "?line=" + encodeURIComponent(id) : "");
  const d = await api(url);
  S.lines = d.lines || [];
  S.line = d.line;
  S.messages = d.messages || [];
  const l = curLine();
  S.curDay = l ? l.cur_day : "";
  S.lineStart = l ? l.start_day : "";
  S.history = [];
  if (S.histVisible && S.line) await loadHistory(true);
  renderLines(); renderChat(); renderRel(); renderHead(); updateBusy();
  // 还没聊过的线：停在顶部，先看真实历史；已聊过的：停在最新
  if (S.messages.length) scrollBottom();
  else $("#chat").scrollTop = 0;
}

async function send() {
  const ta = $("#input");
  const text = ta.value.trim();
  if (!text || S.busy || !S.line) return;
  S.busy = true; updateBusy();
  ta.value = ""; ta.style.height = "auto";

  S.messages.push({ day: S.curDay, sender: "A", content: text, medium: "text", human: true, action: "" });
  renderChat(); setTyping(true); scrollBottom();

  try {
    const r = await api("/api/say", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line: S.line, text }),
    });
    setTyping(false);
    if (r.event_note) toast("事件：" + r.event_note.note);
    if (r.advanced_to) toast("时间来到 " + r.advanced_to);
    await refresh(S.line);
  } catch (e) {
    setTyping(false);
    S.messages.pop();
    ta.value = text;
    renderChat(); scrollBottom();
    toast("没发出去：" + e.message);
  } finally {
    S.busy = false; updateBusy(); ta.focus();
  }
}

async function advanceDay() {
  if (!S.line || S.busy) return;
  S.busy = true; updateBusy();
  try {
    const r = await api("/api/day", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line: S.line, n: 1 }),
    });
    toast("时间来到 " + r.day);
    await refresh(S.line);
  } catch (e) { toast("失败：" + e.message); }
  finally { S.busy = false; updateBusy(); }
}

/* ---------------------------------------------------------------- 新建线 */
function openModal() {
  $("#modal").classList.add("open");
  if (!S.anchors) loadAnchors();
}

function closeModal() { $("#modal").classList.remove("open"); }

async function loadAnchors() {
  try {
    const a = await api("/api/anchors");
    S.anchors = a;
    const dn = $("#pane-now [name=date-now]");
    if (!dn.value) dn.value = a.default_start;
    $("#nowInfo").innerHTML =
      `聊天记录：<b>${a.first_day}</b> ～ <b>${a.last_day}</b>，共 <b>${a.n_messages}</b> 条。<br>` +
      `接着往下聊＝从你的记录之后继续，对方会记得全部历史。`;
    renderTimeline();
  } catch (e) { toast("加载时间轴失败：" + e.message); }
}

function monthRange(first, last) {
  const out = [];
  let [y, m] = first.slice(0, 7).split("-").map(Number);
  const [ey, em] = last.slice(0, 7).split("-").map(Number);
  while (y < ey || (y === ey && m <= em)) {
    out.push(y + "-" + String(m).padStart(2, "0"));
    m++; if (m > 12) { m = 1; y++; }
  }
  return out;
}

function renderTimeline() {
  const A = S.anchors; if (!A) return;
  const box = $("#tl");
  box.innerHTML = "";
  const months = monthRange(A.first_day, A.last_day);
  const nMap = {};
  for (const m of A.monthly) nMap[m.month] = m.n;
  const maxN = Math.max(1, ...A.monthly.map(m => m.n));
  const byMonth = {};
  for (const p of A.posts) {
    const k = (p.day || "").slice(0, 7);
    (byMonth[k] = byMonth[k] || []).push(p);
  }
  for (const mm of months) {
    const n = nMap[mm] || 0;
    const row = document.createElement("div");
    row.className = "tl-month" + (n ? "" : " blank");
    const pct = n ? Math.max(2, Math.round(n / maxN * 100)) : 0;
    const dps = A.decision_points.filter(d => d.day.slice(0, 7) === mm)
      .map(d => `<span class="tl-b dp" title="${esc(d.note)}">${esc(d.label)}</span>`).join("");
    const posts = (byMonth[mm] || [])
      .map(p => `<span class="tl-b ${p.tone}" title="${esc(p.title)}">${esc(p.type_label)}</span>`).join("");
    row.innerHTML = `<span class="tl-mlabel">${mm}</span>
      <span class="tl-bar"><i style="width:${pct}%"></i></span>
      <span class="tl-badges">${dps}${posts}</span>`;
    row.onclick = () => pickMonth(mm, row);
    box.appendChild(row);
  }
  const blanks = months.filter(m => !nMap[m]).length;
  $("#tlSummary").innerHTML =
    `真实记录 <b>${A.first_day}</b> ～ <b>${A.last_day}</b>　共 <b>${months.length}</b> 个月` +
    `（其中 <b>${blanks}</b> 个月双方完全没说话，条为空）　左边数字＝月份，蓝条＝当月聊天量`;
  box.scrollTop = box.scrollHeight;
}

function pickMonth(month, row) {
  $$(".tl-month").forEach(e => e.classList.remove("sel"));
  row.classList.add("sel");
  const A = S.anchors;
  const n = (A.monthly.find(m => m.month === month) || {}).n || 0;
  const rel = A.rel.filter(r => r.period <= month).pop();
  const dps = A.decision_points.filter(d => d.day.slice(0, 7) === month);
  const posts = A.posts.filter(p => (p.day || "").slice(0, 7) === month);
  const day = dps.length ? dps[0].day : month + "-15";
  $("#pane-past [name=date-past]").value = day;

  const relTxt = rel
    ? `亲密度 <b>${rel.closeness}</b> · 信任 <b>${rel.trust}</b> · 沟通 <b>${rel.comm_quality}</b>`
    : "该月无状态估计";
  $("#pastInfo").innerHTML =
    `<b>${month}</b>　当月消息 <b>${n}</b> 条　${relTxt}` +
    (dps.length ? "<br>岔路口：" + dps.map(x => esc(x.label) + " —— " + esc(x.note)).join("；") : "") +
    (posts.length ? "<br>该月标记：" + posts.map(p => esc(p.type_label) + "·" + esc(p.title)).join("；") : "");

  const ni = $("#pane-past [name=name-past]");
  if (!ni.value) ni.value = dps.length ? dps[0].label : month + " 那天";

  loadRecall(month);
}

async function createLine(start, name, rewrite) {
  const body = { start, name, rewrite: rewrite || null, divergence: rewrite ? start : null };
  try {
    const r = await api("/api/lines", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    closeModal();
    S.line = r.sim_id;
    await refresh(r.sim_id);
    toast(`已开一条新线「${r.name}」`);
    $("#input").focus();
  } catch (e) { toast("创建失败：" + e.message); }
}

/* ---------------------------------------------------------------- 事件绑定 */
$("#btnNew").onclick = openModal;
$("#btnClose").onclick = closeModal;
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };

function switchPane(name) {
  $$(".tab").forEach(x => x.classList.toggle("active", x.dataset.pane === name));
  $$(".pane").forEach(p => p.classList.toggle("active", p.id === "pane-" + name));
  if (name === "past") loadAnchors();
}

$$(".tab").forEach(t => t.onclick = () => switchPane(t.dataset.pane));

$("#btnCreateNow").onclick = () => {
  const d = $("#pane-now [name=date-now]").value;
  const n = $("#pane-now [name=name-now]").value.trim() || "继续聊";
  if (!d) return toast("先选一个起始日期");
  createLine(d, n, "");
};

$("#btnCreatePast").onclick = () => {
  const d = $("#pane-past [name=date-past]").value;
  const n = $("#pane-past [name=name-past]").value.trim();
  const rw = $("#pane-past [name=rewrite]").value.trim();
  if (!d) return toast("先选一个日期");
  createLine(d, n || ("从 " + d + " 重新开始"), rw);
};

$("#btnSend").onclick = send;
$("#btnDay").onclick = advanceDay;

$("#btnHist").onclick = async () => {
  S.histVisible = !S.histVisible;
  $("#btnHist").classList.toggle("active", S.histVisible);
  $("#btnHist").textContent = S.histVisible ? "真实记录 ✓" : "真实记录";
  if (S.histVisible && S.line && !S.history.length) await loadHistory(true);
  renderChat();
  scrollBottom();
};

$("#input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#input").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(130, e.target.scrollHeight) + "px";
});

$("#relHead").onclick = () => {
  const b = $("#relBody");
  const open = b.classList.toggle("open");
  $("#relToggle").textContent = open ? "收起 ▴" : "展开 ▾";
};

document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

/* ---------------------------------------------------------------- 启动 */
(async () => {
  try {
    const h = await api("/api/health");
    if (!h.key) toast("未检测到 LLM API Key（环境变量 " + (h.key_env || "LLM_API_KEY") + "）：可以浏览，但发消息会失败");
  } catch (e) { }
  try { await refresh(); } catch (e) { toast("加载失败：" + e.message); }
  if (location.hash === "#new") openModal();
  if (location.hash === "#new-past") { openModal(); switchPane("past"); }
})();
