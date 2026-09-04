/* ============================================================
   Doc-Pipeline v3 Dashboard — Vanilla JS
   ============================================================ */

(function () {
  'use strict';

  // ----------------------------------------------------------
  // Configuration
  // ----------------------------------------------------------
  const API_BASE = (location.protocol === 'http:' || location.protocol === 'https:')
    ? location.origin
    : 'http://127.0.0.1:8910';
  const REFRESH_MS = 5000;

  // ----------------------------------------------------------
  // State
  // ----------------------------------------------------------
  let state = {
    health: null,
    tasks: null,
    agents: null,
    error: null,
    lastUpdated: null,
    loaded: false,
  };

  let refreshTimer = null;

  // ----------------------------------------------------------
  // DOM cache
  // ----------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const els = {};
  function cacheDom () {
    els.errorOverlay   = $('#error-overlay');
    els.errorMsg       = $('#error-msg');
    els.retryBtn       = $('#retry-btn');
    els.dashboard      = $('#dashboard');
    els.lastUpdated    = $('#last-updated');
    els.tokenPrompt    = $('#token-prompt');
    els.tokenInput     = $('#token-input');
    els.tokenSave      = $('#token-save');

    // Panel containers
    els.statusPanel    = $('#panel-status .card-body');
    els.tasksPanel     = $('#panel-tasks .card-body');
    els.agentsPanel    = $('#panel-agents .card-body');
    els.metricsPanel   = $('#panel-metrics .card-body');
    els.qualityPanel   = $('#panel-quality .card-body');
    els.costPanel      = $('#panel-cost .card-body');
    els.logsPanel      = $('#panel-logs .card-body');

    // New task form
    els.newTaskForm    = $('#newtask-form');
    els.ntQuery        = $('#nt-query');
    els.ntPipeline     = $('#nt-pipeline');
    els.ntOutput       = $('#nt-output');
    els.ntSubmit       = $('#nt-submit');
    els.ntMsg          = $('#nt-msg');

    // Badge counters
    els.badgeTasks     = $('#badge-tasks');
    els.badgeAgents    = $('#badge-agents');
    els.badgeStatus    = $('#badge-status');
    els.badgeMetrics   = $('#badge-metrics');
    els.badgeQuality   = $('#badge-quality');
    els.badgeCost      = $('#badge-cost');
    els.badgeLogs      = $('#badge-logs');
  }

  // ----------------------------------------------------------
  // Utility
  // ----------------------------------------------------------
  function pluralize (n, s) {
    return n + ' ' + (n === 1 ? s : s + 's');
  }

  /* DOM 构建辅助：所有动态数据一律走 textContent，禁止字符串拼 HTML */
  function elt (tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function iconEl (id, cls) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'ico' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', '#' + id);
    svg.appendChild(use);
    return svg;
  }

  function emptyState (iconId, msg) {
    const box = elt('div', 'empty-state');
    const wrap = elt('span', 'icon');
    wrap.appendChild(iconEl(iconId, 'ico-lg'));
    box.appendChild(wrap);
    box.appendChild(document.createTextNode(msg));
    return box;
  }

  function timeAgo (date) {
    const diff = Date.now() - date.getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 5) return 'just now';
    if (sec < 60) return sec + 's ago';
    const min = Math.floor(sec / 60);
    if (min < 60) return min + 'm ago';
    return date.toLocaleTimeString();
  }

  function fmtUptime (sec) {
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h + 'h ' + m + 's';
  }

  function formatNum (n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
  }

  function statusClass (status) {
    switch ((status || '').toLowerCase()) {
      case 'running': case 'processing': return 'running';
      case 'pending': case 'waiting':    return 'pending';
      case 'done': case 'completed':
      case 'success': case 'ok':         return 'done';
      case 'failed': case 'error':
      case 'dead': case 'unhealthy':     return 'failed';
      default:                           return 'pending';
    }
  }

  /* 状态点：替代 emoji 指示符，颜色由 token 化 CSS 类控制 */
  function statusDot (status) {
    return elt('span', 'dot dot-' + statusClass(status));
  }

  // ----------------------------------------------------------
  // API fetcher
  // ----------------------------------------------------------
  const TOKEN_KEY = 'docpipe_token';

  function getToken () { return sessionStorage.getItem(TOKEN_KEY) || ''; }

  async function fetchJson (url) {
    const token = getToken();
    const headers = token ? { 'Authorization': 'Bearer ' + token } : {};
    const resp = await fetch(url, { signal: AbortSignal.timeout(4000), headers });
    if (resp.status === 401 && !url.includes('/health')) {
      // 凭证缺失/失效 → 请求 token 后重试一次
      requestToken();
      throw new Error('HTTP 401（需要 API Token）');
    }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }

  function requestToken () {
    if (els.tokenPrompt && els.tokenPrompt.style.display !== 'flex') {
      els.tokenPrompt.style.display = 'flex';
      if (els.tokenInput) els.tokenInput.focus();
    }
  }

  async function fetchAll () {
    const results = await Promise.allSettled([
      fetchJson(API_BASE + '/health'),
      fetchJson(API_BASE + '/api/dashboard'),
      fetchJson(API_BASE + '/api/cost'),
      fetchJson(API_BASE + '/api/quality/feedback'),
      fetchJson(API_BASE + '/api/logs?limit=30&since=86400'),
    ]);
    const health = results[0].status === 'fulfilled' ? results[0].value : null;
    const dash = results[1].status === 'fulfilled' ? results[1].value : null;
    return {
      health,
      tasks: dash || null,           // /api/dashboard 含 tasks[].progress/steps 与 agents
      agents: dash || null,
      cost: results[2].status === 'fulfilled' ? results[2].value : null,
      quality: results[3].status === 'fulfilled' ? results[3].value : null,
      logs: results[4].status === 'fulfilled' ? results[4].value : null,
      failedCount: results.filter(r => r.status === 'rejected').length,
    };
  }

  // ----------------------------------------------------------
  // Renderers
  // ----------------------------------------------------------

  /* ---- Pipeline Status (top-left) ---- */
  function renderStatus (health) {
    if (!health || !health.status) {
      els.statusPanel.replaceChildren(emptyState('i-status', 'No status data'));
      return;
    }

    const ok = health.status === 'ok';
    const statusLabel = ok ? 'ok' : 'error';
    const subs = health.subscribers != null ? health.subscribers : 0;
    const metrics = health.metrics || {};

    // Queue depth: /health 的顶层字段（metrics 子对象里没有该字段）
    const queueDepth = health.queue_depth != null ? health.queue_depth : 0;

    // DB store: messages 为条目数，db_size 为字节数
    let dbLabel = '—';
    if (health.store) {
      const msgs = typeof health.store.messages === 'number' ? health.store.messages : null;
      const bytes = typeof health.store.db_size === 'number' ? health.store.db_size : null;
      if (msgs != null && bytes != null) {
        dbLabel = pluralize(msgs, 'msg') + ' · ' + (bytes / (1024 * 1024)).toFixed(1) + 'MB';
      } else if (msgs != null) {
        dbLabel = pluralize(msgs, 'entry');
      }
    }

    function statCell (label, value, cls) {
      const item = elt('div', 'stat-item');
      item.appendChild(elt('div', 'stat-label', label));
      item.appendChild(elt('div', 'stat-value ' + cls, String(value)));
      return item;
    }

    const grid = elt('div', 'status-grid');

    /* status pill spans 2 cols */
    const pillWrap = elt('div');
    pillWrap.style.cssText = 'grid-column:1/-1;display:flex;justify-content:center;margin-bottom:4px;';
    const pill = elt('span', 'status-pill ' + statusLabel);
    pill.appendChild(elt('span', 'dot'));
    pill.appendChild(document.createTextNode(statusLabel));
    pillWrap.appendChild(pill);
    grid.appendChild(pillWrap);

    /* subtitle stats */
    grid.appendChild(statCell('Subscribers', subs, 'ok'));
    grid.appendChild(statCell('Queue Depth', queueDepth, queueDepth > 10 ? 'warn' : 'ok'));
    grid.appendChild(statCell('DB Store', dbLabel, 'ok'));
    grid.appendChild(statCell('Uptime', metrics.uptime ? fmtUptime(metrics.uptime) : '—', 'ok'));

    els.statusPanel.replaceChildren(grid);

    // Update badge
    if (els.badgeStatus) els.badgeStatus.textContent = statusLabel;
  }

      /* ---- Tasks (top-right) ---- */
  function renderTasks (data) {
    const tasks = (data && data.tasks) || [];
    const count = data && data.task_count != null ? data.task_count : tasks.length;

    // Update badge
    if (els.badgeTasks) els.badgeTasks.textContent = count;

    if (!tasks.length) {
      els.tasksPanel.replaceChildren(emptyState('i-tasks', 'No active tasks'));
      return;
    }

    const table = elt('table', 'tasks-table');
    const thead = elt('thead');
    const headRow = elt('tr');
    for (const h of ['ID', 'Pipeline', 'Status', 'Progress']) {
      headRow.appendChild(elt('th', null, h));
    }
    thead.appendChild(headRow);

    const tbody = elt('tbody');
    for (const t of tasks) {
      const sCls = statusClass(t.status);
      const progress = t.progress != null ? Number(t.progress) || 0 : 0;
      const pct = Math.min(100, Math.max(0, Math.round(progress)));

      const tdId = elt('td', 'task-id', t.id == null ? '' : String(t.id));
      tdId.title = tdId.textContent;

      const badge = elt('span', 'badge-status ' + sCls);
      badge.appendChild(statusDot(t.status));
      badge.appendChild(document.createTextNode(' ' + (t.status || 'unknown')));
      const tdStatus = elt('td');
      tdStatus.appendChild(badge);

      const wrap = elt('div');
      wrap.style.cssText = 'display:flex;align-items:center;gap:6px;';
      const pwrap = elt('div', 'progress-wrap');
      pwrap.style.flex = '1';
      const fill = elt('div', 'progress-fill ' + sCls);
      fill.style.width = pct + '%';
      pwrap.appendChild(fill);
      wrap.appendChild(pwrap);
      wrap.appendChild(elt('span', 'progress-text', pct + '%'));
      const tdProgress = elt('td');
      tdProgress.appendChild(wrap);

      const tr = elt('tr');
      tr.dataset.taskId = t.id == null ? '' : String(t.id);
      tr.appendChild(tdId);
      tr.appendChild(elt('td', 'task-pipeline', t.pipeline || '—'));
      tr.appendChild(tdStatus);
      tr.appendChild(tdProgress);
      if ((t.status || '').toLowerCase() === 'running') {
        tr.classList.add('expandable');
        tr.title = '点击展开实时进度';
        tr.addEventListener('click', () => toggleLiveStream(String(t.id)));
      }
      tbody.appendChild(tr);
      if (liveStreams.has(String(t.id))) {
        tbody.appendChild(liveStreams.get(String(t.id)).row);
      }
    }

    table.appendChild(thead);
    table.appendChild(tbody);
    els.tasksPanel.replaceChildren(table);
  }

  /* ---- Agents (bottom-left) ---- */
  function renderAgents (data) {
    // agents can be an object { name: {...} } or an array
    let list = [];
    if (data && data.agents) {
      if (Array.isArray(data.agents)) {
        list = data.agents;
      } else if (typeof data.agents === 'object') {
        list = Object.entries(data.agents).map(([name, info]) => ({
          name: name,
          ...(typeof info === 'object' ? info : { status: String(info) }),
        }));
      }
    }

    const count = data && data.count != null ? data.count : list.length;
    if (els.badgeAgents) els.badgeAgents.textContent = count;

    if (!list.length) {
      els.agentsPanel.replaceChildren(emptyState('i-agents', 'No agents registered'));
      return;
    }

    const container = elt('div', 'agents-list');
    for (const a of list) {
      const s = (a.status || 'ok').toLowerCase();
      const sTag = s === 'ok' || s === 'healthy' ? 'ok'
                 : s === 'warn' || s === 'warning' || s === 'degraded' ? 'warn'
                 : 'error';
      const itemCls = sTag === 'error' ? 'agent-item error'
                    : sTag === 'warn' ? 'agent-item warn'
                    : 'agent-item';

      const detail = a.type || a.role || a.pipeline || a.host || '';

      const item = elt('div', itemCls);
      const info = elt('div', 'agent-info');
      info.appendChild(elt('span', 'agent-name', a.name || 'agent'));
      if (detail) info.appendChild(elt('span', 'agent-detail', detail));
      item.appendChild(info);
      item.appendChild(elt('span', 'agent-status-tag ' + sTag, a.status || 'ok'));
      container.appendChild(item);
    }
    els.agentsPanel.replaceChildren(container);
  }

  /* ---- Metrics (bottom-right) ---- */
  function renderMetrics (health) {
    const metrics = (health && health.metrics) || {};

    // Normalise metric names (accept both camelCase and snake_case)
    const getMetric = (key) => {
      const val = metrics[key];
      if (val != null) return val;
      // Try alternate naming
      const alt = key.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
      return metrics[alt];
    };

    const sent     = getMetric('sent') ?? 0;
    const received = getMetric('received') ?? 0;
    const failed   = getMetric('failed') ?? 0;
    const retried  = getMetric('retried') ?? 0;
    const dlq      = (metrics.dlq != null ? metrics.dlq
                    : (metrics.dlq_count != null ? metrics.dlq_count
                    : (health.dlq_count != null ? health.dlq_count
                    : (health.dlq_entries != null ? health.dlq_entries.length
                    : 0))));

    const items = [
      { icon: 'i-send',     label: 'Sent',     value: sent,     cls: 'ok' },
      { icon: 'i-download', label: 'Received', value: received, cls: 'ok' },
      { icon: 'i-x',        label: 'Failed',   value: failed,   cls: failed > 0 ? 'error' : 'ok' },
      { icon: 'i-refresh',  label: 'Retried',  value: retried,  cls: retried > 0 ? 'warn' : 'ok' },
      { icon: 'i-trash',    label: 'DLQ',      value: dlq,      cls: dlq > 0 ? 'error' : 'ok' },
    ];

    const grid = elt('div', 'metrics-grid');
    for (const m of items) {
      const card = elt('div', 'metric-card');
      const iconWrap = elt('div', 'metric-icon');
      iconWrap.appendChild(iconEl(m.icon));
      card.appendChild(iconWrap);
      const body = elt('div', 'metric-body');
      body.appendChild(elt('div', 'metric-label', m.label));
      const value = elt('div', 'metric-value', formatNum(m.value));
      value.style.color = 'var(--status-' + m.cls + ')';
      body.appendChild(value);
      card.appendChild(body);
      grid.appendChild(card);
    }
    els.metricsPanel.replaceChildren(grid);
  }

  /* ---- Quality (bottom) ---- */
  function renderQuality (data) {
    if (!data || data.total_records == null) {
      els.qualityPanel.replaceChildren(emptyState('i-quality', '暂无质量数据'));
      if (els.badgeQuality) els.badgeQuality.textContent = '--';
      return;
    }
    if (els.badgeQuality) {
      els.badgeQuality.textContent = 'avg ' + (data.avg_score ?? 0);
    }

    const grid = elt('div', 'metrics-grid');
    const items = [
      { label: 'Avg Score', value: data.avg_score ?? 0, cls: (data.avg_score ?? 0) >= 70 ? 'ok' : 'warn' },
      { label: 'Tasks',     value: data.total_tasks ?? 0, cls: 'ok' },
      { label: 'Records',   value: data.total_records ?? 0, cls: 'ok' },
      { label: 'Weak',      value: data.total_weak ?? 0, cls: (data.total_weak ?? 0) > 0 ? 'error' : 'ok' },
    ];
    for (const m of items) {
      const card = elt('div', 'metric-card');
      card.appendChild(elt('div', 'metric-body'));
      const body = card.firstChild;
      body.appendChild(elt('div', 'metric-label', m.label));
      const value = elt('div', 'metric-value', formatNum(m.value));
      value.style.color = 'var(--status-' + m.cls + ')';
      body.appendChild(value);
      card.appendChild(body);
      grid.appendChild(card);
    }

    // 弱项模式提示（最多 3 条）
    const weak = Array.isArray(data.weak_patterns) ? data.weak_patterns.slice(0, 3) : [];
    if (weak.length) {
      const list = elt('div', 'agents-list');
      for (const w of weak) {
        const name = typeof w === 'string' ? w : (w.pattern || w.name || JSON.stringify(w).slice(0, 40));
        list.appendChild(elt('div', 'agent-item warn', String(name)));
      }
      grid.appendChild(list);
    }
    els.qualityPanel.replaceChildren(grid);
  }

  /* ---- Cost (bottom) ---- */
  function renderCost (data) {
    if (!data || data.total_cost == null) {
      els.costPanel.replaceChildren(emptyState('i-cost', '暂无成本数据'));
      if (els.badgeCost) els.badgeCost.textContent = '--';
      return;
    }
    const total = Number(data.total_cost) || 0;
    if (els.badgeCost) els.badgeCost.textContent = '$' + total.toFixed(4);

    const grid = elt('div', 'metrics-grid');
    let remainingCls = 'ok';
    let remainingText = '不限';
    if (data.budget_remaining != null) {
      remainingText = '$' + Number(data.budget_remaining).toFixed(4);
      remainingCls = data.budget_exceeded ? 'error' : (Number(data.budget_remaining) / (Number(data.budget) || 1) < 0.2 ? 'warn' : 'ok');
    }
    const items = [
      { label: 'Total',     value: '$' + total.toFixed(4), cls: 'ok' },
      { label: 'Budget',    value: data.budget > 0 ? '$' + Number(data.budget).toFixed(2) : '不限', cls: 'ok' },
      { label: 'Remaining', value: remainingText, cls: remainingCls },
      { label: 'Exceeded',  value: data.budget_exceeded ? 'Yes' : 'No', cls: data.budget_exceeded ? 'error' : 'ok' },
    ];
    for (const m of items) {
      const card = elt('div', 'metric-card');
      const body = elt('div', 'metric-body');
      body.appendChild(elt('div', 'metric-label', m.label));
      const value = elt('div', 'metric-value', String(m.value));
      value.style.color = 'var(--status-' + m.cls + ')';
      body.appendChild(value);
      card.appendChild(body);
      grid.appendChild(card);
    }

    // 按供应商成本（最多 5 行）
    const byProvider = data.by_provider && typeof data.by_provider === 'object' ? data.by_provider : {};
    const rows = Object.entries(byProvider).slice(0, 5);
    if (rows.length) {
      const list = elt('div', 'agents-list');
      for (const [name, info] of rows) {
        const cost = typeof info === 'number' ? info : (info && (info.cost ?? info.total_cost)) || 0;
        const item = elt('div', 'agent-item');
        const infoEl = elt('div', 'agent-info');
        infoEl.appendChild(elt('span', 'agent-name', name));
        item.appendChild(infoEl);
        item.appendChild(elt('span', 'agent-detail', '$' + (Number(cost) || 0).toFixed(4)));
        list.appendChild(item);
      }
      grid.appendChild(list);
    }
    els.costPanel.replaceChildren(grid);
  }

  /* ---- Logs (full-width bottom) ---- */
  function renderLogs (data) {
    const logs = (data && Array.isArray(data.logs)) ? data.logs : [];
    if (els.badgeLogs) els.badgeLogs.textContent = String(logs.length);

    if (!logs.length) {
      els.logsPanel.replaceChildren(emptyState('i-logs', '暂无日志'));
      return;
    }

    const table = elt('table', 'tasks-table logs-table');
    const thead = elt('thead');
    const headRow = elt('tr');
    for (const h of ['Time', 'Level', 'Agent', 'Message']) {
      headRow.appendChild(elt('th', null, h));
    }
    thead.appendChild(headRow);

    const tbody = elt('tbody');
    for (const entry of logs.slice(0, 30)) {
      const tr = elt('tr');
      const ts = Number(entry.timestamp) || 0;
      const time = ts > 0 ? new Date(ts * 1000).toLocaleTimeString() : '--';
      tr.appendChild(elt('td', 'task-id', time));
      const level = String(entry.level || 'info').toLowerCase();
      tr.appendChild(elt('td', 'log-level ' + level, level.toUpperCase()));
      tr.appendChild(elt('td', 'task-pipeline', String(entry.agent || '-')));
      const msg = String(entry.message || entry.event || entry.msg || '');
      const tdMsg = elt('td', 'log-msg', msg.slice(0, 160));
      tdMsg.title = msg;
      tr.appendChild(tdMsg);
      tbody.appendChild(tr);
    }
    table.appendChild(thead);
    table.appendChild(tbody);
    els.logsPanel.replaceChildren(table);
  }

  // ----------------------------------------------------------
  // Live SSE progress (running task rows)
  // ----------------------------------------------------------
  const liveStreams = new Map();

  function toggleLiveStream (id) {
    if (liveStreams.has(id)) {
      closeLiveStream(id, 0);
      return;
    }
    const cell = elt('td');
    cell.colSpan = 4;
    const wrap = elt('div', 'live-stream');
    wrap.appendChild(elt('div', 'live-label', '实时进度 · ' + id));
    const meta = elt('div', 'live-meta');
    const bar = elt('div', 'progress-wrap live-bar');
    const fill = elt('div', 'progress-fill running');
    fill.style.width = '0%';
    bar.appendChild(fill);
    const text = elt('span', 'progress-text', '0%');
    const sectionName = elt('span', 'live-section', '等待章节事件…');
    meta.appendChild(bar);
    meta.appendChild(text);
    meta.appendChild(sectionName);
    wrap.appendChild(meta);
    cell.appendChild(wrap);
    const row = elt('tr', 'stream-row');
    row.appendChild(cell);

    const token = getToken();
    const url = API_BASE + '/stream?task_id=' + encodeURIComponent(id)
      + (token ? '&token=' + encodeURIComponent(token) : '');
    const es = new EventSource(url);

    liveStreams.set(id, { es, row, fill, text, sectionName });

    es.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      handleLiveEvent(id, msg);
    };
    es.onerror = () => {
      sectionName.textContent = '连接断开';
      closeLiveStream(id, 0);
    };
  }

  function handleLiveEvent (id, msg) {
    const h = liveStreams.get(id);
    if (!h) return;
    const d = (msg && msg.data) || {};
    switch (msg && msg.type) {
      case 'section': {
        const total = Number(msg.total) > 0 ? Number(msg.total) : 0;
        const idx = Number(msg.section) >= 0 ? Number(msg.section) : 0;
        const pctv = total ? Math.min(100, Math.max(0, Math.round(((idx + 1) / total) * 100))) : 0;
        h.fill.style.width = pctv + '%';
        h.text.textContent = pctv + '%';
        h.sectionName.textContent = (d.section_name || ('section ' + (idx + 1)))
          + (d.char_count != null ? ' · ' + formatNum(d.char_count) + ' chars' : '');
        break;
      }
      case 'progress': {
        const total = Number(d.total) > 0 ? Number(d.total) : (Number(msg.total) || 0);
        const cur = Number(d.current) || 0;
        const pctv = total ? Math.min(100, Math.max(0, Math.round((cur / total) * 100))) : 0;
        h.fill.style.width = pctv + '%';
        h.text.textContent = pctv + '%';
        if (d.message) h.sectionName.textContent = d.message;
        break;
      }
      case 'complete':
        h.fill.style.width = '100%';
        h.fill.className = 'progress-fill done';
        h.text.textContent = '100%';
        h.sectionName.textContent = '完成';
        closeLiveStream(id, 1500);
        break;
      case 'error':
        h.fill.className = 'progress-fill failed';
        h.sectionName.textContent = (d.error || '生成失败');
        closeLiveStream(id, 1500);
        break;
      default:
        break;
    }
  }

  function closeLiveStream (id, keepRowMs) {
    const h = liveStreams.get(id);
    if (!h) return;
    liveStreams.delete(id);
    setTimeout(() => {
      try { h.es.close(); } catch (_) { /* noop */ }
      if (h.row.parentNode) h.row.parentNode.removeChild(h.row);
    }, keepRowMs || 0);
  }

  // ----------------------------------------------------------
  // New task form
  // ----------------------------------------------------------
  async function postJson (url, body) {
    const token = getToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const resp = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 401) {
      requestToken();
      throw new Error('HTTP 401（需要 API Token）');
    }
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
    return data;
  }

  async function loadPipelines () {
    if (!els.ntPipeline) return;
    try {
      const info = await fetchJson(API_BASE + '/api/pipeline');
      const files = (info && info.pipeline_files) || [];
      els.ntPipeline.replaceChildren();
      for (const f of files) {
        const name = String(f).replace(/\.ya?ml$/i, '');
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        els.ntPipeline.appendChild(opt);
      }
    } catch (err) {
      console.warn('[Dashboard] 加载流水线列表失败:', err);
    }
  }

  function showTaskMsg (text, isError) {
    if (!els.ntMsg) return;
    els.ntMsg.hidden = false;
    els.ntMsg.textContent = text;
    els.ntMsg.classList.toggle('is-error', !!isError);
  }

  async function submitTask (ev) {
    ev.preventDefault();
    if (!els.ntQuery || !els.ntSubmit) return;
    const query = (els.ntQuery.value || '').trim();
    if (!query) {
      showTaskMsg('请填写 Query', true);
      return;
    }
    const body = { query };
    if (els.ntPipeline && els.ntPipeline.value) body.pipeline = els.ntPipeline.value;
    const output = (els.ntOutput && els.ntOutput.value || '').trim();
    if (output) body.output = output;

    els.ntSubmit.disabled = true;
    showTaskMsg('提交中…', false);
    try {
      const resp = await postJson(API_BASE + '/api/tasks', body);
      showTaskMsg('已提交任务 ' + (resp.task_id || '')
        + '（' + (resp.status || 'pending') + '）', false);
      els.ntQuery.value = '';
      update().catch(() => {});
    } catch (err) {
      showTaskMsg('提交失败：' + (err.message || err), true);
    } finally {
      els.ntSubmit.disabled = false;
    }
  }

  // ----------------------------------------------------------
  // Main update loop
  // ----------------------------------------------------------
  async function update () {
    try {
      state.error = null;
      state.lastUpdated = new Date();
      const data = await fetchAll();
      state.health = data.health;
      state.tasks = data.tasks;
      state.agents = data.agents;
      state.partialFail = data.failedCount > 0;

      // If all endpoints failed, treat it as offline
      if (!data.health && !data.tasks) {
        throw new Error('All API endpoints unreachable');
      }

      state.loaded = true;
      render();
    } catch (err) {
      console.warn('[Dashboard] Fetch error:', err);
      state.error = err.message || 'Connection failed';
      renderError();
    }
  }

  function render () {
    if (state.error) return; // error overlay is shown by renderError
    hideError();
    if (!state.loaded) return;

    els.lastUpdated.textContent = 'Updated ' + timeAgo(state.lastUpdated)
      + (state.partialFail ? ' · 部分数据不可用' : '');

    renderStatus(state.health);
    renderTasks(state.tasks);
    renderAgents(state.agents);
    renderMetrics(state.health);
    renderQuality(state.quality);
    renderCost(state.cost);
    renderLogs(state.logs);
  }

  function renderError () {
    if (!els.errorOverlay) return;
    els.errorOverlay.style.display = 'flex';
    els.dashboard.style.display = 'none';
    if (els.errorMsg) {
      els.errorMsg.textContent =
        state.error || 'Unable to connect to the Doc-Pipeline API at ' + API_BASE;
    }
  }

  function hideError () {
    if (!els.errorOverlay) return;
    els.errorOverlay.style.display = 'none';
    els.dashboard.style.display = '';
  }

  // ----------------------------------------------------------
  // Retry
  // ----------------------------------------------------------
  function handleRetry () {
    if (refreshTimer) clearTimeout(refreshTimer);
    update().finally(scheduleRefresh);
  }

  // ----------------------------------------------------------
  // Scheduler
  // ----------------------------------------------------------
  function scheduleRefresh () {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      await update();
      scheduleRefresh();
    }, REFRESH_MS);
  }

  // ----------------------------------------------------------
  // Init
  // ----------------------------------------------------------
  function init () {
    cacheDom();
    if (els.retryBtn) els.retryBtn.addEventListener('click', handleRetry);
    if (els.newTaskForm) els.newTaskForm.addEventListener('submit', submitTask);
    loadPipelines();
    if (els.tokenSave) {
      const saveToken = () => {
        const v = (els.tokenInput.value || '').trim();
        if (!v) return;
        sessionStorage.setItem(TOKEN_KEY, v);
        els.tokenPrompt.style.display = 'none';
        handleRetry();
      };
      els.tokenSave.addEventListener('click', saveToken);
      els.tokenInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') saveToken();
      });
    }
    els.lastUpdated.textContent = 'Loading…';
    update().then(scheduleRefresh).catch(() => scheduleRefresh());
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
