// 照片搜索界面。在 Tauri 窗口里通过 invoke 取得后端地址；用浏览器直接打开时走同源后端（调试用）。
"use strict";

const tauri = window.__TAURI__;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const PAGE = 120;
const EXAMPLES = msg("examples"); // 示例搜索词，随界面语言

const app = {
  api: "",
  thumbs: null, // Tauri 里缩略图目录，用 asset 协议直接读；浏览器调试时为空，走后端 /thumb
  backendDown: false,
  status: null, // 最近一次 /api/status
  kind: "all", // 全部 / 照片 / 截图
  time: null, // 时间筛选 {start, end, label}（秒级时间戳，左闭右开）
  view: null, // 当前列表 {mode, label, path, kind, results, hasMore, total, tookMs, loading}
  stack: [], // 可返回的上一层列表
  pending: null, // 模型就绪前提交的搜索，就绪后自动执行
  current: -1, // 预览中的序号（在当前列表里）
  shown: null, // 预览中正在显示的照片；看连拍组里的其他张时与 current 不同
  selected: new Map(), // 多选：列表序号 → 照片；换列表时清空
  lastPick: null, // Shift 连选的起点
};
const HISTORY_KEY = "recentQueries";
const PREFS_KEY = "prefs";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* 界面偏好（筛选范围等）只存在本机；读写失败时用默认值 */
function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
  } catch {
    return {};
  }
}

function savePrefs(patch) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ ...loadPrefs(), ...patch }));
  } catch {
    // 存不了就不记
  }
}

/* ---------- 后端通信 ---------- */

async function resolveApi() {
  if (!tauri) {
    app.api = new URLSearchParams(location.search).get("api") || location.origin;
    return;
  }
  for (;;) {
    const info = await tauri.core.invoke("backend_url"); // 后端启动失败或已退出时会抛出错误
    if (info) {
      app.api = info.url;
      app.thumbs = info.thumbs;
      return;
    }
    const progress = await tauri.core.invoke("setup_progress"); // 首次运行：外壳正在用 uv 准备 Python 环境
    if (progress) showSetup(progress);
    await sleep(progress ? 1000 : 300);
  }
}

/* 首次运行时在列表区域显示准备进度：要下载 Python、PyTorch 等约 3 GB，只此一次 */
let setupStarted = 0;

function showSetup(progress) {
  setupStarted ||= Date.now();
  let card = $("#grid .setup");
  if (!card) {
    card = Object.assign(document.createElement("div"), { className: "welcome setup" });
    card.append(
      Object.assign(document.createElement("h2"), { textContent: t("setup.title") }),
      Object.assign(document.createElement("p"), { textContent: t("setup.text") }),
      Object.assign(document.createElement("p"), { className: "setup-line" }),
    );
    $("#grid").replaceChildren(card);
    $("#summary").replaceChildren();
  }
  const secs = Math.round((Date.now() - setupStarted) / 1000);
  const elapsed = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
  const detail = progress.downloading.length
    ? t("setup.downloading", { items: progress.downloading.join(t("common.list_sep")) }) : progress.line;
  card.querySelector(".setup-line").textContent = detail ? `${detail} · ${elapsed}` : elapsed;
  setStatus("starting", t("setup.status"));
}

function thumbUrl(id) {
  if (!app.thumbs) return `${app.api}/thumb/${id}`;
  const sep = app.thumbs.includes("\\") ? "\\" : "/"; // Windows 路径用反斜杠，其他系统用斜杠
  return tauri.core.convertFileSrc(`${app.thumbs}${sep}${id}.jpg`);
}

async function getJSON(path, params = {}) {
  const url = new URL(app.api + path);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  let res;
  try {
    res = await fetch(url);
  } catch {
    backendDown({ code: "unreachable" });
    throw new Error(t("backend.unreachable"));
  }
  return readJSON(res);
}

async function readJSON(res) {
  const data = await res.json().catch(() => ({}));
  if (res.ok) return data;
  const err = new Error(errorText(data.detail) || res.statusText);
  err.code = data.detail?.code; // 个别错误要区别处理，例如以图搜图过期了就重新上传
  throw err;
}

/* 后端报的错误是 {code, ...参数}，按当前语言显示；FastAPI 参数校验失败时是列表 */
function errorText(detail) {
  if (Array.isArray(detail)) return t("err.invalid_request");
  if (detail && typeof detail === "object") return t(`err.${detail.code}`, detail);
  return detail || "";
}

function warningText(w) {
  return w ? t(`warn.${w.code}`, { ...w, dirs: (w.dirs || []).join(t("common.list_sep")) }) : "";
}

function setStatus(state, text, title = "") {
  $("#status").dataset.state = state;
  $("#status").title = title;
  $("#status-text").textContent = text;
}

let polling = false;
let pollUntil = 0; // 刚发起扫描时后台任务可能还没登记，至少再观察这么久

function startPolling(minMs = 0) {
  pollUntil = Math.max(pollUntil, Date.now() + minMs);
  if (!polling) {
    polling = true;
    pollStatus();
  }
}

async function pollStatus() {
  let s;
  try {
    s = await getJSON("/api/status");
  } catch {
    polling = false; // 后台服务不在了：横幅里可以重启，重启后会重新开始轮询
    return;
  }
  const prev = app.status;
  app.status = s;
  if (s.phase === "error") {
    const reason = errorText(s.error);
    setStatus("error", t("status.startup_failed"), reason); // 完整原因放横幅里，顶栏只放简短状态
    showBanner(reason);
  } else if (s.phase !== "ready") {
    const downloading = s.stage === "downloading_model" && s.download; // 首次下载模型时显示已下载多少
    setStatus("starting", `${downloading ? t("stage.downloading_model_progress", { size: formatBytes(s.download) })
      : t(`stage.${s.stage}`)}…`);
  } else if (s.task) {
    setStatus("busy", t(`task.${s.task.stage}`, s.task));
  } else {
    const warning = warningText(s.warning);
    const ready = t(s.screenshots ? "status.ready" : "status.ready_no_shots", // 没有截图时不写“0 张截图”
      { photos: t("count.photos", { n: s.count }), shots: t("count.screenshots", { n: s.screenshots }) });
    setStatus(warning ? "busy" : "ready", ready + (warning ? t("status.has_warning") : ""), warning);
  }
  if (s.browsable && !prev?.browsable) goHome();
  if (s.searchable && app.pending) {
    const spec = app.pending;
    app.pending = null;
    if (spec.mode === "image" && !spec.path) runImageSearch(spec); // 以图搜图要先上传图片
    else openView(spec);
  }
  // 新照片加入索引（编码过程中每约 1000 张、或扫描结束）：在首页顶部就直接刷新，翻到下面了就只提示
  if (prev && (s.count !== prev.count || (prev.task && !s.task)) && app.view?.mode === "recent" && !app.stack.length) {
    if (!app.view.results.length || window.scrollY < 200) reloadView();
    else if (s.count !== prev.count) $("#refresh-note").hidden = false;
  }
  if (prev?.task && !s.task && settings.open) renderLibrary();
  const busy = s.phase !== "ready" || s.task || Date.now() < pollUntil;
  if (s.phase !== "error" && busy) setTimeout(pollStatus, 800);
  else polling = false;
}

/* ---------- 列表：首页、搜索、找相似 ---------- */

/* 底部横幅：说明后台服务的问题，Tauri 里可一键重启 */
function showBanner(text) {
  $("#banner-text").textContent = text;
  $("#banner-action").hidden = !tauri;
  if (!$("#banner").matches(":popover-open")) $("#banner").showPopover();
}

/* 外壳报来的错误是 {code, detail}（见 src-tauri/src/lib.rs），按当前语言显示 */
function backendErrorText(e) {
  if (e && typeof e === "object" && e.code) return t(`backend.${e.code}`, { detail: e.detail ?? "" });
  return String(e?.message ?? e);
}

