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
  syncActionButtons();
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

/* ---------------------------------------------------------------- 导入向导（v0.3 · 多源合并） */
const IMP = {
  batch: null, files: [], excluded: [], sel: {}, merge: null, busy: false,
};

function kv(k, v) { return `<div class="k">${k}</div><div class="v">${v}</div>`; }

/** 带超时的 api()：卡住的请求会自己中断，界面不会永远停在「处理中…」。
 *  抛出的错误 name === "AbortError" 表示超时。 */
async function apiTimeout(path, opts, ms) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    return await api(path, { ...(opts || {}), signal: ctl.signal });
  } finally {
    clearTimeout(timer);
  }
}
const isTimeout = e => !!(e && (e.name === "AbortError" || e.name === "TimeoutError"));
const T_UPLOAD = 300000, T_PREVIEW = 180000, T_MERGE = 120000, T_COMMIT = 900000;

/** 一份来源的 A/B 映射载荷。
 *  ⚠ 字段名必须与服务端 ImportSourceSel 一致（sender_a / sender_b）。
 *  这里曾经直接展开本地的 {a, b}，而服务端 pydantic 会**静默忽略未知字段**，
 *  于是映射被读成空串，用户看到的是莫名其妙的「「xxx.json」还没选 A/B 双方账号」。
 *  改这里之前先看 tests/test_importers.py::TestImportApiContract。 */
function selPayload(importId) {
  const s = IMP.sel[importId] || {};
  return { import_id: importId, sender_a: s.a || "", sender_b: s.b || "" };
}
function fmtSize(n) {
  if (!n && n !== 0) return "—";
  return n > 1024 * 1024 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
}
function fmtTs(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function impStep(n) {
  for (let i = 1; i <= 3; i++) {
    $(`#imp-pane${i}`).classList.toggle("active", i === n);
    const s = $(`#impS${i}`);
    s.classList.toggle("on", i === n);
    s.classList.toggle("done", i < n);
  }
}

function openImport() {
  IMP.batch = null; IMP.files = []; IMP.excluded = []; IMP.sel = {};
  IMP.merge = null; IMP.busy = false;
  $("#impMerge").innerHTML = "正在分析…";
  $("#impSrcList").innerHTML = "";
  $("#impFile").value = "";
  const nm = V3.curName();
  $("#impTarget").innerHTML = `⚠ 导入会把<b>当前好友「${esc(nm)}」</b>的库按「全部已导入来源」重建一次：` +
    `现有的推演线与分析产物会被清空；<b>人格档案与媒体库（表情包 / 图片）保留</b>；` +
    `<b>其他好友不受影响</b>。`;
  $("#impModal").classList.add("open");
  impStep(1);
  loadSources();
  // 清掉可能残留的服务端批次锁（上次上传后直接关窗的情况），避免再传时 409
  api("/api/import/cancel", { method: "POST" }).catch(() => { });
}

function closeImport() {
  impCancel(true);                       // 关窗即放弃本批，别让服务端的导入锁悬着
  $("#impModal").classList.remove("open");
}

async function impCancel(silent) {
  if (!IMP.batch) return;
  IMP.batch = null;
  try {
    await api("/api/import/cancel", { method: "POST" });
  } catch (e) {
    if (!silent) toast("释放导入会话失败：" + e.message);
  }
}

async function impResetToUpload() {
  IMP.files = []; IMP.excluded = []; IMP.sel = {}; IMP.merge = null;
  await impCancel(true);
  impStep(1);
  $("#impFile").value = "";
  loadSources();
}

async function impUpload(fileList, _retried) {
  const files = Array.from(fileList || []);
  if (!files.length || IMP.busy) return;
  if (files.length > 12) return toast("单次最多 12 份文件，请分两批导入");
  IMP.busy = true;
  const tip = $("#impDrop").querySelector("b");
  tip.textContent = `上传中：${files.length} 份 …`;
  let d = null, err = "";
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), T_UPLOAD);
  try {
    const fd = new FormData();
    files.forEach(f => fd.append("files", f, f.name));
    const r = await fetch("/api/import/upload", { method: "POST", body: fd, signal: ctl.signal });
    if (r.ok) {
      d = await r.json();
    } else {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (e) { }
      err = detail || ("HTTP " + r.status);
      // 服务端还挂着上一次没走完的批次（关向导 / 刷新页面留下的）→ 主动放掉，下面自动重试一次
      if (!_retried && /导入任务进行中/.test(err)) {
        try {
          await api("/api/import/cancel", { method: "POST" });
          err = "";
        } catch (e2) { /* 放不掉就照实报错 */ }
      }
    }
  } catch (e) {
    err = isTimeout(e) ? "上传超时（文件可能过大，或服务未响应）" : e.message;
  } finally {
    clearTimeout(timer);
  }
  IMP.busy = false;
  $("#impFile").value = "";
  tip.textContent = "拖拽文件到这里，或点击选择（可多选）";
  if (d) {
    if (d.reclaimed) toast("已放弃上一次没走完的导入，按本次重新开始");
    IMP.batch = d.batch_id;
    IMP.files = (d.files || []).map(f => ({ ...f }));
    IMP.excluded = []; IMP.sel = {};
    await impPreview();
    return;
  }
  if (err) return toast("上传失败：" + err);
  return impUpload(files, true);        // 已放掉旧批次 → 原样重试一次（不会无限循环）
}

async function impPreview() {
  if (!IMP.batch) return;
  IMP.busy = true;
  impStep(2);
  $("#impMerge").innerHTML = "正在逐份体检并试合并…";
  $("#impSrcList").innerHTML = "";
  try {
    const d = await apiTimeout("/api/import/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: IMP.batch }),
    }, T_PREVIEW);
    const items = d.items || [];
    IMP.files.forEach(f => {
      const it = items.find(x => x.import_id === f.import_id) || {};
      f.report = it.report || null;
      f.candidates = it.candidates || [];
      const sug = (f.report && (f.report.mapping_default || f.report.suggested_args)) || {};
      IMP.sel[f.import_id] = { a: sug.sender_a || "", b: sug.sender_b || "" };
    });
    IMP.merge = d.merge || null;
    renderImpMerge({ merge: d.merge, merge_error: d.merge_error });
    renderImpSources();
  } catch (e) {
    const to = isTimeout(e);
    if (to) await impCancel(true);          // 超时就把服务端的批次一起放掉，别留锁
    $("#impMerge").innerHTML = `<div class="imp-tag warn">` +
      `${to ? "预览超时 —— 已释放本次导入会话，请点「← 重新上传」重试"
           : "预览失败：" + esc(e.message)}</div>`;
    $("#impCommit").disabled = true;
    toast(to ? "预览超时，已释放本次导入" : "预览失败：" + e.message);
  } finally { IMP.busy = false; }
}

