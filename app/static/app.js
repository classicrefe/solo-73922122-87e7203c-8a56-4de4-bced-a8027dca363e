"use strict";

/* ---------- 内置示例（与 examples/ 目录一致） ---------- */
const EXAMPLES = [
  {
    name: "01 自动迁移（唯一落点）",
    original: "语料工程师修订OCR文本",
    revised: "语料工程师修订OCR文本内容",
    labels: { labels: [{ id: "role", start: 0, end: 5 }, { id: "task", start: 7, end: 10 }] }
  },
  {
    name: "02 重复文字 · 多落点需选择",
    original: "abcabcabc",
    revised: "abcabc",
    labels: { labels: [{ id: "rep", start: 3, end: 6 }] }
  },
  {
    name: "03 无法保留（字符被替换）",
    original: "中国人民银行",
    revised: "中国人民银号",
    labels: { labels: [{ id: "bank", start: 4, end: 6 }] }
  },
  {
    name: "04 重复文字 · 双标签互相冲突",
    original: "abab",
    revised: "ab",
    labels: { labels: [{ id: "x", start: 0, end: 2 }, { id: "y", start: 2, end: 4 }] }
  }
];

/* ---------- 全局状态 ---------- */
const state = {
  sessionId: null,
  revision: 0,
  analysis: null,
  selections: {},   // label_id -> {choice:'position',position} | {choice:'discard'}
  confirmed: false,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);            // 按码点切分，避免代理对错位
const esc = (s) => s.replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- 提示条 ---------- */
function showBanner(msg, isError = false) {
  const b = $("banner");
  b.textContent = msg;
  b.className = "banner" + (isError ? " error" : "");
}
function hideBanner() { $("banner").className = "banner hidden"; }