/* 后台服务退出（崩溃），或一开始就没能启动（例如还没运行 uv sync） */
function backendDown(err) {
  if (app.backendDown) return;
  app.backendDown = true;
  const reason = backendErrorText(err);
  const started = Boolean(app.status); // 连上过：已加载的缩略图还能看
  // 顶栏只放简短状态，完整原因在横幅里
  setStatus("error", t(started ? "status.backend_stopped" : "status.startup_failed"), reason);
  if (!tauri) showBanner(t("banner.down_browser", { reason }));
  else showBanner(t(started ? "banner.down_started" : "banner.down_never", { reason }));
}

async function restartBackend() {
  $("#banner-action").disabled = true;
  $("#banner-text").textContent = t("banner.restarting");
  try {
    await tauri.core.invoke("restart_backend");
    await resolveApi();
  } catch (e) {
    $("#banner-text").textContent = t("banner.restart_failed", { reason: backendErrorText(e) });
    $("#banner-action").disabled = false;
    return;
  }
  app.backendDown = false;
  app.status = null; // 重新就绪后回到首页
  $("#banner").hidePopover();
  $("#banner-action").disabled = false;
  startPolling();
}

function openView(spec, push = true) {
  $("#refresh-note").hidden = true;
  clearSelection();
  if (push && app.view) {
    app.view.scrollY = window.scrollY;
    app.stack.push(app.view);
    if (app.stack.length > 30) app.stack.shift();
  }
  app.view = { ...spec, kind: app.kind, time: app.time, results: [], hasMore: true, total: null, tookMs: null, loading: false };
  loadPage(true);
}

function goHome() {
  app.stack = [];
  openView({ mode: "recent", label: t("summary.all"), path: "/api/recent" }, false);
}

function reloadView() {
  const v = app.view;
  if (v) openView({ mode: v.mode, query: v.query, label: v.label, path: v.path, source: v.source, image: v.image, cat: v.cat }, false);
}

function back() {
  if (!app.stack.length || viewer.open || settings.open) return;
  clearSelection();
  app.view = app.stack.pop();
  app.view.loading = false;
  app.kind = app.view.kind;
  app.time = app.view.time;
  syncKindButtons();
  syncTimeButton();
  app.pending = null;
  $("#q").value = app.view.query || ""; // 搜索框跟着回到那一层的关键词
  render(true);
  $("#grid").classList.remove("stale");
  window.scrollTo(0, app.view.scrollY || 0);
  maybeLoadMore();
}

function requestView(spec) {
  if (app.status?.searchable) {
    openView(spec);
  } else {
    app.pending = spec; // 模型还没就绪：先记下，就绪后自动执行
    updateToolbar();
  }
}

function submitQuery(raw) {
  const text = raw.trim();
  if (!text) return;
  hideSuggest();
  remember(text);
  // 搜索词里的时间说法拆出来当作时间筛选，剩下的才是画面描述
  let q = text;
  const parsed = parseTime(text);
  if (parsed) {
    app.time = parsed.range;
    syncTimeButton();
    q = parsed.rest;
    $("#q").value = q;
  }
  if (!q) {
    openView({ mode: "recent", label: t("summary.all"), path: "/api/recent" }); // 只说了时间：浏览这段时间的照片
    return;
  }
  requestView({ mode: "search", query: q, label: t("summary.query", { q }), path: `/api/search?q=${encodeURIComponent(q)}` });
}

function findSimilar(r) {
  if (viewer.open) viewer.close();
  requestView({ mode: "similar", label: t("summary.similar_to", { name: r.name }), path: `/api/similar/${r.id}` });
}

const dupSpec = (cat = "all") => ({ mode: "dups", label: t("summary.dups"), path: "/api/duplicates", cat });

function openDups() {
  if (app.view?.mode === "dups" && !app.pending) return;
  requestView(dupSpec());
}

/* ---------- 以图搜图：选图、拖进窗口、粘贴 ---------- */

const IMAGE_EXT = /\.(jpe?g|png|webp|bmp|gif|heic|heif|tiff?)$/i;
const baseName = (path) => path.split(/[\\/]/).pop();

/* source 是 {path, name}（对话框里选的、拖进窗口的文件）或 {blob, name}（粘贴的、浏览器里选的） */
function searchByImage(source) {
  for (const d of [viewer, settings, help]) if (d.open) d.close();
  const label = source.name ? t("summary.similar_to", { name: source.name }) : t("summary.similar_to_pasted");
  const spec = { mode: "image", label, source };
  if (app.status?.searchable) {
    runImageSearch(spec);
  } else {
    app.pending = spec; // 模型还没就绪：就绪后自动上传并搜索
    updateToolbar();
  }
}

let imageSeq = 0; // 连着拖入几张图时只认最后一张，先发的上传晚回来也不覆盖

async function runImageSearch(spec) {
  const seq = ++imageSeq;
  let slow = false;
  const timer = setTimeout(() => { // 一般 0.1 秒左右就好，慢的时候才提示
    slow = true;
    toast(t("image.reading"), { sticky: true });
  }, 400);
  try {
    spec.image = await postImage(spec.source);
  } catch (e) {
    clearTimeout(timer);
    if (seq !== imageSeq) return;
    toast(t("image.failed", { error: e.message }));
    updateToolbar();
    return;
  }
  clearTimeout(timer);
  if (seq !== imageSeq) return;
  if (slow) hideToast();
  $("#q").value = "";
  openView({ ...spec, path: `/api/image-search/${spec.image.qid}` });
}

async function postImage(source) {
  let res;
  try {
    res = await fetch(`${app.api}/api/image-query`, source.path
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: source.path }) }
      : { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: source.blob });
  } catch {
    throw new Error(t("err.unreachable"));
  }
  return readJSON(res);
}

async function pickImage() {
  if (!tauri?.dialog) return $("#img-file").click();
  const path = await tauri.dialog.open({
    title: t("image.choose"),
    filters: [{ name: t("image.filter"), extensions: ["jpg", "jpeg", "png", "webp", "bmp", "gif", "heic", "heif", "tif", "tiff"] }],
  });
  if (path) searchByImage({ path, name: baseName(path) });
}

const drop = $("#drop");

function showDrop(ok) {
  drop.classList.toggle("bad", !ok);
  $("#drop-text").textContent = t(ok ? "image.drop" : "image.drop_bad");
  if (!drop.matches(":popover-open")) drop.showPopover();
}

function hideDrop() {
  if (drop.matches(":popover-open")) drop.hidePopover();
}

function setupImageDrop() {
  if (tauri) {
    // Tauri 窗口里文件拖放由外壳接管（网页收不到 drop 事件），这里拿到的是文件路径
    tauri.webview.getCurrentWebview().onDragDropEvent(({ payload: p }) => {
      if (p.type === "enter") {
        showDrop(p.paths.some((x) => IMAGE_EXT.test(x)));
      } else if (p.type === "leave") {
        hideDrop();
      } else if (p.type === "drop") {
        hideDrop();
        const path = p.paths.find((x) => IMAGE_EXT.test(x));
        if (path) searchByImage({ path, name: baseName(path) });
        else if (p.paths.length) toast(t("image.not_image"));
      }
    }).catch(() => {});
    return;
  }
  // 浏览器调试：用网页的拖放事件。进出子元素时 dragenter/dragleave 成对触发，计数判断是否真的离开了窗口
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer?.types.includes("Files");
  document.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    const items = [...e.dataTransfer.items];
    if (depth++ === 0) showDrop(items.some((it) => it.kind === "file" && (!it.type || it.type.startsWith("image/"))));
  });
  document.addEventListener("dragover", (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
  document.addEventListener("dragleave", (e) => {
    if (hasFiles(e) && --depth <= 0) {
      depth = 0;
      hideDrop();
    }
  });
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    hideDrop();
    const file = [...e.dataTransfer.files].find((f) => f.type.startsWith("image/") || IMAGE_EXT.test(f.name));
    if (file) searchByImage({ blob: file, name: file.name });
    else toast(t("image.not_image"));
  });
}

/* 最近搜索：只存在本机（WebView 的 localStorage），读写失败时当作没有记录 */
function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
  } catch {
    return [];
  }
}

function saveHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
  } catch {
    // 存不了就不记，不影响搜索
  }
  renderChips();
}

function remember(q) {
  saveHistory([q, ...loadHistory().filter((x) => x !== q)].slice(0, 8));
}

