"use strict";

const state = {
  config: null,
  status: null,
  alerts: [],
  dirty: false,
  busy: false,
  news: null,
  newsSettings: null,
  newsAgentSettings: null,
  newsAgentBusy: false,
  newsAgentResult: null,
  radarBusy: false,
  radarLoaded: false,
  radarSource: "all",
  activeView: "monitor",
  monitorStockIndex: null,
  access: null,
  syncBusy: false,
  conflict: false,
  connected: false,
  radarRevision: null,
};

const sourceNames = { tencent: "腾讯", eastmoney: "东方财富", sina: "新浪" };
const monitorItemCatalog = [
  { id: "open_board", label: "开板预警", note: "封单消失但价格仍在涨停价附近" },
  { id: "bomb", label: "炸板", note: "触及涨停后价格跌离涨停价" },
  { id: "reseal", label: "回封", note: "开板或炸板后重新封上涨停" },
  { id: "rapid_rise", label: "快速拉升", note: "设定时间窗口内快速上涨" },
  { id: "rapid_fall", label: "快速下跌", note: "设定时间窗口内快速下跌" },
  { id: "average", label: "分时均价穿越", note: "跌破或重新收复分时均价" },
  { id: "cost", label: "成本线穿越", note: "跌破或重新收复持仓成本线" },
  { id: "ma5", label: "MA5 穿越", note: "跌破或重新收复动态五日线" },
];
const allMonitorItemIds = monitorItemCatalog.map((item) => item.id);
const runtimeLabels = {
  stopped: "监控已停止",
  starting: "正在启动",
  running: "盘中实时监控中",
  waiting: "休市等待中",
  refreshing: "正在刷新行情",
  stopping: "正在停止",
  error: "监控异常",
};
const marketPhaseLabels = {
  open_auction: "开盘集合竞价",
  auction_gap: "竞价静默期",
  continuous: "连续竞价",
  lunch_break: "午间休市",
  closing_auction: "收盘集合竞价",
  custom: "自定义监控时段",
  closed: "当前休市",
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* empty response */ }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = "toast"; }, 2800);
}

function markDirty() {
  state.dirty = true;
  $("#dirtyHint").hidden = false;
  $("#saveButton").textContent = "保存修改";
  renderSyncState();
}

function markSaved() {
  state.dirty = false;
  state.conflict = false;
  $("#dirtyHint").hidden = true;
  $("#saveButton").textContent = "保存自选";
  renderSyncState();
}

function renderSyncState() {
  $("#connectionLabel").textContent = !state.connected ? "连接已断开 · 数据可能过期"
    : state.access?.remote ? "私有加密连接 · 手机 / 远程端" : "电脑本机 · 共享后台";
  $("#connectionBar").classList.toggle("offline", !state.connected);
  $("#syncLabel").textContent = state.conflict ? "另一端已修改，请处理版本冲突"
    : state.dirty ? "当前修改仅在此页面，保存后同步" : "自选、监控和提醒自动同步";
  $("#syncConflict").hidden = !state.conflict;
}

async function loadAccess() {
  state.access = await api("/api/access");
  if (state.access.remote) {
    state.newsAgentSettings = state.access.agent;
    renderNewsAgentState();
  }
  $("#newsAgentSettingsButton").disabled = Boolean(state.access.remote);
  $("#newsAgentSettingsButton").title = state.access.remote ? "API 地址和密钥只能在电脑上修改" : "配置 Agent API";
  renderSyncState();
}

async function syncSharedData() {
  if (document.hidden || state.syncBusy || state.busy) return;
  state.syncBusy = true;
  const baseRevision = state.config?._revision;
  try {
    const [config, agent] = await Promise.all([api("/api/config"), api("/api/news-agent/result")]);
    if (state.busy) return;
    if (state.config?._revision === baseRevision && config._revision !== baseRevision) {
      if (state.dirty || document.querySelector("dialog[open]")) {
        state.conflict = true;
      } else {
        state.config = config;
        state.conflict = false;
        state.newsSettings = config.news_radar;
        state.radarLoaded = false;
        renderStocks();
      }
    }
    if (!state.newsAgentBusy && agent.result && agent.result.result_id !== state.newsAgentResult?.result_id) {
      renderNewsAgentResult(agent.result);
    }
    renderSyncState();
  } catch (_) { /* status polling reports connectivity; keep unsaved input */ }
  finally { state.syncBusy = false; }
}

async function reloadSharedConfig() {
  if (!window.confirm("载入电脑上的最新配置会放弃本页面尚未保存的修改，是否继续？")) return;
  try {
    const config = await api("/api/config");
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    state.config = config;
    state.newsSettings = config.news_radar;
    state.radarLoaded = false;
    markSaved();
    renderStocks();
    toast("已载入最新共享配置");
  } catch (error) { toast(error.message, true); }
}