async function impRecompute() {
  const sel = IMP.files
    .filter(f => !IMP.excluded.includes(f.import_id))
    .map(f => selPayload(f.import_id))
    .filter(s => s.sender_a && s.sender_b && s.sender_a !== s.sender_b);
  if (!sel.length) {
    $("#impMerge").innerHTML = `<div class="imp-sub">先给每一份来源选好「你 / 对方」，这里会显示合并后的结果。</div>`;
    $("#impCommit").disabled = true;
    return;
  }
  try {
    const d = await apiTimeout("/api/import/merge", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: IMP.batch, sources: sel, exclude: IMP.excluded }),
    }, T_MERGE);
    IMP.merge = d.merge;
    renderImpMerge({ merge: d.merge });
  } catch (e) {
    $("#impMerge").innerHTML = `<div class="imp-tag warn">` +
      `${isTimeout(e) ? "合并预览超时" : "合并预览失败：" + esc(e.message)}</div>`;
  }
}

function renderImpMerge(d) {
  const m = d.merge;
  let h = "";
  if (d.merge_error) h += `<div class="imp-tag warn">合并预览失败：${esc(d.merge_error)}</div>`;
  if (!m) {
    h += `<div class="imp-sub">还没有可合并的来源：先给每一份选好 A/B 双方账号。</div>`;
    $("#impMerge").innerHTML = h;
    $("#impCommit").disabled = true;
    return;
  }
  const dup = m.duplicates_removed || 0;
  h += `<h3 class="ok">合并后 ${m.message_count} 条消息</h3>`;
  h += `<div class="imp-kv">`;
  h += kv("来源份数", `${m.source_count} 份（原始解析 ${m.parsed_total} 条）`);
  h += kv("跨源去重", dup ? `移除 <b>${dup}</b> 条重复消息` : "没有重复");
  if (m.session_span)
    h += kv("合并时间轴", `${fmtTs(m.session_span.first_ts)} → ${fmtTs(m.session_span.last_ts)}` +
      `（${m.session_span.days} 天）`);
  if (m.overlap)
    h += kv("来源重叠区", `${fmtTs(m.overlap.first_ts)} → ${fmtTs(m.overlap.last_ts)}` +
      `（${m.overlap.days} 天，多份来源都覆盖这段）`);
  if (m.unknown_sender)
    h += kv("无法归属", `<span class="bad">${m.unknown_sender} 条消息的发送者不在 A/B 映射里</span>`);
  h += `</div>`;
  if (m.provisional)
    h += `<div class="imp-tag warn">当前用的是体检建议的映射，改选 A/B 后会自动重算</div>`;
  if (m.map_conflicts)
    h += `<div class="imp-tag warn">⚠ 有 ${m.map_conflicts} 组消息「时间 + 内容」相同但发送者相反 —— ` +
      `很可能某份来源的 A/B 选反了（选反会让去重失效、消息翻倍），请核对上方下拉框</div>`;
  if (m.skipped && m.skipped.length)
    h += `<div class="imp-tag warn">还没选 A/B、暂未计入合并：${m.skipped.map(esc).join("、")}</div>`;
  $("#impMerge").innerHTML = h;
  $("#impCommit").disabled = !m.message_count;
}

/* 当前选定的 A / B 各自的脱敏样例：让用户靠「这是不是我说话的样子」来确认方向。
   A 选反会让整份来源的说话人颠倒，而这件事除了人眼没有可靠的自动判据。 */
function abSamplesHtml(f) {
  const cur = IMP.sel[f.import_id] || { a: "", b: "" };
  const cands = f.candidates || [];
  const pick = acc => ((cands.find(c => c.account === acc) || {}).samples) || [];
  const col = (title, acc) =>
    `<div class="imp-ab-col"><b>${title}</b><span class="imp-sub">${esc(acc || "（未选）")}</span>` +
    (pick(acc).map(s => `<div class="sample">${esc(s.text)}</div>`).join("") ||
     `<div class="sample">—</div>`) + `</div>`;
  return col("你（A）", cur.a) + col("对方（B）", cur.b);
}

function refreshAB(importId) {
  const box = $("#ab-" + importId);
  const f = IMP.files.find(x => x.import_id === importId);
  if (box && f) box.innerHTML = abSamplesHtml(f);
}

function renderImpSources() {
  const shown = IMP.files.filter(f => !IMP.excluded.includes(f.import_id));
  let h = `<h3>逐份确认<span class="imp-sub">每份来源的账号名不同，所以各自选一次「你 / 对方」</span></h3>`;
  h += `<div class="imp-tag warn" style="margin-bottom:8px">A 必须是<b>你在每一份来源里</b>的账号名。` +
    `选反会让这份来源的说话人整体颠倒（关系状态、人格档案、推演语料都会跟着错）。` +
    `下拉框的默认值只是<b>按消息条数猜的</b>，请对着下方样例逐份核对。</div>`;
  if (!shown.length) h += `<div class="imp-sub">本批文件都被移除了，点「← 重新上传」重新选。</div>`;
  h += shown.map(f => {
    const r = f.report || {};
    const cands = f.candidates || [];
    const bad = !r.recognized || !r.message_count;
    const cur = IMP.sel[f.import_id] || { a: "", b: "" };
    const opts = v => cands.map(c =>
      `<option value="${esc(c.account)}"${c.account === v ? " selected" : ""}>` +
      `${esc(c.account)}（${c.count} 条，${c.pct}%）</option>`).join("");
    let inner = "";
    if (bad) {
      inner = `<div class="imp-tag warn">✕ 认不出这份文件：` +
        `${esc((r.reasons || [])[0] || r.verdict || "格式未识别")}</div>` +
        `<div class="imp-sub">把它移出本批，其余来源照常导入。</div>`;
    } else {
      inner = `<div class="imp-kv">
        <div class="k">格式</div><div class="v">${esc(r.importer || "—")}` +
        `${r.source_encoding ? `（${esc(r.source_encoding)}）` : ""}</div>
        <div class="k">条数</div><div class="v">${r.message_count} 条有效 / 共 ${r.total_rows} 行</div>`;
      if (r.time_span)
        inner += `<div class="k">跨度</div><div class="v">${esc(r.time_span.first)} → ${esc(r.time_span.last)}</div>`;
      if (r.skipped && Object.keys(r.skipped).length)
        inner += `<div class="k">跳过</div><div class="v">` +
          Object.entries(r.skipped).map(([k, v]) => esc(k.replace("skipped_", "")) + "×" + v).join("，") + `</div>`;
      if (r.line_parse)
        inner += `<div class="k">行解析</div><div class="v">${r.line_parse.parsed} 条` +
          `（续行并入 ${r.line_parse.appended_lines} 行` +
          `${r.line_parse.skipped_no_ts ? `，跳过 ${r.line_parse.skipped_no_ts} 行` : ""}）</div>`;
      inner += `</div>`;
      if (cands.length >= 2) {
        inner += `<div class="form-row">
          <label>你（A）</label><select data-a="${f.import_id}">${opts(cur.a)}</select>
          <label>对方（B）</label><select data-b="${f.import_id}">${opts(cur.b)}</select>
        </div>`;
        const md = r.mapping_default || {};
        inner += `<div class="imp-sub">默认值来源：` +
          (md.source === "config" ? "config.yaml 里你登记过的账号名"
            : md.source === "guess" ? "<b>按消息条数猜的</b> —— 请确认 A 是你" : "—") + `</div>`;
        inner += `<div class="imp-ab" id="ab-${f.import_id}">${abSamplesHtml(f)}</div>`;
      } else {
        inner += `<div class="imp-tag warn">候选账号不足 2 个——双人对话才可导入，请检查导出范围</div>`;
      }
      if (r.reasons && r.reasons.length)
        inner += `<div class="imp-tags">` + r.reasons.slice(0, 3).map(x =>
          `<span class="imp-tag warn">${esc(x)}</span>`).join("") + `</div>`;
      inner += `<div class="imp-tags">` + cands.slice(0, 5).map(c =>
        `<span class="imp-tag">${esc(c.account)}　${c.count} 条（${c.pct}%）</span>`).join("") + `</div>`;
    }
    return `<div class="imp-card2${bad ? " bad" : ""}">
      <div class="imp-card2-head">
        <b>${esc(f.filename)}</b><span class="imp-sub">${fmtSize(f.size)}</span>
        <div class="spacer"></div>
        <button class="btn btn-sm" data-rm="${f.import_id}">移除本份</button>
      </div>${inner}</div>`;
  }).join("");
  if (IMP.excluded.length)
    h += `<div class="imp-sub">已移除 ${IMP.excluded.length} 份，不会导入。</div>`;
  $("#impSrcList").innerHTML = h;

  $$("#impSrcList select[data-a]").forEach(el => el.onchange = () => {
    const id = el.dataset.a;
    (IMP.sel[id] = IMP.sel[id] || {}).a = el.value;
    if (IMP.sel[id].a === IMP.sel[id].b) toast("「你」和「对方」不能是同一个账号");
    refreshAB(id);
    impRecompute();
  });
  $$("#impSrcList select[data-b]").forEach(el => el.onchange = () => {
    const id = el.dataset.b;
    (IMP.sel[id] = IMP.sel[id] || {}).b = el.value;
    if (IMP.sel[id].a === IMP.sel[id].b) toast("「你」和「对方」不能是同一个账号");
    refreshAB(id);
    impRecompute();
  });
  $$("#impSrcList button[data-rm]").forEach(el => el.onclick = () => {
    const id = el.dataset.rm;
    IMP.excluded.push(id);
    renderImpSources();
    impRecompute();
  });
}