/* ---------- API ---------- */
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!resp.ok) {
    const err = new Error("HTTP " + resp.status);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function detailMessage(data) {
  if (!data || data.detail == null) return "未知错误";
  if (typeof data.detail === "string") return data.detail;
  if (data.detail.message) return data.detail.message;
  return JSON.stringify(data.detail, null, 2);
}

/* ---------- 输入区 ---------- */
function updateCounts() {
  $("original-count").textContent = cps($("original").value).length + " / 120 码点";
  $("revised-count").textContent = cps($("revised").value).length + " / 120 码点";
  let msg = "";
  try {
    const v = JSON.parse($("labels").value);
    const arr = Array.isArray(v) ? v : v.labels;
    msg = Array.isArray(arr) ? arr.length + " / 10 个标签" : "";
  } catch { msg = "JSON 尚未解析成功"; }
  $("labels-count").textContent = msg;
}

function fillExample(i) {
  const ex = EXAMPLES[i];
  $("original").value = ex.original;
  $("revised").value = ex.revised;
  $("labels").value = JSON.stringify(ex.labels, null, 2);
  updateCounts();
}

async function saveInputs() {
  hideBanner();
  let labels;
  try { labels = JSON.parse($("labels").value || "[]"); }
  catch (e) { showBanner("标签 JSON 解析失败：" + e.message, true); return; }
  const body = JSON.stringify({
    original: $("original").value,
    revised: $("revised").value,
    labels,
  });
  $("save-status").textContent = "保存中…";
  try {
    let st;
    if (!state.sessionId) {
      st = await api("/api/sessions", { method: "POST", body });
      state.sessionId = st.session_id;
      history.replaceState(null, "", "?session=" + st.session_id);
    } else {
      st = await api(`/api/sessions/${state.sessionId}/inputs`, { method: "PUT", body });
    }
    $("save-status").textContent = "已保存";
    applyState(st, /*fromServer=*/true);
  } catch (e) {
    $("save-status").textContent = "";
    showBanner("保存失败：" + detailMessage(e.data), true);
  }
}

/* ---------- 渲染 ---------- */
function spanPreview(text, start, end) {
  const arr = cps(text);
  const before = arr.slice(0, start).join("");
  const mid = arr.slice(start, end).join("");
  const after = arr.slice(end).join("");
  return esc(before) + "<mark>" + esc(mid) + "</mark>" + esc(after);
}

function renderAnalysis() {
  const box = $("label-cards");
  box.innerHTML = "";
  const a = state.analysis;
  if (!a) {
    $("analysis-summary").textContent = "尚未分析。";
    $("confirm").disabled = true;
    return;
  }
  $("analysis-summary").textContent =
    `最小编辑距离 ${a.distance}；标签 ${a.labels.length} 个：` +
    `自动迁移 ${a.labels.filter(l => l.status === "auto").length}，` +
    `待选择 ${a.labels.filter(l => l.status === "choice").length}，` +
    `无法保留 ${a.labels.filter(l => l.status === "impossible").length}。`;

  const revised = $("revised").value;
  for (const lab of a.labels) {
    const card = document.createElement("div");
    card.className = "label-card";
    card.id = "card-" + lab.id;
    const L = lab.end - lab.start;
    const badgeText = { auto: "自动迁移", choice: "待选择", impossible: "无法保留" }[lab.status];
    let html = `<div class="label-head">
      <span class="lid">${esc(lab.id)}</span>
      <span class="span">原文 [${lab.start}, ${lab.end})</span>
      <span class="src-text">${esc(lab.text)}</span>
      <span class="badge ${lab.status}">${badgeText}</span>
    </div>`;

    if (lab.status === "auto") {
      const p = lab.possible[0];
      html += `<div class="choices">
        <label class="choice-item">
          <input type="radio" name="sel-${esc(lab.id)}" value="keep" checked>
          <span class="pos-tag">迁至 [${p}, ${p + L})</span>
          <span class="preview">${spanPreview(revised, p, p + L)}</span>
        </label>
        <label class="choice-item">
          <input type="radio" name="sel-${esc(lab.id)}" value="discard">
          <span class="pos-tag">丢弃该标签</span>
        </label>
      </div>`;
    } else if (lab.status === "choice") {
      if (lab.possible.length === 1 && !lab.preserved_in_all) {
        html += `<div class="hint">只有一个可能落点，但并非所有最优对齐都保留该标签，请确认是否迁移。</div>`;
      }
      html += `<div class="choices">` + lab.possible.map((p) => `
        <label class="choice-item">
          <input type="radio" name="sel-${esc(lab.id)}" value="${p}">
          <span class="pos-tag">迁至 [${p}, ${p + L})</span>
          <span class="preview">${spanPreview(revised, p, p + L)}</span>
        </label>`).join("") + `
        <label class="choice-item">
          <input type="radio" name="sel-${esc(lab.id)}" value="discard">
          <span class="pos-tag">丢弃该标签</span>
        </label>
      </div>`;
    } else {
      html += `<div class="impossible-note">原跨度字符未在任何最优对齐中完整连续保留，确认时将自动丢弃。</div>`;
    }
    card.innerHTML = html;
    box.appendChild(card);
  }

  // 恢复已保存的选择
  for (const [lid, sel] of Object.entries(state.selections)) {
    const name = "sel-" + lid;
    const value = sel.choice === "discard" ? "discard" : String(sel.position);
    const radio = document.querySelector(`input[name="${CSS.escape(name)}"][value="${CSS.escape(value)}"]`);
    if (radio) radio.checked = true;
  }
  $("confirm").disabled = false;
}

function collectSelections() {
  const out = {};
  if (!state.analysis) return out;
  for (const lab of state.analysis.labels) {
    if (lab.status === "impossible") continue;
    const checked = document.querySelector(`input[name="sel-${CSS.escape(lab.id)}"]:checked`);
    if (!checked) continue;
    if (checked.value === "discard") out[lab.id] = { choice: "discard" };
    else if (checked.value === "keep") continue; // auto 默认迁移，无需记录
    else out[lab.id] = { choice: "position", position: Number(checked.value) };
  }
  return out;
}

/* ---------- 状态应用 ---------- */
function applyState(st, fromServer) {
  state.revision = st.revision;
  state.analysis = st.analysis;
  state.selections = st.selections || {};
  state.confirmed = st.has_result;
  $("session-info").textContent = "会话 " + st.session_id.slice(0, 8) + "…";
  $("revision-info").textContent = "版本 r" + st.revision;
  if (fromServer) {
    $("original").value = st.original;
    $("revised").value = st.revised;
    $("labels").value = JSON.stringify({ labels: st.labels }, null, 2);
    updateCounts();
  }
  renderAnalysis();
  const dl = $("download");
  if (st.has_result) {
    dl.href = st.download_url;
    dl.classList.remove("hidden");
  } else {
    dl.classList.add("hidden");
    $("result-preview").classList.add("hidden");
  }
}

/* ---------- 确认 ---------- */
async function confirmAll() {
  hideBanner();
  $("confirm-status").textContent = "";
  document.querySelectorAll(".label-card.conflict")
    .forEach((c) => c.classList.remove("conflict"));
  const sid = state.sessionId;
  const rev = state.revision;
  try {
    await api(`/api/sessions/${sid}/selections`, {
      method: "PUT",
      body: JSON.stringify({ revision: rev, selections: collectSelections() }),
    });
    const resp = await api(`/api/sessions/${sid}/confirm`, {
      method: "POST",
      body: JSON.stringify({ revision: rev }),
    });
    state.confirmed = true;
    $("confirm-status").textContent = "确认成功，结果已生成。";
    const dl = $("download");
    dl.href = resp.download_url;
    dl.classList.remove("hidden");
    const prev = $("result-preview");
    prev.textContent = JSON.stringify(resp.result, null, 2);
    prev.classList.remove("hidden");
  } catch (e) {
    const d = e.data && e.data.detail;
    if (e.status === 409 && d && d.code === "stale_revision") {
      showBanner("确认被拒绝：" + d.message + "（当前版本 r" + d.current_revision + "）", true);
      await reloadState();
    } else if (e.status === 409 && d && d.code === "conflict") {
      showBanner("确认被拒绝：" + d.message, true);
      (d.conflicting_labels || []).forEach((lid) => {
        const card = $("card-" + lid);
        if (card) card.classList.add("conflict");
      });
      $("confirm-status").textContent = "冲突标签：" + (d.conflicting_labels || []).join("、");
    } else if (e.status === 422 && d && d.code === "unresolved") {
      showBanner("确认被拒绝：" + d.message + "：" + (d.labels || []).join("、"), true);
    } else {
      showBanner("确认失败：" + detailMessage(e.data), true);
    }
  }
}

/* ---------- 轮询：发现别的窗口改了输入 ---------- */
async function reloadState() {
  if (!state.sessionId) return;
  const st = await api(`/api/sessions/${state.sessionId}`);
  applyState(st, true);
}

async function poll() {
  if (!state.sessionId) return;
  try {
    const st = await api(`/api/sessions/${state.sessionId}`);
    if (st.revision !== state.revision) {
      showBanner(`输入已在其他窗口修改（r${state.revision} → r${st.revision}），本页已刷新为最新分析，旧选择已失效。`);
      applyState(st, true);
    }
  } catch { /* 网络抖动忽略 */ }
}

/* ---------- 启动 ---------- */
function init() {
  const sel = $("example-select");
  EXAMPLES.forEach((ex, i) => {
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = ex.name;
    sel.appendChild(opt);
  });
  $("load-example").addEventListener("click", () => fillExample(Number(sel.value)));
  ["original", "revised", "labels"].forEach((id) =>
    $(id).addEventListener("input", updateCounts));
  $("save").addEventListener("click", saveInputs);
  $("confirm").addEventListener("click", confirmAll);
  updateCounts();

  initTabs();
  initStitch();

  const sid = new URLSearchParams(location.search).get("session");
  if (sid) {
    state.sessionId = sid;
    reloadState().catch(() => showBanner("会话不存在或加载失败", true));
  }
  state.pollTimer = setInterval(poll, 3000);
}

/* ---------- 标签页 ---------- */
function initTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) =>
        b.classList.toggle("active", b === btn));
      const name = btn.dataset.tab;
      $("page-migrate").classList.toggle("hidden", name !== "migrate");
      $("page-stitch").classList.toggle("hidden", name !== "stitch");
    });
  });
}

