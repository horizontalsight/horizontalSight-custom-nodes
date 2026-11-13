// civitai_db 管理面板（零构建，原生 ES module）
// 相对当前页面：/db/ 和旧的 /civitai-db/ 都能工作
const API_BASE = new URL("./", window.location.href).pathname;

const STATUS = {
  ok: ["已收录", "ok"],
  new: ["待查询", "new"],
  not_found: ["未找到", "not_found"],
  error: ["查询失败", "error"],
  no_air: ["无 AIR", "no_air"],
  pending: ["待哈希", "pending"],
  ignored: ["已忽略", "ignored"],
  invalid: ["异常", "error"],
};

const INJECT_STATUS = {
  ok: ["已写入", "ok"],
  "dry-run": ["预览", "new"],
  skip: ["跳过", "ignored"],
  error: ["失败", "error"],
};

const S = {
  rows: [],
  stats: {},
  state: null,
  config: null,
  filters: { q: "", type: "", base: "", status: "", path: "", existing: false },
  sort: { key: "baseModel", dir: 1 },
  page: 1,
  pageSize: 50,
  selected: new Set(),
  jobId: null,
  job: null,
  pollTimer: null,
  polling: false,
  editKey: null,
  lastInject: null,
  pathInitialized: false,
  pathCleared: false,
  lastModelsRoot: "",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function enc(value) {
  return encodeURIComponent(String(value ?? ""));
}

function fmtTime(value) {
  return value ? String(value) : "—";
}

function shortKey(key) {
  return String(key || "").startsWith("pending:") ? "待哈希" : String(key || "");
}

function statusOf(row) {
  return STATUS[row.status] || [row.status || "未知", ""];
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = String(message);
  el.className = "toast" + (isError ? " err" : "");
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, isError ? 8000 : 3800);
}