async function openRemoteSettings() {
  try {
    await loadAccess();
    const policy = state.access.policy || {};
    $("#remoteState").textContent = state.access.remote ? "当前已通过 Tailscale 账号验证"
      : !state.access.remote_port ? "远程监听未就绪，请重启软件并检查 8766 端口"
      : policy.enabled ? "远程授权已配置；实际可达性仍取决于 Tailscale 和电脑网络" : "远程授权未启用，本机可正常使用";
    $("#remoteUrl").value = state.access.remote ? location.origin : policy.origin || "运行 remote.cmd 后生成私有地址";
    $("#remoteOwner").textContent = policy.owner_login ? `仅限账号：${policy.owner_login}` : "电脑和手机使用同一个 Tailscale 账号";
    $("#disableRemoteButton").hidden = state.access.remote || !policy.enabled;
    $("#copyRemoteButton").disabled = !state.access.remote && !policy.origin;
    $("#remoteDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

function numberOrNull(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function formatPrice(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "—";
}

function formatNumber(value, digits = 2, suffix = "") {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(digits)}${suffix}` : "—";
}

function formatTime(value, includeDate = false) {
  if (!value) return "尚未获取";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: includeDate ? "2-digit" : undefined,
    day: includeDate ? "2-digit" : undefined,
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

function switchView(view) {
  state.activeView = view === "news" ? "news" : "monitor";
  const newsActive = state.activeView === "news";
  $("#monitorView").hidden = newsActive;
  $("#newsView").hidden = !newsActive;
  document.body.classList.toggle("news-active", newsActive);
  document.querySelectorAll(".module-tab").forEach((button) => {
    const active = button.dataset.view === state.activeView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (newsActive && !state.radarLoaded) loadRadar();
}

function radarCategoryClass(category) {
  return ({ 财经: "finance", 市场: "market", 热股: "market", 快讯: "flash", 热榜: "hot", 科技: "tech" })[category] || "news";
}

function formatRadarNumber(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "";
}

function hasRadarNumber(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function formatRadarHeat(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(number);
}

function renderRadar() {
  const payload = state.news;
  if (!payload) return;
  const sources = payload.sources || [];
  const items = payload.items || [];
  const online = sources.filter((source) => source.ok);
  const latencies = online.map((source) => Number(source.latency_ms)).filter(Number.isFinite);
  $("#radarSourceCount").textContent = `${online.length}/${sources.length}`;
  $("#radarItemCount").textContent = String(payload.item_count ?? items.length);
  $("#radarRelatedCount").textContent = String(payload.related_count ?? 0);
  $("#radarLatency").textContent = latencies.length ? String(Math.min(...latencies)) : "—";
  $("#radarUpdated").textContent = payload.cache_hit
    ? `${formatTime(payload.fetched_at)} · 缓存 ${Math.round(payload.cache_age_seconds || 0)} 秒`
    : formatTime(payload.fetched_at);
  $("#radarStatus").textContent = payload.message || "扫描完成";
  $("#radarDot").className = `status-dot ${payload.ok ? "running" : "error"}`;
  $("#radarTabMeta").textContent = payload.ok ? `${online.length} 个平台在线` : "获取失败";
  const badge = $("#radarRelatedBadge");
  badge.textContent = String(payload.related_count || 0);
  badge.hidden = !payload.related_count;

  const availableSources = sources.filter((source) => source.item_count > 0);
  if (state.radarSource !== "all" && !availableSources.some((source) => source.id === state.radarSource)) {
    state.radarSource = "all";
  }
  $("#radarSourceFilters").innerHTML = [
    `<button class="${state.radarSource === "all" ? "active" : ""}" type="button" data-source="all">全部平台 <small>${items.length}</small></button>`,
    ...availableSources.map((source) => `<button class="${state.radarSource === source.id ? "active" : ""}" type="button" data-source="${escapeHtml(source.id)}">${escapeHtml(source.name)} <small>${source.item_count}</small></button>`),
  ].join("");

  const watchLabels = [...new Set((payload.watch_terms || []).map((item) => item.label).filter(Boolean))];
  $("#radarWatchTerms").innerHTML = watchLabels.length
    ? watchLabels.map((label) => `<span>${escapeHtml(label)}</span>`).join("")
    : "<span>自选股尚未设置名称</span>";
  $("#radarSourceHealth").innerHTML = sources.map((source) => `
    <div class="radar-health-row ${source.ok ? "ok" : "failed"}" title="${escapeHtml(source.error || "")}">
      <i></i><span>${escapeHtml(source.name)}</span><small>${source.ok ? `${source.latency_ms}ms · ${source.item_count} 条` : "获取失败"}</small>
    </div>`).join("");

  const query = $("#radarSearch").value.trim().toLowerCase();
  const relatedOnly = $("#radarRelatedOnly").checked;
  const filtered = items.filter((item) => {
    if (state.radarSource !== "all" && item.source_id !== state.radarSource) return false;
    if (relatedOnly && !item.related) return false;
    if (!query) return true;
    return [item.title, item.stock_code, item.source_name, ...(item.matched_keywords || []), ...(item.matched_stocks || [])]
      .join(" ").toLowerCase().includes(query);
  });
  $("#radarResultHint").textContent = `显示 ${filtered.length} / ${items.length} 条`;
  const feed = $("#radarFeed");
  if (!filtered.length) {
    feed.innerHTML = '<div class="radar-empty"><span class="radar-icon"></span><strong>没有符合条件的资讯</strong><p>可清空搜索或关闭“只看与我相关”。</p></div>';
    return;
  }
  feed.innerHTML = filtered.map((item) => {
    const stockTags = (item.matched_stocks || []).map((value) => `<span class="radar-match stock">自选 · ${escapeHtml(value)}</span>`).join("");
    const keywordTags = (item.matched_keywords || []).map((value) => `<span class="radar-match keyword">${escapeHtml(value)}</span>`).join("");
    const change = hasRadarNumber(item.change_pct) ? Number(item.change_pct) : null;
    const changeText = change !== null ? `${change > 0 ? "+" : ""}${formatRadarNumber(change)}%` : "";
    const rankChange = hasRadarNumber(item.rank_change) ? Number(item.rank_change) : null;
    const marketTags = [
      item.stock_code ? `<span class="radar-stock-code">${escapeHtml(item.stock_code)}</span>` : "",
      hasRadarNumber(item.price) ? `<span>现价 ${formatRadarNumber(item.price)}</span>` : "",
      changeText ? `<strong class="${change > 0 ? "up" : change < 0 ? "down" : "flat"}">${changeText}</strong>` : "",
      hasRadarNumber(item.heat) ? `<span>热度 ${formatRadarHeat(item.heat)}</span>` : "",
      rankChange !== null && rankChange !== 0 ? `<span>排名变动 ${rankChange > 0 ? "+" : ""}${formatRadarNumber(rankChange, 0)}</span>` : "",
      item.hot_tag ? `<span class="radar-hot-tag">${escapeHtml(item.hot_tag)}</span>` : "",
    ].filter(Boolean).join("");
    const content = `
      <span class="radar-rank">${String(item.rank).padStart(2, "0")}</span>
      <div class="radar-item-main">
        <div class="radar-item-meta"><span class="radar-category ${radarCategoryClass(item.category)}">${escapeHtml(item.category)}</span><span>${escapeHtml(item.source_name)}</span><time>${formatTime(item.updated_at)}</time></div>
        <h3>${escapeHtml(item.title)}</h3>
        ${marketTags ? `<div class="radar-market-tags">${marketTags}</div>` : ""}
        ${stockTags || keywordTags ? `<div class="radar-matches">${stockTags}${keywordTags}</div>` : ""}
      </div>
      <span class="radar-open" aria-hidden="true">↗</span>`;
    return item.url
      ? `<a class="radar-item ${item.related ? "related" : ""}" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${content}</a>`
      : `<article class="radar-item ${item.related ? "related" : ""}">${content.replace('<span class="radar-open" aria-hidden="true">↗</span>', '<span class="radar-open muted" aria-hidden="true">—</span>')}</article>`;
  }).join("");
}

async function loadRadar(force = false) {
  if (state.radarBusy) return;
  state.radarBusy = true;
  $("#radarRefreshButton").disabled = true;
  $("#radarRefreshButton").textContent = "扫描中…";
  $("#radarDot").className = "status-dot running";
  $("#radarStatus").textContent = "正在并发扫描资讯平台";
  try {
    state.news = await api(`/api/news-radar${force ? "?refresh=1" : ""}`);
    state.newsSettings = state.news.settings;
    state.radarLoaded = true;
    renderRadar();
  } catch (error) {
    $("#radarDot").className = "status-dot error";
    $("#radarStatus").textContent = error.message;
    $("#radarFeed").innerHTML = `<div class="radar-empty error"><span class="radar-icon"></span><strong>资讯扫描失败</strong><p>${escapeHtml(error.message)}</p></div>`;
    toast(error.message, true);
  } finally {
    state.radarBusy = false;
    $("#radarRefreshButton").disabled = false;
    $("#radarRefreshButton").textContent = "立即扫描";
  }
}

async function openRadarSettings() {
  try {
    const payload = await api("/api/news-radar/settings");
    state.radarRevision = payload._revision;
    state.newsSettings = payload.settings;
    $("#radarRefreshInterval").value = payload.settings.refresh_interval_seconds;
    $("#radarMaxItems").value = payload.settings.max_items_per_source;
    $("#radarKeywords").value = (payload.settings.keywords || []).join("，");
    $("#radarPlatformOptions").innerHTML = (payload.catalog || []).map((platform) => `
      <label class="check-card">
        <input type="checkbox" name="radarPlatform" value="${escapeHtml(platform.id)}" ${(payload.settings.platforms || []).includes(platform.id) ? "checked" : ""}>
        <span><b>${escapeHtml(platform.name)}</b><small>${escapeHtml(platform.category)}</small></span>
      </label>`).join("");
    $("#radarSettingsDialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveRadarSettings() {
  const platforms = [...document.querySelectorAll('input[name="radarPlatform"]:checked')].map((input) => input.value);
  if (!platforms.length || platforms.length > 16) {
    toast(platforms.length ? "资讯平台最多选择 16 个" : "至少选择一个资讯平台", true);
    return false;
  }
  const keywords = $("#radarKeywords").value.split(/[,，\n]/).map((value) => value.trim()).filter(Boolean);
  const payload = {
    ...(state.newsSettings || {}),
    _revision: state.radarRevision,
    platforms,
    keywords,
    refresh_interval_seconds: Number($("#radarRefreshInterval").value),
    max_items_per_source: Number($("#radarMaxItems").value),
  };
  try {
    const saved = await api("/api/news-radar/settings", { method: "PUT", body: JSON.stringify(payload) });
    if (state.config._revision === state.radarRevision) {
      state.config.news_radar = saved.settings;
      state.config._revision = saved._revision;
    }
    state.newsSettings = saved.settings;
    state.news = null;
    state.radarLoaded = false;
    $("#radarSettingsDialog").close();
    toast("资讯雷达设置已保存");
    await loadRadar(true);
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function currentRadarItemIds() {
  const items = state.news?.items || [];
  const query = $("#radarSearch").value.trim().toLowerCase();
  const relatedOnly = $("#radarRelatedOnly").checked;
  return items.filter((item) => {
    if (state.radarSource !== "all" && item.source_id !== state.radarSource) return false;
    if (relatedOnly && !item.related) return false;
    if (!query) return true;
    return [item.title, item.stock_code, item.source_name, ...(item.matched_keywords || []), ...(item.matched_stocks || [])]
      .join(" ").toLowerCase().includes(query);
  }).map((item) => String(item.id || "")).filter(Boolean).slice(0, 120);
}

function renderNewsAgentState() {
  const settings = state.newsAgentSettings;
  const node = $("#newsAgentConfigState");
  if (!settings?.configured) {
    node.textContent = "API 未配置";
    node.className = "agent-config-state";
    return;
  }
  node.textContent = settings.api_key_configured ? "API 已配置" : "接口已配置 · 无密钥";
  node.className = "agent-config-state configured";
}

async function loadNewsAgentSettings() {
  if (state.access?.remote) {
    await loadAccess();
    return state.newsAgentSettings;
  }
  const payload = await api("/api/news-agent/settings");
  state.newsAgentSettings = payload.settings;
  renderNewsAgentState();
  return payload.settings;
}

async function openNewsAgentSettings() {
  if (state.access?.remote) {
    toast("请在电脑本机配置 Agent API 地址和密钥", true);
    return;
  }
  try {
    const settings = await loadNewsAgentSettings();
    $("#newsAgentApiUrl").value = settings.api_url || "";
    $("#newsAgentModel").value = settings.model || "";
    $("#newsAgentTimeout").value = settings.request_timeout_seconds ?? 60;
    $("#newsAgentMaxNews").value = settings.max_news_items ?? 60;
    $("#newsAgentTemperature").value = settings.temperature ?? 0.2;
    $("#newsAgentApiKey").value = "";
    $("#newsAgentApiKey").placeholder = settings.api_key_configured
      ? "已保存密钥；留空则保持不变"
      : "输入 API 密钥；本地免密接口可留空";
    $("#newsAgentKeyHint").textContent = settings.api_key_configured
      ? "本机已有密钥。网页无法读取密钥原文；留空保存不会覆盖。"
      : "密钥只写入本机 data/news-agent.json，不会返回给网页或放入发布压缩包。";
    $("#newsAgentClearKey").checked = false;
    $("#newsAgentSettingsDialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveNewsAgentSettings() {
  const payload = {
    api_url: $("#newsAgentApiUrl").value.trim(),
    model: $("#newsAgentModel").value.trim(),
    request_timeout_seconds: Number($("#newsAgentTimeout").value),
    max_news_items: Number($("#newsAgentMaxNews").value),
    temperature: Number($("#newsAgentTemperature").value),
    api_key: $("#newsAgentApiKey").value.trim(),
    clear_api_key: $("#newsAgentClearKey").checked,
  };
  try {
    const result = await api("/api/news-agent/settings", { method: "PUT", body: JSON.stringify(payload) });
    state.newsAgentSettings = result.settings;
    renderNewsAgentState();
    $("#newsAgentSettingsDialog").close();
    toast("Agent API 设置已保存在本机");
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function agentArray(value) {
  return Array.isArray(value) ? value : [];
}

function agentText(value, fallback = "—") {
  const text = String(value ?? "").trim();
  return escapeHtml(text || fallback);
}

function agentStockTags(stocks) {
  return agentArray(stocks).map((stock) => {
    if (typeof stock === "string") return `<span>${agentText(stock)}</span>`;
    const label = [stock?.name, stock?.code].filter(Boolean).join(" · ");
    return `<span>${agentText(label)}</span>`;
  }).join("");
}

function agentNewsIds(ids) {
  const values = agentArray(ids).filter(Boolean);
  return values.length ? `<div class="agent-news-ids">新闻 ${values.map((id) => agentText(id)).join(" · ")}</div>` : "";
}

function agentConfidence(value) {
  const label = String(value || "不明确");
  const css = label.includes("高") ? "high" : label.includes("中") ? "medium" : "low";
  return `<span class="agent-confidence ${css}">${agentText(label)}置信度</span>`;
}

function renderNewsAgentResult(payload) {
  state.newsAgentResult = payload;
  const target = $("#newsAgentResult");
  const metadata = payload?.metadata || {};
  if (!payload?.structured) {
    target.innerHTML = `
      <div class="agent-result-head"><div><span class="eyebrow">AGENT RESULT</span><h3>分析结果</h3></div></div>
      <pre class="agent-raw">${agentText(payload?.raw_text, "接口没有返回可展示的内容")}</pre>
      <div class="agent-meta">${agentText(metadata.model, "未知模型")} · ${agentText(metadata.api_host, "未知接口")} · ${agentText(metadata.latency_ms, "—")} ms</div>`;
    return;
  }
  const analysis = payload.analysis || {};
  const themes = agentArray(analysis.themes);
  const newsRelations = agentArray(analysis.news_to_market);
  const hotRelations = agentArray(analysis.hot_stock_to_news);
  const watchImpacts = agentArray(analysis.watchlist_impacts);
  const risks = agentArray(analysis.risks);
  target.innerHTML = `
    <div class="agent-result-head">
      <div><span class="eyebrow">AGENT RESULT</span><h3>资讯与市场关联图谱</h3></div>
      <span>${agentText(metadata.news_count, "0")} 条上下文</span>
    </div>
    <div class="agent-overview">${agentText(analysis.overview, "本轮没有形成明确结论")}</div>
    ${themes.length ? `<section class="agent-result-section"><h4>板块主题</h4><div class="agent-theme-grid">${themes.map((theme) => `
      <article class="agent-theme-card">
        <div><strong>${agentText(theme?.sector, "未命名板块")}</strong><span class="agent-direction ${String(theme?.direction || "").includes("利好") ? "up" : String(theme?.direction || "").includes("利空") ? "down" : ""}">${agentText(theme?.direction, "中性")}</span></div>
        <p>${agentText(theme?.reason)}</p>
        <div class="agent-tags">${agentStockTags(theme?.related_stocks)}</div>
        ${agentNewsIds(theme?.related_news_ids)}
      </article>`).join("")}</div></section>` : ""}
    ${newsRelations.length ? `<section class="agent-result-section"><h4>新闻 → 股票 / 板块</h4><div class="agent-relation-list">${newsRelations.map((relation) => `
      <article>
        <div class="agent-relation-title"><strong>${agentText(relation?.title, relation?.news_id || "新闻")}</strong>${agentConfidence(relation?.confidence)}</div>
        <p>${agentText(relation?.relation)}</p>
        <div class="agent-tags">${agentArray(relation?.sectors).map((sector) => `<span class="sector">${agentText(sector)}</span>`).join("")}${agentStockTags(relation?.stocks)}</div>
        ${agentNewsIds([relation?.news_id].filter(Boolean))}
      </article>`).join("")}</div></section>` : ""}
    ${hotRelations.length ? `<section class="agent-result-section"><h4>热榜股票 → 相关新闻</h4><div class="agent-relation-list hot">${hotRelations.map((relation) => `
      <article>
        <div class="agent-relation-title"><strong>${relation?.hot_rank ? `#${agentText(relation.hot_rank)} ` : ""}${agentText(relation?.stock_name, relation?.stock_code || "热榜股票")}</strong>${agentConfidence(relation?.confidence)}</div>
        <p>${agentText(relation?.relation)}</p>
        ${agentArray(relation?.news_titles).length ? `<ul>${agentArray(relation.news_titles).map((title) => `<li>${agentText(title)}</li>`).join("")}</ul>` : ""}
        ${agentNewsIds(relation?.related_news_ids)}
      </article>`).join("")}</div></section>` : ""}
    ${watchImpacts.length ? `<section class="agent-result-section"><h4>自选股影响</h4><div class="agent-theme-grid compact">${watchImpacts.map((impact) => `
      <article class="agent-theme-card"><div><strong>${agentText([impact?.name, impact?.code].filter(Boolean).join(" · "), "自选股")}</strong><span class="agent-direction">${agentText(impact?.direction, "不明确")}</span></div><p>${agentText(impact?.reason)}</p>${agentNewsIds(impact?.related_news_ids)}</article>`).join("")}</div></section>` : ""}
    ${risks.length ? `<section class="agent-result-section agent-risks"><h4>信息边界与风险</h4><ul>${risks.map((risk) => `<li>${agentText(risk)}</li>`).join("")}</ul></section>` : ""}
    <div class="agent-meta">${agentText(metadata.model, "未知模型")} · ${agentText(metadata.api_host, "未知接口")} · ${agentText(metadata.latency_ms, "—")} ms · 热榜 ${agentText(metadata.hot_stock_count, "0")} 条</div>`;
}

async function runNewsAgent() {
  if (state.newsAgentBusy) return;
  try {
    if (!state.newsAgentSettings) await loadNewsAgentSettings();
    if (!state.newsAgentSettings?.configured) {
      toast("请先配置 Agent API 地址和模型", true);
      await openNewsAgentSettings();
      return;
    }
    if (!state.radarLoaded) await loadRadar(false);
    if (!state.news) throw new Error("资讯雷达尚未取得可分析的数据");
    state.newsAgentBusy = true;
    $("#newsAgentRunButton").disabled = true;
    $("#newsAgentRunButton").textContent = "关联分析中…";
    $("#newsAgentResult").innerHTML = '<div class="agent-loading"><i></i><strong>Agent 正在建立新闻、股票与板块关联</strong><p>已提交当前筛选条件下的资讯，同时保留三大热榜前列股票。</p></div>';
    const result = await api("/api/news-agent/analyze", {
      method: "POST",
      body: JSON.stringify({
        question: $("#newsAgentQuestion").value.trim(),
        item_ids: currentRadarItemIds(),
      }),
    });
    renderNewsAgentResult(result);
    toast("资讯关联分析完成");
  } catch (error) {
    $("#newsAgentResult").innerHTML = `<div class="agent-empty error"><strong>Agent 分析失败</strong><p>${escapeHtml(error.message)}</p><button class="button button-ghost" type="button" id="agentErrorSettingsButton">检查 API 设置</button></div>`;
    $("#agentErrorSettingsButton")?.addEventListener("click", openNewsAgentSettings);
    toast(error.message, true);
  } finally {
    state.newsAgentBusy = false;
    $("#newsAgentRunButton").disabled = false;
    $("#newsAgentRunButton").textContent = "分析当前资讯";
  }
}

function quoteMap() {
  const quotes = state.status?.snapshot?.quotes || [];
  return new Map(quotes.map((quote) => [quote.code, quote]));
}

function monitorItemsFor(stock) {
  const configured = Array.isArray(stock?.monitor_items) ? stock.monitor_items : allMonitorItemIds;
  const selected = new Set(configured);
  return monitorItemCatalog.filter((item) => selected.has(item.id));
}

function monitorOptionsHtml(selectedIds, inputName) {
  const selected = new Set(selectedIds);
  return monitorItemCatalog.map((item) => `
    <label class="check-card monitor-rule-card">
      <input type="checkbox" name="${inputName}" value="${item.id}" ${selected.has(item.id) ? "checked" : ""}>
      <span><b>${item.label}</b><small>${item.note}</small></span>
    </label>`).join("");
}

function openStockMonitor(index) {
  syncStockEdits();
  const stock = state.config.stocks[index];
  if (!stock) return;
  state.monitorStockIndex = index;
  const selected = monitorItemsFor(stock).map((item) => item.id);
  $("#stockMonitorTitle").textContent = `${stock.name || stock.code} · 监控项目`;
  $("#stockMonitorSubtitle").textContent = `${stock.code} · 只会生成已勾选项目的盘中提醒`;
  $("#stockMonitorOptions").innerHTML = monitorOptionsHtml(selected, "stockMonitorItem");
  $("#stockMonitorDialog").showModal();
}

function saveStockMonitor() {
  const selected = [...document.querySelectorAll('input[name="stockMonitorItem"]:checked')]
    .map((input) => input.value);
  if (!selected.length) {
    toast("至少选择一个监控项目；如需暂停整只股票，请关闭表格中的“监控”开关", true);
    return false;
  }
  const stock = state.config.stocks[state.monitorStockIndex];
  if (!stock) return false;
  stock.monitor_items = allMonitorItemIds.filter((item) => selected.includes(item));
  $("#stockMonitorDialog").close();
  state.monitorStockIndex = null;
  markDirty();
  renderStocks();
  return true;
}

function renderStocks() {
  const body = $("#stockTableBody");
  const stocks = state.config?.stocks || [];
  const quotes = quoteMap();
  if (!stocks.length) {
    body.innerHTML = '<tr><td colspan="12" class="empty-cell">暂无自选股，请点击“添加股票”</td></tr>';
    return;
  }
  body.innerHTML = stocks.map((stock, index) => {
    const quote = quotes.get(stock.code);
    const monitorItems = monitorItemsFor(stock);
    const monitorItemIds = new Set(monitorItems.map((item) => item.id));
    const autoMa5 = stock.auto_ma5 !== false;
    const effectiveMa5 = quote?.ma5 ?? stock.ma5;
    const ma5Mode = quote?.ma5_mode || (autoMa5 ? "unavailable" : "manual");
    const ma5ModeText = ma5Mode === "auto"
      ? `自动 · ${(quote.ma5_sources || []).map((name) => sourceNames[name] || name).join("+")}`
      : ma5Mode === "fallback" ? "自动失败 · 使用备用值" : ma5Mode === "manual" ? "手工值" : "等待历史日线";
    const ma5History = (quote?.ma5_completed_closes || []).map((item) => `${item.date} ${formatPrice(item.close)}`).join(" / ");
    const change = quote?.change_pct;
    const changeClass = change > 0 ? "positive" : change < 0 ? "negative" : "neutral";
    const boardEnabled = ["open_board", "bomb", "reseal"].some((item) => monitorItemIds.has(item));
    const boardSignal = boardEnabled && quote?.board_state === "sealed"
      ? '<span class="signal board">封板</span>'
      : boardEnabled && quote?.board_state === "opened" ? '<span class="signal opened">当日开板</span>' : "";
    const lineSignals = quote ? [
      monitorItemIds.has("average") ? (quote.below_average ? '<span class="signal bad">均价下</span>' : '<span class="signal">均价上</span>') : "",
      monitorItemIds.has("cost") && stock.cost ? (quote.below_cost ? '<span class="signal bad">成本下</span>' : '<span class="signal">成本上</span>') : "",
      monitorItemIds.has("ma5") && effectiveMa5 ? (quote.below_ma5 ? '<span class="signal bad">MA5 下</span>' : '<span class="signal">MA5 上</span>') : "",
    ].join("") : '<span class="signal">等待行情</span>';
    const monitorSummary = monitorItems.map((item) => item.label).join("、");
    return `
      <tr data-index="${index}">
        <td><input class="row-check stock-enabled" type="checkbox" ${stock.enabled !== false ? "checked" : ""} aria-label="监控 ${escapeHtml(stock.code)}"></td>
        <td><input class="row-check stock-widget-enabled" type="checkbox" ${stock.widget_enabled !== false ? "checked" : ""} aria-label="在悬浮窗显示 ${escapeHtml(stock.code)}"></td>
        <td class="stock-ident"><strong>${escapeHtml(stock.name || quote?.name || "待识别")}</strong><small>${escapeHtml(stock.code)}</small></td>
        <td class="price ${changeClass}"><strong>${formatPrice(quote?.last)}</strong><small>高 ${formatPrice(quote?.high)}</small></td>
        <td class="stock-change ${changeClass}">${Number.isFinite(change) ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : "—"}</td>
        <td>${formatPrice(quote?.average_price)}</td>
        <td><input class="cell-input stock-cost" type="number" min="0.01" step="0.01" value="${stock.cost ?? ""}" placeholder="未设置" aria-label="${escapeHtml(stock.code)} 成本线"></td>
        <td>
          <div class="ma5-editor" title="${escapeHtml(ma5History || quote?.ma5_error || ma5ModeText)}">
            <div class="ma5-reading"><strong>${formatPrice(effectiveMa5)}</strong><small>${escapeHtml(ma5ModeText)}</small></div>
            <label class="auto-ma5-check"><input class="stock-auto-ma5" type="checkbox" ${autoMa5 ? "checked" : ""}>自动</label>
            <input class="cell-input stock-ma5" type="number" min="0.01" step="0.01" value="${stock.ma5 ?? ""}" placeholder="备用" title="手工值；自动历史日线不可用时作为备用" aria-label="${escapeHtml(stock.code)} 备用 MA5">
          </div>
        </td>
        <td><button class="monitor-items-button" type="button" title="${escapeHtml(monitorSummary)}" aria-label="设置 ${escapeHtml(stock.code)} 监控项目"><strong>${monitorItems.length}</strong><span>项规则</span><small>${escapeHtml(monitorItems.slice(0, 2).map((item) => item.label).join(" / "))}${monitorItems.length > 2 ? "…" : ""}</small></button></td>
        <td><div class="signal-stack">${boardSignal}${lineSignals || '<span class="signal">波动监控</span>'}</div></td>
        <td><div class="source-mini">${escapeHtml((quote?.sources || []).map((name) => sourceNames[name] || name).join(" / ") || "—")}</div></td>
        <td><div class="row-actions"><button class="analysis-button" type="button" aria-label="分析 ${escapeHtml(stock.code)}">分析</button><button class="remove-button" type="button" aria-label="删除 ${escapeHtml(stock.code)}" title="删除">×</button></div></td>
      </tr>`;
  }).join("");
  const labels = ["监控", "电脑悬浮窗", "股票", "现价", "涨跌幅", "分时均价", "成本线", "五日均线", "监控项目", "关键信号", "行情来源", "操作"];
  body.querySelectorAll("tr[data-index]").forEach((row) => {
    [...row.cells].forEach((cell, index) => { cell.dataset.label = labels[index]; });
  });
}

function renderSources() {
  const configured = state.config?.providers || ["tencent", "eastmoney", "sina"];
  const results = new Map((state.status?.snapshot?.sources || []).map((source) => [source.name, source]));
  $("#sourceList").innerHTML = configured.map((name) => {
    const item = results.get(name);
    const css = !item ? "pending" : item.ok ? "ok" : "failed";
    const detail = !item ? "等待请求" : item.ok ? `${item.latency_ms}ms · ${item.quote_count} 只` : (item.error || "请求失败");
    return `<div class="source-chip ${css}" title="${escapeHtml(detail)}"><span class="source-light"></span><b>${escapeHtml(sourceNames[name] || name)}</b><small>${escapeHtml(detail)}</small></div>`;
  }).join("");
}

function renderAlerts() {
  const feed = $("#alertFeed");
  $("#alertCount").textContent = String(state.alerts.length);
  if (!state.alerts.length) {
    feed.innerHTML = '<div class="empty-alert"><span class="radar-icon"></span><strong>尚无提醒</strong><p>炸板、开板和关键线穿越会出现在这里。</p></div>';
    return;
  }
  feed.innerHTML = state.alerts.map((alert) => `
    <article class="alert-item ${escapeHtml(alert.severity)}">
      <div>
        <div class="alert-meta"><span>${escapeHtml(alert.event_label)} · ${escapeHtml(alert.code)}</span><time>${formatTime(alert.occurred_at, true)}</time></div>
        <strong>${escapeHtml(alert.name || alert.code)}　${formatPrice(alert.price)}</strong>
        <p>${escapeHtml(alert.message)}</p>
      </div>
    </article>`).join("");
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  const active = ["starting", "running", "waiting", "refreshing", "stopping"].includes(status.state);
  const monitorActive = ["starting", "running", "waiting", "stopping"].includes(status.state);
  const dot = $("#runtimeDot");
  dot.className = `status-dot ${status.state === "error" ? "error" : active ? (status.state === "waiting" ? "warning" : "running") : ""}`;
  $("#runtimeText").textContent = runtimeLabels[status.state] || status.message;
  $("#marketStatus").textContent = marketPhaseLabels[status.market_phase] || (status.market_open ? "A 股交易时段" : "当前休市");
  $("#stockCount").textContent = String(status.stock_count ?? state.config?.stocks?.length ?? 0);
  $("#lastUpdate").textContent = formatTime(status.snapshot?.updated_at);
  $("#snapshotMessage").textContent = status.error || status.snapshot?.message || status.message;
  const sources = status.snapshot?.sources || [];
  $("#sourceCount").textContent = sources.length ? `${sources.filter((item) => item.ok).length}/${sources.length}` : "—";
  const latencies = sources.filter((item) => item.ok).map((item) => item.latency_ms);
  $("#fastestLatency").textContent = latencies.length ? String(Math.min(...latencies)) : "—";
  const button = $("#monitorButton");
  button.textContent = monitorActive ? "停止监控" : "启动监控";
  button.className = `button button-primary${monitorActive ? " is-stop" : ""}`;
  button.disabled = status.state === "stopping" || status.state === "refreshing" || state.busy;
  $("#refreshButton").disabled = active || state.busy;
  $("#saveButton").disabled = monitorActive || state.busy;
  $("#addStockButton").disabled = monitorActive || state.busy;
  renderSources();
  if (!$("#stockTableBody").contains(document.activeElement)) renderStocks();
}

function syncStockEdits() {
  document.querySelectorAll("#stockTableBody tr[data-index]").forEach((row) => {
    const stock = state.config.stocks[Number(row.dataset.index)];
    if (!stock) return;
    stock.enabled = row.querySelector(".stock-enabled").checked;
    stock.widget_enabled = row.querySelector(".stock-widget-enabled").checked;
    stock.cost = numberOrNull(row.querySelector(".stock-cost").value);
    stock.ma5 = numberOrNull(row.querySelector(".stock-ma5").value);
    stock.auto_ma5 = row.querySelector(".stock-auto-ma5").checked;
  });
}

async function saveConfig(successMessage = "配置已保存") {
  if (!state.config) return false;
  syncStockEdits();
  state.busy = true;
  renderStatus();
  try {
    state.config = await api("/api/config", { method: "PUT", body: JSON.stringify(state.config) });
    markSaved();
    toast(successMessage);
    renderStocks();
    return true;
  } catch (error) {
    if (error.status === 409 && error.message.includes("其他设备")) {
      state.conflict = true;
      renderSyncState();
    }
    toast(error.message, true);
    return false;
  } finally {
    state.busy = false;
    renderStatus();
  }
}

async function loadStatus() {
  if (document.hidden) return;
  try {
    state.status = await api("/api/status");
    state.connected = true;
    renderStatus();
  } catch (error) {
    state.connected = false;
    $("#runtimeText").textContent = "后台连接失败 · 请检查电脑和 Tailscale";
    $("#runtimeDot").className = "status-dot error";
  }
  renderSyncState();
}

async function loadAlerts() {
  if (document.hidden) return;
  try {
    const payload = await api("/api/alerts?limit=100");
    state.alerts = payload.alerts || [];
    renderAlerts();
  } catch (_) { /* keep the last visible feed */ }
}

function openSettings() {
  const config = state.config;
  $("#pollInterval").value = config.poll_interval_seconds ?? 2;
  $("#requestTimeout").value = config.request_timeout_seconds ?? 2.5;
  $("#lineConfirmations").value = config.line_confirmations ?? 2;
  $("#lineHoldPolls").value = config.line_hold_polls ?? 2;
  $("#rapidMoveWindow").value = config.rapid_move_window_seconds ?? 20;
  $("#rapidMoveThreshold").value = config.rapid_move_threshold_pct ?? 3;
  $("#beepEnabled").checked = config.notifications?.beep !== false;
  $("#notifyRecovery").checked = config.notify_recovery !== false;
  document.querySelectorAll('input[name="provider"]').forEach((input) => {
    input.checked = (config.providers || []).includes(input.value);
  });
  $("#settingsDialog").showModal();
}

function applySettings() {
  const providers = [...document.querySelectorAll('input[name="provider"]:checked')].map((item) => item.value);
  if (!providers.length) {
    toast("至少保留一个数据源", true);
    return false;
  }
  state.config.poll_interval_seconds = Number($("#pollInterval").value);
  state.config.request_timeout_seconds = Number($("#requestTimeout").value);
  state.config.line_confirmations = Number($("#lineConfirmations").value);
  state.config.line_hold_polls = Number($("#lineHoldPolls").value);
  state.config.rapid_move_window_seconds = Number($("#rapidMoveWindow").value);
  state.config.rapid_move_threshold_pct = Number($("#rapidMoveThreshold").value);
  state.config.providers = providers;
  state.config.notify_recovery = $("#notifyRecovery").checked;
  state.config.notifications ||= { beep: true, webhooks: [] };
  state.config.notifications.beep = $("#beepEnabled").checked;
  return true;
}

function addStock() {
  const rawCode = $("#newCode").value.trim().toUpperCase();
  const digits = rawCode.replace(/^SH|^SZ|^BJ/, "").split(".")[0].replace(/\D/g, "");
  if (digits.length !== 6) {
    toast("请输入六位 A 股代码", true);
    return false;
  }
  if (state.config.stocks.some((stock) => stock.code === digits)) {
    toast("该股票已在自选列表中", true);
    return false;
  }
  const monitorItems = [...document.querySelectorAll('input[name="newMonitorItem"]:checked')]
    .map((input) => input.value);
  if (!monitorItems.length) {
    toast("至少选择一个监控项目", true);
    return false;
  }
  state.config.stocks.push({
    code: digits,
    name: $("#newName").value.trim(),
    cost: numberOrNull($("#newCost").value),
    ma5: numberOrNull($("#newMa5").value),
    auto_ma5: $("#newAutoMa5").checked,
    widget_enabled: $("#newWidgetEnabled").checked,
    limit_pct: numberOrNull($("#newLimitPct").value),
    monitor_items: allMonitorItemIds.filter((item) => monitorItems.includes(item)),
    enabled: true,
  });
  markDirty();
  renderStocks();
  return true;
}

function drawAnalysisChart(points) {
  const canvas = $("#analysisChart");
  if (!canvas || !points?.length) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, rect.width);
  const height = Math.max(220, rect.height);
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const padding = { top: 18, right: 16, bottom: 25, left: 48 };
  const values = points.flatMap((item) => [item.close, item.ma5, item.ma20, item.ma60]).filter((value) => Number.isFinite(Number(value))).map(Number);
  if (!values.length) return;
  let low = Math.min(...values);
  let high = Math.max(...values);
  const margin = Math.max((high - low) * 0.08, high * 0.006);
  low -= margin;
  high += margin;
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const x = (index) => padding.left + (points.length === 1 ? 0 : index / (points.length - 1) * plotWidth);
  const y = (value) => padding.top + (high - value) / (high - low || 1) * plotHeight;

  context.font = "10px Consolas, monospace";
  context.textAlign = "right";
  context.fillStyle = "#6f8980";
  context.strokeStyle = "rgba(191,230,216,.09)";
  context.lineWidth = 1;
  for (let index = 0; index <= 4; index += 1) {
    const value = high - (high - low) * index / 4;
    const position = padding.top + plotHeight * index / 4;
    context.beginPath();
    context.moveTo(padding.left, position);
    context.lineTo(width - padding.right, position);
    context.stroke();
    context.fillText(value.toFixed(2), padding.left - 7, position + 3);
  }

  const series = [
    ["close", "#edf8f3", 2.1], ["ma5", "#f4c86a", 1.25],
    ["ma20", "#72dfe8", 1.25], ["ma60", "#81f0b2", 1.25],
  ];
  series.forEach(([field, color, lineWidth]) => {
    context.beginPath();
    let started = false;
    points.forEach((item, index) => {
      const value = Number(item[field]);
      if (!Number.isFinite(value)) { started = false; return; }
      if (!started) { context.moveTo(x(index), y(value)); started = true; }
      else context.lineTo(x(index), y(value));
    });
    context.strokeStyle = color;
    context.lineWidth = lineWidth;
    context.stroke();
  });
  context.textAlign = "left";
  context.fillStyle = "#6f8980";
  context.fillText(points[0].date.slice(5), padding.left, height - 7);
  context.textAlign = "right";
  context.fillText(points.at(-1).date.slice(5), width - padding.right, height - 7);
}

function renderAnalysis(analysis) {
  const indicators = analysis.indicators || {};
  const macd = indicators.macd || {};
  const boll = indicators.boll || {};
  const scoreClass = analysis.technical_score >= 35 ? "positive" : analysis.technical_score <= -35 ? "negative" : "neutral";
  const sourceStatus = (analysis.source_status || []).map((source) => `
    <div class="analysis-source ${source.ok ? "ok" : "failed"}" title="${escapeHtml(source.error || "")}">
      <i></i><span>${escapeHtml(sourceNames[source.name] || source.name)}</span>
      <small>${source.ok ? `${source.bars} 根 · ${source.adjusted ? "前复权" : "校验"}` : "不可用"}</small>
    </div>`).join("");
  const signals = (analysis.signals || []).map((item) => `
    <article class="analysis-signal ${escapeHtml(item.direction)}">
      <span>${escapeHtml(item.method)}</span><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.detail)}</p>
    </article>`).join("");
  const levels = (analysis.levels || []).map((item) => `
    <div class="analysis-level"><span>${escapeHtml(item.name)}</span><strong>${formatPrice(item.value)}</strong><small class="${item.distance_pct >= 0 ? "positive" : "negative"}">${escapeHtml(item.relation)} · ${item.distance_pct >= 0 ? "+" : ""}${formatNumber(item.distance_pct, 2, "%")}</small></div>`).join("");
  const risks = (analysis.risks || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("#analysisTitle").textContent = `${analysis.name || analysis.code} · 技术分析`;
  $("#analysisBody").innerHTML = `
    <section class="analysis-summary">
      <div class="analysis-score ${scoreClass}"><small>技术强弱</small><strong>${analysis.technical_score >= 0 ? "+" : ""}${analysis.technical_score}</strong><span>${escapeHtml(analysis.label)}</span></div>
      <div><p class="analysis-price">${formatPrice(analysis.price)} <span>${Number.isFinite(Number(analysis.change_pct)) ? `${analysis.change_pct >= 0 ? "+" : ""}${formatNumber(analysis.change_pct, 2, "%")}` : ""}</span></p><h3>${escapeHtml(analysis.summary)}</h3><small>${escapeHtml(analysis.data_date)} · ${analysis.bars_count} 根日线 · ${analysis.adjusted ? "前复权" : "未复权降级"}</small></div>
    </section>
    <section class="analysis-metrics">
      <div><span>MA5 / MA20</span><strong>${formatPrice(indicators.ma5)} / ${formatPrice(indicators.ma20)}</strong><small>MA20 五日斜率 ${formatNumber(indicators.ma20_slope_5d_pct, 2, "%")}</small></div>
      <div><span>MA60</span><strong>${formatPrice(indicators.ma60)}</strong><small>中期趋势过滤</small></div>
      <div><span>MACD</span><strong>${formatNumber(macd.dif, 4)} / ${formatNumber(macd.dea, 4)}</strong><small>柱体 ${formatNumber(macd.histogram, 4)}</small></div>
      <div><span>RSI(14)</span><strong>${formatNumber(indicators.rsi14, 1)}</strong><small>30 / 50 / 70 分区</small></div>
      <div><span>ATR(14)</span><strong>${formatPrice(indicators.atr14)}</strong><small>占现价 ${formatNumber(indicators.natr14_pct, 2, "%")}</small></div>
      <div><span>布林通道</span><strong>${formatPrice(boll.lower)} – ${formatPrice(boll.upper)}</strong><small>中轨 ${formatPrice(boll.middle)}</small></div>
      <div><span>20 日涨跌</span><strong>${formatNumber(indicators.return20_pct, 2, "%")}</strong><small>日收益波动 ${formatNumber(indicators.volatility20_pct, 2, "%")}</small></div>
      <div><span>20 日通道</span><strong>${formatPrice(indicators.donchian20?.low)} – ${formatPrice(indicators.donchian20?.high)}</strong><small>突破 / 跌破参考</small></div>
    </section>
    <section class="analysis-chart-panel">
      <div class="analysis-section-title"><div><span>PRICE STRUCTURE</span><h3>价格与均线</h3></div><div class="chart-legend"><i class="close-line"></i>收盘 <i class="ma5-line"></i>MA5 <i class="ma20-line"></i>MA20 <i class="ma60-line"></i>MA60</div></div>
      <canvas id="analysisChart" aria-label="最近 80 个交易日价格与均线图"></canvas>
    </section>
    <div class="analysis-columns">
      <section><div class="analysis-section-title"><div><span>SIGNAL EVIDENCE</span><h3>信号证据</h3></div></div><div class="analysis-signal-list">${signals || '<p class="analysis-empty">暂无信号</p>'}</div></section>
      <section><div class="analysis-section-title"><div><span>KEY LEVELS</span><h3>关键价位</h3></div></div><div class="analysis-level-list">${levels || '<p class="analysis-empty">暂无价位</p>'}</div></section>
    </div>
    <section class="analysis-risk"><div class="analysis-section-title"><div><span>RISK CHECK</span><h3>风险与限制</h3></div></div><ul>${risks}</ul></section>
    <section class="analysis-provenance"><div>${sourceStatus}</div><p>${escapeHtml(analysis.disclaimer)}</p></section>`;
  requestAnimationFrame(() => drawAnalysisChart(analysis.chart || []));
}

async function openAnalysis(code, name) {
  $("#analysisTitle").textContent = `${name || code} · 技术分析`;
  $("#analysisBody").innerHTML = '<div class="analysis-loading"><span></span><p>正在并发获取复权日线并计算指标…</p></div>';
  const dialog = $("#analysisDialog");
  if (!dialog.open) dialog.showModal();
  try {
    renderAnalysis(await api(`/api/analysis?code=${encodeURIComponent(code)}`));
  } catch (error) {
    $("#analysisBody").innerHTML = `<div class="analysis-error"><strong>分析暂不可用</strong><p>${escapeHtml(error.message)}</p><small>可稍后重试；现有盘中监控不受影响。</small></div>`;
  }
}

function bindEvents() {
  $("#remoteButton").addEventListener("click", openRemoteSettings);
  $("#reloadConfigButton").addEventListener("click", reloadSharedConfig);
  $("#copyRemoteButton").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#remoteUrl").value); toast("私有地址已复制"); }
    catch (_) { $("#remoteUrl").select(); toast("请长按或按 Ctrl+C 复制地址"); }
  });
  $("#disableRemoteButton").addEventListener("click", async () => {
    if (!window.confirm("关闭后手机会立即断开，本机不受影响。是否关闭？")) return;
    try { await api("/api/remote/disable", { method: "POST" }); $("#remoteDialog").close(); toast("已关闭软件远程授权"); }
    catch (error) { toast(error.message, true); }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadStatus(); loadAlerts(); syncSharedData(); }
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  document.querySelectorAll(".module-tab").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  $("#radarRefreshButton").addEventListener("click", () => loadRadar(true));
  $("#radarSettingsButton").addEventListener("click", openRadarSettings);
  $("#newsAgentSettingsButton").addEventListener("click", openNewsAgentSettings);
  $("#newsAgentRunButton").addEventListener("click", runNewsAgent);
  $("#radarSearch").addEventListener("input", renderRadar);
  $("#radarRelatedOnly").addEventListener("change", renderRadar);
  $("#radarSourceFilters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-source]");
    if (!button) return;
    state.radarSource = button.dataset.source;
    renderRadar();
  });
  $("#saveRadarSettingsButton").addEventListener("click", async (event) => {
    event.preventDefault();
    await saveRadarSettings();
  });
  $("#saveNewsAgentSettingsButton").addEventListener("click", async (event) => {
    event.preventDefault();
    if (!$("#newsAgentSettingsForm").reportValidity()) return;
    await saveNewsAgentSettings();
  });
  $("#stockTableBody").addEventListener("input", (event) => {
    if (event.target.matches(".stock-enabled, .stock-widget-enabled, .stock-cost, .stock-ma5, .stock-auto-ma5")) {
      syncStockEdits();
      markDirty();
    }
  });
  $("#stockTableBody").addEventListener("click", async (event) => {
    const monitorButton = event.target.closest(".monitor-items-button");
    if (monitorButton) {
      const row = monitorButton.closest("tr");
      openStockMonitor(Number(row.dataset.index));
      return;
    }
    const analysisButton = event.target.closest(".analysis-button");
    if (analysisButton) {
      const row = analysisButton.closest("tr");
      const stock = state.config.stocks[Number(row.dataset.index)];
      const code = stock.code;
      const name = stock.name;
      if (state.dirty && !(await saveConfig("配置已保存，开始分析"))) return;
      openAnalysis(code, name);
      return;
    }
    const button = event.target.closest(".remove-button");
    if (!button) return;
    const row = button.closest("tr");
    syncStockEdits();
    state.config.stocks.splice(Number(row.dataset.index), 1);
    markDirty();
    renderStocks();
  });
  $("#saveButton").addEventListener("click", () => saveConfig());
  $("#addStockButton").addEventListener("click", () => {
    $("#stockForm").reset();
    $("#newMonitorOptions").innerHTML = monitorOptionsHtml(allMonitorItemIds, "newMonitorItem");
    $("#stockDialog").showModal();
    $("#newCode").focus();
  });
  $("#confirmAddButton").addEventListener("click", (event) => {
    event.preventDefault();
    if (addStock()) $("#stockDialog").close();
  });
  $("#saveStockMonitorButton").addEventListener("click", (event) => {
    event.preventDefault();
    saveStockMonitor();
  });
  $("#settingsButton").addEventListener("click", openSettings);
  $("#closeAnalysisButton").addEventListener("click", () => $("#analysisDialog").close());
  $("#applySettingsButton").addEventListener("click", async (event) => {
    event.preventDefault();
    if (!applySettings()) return;
    $("#settingsDialog").close();
    markDirty();
    await saveConfig("监控设置已保存");
  });
  $("#monitorButton").addEventListener("click", async () => {
    const active = ["starting", "running", "waiting", "stopping"].includes(state.status?.state);
    if (!active && state.dirty && !(await saveConfig())) return;
    state.busy = true;
    renderStatus();
    try {
      state.status = await api(active ? "/api/monitor/stop" : "/api/monitor/start", { method: "POST" });
      toast(active ? "正在停止监控" : "监控已启动");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
      await loadStatus();
    }
  });
  $("#refreshButton").addEventListener("click", async () => {
    if (state.dirty && !(await saveConfig())) return;
    state.busy = true;
    renderStatus();
    try {
      state.status = await api("/api/monitor/refresh", { method: "POST" });
      toast("正在并发请求三路行情");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
      await loadStatus();
    }
  });
}

async function init() {
  bindEvents();
  try {
    const [config, status, alerts] = await Promise.all([
      api("/api/config"), api("/api/status"), api("/api/alerts?limit=100"),
    ]);
    state.config = config;
    state.status = status;
    state.connected = true;
    state.alerts = alerts.alerts || [];
    renderStocks();
    renderStatus();
    renderAlerts();
    try {
      await loadAccess();
      await loadNewsAgentSettings();
      await syncSharedData();
    } catch (agentError) {
      renderNewsAgentState();
      console.warn("Agent settings unavailable", agentError);
    }
  } catch (error) {
    toast(`初始化失败：${error.message}`, true);
  }
  setInterval(loadStatus, 2000);
  setInterval(loadAlerts, 5000);
  setInterval(syncSharedData, 3000);
  setInterval(() => {
    if (document.hidden || state.activeView !== "news" || !state.news?.fetched_at || state.radarBusy) return;
    const interval = Number(state.newsSettings?.refresh_interval_seconds || 60) * 1000;
    if (Date.now() - new Date(state.news.fetched_at).getTime() >= interval) loadRadar(true);
  }, 10000);
  setInterval(() => {
    const now = state.status?.server_time ? new Date(state.status.server_time) : new Date();
    $("#clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  }, 1000);
}

document.addEventListener("DOMContentLoaded", init);