/* ---------- 拼稿 ---------- */
const STITCH_DEMO = [
  { revised: "甲乙丙丁戊", labels: [{ id: "L1", start: 3, end: 5 }] },
  { revised: "丁戊己庚", labels: [{ id: "L1", start: 0, end: 2 }, { id: "L2", start: 2, end: 4 }] },
  { revised: "己庚辛壬", labels: [{ id: "L9", start: 0, end: 2 }] },
];

const stitchState = { segmentCount: 2 };

function stitchEl() {
  const box = $("stitch-segments");
  box.innerHTML = "";
  for (let i = 1; i <= stitchState.segmentCount; i++) {
    const row = document.createElement("div");
    row.className = "stitch-row";
    row.innerHTML = `
      <span class="stitch-idx">第 ${i} 段</span>
      <input type="text" class="stitch-sid" placeholder="会话 ID（session_id）" spellcheck="false">
      <input type="number" min="1" class="stitch-rev" placeholder="确认版本 revision" title="确认版本 revision">`;
    box.appendChild(row);
  }
  $("stitch-seg-count").textContent = `${stitchState.segmentCount} 段（允许 2～6）`;
  renderSeamInputs();
}

function renderSeamInputs() {
  const n = stitchState.segmentCount;
  const box = $("stitch-seams");
  box.innerHTML = "";
  if (n < 2) { box.textContent = "先确定段数。"; return; }
  for (let i = 1; i < n; i++) {
    const row = document.createElement("div");
    row.className = "stitch-row";
    row.innerHTML = `
      <span class="stitch-idx">接缝 ${i}（第 ${i} 段末尾 ↔ 第 ${i + 1} 段开头）</span>
      <input type="text" class="stitch-cands" placeholder="1～3 个候选重叠码点数，逗号分隔，如 1,2,3" spellcheck="false">`;
    box.appendChild(row);
  }
}