function chip(text) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "chip";
  b.textContent = text;
  b.addEventListener("click", () => {
    $("#q").value = text;
    submitQuery(text);
  });
  return b;
}

function renderChips() {
  const history = loadHistory();
  $("#history-row").hidden = !history.length;
  $("#history-chips").replaceChildren(...history.map(chip));
  $("#chips").replaceChildren(...EXAMPLES.filter((q) => !history.includes(q)).map(chip));
}

/* ---------- 时间筛选（解析规则在 time.js） ---------- */

function syncTimeButton() {
  $("#time-btn").textContent = app.time ? app.time.label : t("time.any");
  $("#time-btn").classList.toggle("active", Boolean(app.time));
  $("#time-clear").hidden = !app.time;
}

function setTime(range) {
  app.time = range;
  syncTimeButton();
  if ($("#time-menu").matches(":popover-open")) $("#time-menu").hidePopover();
  reloadView();
}

function menuItem(text, onPick, { current = false, count = null } = {}) {
  const b = Object.assign(document.createElement("button"), { type: "button", className: "item", textContent: text });
  b.setAttribute("role", "menuitem");
  b.classList.toggle("current", current);
  if (count != null) {
    b.append(Object.assign(document.createElement("span"), { className: "count", textContent: t("time.year_count", { n: count }) }));
  }
  b.addEventListener("click", onPick);
  return b;
}

function renderTimeMenu() {
  const label = app.time?.label;
  const recent = (days, key) => menuItem(t(key), () => setTime(lastDays(days, t(key))), { current: label === t(key) });
  const nodes = [
    menuItem(t("time.any"), () => setTime(null), { current: !app.time }),
    recent(30, "time.last_30_days"),
    recent(365, "time.last_12_months"),
  ];
  const years = app.status?.years || [];
  if (years.length) {
    nodes.push(Object.assign(document.createElement("div"), { className: "sep" }));
    nodes.push(Object.assign(document.createElement("div"), { className: "label", textContent: t("time.by_year") }));
    for (const [year, count] of years) {
      const text = t("time.year", { y: year });
      nodes.push(menuItem(text, () => setTime(yearRange(year)), { current: label === text, count }));
    }
  }
  nodes.push(Object.assign(document.createElement("div"), { className: "label", textContent: t("time.hint") }));
  $("#time-menu").replaceChildren(...nodes);
}

/* ---------- 搜索框下拉建议 ---------- */

const pop = $("#suggest-pop");
let popRows = []; // 可选的行 {el, text}
let popIndex = -1;

function hideSuggest() {
  if (pop.matches(":popover-open")) pop.hidePopover();
  popIndex = -1;
}

function pickSuggest(text) {
  $("#q").value = text;
  submitQuery(text);
}

function suggestRow(text, removable) {
  const b = Object.assign(document.createElement("button"), { type: "button", className: "item", tabIndex: -1 });
  b.setAttribute("role", "option");
  b.append(Object.assign(document.createElement("span"), { textContent: text }));
  // 用 pointerdown 而不是 click：在输入框失去焦点、下拉关闭之前就处理
  b.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    pickSuggest(text);
  });
  if (removable) {
    const x = Object.assign(document.createElement("span"), { className: "remove", textContent: "×", title: t("suggest.remove") });
    x.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      saveHistory(loadHistory().filter((h) => h !== text));
      renderSuggest();
    });
    b.append(x);
  }
  popRows.push({ el: b, text });
  return b;
}

function renderSuggest() {
  const q = $("#q").value.trim();
  const history = loadHistory().filter((h) => !q || h.includes(q)).slice(0, 8);
  const examples = EXAMPLES.filter((e) => !history.includes(e) && (!q || e.includes(q))).slice(0, 5);
  const parsed = q ? parseTime(q) : null;
  popRows = [];
  popIndex = -1;
  const nodes = [];
  if (parsed) {
    const { label } = parsed.range;
    nodes.push(Object.assign(document.createElement("div"), {
      className: "hint-row",
      textContent: parsed.rest ? t("suggest.time_search", { label, rest: parsed.rest }) : t("suggest.time_browse", { label }),
    }));
  }
  if (history.length) {
    nodes.push(Object.assign(document.createElement("div"), { className: "label", textContent: t("suggest.recent_head") }));
    nodes.push(...history.map((h) => suggestRow(h, true)));
  }
  if (examples.length) {
    nodes.push(Object.assign(document.createElement("div"), { className: "label", textContent: t("suggest.try_head") }));
    nodes.push(...examples.map((e) => suggestRow(e, false)));
  }
  if (!nodes.length) return hideSuggest();
  pop.replaceChildren(...nodes);
  if (!pop.matches(":popover-open")) pop.showPopover();
}

function moveSuggest(delta) {
  if (!popRows.length) return;
  popIndex = (popIndex + delta + popRows.length) % popRows.length;
  popRows.forEach((r, i) => r.el.classList.toggle("active", i === popIndex));
  popRows[popIndex].el.scrollIntoView({ block: "nearest" });
}

async function loadPage(reset = false) {
  const v = app.view;
  if (!v || v.loading || (!reset && !v.hasMore)) return;
  v.loading = true;
  if (reset) $("#grid").classList.add("stale"); // 旧结果先变淡，新结果到了再替换，避免闪白
  updateToolbar();
  setSentinel();
  try {
    const params = { offset: v.results.length, limit: PAGE, kind: v.kind };
    if (v.time) Object.assign(params, { start: v.time.start, end: v.time.end });
    if (v.mode === "dups") params.cat = v.cat;
    let data;
    try {
      data = await getJSON(v.path, params);
    } catch (e) {
      // 以图搜图的查询编号过期了（后台服务重启过，或之后又搜了很多张图）：重新上传同一张图再查
      if (e.code !== "query_expired" || !v.source) throw e;
      v.image = await postImage(v.source);
      v.path = `/api/image-search/${v.image.qid}`;
      data = await getJSON(v.path, params);
    }
    if (app.view !== v) return;
    v.results.push(...data.results);
    v.hasMore = data.has_more;
    v.total = data.total ?? v.total;
    v.photos = data.photos ?? v.photos;
    v.tookMs = data.took_ms ?? v.tookMs;
    if (v.mode === "dups") Object.assign(v, { groups: data.groups, extra: data.extra, counts: data.counts });
  } catch (e) {
    if (app.view !== v) return;
    v.hasMore = false;
    v.error = e.message;
    if (v.results.length) toast(t("toast.load_failed", { error: e.message }));
  } finally {
    v.loading = false;
  }
  if (app.view !== v) return;
  render(reset);
  if (reset) {
    $("#grid").classList.remove("stale");
    window.scrollTo(0, 0);
  }
  maybeLoadMore();
}

function maybeLoadMore() {
  const v = app.view;
  if (v && v.hasMore && !v.loading && $("#sentinel").getBoundingClientRect().top < innerHeight + 1200) loadPage();
}

/* ---------- 渲染 ---------- */

let rendered = 0;
let lastGroup = null;

function monthOf(taken) {
  return taken ? fmtMonth(Number(taken.slice(0, 4)), Number(taken.slice(5, 7))) : t("grid.unknown_date");
}

const percent = (score) => `${Math.round(score * 100)}%`;