async function impCommit() {
  if (IMP.busy || !IMP.batch) return;
  const chosen = IMP.files.filter(f => !IMP.excluded.includes(f.import_id));
  const sources = chosen.map(f => selPayload(f.import_id));
  const badIdx = sources.findIndex(s => !s.sender_a || !s.sender_b);
  if (badIdx >= 0) return toast(`「${chosen[badIdx].filename}」还没选「你 / 对方」`);
  if (sources.some(s => s.sender_a === s.sender_b)) return toast("「你」和「对方」不能是同一个账号");
  const gap = parseInt($("#impGap").value, 10);
  IMP.busy = true;
  $("#impCommit").disabled = true;
  impStep(3);
  $("#impResult").innerHTML = "导入中…（逐份归档 → 从全部已导入来源重建 → 脱敏 → 会话化 → 门禁）";
  try {
    const d = await apiTimeout("/api/import/commit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        batch_id: IMP.batch, sources, exclude: IMP.excluded,
        session_gap_minutes: isNaN(gap) ? null : gap,
      }),
    }, T_COMMIT);
    renderImpResult(d.summary, d.media_link, d.config_note);
    toast("导入完成");
  } catch (e) {
    const to = isTimeout(e);
    $("#impResult").innerHTML =
      `<h3 class="bad">✕ ${to ? "导入超时" : "导入失败"}</h3>` +
      `<div>${esc(to ? "等待超过 15 分钟。导入可能仍在后台继续，可先刷新页面查看结果；"
                     : e.message)}</div>` +
      `<div style="margin-top:8px">临时文件已清理；已归档的来源仍保留，可点「再导一份」重试。</div>`;
  } finally {
    IMP.busy = false;
    $("#impCommit").disabled = false;
  }
}

function renderImpResult(s, link, configNote) {
  const ok = !!s.gates_all_pass;
  const gates = s.gates || {};
  const gname = {
    G2_时间序列有序: "时间序列有序", G3_双人占比: "双人占比",
    G4_长断档告警: "长断档告警（不阻塞）", G5_脱敏残留: "脱敏残留",
    "G7_未知发送者/类型": "未知发送者/类型", G8_跨源重复: "跨源重复（不阻塞）",
  };
  let h = `<h3 class="${ok ? "ok" : "bad"}">` +
    `${ok ? "✓ 导入完成，全部门禁通过" : "导入完成，但存在未通过的门禁"}</h3>
     <div class="imp-kv">
       <div class="k">消息数</div><div class="v">${s.message_count} 条 / ${s.session_count} 个会话</div>
       <div class="k">来源</div><div class="v">${s.source_count} 份（库内共登记 ${s.registered_sources ?? s.source_count} 份）</div>
       <div class="k">时间跨度</div><div class="v">${esc(s.first_day)} → ${esc(s.last_day)}（${s.active_days} 个有消息日）</div>
       ${s.duplicates_removed ? `<div class="k">跨源去重</div><div class="v">移除 ${s.duplicates_removed} 条重复消息</div>` : ""}
       <div class="k">内容字数</div><div class="v">${s.char_count_total}</div>
       ${link && link.linked ? `<div class="k">媒体关联</div><div class="v">${link.linked} 条消息匹配到图片</div>` : ""}
     </div>
     <div class="imp-gates">` +
    Object.entries(gates).map(([k, v]) =>
      `<div class="imp-gate${v ? "" : " bad"}"><span class="${v ? "g-ok" : "g-bad"}">${v ? "✓" : "✕"}</span>${esc(gname[k] || k)}</div>`).join("") +
    `</div>`;
  if (s.sources && s.sources.length)
    h += `<div class="imp-cand"><b>本次参与合并的来源</b>` +
      s.sources.map(x => `<div class="sample">${esc(x.name)}（${esc(x.importer)}，${x.message_count} 条）</div>`).join("") +
      `</div>`;
  if (s.missing_sources && s.missing_sources.length)
    h += `<div class="imp-tag warn">这些已登记来源的存档找不到了，本次未参与：` +
      `${s.missing_sources.map(esc).join("、")}</div>`;
  if (configNote) h += `<div class="imp-tag warn">${esc(configNote)}</div>`;
  h += `<div class="imp-next">
       <span>下一步：在本机跑一遍分析（事件 / 记忆 / 关系状态 / 转折点 / 人格档案）</span>
       <label class="chk"><input type="checkbox" id="impSkipLLM"><span>离线分析</span></label>
       <button class="btn btn-primary btn-sm" id="impAnalyze">立即分析</button>
     </div>`;
  $("#impResult").innerHTML = h;
  const ia = $("#impAnalyze");
  if (ia) ia.onclick = () => {
    const skip = $("#impSkipLLM") ? $("#impSkipLLM").checked : false;
    closeImport();
    openAnalyze(skip);
  };
}