function collectStitchRequest() {
  const segments = [...document.querySelectorAll("#stitch-segments .stitch-row")]
    .map((row) => ({
      session_id: row.querySelector(".stitch-sid").value.trim(),
      revision: Number(row.querySelector(".stitch-rev").value),
    }));
  const seams = [...document.querySelectorAll("#stitch-seams .stitch-row")]
    .map((row) => {
      const cands = row.querySelector(".stitch-cands").value
        .split(",").map((s) => s.trim()).filter(Boolean).map(Number);
      return { candidates: cands };
    });
  return { segments, seams };
}

function fillStitchForm(refs, candidatesPerSeam) {
  stitchState.segmentCount = refs.length;
  stitchEl();
  const rows = document.querySelectorAll("#stitch-segments .stitch-row");
  refs.forEach((ref, i) => {
    rows[i].querySelector(".stitch-sid").value = ref.session_id;
    rows[i].querySelector(".stitch-rev").value = ref.revision;
  });
  const seamRows = document.querySelectorAll("#stitch-seams .stitch-row");
  candidatesPerSeam.forEach((cands, i) => {
    seamRows[i].querySelector(".stitch-cands").value = cands.join(",");
  });
}

async function loadStitchDemo() {
  hideBanner();
  $("stitch-run-status").textContent = "示例会话创建中…";
  try {
    const refs = [];
    for (const item of STITCH_DEMO) {
      const r = await api("/api/sessions", {
        method: "POST",
        body: JSON.stringify({
          original: item.revised, revised: item.revised, labels: item.labels,
        }),
      });
      const c = await api(`/api/sessions/${r.session_id}/confirm`, {
        method: "POST", body: JSON.stringify({ revision: r.revision }),
      });
      if (!c.ok) throw new Error("示例会话确认失败");
      refs.push({ session_id: r.session_id, revision: r.revision });
    }
    fillStitchForm(refs, [[1, 2, 3], [1, 2]]);
    $("stitch-run-status").textContent = "示例已填入，可直接试拼。";
  } catch (e) {
    $("stitch-run-status").textContent = "";
    showBanner("示例创建失败：" + detailMessage(e.data), true);
  }
}

function renderRejections(rejections) {
  if (!rejections || !rejections.length) return "";
  const items = rejections.map((r) =>
    `<li>接缝 ${r.seam_index}，候选 ${r.candidate}：${esc(r.reason)}</li>`).join("");
  return `<div class="stitch-sub"><strong>被拒候选：</strong><ul>${items}</ul></div>`;
}