async function api(path, options = {}) {
  const response = await fetch(API_BASE + path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data;
  try {
    data = await response.json();
  } catch (error) {
    throw new Error(`HTTP ${response.status}`);
  }
  if (!response.ok || !data.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

// ---------------------------------------------------------------------------
// 加载与渲染
// ---------------------------------------------------------------------------
async function loadAll() {
  try {
    const [stateRes, entriesRes] = await Promise.all([
      api("state"), api("entries"),
    ]);
    S.state = stateRes.state;
    S.stats = entriesRes.stats || {};
    S.rows = entriesRes.rows || [];
    if (S.state && S.state.db_path) $("#db-path").textContent = S.state.db_path;
    if (S.state && S.state.corrupt) {
      toast("数据库损坏：" + (S.state.db_error || "无法读取"), true);
    }
    renderAll();
    if (S.state && S.state.job && ["queued", "running"].includes(S.state.job.state)) {
      S.job = S.state.job;
      S.jobId = S.state.job.id;
      showJob(S.state.job);
      pollJob();
    }
  } catch (error) {
    toast("加载失败：" + error.message, true);
  }
}

function renderAll() {
  renderStats();
  renderFilters();
  renderTable();
  renderSelection();
}

function renderStats() {
  const s = S.stats || {};
  const map = {
    "stat-total": s.total || 0,
    "stat-ok": s.ok || 0,
    "stat-new": s.new || 0,
    "stat-notfound": s.not_found || 0,
    "stat-error": (s.error || 0) + (s.no_air || 0),
    "stat-pending": s.pending || 0,
    "stat-ignored": s.ignored || 0,
  };
  for (const [id, value] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }
}

function fillSelect(select, placeholder, values, current) {
  const html = [`<option value="">${esc(placeholder)}</option>`]
    .concat(values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`));
  select.innerHTML = html.join("");
  select.value = current && values.includes(current) ? current : "";
}

function renderFilters() {
  const types = [...new Set(S.rows.map((r) => r.type).filter(Boolean))].sort();
  const bases = [...new Set(S.rows.map((r) => r.baseModel).filter(Boolean))].sort();
  fillSelect($("#f-type"), "全部类型", types, S.filters.type);
  fillSelect($("#f-base"), "全部 baseModel", bases, S.filters.base);
}

function sortValue(row, key) {
  if (key === "AutoV2") return String(row.key || "").toLowerCase();
  if (key === "status") return statusOf(row)[0];
  const value = row[key];
  return String(value ?? "").toLowerCase();
}

function pathMatches(path, directory) {
  if (!directory) return true;
  const norm = (value) => String(value).replace(/\//g, "\\")
    .replace(/\\+$/, "").toLowerCase();
  const candidate = norm(path);
  const root = norm(directory);
  if (!root) return true;
  return candidate === root || candidate.startsWith(root + "\\");
}

function filteredRows() {
  const f = S.filters;
  const q = f.q.trim().toLowerCase();
  const pathQuery = f.path.trim();
  let rows = S.rows.filter((row) => {
    if (f.type && row.type !== f.type) return false;
    if (f.base && row.baseModel !== f.base) return false;
    if (f.status === "missing_path") {
      if (row.pathExists) return false;
    } else if (f.status && row.status !== f.status) {
      return false;
    }
    if (f.existing && !row.pathExists) return false;
    if (pathQuery) {
      const hit = (row.paths || []).some((p) => pathMatches(p, pathQuery));
      if (!hit) return false;
    }
    if (q) {
      const hay = [row.modelName, row.file, row.AIR, row.key,
        ...(row.files || []), ...(row.paths || [])].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  const key = S.sort.key;
  const dir = S.sort.dir;
  rows.sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
  return rows;
}

function rowHtml(row) {
  const st = statusOf(row);
  const key = esc(row.key);
  const selected = S.selected.has(row.key);
  const files = row.files || [];
  const paths = row.paths || [];
  const pathText = paths[0] || "—";
  const extraFiles = files.length > 1 ? ` <span class="sub">+${files.length - 1}</span>` : "";
  const air = row.AIR || "";
  const airShort = air.length > 42 ? air.slice(0, 42) + "…" : air;
  const urlButton = row.url
    ? `<button class="icon-btn" data-act="url" title="打开 Civitai 网页">🔗</button>`
    : "";
  return `<tr data-key="${key}" class="${selected ? "selected" : ""}">
    <td><input type="checkbox" data-select="${key}" ${selected ? "checked" : ""}></td>
    <td><span class="status ${st[1]}">${esc(st[0])}</span></td>
    <td><span class="ellipsis" title="${esc(row.file)}">${esc(row.file)}</span>${extraFiles}</td>
    <td class="mono">${esc(shortKey(row.key))}</td>
    <td><span class="ellipsis" title="${esc(row.modelName)}">${esc(row.modelName || "—")}</span></td>
    <td class="mono"><span class="ellipsis" title="${esc(air)}">${esc(airShort || "—")}</span></td>
    <td>${esc(row.type || "—")}</td>
    <td>${esc(row.baseModel || "—")}</td>
    <td><span class="ellipsis" title="${esc(paths.join("\n"))}">${esc(pathText)}</span></td>
    <td><div class="row-actions">
      ${urlButton}
      <button class="icon-btn" data-act="menu" title="更多操作">⋯</button>
      <button class="icon-btn" data-act="edit" title="编辑">✎</button>
    </div></td>
  </tr>`;
}

function renderTable() {
  const rows = filteredRows();
  const totalPages = Math.max(1, Math.ceil(rows.length / S.pageSize));
  S.page = Math.min(Math.max(1, S.page), totalPages);
  const start = (S.page - 1) * S.pageSize;
  const pageRows = rows.slice(start, start + S.pageSize);
  $("#rows").innerHTML = pageRows.map(rowHtml).join("");
  $("#pager-info").textContent = `共 ${rows.length} 条 · 第 ${S.page}/${totalPages} 页`;
  $("#page-now").textContent = String(S.page);
  $("#page-prev").disabled = S.page <= 1;
  $("#page-next").disabled = S.page >= totalPages;
  $$("#tab-models thead th[data-sort]").forEach((th) => {
    const base = th.textContent.replace(/[▲▼]\s*$/, "").trim();
    th.innerHTML = esc(base) + (th.dataset.sort === S.sort.key
      ? ` <span class="arrow">${S.sort.dir > 0 ? "▲" : "▼"}</span>` : "");
  });
  const allPageKeys = pageRows.map((r) => r.key);
  const allChecked = allPageKeys.length > 0 && allPageKeys.every((k) => S.selected.has(k));
  $("#check-all").checked = allChecked;
}

function renderSelection() {
  const count = S.selected.size;
  $("#sel-count").textContent = count ? `已选 ${count}` : "";
  $("#btn-bulk-query").disabled = count === 0;
  $("#btn-bulk-delete").disabled = count === 0;
}

function byKey(key) {
  return S.rows.find((row) => row.key === key);
}

function pageKeys() {
  const rows = filteredRows();
  const start = (S.page - 1) * S.pageSize;
  return rows.slice(start, start + S.pageSize).map((r) => r.key);
}

function toggleSelect(key, checked) {
  if (checked) S.selected.add(key);
  else S.selected.delete(key);
  renderTable();
  renderSelection();
}

// ---------------------------------------------------------------------------
// 任务
// ---------------------------------------------------------------------------
async function startJob(action, params = {}) {
  if (S.jobId) {
    toast("已有任务在运行，请等待或取消。", true);
    return false;
  }
  try {
    const data = await api("jobs", {
      method: "POST",
      body: JSON.stringify({ action, params }),
    });
    S.jobId = data.job_id;
    S.job = null;
    showJob({ state: "queued", total: 0, current: 0, detail: "已提交", logs: [] });
    pollJob();
    return true;
  } catch (error) {
    toast("启动任务失败：" + error.message, true);
    return false;
  }
}

async function pollJob() {
  if (S.polling) return;
  S.polling = true;
  const tick = async () => {
    if (!S.jobId) {
      S.polling = false;
      return;
    }
    try {
      const data = await api("jobs/" + enc(S.jobId));
      S.job = data.job;
      showJob(data.job);
      if (["done", "error", "cancelled"].includes(data.job.state)) {
        clearInterval(S.pollTimer);
        S.pollTimer = null;
        S.jobId = null;
        S.polling = false;
        await loadAll();
        if (data.job.action === "inject" && data.job.result) {
          renderInjectResult(data.job.result);
        }
        if (data.job.state === "error") toast(data.job.message || "任务失败", true);
        else if (data.job.state === "cancelled") toast("任务已取消");
        else toast("任务完成");
      }
    } catch (error) {
      clearInterval(S.pollTimer);
      S.pollTimer = null;
      S.jobId = null;
      S.polling = false;
      toast("读取任务状态失败：" + error.message, true);
    }
  };
  await tick();
  if (S.jobId) S.pollTimer = setInterval(tick, 700);
}

function showJob(job) {
  const panel = $("#job-panel");
  panel.hidden = false;
  const labels = {
    queued: "排队中", running: "运行中", done: "完成",
    error: "失败", cancelled: "已取消",
  };
  $("#job-state").textContent = labels[job.state] || job.state || "空闲";
  const total = Number(job.total || 0);
  const current = Number(job.current || 0);
  const detail = job.detail || job.message || "";
  $("#job-detail").textContent = total
    ? `${current}/${total}  ${detail}`
    : detail;
  const percent = total ? Math.min(100, Math.round(current * 100 / total)) : 0;
  $("#job-progress").style.width = percent + "%";
  $("#job-log").textContent = (job.logs || []).join("\n");
  $("#job-cancel").disabled = !["queued", "running"].includes(job.state);
  if (job.logs && job.logs.length) {
    const log = $("#job-log");
    log.scrollTop = log.scrollHeight;
  }
}

// ---------------------------------------------------------------------------
// 右键菜单
// ---------------------------------------------------------------------------
let ctxElement = null;

function hideContextMenu() {
  if (ctxElement) {
    ctxElement.remove();
    ctxElement = null;
  }
}

function showContextMenu(x, y, row) {
  hideContextMenu();
  const menu = document.createElement("div");
  menu.className = "ctx";
  menu.setAttribute("role", "menu");
  const item = (label, fn, disabled = false) => {
    const button = document.createElement("button");
    button.textContent = label;
    button.disabled = Boolean(disabled);
    button.addEventListener("click", () => {
      hideContextMenu();
      Promise.resolve(fn()).catch((error) => toast(error.message, true));
    });
    menu.appendChild(button);
  };
  const separator = () => menu.appendChild(document.createElement("hr"));

  item("编辑", () => openEditor(row.key));
  item("删除", () => deleteEntry(row.key), false);
  separator();
  item("复制模型名", () => copyText(row.modelName || row.file || ""), !row.modelName && !row.file);
  item("复制 AutoV2", () => copyText(shortKey(row.key)), row.key.startsWith("pending:"));
  item("复制 AIR", () => copyText(row.AIR), !row.AIR);
  item("复制路径", () => copyText((row.paths || []).join("\n")), !(row.paths || []).length);
  separator();
  item("打开 Civitai 网页", () => window.open(row.url, "_blank", "noopener"), !row.url);
  item("在资源管理器中打开", () => reveal(row), !(row.paths || []).length);
  separator();
  item("重新查询此条", () => startJob("query_selected", { selected: [row.key], limit: 1 }),
    row.key.startsWith("pending:"));
  item(row.ignored ? "取消忽略" : "标记忽略", () => toggleIgnore(row));
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 6)) + "px";
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 6)) + "px";
  ctxElement = menu;
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制");
  } catch (error) {
    toast("复制失败：" + error.message, true);
  }
}

async function reveal(row) {
  const path = (row.paths || []).find(Boolean);
  if (!path) return;
  await api("reveal", { method: "POST", body: JSON.stringify({ path }) });
}

async function deleteEntry(key) {
  const row = byKey(key);
  if (!row) return;
  if (!confirm(`确定删除这条记录吗？\n\n${row.modelName || row.file || key}`)) return;
  await api("entry/" + enc(key), { method: "DELETE" });
  S.selected.delete(key);
  await loadAll();
  toast("已删除");
}

async function toggleIgnore(row) {
  const data = await api("entry/" + enc(row.key));
  const entry = data.entry;
  entry.ignored = !entry.ignored;
  await api("entry/" + enc(row.key), {
    method: "PUT", body: JSON.stringify({ entry }),
  });
  await loadAll();
  toast(entry.ignored ? "已标记忽略" : "已取消忽略");
}

// ---------------------------------------------------------------------------
// 编辑弹窗
// ---------------------------------------------------------------------------
function openModal(html, onMount) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-backdrop"><div class="modal">${html}</div></div>`;
  const backdrop = root.firstElementChild;
  backdrop.addEventListener("pointerdown", (event) => {
    if (event.target === backdrop) closeModal();
  });
  root.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", closeModal);
  });
  if (onMount) onMount(backdrop.querySelector(".modal"));
  return backdrop.querySelector(".modal");
}

function closeModal() {
  $("#modal-root").innerHTML = "";
}

function splitLines(text) {
  return String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function numberOrNull(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
}

async function openEditor(key) {
  try {
    const data = await api("entry/" + enc(key));
    const entry = data.entry;
    const kind = entry.failedKind || "";
    const html = `
      <h3>编辑记录 · ${esc(shortKey(key))}</h3>
      <div class="grid2">
        <label class="field"><span>文件名（每行一个）</span>
          <textarea data-f="file" rows="3">${esc((entry.file || []).join("\n"))}</textarea></label>
        <label class="field"><span>本地路径（每行一个）</span>
          <textarea data-f="paths" rows="3">${esc((entry.paths || []).join("\n"))}</textarea></label>
        <label class="field"><span>模型名</span><input data-f="modelName" type="text" value="${esc(entry.modelName || "")}"></label>
        <label class="field"><span>AIR</span><input data-f="AIR" type="text" value="${esc(entry.AIR || "")}"></label>
        <label class="field"><span>modelId</span><input data-f="modelId" type="number" value="${esc(entry.modelId ?? "")}"></label>
        <label class="field"><span>modelVersionId</span><input data-f="modelVersionId" type="number" value="${esc(entry.modelVersionId ?? "")}"></label>
        <label class="field"><span>URL</span><input data-f="url" type="text" value="${esc(entry.url || "")}"></label>
        <label class="field"><span>类型</span><input data-f="type" type="text" value="${esc(entry.type || "")}"></label>
        <label class="field"><span>baseModel</span><input data-f="baseModel" type="text" value="${esc(entry.baseModel || "")}"></label>
        <label class="field"><span>baseModelType</span><input data-f="baseModelType" type="text" value="${esc(entry.baseModelType || "")}"></label>
        <label class="field"><span>失败类型</span>
          <select data-f="failedKind">
            <option value="" ${kind ? "" : "selected"}>无</option>
            <option value="not_found" ${kind === "not_found" ? "selected" : ""}>未找到（404）</option>
            <option value="error" ${kind === "error" ? "selected" : ""}>查询失败（网络/限流）</option>
            <option value="no_air" ${kind === "no_air" ? "selected" : ""}>无 AIR</option>
          </select></label>
        <label class="field"><span>查询时间</span><input data-f="queriedAt" type="text" value="${esc(entry.queriedAt || "")}"></label>
        <label class="field"><span>最后尝试</span><input data-f="lastTriedAt" type="text" value="${esc(entry.lastTriedAt || "")}"></label>
        <label class="field"><span>错误信息</span><input data-f="error" type="text" value="${esc(entry.error || "")}"></label>
      </div>
      <div class="checkrow" style="margin-top:10px">
        <label><input data-f="failed" type="checkbox" ${entry.failed ? "checked" : ""}> failed</label>
        <label><input data-f="ignored" type="checkbox" ${entry.ignored ? "checked" : ""}> 忽略</label>
        <span class="hint">AutoV2：${esc(shortKey(key))}</span>
      </div>
      <details>
        <summary>原始 JSON</summary>
        <textarea class="json-box" data-json>${esc(JSON.stringify(entry, null, 2))}</textarea>
        <div class="actions">
          <button data-apply-json>从 JSON 应用到表单</button>
          <button data-copy-json>复制 JSON</button>
        </div>
      </details>
      <div class="modal-actions">
        <button data-close>取消</button>
        <button class="primary" data-save>保存</button>
      </div>`;
    const modal = openModal(html);
    const field = (name) => modal.querySelector(`[data-f="${name}"]`);
    const fill = (value) => {
      field("file").value = (value.file || []).join("\n");
      field("paths").value = (value.paths || []).join("\n");
      for (const name of ["modelName", "AIR", "url", "type", "baseModel",
        "baseModelType", "queriedAt", "lastTriedAt", "error"]) {
        field(name).value = value[name] ?? "";
      }
      field("modelId").value = value.modelId ?? "";
      field("modelVersionId").value = value.modelVersionId ?? "";
      field("failedKind").value = value.failedKind || "";
      field("failed").checked = Boolean(value.failed);
      field("ignored").checked = Boolean(value.ignored);
      modal.querySelector("[data-json]").value = JSON.stringify(value, null, 2);
    };
    modal.querySelector("[data-apply-json]").addEventListener("click", () => {
      try {
        fill(JSON.parse(modal.querySelector("[data-json]").value));
        toast("已应用到表单");
      } catch (error) {
        toast("JSON 格式错误：" + error.message, true);
      }
    });
    modal.querySelector("[data-copy-json]").addEventListener("click", () => {
      copyText(modal.querySelector("[data-json]").value);
    });
    modal.querySelector("[data-save]").addEventListener("click", async () => {
      const updated = JSON.parse(JSON.stringify(entry));
      updated.file = splitLines(field("file").value);
      updated.paths = splitLines(field("paths").value);
      for (const name of ["modelName", "AIR", "url", "type", "baseModel",
        "baseModelType", "error"]) {
        updated[name] = field(name).value.trim() || null;
      }
      updated.modelId = numberOrNull(field("modelId").value);
      updated.modelVersionId = numberOrNull(field("modelVersionId").value);
      updated.queriedAt = field("queriedAt").value.trim() || null;
      updated.lastTriedAt = field("lastTriedAt").value.trim() || null;
      updated.failedKind = field("failedKind").value || null;
      updated.failed = field("failed").checked;
      updated.ignored = field("ignored").checked;
      try {
        await api("entry/" + enc(key), {
          method: "PUT", body: JSON.stringify({ entry: updated }),
        });
        closeModal();
        await loadAll();
        toast("已保存");
      } catch (error) {
        toast("保存失败：" + error.message, true);
      }
    });
  } catch (error) {
    toast("读取记录失败：" + error.message, true);
  }
}

// ---------------------------------------------------------------------------
// 扫描弹窗
// ---------------------------------------------------------------------------
function openScanModal() {
  const scan = (S.config && S.config.scan) || {};
  const extensions = (scan.extensions || []).join(",");
  const html = `
    <h3>扫描登记</h3>
    <div class="grid2">
      <label class="field"><span>目录（留空 = 使用设置的模型类别目录）</span>
        <input id="scan-folder" type="text" placeholder="留空则扫描 ComfyUI 注册的 diffusion_models / checkpoints / loras"></label>
      <label class="field"><span>后缀（逗号分隔，留空用设置）</span>
        <input id="scan-ext" type="text" value="${esc(extensions)}"></label>
    </div>
    <div class="checkrow" style="margin-top:10px">
      <label><input id="scan-recursive" type="checkbox" checked> 递归子目录</label>
      <span class="hint">只登记路径和文件名，不计算哈希。</span>
    </div>
    <div class="modal-actions">
      <button data-close>取消</button>
      <button class="primary" id="scan-start">开始扫描</button>
    </div>`;
  const modal = openModal(html);
  $("#scan-start", modal).addEventListener("click", async () => {
    const folder = $("#scan-folder", modal).value.trim();
    const extensions = $("#scan-ext", modal).value.trim();
    const recursive = $("#scan-recursive", modal).checked;
    closeModal();
    await startJob("scan", { folder, extensions, recursive });
  });
}

// ---------------------------------------------------------------------------
// PNG 注入
// ---------------------------------------------------------------------------
function injectParams(dryRun) {
  return {
    input: $("#inj-input").value.trim(),
    output: $("#inj-output").value.trim(),
    suffix: $("#inj-suffix").value.trim() || "_comfy",
    kinds: $("#inj-kinds").value.trim() || "model,lora",
    dry_run: dryRun,
    overwrite: $("#inj-overwrite").checked,
    include_disabled: $("#inj-disabled").checked,
    allow_api: $("#inj-api").checked,
    fix_prompt: $("#inj-fix-prompt").checked,
    fix_values: $("#inj-fix-values").checked,
  };
}

async function startInject(dryRun) {
  const params = injectParams(dryRun);
  if (!params.input) {
    toast("请先填写输入目录", true);
    return;
  }
  S.lastInject = params;
  await startJob("inject", params);
}

function renderInjectResult(result) {
  const summary = [
    `总计 ${result.total ?? 0}`,
    `成功 ${result.ok ?? 0}`,
    `跳过 ${result.skipped ?? 0}`,
    `失败 ${result.errors ?? 0}`,
    `提示词修复 ${result.prompt_fixed ?? 0}`,
  ].join(" · ");
  const mode = S.lastInject && S.lastInject.dry_run
    ? "预览（未写文件）" : "执行（已写输出）";
  $("#inj-summary").textContent = `${mode} · ${summary}`;
  const rows = (result.results || []).map((item) => {
    const st = INJECT_STATUS[item.status] || [item.status, ""];
    const pf = item.prompt_fix || {};
    const prompt = pf.repaired ? "已修复" : (pf.inlined ? `内联 ${pf.inlined}` : "—");
    const delta = (item.size_before && item.size_after)
      ? `${item.size_after - item.size_before >= 0 ? "+" : ""}${item.size_after - item.size_before} B`
      : "—";
    const reason = item.reason
      || (item.unresolved && item.unresolved.length
        ? item.unresolved.map((u) => `${u[0]} → ${u[1]}`).join("；")
        : "");
    return `<tr>
      <td><span class="ellipsis" title="${esc(item.file)}">${esc(item.file)}</span></td>
      <td><span class="status ${st[1]}">${esc(st[0])}</span></td>
      <td>${(item.airs || []).length}</td>
      <td>${esc(prompt)}</td>
      <td class="mono">${esc(delta)}</td>
      <td><span class="ellipsis" title="${esc(reason)}">${esc(reason)}</span></td>
    </tr>`;
  });
  $("#inj-rows").innerHTML = rows.join("") ||
    `<tr><td colspan="6" class="hint">没有结果</td></tr>`;
}

// ---------------------------------------------------------------------------
// 目录选择
// ---------------------------------------------------------------------------
async function openBrowse(inputId) {
  const input = document.getElementById(inputId);
  let current = (input && input.value.trim()) || "";
  const render = async (path) => {
    const data = await api("browse?path=" + enc(path));
    const dirs = data.dirs || [];
    const html = `
      <h3>选择目录</h3>
      <div class="mono hint">${esc(data.path)}</div>
      <div class="dir-list">
        ${data.parent ? `<button data-go="${esc(data.parent)}">⬆ 上一级</button>` : ""}
        ${dirs.map((name) => {
          const next = data.path.replace(/[\\/]+$/, "") +
            (data.path.includes("\\") ? "\\" : "/") + name;
          return `<button data-go="${esc(next)}">📁 ${esc(name)}</button>`;
        }).join("")}
      </div>
      <div class="modal-actions">
        <button data-close>取消</button>
        <button class="primary" data-pick>选择当前目录</button>
      </div>`;
    const modal = openModal(html);
    modal.querySelectorAll("[data-go]").forEach((button) => {
      button.addEventListener("click", () => render(button.dataset.go));
    });
    modal.querySelector("[data-pick]").addEventListener("click", () => {
      if (input) {
        input.value = data.path;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      closeModal();
    });
  };
  try {
    await render(current);
  } catch (error) {
    toast("浏览目录失败：" + error.message, true);
  }
}

// ---------------------------------------------------------------------------
// 设置
// ---------------------------------------------------------------------------
const CATEGORY_LABELS = {
  diffusion_models: "diffusion_models",
  checkpoints: "checkpoints",
  loras: "loras",
  vae: "vae",
  text_encoders: "text_encoders",
  controlnet: "controlnet",
  upscale_models: "upscale_models",
};

const EXTENSION_OPTIONS = [".safetensors", ".ckpt", ".pt", ".pth", ".bin",
  ".gguf", ".sft", ".onnx", ".pkl"];

function joinPath(root, child) {
  if (!root) return "";
  return String(root).replace(/[\\/]+$/, "")
    + (String(root).includes("\\") ? "\\" : "/") + child;
}

function defaultModelsRoot() {
  if (S.state && S.state.models_root) return S.state.models_root;
  if (S.config && S.config.comfyui_root) return joinPath(S.config.comfyui_root, "models");
  return "";
}

function updateModelsPreview() {
  const root = $("#cfg-comfyui-root") ? $("#cfg-comfyui-root").value.trim() : "";
  const models = root ? joinPath(root, "models") : "";
  if ($("#cfg-models-root")) $("#cfg-models-root").value = models;
}

function applyModelPathDefault(force = false) {
  const root = defaultModelsRoot();
  if (!root) return;
  S.lastModelsRoot = root;
  if (!force && S.pathCleared) return;
  if (!force && S.pathInitialized) return;
  const input = $("#f-path");
  if (!input) return;
  input.value = root;
  S.filters.path = root;
  S.pathInitialized = true;
  renderTable();
}

async function loadConfig() {
  try {
    const data = await api("config");
    S.config = data.config;
    renderConfig();
    applyModelPathDefault();
  } catch (error) {
    toast("读取设置失败：" + error.message, true);
  }
}

function renderConfig() {
  if (!S.config) return;
  const cfg = S.config;
  $("#cfg-db").value = cfg.db_path || "";
  $("#cfg-api").value = cfg.api_base || "";
  $("#cfg-comfyui-root").value = cfg.comfyui_root || "";
  updateModelsPreview();
  $("#cfg-timeout").value = cfg.request_timeout ?? 20;
  $("#cfg-retries").value = cfg.query_retries ?? 3;
  $("#cfg-delay").value = cfg.query_delay ?? 1;
  $("#cfg-429").value = cfg.max_consecutive_429 ?? 3;
  $("#cfg-hash-cache").value = cfg.hash_cache || "";
  $("#cfg-seed-cache").value = cfg.seed_hash_cache_from || "";
  $("#cfg-backup-keep").value = cfg.backup_keep ?? 10;
  $("#cfg-output-subdir").value = (cfg.inject && cfg.inject.output_subdir) || "add_data";
  $("#cfg-suffix").value = (cfg.inject && cfg.inject.suffix) || "_comfy";
  $("#cfg-kinds").value = (cfg.inject && cfg.inject.kinds) || "model,lora";
  $("#cfg-allow-remote").checked = Boolean(cfg.allow_remote);

  const scan = cfg.scan || {};
  const cats = new Set(scan.categories || []);
  $("#cfg-categories").innerHTML = Object.entries(CATEGORY_LABELS)
    .map(([value, label]) => `<label><input type="checkbox" data-cat="${value}" ${cats.has(value) ? "checked" : ""}> ${label}</label>`)
    .join("");

  const exts = new Set(scan.extensions || []);
  $("#cfg-extensions").innerHTML = EXTENSION_OPTIONS
    .map((value) => `<label><input type="checkbox" data-ext="${value}" ${exts.has(value) ? "checked" : ""}> ${value}</label>`)
    .join("");
  const custom = [...exts].filter((value) => !EXTENSION_OPTIONS.includes(value));
  $("#cfg-ext-custom").value = custom.join(",");
  $("#cfg-extra-folders").value = (scan.extra_folders || []).join("\n");
}

async function saveConfig() {
  if (!S.config) return;
  const cfg = JSON.parse(JSON.stringify(S.config));
  const oldDb = cfg.db_path;
  const oldModelsRoot = S.lastModelsRoot;
  cfg.db_path = $("#cfg-db").value.trim();
  if (oldDb !== cfg.db_path && !confirm("数据库路径已改变，保存后会立即加载新库。继续？")) return;
  cfg.api_base = $("#cfg-api").value.trim() || "https://civitai.red";
  cfg.comfyui_root = $("#cfg-comfyui-root").value.trim();
  cfg.request_timeout = Number($("#cfg-timeout").value) || 20;
  cfg.query_retries = Number($("#cfg-retries").value) || 0;
  cfg.query_delay = Number($("#cfg-delay").value) || 0;
  cfg.max_consecutive_429 = Number($("#cfg-429").value) || 3;
  cfg.hash_cache = $("#cfg-hash-cache").value.trim();
  cfg.seed_hash_cache_from = $("#cfg-seed-cache").value.trim();
  cfg.backup_keep = Number($("#cfg-backup-keep").value) || 0;
  cfg.allow_remote = $("#cfg-allow-remote").checked;
  cfg.inject = cfg.inject || {};
  cfg.inject.output_subdir = $("#cfg-output-subdir").value.trim() || "add_data";
  cfg.inject.suffix = $("#cfg-suffix").value.trim() || "_comfy";
  cfg.inject.kinds = $("#cfg-kinds").value.trim() || "model,lora";
  cfg.scan = cfg.scan || {};
  cfg.scan.categories = $$("#cfg-categories [data-cat]")
    .filter((el) => el.checked).map((el) => el.dataset.cat);
  const exts = $$("#cfg-extensions [data-ext]")
    .filter((el) => el.checked).map((el) => el.dataset.ext);
  for (const value of $("#cfg-ext-custom").value.split(",")) {
    const text = value.trim().toLowerCase();
    if (text && !exts.includes(text)) exts.push(text);
  }
  cfg.scan.extensions = exts;
  cfg.scan.extra_folders = splitLines($("#cfg-extra-folders").value);
  try {
    const data = await api("config", {
      method: "PUT", body: JSON.stringify({ config: cfg }),
    });
    S.config = data.config;
    renderConfig();
    await loadAll();
    if (!S.filters.path || S.filters.path === oldModelsRoot) {
      S.pathCleared = false;
      S.pathInitialized = false;
      applyModelPathDefault(true);
    }
    toast("设置已保存");
  } catch (error) {
    toast("保存设置失败：" + error.message, true);
  }
}

// ---------------------------------------------------------------------------
// 体检
// ---------------------------------------------------------------------------
async function openHealth() {
  try {
    const data = await api("health");
    const issues = data.issues || [];
    const counts = issues.reduce((acc, issue) => {
      acc[issue.severity] = (acc[issue.severity] || 0) + 1;
      return acc;
    }, {});
    const html = `
      <h3>数据库体检</h3>
      <div class="hint">错误 ${counts.error || 0} · 警告 ${counts.warn || 0} · 提示 ${counts.info || 0}</div>
      <div style="margin-top:10px;max-height:55vh;overflow:auto;border:1px solid var(--border);border-radius:6px">
        ${issues.length ? issues.map((issue) => `<div class="issue">
          <span class="sev ${esc(issue.severity)}">[${esc(issue.severity)}]</span>
          <b>${esc(shortKey(issue.key))}</b> ${esc(issue.message)}
          <div class="hint">${esc(issue.suggestion || "")}</div>
        </div>`).join("") : `<div class="issue hint">没有发现问题。</div>`}
      </div>
      <div class="modal-actions"><button data-close>关闭</button></div>`;
    openModal(html);
  } catch (error) {
    toast("体检失败：" + error.message, true);
  }
}

// ---------------------------------------------------------------------------
// 事件绑定
// ---------------------------------------------------------------------------
function bind() {
  $$("nav.tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      $$("nav.tabs button").forEach((b) => b.classList.toggle("active", b === button));
      for (const tab of ["models", "inject", "settings"]) {
        const section = document.getElementById("tab-" + tab);
        if (section) section.hidden = tab !== button.dataset.tab;
      }
    });
  });
  if (location.hash === "#inject") {
    const button = $('nav.tabs button[data-tab="inject"]');
    if (button) button.click();
  }

  $("#btn-refresh").addEventListener("click", loadAll);
  $("#btn-health").addEventListener("click", openHealth);

  $("#f-q").addEventListener("input", (event) => {
    S.filters.q = event.target.value; S.page = 1; renderTable();
  });
  $("#f-type").addEventListener("change", (event) => {
    S.filters.type = event.target.value; S.page = 1; renderTable();
  });
  $("#f-base").addEventListener("change", (event) => {
    S.filters.base = event.target.value; S.page = 1; renderTable();
  });
  $("#f-status").addEventListener("change", (event) => {
    S.filters.status = event.target.value; S.page = 1; renderTable();
  });
  $("#f-path").addEventListener("input", (event) => {
    S.filters.path = event.target.value;
    S.pathInitialized = true;
    S.pathCleared = false;
    S.page = 1;
    renderTable();
  });
  $("#btn-path-browse").addEventListener("click", () => openBrowse("f-path"));
  $("#btn-path-clear").addEventListener("click", () => {
    S.pathCleared = true;
    S.pathInitialized = true;
    S.filters.path = "";
    $("#f-path").value = "";
    S.page = 1;
    renderTable();
  });
  $("#f-existing").addEventListener("change", (event) => {
    S.filters.existing = event.target.checked; S.page = 1; renderTable();
  });

  $$("#tab-models thead th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (S.sort.key === key) S.sort.dir *= -1;
      else { S.sort.key = key; S.sort.dir = 1; }
      renderTable();
    });
  });

  $("#rows").addEventListener("click", (event) => {
    const checkbox = event.target.closest("input[data-select]");
    if (checkbox) {
      toggleSelect(checkbox.dataset.select, checkbox.checked);
      return;
    }
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const tr = button.closest("tr[data-key]");
    const row = tr && byKey(tr.dataset.key);
    if (!row) return;
    if (button.dataset.act === "edit") openEditor(row.key);
    else if (button.dataset.act === "url") window.open(row.url, "_blank", "noopener");
    else if (button.dataset.act === "menu") {
      const rect = button.getBoundingClientRect();
      showContextMenu(rect.left, rect.bottom, row);
    }
  });

  $("#rows").addEventListener("contextmenu", (event) => {
    const tr = event.target.closest("tr[data-key]");
    if (!tr) return;
    event.preventDefault();
    const row = byKey(tr.dataset.key);
    if (row) showContextMenu(event.clientX, event.clientY, row);
  });

  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".ctx")) hideContextMenu();
  });
  document.addEventListener("scroll", hideContextMenu, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hideContextMenu();
      closeModal();
    }
  });

  $("#check-all").addEventListener("change", (event) => {
    for (const key of pageKeys()) toggleSelect(key, event.target.checked);
  });
  $("#page-prev").addEventListener("click", () => { S.page -= 1; renderTable(); });
  $("#page-next").addEventListener("click", () => { S.page += 1; renderTable(); });
  $("#page-size").addEventListener("change", (event) => {
    S.pageSize = Number(event.target.value) || 50; S.page = 1; renderTable();
  });

  $("#btn-scan").addEventListener("click", openScanModal);
  $("#btn-verify").addEventListener("click", () => startJob("verify"));
  $("#btn-hash-inc").addEventListener("click", () => startJob("hash_inc"));
  $("#btn-hash-full").addEventListener("click", () => {
    if (!confirm("全量校验会遍历所有有路径的记录；命中缓存时不会重读文件。继续？")) return;
    startJob("hash_full");
  });
  const queryLimit = () => Number($("#query-limit").value) || 0;
  $("#btn-query-new").addEventListener("click", () => startJob("query_new", { limit: queryLimit() }));
  $("#btn-query-nf").addEventListener("click", () => startJob("query_not_found", { limit: queryLimit() }));
  $("#btn-query-fail").addEventListener("click", () => startJob("query_failed", { limit: queryLimit() }));

  $("#btn-bulk-query").addEventListener("click", () => {
    const selected = [...S.selected];
    if (!selected.length) return;
    startJob("query_selected", { selected, limit: selected.length });
  });
  $("#btn-bulk-delete").addEventListener("click", async () => {
    const selected = [...S.selected];
    if (!selected.length) return;
    if (!confirm(`确定删除选中的 ${selected.length} 条记录吗？`)) return;
    for (const key of selected) {
      try { await api("entry/" + enc(key), { method: "DELETE" }); }
      catch (error) { toast(`删除 ${shortKey(key)} 失败：${error.message}`, true); }
    }
    S.selected.clear();
    await loadAll();
  });

  $("#job-cancel").addEventListener("click", async () => {
    if (!S.jobId) return;
    try { await api("jobs/" + enc(S.jobId) + "/cancel", { method: "POST" }); }
    catch (error) { toast(error.message, true); }
  });
  $("#job-hide").addEventListener("click", () => { $("#job-panel").hidden = true; });

  $("#inj-preview").addEventListener("click", () => startInject(true));
  $("#inj-run").addEventListener("click", () => startInject(false));
  $$("[data-browse]").forEach((button) => {
    button.addEventListener("click", () => openBrowse(button.dataset.browse));
  });

  $("#btn-save-config").addEventListener("click", saveConfig);
  $("#btn-reload-config").addEventListener("click", loadConfig);
  $("#cfg-comfyui-root").addEventListener("input", updateModelsPreview);
  $("#btn-detect-comfyui").addEventListener("click", () => {
    const detected = (S.state && S.state.comfyui_root)
      || (S.config && S.config.comfyui_root) || "";
    $("#cfg-comfyui-root").value = detected;
    updateModelsPreview();
    toast(detected ? "已填入从插件安装位置推断的根目录" : "没有推断出 ComfyUI 根目录，请手动填写");
  });
}

async function init() {
  bind();
  await loadAll();
  await loadConfig();
}

document.addEventListener("DOMContentLoaded", init);