/* ---------------------------------------------------------------- 已导入来源管理 */
async function loadSources() {
  const box = $("#impSrcBox");
  if (!box) return;
  box.innerHTML = `<div class="imp-sub">读取已导入来源…</div>`;
  try {
    const d = await api("/api/import/sources");
    const src = d.sources || [];
    window.__sources = src;
    let h = `<b>已导入的来源（${src.length}）</b>`;
    if (!src.length) {
      h += `<div class="imp-sub">还没有导入过任何来源。同一段对话在不同应用里的记录，` +
        `可以分多次导入——每次都会与已有来源重新合并。</div>`;
    } else {
      h += `<div class="imp-sub">库就是这些来源合并出来的结果。移除一份会立刻按剩余来源重建一次库；` +
        `「你 / 对方」选反了就点「对调 A/B」——来源有存档，不用重新上传。</div>`;
      h += src.map(s => {
        const map = Object.entries(s.sender_map || {})
          .map(([k, v]) => `${k}→${v}`).join("，") || "—";
        const span = s.first_ts
          ? fmtTs(s.first_ts).slice(0, 10) + " → " + fmtTs(s.last_ts).slice(0, 10) : "—";
        return `<div class="src-row">
        <div class="src-main">
          <b>${esc(s.name)}</b>
          <span class="imp-sub">${esc(s.importer || "—")} · ${s.message_count} 条 · ${span} · ${fmtSize(s.size)}</span>
          <span class="imp-sub">账号映射：${esc(map)}</span>
        </div>
        <button class="btn btn-sm" data-swap="${esc(s.source_id)}">对调 A/B</button>
        <button class="btn btn-sm" data-del="${esc(s.source_id)}">移除</button>
      </div>`;
      }).join("");
    }
    const md = d.media || {};
    h += `<div class="imp-sub" style="margin-top:10px">媒体库：${md.count || 0} 个文件</div>`;
    box.innerHTML = h;
    $$("#impSrcBox button[data-swap]").forEach(el => el.onclick = async () => {
      const id = el.dataset.swap;
      const it = (window.__sources || []).find(x => x.source_id === id) || {};
      const map = Object.entries(it.sender_map || {}).map(([k, v]) => `${k}→${v}`).join("，");
      if (!confirm(`对调「${it.name || id}」的你/对方？\n\n当前：${map}\n对调后：A 与 B 互换\n\n` +
        `会立刻按全部来源重建一次库（分析产物会失效，需要重新分析；人格档案与媒体库保留）。`)) return;
      el.disabled = true;
      try {
        const d = await api("/api/import/sources/" + encodeURIComponent(id) + "/mapping",
          { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const s = d.summary || {};
        toast(`已对调，重建后 ${s.message_count} 条`);
        await loadSources();
      } catch (e) {
        toast("对调失败：" + e.message);
        el.disabled = false;
      }
    });
    $$("#impSrcBox button[data-del]").forEach(el => el.onclick = async () => {
      const id = el.dataset.del;
      const it = (window.__sources || []).find(x => x.source_id === id) || {};
      if (!confirm(`移除来源「${it.name || id}」？\n\n会把它的存档删掉，并按剩余来源重建一次库（分析产物会失效，需要重新分析）。`)) return;
      el.disabled = true;
      try {
        await api("/api/import/sources/" + encodeURIComponent(id), { method: "DELETE" });
        toast("已移除并重建");
        await loadSources();
      } catch (e) {
        toast("移除失败：" + e.message);
        el.disabled = false;
      }
    });
  } catch (e) {
    box.innerHTML = `<div class="imp-tag warn">读取已导入来源失败：${esc(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- 媒体库（表情包 / 图片） */
const MED = { busy: false };

function openMedia() {
  $("#medDrop").querySelector("b").textContent = "拖拽图片到这里，或点击选择（可多选）";
  $("#medMsg").textContent = "";
  $("#medModal").classList.add("open");
  loadMedia();
}
function closeMedia() { $("#medModal").classList.remove("open"); }

async function loadMedia() {
  const box = $("#medList");
  box.innerHTML = `<div class="imp-sub">读取媒体库…</div>`;
  try {
    const d = await apiTimeout("/api/media", null, 30000);
    const st = d.stats || {};
    $("#medStats").innerHTML = `已导入 <b>${st.count || 0}</b> 个文件` +
      (st.by_kind ? "（" + Object.entries(st.by_kind).map(([k, v]) =>
        (k === "sticker" ? "表情包" : "图片") + " " + v).join("，") + "）" : "") +
      `，占用 ${fmtSize(st.total_bytes || 0)}`;
    const items = d.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="imp-sub">媒体库是空的。导入表情包 / 图片后，` +
        `对话里对方真实用过的表情就会显示成真图，而不是占位符。</div>`;
      return;
    }
    box.innerHTML = items.map(m => `<div class="med-row">
      <img class="med-thumb" src="/api/media/${encodeURIComponent(m.filename)}" alt="" loading="lazy">
      <div class="med-main">
        <b>${esc(m.filename)}</b>
        <span class="imp-sub">${m.kind === "sticker" ? "表情包" : "图片"} · ${fmtSize(m.size)}` +
      `${m.width ? ` · ${m.width}×${m.height}` : ""} · ` +
      `${m.linked_messages ? `已关联 ${m.linked_messages} 条消息` : "暂未关联到消息"}</span>
      </div>
      <button class="btn btn-sm" data-mdel="${esc(m.media_id)}">删除</button>
    </div>`).join("");
    $$("#medList button[data-mdel]").forEach(el => el.onclick = async () => {
      if (!confirm("从媒体库删除这个文件？（不影响聊天记录）")) return;
      el.disabled = true;
      try {
        await api("/api/media/item/" + encodeURIComponent(el.dataset.mdel), { method: "DELETE" });
        await loadMedia();
      } catch (e) { toast("删除失败：" + e.message); el.disabled = false; }
    });
  } catch (e) {
    box.innerHTML = `<div class="imp-tag warn">读取媒体库失败：${esc(e.message)}</div>`;
  }
}

async function medUpload(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length || MED.busy) return;
  MED.busy = true;
  const kind = $("#medKind").value;
  $("#medMsg").textContent = `上传中：${files.length} 张 …`;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), T_UPLOAD);
  try {
    const fd = new FormData();
    files.forEach(f => fd.append("files", f, f.name));
    const r = await fetch("/api/media/import?kind=" + encodeURIComponent(kind),
      { method: "POST", body: fd, signal: ctl.signal });
    if (!r.ok) {
      let d = ""; try { d = (await r.json()).detail || ""; } catch (e) { }
      throw new Error(d || ("HTTP " + r.status));
    }
    const d = await r.json();
    $("#medMsg").textContent = `新增 ${d.added_count} 个，重复跳过 ${d.dedup_count} 个，` +
      `失败 ${d.failed_count} 个` + (d.media_link ? `；本次关联到 ${d.media_link.linked} 条消息` : "");
    if (d.failed && d.failed.length) toast("部分失败：" + d.failed[0].reason);
    await loadMedia();
  } catch (e) {
    toast(isTimeout(e) ? "导入超时（图片较多或过大）" : "导入失败：" + e.message);
    $("#medMsg").textContent = "";
  } finally {
    clearTimeout(timer);
    MED.busy = false;
    $("#medFile").value = "";
  }
}