function renderStitchResult(body) {
  const box = $("stitch-result");
  const prev = $("stitch-preview");
  const dl = $("stitch-download");
  dl.classList.add("hidden");
  prev.classList.add("hidden");
  let html = "";
  if (body.status === "unique") {
    const r = body.result;
    html = `<div class="stitch-ok">✅ ${esc(body.message)}</div>
      <div class="stitch-sub">各接缝采用重叠码点数：${r.meta.overlaps.join("、")}</div>
      <div class="stitch-sub">各段在合稿中的半开区间：</div>
      <ul>${r.segments.map((s) =>
        `<li>第 ${s.session_index} 段（会话 ${esc(s.session_id.slice(0, 8))}…，r${s.revision}）：[${s.start}, ${s.end})，长 ${s.length}</li>`).join("")}</ul>
      <div class="stitch-sub">合稿标签（完全重合已合并，sources 记录会话号与标签号）：</div>
      <ul>${r.labels.map((l) =>
        `<li>${esc(l.id)} [${l.start}, ${l.end}) “${esc(l.text)}” ← ` +
        l.sources.map((src) => `第${src.session_index}段·${esc(src.label_id)}`).join("，") + `</li>`).join("")}</ul>`;
    dl.href = body.download_url;
    dl.classList.remove("hidden");
    prev.textContent = JSON.stringify(r, null, 2);
    prev.classList.remove("hidden");
  } else if (body.status === "tie") {
    html = `<div class="stitch-warn">⚠️ ${esc(body.message)}</div>
      <div class="stitch-sub">并列最短的重叠长度组合（请复核后收窄候选，本次不生成文件）：</div>
      <ul>${body.ties.map((t) => `<li>[${t.overlaps.join(", ")}]</li>`).join("")}</ul>`;
  } else {
    html = `<div class="stitch-bad">❌ ${esc(body.message)}</div>`;
    if (body.reason === "label_conflict" && body.label_conflicts) {
      html += `<div class="stitch-sub">非完全重合的标签相交：</div><ul>` +
        body.label_conflicts.slice(0, 8).map((cf) =>
          `<li>重叠 [${cf.overlaps.join(",")}] 下：第${cf.label_a.session_index}段标签 ${esc(cf.label_a.label_id)} 与 第${cf.label_b.session_index}段标签 ${esc(cf.label_b.label_id)}</li>`).join("") +
        `</ul><div class="hint">请检查接缝候选或标签后重试；本次不生成文件。</div>`;
    } else {
      html += `<div class="hint">请检查接缝重叠长度或标签后重试；本次不生成文件。</div>`;
    }
  }
  html += renderRejections(body.seam_rejections);
  box.innerHTML = html;
}

async function runStitch() {
  hideBanner();
  $("stitch-run-status").textContent = "试拼中…";
  const payload = collectStitchRequest();
  try {
    const body = await api("/api/stitches", {
      method: "POST", body: JSON.stringify(payload),
    });
    $("stitch-run-status").textContent = "完成";
    renderStitchResult(body);
  } catch (e) {
    $("stitch-run-status").textContent = "";
    const d = e.data && e.data.detail;
    if (d && typeof d === "object" && d.message) {
      showBanner("拼稿中止：" + d.message, true);
      $("stitch-result").innerHTML =
        `<div class="stitch-bad">❌ ${esc(d.message)}</div>` +
        (d.session_index ? `<div class="hint">问题出在第 ${d.session_index} 段。</div>` : "");
    } else {
      showBanner("拼稿失败：" + detailMessage(e.data), true);
    }
  }
}

function initStitch() {
  $("stitch-add-segment").addEventListener("click", () => {
    if (stitchState.segmentCount >= 6) { showBanner("最多 6 段", true); return; }
    stitchState.segmentCount += 1;
    stitchEl();
  });
  $("stitch-remove-segment").addEventListener("click", () => {
    if (stitchState.segmentCount <= 2) { showBanner("至少 2 段", true); return; }
    stitchState.segmentCount -= 1;
    stitchEl();
  });
  $("stitch-demo").addEventListener("click", loadStitchDemo);
  $("stitch-run").addEventListener("click", runStitch);
  stitchEl();
}

document.addEventListener("DOMContentLoaded", init);