function makeTile(r, i) {
  const mode = app.view?.mode;
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "tile";
  tile.dataset.index = i;
  tile.title = r.name;
  const img = document.createElement("img");
  img.loading = "lazy";
  img.decoding = "async";
  img.draggable = false;
  img.alt = r.name;
  img.src = thumbUrl(r.id);
  const meta = document.createElement("span");
  meta.className = "meta";
  const date = (r.taken_at || "").slice(0, 10);
  if (mode === "dups") {
    // 比较重复照片时要看清晰度和在哪个文件夹（完全相同的两张文件名往往也一样），一直显示
    tile.title = r.path;
    meta.classList.add("always");
    const size = [r.width ? `${r.width}×${r.height}` : "", r.bytes ? formatBytes(r.bytes) : ""].filter(Boolean).join(" · ");
    const folder = r.path.split(/[\\/]/).slice(-2, -1)[0];
    meta.append(Object.assign(document.createElement("span"), { textContent: size }),
      Object.assign(document.createElement("span"), { className: "where", textContent: folder ? `${folder}${r.path.includes("\\") ? "\\" : "/"}${r.name}` : r.name }));
  } else {
    const scored = (mode === "similar" || mode === "image") && typeof r.score === "number";
    meta.textContent = scored ? [date, percent(r.score)].filter(Boolean).join(" · ") : date;
    if (scored && r.score >= 0.9) meta.classList.add("always"); // 很可能是同一张照片：不用悬停也显示相似度
  }
  const check = Object.assign(document.createElement("span"), { className: "check", title: t("grid.select") });
  tile.append(img, meta, check);
  tile.classList.toggle("selected", app.selected.has(i));
  if (mode === "dups") {
    const role = r.dup?.role;
    if (role === "keep" || role === "identical" || role === "near") {
      tile.append(Object.assign(document.createElement("span"), {
        className: `role ${role}`, textContent: t(`dups.role_${role}`), title: t(`dups.role_${role}_title`),
      }));
    }
    if (r.gone) markGone(tile);
    return tile;
  }
  if (r.count > 1) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.title = t("grid.burst_title", { n: r.count });
    badge.innerHTML = STACK_ICON;
    badge.append(String(r.count));
    tile.append(badge);
  }
  const similar = Object.assign(document.createElement("span"), { className: "similar-btn", title: t("grid.find_similar") });
  similar.innerHTML = SIMILAR_ICON;
  tile.append(similar);
  return tile;
}

const STACK_ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'
  + '<rect x="2" y="5" width="9" height="9" rx="1.5"/><path d="M5 2.5h7a1.5 1.5 0 0 1 1.5 1.5v7"/></svg>';
const SIMILAR_ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">'
  + '<circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/></svg>';

/* 重复照片页每组的标题：类别 · 张数 · 日期 · 可腾出的空间，有多余副本时可一键选中 */
function dupHead(r) {
  const h = r.dup.head;
  const box = Object.assign(document.createElement("div"), { className: "group dup-head" });
  const parts = [t("count.photos", { n: h.n })];
  const day = h.taken ? new Date(h.taken.replace(" ", "T")) : null;
  if (day && !Number.isNaN(day.getTime())) parts.push(fmtDay(day));
  if (h.extra) parts.push(t("dups.can_free", { size: formatBytes(h.extra) }));
  box.append(
    Object.assign(document.createElement("span"), { textContent: t(`dups.cat_${h.cat}`), title: t(`dups.cat_${h.cat}_title`) }),
    Object.assign(document.createElement("span"), { className: "dup-meta", textContent: parts.join(" · ") }),
  );
  if (h.extra) {
    const btn = Object.assign(document.createElement("button"), { type: "button", className: "link", textContent: t("dups.select_extras") });
    btn.dataset.group = r.dup.g;
    box.append(btn);
  }
  return box;
}

function selectExtras(g) {
  app.view.results.forEach((r, i) => {
    if (r.dup?.g === g && (r.dup.role === "identical" || r.dup.role === "near")) setSelected(i, true);
  });
  updateSelbar();
}

function render(reset) {
  const v = app.view;
  const grid = $("#grid");
  if (reset) {
    grid.replaceChildren();
    rendered = 0;
    lastGroup = null;
  }
  const frag = document.createDocumentFragment();
  for (; rendered < v.results.length; rendered++) {
    const r = v.results[rendered];
    if (v.mode === "recent") {
      const group = monthOf(r.taken_at);
      if (group !== lastGroup) {
        const h = document.createElement("h3");
        h.className = "group";
        h.textContent = group;
        frag.append(h);
        lastGroup = group;
      }
    } else if (v.mode === "dups" && r.dup?.head) {
      frag.append(dupHead(r));
    }
    frag.append(makeTile(r, rendered));
  }
  grid.append(frag);
  grid.querySelector(".message, .welcome")?.remove();
  if (!v.results.length && v.mode === "recent" && v.total === 0 && v.kind === "all") {
    grid.append(welcomeCard());
  } else if (!v.results.length && v.mode === "dups" && !v.error) {
    const box = Object.assign(document.createElement("div"), { className: "welcome" });
    box.append(Object.assign(document.createElement("h2"), { textContent: t("grid.no_dups") }),
      Object.assign(document.createElement("p"), { textContent: t("grid.no_dups_hint") }));
    grid.append(box);
  } else if (!v.results.length) {
    const p = document.createElement("p");
    p.className = "message";
    p.textContent = v.error ? t("grid.error", { error: v.error }) : t("grid.no_results");
    grid.append(p);
  }
  updateToolbar();
  setSentinel();
}

/* 图库还是空的：引导添加照片文件夹 */
function welcomeCard() {
  const box = document.createElement("div");
  box.className = "welcome";
  const h = Object.assign(document.createElement("h2"), { textContent: t("welcome.title") });
  const p = Object.assign(document.createElement("p"), { textContent: t("welcome.text") });
  const btn = Object.assign(document.createElement("button"), { type: "button", className: "btn primary", textContent: t("welcome.add") });
  btn.addEventListener("click", async () => {
    openSettings();
    addDir();
  });
  box.append(h, p, btn);
  return box;
}

function setSentinel() {
  const v = app.view;
  let text = "";
  if (v?.loading) text = v.results.length ? t("grid.loading") : "";
  else if (v && !v.hasMore && v.results.length && v.mode === "recent") text = t("grid.all_shown", { n: v.results.length });
  $("#sentinel").textContent = text;
}

/* 列表上方的说明；以图搜图时前面放查询图片的小图 */
function setSummary(label, detail, thumb) {
  const nodes = [];
  if (thumb) nodes.push(Object.assign(document.createElement("img"), { className: "q-thumb", src: thumb, alt: "" }));
  nodes.push(Object.assign(document.createElement("b"), { textContent: label }), detail ? ` · ${detail}` : "");
  $("#summary").replaceChildren(...nodes);
}

function updateToolbar() {
  const v = app.view;
  $("#back").hidden = !app.stack.length;
  $("#suggest").hidden = !(v && v.mode === "recent") || Boolean(app.pending) || app.status?.count === 0;
  const dups = v?.mode === "dups" && !app.pending;
  $("#dup-bar").hidden = !dups;
  $("#dups-btn").setAttribute("aria-pressed", String(dups || app.pending?.mode === "dups"));
  $("#dups-btn").hidden = app.status?.count === 0; // 图库还是空的
  if (dups) syncDupBar(v);
  if (app.pending) return setSummary(app.pending.label, t("summary.pending"));
  if (!v) return $("#summary").replaceChildren(t("summary.reading"));
  if (v.mode === "recent") {
    setSummary(t(`summary.${v.kind}`), v.photos != null ? t("summary.count", { n: v.photos }) : "");
  } else if (v.error && !v.results.length) {
    setSummary(v.label, t("summary.search_failed"), v.image?.preview);
  } else if (v.mode === "search") {
    setSummary(v.label, v.loading && !v.results.length ? t("summary.searching") : t("summary.by_relevance", { ms: v.tookMs ?? "-" }));
  } else if (v.mode === "dups") {
    const parts = v.groups == null ? [t("summary.dups_loading")] : [t("dups.groups", { n: v.groups }), t("count.photos", { n: v.total })];
    if (v.extra) parts.push(t("dups.can_free", { size: formatBytes(v.extra) }));
    setSummary(v.label, parts.join(" · "));
  } else {
    setSummary(v.label, v.loading && !v.results.length ? t("summary.searching") : t("summary.by_similarity"), v.image?.preview);
  }
}

/* 重复照片页的类别按钮：当前类别高亮，数字是各类的组数（随照片/截图、时间筛选变化） */
function syncDupBar(v) {
  for (const b of $$("#dup-cat button")) {
    b.setAttribute("aria-pressed", String(b.dataset.cat === v.cat));
    const n = v.counts?.[b.dataset.cat];
    b.querySelector(".n").textContent = n == null ? "" : n.toLocaleString(LANG);
  }
}