async function medImportDir() {
  const path = $("#medDir").value.trim();
  if (!path) return toast("先填写一个目录路径");
  if (MED.busy) return;
  MED.busy = true;
  $("#medMsg").textContent = "正在导入目录…";
  try {
    const d = await apiTimeout("/api/media/import_dir", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, kind: $("#medKind").value }),
    }, T_UPLOAD);
    $("#medMsg").textContent = `新增 ${d.added_count} 个，重复跳过 ${d.dedup_count} 个，` +
      `失败 ${d.failed_count} 个` + (d.media_link ? `；本次关联到 ${d.media_link.linked} 条消息` : "");
    await loadMedia();
  } catch (e) {
    toast(isTimeout(e) ? "导入超时（目录里图片较多）" : "导入失败：" + e.message);
    $("#medMsg").textContent = "";
  } finally { MED.busy = false; }
}

/* 向导与媒体库的事件绑定 */
$("#btnImport").onclick = openImport;
$("#impClose").onclick = closeImport;
$("#impModal").onclick = e => { if (e.target.id === "impModal") closeImport(); };
$("#impBack").onclick = impResetToUpload;
$("#impAgain").onclick = impResetToUpload;
$("#impDone").onclick = closeImport;
$("#impCommit").onclick = impCommit;
$("#impDrop").onclick = () => $("#impFile").click();
$("#impFile").onchange = e => { impUpload(e.target.files); e.target.value = ""; };
$("#impDrop").ondragover = e => { e.preventDefault(); $("#impDrop").classList.add("over"); };
$("#impDrop").ondragleave = () => $("#impDrop").classList.remove("over");
$("#impDrop").ondrop = e => {
  e.preventDefault();
  $("#impDrop").classList.remove("over");
  impUpload(e.dataTransfer.files);
};

$("#btnMediaOpen").onclick = openMedia;
$("#medClose").onclick = closeMedia;
$("#medModal").onclick = e => { if (e.target.id === "medModal") closeMedia(); };
$("#medDrop").onclick = () => $("#medFile").click();
$("#medFile").onchange = e => { medUpload(e.target.files); e.target.value = ""; };
$("#medDrop").ondragover = e => { e.preventDefault(); $("#medDrop").classList.add("over"); };
$("#medDrop").ondragleave = () => $("#medDrop").classList.remove("over");
$("#medDrop").ondrop = e => {
  e.preventDefault();
  $("#medDrop").classList.remove("over");
  medUpload(e.dataTransfer.files);
};
$("#medDirBtn").onclick = medImportDir;

document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeImport(); closeMedia(); }
});

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

/* ================================================================================
   v0.3 桌面化：分析任务 / 人物档案 / LLM 设置 / 多好友 / 首次运行引导
   ---- 与服务端约定 ----
   POST /api/analyze            {skip_llm} → {job_id}
   GET  /api/analyze/status     {running, phase, message, elapsed_s, stages, last_result, error}
   POST /api/analyze/cancel     中止（阶段之间生效）
   GET  /api/persona            只读档案（persona 四层 + 关系五维 + 转折点 + 统计）
   GET/PUT /api/settings        设置读写（永远不回传完整 key）
   POST /api/settings/test      最小一次补全验证连通性（超时 10s）
   GET/POST/DELETE /api/profiles + switch / rename
   GET  /api/onboarding, POST /api/onboarding/demo
   ================================================================================ */
const V3 = {
  profiles: [], active: "", busy: "", analyze: null, timer: null,
  frdMode: "new", frdId: "", onboarded: false,
};

function busyReason() {
  if (V3.busy) return V3.busy;
  if (V3.analyze && V3.analyze.running) return "分析正在进行中";
  return "";
}

/* 分析/导入进行中：相关入口一律置灰并说明原因 */
function syncActionButtons() {
  const why = busyReason();
  const tip = why ? `（${why}，暂时不可用）` : "";
  const pairs = [
    ["#btnImport", "导入聊天记录（本地，不上传任何远端）"],
    ["#btnAnalyze", "在本机分析聊天记录（事件/记忆/关系/转折点/人格）"],
    ["#btnDay", "推进一天"],
  ];
  for (const [sel, base] of pairs) {
    const el = $(sel);
    if (!el) continue;
    if (sel === "#btnDay") {
      el.disabled = (S.busy || !S.line) || !!why;   // 基础态来自 updateBusy 的判定
      el.title = "推进一天" + tip;
      continue;
    }
    el.disabled = !!why;
    el.title = base + tip;
  }
  const nf = $("#fbNewBtn");
  if (nf) nf.disabled = !!why;
}

/* ---------------------------------------------------------------- 好友 */
function friendById(id) { return V3.profiles.find(p => p.id === id) || null; }
V3.curName = function () {
  if (V3.active && friendById(V3.active)) return friendById(V3.active).name;
  const st = friendById("");
  return st ? st.name : "当前数据目录";
};

function renderFriends() {
  $("#fbCur").textContent = V3.curName();
  const box = $("#fbList");
  box.innerHTML = "";
  if (!V3.profiles.length) {
    box.innerHTML = '<div class="fb-empty">还没有好友档案。</div>';
  }
  for (const p of V3.profiles) {
    const el = document.createElement("div");
    const active = p.active || (!V3.active && p.synthetic);
    el.className = "fb-item" + (active ? " active" : "") + (p.synthetic ? " locked" : "");
    el.dataset.id = p.id;
    const tag = p.synthetic ? '<span class="fb-tag">当前目录</span>'
      : (p.has_db ? "" : '<span class="fb-tag">无数据</span>');
    el.innerHTML =
      `<span class="fb-nm" title="数据目录：${esc(p.dir || "")}">${esc(p.name)}</span>${tag}` +
      (p.synthetic ? "" :
        `<span class="fb-ops">
           <button class="fb-op" data-op="rename" title="重命名">✎</button>
           <button class="fb-op del" data-op="del" title="删除该好友及其全部数据">×</button>
         </span>`);
    el.onclick = e => {
      const op = e.target.dataset ? e.target.dataset.op : "";
      if (op === "rename") { e.stopPropagation(); openFriendModal("rename", p); return; }
      if (op === "del") { e.stopPropagation(); delFriend(p); return; }
      if (!active) switchFriend(p.id);
    };
    box.appendChild(el);
  }
  const acts = document.createElement("div");
  acts.className = "fb-acts";
  acts.innerHTML = '<button class="btn btn-sm" id="fbNewBtn">＋ 新建好友</button>';
  box.appendChild(acts);
  $("#fbNewBtn").onclick = () => openFriendModal("new");
  /* 有多个好友时默认展开列表；用户手动开合过就尊重用户的选择 */
  if (!V3.fbTouched && V3.profiles.length > 1) {
    $("#fbBody").classList.add("open");
    $("#fbCaret").textContent = "▴";
  }
  syncActionButtons();
}

