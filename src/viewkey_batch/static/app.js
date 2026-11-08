const UI_STATE_KEY = "viewkey-ui-state";

function savedUiState() {
  try { return JSON.parse(localStorage.getItem(UI_STATE_KEY) || "{}"); }
  catch (_) { return {}; }
}

const saved = savedUiState();
const state = {
  videos: [],
  statuses: {},
  downloadedKeys: new Set(),
  downloads: [],
  downloadCounts: {},
  settings: null,
  downloadPollTimer: null,
  selected: new Set(),
  downloadSelected: new Set(),
  // category：分类浏览；search：全站搜索（走站点的 search_result.php）
  mode: "category",
  query: "",
  activeView: saved.activeView || "catalog",
  category: saved.category || "latest",
  page: 1,
  pagesLoaded: 0,
  hasMore: false,
  loadingMore: false,
  listSeq: 0,
  listInfo: {cached: false, age: 0},
  browseError: "",
  animationTimer: null,
  jobId: null,
  jobTimer: null,
  captchaSid: null,
  downloadSignature: "",
  downloadFilter: "pending",
  settingsSection: saved.settingsSection || "settingsAccount",
  sites: [],
  activeSiteId: "",
  proxyReady: true,
  lastErrorToast: "",
  lastErrorAt: 0,
};

const $ = (selector) => document.querySelector(selector);
// 缩略图交给后端代取，这样 CDN 图片也走设置里的代理（浏览器直连 CDN 用不上代理）
const thumbUrl = (url) => (url ? `/api/thumb?u=${encodeURIComponent(url)}` : "");
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[c]);

function toast(message, error = false) {
  if (error) {
    const now = Date.now();
    if (state.lastErrorToast === message && now - state.lastErrorAt < 8000) return;
    state.lastErrorToast = message;
    state.lastErrorAt = now;
  }
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("#toastRegion").append(node);
  setTimeout(() => node.remove(), 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    cache: "no-store",
    ...options,
  });
  const body = await response.text();
  let data = {};
  try { data = body ? JSON.parse(body) : {}; } catch (_) { data = {detail: body || "服务器未返回 JSON"}; }
  if (!response.ok) throw new Error(`服务器返回异常（HTTP ${response.status}）：${data.detail || "请求失败"}`);
  return data;
}

// identity 是后端的去重主键；旧数据没有 identity 字段时回退到 viewkey
const videoKey = (video) => video.identity || video.viewkey;

// 服务端已经按分类/关键词返回结果，前端不再二次过滤：
// 以前按搜索词过滤当前页，结果页显示为空却写着「还没有采集结果」，很容易误判成采集失败
function visibleVideos() {
  return state.videos;
}

// 空列表要区分「搜索没结果」「请求失败」和「确实没采集到」，
// 否则一次网络错误会让人以为采集功能坏了
function updateEmptyState(videos) {
  const title = $("#emptyTitle");
  const desc = $("#emptyDesc");
  const clearButton = $("#clearSearchButton");
  const retryButton = $("#retryBrowseButton");
  if (!title || !desc || !clearButton || !retryButton) return;
  const showClear = visible => clearButton.classList.toggle("hidden", !visible);
  const showRetry = visible => retryButton.classList.toggle("hidden", !visible);
  if (videos.length) {
    showClear(false);
    showRetry(false);
    return;
  }
  const searching = state.mode === "search";
  if (state.browseError) {
    title.textContent = searching ? "这次搜索没有取到结果" : "这一页没有取到数据";
    desc.textContent = state.browseError;
    showClear(false);
    showRetry(true);
    return;
  }
  showRetry(false);
  if (searching) {
    title.textContent = `没有找到「${state.query}」`;
    desc.textContent = "换个关键词试试，也可以直接粘贴视频 ID 或视频地址。";
    showClear(true);
  } else {
    title.textContent = "还没有采集结果";
    desc.textContent = "从左侧选择分类，这里会实时显示该页的视频。";
    showClear(false);
  }
}

function persistUiState() {
  localStorage.setItem(UI_STATE_KEY, JSON.stringify({
    activeView: state.activeView,
    category: state.category,
    settingsSection: state.settingsSection,
  }));
}

function syncRoute(replace = false) {
  const params = new URLSearchParams();
  if (state.mode === "search" && state.query) params.set("q", state.query);
  else params.set("category", state.category);
  const hash = state.activeView === "downloads" ? "#/downloads" : state.activeView === "settings" ? "#/settings" : `#/catalog?${params}`;
  history[replace ? "replaceState" : "pushState"](null, "", hash);
  persistUiState();
}