function syncKindButtons() {
  for (const b of $$("#kind button")) b.setAttribute("aria-pressed", String(b.dataset.kind === app.kind));
}

/* ---------- 大图预览 ---------- */

const viewer = $("#viewer");
const stage = $("#stage");
const vImg = $("#v-img");
let zoom = null; // 放大状态 {fx, fy}：点击位置占图片宽高的比例
let drag = null;

const previewUrl = (r) => `${app.api}/photo/${r.id}`;
const photoAt = (i) => app.view?.results[i];

function formatTime(taken) {
  if (!taken) return t("viewer.unknown");
  const d = new Date(taken.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? taken : fmtDateTime(d);
}

function formatBytes(n) {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

function openViewer(i) {
  const r = photoAt(i);
  if (!r) return;
  app.current = i;
  showPhoto(r);
  renderStrip(r);
  if (!viewer.open) {
    viewer.showModal();
    viewer.focus();
  }
  if (i >= app.view.results.length - 5) loadPage(); // 翻到已加载列表的末尾附近，先取下一页
}

/* 在预览里显示一张照片：列表中的当前项，或它所在连拍组里的另一张 */
function showPhoto(r) {
  app.shown = r;
  setZoom(null);
  $("#v-note").hidden = true;
  vImg.src = thumbUrl(r.id); // 先放缩略图，预览图到了再替换
  vImg.alt = r.name;
  const full = new Image();
  full.onload = () => {
    if (app.shown !== r || zoom) return;
    vImg.src = full.src;
    // 预先加载列表里前后两张，连续翻看时不用等
    for (const j of [app.current + 1, app.current - 1]) if (photoAt(j)) new Image().src = previewUrl(photoAt(j));
  };
  full.onerror = () => {
    if (app.shown === r) showNote(t("viewer.original_missing"));
  };
  full.src = previewUrl(r);
  fillInfo(r);
}

function renderStrip(r) {
  const show = r.count > 1 && app.view.mode !== "dups"; // 重复照片页里整组已经平铺在列表中
  $("#v-group").hidden = !show;
  $("#v-strip").replaceChildren();
  if (!show) return;
  $("#v-group-label").textContent = t("viewer.burst_label", { n: r.count });
  const i = app.current;
  getJSON(`/api/group/${r.id}`)
    .then((d) => {
      if (app.current !== i) return;
      $("#v-strip").replaceChildren(...d.results.map((m) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "strip-item";
        b.title = m.name;
        b.dataset.id = m.id;
        const img = document.createElement("img");
        img.src = thumbUrl(m.id);
        img.alt = m.name;
        b.append(img);
        b.addEventListener("click", () => {
          showPhoto(m);
          markStrip();
        });
        return b;
      }));
      markStrip();
    })
    .catch(() => {});
}

function markStrip() {
  for (const b of $$("#v-strip .strip-item")) b.classList.toggle("active", Number(b.dataset.id) === app.shown.id);
}

function fillInfo(r) {
  const v = app.view;
  const i = app.current;
  const total = v.total ?? `${v.results.length}${v.hasMore ? "+" : ""}`;
  $("#v-counter").textContent = `${i + 1} / ${total}`;
  $("#v-name").textContent = r.name;
  $("#v-time").textContent = formatTime(r.taken_at);
  const pixels = r.width * r.height;
  $("#v-size").textContent = pixels
    ? t("viewer.dimensions_value", { w: r.width, h: r.height, mp: (pixels / 1e6).toFixed(1), wan: Math.round(pixels / 1e4) })
    : t("viewer.unknown");
  $("#v-bytes").textContent = r.bytes ? formatBytes(r.bytes) : "…";
  $("#v-camera").textContent = "…";
  // 文本搜索的排序分扣过基线，数值本身对用户没有意义，只在找相似、以图搜图时显示相似度
  const hasScore = (v.mode === "similar" || v.mode === "image") && typeof r.score === "number";
  $("#v-score-label").hidden = $("#v-score").hidden = !hasScore;
  if (hasScore) $("#v-score").textContent = percent(r.score);
  // 重复照片页里逐张比较时，说明这张是建议保留的、完全相同的，还是几乎相同的
  const role = v.mode === "dups" && r.dup?.role !== "similar" ? r.dup?.role : null;
  $("#v-role-label").hidden = $("#v-role").hidden = !role;
  if (role) $("#v-role").textContent = t(`viewer.dup_${role}`);
  $("#v-path").textContent = r.path;
  $("#v-prev").disabled = i <= 0;
  $("#v-next").disabled = i >= v.results.length - 1;
  getJSON(`/api/info/${r.id}`)
    .then((d) => {
      if (app.shown !== r) return;
      $("#v-bytes").textContent = d.bytes ? formatBytes(d.bytes) : t("viewer.unknown");
      $("#v-camera").textContent = d.camera || t("viewer.unknown");
    })
    .catch(() => {});
}

function showNote(text) {
  $("#v-note").textContent = text;
  $("#v-note").hidden = false;
}

function step(delta) {
  if (zoom) return;
  const i = app.current + delta;
  if (!photoAt(i)) return;
  openViewer(i);
  if (slideTimer) setPlaying(true); // 手动翻页后重新计时，免得马上又跳下一张
}

/* 缩略图大小：小 / 中 / 大，记在本机 */
const GRID_SIZES = [{ key: "size.small", min: 130 }, { key: "size.medium", min: 180 }, { key: "size.large", min: 260 }];

function setGridSize(i) {
  app.gridSize = Math.max(0, Math.min(GRID_SIZES.length - 1, i));
  document.documentElement.style.setProperty("--tile-min", `${GRID_SIZES[app.gridSize].min}px`);
  $("#size-btn").title = t("toolbar.size_title", { size: t(GRID_SIZES[app.gridSize].key) });
  savePrefs({ gridSize: app.gridSize });
  maybeLoadMore();
}

function setZoom(z) {
  zoom = z;
  stage.classList.toggle("zoomed", Boolean(z));
  if (!z) return;
  const r = app.shown;
  const center = () => {
    stage.scrollLeft = z.fx * vImg.width - stage.clientWidth / 2;
    stage.scrollTop = z.fy * vImg.height - stage.clientHeight / 2;
  };
  center();
  // 放大时换成原始分辨率（最长边不超过 8192）
  const big = new Image();
  big.onload = () => {
    if (zoom !== z || app.shown !== r) return;
    vImg.src = big.src;
    vImg.decode().then(center).catch(center);
  };
  big.src = `${previewUrl(r)}?max_side=8192`;
}

vImg.addEventListener("click", (e) => {
  if (drag?.moved) return;
  if (zoom) return setZoom(null);
  // object-fit: contain 时图片实际显示区域比元素小，按实际区域换算点击位置
  const box = vImg.getBoundingClientRect();
  const scale = Math.min(box.width / vImg.naturalWidth, box.height / vImg.naturalHeight);
  const w = vImg.naturalWidth * scale;
  const h = vImg.naturalHeight * scale;
  const fx = (e.clientX - box.left - (box.width - w) / 2) / w;
  const fy = (e.clientY - box.top - (box.height - h) / 2) / h;
  setZoom({ fx: Math.min(1, Math.max(0, fx)), fy: Math.min(1, Math.max(0, fy)) });
});

stage.addEventListener("pointerdown", (e) => {
  if (!zoom || e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY, left: stage.scrollLeft, top: stage.scrollTop, moved: false };
  stage.setPointerCapture(e.pointerId);
  stage.classList.add("dragging");
});
stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x;
  const dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  stage.scrollLeft = drag.left - dx;
  stage.scrollTop = drag.top - dy;
});
stage.addEventListener("pointerup", () => {
  stage.classList.remove("dragging");
  setTimeout(() => (drag = null)); // 让紧随其后的 click 还能判断是否拖动过
});
stage.addEventListener("click", (e) => {
  if (e.target === stage && !zoom) viewer.close(); // 点图片外的空白处关闭
});

/* 幻灯片：每 3 秒下一张，到已加载列表末尾时先取下一页，真正到底就停 */
let slideTimer = null;