async function loadProfiles() {
  try {
    const d = await api("/api/profiles");
    V3.profiles = d.profiles || [];
    V3.active = d.active || "";
    V3.busy = d.busy || V3.busy;
    if (d.busy) V3.busy = d.busy;
    renderFriends();
  } catch (e) { /* 好友列表失败不阻塞主流程 */ }
}

async function switchFriend(id) {
  if (busyReason()) return toast("暂时不能切换好友：" + busyReason());
  try {
    toast("正在切换好友…");
    const d = await api("/api/profiles/switch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    V3.profiles = d.profiles || []; V3.active = d.active || "";
    S.anchors = null;                       // 换了好友：时间轴等缓存全部作废
    renderFriends();
    await refresh();
    toast(`已切换到「${V3.curName()}」`);
  } catch (e) { toast("切换失败：" + e.message); }
}

async function delFriend(p) {
  const ok = confirm(
    `删除好友「${p.name}」？\n\n` +
    `• 该好友的全部数据将被删除：聊天库、人格档案、表情包缓存、所有对话线\n` +
    `• 数据目录：${p.dir}\n` +
    `• 其他好友不受影响\n\n此操作不可撤销。`);
  if (!ok) return;
  try {
    const d = await api("/api/profiles/" + encodeURIComponent(p.id), { method: "DELETE" });
    V3.profiles = d.profiles || []; V3.active = d.active || "";
    S.anchors = null;
    renderFriends();
    await refresh();
    toast("已删除该好友");
  } catch (e) { toast("删除失败：" + e.message); }
}

function openFriendModal(mode, p) {
  V3.frdMode = mode;
  V3.frdId = p ? p.id : "";
  $("#frdTitle").textContent = mode === "rename" ? "重命名好友" : "新建好友";
  $("#frdName").value = p ? p.name : "";
  $("#frdHint").innerHTML = mode === "rename"
    ? "只改显示名，数据目录不变。"
    : "每个好友拥有完全独立的数据目录（库、人格档案、表情包、对话线），互不可见。";
  $("#frdModal").classList.add("open");
  setTimeout(() => $("#frdName").focus(), 30);
}

async function submitFriend() {
  const name = $("#frdName").value.trim();
  if (!name) return toast("先给好友起个名字");
  try {
    if (V3.frdMode === "rename") {
      const d = await api(`/api/profiles/${encodeURIComponent(V3.frdId)}/rename`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      V3.profiles = d.profiles || [];
      toast("已重命名");
    } else {
      const d = await api("/api/profiles", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      V3.profiles = d.profiles || [];
      toast(`已创建「${name}」，正在切换…`);
      $("#frdModal").classList.remove("open");
      await switchFriend(d.profile.id);
      return;
    }
    $("#frdModal").classList.remove("open");
    renderFriends();
  } catch (e) { toast("操作失败：" + e.message); }
}

/* ---------------------------------------------------------------- 分析 */
function anaStep(n) {
  $("#ana-pane1").classList.toggle("active", n === 1);
  $("#ana-pane2").classList.toggle("active", n === 2);
}

function openAnalyze(skipLLM) {
  if (busyReason() === "导入正在进行中") return toast("导入正在进行中，稍后再分析");
  $("#anaSkip").checked = !!skipLLM;
  renderAnaHint();
  $("#anaWho").textContent = `将在「${V3.curName()}」的数据目录内分析；产物只写在本机。`;
  anaStep(V3.analyze && V3.analyze.running ? 2 : 1);
  $("#anaModal").classList.add("open");
  if (V3.analyze && V3.analyze.running) renderAnaProgress(V3.analyze);
}

function renderAnaHint() {
  const skip = $("#anaSkip").checked;
  $("#anaSkipHint").innerHTML = skip
    ? "离线路径：关键词启发式判定事件 + persona 空模板。<b>零 token 成本</b>，结果较粗，之后可随时重跑。"
    : "LLM 路径：逐会话抽取事件并生成人格档案，效果最好，<b>会消耗 token</b>（只发送脱敏后的文本）。";
}

async function startAnalyze() {
  const skip = $("#anaSkip").checked;
  anaStep(2);
  $("#anaMsg").textContent = "正在启动分析…";
  $("#anaCancel").style.display = "";
  $("#anaFinish").disabled = true;
  $("#anaStages").innerHTML = "";
  try {
    await api("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skip_llm: skip }),
    });
    await tick();
    startPolling();
  } catch (e) {
    toast("无法开始分析：" + e.message);
    anaStep(1);
  }
}

async function cancelAnalyze() {
  try {
    const d = await api("/api/analyze/cancel", { method: "POST" });
    toast(d.message || "已请求中止");
    $("#anaMsg").textContent = d.message || "正在中止…";
  } catch (e) { toast("取消失败：" + e.message); }
}

function renderAnaStages(st) {
  const box = $("#anaStages");
  if (!box.children.length) {
    box.innerHTML = (st.stages || []).map(s =>
      `<li data-code="${esc(s.code)}"><i></i><span>${esc(s.label)}</span></li>`).join("");
  }
}

function renderAnaProgress(st) {
  renderAnaStages(st);
  const order = (st.stages || []).map(s => s.code);
  const cur = String(st.phase || "");
  const curIdx = order.indexOf(cur);
  const doneUpTo = st.running ? curIdx - 1 : curIdx;
  $$("#anaStages li").forEach(li => {
    const i = order.indexOf(li.dataset.code);
    li.classList.toggle("done", i <= doneUpTo);
    li.classList.toggle("on", st.running && i === curIdx);
  });
  $("#anaPhase").textContent = st.running
    ? (cur ? `阶段 ${cur} · ${(st.stages || []).map(s => s.label)[curIdx] || ""}` : "准备中…")
    : "已结束";
  $("#anaElapsed").textContent = (st.elapsed_s || 0).toFixed(1) + "s";
  if (st.running) $("#anaMsg").textContent = st.message || "处理中…";
}

function renderAnaResult(st) {
  const box = $("#anaMsg");
  box.className = "ana-msg";
  if (st.error) {
    box.innerHTML = `<div class="ana-result"><h3 class="bad">✕ 分析未完成</h3>
      <div>${esc(st.error)}</div>
      <div class="ana-note">常见原因：数据库还没有消息（先去「导入记录」）；或 LLM 不可用（可改用「离线分析」）。</div></div>`;
  } else if (st.cancelled) {
    box.innerHTML = `<div class="ana-result"><h3 class="bad">■ 已中止</h3>
      <div>分析产物可能不完整，重新分析即可覆盖。</div></div>`;
  } else {
    const r = st.last_result || {};
    const kv = (k, v) => `<div class="k">${k}</div><div class="v">${v}</div>`;
    box.innerHTML = `<div class="ana-result"><h3 class="ok">✓ 分析完成（${(st.elapsed_s || 0).toFixed(1)}s）</h3>
      <div class="imp-kv">
        ${kv("事件", `${r.events ?? 0} 条`)}
        ${kv("记忆", `${r.facts ?? 0} 条`)}
        ${kv("关系状态", `${r.rel_months ?? 0} 个月`)}
        ${kv("转折点", `${r.turning_points ?? 0} 个`)}
        ${kv("人格档案", r.persona === "llm" ? "LLM 生成" : "手写模板（可自行填充）")}
      </div>
      <div class="form-row" style="margin-top:10px">
        <button class="btn btn-sm" id="anaSeePersona">查看人物档案</button>
      </div></div>`;
    const b = $("#anaSeePersona");
    if (b) b.onclick = () => { closeAna(); openPersona(); };
  }
  $("#anaCancel").style.display = "none";
  $("#anaFinish").disabled = false;
}

function closeAna() { $("#anaModal").classList.remove("open"); }

function onAnalyzeFinished(st) {
  renderAnaProgress(st);
  renderAnaResult(st);
  syncActionButtons();
  if (st.error) { toast("分析未完成：" + st.error.slice(0, 80)); return; }
  if (st.cancelled) { toast("分析已中止"); return; }
  toast("分析完成");
  loadProfiles();
  refresh();                                   // 关系状态/时间轴数据已变
}

/* 轮询：一份状态给进度面板，一份健康检查给按钮置灰 */
function startPolling() {
  if (V3.timer) return;
  V3.timer = setInterval(tick, 2000);
}

async function tick() {
  try {
    const st = await api("/api/analyze/status");
    const was = !!(V3.analyze && V3.analyze.running);
    V3.analyze = st;
    if ($("#anaModal").classList.contains("open") && (st.running || was)) {
      renderAnaProgress(st);
      if (st.running) $("#anaMsg").textContent = st.message || "处理中…";
    }
    if (was && !st.running) onAnalyzeFinished(st);
    if (!was && st.running && $("#anaModal").classList.contains("open")) renderAnaProgress(st);
  } catch (e) { /* 忽略瞬时错误 */ }
  try {
    const h = await api("/api/health");
    V3.busy = h.busy || "";
    if (h.profile && !V3.active) V3.active = h.profile;
  } catch (e) { }
  syncActionButtons();
}

/* ---------------------------------------------------------------- 人物档案 */
async function openPersona() {
  $("#perModal").classList.add("open");
  $("#perBody").innerHTML = "加载中…";
  try {
    const d = await api("/api/persona");
    renderPersona(d);
  } catch (e) {
    $("#perBody").innerHTML = `<div class="per-empty">读取失败：${esc(e.message)}</div>`;
  }
}

const LAYER_KEYS = ["L", "M", "S", "U"];

function renderPersona(d) {
  $("#perWho").textContent = "· " + V3.curName();
  const st = d.stats || {};
  const meta = d.layer_meta || {};
  let h = "";

  h += `<div class="per-sec-title">数据概览 <small>来自当前好友的数据目录</small></div>
    <div class="per-stats">
      <div class="per-stat"><b>${st.n_messages ?? 0}</b><span>真实消息</span></div>
      <div class="per-stat"><b>${st.n_events ?? 0}</b><span>抽取事件</span></div>
      <div class="per-stat"><b>${st.n_facts ?? 0}</b><span>记忆条目</span></div>
      <div class="per-stat"><b>${st.rel_months ?? 0}</b><span>关系状态月数</span></div>
      <div class="per-stat"><b>${st.n_turning_points ?? 0}</b><span>转折点</span></div>
      <div class="per-stat"><b>${esc(st.first_day || "—")}</b><span>最早消息</span></div>
      <div class="per-stat"><b>${esc(st.last_day || "—")}</b><span>最晚消息</span></div>
    </div>`;

  if (d.empty_templates && d.empty_templates.length) {
    h += `<div class="ana-note">人格档案 ${d.empty_templates.join(" / ")} 还是空模板：
      编辑 <code>${esc(d.persona_dir)}/persona_v1_${esc(d.empty_templates[0])}.json</code>
      填写各层条目，或改用 LLM 路径重新分析。</div>`;
  }

  h += `<div class="per-sec-title">人格档案 <small>L/M/S/U 四层 · 由记录推导，可手写校准</small></div>
    <div class="per-grid">`;
  for (const [person, p] of Object.entries(d.people || {})) {
    h += `<div class="per-person"><header>
        <b>${esc(p.display_name || person)}</b>
        <span class="who">${person === "A" ? "你（用户本人）" : "对方（数字人格）"}</span>
        ${p.has_file ? "" : '<span class="fb-tag">文件缺失</span>'}
      </header>`;
    for (const lk of LAYER_KEYS) {
      const m = meta[lk] || {};
      const items = (p.layers || {})[lk] || [];
      h += `<div class="per-layer">
        <div class="per-lh">${esc(m.label || lk)}<span class="tone">${esc(lk)}层 · ${esc(m.tone || "")}</span>
          <span class="hint">${esc(m.hint || "")}</span></div>`;
      h += items.length
        ? items.map(it => `<div class="per-li"><span class="lb">${esc(it.label || "·")}</span><span>${esc(it.item)}</span></div>`).join("")
        : `<div class="per-empty">未填写</div>`;
      h += `</div>`;
    }
    h += `</div>`;
  }
  h += `</div>`;

  const rc = d.rel_current;
  h += `<div class="per-sec-title">关系状态（五维估计量） <small>模拟估计量 · 非事实</small></div>`;
  if (rc) {
    h += `<div class="per-rel">`;
    for (const [k, label] of DIMS) {
      const v = rc[k] == null ? 0 : rc[k];
      h += `<div class="per-rel-row"><span>${label}</span>
        <span class="bar"><i style="width:${Math.min(100, v * 10)}%"></i></span><b>${v}</b></div>`;
    }
    h += `<div class="ana-note">截至 ${esc(rc.period)}（置信度 ${rc.confidence ?? "—"}）</div></div>`;
    const trend = (d.rel || []).slice(-14);
    if (trend.length) {
      h += `<div class="per-trend"><table class="per-tb"><thead><tr>
        <th>月份</th><th>亲密</th><th>冲突</th><th>信任</th><th>情绪安全</th><th>沟通</th></tr></thead><tbody>`;
      for (const r of trend) {
        h += `<tr><td>${esc(r.period)}</td><td>${r.closeness}</td><td>${r.conflict}</td>
          <td>${r.trust}</td><td>${r.emotional_safety}</td><td>${r.comm_quality}</td></tr>`;
      }
      h += `</tbody></table></div>`;
    }
  } else {
    h += `<div class="per-empty">还没有关系状态数据，先跑一次「分析」。</div>`;
  }

  h += `<div class="per-sec-title">转折点 <small>断联窗口 / 重要事件 / 活跃高峰</small></div>`;
  const tps = d.turning_points || [];
  h += tps.length
    ? tps.map(t => `<div class="per-tp"><div class="hd">
         <span class="day">${esc(t.day || "")}</span>
         <span class="tl-b ${esc(t.tone || "plain")}">${esc(t.type_label || "")}</span>
         <span>${esc(t.title || "")}</span></div>
         <div class="ds">${esc(t.desc || "")}</div></div>`).join("")
    : `<div class="per-empty">还没有转折点数据，先跑一次「分析」。</div>`;

  $("#perBody").innerHTML = h;
}

/* ---------------------------------------------------------------- LLM 设置 */
function setRes(kind, text) {
  const el = $("#setResult");
  el.className = "set-result on" + (kind ? " " + kind : "");
  el.textContent = text || "";
}

function applySettingsView(d) {
  if (d.providers) {
    $("#setProvider").innerHTML = d.providers.map(p =>
      `<option value="${esc(p.value)}">${esc(p.label)}</option>`).join("");
  }
  if (d.provider) $("#setProvider").value = d.provider;
  $("#setBaseUrl").value = d.base_url || "";
  $("#setModel").value = d.model || "";
  $("#setKey").placeholder = d.key_state === "set"
    ? `已保存：${d.key_hint}（留空 = 不修改）`
    : "尚未设置（保存后写入系统凭据管理器）";
  const backend = { credential: "Windows 凭据管理器", "dpapi-file": "DPAPI 加密文件" }[d.key_backend]
    || "（未保存）";
  $("#setKeyState").innerHTML =
    `密钥状态：<b>${d.key_state === "set" ? "已设置" : "未设置"}</b>` +
    (d.key_state === "set" ? `　存放位置：${backend}` : "") +
    `　环境变量：<code>${esc(d.api_key_env || "")}</code>` +
    (d.keyring_available === false ? "　（keyring 不可用，已回退 DPAPI 文件）" : "");
}

async function openSettings() {
  $("#setModal").classList.add("open");
  setRes("", "");
  try {
    applySettingsView(await api("/api/settings"));
  } catch (e) { setRes("bad", "读取设置失败：" + e.message); }
}

async function saveSettings(andTest) {
  const body = {
    provider: $("#setProvider").value,
    base_url: $("#setBaseUrl").value.trim(),
    model: $("#setModel").value.trim(),
  };
  const key = $("#setKey").value;
  if (key) body.api_key = key;
  setRes("wait", "保存中…");
  try {
    const d = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("#setKey").value = "";
    applySettingsView(d);
    if (andTest) { await testSettings(); return; }
    setRes("ok", "✓ 已保存" + (d.message ? "；" + d.message : ""));
    toast("设置已保存");
  } catch (e) { setRes("bad", "✕ " + e.message); }
}

async function testSettings() {
  setRes("wait", "正在用当前配置发一次最小请求（最多 10 秒）…");
  try {
    const d = await api("/api/settings/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    setRes(d.ok ? "ok" : "bad",
      (d.ok ? "✓ " : "✕ ") + (d.message || "") +
      (d.latency_ms != null ? `　（${d.latency_ms} ms）` : ""));
  } catch (e) { setRes("bad", "✕ 测试失败：" + e.message); }
}

async function clearKey() {
  if (!confirm("清除已保存的 API Key？清除后需要重新填写才能发消息。")) return;
  try {
    const d = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: "" }),
    });
    applySettingsView(d);
    setRes("ok", "✓ " + (d.message || "已清除"));
    toast("已清除密钥");
  } catch (e) { setRes("bad", "✕ " + e.message); }
}

