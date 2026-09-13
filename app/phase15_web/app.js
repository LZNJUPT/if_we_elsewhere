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

/* ---------------------------------------------------------------- 导入向导（v0.2 O-5f） */
const IMP = { id: null, report: null, candidates: [], busy: false };

function impStep(n) {
  for (let i = 1; i <= 3; i++) {
    $(`#imp-pane${i}`).classList.toggle("active", i === n);
    const s = $(`#impS${i}`);
    s.classList.toggle("on", i === n);
    s.classList.toggle("done", i < n);
  }
}

function openImport() {
  IMP.id = null; IMP.report = null; IMP.candidates = [];
  const nm = V3.curName();
  $("#impTarget").innerHTML = `⚠ 导入会全量重建<b>当前好友「${esc(nm)}」</b>的数据库：` +
    `他的推演线与分析产物会被清空（persona 文件保留）；<b>其他好友不受影响</b>。`;
  $("#impModal").classList.add("open");
  impStep(1);
}

function closeImport() { $("#impModal").classList.remove("open"); }

function impResetToUpload() { IMP.id = null; impStep(1); }

async function impUpload(file) {
  if (IMP.busy) return;
  IMP.busy = true;
  $("#impDrop").querySelector("b").textContent = `上传中：${file.name} …`;
  try {
    const r = await fetch("/api/import/upload?filename=" + encodeURIComponent(file.name),
      { method: "POST", body: file });
    if (!r.ok) {
      let d = ""; try { d = (await r.json()).detail || ""; } catch (e) { }
      throw new Error(d || ("HTTP " + r.status));
    }
    const d = await r.json();
    IMP.id = d.import_id;
    await impPreview();
  } catch (e) {
    toast("上传失败：" + e.message);
    $("#impDrop").querySelector("b").textContent = "拖拽文件到这里，或点击选择";
  } finally { IMP.busy = false; }
}

async function impPreview() {
  if (!IMP.id) return;
  IMP.busy = true;
  impStep(2);
  $("#impReport").innerHTML = "分析中…";
  try {
    const d = await api("/api/import/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ import_id: IMP.id }),
    });
    IMP.report = d.report;
    IMP.candidates = d.candidates || [];
    renderImpReport(d);
  } catch (e) {
    $("#impReport").innerHTML = `<h3 class="bad">预览失败</h3><div>${esc(e.message)}</div>`;
    toast("预览失败：" + e.message);
  } finally { IMP.busy = false; }
}

function renderImpReport(d) {
  const r = d.report;
  const ok = !!r.importable;
  let h = `<h3 class="${ok ? "ok" : "bad"}">${ok ? "✓ " : "✕ "}${esc(r.verdict || "无法识别格式")}</h3>`;
  if (!r.recognized) {
    h += `<div>这个文件我们认不出来。可能的原因：</div>
          <ul>${(r.reasons || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
          <div>可尝试的格式与来源见第一步的说明。</div>`;
    $("#impReport").innerHTML = h;
    $("#impCommit").disabled = true;
    $("#impPickRow").style.display = "none";
    return;
  }
  const kv = (k, v) => `<div class="k">${k}</div><div class="v">${v}</div>`;
  h += `<div class="imp-kv">`;
  h += kv("格式", esc(r.importer) + (r.source_encoding ? `（${esc(r.source_encoding)}）` : ""));
  h += kv("消息数", `${r.message_count} 条有效 / 共 ${r.total_rows} 行`);
  if (r.time_span) h += kv("时间跨度", `${esc(r.time_span.first)} → ${esc(r.time_span.last)}（${r.time_span.days} 天 / ${r.time_span.active_days} 个有消息日）`);
  h += kv("消息类型", Object.entries(r.type_dist || {}).map(([k, v]) => esc(k) + "×" + v).join("，") || "—");
  if (r.skipped && Object.keys(r.skipped).length)
    h += kv("跳过", Object.entries(r.skipped).map(([k, v]) => esc(k.replace("skipped_", "")) + "×" + v).join("，"));
  const pe = r.privacy_estimate;
  if (pe) h += kv("脱敏预估", `${pe.messages_with_hits} 条命中隐私模式` +
    (Object.keys(pe.by_category || {}).length
      ? "（" + Object.entries(pe.by_category).map(([k, v]) => esc(k) + "×" + v).join("，") + "）" : ""));
  h += `</div>`;
  if (r.reasons && r.reasons.length)
    h += `<div class="imp-tags">${r.reasons.map(x => `<span class="imp-tag warn">${esc(x)}</span>`).join("")}</div>`;

  // A/B 候选 + 样例
  if (IMP.candidates.length) {
    $("#impSelA").innerHTML = IMP.candidates.map(c =>
      `<option value="${esc(c.account)}">${esc(c.account)}（${c.count} 条，${c.pct}%）</option>`).join("");
    $("#impSelB").innerHTML = $("#impSelA").innerHTML;
    $("#impSelA").selectedIndex = 0;
    $("#impSelB").selectedIndex = Math.min(1, IMP.candidates.length - 1);
    h += IMP.candidates.slice(0, 4).map(c =>
      `<div class="imp-cand"><b>${esc(c.account)}</b>　${c.count} 条（${c.pct}%）` +
      (c.samples || []).map(s => `<div class="sample">${esc(s.text)}</div>`).join("") +
      `</div>`).join("");
    $("#impPickRow").style.display = IMP.candidates.length >= 2 ? "flex" : "none";
    $("#impCommit").disabled = !(ok && IMP.candidates.length >= 2);
    if (IMP.candidates.length < 2)
      h += `<div class="imp-tag warn">候选账号不足 2 个——双人对话才可导入，请检查导出范围</div>`;
  } else {
    $("#impPickRow").style.display = "none";
    $("#impCommit").disabled = true;
  }
  $("#impReport").innerHTML = h;
}