function setPlaying(on) {
  clearInterval(slideTimer);
  slideTimer = on ? setInterval(nextSlide, 3000) : null;
  $("#v-play").textContent = on ? "❚❚" : "▶";
  $("#v-play").title = t(on ? "viewer.pause_title" : "viewer.play_title");
  $("#v-play").classList.toggle("on", on);
}

function nextSlide() {
  if (zoom) return;
  if (photoAt(app.current + 1)) openViewer(app.current + 1);
  else if (!app.view.hasMore) setPlaying(false);
  else loadPage();
}

/* 全屏：Tauri 里让整个窗口全屏；浏览器调试时用网页全屏 */
let fullscreen = false;

async function setFullscreen(on) {
  if (on === fullscreen) return;
  try {
    if (tauri) await tauri.window.getCurrentWindow().setFullscreen(on);
    else if (on) await document.documentElement.requestFullscreen();
    else if (document.fullscreenElement) await document.exitFullscreen();
    fullscreen = on;
  } catch (e) {
    toast(t("viewer.fullscreen_failed", { error: e }));
  }
  $("#v-full").classList.toggle("on", fullscreen);
  $("#v-full").title = t(fullscreen ? "viewer.exit_full_title" : "viewer.full_title");
}

function toggleInfo() {
  const bare = !viewer.classList.contains("bare");
  viewer.classList.toggle("bare", bare);
  $("#v-info").classList.toggle("on", !bare);
  savePrefs({ bareViewer: bare });
}

viewer.addEventListener("keydown", (e) => {
  if (e.target.closest("button") && e.key === " ") return; // 焦点在按钮上时空格是点按钮
  const key = e.key.toLowerCase();
  if (e.key === "ArrowLeft") step(-1);
  else if (e.key === "ArrowRight") step(1);
  else if (e.key === " ") {
    e.preventDefault();
    setPlaying(!slideTimer);
  } else if (key === "f") setFullscreen(!fullscreen);
  else if (key === "i") toggleInfo();
  else if (key === "s" && !e.ctrlKey && !e.metaKey) findSimilar(app.shown);
});
viewer.addEventListener("cancel", (e) => {
  // Esc 依次：退出放大 → 退出全屏 → 关闭预览
  if (zoom) {
    e.preventDefault();
    setZoom(null);
  } else if (fullscreen) {
    e.preventDefault();
    setFullscreen(false);
  }
});
viewer.addEventListener("close", () => {
  setZoom(null);
  setPlaying(false);
  setFullscreen(false);
});
$("#v-play").addEventListener("click", () => setPlaying(!slideTimer));
$("#v-full").addEventListener("click", () => setFullscreen(!fullscreen));
$("#v-info").addEventListener("click", toggleInfo);

/* ---------- 操作 ---------- */

let toastTimer;
/* 底部提示：可带一个操作按钮；sticky 时不自动消失（等下一条提示替换） */
function toast(text, { action, onAction, sticky } = {}) {
  const el = $("#toast");
  el.replaceChildren(text);
  if (action) {
    const b = Object.assign(document.createElement("button"), { type: "button", className: "toast-action", textContent: action });
    b.addEventListener("click", () => {
      el.hidePopover();
      onAction();
    });
    el.append(b);
  }
  if (el.matches(":popover-open")) el.hidePopover();
  el.showPopover(); // 放在顶层，预览打开时也能看到
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => el.hidePopover(), action ? 6000 : 2600);
}

function hideToast() {
  clearTimeout(toastTimer);
  if ($("#toast").matches(":popover-open")) $("#toast").hidePopover();
}

async function copyImage() {
  const r = app.shown;
  try {
    // 把 Promise 交给 ClipboardItem，下载期间不会丢掉“用户点击”的授权
    const blob = fetch(`${previewUrl(r)}?fmt=png`).then((res) => {
      if (!res.ok) throw new Error(t("err.original_unavailable"));
      return res.blob();
    });
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    toast(t("toast.image_copied"));
  } catch (e) {
    toast(t("toast.copy_failed", { error: e.message || e }));
  }
}

async function copyPath() {
  try {
    await navigator.clipboard.writeText(app.shown.path);
    toast(t("toast.path_copied"));
  } catch (e) {
    toast(t("toast.copy_failed", { error: e.message || e }));
  }
}

async function opener(command, args) {
  if (app.view?.mode === "dups") checkGoneOnReturn = true; // 用户可能会去资源管理器里删掉照片
  try {
    await tauri.core.invoke(`plugin:opener|${command}`, args);
  } catch (e) {
    toast(t("toast.action_failed", { error: e }));
  }
}

/* 整理重复照片时，用户在资源管理器里删完照片回到窗口：标出列表里已删除的，再重新扫描让索引跟上 */
let checkGoneOnReturn = false;

async function markDeleted() {
  const v = app.view;
  if (!checkGoneOnReturn || v?.mode !== "dups") return;
  checkGoneOnReturn = false;
  const ids = v.results.filter((r) => !r.gone).map((r) => r.id);
  let d;
  try {
    d = await sendJSON("POST", "/api/missing", { ids });
  } catch {
    return;
  }
  if (app.view !== v || !d.missing.length) return;
  const gone = new Set(d.missing);
  v.results.forEach((r, i) => {
    if (!gone.has(r.id)) return;
    r.gone = true;
    setSelected(i, false);
    const tile = tileAt(i);
    if (tile) markGone(tile);
  });
  updateSelbar();
  toast(t("dups.removed", { n: gone.size }));
  sendJSON("POST", "/api/rescan").then(() => startPolling(3000)).catch(() => {});
}

function markGone(tile) {
  tile.classList.add("gone");
  tile.querySelector(".role")?.remove();
  tile.append(Object.assign(document.createElement("span"), { className: "role gone", textContent: t("dups.deleted") }));
}

/* ---------- 多选与导出 ---------- */

function tileAt(i) {
  return $(`#grid .tile[data-index="${i}"]`);
}

function updateSelbar() {
  const n = app.selected.size;
  $("#selbar").hidden = n === 0;
  $("#grid").classList.toggle("selecting", n > 0);
  $("#sel-count").textContent = t("sel.count", { n });
}

function setSelected(i, on) {
  const r = photoAt(i);
  if (!r || (on && r.gone)) return; // 已经删掉的不能再选
  if (on) app.selected.set(i, r);
  else app.selected.delete(i);
  tileAt(i)?.classList.toggle("selected", on);
}

function toggleSelect(i) {
  setSelected(i, !app.selected.has(i));
  app.lastPick = i;
  updateSelbar();
}

function selectRange(a, b) {
  for (let i = Math.min(a, b); i <= Math.max(a, b); i++) setSelected(i, true);
  app.lastPick = b;
  updateSelbar();
}

function selectAllLoaded() {
  app.view?.results.forEach((_, i) => setSelected(i, true));
  updateSelbar();
}

function clearSelection() {
  for (const i of app.selected.keys()) tileAt(i)?.classList.remove("selected");
  app.selected.clear();
  app.lastPick = null;
  updateSelbar();
}

async function copySelectedPaths() {
  const paths = [...app.selected.values()].map((r) => r.path);
  try {
    await navigator.clipboard.writeText(paths.join("\r\n"));
    toast(t("sel.paths_copied", { n: paths.length }));
  } catch (e) {
    toast(t("toast.copy_failed", { error: e.message || e }));
  }
}