/* ---------------------------------------------------------------- 首次运行引导 */
async function checkOnboarding() {
  if (sessionStorage.getItem("ifwe_onb_skip") === "1") return;
  try {
    const d = await api("/api/onboarding");
    V3.onboarded = true;
    if (!d.fresh) return;
    $("#onbHint").innerHTML = d.demo_ready
      ? "示例数据已就绪，可直接进入。"
      : "不会导入任何真实数据；示例为纯虚构对话。";
    $("#onb").classList.add("open");
  } catch (e) { }
}

async function doDemo() {
  const opt = $('.onb-opt[data-act="demo"]');
  opt.classList.add("busy");
  $("#onbHint").textContent = "正在构建示例库（导入 → 离线分析 → 装载示例人格）…";
  try {
    const d = await api("/api/onboarding/demo", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    $("#onb").classList.remove("open");
    await loadProfiles();
    S.anchors = null;
    await refresh();
    const s = d.summary || {};
    toast(`示例好友已就绪：${s.message_count ?? 0} 条虚构消息`);
  } catch (e) {
    $("#onbHint").textContent = "构建失败：" + e.message;
  } finally {
    opt.classList.remove("busy");
  }
}

/* ---------------------------------------------------------------- 事件绑定 */
$("#fbHead").onclick = () => {
  const b = $("#fbBody");
  const open = b.classList.toggle("open");
  $("#fbCaret").textContent = open ? "▴" : "▾";
  V3.fbTouched = true;
};
$("#btnAnalyze").onclick = () => openAnalyze(false);
$("#btnPersona").onclick = openPersona;
$("#btnSettings").onclick = openSettings;
$("#anaClose").onclick = closeAna;
$("#anaModal").onclick = e => { if (e.target.id === "anaModal") closeAna(); };
$("#anaSkip").onchange = renderAnaHint;
$("#anaStart").onclick = startAnalyze;
$("#anaCancel").onclick = cancelAnalyze;
$("#anaFinish").onclick = () => { closeAna(); refresh(); };
$("#perClose").onclick = () => $("#perModal").classList.remove("open");
$("#perModal").onclick = e => { if (e.target.id === "perModal") $("#perModal").classList.remove("open"); };
$("#setClose").onclick = () => $("#setModal").classList.remove("open");
$("#setModal").onclick = e => { if (e.target.id === "setModal") $("#setModal").classList.remove("open"); };
$("#setSave").onclick = () => saveSettings(false);
$("#setTest").onclick = () => saveSettings(true);
$("#setClear").onclick = clearKey;
$("#frdClose").onclick = () => $("#frdModal").classList.remove("open");
$("#frdModal").onclick = e => { if (e.target.id === "frdModal") $("#frdModal").classList.remove("open"); };
$("#frdOK").onclick = submitFriend;
$("#frdName").addEventListener("keydown", e => { if (e.key === "Enter") submitFriend(); });
$("#onbSkip").onclick = () => { sessionStorage.setItem("ifwe_onb_skip", "1"); $("#onb").classList.remove("open"); };
$$(".onb-opt").forEach(o => o.onclick = () => {
  const act = o.dataset.act;
  if (act === "demo") doDemo();
  else if (act === "import") { $("#onb").classList.remove("open"); openImport(); }
  else if (act === "settings") { $("#onb").classList.remove("open"); openSettings(); }
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  ["#anaModal", "#perModal", "#setModal", "#frdModal"].forEach(s => $(s).classList.remove("open"));
});

/* ---------------------------------------------------------------- 启动 */
(async () => {
  try {
    const h = await api("/api/health");
    V3.busy = h.busy || "";
    if (!h.key) toast("未检测到 LLM API Key：可以浏览与分析（离线），发消息需要在「设置」中配置");
  } catch (e) { }
  await loadProfiles();
  try { await refresh(); } catch (e) { toast("加载失败：" + e.message); }
  startPolling();
  tick();
  checkOnboarding();
  if (location.hash === "#new") openModal();
  if (location.hash === "#new-past") { openModal(); switchPane("past"); }
})();