async function impCommit() {
  if (IMP.busy || !IMP.id) return;
  const a = $("#impSelA").value, b = $("#impSelB").value;
  if (!a || !b || a === b) return toast("请选择两个不同的账号分别作为你（A）与对方（B）");
  const gap = parseInt($("#impGap").value, 10);
  IMP.busy = true;
  $("#impCommit").disabled = true;
  impStep(3);
  $("#impResult").innerHTML = "导入中…（脱敏 → 会话化 → 入库 → 质量门禁）";
  try {
    const d = await api("/api/import/commit", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ import_id: IMP.id, sender_a: a, sender_b: b,
        session_gap_minutes: isNaN(gap) ? null : gap }),
    });
    renderImpResult(d.summary);
    toast("导入完成");
  } catch (e) {
    $("#impResult").innerHTML =
      `<h3 class="bad">✕ 导入失败</h3><div>${esc(e.message)}</div>` +
      `<div style="margin-top:8px">临时文件已清理，可点击「再导一份」重新开始。</div>`;
  } finally {
    IMP.busy = false;
    $("#impCommit").disabled = false;
  }
}

function renderImpResult(s) {
  const ok = !!s.gates_all_pass;
  const gates = s.gates || {};
  const gname = { G2_时间序列有序: "时间序列有序", G3_双人占比: "双人占比",
    G4_长断档告警: "长断档告警（不阻塞）", G5_脱敏残留: "脱敏残留", "G7_未知发送者/类型": "未知发送者/类型" };
  $("#impResult").innerHTML =
    `<h3 class="${ok ? "ok" : "bad"}">${ok ? "✓ 导入完成，全部门禁通过" : "导入完成，但存在未通过的门禁"}</h3>
     <div class="imp-kv">
       <div class="k">消息数</div><div class="v">${s.message_count} 条 / ${s.session_count} 个会话</div>
       <div class="k">时间跨度</div><div class="v">${esc(s.first_day)} → ${esc(s.last_day)}（${s.active_days} 个有消息日）</div>
       <div class="k">内容字数</div><div class="v">${s.char_count_total}</div>
     </div>
     <div class="imp-gates">` +
    Object.entries(gates).map(([k, v]) =>
      `<div class="imp-gate${v ? "" : " bad"}"><span class="${v ? "g-ok" : "g-bad"}">${v ? "✓" : "✕"}</span>${esc(gname[k] || k)}</div>`).join("") +
    `</div>
     <div class="imp-next">
       <span>下一步：在本机跑一遍分析（事件 / 记忆 / 关系状态 / 转折点 / 人格档案）</span>
       <label class="chk"><input type="checkbox" id="impSkipLLM"><span>离线分析</span></label>
       <button class="btn btn-primary btn-sm" id="impAnalyze">立即分析</button>
     </div>`;
  const ia = $("#impAnalyze");
  if (ia) ia.onclick = () => {
    const skip = $("#impSkipLLM") ? $("#impSkipLLM").checked : false;
    closeImport();
    openAnalyze(skip);
  };
}

/* 向导事件绑定 */
$("#btnImport").onclick = openImport;
$("#impClose").onclick = closeImport;
$("#impModal").onclick = e => { if (e.target.id === "impModal") closeImport(); };
$("#impBack").onclick = impResetToUpload;
$("#impAgain").onclick = impResetToUpload;
$("#impDone").onclick = closeImport;
$("#impCommit").onclick = impCommit;
$("#impDrop").onclick = () => $("#impFile").click();
$("#impFile").onchange = e => { const f = e.target.files[0]; if (f) impUpload(f); e.target.value = ""; };
$("#impDrop").ondragover = e => { e.preventDefault(); $("#impDrop").classList.add("over"); };
$("#impDrop").ondragleave = () => $("#impDrop").classList.remove("over");
$("#impDrop").ondrop = e => {
  e.preventDefault();
  $("#impDrop").classList.remove("over");
  const f = e.dataTransfer.files[0];
  if (f) impUpload(f);
};
document.addEventListener("keydown", e => { if (e.key === "Escape") closeImport(); });

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