/* 在资源管理器里选中这些照片（每个文件夹开一个窗口），删不删由用户自己决定；本程序从不删除照片 */
async function revealSelected() {
  const paths = [...app.selected.values()].map((r) => r.path);
  const folders = new Set(paths.map((p) => p.slice(0, Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"))).toLowerCase()));
  if (folders.size > 3) {
    const ok = await tauri.dialog.ask(t("sel.reveal_many", { n: folders.size }), {
      title: t("sel.reveal"), kind: "info", okLabel: t("sel.reveal"), cancelLabel: t("common.cancel"),
    });
    if (!ok) return;
  }
  opener("reveal_item_in_dir", { paths });
}

async function exportSelected() {
  const items = [...app.selected.values()];
  if (!items.length) return;
  const dest = tauri?.dialog
    ? await tauri.dialog.open({ directory: true, title: t("export.choose") })
    : prompt(t("export.prompt"));
  if (!dest) return;
  // 重复照片页里每张都是单独列出来挑的，不再问要不要整组导出
  const groups = app.view.mode === "dups" ? 0 : items.filter((r) => r.count > 1).length;
  let includeGroups = false;
  if (groups) {
    const question = t("export.groups_question", { n: groups });
    includeGroups = tauri?.dialog
      ? await tauri.dialog.ask(question, {
        title: t("export.dialog_title"), kind: "info", okLabel: t("export.whole_groups"), cancelLabel: t("export.shown_only"),
      })
      : confirm(`${question}\n${t("export.confirm_hint")}`);
  }
  toast(t("export.running"), { sticky: true });
  try {
    const d = await sendJSON("POST", "/api/export", { ids: items.map((r) => r.id), dest, include_groups: includeGroups });
    const parts = [t("export.done", { n: d.copied })];
    if (d.skipped) parts.push(t("export.skipped", { n: d.skipped }));
    if (d.failed.length) parts.push(t("export.failed_count", { n: d.failed.length }));
    const open = { action: t("export.open_folder"), onAction: () => opener("open_path", { path: d.dest }) };
    toast(parts.join(t("export.sep")), tauri ? open : {});
    clearSelection();
  } catch (e) {
    toast(t("export.failed", { error: e.message }));
  }
}

/* 方向键在照片间移动焦点：按实际位置找最近的一张，月份标题打断行列也不受影响 */
function moveFocus(tile, key) {
  const tiles = [...$$("#grid .tile")];
  const box = tile.getBoundingClientRect();
  const cx = box.left + box.width / 2;
  let best = null;
  let bestScore = Infinity;
  for (const other of tiles) {
    if (other === tile) continue;
    const b = other.getBoundingClientRect();
    const dx = b.left + b.width / 2 - cx;
    const dy = b.top - box.top;
    let score;
    if (key === "ArrowRight") score = Math.abs(dy) < 2 && dx > 0 ? dx : Infinity;
    else if (key === "ArrowLeft") score = Math.abs(dy) < 2 && dx < 0 ? -dx : Infinity;
    else if (key === "ArrowDown") score = dy > 2 ? dy * 10 + Math.abs(dx) : Infinity;
    else score = dy < -2 ? -dy * 10 + Math.abs(dx) : Infinity;
    if (score < bestScore) {
      bestScore = score;
      best = other;
    }
  }
  // 行末按右键接到下一行开头，行首按左键接到上一行末尾
  if (!best && (key === "ArrowRight" || key === "ArrowLeft")) {
    best = tiles[tiles.indexOf(tile) + (key === "ArrowRight" ? 1 : -1)];
  }
  if (best) {
    best.focus();
    best.scrollIntoView({ block: "nearest" });
  }
}

/* ---------- 图库设置 ---------- */

const settings = $("#settings");
let library = null;

async function sendJSON(method, path, body) {
  let res;
  try {
    res = await fetch(app.api + path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error(t("err.unreachable"));
  }
  return readJSON(res);
}

function openSettings() {
  if (!settings.open) settings.showModal();
  renderLibrary();
  tauri?.app.getVersion().then((version) => {
    $("#about").textContent = t("settings.about_version", { app: t("app.title"), version });
  });
}

/* 设置里的语言选择：存进偏好后刷新页面，所有文字按新语言重新生成 */
function setupLanguageSelect() {
  const select = $("#lang-select");
  const saved = loadPrefs().lang;
  const options = [["", t("settings.lang_auto")], ...Object.entries(LOCALES).map(([code, table]) => [code, table._name])];
  select.replaceChildren(...options.map(([value, text]) => Object.assign(document.createElement("option"), { value, textContent: text })));
  select.value = saved && LOCALES[saved] ? saved : "";
  select.addEventListener("change", () => {
    savePrefs({ lang: select.value || null });
    location.reload();
  });
}

function statRow(label, value) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  return [dt, dd];
}

async function renderLibrary() {
  if (!app.api) {
    // 首次运行还在准备环境，或后台服务没能启动：还没有图库信息可读
    $("#dir-list").replaceChildren(Object.assign(document.createElement("li"), { className: "hint", textContent: t("settings.wait_backend") }));
    $("#lib-stats").replaceChildren();
    $("#add-dir").disabled = $("#rescan").disabled = true;
    return;
  }
  try {
    library = await getJSON("/api/library");
  } catch (e) {
    $("#dir-list").replaceChildren(Object.assign(document.createElement("li"), { textContent: t("settings.load_failed", { error: e.message }) }));
    return;
  }
  $("#dir-list").replaceChildren(...library.dirs.map((d) => {
    const li = document.createElement("li");
    const path = Object.assign(document.createElement("span"), { className: "dir-path", textContent: d.path });
    const meta = Object.assign(document.createElement("span"), {
      className: d.online ? "dir-meta" : "dir-meta bad",
      textContent: t(d.online ? "settings.folder_photos" : "settings.folder_offline", { n: d.photos }),
    });
    const remove = Object.assign(document.createElement("button"), { type: "button", className: "link danger", textContent: t("settings.remove") });
    remove.addEventListener("click", () => removeDir(d.path));
    li.append(path, meta, remove);
    return li;
  }));
  if (!library.dirs.length) {
    $("#dir-list").append(Object.assign(document.createElement("li"), { className: "hint", textContent: t("settings.no_folders") }));
  }
  const ready = app.status?.phase === "ready";
  const photos = t("count.photos", { n: library.count });
  const shots = t("count.screenshots", { n: library.screenshots });
  const groups = statRow(t("settings.stat_groups"), library.groups ? t("settings.stat_groups_value", { n: library.groups }) : t("settings.none"));
  if (library.groups) {
    const review = Object.assign(document.createElement("button"), { type: "button", className: "link", textContent: t("settings.review") });
    review.addEventListener("click", () => {
      settings.close();
      openDups();
    });
    groups[1].append(" · ", review);
  }
  $("#lib-stats").replaceChildren(
    ...statRow(t("settings.stat_photos"), library.screenshots ? t("settings.stat_photos_value", { photos, shots }) : photos),
    ...groups,
    ...statRow(t("settings.stat_index"), t("settings.stat_index_value", { dir: library.index_dir, size: formatBytes(library.index_bytes) })),
    ...statRow(t("settings.stat_model"), `${library.model}${library.device ? ` · ${library.device}` : ""}`),
    ...statRow(t("settings.stat_last_scan"), library.last_scan || t("settings.never_scanned")),
  );
  $("#rescan").disabled = library.busy || !ready;
  $("#add-dir").disabled = !app.status || app.status.phase === "error"; // 模型还在下载时也能先加文件夹
  $("#rescan-note").textContent = t(library.busy ? "settings.rescan_busy" : ready ? "settings.rescan_hint" : "settings.rescan_wait");
}

async function setDirs(dirs) {
  try {
    const d = await sendJSON("PUT", "/api/library", { photo_dirs: dirs });
    const done = { after_load: "settings.saved_after_load", queued: "settings.saved_queued" }[d.rescan];
    toast(t(done || "settings.saved_started"));
    startPolling(3000);
  } catch (e) {
    toast(t("settings.save_failed", { error: e.message }));
  }
  renderLibrary();
}

async function addDir() {
  // Tauri 里用系统的选择文件夹对话框；浏览器调试时退回输入路径
  const path = tauri?.dialog
    ? await tauri.dialog.open({ directory: true, title: t("settings.choose_folder") })
    : prompt(t("settings.folder_prompt"));
  if (!path) return;
  const current = (library || (await getJSON("/api/library"))).dirs.map((d) => d.path);
  setDirs([...current, path]);
}

async function removeDir(path) {
  const question = t("settings.remove_confirm", { path });
  const ok = tauri?.dialog
    ? await tauri.dialog.ask(question, {
      title: t("settings.remove_title"), kind: "warning", okLabel: t("settings.remove"), cancelLabel: t("common.cancel"),
    })
    : confirm(question);
  if (ok) setDirs(library.dirs.map((d) => d.path).filter((p) => p !== path));
}

async function startRescan() {
  try {
    const d = await sendJSON("POST", "/api/rescan");
    toast(t(d.rescan === "queued" ? "settings.rescan_queued" : "settings.rescan_started"));
    startPolling(3000);
  } catch (e) {
    toast(t("settings.rescan_failed", { error: e.message }));
  }
  renderLibrary();
}

/* ---------- 事件绑定与启动 ---------- */

$("#open-settings").addEventListener("click", openSettings);
$("#status").addEventListener("click", openSettings);
$("#settings-close").addEventListener("click", () => settings.close());
$("#add-dir").addEventListener("click", addDir);
$("#rescan").addEventListener("click", startRescan);

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  submitQuery($("#q").value);
});
$("#home").addEventListener("click", () => {
  $("#q").value = "";
  app.pending = null;
  goHome();
});
$("#back").addEventListener("click", back);
$("#grid").addEventListener("click", (e) => {
  if ($("#grid").classList.contains("stale")) return;
  const extras = e.target.closest(".dup-head .link");
  if (extras) return selectExtras(Number(extras.dataset.group));
  const tile = e.target.closest(".tile");
  if (!tile) return;
  const i = Number(tile.dataset.index);
  if (e.target.closest(".similar-btn") && !app.selected.size) findSimilar(photoAt(i));
  else if (e.shiftKey && app.lastPick != null) selectRange(app.lastPick, i);
  else if (e.target.closest(".check") || e.ctrlKey || e.metaKey || app.selected.size) toggleSelect(i);
  else openViewer(i);
});
$("#grid").addEventListener("keydown", (e) => {
  const tile = e.target.closest(".tile");
  if (!tile) return;
  if (e.key.startsWith("Arrow")) {
    e.preventDefault();
    moveFocus(tile, e.key);
  } else if (e.key === " ") {
    e.preventDefault(); // 空格是选择，不是点开
    toggleSelect(Number(tile.dataset.index));
  }
});
$("#grid").addEventListener("keyup", (e) => {
  if (e.key === " ") e.preventDefault();
});
$("#sel-all").addEventListener("click", selectAllLoaded);
$("#sel-copy").addEventListener("click", copySelectedPaths);
$("#sel-reveal").addEventListener("click", revealSelected);
$("#sel-export").addEventListener("click", exportSelected);
$("#img-btn").addEventListener("click", pickImage);
$("#img-file").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // 再选同一个文件也要触发
  if (file) searchByImage({ blob: file, name: file.name });
});
// Ctrl+V 粘贴图片：截图工具、浏览器里“复制图片”都可以。输入框里粘贴文字照常
document.addEventListener("paste", (e) => {
  const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/") || IMAGE_EXT.test(f.name));
  if (!file || (e.target instanceof HTMLInputElement && e.clipboardData.types.includes("text/plain"))) return;
  e.preventDefault();
  searchByImage({ blob: file, name: file.name === "image.png" ? "" : file.name }); // 截图粘贴进来都叫 image.png
});
$("#dups-btn").addEventListener("click", openDups);
for (const btn of $$("#dup-cat button")) {
  btn.addEventListener("click", () => {
    if (app.view?.mode === "dups" && btn.dataset.cat !== app.view.cat) openView(dupSpec(btn.dataset.cat), false);
  });
}
$("#sel-clear").addEventListener("click", clearSelection);
const help = $("#help");
$("#open-help").addEventListener("click", () => {
  settings.close();
  help.showModal();
});
$("#help-close").addEventListener("click", () => help.close());
for (const btn of $$("#kind button")) {
  btn.addEventListener("click", () => {
    if (btn.dataset.kind === app.kind) return;
    app.kind = btn.dataset.kind;
    savePrefs({ kind: app.kind });
    syncKindButtons();
    reloadView();
  });
}
$("#clear-history").addEventListener("click", () => saveHistory([]));
$("#refresh-note").addEventListener("click", reloadView);
$("#time-menu").addEventListener("beforetoggle", (e) => {
  if (e.newState === "open") renderTimeMenu();
});
$("#time-clear").addEventListener("click", () => setTime(null));

