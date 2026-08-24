/* ============================================================
   Doc-Pipeline v3 Dashboard — Vanilla JS
   ============================================================ */

(function () {
  'use strict';

  // ----------------------------------------------------------
  // Configuration
  // ----------------------------------------------------------
  const API_BASE = 'http://127.0.0.1:8910';
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

    // Badge counters
    els.badgeTasks     = $('#badge-tasks');
    els.badgeAgents    = $('#badge-agents');
    els.badgeStatus    = $('#badge-status');
    els.badgeMetrics   = $('#badge-metrics');
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

  function emptyState (icon, msg) {
    const box = elt('div', 'empty-state');
    box.appendChild(elt('span', 'icon', icon));
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

  function statusEmoji (status) {
    switch ((status || '').toLowerCase()) {
      case 'running': case 'processing': return '🟢';
      case 'pending': case 'waiting':    return '🟡';
      case 'done': case 'completed':
      case 'success': case 'ok':         return '✅';
      case 'failed': case 'error':
      case 'dead': case 'unhealthy':     return '❌';
      default:                           return '⚪';
    }
  }

  // ----------------------------------------------------------
  // API fetcher
  // ----------------------------------------------------------
  const TOKEN_KEY = 'docpipe_token';

  function getToken () { return localStorage.getItem(TOKEN_KEY) || ''; }

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
    ]);
    const health = results[0].status === 'fulfilled' ? results[0].value : null;
    const dash = results[1].status === 'fulfilled' ? results[1].value : null;
    return {
      health,
      tasks: dash || null,           // /api/dashboard 含 tasks[].progress/steps 与 agents
      agents: dash || null,
      failedCount: results.filter(r => r.status === 'rejected').length,
    };
  }

  // ----------------------------------------------------------
  // Renderers
  // ----------------------------------------------------------

  /* ---- Pipeline Status (top-left) ---- */
  function renderStatus (health) {
    if (!health || !health.status) {
      els.statusPanel.replaceChildren(emptyState('📡', 'No status data'));
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
      els.tasksPanel.replaceChildren(emptyState('📋', 'No active tasks'));
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
      const emoji = statusEmoji(t.status);
      const progress = t.progress != null ? t.progress : 0;
      const steps = t.steps != null ? t.steps : 100;
      const pct = Math.min(100, Math.max(0, Math.round((progress / (steps || 100)) * 100)));

      const tdId = elt('td', 'task-id', t.id == null ? '' : String(t.id));
      tdId.title = tdId.textContent;

      const badge = elt('span', 'badge-status ' + sCls, emoji + ' ' + (t.status || 'unknown'));
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
      tr.appendChild(tdId);
      tr.appendChild(elt('td', 'task-pipeline', t.pipeline || '—'));
      tr.appendChild(tdStatus);
      tr.appendChild(tdProgress);
      tbody.appendChild(tr);
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
      els.agentsPanel.replaceChildren(emptyState('🤖', 'No agents registered'));
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
      { icon: '📤', label: 'Sent',     value: sent,     cls: 'ok' },
      { icon: '📥', label: 'Received', value: received, cls: 'ok' },
      { icon: '❌', label: 'Failed',   value: failed,   cls: failed > 0 ? 'error' : 'ok' },
      { icon: '🔄', label: 'Retried',  value: retried,  cls: retried > 0 ? 'warn' : 'ok' },
      { icon: '🗑️', label: 'DLQ',     value: dlq,      cls: dlq > 0 ? 'error' : 'ok' },
    ];

    const grid = elt('div', 'metrics-grid');
    for (const m of items) {
      const card = elt('div', 'metric-card');
      card.appendChild(elt('div', 'metric-icon', m.icon));
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
      + (state.partialFail ? ' · ⚠ 部分数据不可用' : '');

    renderStatus(state.health);
    renderTasks(state.tasks);
    renderAgents(state.agents);
    renderMetrics(state.health);
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
    if (els.tokenSave) {
      const saveToken = () => {
        const v = (els.tokenInput.value || '').trim();
        if (!v) return;
        localStorage.setItem(TOKEN_KEY, v);
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
