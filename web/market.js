/* Independent market view: cached GETs never trigger expensive collection. */
(() => {
  const el = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const num = (v, digits = 1, suffix = "") => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(digits) + suffix;
  const signed = (v, digits = 1, suffix = "") => v == null ? "—" : (v > 0 ? "+" : "") + num(v, digits, suffix);
  const money = (v) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v) >= 1e8 ? num(Number(v) / 1e8, 2, " 亿") : Number(v) >= 1e4 ? num(Number(v) / 1e4, 0, " 万") : num(Number(v), 0, " 元");
  const tone = (v) => v == null || v === 0 ? "" : v > 0 ? "market-up" : "market-down";
  const timeText = (v) => v ? new Date(v).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false}) : "未知";
  let current = null, loading = false, sending = false, dirty = false, settings = null;
  let epoch = 0, editVersion = 0;
  let seen = new Set(), initialized = false;

  async function request(path, options = {}) {
    const response = await fetch("/api/market-monitor" + path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `请求失败 ${response.status}`);
    return result;
  }

  function fillSettings(s) {
    settings = s;
    el("marketInterval").value = s.interval_seconds;
    el("marketLookback").value = s.lookback_minutes;
    el("marketConfirm").value = s.confirmations;
    el("marketRankJump").value = s.rank_jump;
    el("marketAlerts").checked = s.alerts_enabled;
  }

  function renderTable() {
    const query = el("marketSearch").value.trim();
    const mode = el("marketSort").value;
    const minutes = String(current?.settings?.lookback_minutes || 5);
    const rows = (current?.snapshot?.sectors || []).filter((r) => r.name.includes(query)).slice();
    rows.sort((a, b) => {
      if (mode === "amount") return (b.amount_share ?? -1) - (a.amount_share ?? -1);
      if (["rise", "fall"].includes(mode)) {
        const av = a.rank_changes?.[minutes], bv = b.rank_changes?.[minutes];
        if (av == null || bv == null) return av == null ? (bv == null ? a.rank - b.rank : 1) : -1;
        return mode === "rise" ? bv - av : av - bv;
      }
      return a.rank - b.rank;
    });
    el("marketSectorRows").innerHTML = rows.map((r) => `<tr><td><small>${r.rank}</small> ${esc(r.name)}</td>
      <td class="${tone(r.change_pct)}">${signed(r.change_pct, 2, "%")}</td><td class="${tone(r.excess_pct)}">${signed(r.excess_pct, 2, "pp")}</td>
      <td>${num(r.up_ratio, 1, "%")}</td><td class="market-rank-deltas">${[5,15,30].map((m) => `<span class="${tone(r.rank_changes?.[String(m)])}">${signed(r.rank_changes?.[String(m)], 0)}</span>`).join(" / ")}</td>
      <td>${num(r.amount_share, 2, "%")}</td><td>${esc(r.label)}</td><td>${esc(r.leader || "—")}</td></tr>`).join("") || '<tr><td colspan="8">没有匹配的行业数据</td></tr>';
  }

  function renderChart(history) {
    const points = history.filter((p) => p.score != null && Number.isFinite(Date.parse(p.at)));
    if (points.length < 2) {
      el("marketChart").textContent = "还没有足够的日内轨迹；盘后快照不补造历史。";
      return;
    }
    const begin = Date.parse(points[0].at), end = Date.parse(points.at(-1).at);
    const coordinate = (p) => `${40 + 880 * (Date.parse(p.at) - begin) / Math.max(end - begin, 1)},${170 - 1.4 * Math.max(0, Math.min(100, p.score))}`;
    const segments = [[]];
    points.forEach((p, i) => {
      if (i && Date.parse(p.at) - Date.parse(points[i-1].at) > 360000) segments.push([]);
      segments.at(-1).push(coordinate(p));
    });
    el("marketChart").innerHTML = `<svg viewBox="0 0 960 200" role="img" aria-label="日内情绪温度轨迹，0 到 100 分">
      ${[25,50,75].map((n) => `<line x1="40" x2="920" y1="${170-n*1.4}" y2="${170-n*1.4}" class="market-gridline"/><text x="4" y="${175-n*1.4}">${n}</text>`).join("")}
      ${segments.map((s) => `<polyline points="${s.join(" ")}" class="market-score-line"/>`).join("")}
      <text x="40" y="195">${esc(timeText(points[0].at).split(" ").at(-1))}</text><text x="920" y="195" text-anchor="end">${esc(timeText(points.at(-1).at).split(" ").at(-1))}</text></svg>`;
  }

  function ladderStock(stock, detail = "") {
    if (!stock) return '<span class="market-ladder-empty">待核验</span>';
    const position = stock.position_label || stock.price_position_level;
    const suffix = [stock.streak ? `${stock.streak}板` : "", position || "", stock.change_pct == null ? "" : signed(stock.change_pct, 2, "%"), detail].filter(Boolean).join(" · ");
    return `<span class="market-ladder-stock"><b>${esc(stock.name || stock.code || "未命名")}</b>${stock.code ? `<small>${esc(stock.code)}</small>` : ""}${suffix ? `<em>${esc(suffix)}</em>` : ""}</span>`;
  }

  function positionCandidate(stock) {
    const metrics = [
      stock.pre_return_5d == null ? "" : `封板前5日 ${signed(stock.pre_return_5d, 1, "%")}`,
      stock.pre_excess_5d == null ? "" : `封板前5日超额 ${signed(stock.pre_excess_5d, 1, "pp")}`,
      stock.distance_20d_high_pct == null ? "" : `距20日高 ${signed(stock.distance_20d_high_pct, 1, "%")}`,
      stock.prior_limit_count_10d == null ? "" : `近10日涨停≈${stock.prior_limit_count_10d}次`,
      stock.position_label === "低位补涨" && stock.catch_up_confidence ? `补涨条件分 ${stock.catch_up_confidence} ${stock.catch_up_score}` : "",
    ].filter(Boolean).join(" · ");
    return `<article class="market-position-stock"><div><b>${esc(stock.name || stock.code)}</b><small>${esc(stock.code || "")}</small></div><p>${esc(metrics || "历史证据不足")}</p><em>${esc(stock.position_reason || stock.price_position_reason || "等待位置判定")}</em></article>`;
  }

  function positionGroup(title, rows, css = "") {
    return `<section class="market-position-group ${css}"><strong>${esc(title)}</strong>${rows.length ? rows.map(positionCandidate).join("") : '<p class="market-position-empty">暂无符合完整条件的标的</p>'}</section>`;
  }

  function leaderComponents(components) {
    return Object.entries(components || {}).map(([name, value]) => `<span><i>${esc(name)}</i><b>${num(value, 1)}</b></span>`).join("") || '<span><i>评分证据</i><b>待积累</b></span>';
  }

  function leaderCard(stock, mode = "market") {
    if (!stock) return '<article class="market-leader-card empty">暂无合格候选</article>';
    const isMarket = mode === "market", score = isMarket ? stock.market_leader_score : stock.sector_leader_score;
    const role = isMarket ? stock.market_leader_role : stock.sector_leader_role;
    const components = isMarket ? stock.market_leader_components : stock.sector_leader_components;
    const evidence = [
      stock.streak ? `${stock.streak}板` : "首板",
      stock.first_limit_time ? `首封 ${stock.first_limit_time}` : "",
      stock.followers_after_limit == null ? "" : `10分钟后续封板 ${stock.followers_after_limit}只`,
      stock.seal_float_ratio_pct == null ? "" : `封单/流通值 ${num(stock.seal_float_ratio_pct, 3, "%")}`,
      stock.limit_utilization_pct == null ? "" : `涨停利用率 ${num(stock.limit_utilization_pct, 1, "%")}`,
      stock.attention_best_rank == null ? "" : `热榜最高 #${stock.attention_best_rank}`,
      `事件响应 ${stock.influence_observations || 0}次`,
    ].filter(Boolean).join(" · ");
    return `<article class="market-leader-card ${stock.dual_leader ? "dual" : ""}"><header><div><span>${esc(role || "候选")}</span><h5>${esc(stock.name || stock.code)}</h5><small>${esc(stock.code || "")}</small></div><strong>${num(score, 1)}</strong></header><p>${esc(evidence)}</p><div class="market-leader-components">${leaderComponents(components)}</div>${(stock.attention_sources || []).length ? `<em>热度来源：${esc(stock.attention_sources.join("、"))}</em>` : ""}</article>`;
  }

  function renderMarketLeaders(snapshot) {
    const leaders = snapshot?.market_speculation_leaders || [], method = snapshot?.leader_analysis || {};
    el("marketLeaderSummary").innerHTML = `<div class="market-heading"><div><p class="eyebrow">MARKET SPECULATION LEADER</p><h3>市场投机龙识别</h3></div><span>市场空间 × 全市场带动 × 热度 × 分歧生存 × 流动性</span></div><div class="market-leader-grid">${leaders.length ? leaders.map((stock) => leaderCard(stock, "market")).join("") : '<p class="market-muted">当前涨停池没有可评分候选。</p>'}</div><p class="market-footnote">${esc(method.method || "需要连续快照积累封板/炸板后的市场响应")}${method.hotlist_stock_count ? ` · 已接入 ${esc(method.hotlist_stock_count)} 只缓存热榜股票` : " · 尚无缓存热榜，热度项暂不加分"}</p>`;
  }

  function renderSectorLadders(snapshot) {
    const ladders = snapshot?.sector_ladders || [];
    el("marketSectorLadders").innerHTML = ladders.map((sector) => {
      const directions = (sector.main_directions || []).map((item) => `<span>${esc(item.name)}${item.limit_up_count ? ` · ${item.limit_up_count}板` : " · 趋势候选"}</span>`).join("") || "<span>待成分与涨停数据</span>";
      const height = (sector.ladder || []).map((level) => `<div><b>${esc(level.label)}</b><p>${(level.stocks || []).map((stock) => `${esc(stock.name || stock.code)}${stock.first_limit_time ? ` ${esc(stock.first_limit_time)}` : ""}`).join("、") || "—"}</p></div>`).join("") || '<div><b>涨停梯队</b><p>当前无匹配封板股</p></div>';
      const promoted = (sector.promoted_stocks || []).map((stock) => `${esc(stock.name || stock.code)}${stock.streak ? ` ${esc(stock.streak)}板` : ""}`).join("、") || "—";
      const broken = (sector.broken_focus || []).map((stock) => esc(stock.name || stock.code)).join("、") || "—";
      const positionGroups = [
        positionGroup("低位补涨", sector.low_catch_up_candidates || [], "catch-up"),
        positionGroup("高位反包", sector.high_rebound_candidates || [], "rebound"),
        positionGroup("趋势加速", sector.trend_acceleration_candidates || [], "acceleration"),
        positionGroup("中位跟随 / 待核验", sector.follow_candidates || [], "follow"),
      ].join("");
      const leaderEvidence = (sector.sector_leader_candidates || []).map((stock) => leaderCard(stock, "sector")).join("") || '<p class="market-muted">当前板块未匹配到封板候选。</p>';
      return `<article class="market-sector-ladder ${sector.data_complete ? "" : "incomplete"}">
        <div class="market-sector-ladder-head"><div><span>强度 #${esc(sector.rank)}</span><h4>${esc(sector.name)}</h4></div><strong class="${tone(sector.change_pct)}">${signed(sector.change_pct, 2, "%")}</strong></div>
        <div class="market-sector-ladder-stats"><div><span>涨停 / 炸板</span><b>${esc(sector.limit_up_count)} / ${esc(sector.broken_count)}</b></div><div><span>晋级股 / 最高板</span><b>${esc(sector.promotion_count)} / ${sector.max_streak ? `${esc(sector.max_streak)}板` : "—"}</b></div><div><span>上涨占比</span><b>${num(sector.up_ratio, 1, "%")}</b></div><div><span>成交额占比</span><b>${num(sector.amount_share, 2, "%")}</b></div><div><span>身位日线 / 双源</span><b>${esc(sector.position_history_covered || 0)} / ${esc(sector.position_history_verified || 0)}</b></div></div>
        <div class="market-main-directions"><strong>主攻细分方向</strong><div>${directions}</div></div>
        <div class="market-ladder-roles"><div><strong>板块龙识别</strong>${ladderStock(sector.sector_leader, sector.sector_leader ? `${sector.sector_leader.sector_leader_role} · ${num(sector.sector_leader.sector_leader_score, 1)}分` : "")}</div><div><strong>情绪龙 / 空间板</strong>${ladderStock(sector.emotion_leader, sector.emotion_leader?.first_limit_time ? `首封 ${sector.emotion_leader.first_limit_time}` : "")}</div><div><strong>板块领涨</strong>${sector.sector_feed_leader ? `<span class="market-ladder-stock"><b>${esc(sector.sector_feed_leader)}</b><em>行业行情字段</em></span>` : ladderStock(sector.trend_core)}</div><div><strong>容量核心</strong>${ladderStock(sector.capacity_core, sector.capacity_core?.amount == null ? "成交额待核验" : `成交 ${money(sector.capacity_core.amount)}`)}</div><div><strong>趋势核心</strong>${ladderStock(sector.trend_core, "成分股涨幅排序")}</div><div><strong>最早上板</strong>${ladderStock(sector.earliest_limit, sector.earliest_limit?.first_limit_time || "时间待核验")}</div><div><strong>最大封单</strong>${ladderStock(sector.max_seal, sector.max_seal?.seal_amount == null ? "封单待核验" : `封单 ${money(sector.max_seal.seal_amount)}`)}</div></div>
        <details class="market-sector-leader-evidence"><summary>查看板块龙候选评分证据</summary><div class="market-leader-grid sector">${leaderEvidence}</div></details>
        <div class="market-limit-ladder"><strong>连板梯队</strong>${height}<div><b>晋级/连板股</b><p>${promoted}</p></div><div class="broken"><b>炸板关注</b><p>${broken}</p></div></div>
        <div class="market-position-board"><div class="market-position-title"><strong>首板价格身位分类</strong><span>行业基线：${esc(sector.position_sector_baseline || "待核验")} · 参考封板前5/10日涨幅、行业超额、20日高点距离和近10日涨停记忆</span></div><div class="market-position-grid">${positionGroups}</div></div>
        <p class="market-ladder-analysis">${esc(sector.analysis)}</p>
        ${sector.data_complete ? "" : `<p class="market-ladder-warning">数据边界：${esc((sector.missing_data || []).join("；") || "部分字段待核验")}</p>`}
      </article>`;
    }).join("") || '<p class="market-muted">尚无可用的强度前 5 板块梯队数据。</p>';
  }

  function render(data) {
    current = data;
    if (!dirty) fillSettings(data.settings);
    const conflict = dirty && settings?._revision !== data.settings._revision;
    el("marketReload").hidden = !dirty;
    const s = data.snapshot, m = s?.sentiment;
    el("marketToggle").textContent = data.enabled ? "停止市场监控" : "启动市场监控";
    el("marketToggle").disabled = sending;
    el("marketRefresh").disabled = sending || data.busy;
    const phase = ({continuous:"交易中", closing_auction:"收盘集合竞价", lunch_break:"午休", open_auction:"开盘竞价", auction_gap:"竞价间隙", closed:"非交易时段", custom:"非标准交易时段"})[s?.phase] || "尚无快照";
    el("marketMonitorStatus").textContent = [data.busy ? "正在分页采集…" : data.enabled ? "连续监控已开启" : "连续监控未开启", phase,
      s ? `采集 ${timeText(s.captured_at)}` : "首次采集可能需要几十秒", s?.stale ? "旧快照，仅供参考" : "",
      data.error || "", conflict ? "其他设备更新了设置，本页修改尚未保存" : ""].filter(Boolean).join(" · ");
    el("marketScore").textContent = num(m?.score);
    el("marketCycle").textContent = s?.stale ? "旧快照" : m?.cycle || "等待合格数据";
    el("marketDelta").textContent = m?.delta == null ? "积累基线中 · 不判断方向" : `${data.settings.lookback_minutes} 分钟变化 ${signed(m.delta)} 分`;
    const pool = (kind) => num(s?.pools?.[kind]?.count, 0);
    const cards = [
      ["上涨 / 下跌 / 平盘", m ? `${m.up} / ${m.down} / ${m.flat}` : "—", "剔除无效、过期与停牌空值"],
      ["涨跌幅中位数", signed(m?.median_pct, 2, "%"), `等权平均 ${signed(m?.equal_weight_pct, 2, "%")}`],
      ["涨幅≥3% / 跌幅≥3%", m ? `${m.strong} / ${m.weak}` : "—", `市场广度 ${signed(m?.breadth_pct, 1, "%")}`],
      ["有效样本成交额", m?.amount == null ? "—" : num(m.amount / 1e8, 0, " 亿"), "当日累计；不与昨天全天直接比较"],
      ["涨停 / 跌停 / 炸板池", `${pool("up")} / ${pool("down")} / ${pool("broken")}`, `当前炸板占比 ${num(s?.broken_rate, 1, "%")} · 专题口径`],
      ["信号状态", s?.signal_eligible && data.enabled ? "情绪规则运行中" : "积累 / 等待 / 暂停", s?.rotation_eligible && data.enabled ? "轮动规则运行中" : "轮动需完整同日行业及基线"],
    ];
    el("marketMetrics").innerHTML = cards.map(([title,value,note]) => `<article class="market-card"><span>${esc(title)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></article>`).join("");
    renderChart(data.history || []);
    el("marketHeatmap").innerHTML = (s?.sectors || []).map((r) => `<button type="button" class="market-tile ${tone(r.change_pct)}" data-sector="${esc(r.name)}" title="点击筛选该行业"><span>${esc(r.name)}</span><strong>${signed(r.change_pct, 2, "%")}</strong><small>${esc(r.label)}</small></button>`).join("") || '<div class="market-card">没有行业数据，独立采集失败不影响自选股监控。</div>';
    renderMarketLeaders(s);
    renderSectorLadders(s);
    renderTable();
    const events = data.events || [];
    const newest = events.filter((e) => !seen.has(e.id));
    if (initialized && newest.length && !document.hidden && !el("marketView").hidden) toast(newest[0].message);
    el("marketEvents").innerHTML = events.map((e) => `<article class="market-event"><time>${esc(timeText(e.at))}</time><p>${esc(e.message)}</p></article>`).join("") || '<p class="market-muted">尚无已确认变化。首次快照、盘后快照不触发提醒。</p>';
    seen = new Set(events.map((e) => e.id));
    initialized = true;
    el("marketQuality").innerHTML = s ? [
      ...[["全市场", s.market_quality], ["一级行业", s.sector_quality]].map(([title,q]) => `<div class="market-quality-row"><strong>${title} · ${q.source === "tencent" ? "腾讯备用" : "东方财富"}</strong><p>${q.complete ? "列表完整" : "列表不完整"} ${q.received}/${q.expected} · 有效 ${q.valid} · 剔除 ${q.excluded}</p><p>行情时间（中位）${esc(timeText(q.quote_time))} · 采集时新鲜率 ${num(q.fresh_pct, 1, "%")}</p>${q.error ? `<p class="market-warning">${esc(q.error)}</p>` : ""}</div>`),
      '<p class="market-muted">东财多网关补页属于同一数据商，不能算作独立多源验证。腾讯仅在已有完整市场名单时备用；名单过期时暂停信号。股池只有日期、没有可靠逐笔时间，不用于独立触发提醒。</p>',
      `<p class="market-muted">下轮等待约 ${data.next_interval_seconds} 秒（另加采集耗时）。数据与提醒仅保存于电脑本地，保留 30 天。</p>`,
    ].join("") : "尚未采集";
  }

  async function load() {
    if (loading || sending) return;
    loading = true;
    const started = epoch;
    try { const data = await request(""); if (started === epoch) render(data); }
    catch (error) { el("marketMonitorStatus").textContent = `连接失败：${error.message}；当前显示内容可能已过期`; }
    finally { loading = false; }
  }

  async function action(name) {
    if (sending) return;
    epoch++;
    sending = true;
    try { render(await request(`/${name}`, {method:"POST"})); }
    catch (error) { toast(error.message, true); }
    finally { sending = false; await load(); }
  }

  function bind() {
    el("marketRefresh").addEventListener("click", () => action("refresh"));
    el("marketToggle").addEventListener("click", () => action(current?.enabled ? "stop" : "start"));
    el("marketSearch").addEventListener("input", renderTable);
    el("marketSort").addEventListener("change", renderTable);
    el("marketHeatmap").addEventListener("click", (e) => {
      const tile = e.target.closest("[data-sector]");
      if (tile) { el("marketSearch").value = tile.dataset.sector; renderTable(); }
    });
    el("marketSettingsForm").addEventListener("input", () => {dirty = true; editVersion++; el("marketReload").hidden = false;});
    el("marketReload").addEventListener("click", () => {dirty = false; load();});
    el("marketSettingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!settings || sending) return;
      const savedEdit = editVersion;
      epoch++;
      sending = true;
      const draft = {interval_seconds:Number(el("marketInterval").value), lookback_minutes:Number(el("marketLookback").value),
        confirmations:Number(el("marketConfirm").value), rank_jump:Number(el("marketRankJump").value), alerts_enabled:el("marketAlerts").checked,
        _revision:settings._revision};
      try {
        settings = await request("/settings", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(draft)});
        if (editVersion === savedEdit) dirty = false;
        toast("市场监控设置已保存");
      } catch (error) { toast(error.message, true); }
      finally { sending = false; await load(); }
    });
    setInterval(() => {if (!document.hidden && !el("marketView").hidden) load();}, 5000);
    document.addEventListener("visibilitychange", () => {if (!document.hidden && !el("marketView").hidden) load();});
  }
  window.marketMonitor = {activate:load};
  document.addEventListener("DOMContentLoaded", bind);
})();