// 搜索框：点击或输入时显示建议（不在启动自动聚焦时弹出），方向键选择，Esc 只关下拉
const qInput = $("#q");
qInput.addEventListener("click", renderSuggest);
qInput.addEventListener("input", renderSuggest);
qInput.addEventListener("blur", () => setTimeout(hideSuggest, 100));
qInput.addEventListener("keydown", (e) => {
  if (e.isComposing) return; // 中文输入法选词时不抢按键
  const open = pop.matches(":popover-open");
  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (!open) renderSuggest();
    moveSuggest(1);
  } else if (e.key === "ArrowUp" && open) {
    e.preventDefault();
    moveSuggest(-1);
  } else if (e.key === "Enter" && open && popIndex >= 0) {
    e.preventDefault();
    pickSuggest(popRows[popIndex].text);
  } else if (e.key === "Escape" && open) {
    e.preventDefault(); // 不清空输入框，只关闭下拉
    hideSuggest();
  }
});
$("#banner-action").addEventListener("click", restartBackend);
tauri?.event.listen("backend-exited", (e) => backendDown(e.payload));

$("#v-prev").addEventListener("click", () => step(-1));
$("#v-next").addEventListener("click", () => step(1));
$("#v-close").addEventListener("click", () => viewer.close());
$("#v-similar").addEventListener("click", () => findSimilar(app.shown));
$("#v-copy").addEventListener("click", copyImage);
$("#v-copy-path").addEventListener("click", copyPath);
$("#v-open").addEventListener("click", () => opener("open_path", { path: app.shown.path }));
$("#v-reveal").addEventListener("click", () => opener("reveal_item_in_dir", { paths: [app.shown.path] }));

document.addEventListener("keydown", (e) => {
  if (viewer.open || settings.open || help.open) return;
  const typing = e.target instanceof HTMLInputElement;
  if ((e.key === "/" && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k")) {
    e.preventDefault();
    $("#q").focus();
    $("#q").select();
  } else if (e.altKey && e.key === "ArrowLeft") {
    e.preventDefault();
    back();
  } else if (typing) {
    // 输入框里的按键（包括 Ctrl+A、?）保持原样
  } else if ((e.ctrlKey || e.metaKey) && ["=", "+", "-"].includes(e.key)) {
    e.preventDefault();
    setGridSize(app.gridSize + (e.key === "-" ? -1 : 1));
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    selectAllLoaded();
  } else if (e.key === "Escape" && app.selected.size) {
    clearSelection();
  } else if (e.key === "?") {
    help.showModal();
  }
});
document.addEventListener("mouseup", (e) => {
  if (e.button === 3) back(); // 鼠标侧键“后退”
});
window.addEventListener("focus", markDeleted);
window.addEventListener("scroll", maybeLoadMore, { passive: true });
window.addEventListener("resize", maybeLoadMore);
$("#size-btn").addEventListener("click", () => setGridSize((app.gridSize + 1) % GRID_SIZES.length));
window.addEventListener("wheel", (e) => {
  if (!e.ctrlKey || viewer.open) return;
  e.preventDefault(); // Ctrl+滚轮调缩略图大小，而不是缩放整个页面
  setGridSize(app.gridSize + (e.deltaY < 0 ? 1 : -1));
}, { passive: false });

(async () => {
  applyI18n();
  document.title = t("app.title");
  tauri?.window.getCurrentWindow().setTitle(t("app.title")).catch(() => {});
  setupLanguageSelect();
  document.body.classList.toggle("in-tauri", Boolean(tauri));
  const prefs = loadPrefs();
  app.kind = ["all", "photo", "screenshot"].includes(prefs.kind) ? prefs.kind : "all";
  syncKindButtons();
  setGridSize(Number.isInteger(prefs.gridSize) ? prefs.gridSize : 1);
  viewer.classList.toggle("bare", Boolean(prefs.bareViewer));
  $("#v-info").classList.toggle("on", !prefs.bareViewer);
  renderChips();
  updateToolbar();
  setupImageDrop();
  $("#q").focus();
  try {
    await resolveApi();
  } catch (e) {
    backendDown(e);
    return;
  }
  startPolling();
})();