function readRoute() {
  const match = location.hash.match(/^#\/(catalog|downloads|settings)(?:\?(.*))?$/);
  if (!match) return;
  state.activeView = match[1];
  const params = new URLSearchParams(match[2] || "");
  const query = (params.get("q") || "").trim();
  if (query) {
    state.mode = "search";
    state.query = query;
  } else {
    state.mode = "category";
    state.query = "";
    if (params.get("category")) state.category = params.get("category");
  }
}

function stateText(status) {
  const labels = {queued: "等待下载", downloading: "正在下载", completed: "下载完成", failed: "下载失败"};
  return labels[status?.state] || "";
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function render(animate = false) {
  const videos = visibleVideos();
  const resultCount = $("#resultCount");
  if (resultCount) resultCount.textContent = videos.length;
  $("#selectedCount").textContent = state.selected.size;
  $("#catalogBadge").textContent = state.videos.length;
  $("#downloadButton").disabled = state.selected.size === 0;
  $("#removeButton").disabled = state.selected.size === 0;
  $("#selectAll").checked = videos.length > 0 && videos.every(v => state.selected.has(videoKey(v)));
  $("#selectAll").indeterminate = videos.some(v => state.selected.has(videoKey(v))) && !$("#selectAll").checked;
  $("#emptyState").classList.toggle("hidden", videos.length > 0);
  $("#videoGrid").classList.toggle("hidden", videos.length === 0);
  updateEmptyState(videos);

  const grid = $("#videoGrid");
  const existing = new Map([...grid.children].map(card => [card.dataset.key, card]));
  videos.forEach((video, index) => {
    const key = videoKey(video);
    const selected = state.selected.has(key);
    const status = state.statuses[key];
    let card = existing.get(key);
    const contentSignature = JSON.stringify([video.title, video.author, video.thumbnail_url, video.duration, video.views]);
    if (card && card.dataset.contentSignature !== contentSignature) {
      card.remove();
      card = null;
    }
    if (!card) {
      const template = document.createElement("template");
      template.innerHTML = `<article class="video-card" data-key="${escapeHtml(key)}">
        <div class="thumb${video.thumbnail_url ? "" : " thumb-empty"}">
          ${video.thumbnail_url
            ? `<img src="${escapeHtml(thumbUrl(video.thumbnail_url))}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
            : '<i data-lucide="image-off"></i>'}
          <div class="downloaded-badge-slot"></div>
          <label class="card-select check-control" title="选择视频">
            <input type="checkbox"><span></span>
          </label>
          ${video.duration ? `<span class="duration">${escapeHtml(video.duration)}</span>` : ""}
        </div>
        <div class="card-body">
          <h3 class="video-title" title="${escapeHtml(video.title)}">${escapeHtml(video.title || "未命名视频")}</h3>
          <div class="metadata">
            <span class="author">${escapeHtml(video.author || video.source || "未知作者")}</span>
            ${video.views ? `<span>${escapeHtml(video.views)} 次播放</span>` : ""}
            <span class="key">${escapeHtml((video.viewkey || video.identity || "").slice(0, 8))}</span>
          </div>
        </div>
      </article>`;
      card = template.content.firstElementChild;
      card.dataset.contentSignature = contentSignature;
      const image = card.querySelector(".thumb img");
      const showImage = () => image.classList.add("is-loaded");
      if (image) {
        if (image.complete) showImage();
        else image.addEventListener("load", showImage, {once: true});
      }
      card.querySelector("input").addEventListener("change", event => {
      event.target.checked ? state.selected.add(card.dataset.key) : state.selected.delete(card.dataset.key);
      render();
      });
    }
    existing.delete(key);
    card.style.setProperty("--card-index", Math.min(index, 12));
    card.classList.toggle("selected", selected);
    card.querySelector("input").checked = selected;
    card.querySelector(".downloaded-badge-slot").innerHTML = state.downloadedKeys.has(key)
      ? '<span class="downloaded-badge"><i data-lucide="check"></i>已下载</span>'
      : "";
    grid.append(card);
  });
  existing.forEach(card => card.remove());
  if (animate && videos.length) {
    grid.classList.remove("animate-in");
    void grid.offsetWidth;
    grid.classList.add("animate-in");
    clearTimeout(state.animationTimer);
    state.animationTimer = setTimeout(() => grid.classList.remove("animate-in"), 900);
  }
  if (window.lucide) lucide.createIcons();
}

// 侧栏任务分类的统一筛选规则，工具栏全选也复用
function downloadsMatchingFilter(filter) {
  return state.downloads.filter(item => {
    switch (filter) {
      case "pending": return item.state !== "completed";
      case "downloading": return item.state === "downloading";
      case "queued": return item.state === "queued";
      case "failed": return item.state === "failed";
      case "completed": return item.state === "completed";
      default: return true;
    }
  });
}

function updateDownloadSidebar(counts) {
  const pending = counts.queued + counts.downloading + counts.failed;
  $("#filterPendingBadge").textContent = pending;
  $("#filterCompletedBadge").textContent = counts.completed;
  $("#filterFailedBadge").textContent = counts.failed;
  document.querySelectorAll("#downloadFilterSection .category-item").forEach(item => {
    item.classList.toggle("active", item.dataset.filter === state.downloadFilter);
  });
}

function renderDownloads(totalSize = 0) {
  const visibleDownloads = downloadsMatchingFilter(state.downloadFilter);
  const counts = {queued: 0, downloading: 0, completed: 0, failed: 0, ...state.downloadCounts};
  $("#queuedCount").textContent = counts.queued;
  $("#downloadingCount").textContent = counts.downloading;
  $("#completedCount").textContent = counts.completed;
  $("#failedCount").textContent = counts.failed;
  $("#retryAllButton").disabled = counts.failed === 0;
  const unfinished = counts.queued + counts.downloading + counts.failed;
  $("#downloadMissingCount").textContent = unfinished;
  $("#downloadMissingButton").disabled = unfinished === 0;
  $("#totalDiskUsage").textContent = formatBytes(totalSize);
  $("#totalDiskUsage").dataset.bytes = totalSize;
  const active = counts.queued + counts.downloading;
  $("#activeDownloadBadge").textContent = active;
  $("#activeDownloadBadge").classList.toggle("hidden", active === 0);
  updateDownloadSidebar(counts);
  $("#downloadEmpty").classList.toggle("hidden", visibleDownloads.length > 0);
  $("#downloadList").classList.toggle("hidden", visibleDownloads.length === 0);
  $("#downloadEmpty h2").textContent = state.downloads.length ? "当前筛选没有任务" : "还没有下载任务";
  $("#downloadEmpty p").textContent = state.downloads.length ? "切换到其他状态查看下载任务。" : "在视频目录中勾选内容，然后点击“下载所选”。";
  const downloadKeys = new Set(state.downloads.map(item => item.viewkey));
  state.downloadSelected.forEach(key => { if (!downloadKeys.has(key)) state.downloadSelected.delete(key); });
  const selectAllDownloads = $("#selectAllDownloads");
  selectAllDownloads.checked = visibleDownloads.length > 0 && visibleDownloads.every(item => state.downloadSelected.has(item.viewkey));
  selectAllDownloads.indeterminate = visibleDownloads.some(item => state.downloadSelected.has(item.viewkey)) && !selectAllDownloads.checked;
  $("#selectedDownloadCount").textContent = state.downloadSelected.size;
  $("#removeDownloadsButton").disabled = state.downloadSelected.size === 0;
  $("#downloadSelectedButton").disabled = state.downloadSelected.size === 0;

  const signature = state.downloads.map(item => item.viewkey).join("|");
  if (signature !== state.downloadSignature) {
    state.downloadSignature = signature;
    $("#downloadList").innerHTML = state.downloads.map(item => `
    <div class="download-row ${escapeHtml(item.state)}" data-key="${escapeHtml(item.viewkey)}" data-action-state="">
      <label class="check-control download-row-select" title="选择任务"><input class="download-checkbox" data-key="${escapeHtml(item.viewkey)}" type="checkbox"><span></span></label>
      <div class="download-thumb"><img src="${escapeHtml(thumbUrl(item.thumbnail_url))}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.classList.add('is-hidden')"></div>
      <div class="download-info">
        <strong class="download-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title || item.viewkey)}</strong>
        <span class="download-meta"><span class="meta-file"></span><span class="meta-sep" hidden>·</span><span class="meta-size"></span><span class="meta-sep" hidden>·</span><span class="meta-extra"></span></span>
        <div class="row-progress"><span style="width:${item.percent || 0}%"></span></div>
      </div>
      <div class="download-side">
        <span class="download-state"></span>
        <div class="row-action"></div>
      </div>
    </div>
    `).join("");
  }

  const rows = new Map([...document.querySelectorAll(".download-row")].map(row => [row.dataset.key, row]));
  state.downloads.forEach(item => {
    const row = rows.get(item.viewkey);
    if (!row) return;
    row.classList.toggle("hidden", !visibleDownloads.some(entry => entry.viewkey === item.viewkey));
    row.classList.toggle("failed", item.state === "failed");
    row.querySelector(".download-checkbox").checked = state.downloadSelected.has(item.viewkey);
    const metaFile = row.querySelector(".meta-file");
    metaFile.textContent = item.file_name || "等待生成文件";
    const metaSize = row.querySelector(".meta-size");
    const metaExtra = row.querySelector(".meta-extra");
    const separators = row.querySelectorAll(".meta-sep");
    const extraText = item.state === "downloading" && item.speed ? item.speed : item.state === "failed" && item.error ? item.error : "";
    metaSize.textContent = item.file_size ? formatBytes(item.file_size) : "";
    metaExtra.textContent = extraText;
    metaExtra.classList.toggle("is-error", item.state === "failed" && Boolean(item.error));
    let parts = [metaFile.textContent, metaSize.textContent, extraText].filter(Boolean).length;
    let shown = 0;
    separators.forEach(sep => { shown += 1; sep.hidden = shown >= parts; });
    const stateNode = row.querySelector(".download-state");
    stateNode.className = `download-state ${item.state}`;
    stateNode.textContent = item.state === "downloading" ? `${stateText(item)} ${item.percent || 0}%` : stateText(item);
    row.querySelector(".row-progress span").style.width = `${item.percent || 0}%`;
    if (row.dataset.actionState !== item.state) {
      row.dataset.actionState = item.state;
      row.querySelector(".row-action").innerHTML = `${item.state === "failed"
        ? `<button class="icon-button retry-download" data-key="${escapeHtml(item.viewkey)}" title="重新下载"><i data-lucide="rotate-ccw"></i></button>`
        : `<span class="row-action-placeholder"></span>`}
        <button class="icon-button delete-download" data-key="${escapeHtml(item.viewkey)}" title="删除任务"><i data-lucide="trash-2"></i></button>`;
    }
  });
  if (state.activeView === "downloads") $("#viewSubtitle").textContent = `${state.downloads.length} 个下载任务`;

  document.querySelectorAll(".retry-download:not([data-bound])").forEach(button => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      try {
        watchJob(await api("/api/downloads", {method: "POST", body: JSON.stringify({viewkeys: [button.dataset.key], workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4})}));
      } catch (error) { toast(error.message, true); }
    });
  });
  document.querySelectorAll(".download-checkbox:not([data-bound])").forEach(checkbox => {
    checkbox.dataset.bound = "true";
    checkbox.addEventListener("change", () => {
      checkbox.checked ? state.downloadSelected.add(checkbox.dataset.key) : state.downloadSelected.delete(checkbox.dataset.key);
      renderDownloads(totalSize);
    });
  });
  document.querySelectorAll(".delete-download:not([data-bound])").forEach(button => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      if (!confirm("从下载管理删除此任务？本地视频文件不会删除。")) return;
      try {
        await api("/api/downloads/remove", {method: "POST", body: JSON.stringify({viewkeys: [button.dataset.key]})});
        state.downloadSelected.delete(button.dataset.key);
        await loadDownloads();
      } catch (error) { toast(error.message, true); }
    });
  });
  if (window.lucide) lucide.createIcons();
}

function formatAge(seconds) {
  if (!seconds || seconds < 45) return "刚刚更新";
  if (seconds < 3600) return `缓存 ${Math.round(seconds / 60)} 分钟前`;
  return `缓存 ${Math.round(seconds / 3600)} 小时前`;
}

function updateHeading() {
  if (state.activeView !== "catalog") return;
  const searching = state.mode === "search";
  $("#viewTitle").textContent = searching ? `搜索「${state.query}」` : "视频目录";
  $("#viewSubtitle").innerHTML = `<span id="resultCount">${state.videos.length}</span> ${searching ? "个搜索结果" : "个采集结果"}`;
  $("#searchResetButton")?.classList.toggle("hidden", !$("#searchInput").value.trim());
}

// 底部进度 + 「加载更多」兜底：自动翻页失效或不支持 IntersectionObserver 时仍可手动继续
function updateListFooter() {
  const progress = $("#listProgress");
  if (!progress) return;
  const parts = [`已加载 ${state.videos.length} 条`];
  if (state.pagesLoaded > 1) parts.push(`第 ${state.pagesLoaded} 页`);
  if (state.listInfo.cached) parts.push(formatAge(state.listInfo.age));
  progress.textContent = parts.join(" · ");
  $("#loadSpinner").classList.toggle("hidden", !state.loadingMore);
  $("#loadMoreButton").classList.toggle("hidden", !state.hasMore || state.loadingMore);
  $("#listEnd").classList.toggle("hidden", state.hasMore || !state.videos.length || state.loadingMore);
  $("#listFooter").classList.toggle("hidden", !state.videos.length && !state.browseError);
}

function catalogEndpoint(page, refresh) {
  const base = state.mode === "search"
    ? `/api/search?q=${encodeURIComponent(state.query)}&page=${page}`
    : `/api/browse?category=${encodeURIComponent(state.category)}&page=${page}`;
  return refresh ? `${base}&refresh=1` : base;
}

async function loadPage(page, {append = false, refresh = false} = {}) {
  const seq = ++state.listSeq;
  const refreshButton = $("#refreshButton");
  const grid = $("#videoGrid");
  if (!append) {
    refreshButton.disabled = true;
    refreshButton.classList.add("is-loading");
    grid.classList.add("is-refreshing");
  }
  state.loadingMore = append;
  updateListFooter();
  try {
    const data = await api(catalogEndpoint(page, refresh));
    if (seq !== state.listSeq) return;
    const incoming = data.videos || [];
    if (append) {
      const seen = new Set(state.videos.map(videoKey));
      state.videos = [...state.videos, ...incoming.filter(video => !seen.has(videoKey(video)))];
    } else {
      state.videos = incoming;
    }
    state.page = page;
    state.pagesLoaded = append ? state.pagesLoaded + 1 : 1;
    // 站点偶尔会返回重复页，此时直接收尾，避免滚动到底反复请求同一页
    state.hasMore = Boolean(data.pagination?.has_next) && (!append || incoming.length > 0);
    state.statuses = {...state.statuses, ...(data.download_status || {})};
    (data.downloaded_keys || []).forEach(key => state.downloadedKeys.add(key));
    state.listInfo = {cached: Boolean(data.cached), age: Number(data.cache_age) || 0};
    state.browseError = "";
    const keys = new Set(state.videos.map(videoKey));
    state.selected.forEach(key => { if (!keys.has(key)) state.selected.delete(key); });
    updateHeading();
    render(!append);
  } catch (error) {
    if (seq !== state.listSeq) return;
    state.browseError = error.message;
    if (append) {
      // 追加失败时保留已加载内容，把错误放在页脚，别把整页清空
      toast(error.message, true);
    } else {
      state.videos = [];
      render();
    }
  } finally {
    if (seq === state.listSeq) {
      state.loadingMore = false;
      refreshButton.disabled = false;
      refreshButton.classList.remove("is-loading");
      grid.classList.remove("is-refreshing");
      updateListFooter();
    }
  }
}

function loadCatalog(options) {
  return loadPage(1, options);
}

function maybeLoadMore() {
  if (state.activeView !== "catalog") return;
  if (!state.hasMore || state.loadingMore || !state.videos.length) return;
  loadPage(state.page + 1, {append: true});
}

async function loadDownloads() {
  try {
    const data = await api("/api/downloads");
    state.downloads = data.downloads;
    state.downloadCounts = data.counts;
    state.downloadedKeys = new Set(data.downloads.filter(item => item.state === "completed").map(item => item.viewkey));
    data.downloads.forEach(item => { state.statuses[item.viewkey] = item; });
    renderDownloads(data.total_size);
    if (state.activeView === "catalog") render();
  } catch (error) {
    toast(error.message, true);
  }
}

// 采集任务进行中时从本地库刷新，避免高频轮询打目标站触发人机验证
async function refreshCatalogFromStore() {
  try {
    const data = await api("/api/catalog");
    const videos = state.mode === "search"
      ? data.videos
      : data.videos.filter(video => {
          const sources = video.sources?.length ? video.sources : [video.source];
          return sources.includes(state.category);
        });
    state.videos = videos;
    state.statuses = {...state.statuses, ...data.download_status};
    const keys = new Set(state.videos.map(videoKey));
    state.selected.forEach(key => { if (!keys.has(key)) state.selected.delete(key); });
    render();
  } catch (_) { /* 轮询失败不打断任务 */ }
}

function fillSettings(data) {
  state.settings = data;
  $("#settingDownloadDir").value = data.download_dir || "";
  $("#settingFolderMode").value = data.folder_mode || "flat";
  $("#settingPreferHd").checked = Boolean(data.prefer_hd);
  $("#settingWorkers").value = data.workers;
  $("#settingFragments").value = data.fragments;
  $("#settingUiRefresh").value = data.ui_refresh_seconds;
  $("#settingProxy").value = data.proxy || "";
  state.proxyReady = data.proxy_ready !== false;
  // 没有 socksio 时提前说明，免得用户填了 socks5 却只看到一句报错
  const hint = document.querySelector(".proxy-note");
  if (hint && !state.proxyReady && !hint.dataset.flagged) {
    hint.dataset.flagged = "1";
    hint.append("当前环境缺少 socks 依赖，暂只支持 HTTP/HTTPS 代理。");
  }
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    fillSettings(data);
    return data;
  } catch (error) { toast(error.message, true); return null; }
}

function renderCacheStats(data) {
  const node = $("#cacheStats");
  if (!node) return;
  const size = data.bytes ? formatBytes(data.bytes) : "0 B";
  node.textContent = `${data.entries} 页 · ${size}`;
}

async function loadCacheStats() {
  try { renderCacheStats(await api("/api/cache")); }
  catch (_) { /* 缓存信息读不到不影响主流程 */ }
}

function restartDownloadPolling() {
  clearInterval(state.downloadPollTimer);
  const seconds = Math.max(1, Number(state.settings?.ui_refresh_seconds || 3));
  state.downloadPollTimer = setInterval(() => loadDownloads(), seconds * 1000);
}

// ---- 账号登录 ----

function updateAccountStatus(data) {
  const node = $("#accountStatus");
  if (!node) return;
  if (data.logged_in === true) node.textContent = `已登录（${data.cookie_count || 0} 条 Cookie）`;
  else if (data.has_cookies) node.textContent = `已保存 ${data.cookie_count} 条 Cookie，未验证是否有效，可点击「检测登录状态」`;
  else node.textContent = "未登录。VIP 高清视频需要登录后才能下载";
}

async function loadAccount() {
  try {
    updateAccountStatus(await api("/api/account"));
  } catch (_) { /* 状态获取失败不打断页面 */ }
}

async function refreshCaptcha() {
  try {
    const data = await api("/api/account/captcha");
    state.captchaSid = data.sid;
    $("#captchaImage").src = data.image;
  } catch (error) { toast(error.message, true); }
}

function bindAccountControls() {
  $("#captchaImage").addEventListener("click", refreshCaptcha);
  $("#loginButton").addEventListener("click", async () => {
    const payload = {
      sid: state.captchaSid,
      username: $("#accountUsername").value.trim(),
      password: $("#accountPassword").value,
      captcha: $("#accountCaptcha").value.trim(),
    };
    if (!payload.sid) { toast("请先等待验证码加载", true); return; }
    if (!payload.username || !payload.password || !payload.captcha) { toast("请填写用户名、密码和验证码", true); return; }
    try {
      const result = await api("/api/account/login", {method: "POST", body: JSON.stringify(payload)});
      toast(result.message, !result.ok);
      if (result.ok) {
        $("#accountPassword").value = "";
        $("#accountCaptcha").value = "";
      }
      loadAccount();
      refreshCaptcha();
    } catch (error) {
      toast(error.message, true);
      refreshCaptcha();
    }
  });
  $("#verifyLoginButton").addEventListener("click", async () => {
    try {
      const data = await api("/api/account/verify", {method: "POST"});
      updateAccountStatus(data);
      toast(data.message, data.logged_in !== true);
    } catch (error) { toast(error.message, true); }
  });
  $("#logoutButton").addEventListener("click", async () => {
    if (!confirm("退出登录并清除已保存的 Cookie？")) return;
    try {
      await api("/api/account/cookies", {method: "DELETE"});
      state.captchaSid = null;
      loadAccount();
      toast("已退出登录");
    } catch (error) { toast(error.message, true); }
  });
  $("#saveCookiesButton").addEventListener("click", async () => {
    const content = $("#settingCookies").value.trim();
    if (!content) { toast("请先粘贴 Cookie 内容", true); return; }
    try {
      const result = await api("/api/account/cookies", {method: "POST", body: JSON.stringify({content})});
      $("#settingCookies").value = "";
      toast(`已保存 ${result.cookie_count} 条 Cookie`);
      loadAccount();
    } catch (error) { toast(error.message, true); }
  });
}

async function downloadSelectedTasks() {
  const keys = [...state.downloadSelected];
  if (!keys.length) return;
  try {
    const settings = state.settings || await loadSettings() || {};
    const job = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({viewkeys: keys, workers: settings.workers || 2, fragments: settings.fragments || 4}),
    });
    state.downloadSelected.clear();
    watchJob(job);
  } catch (error) { toast(error.message, true); }
}

function collectSettings() {
  return {
    download_dir: $("#settingDownloadDir").value.trim(),
    folder_mode: $("#settingFolderMode").value,
    prefer_hd: $("#settingPreferHd").checked,
    workers: Number($("#settingWorkers").value),
    fragments: Number($("#settingFragments").value),
    ui_refresh_seconds: Number($("#settingUiRefresh").value),
    proxy: $("#settingProxy").value.trim(),
  };
}

// ---- 站点列表 ----

function renderSites(data) {
  state.sites = data.sites || [];
  state.activeSiteId = data.active_id || "";
  const list = $("#siteList");
  if (list) {
    if (!state.sites.length) {
      list.innerHTML = '<div class="site-empty">还没有保存任何站点</div>';
    } else {
      list.innerHTML = state.sites.map(site => {
        const active = site.id === state.activeSiteId;
        return `<div class="site-item${active ? " active" : ""}" data-site-id="${escapeHtml(site.id)}">
          <input class="site-radio" type="radio" name="activeSite" ${active ? "checked" : ""} title="设为当前站点">
          <div class="site-item-body">
            <strong>${escapeHtml(site.name || site.base_url)}</strong>
            <span>${escapeHtml(site.base_url)}</span>
          </div>
          ${active ? '<span class="site-badge">当前</span>' : ""}
          <button class="icon-button delete-site" type="button" title="删除站点"><i data-lucide="trash-2"></i></button>
        </div>`;
      }).join("");
    }
  }
  renderSiteMenu();
  if (window.lucide) lucide.createIcons();
}

// 顶栏站点菜单：让用户不进设置页也能直接切换生效站点
function renderSiteMenu() {
  const menu = $("#siteMenu");
  if (!menu) return;
  const active = state.sites.find(site => site.id === state.activeSiteId);
  if (active) $("#siteHost").textContent = new URL(active.base_url).host;
  menu.innerHTML = state.sites.map(site => `
    <button type="button" class="site-menu-item${site.id === state.activeSiteId ? " active" : ""}" data-site-id="${escapeHtml(site.id)}">
      <span class="check-slot">${site.id === state.activeSiteId ? '<i data-lucide="check"></i>' : ""}</span>
      <span class="site-menu-item-body">
        <strong>${escapeHtml(site.name || site.base_url)}</strong>
        <span>${escapeHtml(site.base_url)}</span>
      </span>
    </button>`).join("") + `<div class="site-menu-foot">
      <button type="button" id="manageSitesButton"><i data-lucide="settings-2"></i>管理站点列表</button>
    </div>`;
  if (window.lucide) lucide.createIcons();
}

async function loadSites() {
  try {
    renderSites(await api("/api/sites"));
  } catch (error) { toast(error.message, true); }
}

async function switchSite(siteId) {
  try {
    const data = await api("/api/sites/active", {method: "POST", body: JSON.stringify({site_id: siteId})});
    renderSites(data);
    closeSiteMenu();
    toast(`已切换到 ${data.site.name}`);
    state.page = 1;
    state.selected.clear();
    await loadCatalog();
  } catch (error) { toast(error.message, true); await loadSites(); }
}

function closeSiteMenu() {
  $("#siteMenu")?.classList.add("hidden");
  $("#siteSwitcherButton")?.classList.remove("open");
}

function bindSiteSwitcher() {
  const button = $("#siteSwitcherButton");
  const menu = $("#siteMenu");
  button.addEventListener("click", event => {
    event.stopPropagation();
    const willOpen = menu.classList.contains("hidden");
    menu.classList.toggle("hidden", !willOpen);
    button.classList.toggle("open", willOpen);
  });
  menu.addEventListener("click", event => {
    if (event.target.closest("#manageSitesButton")) {
      closeSiteMenu();
      state.settingsSection = "settingsSites";
      applyControlState();
      applySettingsSection();
      setView("settings");
      return;
    }
    const item = event.target.closest(".site-menu-item");
    if (!item || item.dataset.siteId === state.activeSiteId) { closeSiteMenu(); return; }
    switchSite(item.dataset.siteId);
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".site-switcher")) closeSiteMenu();
  });
  document.addEventListener("keydown", event => { if (event.key === "Escape") closeSiteMenu(); });
}

async function addSite() {
  const baseUrl = $("#siteUrlInput").value.trim();
  if (!baseUrl) { toast("请填写站点地址", true); return; }
  try {
    const data = await api("/api/sites", {
      method: "POST",
      body: JSON.stringify({name: $("#siteNameInput").value.trim(), base_url: baseUrl}),
    });
    $("#siteNameInput").value = "";
    $("#siteUrlInput").value = "";
    renderSites(data);
    toast("站点已保存");
  } catch (error) { toast(error.message, true); }
}

async function removeSite(siteId, name) {
  if (!confirm(`从站点列表移除「${name}」？已采集的条目不会删除。`)) return;
  const previousActive = state.activeSiteId;
  try {
    const data = await api(`/api/sites/${encodeURIComponent(siteId)}`, {method: "DELETE"});
    renderSites(data);
    toast("站点已移除");
    // 删掉的是当前站点时，后端会自动切到第一个站点，目录要按新域名重新加载
    if (data.active_id !== previousActive) {
      state.page = 1;
      state.selected.clear();
      await loadCatalog();
    }
  } catch (error) { toast(error.message, true); }
}

function bindSiteControls() {
  $("#addSiteButton").addEventListener("click", addSite);
  $("#siteUrlInput").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); addSite(); } });
  $("#siteList").addEventListener("change", event => {
    const radio = event.target.closest(".site-radio");
    if (!radio) return;
    const item = radio.closest(".site-item");
    if (item && item.dataset.siteId !== state.activeSiteId) switchSite(item.dataset.siteId);
  });
  $("#siteList").addEventListener("click", event => {
    const button = event.target.closest(".delete-site");
    if (!button) return;
    const item = button.closest(".site-item");
    const site = state.sites.find(entry => entry.id === item.dataset.siteId);
    removeSite(item.dataset.siteId, site ? site.name || site.base_url : "该站点");
  });
}

async function testProxy() {
  const button = $("#testProxyButton");
  const result = $("#proxyTestResult");
  button.disabled = true;
  result.className = "proxy-result";
  result.textContent = "正在测试…";
  try {
    const data = await api("/api/proxy/test", {
      method: "POST",
      body: JSON.stringify({proxy: $("#settingProxy").value.trim()}),
    });
    result.className = `proxy-result ${data.ok ? "ok" : "bad"}`;
    result.textContent = data.message;
  } catch (error) {
    result.className = "proxy-result bad";
    result.textContent = error.message;
  } finally { button.disabled = false; }
}

function setView(view, {sync = true, replace = false} = {}) {
    state.activeView = view;
  document.querySelectorAll(".side-nav-item").forEach(tab => tab.classList.toggle("active", tab.dataset.view === view));
  $("#catalogView").classList.toggle("hidden", view !== "catalog");
  $("#downloadsView").classList.toggle("hidden", view !== "downloads");
  $("#settingsView").classList.toggle("hidden", view !== "settings");
  $("#searchBox").classList.toggle("hidden", view !== "catalog");
  // 左侧分类区跟随视图切换：目录→视频分类，下载→任务分类，设置→设置分类
  $("#categorySection").classList.toggle("hidden", view !== "catalog");
  $("#downloadFilterSection").classList.toggle("hidden", view !== "downloads");
  $("#settingsNavSection").classList.toggle("hidden", view !== "settings");
  $("#viewTitle").textContent = view === "catalog" ? (state.mode === "search" ? `搜索「${state.query}」` : "视频目录") : view === "downloads" ? "下载管理" : "设置中心";
  $("#viewSubtitle").innerHTML = view === "catalog"
    ? `<span id="resultCount">${state.videos.length}</span> ${state.mode === "search" ? "个搜索结果" : "个采集结果"}`
    : view === "downloads" ? `${state.downloads.length} 个下载任务` : "下载目录、性能和界面设置";
  if (sync) syncRoute(replace);
  if (window.lucide) lucide.createIcons();
}

function applyControlState() {
  document.querySelectorAll(".side-nav-item").forEach(tab => tab.classList.toggle("active", tab.dataset.view === state.activeView));
  document.querySelectorAll("#categorySection .category-item").forEach(item => item.classList.toggle("active", state.mode === "category" && item.dataset.category === state.category));
  document.querySelectorAll("#downloadFilterSection .category-item").forEach(item => item.classList.toggle("active", item.dataset.filter === state.downloadFilter));
  document.querySelectorAll("#settingsNavSection .category-item").forEach(item => item.classList.toggle("active", item.dataset.settingsSection === state.settingsSection));
  document.querySelectorAll(".mode-tab").forEach(tab => tab.classList.toggle("active", tab.dataset.mode === state.mode));
  document.querySelectorAll(".source-pane").forEach(pane => pane.classList.toggle("hidden", pane.dataset.pane !== state.mode));
}

// 设置中心按左侧分类真正分区：同一时间只显示一个设置分区
function applySettingsSection() {
  ["settingsAccount", "settingsSites", "settingsProxy", "settingsStorage", "settingsRefresh"].forEach(id => {
    document.getElementById(id)?.classList.toggle("hidden", id !== state.settingsSection);
  });
}

function setJob(job) {
  const panel = $("#jobPanel");
  panel.classList.remove("empty");
  $("#jobTitle").textContent = job.kind === "crawl" ? "采集任务" : "下载任务";
  $("#jobMessage").textContent = job.error || job.message;
  const percent = job.total ? Math.round(job.current * 100 / job.total) : (job.status === "completed" ? 100 : 35);
  $("#jobProgress").style.width = `${percent}%`;
  const crawlButton = $("#crawlButton");
  if (crawlButton) crawlButton.disabled = job.status === "queued" || job.status === "running";
}

function watchJob(job) {
  state.jobId = job.id;
  setJob(job);
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const current = await api(`/api/jobs/${state.jobId}`);
      setJob(current);
      if (current.kind === "crawl") {
        // 任务未结束时只读本地库，结束后由下方完成分支做一次真实翻页刷新
        if (!["completed", "failed"].includes(current.status)) await refreshCatalogFromStore();
      }
      else await loadDownloads();
      if (["completed", "failed"].includes(current.status)) {
        clearInterval(state.jobTimer);
        const crawlButton = $("#crawlButton");
        if (crawlButton) crawlButton.disabled = false;
        if (current.kind === "crawl") await loadCatalog({refresh: true});
        await loadDownloads();
        toast(current.error || current.message, current.status === "failed");
      }
    } catch (error) {
      clearInterval(state.jobTimer);
      toast(error.message, true);
    }
  }, 1200);
}

// ---- 全站搜索 ----
// 搜索走站点的 search_result.php，不再是当前页的本地过滤；
// 输入停顿 450ms 才发请求，避免每敲一个字都打一次站点
let searchTimer = null;

async function startSearch(query) {
  const changed = state.mode !== "search" || state.query !== query;
  state.mode = "search";
  state.query = query;
  state.page = 1;
  state.pagesLoaded = 0;
  state.hasMore = false;
  if (changed) state.selected.clear();
  applyControlState();
  setView("catalog");
  syncRoute(true);
  await loadPage(1);
}

async function exitSearch() {
  if (state.mode !== "search") {
    if ($("#searchInput").value) $("#searchInput").value = "";
    return;
  }
  state.mode = "category";
  state.query = "";
  state.page = 1;
  state.pagesLoaded = 0;
  state.hasMore = false;
  state.selected.clear();
  applyControlState();
  syncRoute(true);
  await loadPage(1);
}

function bindSearchControls() {
  const input = $("#searchInput");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const query = input.value.trim();
    $("#searchResetButton").classList.toggle("hidden", !query);
    if (!query) {
      if (state.mode === "search") exitSearch();
      return;
    }
    // 单字先等回车，两个字以上自动搜
    if (query.length < 2) return;
    searchTimer = setTimeout(() => startSearch(query), 450);
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(searchTimer);
      const query = input.value.trim();
      if (query) startSearch(query);
      else exitSearch();
    } else if (event.key === "Escape") {
      clearTimeout(searchTimer);
      input.value = "";
      exitSearch();
    }
  });
  $("#clearSearchButton").addEventListener("click", () => {
    clearTimeout(searchTimer);
    input.value = "";
    exitSearch();
    input.focus();
  });
  $("#searchResetButton").addEventListener("click", () => {
    clearTimeout(searchTimer);
    input.value = "";
    exitSearch();
    input.focus();
  });
}

// 滚到底部自动加载下一页（无缝翻页）；不支持 IntersectionObserver 时退化成按钮
function setupInfiniteScroll() {
  const sentinel = $("#loadSentinel");
  if (!sentinel) return;
  if (!("IntersectionObserver" in window)) {
    $("#loadMoreButton").classList.remove("hidden");
    return;
  }
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) maybeLoadMore();
  }, {rootMargin: "700px 0px"});
  observer.observe(sentinel);
}

document.addEventListener("DOMContentLoaded", () => {
  readRoute();
  $("#searchInput").value = state.mode === "search" ? state.query : "";
  applyControlState();
  applySettingsSection();
  setView(state.activeView, {sync: false});
  syncRoute(true);
  if (window.lucide) lucide.createIcons();
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("viewkey-theme", next);
    if (window.lucide) lucide.createIcons();
  });

  document.querySelectorAll(".side-nav-item").forEach(tab => tab.addEventListener("click", () => {
    setView(tab.dataset.view);
    if (tab.dataset.view === "catalog" && state.videos.length === 0) loadCatalog();
    if (tab.dataset.view === "downloads") loadDownloads();
    if (tab.dataset.view === "settings") { loadSettings(); loadAccount(); loadCacheStats(); if (!state.captchaSid) refreshCaptcha(); }
  }));
  bindAccountControls();
  bindSiteControls();
  bindSiteSwitcher();
  $("#testProxyButton").addEventListener("click", testProxy);
  loadAccount();
  loadSites();
  document.querySelectorAll("#categorySection .category-item").forEach(item => item.addEventListener("click", () => {
    const wasSearching = state.mode === "search";
    state.mode = "category";
    state.query = "";
    state.category = item.dataset.category;
    state.page = 1;
    state.pagesLoaded = 0;
    state.hasMore = false;
    state.selected.clear();
    // 切分类时清掉搜索框，否则会让人以为还在搜索结果里
    if (wasSearching || $("#searchInput").value) $("#searchInput").value = "";
    applyControlState();
    setView("catalog");
    loadCatalog();
  }));

  bindSearchControls();
  setupInfiniteScroll();
  $("#retryBrowseButton").addEventListener("click", () => { loadCatalog({refresh: true}); });
  $("#refreshButton").addEventListener("click", async () => {
    state.selected.clear();
    await loadCatalog({refresh: true});
    await loadDownloads();
  });
  $("#loadMoreButton").addEventListener("click", async () => { if (state.hasMore) await loadPage(state.page + 1, {append: true}); });
  $("#clearCacheButton").addEventListener("click", async () => {
    try {
      const data = await api("/api/cache/clear", {method: "POST"});
      await loadCacheStats();
      toast(data.removed ? `已清理 ${data.removed} 页列表缓存` : "缓存已经是空的");
    } catch (error) { toast(error.message, true); }
  });
  $("#settingsForm").addEventListener("submit", async event => {
    event.preventDefault();
    const payload = collectSettings();
    try {
      const data = await api("/api/settings", {method: "PUT", body: JSON.stringify(payload)});
      fillSettings(data);
      restartDownloadPolling();
      $("#settingsSaveMessage").textContent = "已保存";
      setTimeout(() => { $("#settingsSaveMessage").textContent = ""; }, 3000);
    } catch (error) { toast(error.message, true); }
  });
  $("#selectAllDownloads").addEventListener("change", event => {
    downloadsMatchingFilter(state.downloadFilter).forEach(item => event.target.checked
      ? state.downloadSelected.add(item.viewkey)
      : state.downloadSelected.delete(item.viewkey));
    renderDownloads(Number($("#totalDiskUsage").dataset.bytes || 0));
  });
  document.querySelectorAll("#downloadFilterSection .category-item").forEach(button => button.addEventListener("click", () => {
    state.downloadFilter = button.dataset.filter;
    $("#selectAllDownloads").checked = false;
    applyControlState();
    renderDownloads(Number($("#totalDiskUsage").dataset.bytes || 0));
    window.scrollTo({top: 0});
  }));
  document.querySelectorAll("#settingsNavSection .category-item").forEach(button => button.addEventListener("click", () => {
    state.settingsSection = button.dataset.settingsSection;
    persistUiState();
    applyControlState();
    applySettingsSection();
  }));
  $("#removeDownloadsButton").addEventListener("click", async () => {
    if (!state.downloadSelected.size) return;
    if (!confirm(`从下载管理移除 ${state.downloadSelected.size} 个任务？本地视频文件不会删除。`)) return;
    try {
      await api("/api/downloads/remove", {method: "POST", body: JSON.stringify({viewkeys: [...state.downloadSelected]})});
      state.downloadSelected.clear();
      await loadDownloads();
      toast("已删除所选下载任务");
    } catch (error) { toast(error.message, true); }
  });
  $("#downloadSelectedButton").addEventListener("click", downloadSelectedTasks);
  $("#downloadMissingButton").addEventListener("click", async () => {
    try {
      // 只续传下载管理里「未完成」的任务；已删除的任务和从没进过队列的采集结果不会被拉回来
      const data = await api("/api/downloads/unfinished");
      if (!data.count) return toast("下载管理里没有未完成的任务");
      if (!confirm(`将重新下载下载管理里的 ${data.count} 个未完成任务，继续？`)) return;
      const job = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({viewkeys: data.viewkeys, workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4}),
      });
      toast(`已加入 ${job.total ?? data.count} 个下载任务`);
      watchJob(job);
    } catch (error) { toast(error.message, true); }
  });
  $("#retryAllButton").addEventListener("click", async () => {
    const failed = state.downloads.filter(item => item.state === "failed").map(item => item.viewkey);
    if (!failed.length) return;
    try {
      const job = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({viewkeys: failed, workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4}),
      });
      watchJob(job);
    } catch (error) { toast(error.message, true); }
  });

  $("#selectAll").addEventListener("change", event => {
    visibleVideos().forEach(video => event.target.checked ? state.selected.add(videoKey(video)) : state.selected.delete(videoKey(video)));
    render();
  });

  $("#downloadButton").addEventListener("click", async () => {
    try {
      const job = await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({viewkeys: [...state.selected], workers: state.settings?.workers || 2, fragments: state.settings?.fragments || 4}),
      });
      watchJob(job);
      state.selected.clear();
      render();
      toast(`已加入 ${job.total || 0} 个下载任务，可继续选择视频`);
    } catch (error) { toast(error.message, true); }
  });

  $("#removeButton").addEventListener("click", async () => {
    if (!confirm(`从目录移出 ${state.selected.size} 个视频？已下载文件不会删除。`)) return;
    try {
      await api("/api/catalog/remove", {method: "POST", body: JSON.stringify({viewkeys: [...state.selected]})});
      state.selected.clear();
      await loadCatalog();
    } catch (error) { toast(error.message, true); }
  });

  // Keep the interface interactive while a slow listing page is loading.
  void (async () => {
    try {
      const config = await api("/api/config");
      $("#siteHost").textContent = new URL(config.base_url).host;
      if (config.sites?.length) renderSites({sites: config.sites, active_id: config.site_id});
    } catch (_) {}
    await loadSettings();
    if (state.activeView === "settings") loadCacheStats();
    restartDownloadPolling();
    if (state.activeView === "downloads") await loadDownloads();
    else if (state.activeView === "catalog") { await loadCatalog(); await loadDownloads(); }
  })();
});

window.addEventListener("popstate", () => {
  readRoute();
  $("#searchInput").value = state.mode === "search" ? state.query : "";
  applyControlState();
  applySettingsSection();
  state.selected.clear();
  render();
  setView(state.activeView, {sync: false});
  persistUiState();
  if (state.activeView === "catalog") loadCatalog();
  if (state.activeView === "downloads") loadDownloads();
  if (state.activeView === "settings") { loadSettings(); loadCacheStats(); }
});
