// Analytics tab: KPI cards, incident trend and status charts, category breakdown,
// the risk detection radar, the incident activity table and report export. Every figure comes from
// /api/analytics (Flask), which aggregates public.emergency_incidents on the server.
(function () {
  const root = document.getElementById('analytics');
  const cfgEl = document.getElementById('analytics-config');
  if (!root || !cfgEl || !window.AnnUI) return;
  const { h, icon, toast, api } = window.AnnUI;
  const cfg = JSON.parse(cfgEl.textContent);
  const $ = (id) => document.getElementById(id);

  const state = {
    range: cfg.options.defaultRange, from: '', to: '',
    status: '', category: '', critical: false, q: '',
    sort: 'reported', dir: 'desc', page: 1, perPage: cfg.options.pageSizes[0],
  };
  let summary = null;
  let loaded = false;
  let stale = false;
  let summarySeq = 0;
  let tableSeq = 0;

  const SERIES = [
    // Each series differs in line style and point shape as well as colour (see the legend swatches).
    { key: 'total', label: 'Total', color: '#00e5ff', point: 'circle', dash: [], radius: 4 },
    { key: 'resolved', label: 'Resolved', color: '#34d399', point: 'triangle', dash: [7, 4], radius: 5 },
    { key: 'pending', label: 'Pending', color: '#fbbf24', point: 'rect', dash: [2, 3], radius: 4.5 },
  ];
  const GROUP_COLOR = { resolved: '#22d3ee', in_progress: '#3b82f6', pending: '#fbbf24', cancelled: '#64748b' };
  const CRITICAL_COLOR = '#ef4444';
  const STATUS_STYLE = {
    new:          'bg-red-500/15 text-red-300 border-red-500/40',
    acknowledged: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
    dispatched:   'bg-blue-500/15 text-blue-300 border-blue-500/40',
    en_route:     'bg-blue-500/15 text-blue-300 border-blue-500/40',
    on_scene:     'bg-violet-500/15 text-violet-300 border-violet-500/40',
    resolved:     'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
    cancelled:    'bg-slate-600/30 text-slate-400 border-slate-600',
  };
  // Icon and bar colour follow what the category is, never its rank, so a filter can't repaint them.
  const CATEGORY_LOOK = [
    [/medic|ems|health|ambul/i, 'medical_services', '#22d3ee'],
    [/road|traffic|vehic|accident|collision/i, 'car_crash', '#3b82f6'],
    [/fire|hazmat|smoke/i, 'local_fire_department', '#f97316'],
    [/crime|police|law|secur|violen/i, 'local_police', '#a78bfa'],
    [/flood|storm|typhoon|disaster|quake|rescue/i, 'flood', '#2dd4bf'],
  ];
  const categoryLook = (label) => CATEGORY_LOOK.find(([re]) => re.test(label)) || [null, 'more_horiz', '#94a3b8'];

  const num = new Intl.NumberFormat('en-PH');
  const pctText = (v) => (v == null ? '—' : `${v.toFixed(1)}%`);
  const dateFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const fullFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', dateStyle: 'medium', timeStyle: 'medium' });
  const dayFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'UTC', month: 'short', day: '2-digit', year: 'numeric' });
  const fmtDay = (isoDay) => dayFmt.format(new Date(`${isoDay}T00:00:00Z`));

  // ------------------------------------------------------------ requests

  function rangeParams() {
    const p = new URLSearchParams({ range: state.range });
    if (state.range === 'custom') { p.set('from', state.from); p.set('to', state.to); }
    return p;
  }

  function filterParams() {
    const p = rangeParams();
    if (state.status) p.set('status', state.status);
    if (state.category) p.set('category', state.category);
    if (state.critical) p.set('priority', '1');
    if (state.q) p.set('q', state.q);
    return p;
  }

  function setBusy(busy, quiet) {
    root.setAttribute('aria-busy', String(busy));
    $('an-loading').classList.toggle('hidden', !busy);
    $('an-content').classList.toggle('opacity-50', busy && !quiet);
  }

  function showError(message) {
    $('an-error').querySelector('[data-text]').textContent = message;
    $('an-error').classList.remove('hidden');
    $('an-content').classList.add('hidden');   // never leave another period's figures on screen
  }

  // Loads the figures and the table together so every part of the tab describes the same period.
  async function loadAll({ quiet = false } = {}) {
    const seq = ++summarySeq;
    loaded = true;
    stale = false;
    setBusy(true, quiet);
    loadRisk();   // on its own: a failure there is shown in its panel and never hides the other figures
    try {
      const [data] = await Promise.all([api(`${cfg.api.summary}?${rangeParams()}`), loadTable({ rethrow: true })]);
      if (seq !== summarySeq) return;
      summary = data;
      $('an-error').classList.add('hidden');
      $('an-content').classList.remove('hidden');
      renderSummary();
    } catch (err) {
      if (seq !== summarySeq) return;
      if (err.status === 422 && err.fields && state.range === 'custom') showCustomError(err.message);
      showError(err.message);
    } finally {
      if (seq === summarySeq) setBusy(false);
    }
  }

  async function loadTable({ rethrow = false } = {}) {
    const seq = ++tableSeq;
    const p = filterParams();
    p.set('sort', state.sort); p.set('dir', state.dir);
    p.set('page', state.page); p.set('per_page', state.perPage);
    $('an-table-body').classList.add('opacity-50');
    try {
      const data = await api(`${cfg.api.incidents}?${p}`);
      if (seq !== tableSeq) return;
      state.page = data.page;
      renderTable(data);
    } catch (err) {
      if (seq !== tableSeq) return;
      if (rethrow) throw err;
      $('an-table-body').replaceChildren(h('tr', {}, h('td', { colspan: '6', class: 'px-3 py-8 text-center' },
        h('p', { class: 'text-sm text-red-300', role: 'alert', text: err.message }),
        h('button', { type: 'button', class: 'mt-1 text-xs underline text-red-200', onclick: () => loadTable(), text: 'Retry' }))));
      $('an-pager').replaceChildren();
    } finally {
      if (seq === tableSeq) $('an-table-body').classList.remove('opacity-50');
    }
  }

  // ------------------------------------------------------------ summary rendering

  function renderSummary() {
    const p = summary.period;
    $('an-period').textContent = `${fmtDay(p.from)} – ${fmtDay(p.to)} · ${p.days} day${p.days === 1 ? '' : 's'} · compared with ${fmtDay(p.previous.from)} – ${fmtDay(p.previous.to)} · ${p.timezone}`;
    renderKpis();
    renderTrend();
    renderStatus();
    renderCategories();
    renderRiskFilters();
    renderChips();
  }

  // goodWhen: which direction is an improvement ('up', 'down') or null when neither is.
  function changeLine(change, goodWhen) {
    const days = summary.period.days;
    const versus = `vs. previous ${days === 1 ? 'day' : `${days} days`}`;
    if (change == null) {
      return h('p', { class: 'mt-1 text-[0.6875rem] text-slate-400', title: 'The previous period has no figure to compare with, so a percentage change cannot be calculated.' },
        '— no earlier data to compare');
    }
    const up = change > 0;
    const flat = change === 0;
    const tone = flat || !goodWhen ? 'text-slate-300' : (up ? 'up' : 'down') === goodWhen ? 'text-emerald-400' : 'text-red-400';
    return h('p', { class: 'mt-1 flex flex-wrap items-center gap-x-1.5 text-[0.6875rem]' },
      h('span', { class: `inline-flex items-center font-semibold ${tone}` },
        icon(flat ? 'remove' : up ? 'arrow_upward' : 'arrow_downward', 'text-[0.875rem]'), `${up ? '+' : ''}${change.toFixed(1)}%`),
      h('span', { class: 'text-slate-400', text: versus }));
  }

  function kpiCard({ accent, glyph, label, value, sub, change, goodWhen, title }) {
    return h('div', { class: `rounded-xl bg-[#0b1220] border ${accent.border} p-4 flex items-center gap-4 shadow-[0_0_18px_-8px_rgba(0,229,255,0.35)]`, title },
      h('div', { class: `w-14 h-14 shrink-0 rounded-xl border ${accent.border} ${accent.tile} flex items-center justify-center` }, icon(glyph, `text-[1.875rem] ${accent.text}`)),
      h('div', { class: 'min-w-0' },
        h('p', { class: 'text-xs font-semibold text-slate-300', text: label }),
        h('p', { class: 'text-2xl font-bold text-white leading-tight', text: value }),
        sub && h('p', { class: 'text-[0.6875rem] text-slate-400', text: sub }),
        changeLine(change, goodWhen)));
  }

  function renderKpis() {
    const k = summary.kpis;
    const none = k.total.value === 0;
    $('an-kpis').replaceChildren(
      kpiCard({
        accent: { border: 'border-red-500/40', tile: 'bg-red-500/10', text: 'text-red-400' }, glyph: 'e911_emergency',
        label: 'Total Incidents', value: num.format(k.total.value), change: k.total.change, goodWhen: null,
        sub: none ? 'None reported in this range' : `${num.format(k.total.previous)} in the previous period`,
        title: 'Incidents reported in the selected range, including archived ones.',
      }),
      kpiCard({
        accent: { border: 'border-sky-500/40', tile: 'bg-sky-500/10', text: 'text-sky-400' }, glyph: 'schedule',
        label: 'Average Resolution Time', value: k.resolution.label, change: k.resolution.change, goodWhen: 'down',
        sub: k.resolution.sample ? `Across ${num.format(k.resolution.sample)} resolved incident${k.resolution.sample === 1 ? '' : 's'}` : 'No resolved incidents yet',
        title: 'Average time from report to resolution. Unresolved and cancelled incidents are excluded.',
      }),
      kpiCard({
        accent: { border: 'border-emerald-500/40', tile: 'bg-emerald-500/10', text: 'text-emerald-400' }, glyph: 'check_circle',
        label: 'Resolved Incidents', value: num.format(k.resolved.value), change: k.resolved.change, goodWhen: 'up',
        sub: k.resolved.rate == null ? 'Resolution rate unavailable' : `${pctText(k.resolved.rate)} resolution rate`,
        title: 'Incidents reported in the range whose status is Resolved. Rate = resolved ÷ total reported.',
      }),
      kpiCard({
        accent: { border: 'border-amber-500/40', tile: 'bg-amber-500/10', text: 'text-amber-400' }, glyph: 'pending_actions',
        label: 'Pending Incidents', value: num.format(k.pending.value), change: k.pending.change, goodWhen: 'down',
        sub: k.pending.share == null ? 'Share unavailable' : `${pctText(k.pending.share)} of total`,
        title: 'Incidents still waiting for acknowledgment or dispatch (New or Acknowledged).',
      }));
  }

  // ------------------------------------------------------------ trend chart

  const hasCharts = Boolean(window.Chart);
  if (hasCharts) {
    Chart.defaults.color = '#94a3b8';
    Chart.defaults.font.family = '"Plus Jakarta Sans", Inter, sans-serif';
    // Charts are drawn on a canvas in pixels, so follow the display size (Settings -> Display) by hand.
    Chart.defaults.font.size = 11 * parseFloat(getComputedStyle(document.documentElement).fontSize) / 16;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) Chart.defaults.animation = false;
  }
  const tooltipLook = { backgroundColor: '#0d1424', borderColor: 'rgba(0,229,255,0.35)', borderWidth: 1, titleColor: '#fff', bodyColor: '#e2e8f0', padding: 10, usePointStyle: true, boxPadding: 4 };
  const hiddenSeries = new Set();
  let trendChart = null;
  let statusChart = null;

  function chartMessage(el, text) {
    el.textContent = text;
    el.classList.toggle('hidden', !text);
  }

  function renderTrend() {
    const buckets = summary.trend.buckets;
    $('an-trend-unit').textContent = { hour: '· per hour', day: '· per day', month: '· per month' }[summary.trend.granularity];
    const empty = summary.kpis.total.value === 0;
    chartMessage($('an-trend-empty'), !hasCharts ? 'The chart library could not be loaded. Check your connection and refresh.'
      : empty ? 'No incidents were reported in this range.' : '');
    $('an-trend').classList.toggle('invisible', empty || !hasCharts);
    if (!hasCharts) return;
    renderTrendTable(buckets);
    $('an-trend').setAttribute('aria-label', empty ? 'Incident trend: no incidents were reported in this range.'
      : `Incident trend ${{ hour: 'per hour', day: 'per day', month: 'per month' }[summary.trend.granularity]}: ${SERIES.map((x) => `${x.label} ${num.format(summary.kpis[x.key].value)}`).join(', ')} over ${buckets.length} intervals. The data table below lists every value.`);
    const dense = buckets.length > 45;
    const datasets = SERIES.map((s, i) => ({
      label: s.label, data: buckets.map((b) => b[s.key]), hidden: hiddenSeries.has(s.key),
      borderColor: s.color, backgroundColor: i === 0 ? 'rgba(0,229,255,0.07)' : s.color, fill: i === 0,
      borderWidth: 2, borderDash: s.dash, cubicInterpolationMode: 'monotone', pointStyle: s.point, pointRadius: dense ? 0 : s.radius, pointHoverRadius: s.radius + 3,
      pointBackgroundColor: s.color, pointBorderColor: '#0b1220', pointBorderWidth: 1.5,
    }));
    if (trendChart) {
      trendChart.data.labels = buckets.map((b) => b.label);
      trendChart.data.datasets = datasets;
      trendChart.update();
      return;
    }
    trendChart = new Chart($('an-trend'), {
      type: 'line',
      data: { labels: buckets.map((b) => b.label), datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipLook, callbacks: { title: (items) => summary.trend.buckets[items[0].dataIndex]?.title || '' } },
        },
        scales: {
          x: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { maxTicksLimit: 12, maxRotation: 0, autoSkipPadding: 12 } },
          y: { beginAtZero: true, suggestedMax: 4, grid: { color: 'rgba(255,255,255,0.06)' }, ticks: { precision: 0 }, title: { display: true, text: 'Incidents' } },
        },
      },
    });
  }

  function renderTrendTable(buckets) {
    const cell = (tag, value, cls = '') => h(tag, { class: `px-2 py-1 ${cls}`, scope: tag === 'th' ? 'col' : null, text: value });
    $('an-trend-table').replaceChildren(
      h('caption', { class: 'sr-only', text: 'Incidents reported per interval: total, resolved and pending' }),
      h('thead', {}, h('tr', { class: 'text-left text-slate-400' }, cell('th', 'Interval'), ...SERIES.map((x) => cell('th', x.label, 'text-right')))),
      h('tbody', {}, ...buckets.map((b) => h('tr', { class: 'border-t border-brand-borderSubtle/60' },
        h('th', { scope: 'row', class: 'px-2 py-1 text-left font-normal text-slate-300', text: b.title }),
        ...SERIES.map((x) => cell('td', num.format(b[x.key]), 'text-right tabular-nums text-slate-100'))))));
  }

  $('an-trend-legend').querySelectorAll('[data-series]').forEach((btn) => btn.addEventListener('click', () => {
    const key = btn.dataset.series;
    const show = hiddenSeries.has(key);
    if (show) hiddenSeries.delete(key); else hiddenSeries.add(key);
    btn.setAttribute('aria-pressed', String(show));
    if (!trendChart) return;
    trendChart.setDatasetVisibility(SERIES.findIndex((s) => s.key === key), show);
    trendChart.update();
  }));

  // ------------------------------------------------------------ status donut

  function renderStatus() {
    const groups = summary.status;
    const total = summary.kpis.total.value;
    $('an-status-total').textContent = num.format(total);
    $('an-status').classList.toggle('invisible', total === 0 || !hasCharts);
    $('an-status').setAttribute('aria-label', `Incidents by current status, ${num.format(total)} in total: `
      + groups.map((g) => `${g.label} ${num.format(g.count)}`).join(', ') + '. The list beside the chart shows the same figures and filters the table.');

    const legendRow = ({ key, label, count, pct, color, pressed, note, onclick }) => h('button', {
      type: 'button', 'aria-pressed': String(pressed), onclick, title: note || `Filter the incident table by ${label}`,
      class: 'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left hover:bg-white/5 aria-pressed:bg-brand-cyan/10 aria-pressed:ring-1 aria-pressed:ring-brand-cyan/50',
    },
      h('span', { class: 'w-2.5 h-2.5 rounded-full shrink-0', style: `background:${color}` }),
      h('span', { class: 'flex-1 min-w-0 truncate text-slate-200', text: label }),
      h('span', { class: 'font-bold text-white tabular-nums', text: num.format(count) }),
      h('span', { class: 'w-12 text-right text-slate-400 tabular-nums text-xs', text: pctText(pct) }));

    $('an-status-legend').replaceChildren(
      ...groups.map((g) => legendRow({ ...g, color: GROUP_COLOR[g.key], pressed: state.status === g.key, onclick: () => toggleFilter('status', g.key) })),
      h('div', { class: 'my-1 border-t border-brand-borderSubtle' }),
      legendRow({
        key: 'critical', label: 'Critical (P1)', count: summary.critical.count, pct: summary.critical.pct, color: CRITICAL_COLOR,
        pressed: state.critical, onclick: () => toggleFilter('critical', true),
        note: 'Priority P1. A priority, not a status: these incidents are already counted in the statuses above.',
      }));

    if (!hasCharts) return;
    const data = {
      labels: groups.map((g) => g.label),
      datasets: [{ data: groups.map((g) => g.count), backgroundColor: groups.map((g) => GROUP_COLOR[g.key]), borderColor: '#0b1220', borderWidth: 2, hoverOffset: 4 }],
    };
    if (statusChart) {
      statusChart.data = data;
      statusChart.update();
      return;
    }
    statusChart = new Chart($('an-status'), {
      type: 'doughnut',
      data,
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '74%',
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipLook, callbacks: { label: (c) => ` ${c.label}: ${num.format(c.raw)} (${pctText(summary.status[c.dataIndex].pct)})` } },
        },
        onClick: (_, hit) => { if (hit.length) toggleFilter('status', summary.status[hit[0].index].key); },
        onHover: (e, hit) => { e.native.target.style.cursor = hit.length ? 'pointer' : 'default'; },
      },
    });
  }

  // ------------------------------------------------------------ categories

  function renderCategories() {
    const cats = summary.categories;
    const select = $('an-table-filters').elements.category;
    select.replaceChildren(h('option', { value: '', text: 'All categories' }),
      ...cats.map((c) => h('option', { value: c.key, text: c.label })));
    if (state.category && !cats.some((c) => c.key === state.category)) select.append(h('option', { value: state.category, text: state.category }));
    select.value = state.category;

    if (!cats.length) {
      $('an-categories').replaceChildren(h('p', { class: 'py-10 text-center text-sm text-slate-400', text: 'No incidents were reported in this range.' }));
      return;
    }
    const max = cats[0].count;
    $('an-categories').replaceChildren(...cats.map((c) => {
      const [, glyph, color] = categoryLook(c.label);
      return h('button', {
        type: 'button', 'aria-pressed': String(state.category === c.key), onclick: () => toggleFilter('category', c.key),
        title: `${c.label}: ${num.format(c.count)} incident${c.count === 1 ? '' : 's'} (${pctText(c.pct)}). Click to filter the table.`,
        class: 'w-full grid grid-cols-[1.5rem_minmax(0,1fr)_2.5rem_3rem] items-center gap-x-2 px-2 py-2 rounded-md text-left hover:bg-white/5 aria-pressed:bg-brand-cyan/10 aria-pressed:ring-1 aria-pressed:ring-brand-cyan/50',
      },
        h('span', { class: 'material-symbols-outlined text-[1.25rem]', style: `color:${color}`, 'aria-hidden': 'true', text: glyph }),
        h('span', { class: 'min-w-0' },
          h('span', { class: 'block truncate text-sm text-slate-200', text: c.label }),
          h('span', { class: 'block mt-1 h-2 rounded-full bg-slate-800/80 overflow-hidden' },
            h('span', { class: 'block h-full rounded-full', style: `width:${Math.max(2, (c.count / max) * 100)}%;background:${color}` }))),
        h('span', { class: 'text-right text-sm font-bold text-white tabular-nums', text: num.format(c.count) }),
        h('span', { class: 'text-right text-xs text-slate-400 tabular-nums', text: pctText(c.pct) }));
    }));
  }

  // ------------------------------------------------------------ table filters

  function toggleFilter(name, value) {
    state[name] = state[name] === value ? (name === 'critical' ? false : '') : value;
    applyFilters();
  }

  function applyFilters() {
    state.page = 1;
    const form = $('an-table-filters').elements;
    form.status.value = state.status;
    form.category.value = state.category;
    form.q.value = state.q;
    if (summary) { renderStatus(); renderCategories(); }
    renderChips();
    loadTable();
  }

  function renderChips() {
    const chips = [];
    const chip = (text, clear) => h('span', { class: 'inline-flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full bg-brand-cyan/10 border border-brand-cyan/40 text-brand-cyan' }, text,
      h('button', { type: 'button', class: 'rounded-full hover:bg-brand-cyan/20 leading-none', 'aria-label': `Remove filter ${text}`, onclick: () => { clear(); applyFilters(); } }, icon('close', 'text-[0.875rem]')));
    if (state.status) chips.push(chip(`Status: ${cfg.options.groups[state.status]}`, () => { state.status = ''; }));
    if (state.category) chips.push(chip(`Category: ${summary?.categories.find((c) => c.key === state.category)?.label || state.category}`, () => { state.category = ''; }));
    if (state.critical) chips.push(chip('Priority: Critical (P1)', () => { state.critical = false; }));
    if (state.q) chips.push(chip(`Search: ${state.q}`, () => { state.q = ''; }));
    if (chips.length > 1) chips.push(h('button', { type: 'button', class: 'text-slate-400 underline', onclick: clearFilters, text: 'Clear all' }));
    $('an-chips').replaceChildren(...chips);
    $('an-chips').classList.toggle('hidden', !chips.length);
  }

  function clearFilters() {
    Object.assign(state, { status: '', category: '', critical: false, q: '' });
    applyFilters();
  }
  const hasFilters = () => Boolean(state.status || state.category || state.critical || state.q);

  let searchTimer;
  $('an-table-filters').addEventListener('input', (e) => {
    const form = e.currentTarget.elements;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      Object.assign(state, { q: form.q.value.trim(), status: form.status.value, category: form.category.value });
      applyFilters();
    }, e.target.name === 'q' ? 350 : 0);
  });
  $('an-table-filters').addEventListener('submit', (e) => e.preventDefault());

  // ------------------------------------------------------------ table

  const COLUMNS = [
    ['id', 'ID', true], ['category', 'Category', true], [null, 'Location', false],
    ['status', 'Status', true], [null, 'Response Time', false], ['reported', 'Reported', true],
  ];

  function renderHead() {
    $('an-table-head').replaceChildren(...COLUMNS.map(([key, label, sortable]) => {
      const cls = 'px-3 py-2 text-left font-mono text-[0.625rem] uppercase tracking-wider text-slate-400 whitespace-nowrap';
      if (!sortable) return h('th', { class: cls, scope: 'col', text: label, title: label === 'Response Time' ? 'Report to first On Scene update' : null });
      const active = state.sort === key;
      return h('th', { class: cls, scope: 'col', 'aria-sort': active ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
        h('button', {
          type: 'button', class: `inline-flex items-center gap-0.5 uppercase tracking-wider hover:text-white ${active ? 'text-brand-cyan' : ''}`,
          title: `Sort by ${label}`,
          onclick: () => {
            state.dir = active && state.dir === 'desc' ? 'asc' : 'desc';
            state.sort = key;
            state.page = 1;
            loadTable();
          },
        }, label, icon(active ? (state.dir === 'asc' ? 'arrow_upward' : 'arrow_downward') : 'unfold_more', 'text-[0.8125rem]')));
    }));
  }

  function openIncident(i) {
    const opened = window.ESRMSDispatch?.open(i);
    if (!opened) toast(`${i.code} is not in the live Dispatch Center list yet. Refresh the Dispatch Center and try again.`, 'info');
  }

  function row(i) {
    const td = (...c) => h('td', { class: 'px-3 py-1.5 align-middle' }, ...c);
    const [, glyph, color] = categoryLook(i.category);
    const specific = i.emergency_type.includes(':') ? i.emergency_type.split(':').slice(1).join(':').trim() : '';
    return h('tr', {
      tabindex: '0', role: 'link', 'aria-label': `Open ${i.code}`,
      class: 'border-b border-brand-borderSubtle/60 cursor-pointer hover:bg-brand-cyan/5 focus-visible:bg-brand-cyan/10',
      title: i.archived ? 'Open in Incident Report' : 'Open in Dispatch Center',
      onclick: () => openIncident(i),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openIncident(i); } },
    },
      td(h('span', { class: 'font-mono text-xs text-brand-cyan whitespace-nowrap', text: i.code }),
        i.priority === 1 && h('span', { class: 'ml-1.5 px-1 rounded text-[0.625rem] font-mono font-bold bg-red-700 text-white', title: 'Critical priority', text: 'P1' })),
      td(h('div', { class: 'flex items-center gap-2 min-w-[10rem]' },
        h('span', { class: 'material-symbols-outlined text-[1.125rem] shrink-0', style: `color:${color}`, 'aria-hidden': 'true', text: glyph }),
        h('div', { class: 'min-w-0' },
          h('div', { class: 'text-slate-100 truncate', text: i.category }),
          specific && h('div', { class: 'text-[0.6875rem] text-slate-400 truncate', text: specific })))),
      td(i.has_location
        ? h('span', { class: 'font-mono text-xs text-slate-300 whitespace-nowrap', title: i.location_verified ? 'GPS location verified by a dispatcher' : 'GPS location, not verified' },
            `${Number(i.latitude).toFixed(5)}, ${Number(i.longitude).toFixed(5)}`, i.location_verified && h('span', { class: 'text-emerald-400', text: ' ✓' }))
        : h('span', { class: 'text-xs text-slate-400', text: 'Unavailable' })),
      td(h('span', { class: `inline-block px-2 py-0.5 rounded-full border text-[0.6875rem] font-semibold whitespace-nowrap ${STATUS_STYLE[i.status] || ''}`, text: i.status_label })),
      td(h('span', { class: 'font-mono text-xs text-slate-300 whitespace-nowrap', title: i.response_seconds == null ? 'No On Scene arrival was recorded for this incident.' : 'Report to first On Scene update', text: i.response_seconds == null ? '—' : i.response_label })),
      td(h('span', { class: 'text-xs text-slate-300 whitespace-nowrap', title: fullFmt.format(new Date(i.created_at)), text: dateFmt.format(new Date(i.created_at)) })));
  }

  function renderTable(data) {
    renderHead();
    if (!data.items.length) {
      $('an-table-body').replaceChildren(h('tr', {}, h('td', { colspan: '6', class: 'px-3 py-10 text-center text-sm text-slate-400' },
        hasFilters() ? 'No incidents match these filters. ' : 'No incidents were reported in this range.',
        hasFilters() && h('button', { type: 'button', class: 'text-brand-cyan underline', onclick: clearFilters, text: 'Clear filters' }))));
    } else {
      $('an-table-body').replaceChildren(...data.items.map(row));
    }
    const first = (data.page - 1) * data.per_page + 1;
    const pageBtn = (label, glyph, page, disabled) => h('button', {
      type: 'button', disabled, 'aria-label': label,
      class: 'p-1 rounded border border-brand-borderSubtle text-slate-300 hover:text-brand-cyan hover:border-brand-cyan/50 disabled:opacity-30 disabled:pointer-events-none',
      onclick: () => { state.page = page; loadTable(); },
    }, icon(glyph, 'text-[1.125rem] align-middle'));
    $('an-pager').replaceChildren(
      h('span', { text: data.total ? `Showing ${num.format(first)}–${num.format(first + data.items.length - 1)} of ${num.format(data.total)}` : 'No incidents to show' }),
      h('div', { class: 'flex items-center gap-2' },
        h('label', { class: 'flex items-center gap-1.5' }, 'Rows',
          h('select', {
            class: 'px-1.5 py-1 rounded bg-[#0a1020] border border-brand-borderSubtle text-xs text-slate-100',
            onchange: (e) => { state.perPage = Number(e.target.value); state.page = 1; loadTable(); },
          }, ...cfg.options.pageSizes.map((n) => h('option', { value: n, selected: n === data.per_page, text: n })))),
        pageBtn('Previous page', 'chevron_left', data.page - 1, data.page <= 1),
        h('span', { class: 'tabular-nums', text: `Page ${data.page} of ${data.pages}` }),
        pageBtn('Next page', 'chevron_right', data.page + 1, data.page >= data.pages)));
  }

  // ------------------------------------------------------------ risk detection radar
  // Areas, scores, levels, coverage and every position are calculated on the server from the incident
  // records and the organization bases (app/risk.py). This only draws what /api/analytics/risk returns;
  // nothing here is sample data, and only fixed bases are shown: no responder or vehicle is located.

  const risk = { category: '', priority: '', level: '', data: null, seq: 0, showAll: false, pinned: null };
  const RISK_TOP = 5;
  // [dot, badge, map colour]. The level is always written out too, never shown by colour alone.
  const RISK_LOOK = {
    high: ['bg-red-500', 'text-red-200 border-red-500/60 bg-red-500/15', '#ef4444'],
    moderate: ['bg-amber-400', 'text-amber-200 border-amber-400/60 bg-amber-400/10', '#f59e0b'],
    low: ['bg-yellow-200', 'text-slate-200 border-slate-500 bg-slate-500/10', '#fde047'],
    insufficient: ['bg-slate-500', 'text-slate-300 border-slate-600 bg-slate-600/20', '#94a3b8'],
  };
  const TREND_LOOK = {
    increasing: ['arrow_upward', 'text-red-300'], decreasing: ['arrow_downward', 'text-emerald-300'],
    stable: ['arrow_forward', 'text-slate-300'], insufficient: ['remove', 'text-slate-400'],
  };
  const ORG_LETTER = { ems: 'E', fire: 'F', police: 'P', barangay: 'B', hospital: 'H', other: 'O' };
  const plural = (n, word) => `${num.format(n)} ${word}${n === 1 ? '' : 's'}`;
  const km = (v) => `${v.toFixed(1)} km`;
  const levelText = (a) => (a.level === 'insufficient' ? 'Insufficient data' : a.level_label);
  // Incident points by priority: P1 and P2 red, P3 orange, P4 or not yet set yellow.
  const incidentColor = (p) => (p === 1 || p === 2 ? '#ef4444' : p === 3 ? '#f97316' : '#fde047');
  const riskFilters = $('an-risk-filters').elements;
  const riskDialog = $('an-risk-dialog');

  function riskParams() {
    const p = rangeParams();
    if (risk.category) p.set('category', risk.category);
    if (risk.priority) p.set('priority', risk.priority);
    return p;
  }

  function levelBadge(area, extra = '') {
    return h('span', { class: `inline-flex items-center px-1.5 py-px rounded border text-[0.625rem] font-mono font-bold uppercase tracking-wide ${RISK_LOOK[area.level][1]} ${extra}`,
      text: area.level === 'insufficient' ? 'Insufficient data' : `${area.level_label}${area.level === 'high' ? ' risk' : ''}` });
  }

  function trendLine(area, cls = 'text-[0.6875rem]') {
    const [glyph, tone] = TREND_LOOK[area.trend];
    const detail = area.trend === 'insufficient' ? ''
      : area.change != null ? ` ${area.change > 0 ? '+' : ''}${area.change.toFixed(0)}% vs previous period (${num.format(area.previous)} before)`
        : ` (${num.format(area.previous)} in the previous period)`;
    return h('span', { class: `inline-flex items-center gap-0.5 ${cls} ${tone}` }, icon(glyph, 'text-[0.875rem]'),
      h('span', { class: 'font-semibold', text: area.trend_label }), h('span', { class: 'text-slate-400', text: detail }));
  }

  async function loadRisk() {
    const seq = ++risk.seq;
    $('an-radar').classList.add('opacity-50');
    try {
      const data = await api(`${cfg.api.risk}?${riskParams()}`);
      if (seq !== risk.seq) return;
      risk.data = data;
      renderRisk();
    } catch (err) {
      if (seq !== risk.seq) return;
      risk.data = risk.pinned = null;   // never leave another period's areas on screen
      $('an-risk-note').classList.add('hidden');
      $('an-risk-count').textContent = '';
      $('an-risk-map').disabled = true;
      $('an-radar-scale').textContent = '';
      $('an-radar-summary').replaceChildren();
      $('an-radar-gaps').replaceChildren();
      $('an-risk-list').replaceChildren();
      $('an-radar').replaceChildren(radarGrid(null));
      chartMessage($('an-radar-empty'), '');
      $('an-radar-readout').replaceChildren(
        h('p', { class: 'text-sm text-red-300', role: 'alert', text: err.message }),
        h('button', { type: 'button', class: 'mt-1 text-xs underline text-red-200', onclick: loadRisk, text: 'Retry' }));
    } finally {
      if (seq === risk.seq) $('an-radar').classList.remove('opacity-50');
    }
  }

  // The incident type choices are the period's own categories (same list as Incidents by Category).
  function renderRiskFilters() {
    const cats = summary ? summary.categories : [];
    riskFilters.category.replaceChildren(h('option', { value: '', text: 'All Types' }),
      ...cats.map((c) => h('option', { value: c.key, text: c.label })));
    if (risk.category && !cats.some((c) => c.key === risk.category)) riskFilters.category.append(h('option', { value: risk.category, text: risk.category }));
    riskFilters.category.value = risk.category;
  }

  const shownAreas = () => risk.data.areas.filter((a) => !risk.level || a.level === risk.level);

  function renderRisk() {
    const data = risk.data;
    const t = data.totals;
    if (riskFilters.priority.options.length === 1) {
      riskFilters.priority.append(...Object.entries(data.priorities).map(([value, text]) => h('option', { value, text })));
    }
    riskFilters.priority.value = risk.priority;

    const note = $('an-risk-note');
    note.classList.toggle('hidden', !data.reason);
    note.replaceChildren(icon('info', 'text-[1rem] shrink-0'), h('span', { text: data.reason || '' }));

    const shown = shownAreas();
    renderRadar(shown);
    renderRadarSummary();
    renderGaps(shown);
    renderRiskList(shown);

    $('an-risk-map').disabled = !shown.length || !window.ESRMSDispatch?.map;
    const hidden = t.located - data.radar.plotted;
    $('an-risk-count').textContent = t.incidents
      ? `${plural(t.located, 'located incident')} in ${plural(t.areas, 'area')}${t.unlocated ? ` · ${num.format(t.unlocated)} without GPS not placed` : ''}`
        + (hidden > 0 ? ` · the newest ${num.format(data.radar.plotted)} are drawn` : '')
      : '';
    const m = data.method;
    $('an-risk-method').textContent = `${m.areas} ${m.score} ${m.levels} ${m.minimum} Trend compares an area with the same area in the previous period. `
      + 'The radar centre is the middle of the located incidents; every point is placed by its own recorded coordinates. '
      + 'This is an analysis of past reports, not a declaration that a place is dangerous.';
  }

  // ---- the radar itself

  const SVG_NS = 'http://www.w3.org/2000/svg';
  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'text') el.textContent = v;
      else el.setAttribute(k, v);
    }
    el.append(...children.flat().filter(Boolean));
    return el;
  }
  const RADAR_R = 100;
  const CYAN = '#00e5ff';

  // Rings, crosshair and distance labels. `rangeKm` is the real distance the outer ring stands for.
  function radarGrid(rangeKm) {
    const diagonal = RADAR_R * Math.SQRT1_2;
    return s('g', { 'aria-hidden': 'true', 'pointer-events': 'none' },
      ...[1, 2, 3, 4].map((i) => s('circle', { r: RADAR_R * i / 4, fill: 'none', stroke: CYAN, 'stroke-opacity': i === 4 ? 0.7 : 0.25, 'stroke-width': 0.6 })),
      s('line', { x1: -RADAR_R, x2: RADAR_R, stroke: CYAN, 'stroke-opacity': 0.3, 'stroke-width': 0.5 }),
      s('line', { y1: -RADAR_R, y2: RADAR_R, stroke: CYAN, 'stroke-opacity': 0.3, 'stroke-width': 0.5 }),
      s('line', { x1: -diagonal, y1: -diagonal, x2: diagonal, y2: diagonal, stroke: CYAN, 'stroke-opacity': 0.12, 'stroke-width': 0.5 }),
      s('line', { x1: -diagonal, y1: diagonal, x2: diagonal, y2: -diagonal, stroke: CYAN, 'stroke-opacity': 0.12, 'stroke-width': 0.5 }),
      s('path', { d: 'M-4 0H4M0 -4V4', stroke: CYAN, 'stroke-width': 0.9 }),
      s('text', { y: -RADAR_R - 3, 'text-anchor': 'middle', 'font-size': 6, 'font-family': 'JetBrains Mono, monospace', fill: CYAN, text: 'N' }),
      rangeKm && [1, 2, 3, 4].map((i) => s('text', {
        x: 2, y: -RADAR_R * i / 4 + 6.5, 'font-size': 5, 'font-family': 'JetBrains Mono, monospace', fill: CYAN, 'fill-opacity': 0.75,
        text: `${(rangeKm * i / 4).toFixed(rangeKm < 20 ? 1 : 0)} km`,
      })));
  }

  // Hover or keyboard focus previews a mark in the readout; a click or Enter keeps it there.
  function mark(el, item) {
    el.classList.add('esrms-radar__mark');
    el.dataset.key = item.key;
    el.addEventListener('mouseenter', () => renderReadout(item));
    el.addEventListener('mouseleave', () => renderReadout(risk.pinned));
    el.addEventListener('focus', () => renderReadout(item));
    el.addEventListener('blur', () => renderReadout(risk.pinned));
    el.addEventListener('click', () => pin(item));
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pin(item); } });
    return el;
  }

  function pin(item) {
    risk.pinned = item;
    for (const el of $('an-radar').querySelectorAll('.esrms-radar__mark')) {
      el.classList.toggle('esrms-radar__mark--on', Boolean(item) && el.dataset.key === item.key);
    }
    renderReadout(item);
  }

  function renderRadar(shown) {
    const r = risk.data.radar;
    const svg = $('an-radar');
    const empty = !shown.length;
    chartMessage($('an-radar-empty'), !empty ? '' : risk.data.areas.length ? 'No area is at this risk level in this range.'
      : risk.data.reason || 'No incidents to place in this range.');
    if (!r.center) {
      svg.replaceChildren(radarGrid(null));
      svg.setAttribute('aria-label', 'Risk detection radar: no located incidents in this range.');
      $('an-radar-scale').textContent = '';
      pin(null);
      return;
    }
    const scale = RADAR_R / r.range_km;
    const at = (o) => [o.east_km * scale, -o.north_km * scale];
    const ids = new Set(shown.map((a) => a.id));
    const incidents = r.incidents.filter((i) => ids.has(i.area));
    const bases = r.organizations.filter((o) => Math.hypot(o.east_km, o.north_km) <= r.range_km);
    const items = new Map();
    const item = (kind, d) => { const it = { key: `${kind}:${d.id}`, kind, d }; items.set(it.key, it); return it; };

    // Largest zones first, so a small zone inside a large one can still be picked.
    const zones = [...shown].sort((a, b) => b.radius_km - a.radius_km).map((a) => {
      const [cx, cy] = at(a);
      const color = RISK_LOOK[a.level][2];
      return mark(s('circle', {
        cx, cy, r: Math.max(a.radius_km * scale, 7), fill: color, 'fill-opacity': 0.16, stroke: color, 'stroke-width': 1, 'stroke-dasharray': '3 2',
        role: 'button', tabindex: 0, 'aria-label': `${levelText(a)} risk area ${a.label}: ${plural(a.count, 'historical incident')}. Press Enter for the analysis.`,
      }), item('area', a));
    });
    const baseMarks = bases.map((o) => {
      const [x, y] = at(o);
      return mark(s('g', { transform: `translate(${x} ${y})`, role: 'button', tabindex: 0, 'aria-label': `${o.base_label} ${o.name}, ${o.status_label}, ${km(o.center_km)} from the radar centre` },
        s('rect', { x: -3.4, y: -3.4, width: 6.8, height: 6.8, rx: 1.4, fill: o.available ? '#2563eb' : '#1e3a8a', stroke: '#bfdbfe', 'stroke-width': 0.8, 'stroke-dasharray': o.available ? null : '1.5 1' }),
        s('text', { y: 1.7, 'text-anchor': 'middle', 'font-size': 4.6, 'font-weight': 700, 'font-family': 'JetBrains Mono, monospace', fill: '#fff', 'pointer-events': 'none', text: ORG_LETTER[o.org_type] || 'O' })),
      item('org', o));
    });
    // Oldest first, so the newest incident of a spot is the one on top. Reached by pointer; the table lists them for the keyboard.
    const points = [...incidents].reverse().map((i) => {
      const [cx, cy] = at(i);
      return mark(s('circle', { cx, cy, r: 2.3, fill: incidentColor(i.priority), stroke: '#0b1220', 'stroke-width': 0.6, role: 'button', tabindex: -1,
        'aria-label': `${i.code}, ${i.emergency_type}, ${i.priority_label}` }), item('incident', i));
    });

    svg.replaceChildren(
      s('defs', {}, s('clipPath', { id: 'an-radar-clip' }, s('circle', { r: RADAR_R }))),
      radarGrid(r.range_km),
      s('g', { 'clip-path': 'url(#an-radar-clip)' }, zones),
      s('g', {}, baseMarks),
      s('g', {}, points));
    svg.setAttribute('aria-label', `Risk detection radar: ${plural(incidents.length, 'incident')} in ${plural(shown.length, 'area')} and ${plural(bases.length, 'organization base')}, `
      + `within ${km(r.range_km)} of the centre. The ranked list below gives the same areas.`);
    const beyond = r.organizations.length - bases.length;
    $('an-radar-scale').textContent = `Centre ${r.center.latitude.toFixed(4)}, ${r.center.longitude.toFixed(4)} · outer ring ${km(r.range_km)}`
      + (beyond ? ` · ${plural(beyond, 'base')} beyond range` : '');
    pin(risk.pinned && items.get(risk.pinned.key) || null);
  }

  function renderReadout(item) {
    const box = $('an-radar-readout');
    const title = (text, tone = 'text-brand-cyan') => h('p', { class: `font-mono font-bold uppercase tracking-widest text-[0.6875rem] ${tone}`, text });
    const line = (label, ...value) => h('div', { class: 'grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-2' },
      h('dt', { class: 'text-slate-400', text: label }), h('dd', { class: 'text-slate-100 break-words' }, ...value));
    const action = (text, onclick) => h('button', { type: 'button', onclick, text,
      class: 'mt-2 px-2.5 py-1 rounded-md border border-brand-cyan/60 text-brand-cyan text-[0.6875rem] font-mono font-bold uppercase tracking-wide hover:bg-brand-cyan/10' });
    if (!item) {
      box.replaceChildren(h('p', { class: 'text-slate-400', text: 'Hover over a point for its details, or select it to keep them here. Red, orange and yellow points are incidents; blue squares are organization bases; shaded circles are areas.' }));
      return;
    }
    const d = item.d;
    if (item.kind === 'incident') {
      box.replaceChildren(title('Incident'), h('dl', { class: 'mt-1 flex flex-col gap-0.5' },
        line('Incident ID:', d.code),
        line('Type:', d.emergency_type || 'Uncategorized'),
        line('Priority:', d.priority_label),
        line('Location:', `${Number(d.latitude).toFixed(5)}, ${Number(d.longitude).toFixed(5)} · ${d.area_label}`),
        line('Reported:', fullFmt.format(new Date(d.created_at)))),
        action(d.archived ? 'Open in Incident Report' : 'Open in Dispatch Center', () => openIncident(d)));
    } else if (item.kind === 'area') {
      box.replaceChildren(title(d.level === 'high' ? 'High-risk area' : 'Risk area', d.level === 'high' ? 'text-red-300' : 'text-brand-cyan'),
        h('dl', { class: 'mt-1 flex flex-col gap-0.5' },
          line('Area:', d.label),
          line('Risk Level:', levelBadge(d)),
          line('Historical Incidents:', num.format(d.count)),
          line('Most Common Incident:', `${d.top_category.label} (${num.format(d.top_category.count)})`),
          line('Trend:', trendLine(d, 'text-xs')),
          line('Nearby Organizations:', d.nearest.length
            ? h('ul', {}, ...d.nearest.map((o) => h('li', { title: o.name, text: `${o.org_type_label} — ${km(o.distance_km)}${o.available ? '' : ` (${o.status_label})`}` })))
            : 'No verified organization base is registered.')),
        action('View details', () => openArea(d.id)));
    } else {
      box.replaceChildren(title('Organization base', 'text-blue-300'), h('dl', { class: 'mt-1 flex flex-col gap-0.5' },
        line('Organization:', d.name),
        line('Type:', d.base_label),
        line('Status:', d.status_label),
        line('From radar centre:', km(d.center_km))),
        h('p', { class: 'mt-1 text-[0.625rem] text-slate-400', text: 'The registered base location. No responder or vehicle is tracked.' }));
    }
  }

  function renderRadarSummary() {
    const sum = risk.data.radar.summary;
    const t = risk.data.totals;
    const ungraded = 'Not enough historical data to grade areas in this range.';
    const line = (label, value, sub, title) => h('div', { class: 'py-1.5', title },
      h('dt', { class: 'text-[0.625rem] font-mono uppercase tracking-wider text-slate-400', text: label }),
      h('dd', { class: 'font-semibold text-white break-words' }, value, sub && h('span', { class: 'block text-[0.6875rem] font-normal text-slate-400', text: sub })));
    const count = (v) => (v == null ? '—' : num.format(v));
    const cover = sum.highest?.coverage;
    $('an-radar-summary').replaceChildren(
      line('High-Risk Areas', count(sum.high_areas), null, sum.high_areas == null ? ungraded : 'Areas graded High in this range.'),
      line('Active Risk Zones', count(sum.active_zones), null, sum.active_zones == null ? ungraded : 'High or Moderate areas with at least one incident that is not yet resolved or cancelled.'),
      line('Historical Incidents', num.format(t.incidents), t.unlocated ? `${num.format(t.located)} with GPS on the radar` : null, 'Incidents reported in this range that match the filters.'),
      line('Most Common', sum.most_common ? sum.most_common.label : '—', sum.most_common && `${plural(sum.most_common.count, 'incident')}`),
      line('Highest Risk Area', sum.highest ? sum.highest.label : '—', sum.highest && `${sum.highest.level_label} · ${plural(sum.highest.count, 'incident')}`, sum.highest ? 'The area with the highest risk score.' : ungraded),
      line('Nearby Organization Coverage', cover ? cover.level_label : '—',
        cover && `${num.format(cover.available)} of ${plural(cover.within, 'base')} available within ${cover.radius_km} km of the highest risk area`));
  }

  // High areas whose nearby coverage looks thin. A prompt to review, nothing more: no organization is deployed.
  function renderGaps(shown) {
    const nearestOf = (a, kind, label) => {
      const o = a.nearest.find((x) => x.org_type === kind);
      return h('div', { class: 'grid grid-cols-[9.5rem_minmax(0,1fr)] gap-x-2' },
        h('dt', { class: 'text-amber-200/80', text: o?.available ? `Nearest available ${label}:` : `Nearest ${label}:` }),
        h('dd', { class: 'font-semibold', text: !o ? 'None registered' : o.available ? km(o.distance_km) : `${km(o.distance_km)}, none available (${o.status_label})` }));
    };
    $('an-radar-gaps').replaceChildren(...shown.filter((a) => a.coverage.potential_gap).map((a) => h('div', {
      class: 'rounded-lg border border-amber-500/50 bg-amber-950/40 px-3 py-2 text-xs text-amber-100', role: 'status' },
      h('p', { class: 'flex items-center gap-1 font-mono font-bold uppercase tracking-widest text-[0.6875rem]' }, icon('warning', 'text-[1rem]'), 'Potential resource gap'),
      h('p', { class: 'mt-1 font-semibold text-white', text: a.label }),
      h('p', { text: `High historical incident concentration: ${plural(a.count, 'incident')}. ${num.format(a.coverage.available)} of ${plural(a.coverage.within, 'base')} available within ${a.coverage.radius_km} km.` }),
      h('dl', { class: 'mt-1 flex flex-col gap-0.5' }, nearestOf(a, 'ems', 'EMS'), nearestOf(a, 'fire', 'Fire'), nearestOf(a, 'police', 'Police')),
      h('p', { class: 'mt-1' }, h('b', { text: 'Recommendation: ' }), 'Review emergency organization coverage for this area.'),
      h('p', { class: 'text-[0.625rem] text-amber-200/70', text: 'Analytical recommendation only. No organization is deployed automatically.' }))));
  }

  // The same areas as text, for the keyboard and screen readers.
  function renderRiskList(shown) {
    const visible = risk.showAll ? shown : shown.slice(0, RISK_TOP);
    const items = visible.map((a) => h('li', {}, h('button', {
      type: 'button', onclick: () => openArea(a.id), 'aria-label': `Open ${a.label}: ${plural(a.count, 'incident')}, ${a.level_label}`,
      class: 'w-full flex items-start gap-3 px-3 py-2 rounded-lg border border-brand-borderSubtle bg-[#0a1020] text-left hover:border-brand-cyan/50 hover:bg-brand-cyan/5 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan',
    },
      h('span', { class: 'mt-0.5 flex items-center gap-1.5 shrink-0' },
        h('span', { class: `w-2.5 h-2.5 rounded-full ${RISK_LOOK[a.level][0]}`, 'aria-hidden': 'true' }),
        h('span', { class: 'w-4 text-sm font-mono font-bold text-slate-300 tabular-nums', text: String(a.rank) })),
      h('span', { class: 'min-w-0 flex-1' },
        h('span', { class: 'flex flex-wrap items-center gap-x-2 gap-y-0.5' },
          h('span', { class: 'text-sm font-semibold text-white break-words', text: a.label }), levelBadge(a)),
        h('span', { class: 'block text-xs text-slate-300' }, h('b', { class: 'tabular-nums', text: plural(a.count, 'incident') }),
          a.critical ? ` · ${num.format(a.critical)} critical` : '', ` · score ${a.score.toFixed(1)}`),
        h('span', { class: 'block text-[0.6875rem] text-slate-400', text: `Most common: ${a.top_category.label} (${num.format(a.top_category.count)})` }),
        h('span', { class: 'block' }, trendLine(a))),
      icon('chevron_right', 'text-[1.125rem] text-slate-500 self-center'))));
    if (!items.length) {
      items.push(h('li', { class: 'py-4 text-center text-sm text-slate-400', text: risk.data.areas.length ? 'No area is at this risk level.' : 'No area to show.' }));
    } else if (shown.length > RISK_TOP) {
      items.push(h('li', {}, h('button', { type: 'button', class: 'text-xs text-brand-cyan underline', 'aria-expanded': String(risk.showAll),
        onclick: () => { risk.showAll = !risk.showAll; renderRiskList(shownAreas()); }, text: risk.showAll ? `Show the top ${RISK_TOP} only` : `Show all ${num.format(shown.length)} areas` })));
    }
    $('an-risk-list').replaceChildren(...items);
  }

  $('an-risk-filters').addEventListener('change', (e) => {
    if (e.target.name === 'range') {   // one period for the whole tab: hand over to the header's date range
      rangeSelect.value = riskFilters.range.value;
      rangeSelect.dispatchEvent(new Event('change'));
      return;
    }
    if (e.target.name === 'level') {
      Object.assign(risk, { level: riskFilters.level.value, showAll: false });
      if (risk.data) renderRisk();
      return;
    }
    Object.assign(risk, { category: riskFilters.category.value, priority: riskFilters.priority.value, showAll: false });
    loadRisk();
  });
  $('an-risk-filters').addEventListener('submit', (e) => e.preventDefault());
  $('an-radar').replaceChildren(radarGrid(null));
  renderReadout(null);

  // ---- one area in full

  const fact = (label, ...value) => h('div', { class: 'rounded-lg border border-brand-borderSubtle bg-[#0a1020] px-3 py-2' },
    h('dt', { class: 'text-[0.625rem] font-mono uppercase tracking-wider text-slate-400', text: label }),
    h('dd', { class: 'mt-0.5 text-sm font-semibold text-white' }, ...value));
  const subhead = (text, glyph) => h('h3', { class: 'mt-4 mb-1.5 flex items-center gap-1.5 text-[0.6875rem] font-mono font-bold uppercase tracking-widest text-brand-cyan' },
    icon(glyph, 'text-[0.9375rem]'), text);

  async function openArea(id) {
    const box = $('an-risk-detail');
    box.replaceChildren(h('p', { class: 'py-10 text-center text-sm text-slate-400', role: 'status', text: 'Loading area…' }));
    if (!riskDialog.open) riskDialog.showModal();
    const p = riskParams();
    p.set('id', id);
    try {
      renderArea(await api(`${cfg.api.riskArea}?${p}`));
    } catch (err) {
      box.replaceChildren(h('p', { class: 'py-6 text-center text-sm text-red-300', role: 'alert', text: err.message }),
        h('div', { class: 'text-center' }, h('button', { type: 'button', class: 'text-xs underline text-slate-300', onclick: () => riskDialog.close(), text: 'Close' })));
    }
  }

  function renderArea(d) {
    const a = d.area;
    const c = d.coverage;
    const max = a.categories[0].count;
    const parts = a.score_parts;
    const orgRow = (o) => h('li', { class: `flex items-center gap-2 rounded-md border border-brand-borderSubtle bg-[#0a1020] px-2.5 py-1.5 ${o.within ? '' : 'opacity-60'}` },
      h('span', { class: 'esrms-org-dot', 'aria-hidden': 'true', text: ORG_LETTER[o.org_type] || 'O' }),
      h('span', { class: 'min-w-0 flex-1' },
        h('span', { class: 'block text-xs font-semibold text-white truncate', text: o.name }),
        h('span', { class: 'block text-[0.625rem] font-mono uppercase text-slate-400', text: `${o.base_label} · ${o.status_label}${o.within ? '' : ` · beyond ${c.radius_km} km`}` })),
      h('b', { class: 'shrink-0 font-mono text-xs text-slate-100 tabular-nums', text: `${o.distance_km.toFixed(1)} km` }));

    $('an-risk-detail').replaceChildren(
      h('div', { class: 'flex items-start justify-between gap-3' },
        h('div', { class: 'min-w-0' },
          h('p', { class: 'text-[0.6875rem] font-mono font-bold uppercase tracking-widest text-brand-cyan', text: a.level === 'high' ? 'High-risk area' : 'Area detail' }),
          h('h2', { id: 'an-risk-dialog-title', class: 'text-lg font-bold text-white break-words', text: a.label }),
          h('p', { class: 'text-[0.6875rem] font-mono text-slate-400', text: `Centre ${a.center.latitude.toFixed(5)}, ${a.center.longitude.toFixed(5)} · ${a.cell_km} km area · ${fmtDay(d.period.from)} – ${fmtDay(d.period.to)}` })),
        h('button', { type: 'button', class: 'shrink-0 p-1 rounded text-slate-400 hover:text-white hover:bg-white/10', 'aria-label': 'Close', onclick: () => riskDialog.close() }, icon('close', 'text-[1.25rem]'))),
      d.reason && h('p', { class: 'mt-3 flex items-start gap-1.5 rounded-md border border-amber-500/40 bg-amber-950/30 px-2.5 py-2 text-xs text-amber-100' },
        icon('info', 'text-[1rem] shrink-0'), d.reason),

      h('dl', { class: 'mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2' },
        fact('Risk level', levelBadge(a)),
        fact('Total incidents', num.format(a.count), a.critical ? h('span', { class: 'block text-[0.6875rem] font-normal text-slate-400', text: `${num.format(a.critical)} critical (P1)` }) : null),
        fact('Most common', a.top_category.label),
        fact('Trend', trendLine(a, 'text-sm'))),
      h('p', { class: 'mt-1.5 text-[0.6875rem] text-slate-400', title: risk.data?.method.score || '',
        text: `Risk score ${a.score.toFixed(1)} = ${parts.frequency} frequency + ${parts.severity} severity + ${parts.recency} recency · ${a.concentration.toFixed(2)}× the average area` }),

      subhead('Incident breakdown', 'category'),
      h('ul', { class: 'flex flex-col gap-1' }, ...a.categories.map((cat) => h('li', { class: 'grid grid-cols-[minmax(0,1fr)_2.5rem_3rem] items-center gap-x-2 text-sm' },
        h('span', { class: 'min-w-0' },
          h('span', { class: 'block truncate text-slate-200', text: cat.label }),
          h('span', { class: 'block mt-0.5 h-1.5 rounded-full bg-slate-800/80 overflow-hidden' },
            h('span', { class: 'block h-full rounded-full', style: `width:${Math.max(2, (cat.count / max) * 100)}%;background:${categoryLook(cat.label)[2]}` }))),
        h('span', { class: 'text-right font-bold text-white tabular-nums', text: num.format(cat.count) }),
        h('span', { class: 'text-right text-xs text-slate-400 tabular-nums', text: pctText(cat.pct) })))),

      subhead('Recent activity', 'assignment'),
      h('ul', { class: 'flex flex-col divide-y divide-brand-borderSubtle/60' }, ...d.recent.map((i) => h('li', {}, h('button', {
        type: 'button', class: 'w-full flex flex-wrap items-center gap-x-3 gap-y-0.5 py-1.5 text-left text-xs hover:bg-brand-cyan/5',
        title: i.archived ? 'Open in Incident Report' : 'Open in Dispatch Center', onclick: () => { riskDialog.close(); openIncident(i); },
      },
        h('span', { class: 'font-mono text-brand-cyan', text: i.code }),
        i.priority === 1 && h('span', { class: 'px-1 rounded text-[0.625rem] font-mono font-bold bg-red-700 text-white', text: 'P1' }),
        h('span', { class: 'min-w-0 flex-1 truncate text-slate-200', text: i.emergency_type }),
        h('span', { class: `px-1.5 py-px rounded-full border text-[0.625rem] font-semibold ${STATUS_STYLE[i.status] || ''}`, text: cfg.statuses?.[i.status] || i.status }),
        h('span', { class: 'text-slate-400 whitespace-nowrap', text: dateFmt.format(new Date(i.created_at)) }))))),
      a.count > d.recent.length && h('p', { class: 'mt-1 text-[0.6875rem] text-slate-400', text: `Showing the latest ${d.recent.length} of ${num.format(a.count)}.` }),

      subhead('Nearby organizations', 'hub'),
      d.organizations.length
        ? h('ul', { class: 'grid grid-cols-1 sm:grid-cols-2 gap-1.5' }, ...d.organizations.map(orgRow))
        : h('p', { class: 'text-xs text-slate-400', text: 'No verified organization base with coordinates is registered yet.' }),
      h('p', { class: 'mt-1 text-[0.6875rem] text-slate-400', text: 'Straight-line distance from the area centre to each organization\'s fixed base. No responder is located or tracked.' }),

      subhead('Resource coverage', 'shield'),
      h('dl', { class: 'grid grid-cols-1 sm:grid-cols-3 gap-2' },
        fact('Incident concentration', a.level === 'insufficient' ? 'Insufficient data' : a.level_label),
        fact(`Available organizations within ${c.radius_km} km`, `${num.format(c.available)} of ${num.format(c.within)} nearby`),
        fact('Potential coverage', c.level_label)),
      c.potential_gap && h('p', { class: 'mt-2 flex items-start gap-1.5 rounded-md border border-amber-500/50 bg-amber-950/40 px-2.5 py-2 text-xs text-amber-100', role: 'status' },
        icon('warning', 'text-[1rem] shrink-0'),
        h('span', {}, h('b', { text: 'Potential Resource Gap. ' }),
          c.missing_types.length ? `No available ${c.missing_types.join(' or ')} base within ${c.radius_km} km, although ${a.top_category.label} is the most common emergency here.`
            : `Only ${num.format(c.available)} available organization base${c.available === 1 ? '' : 's'} within ${c.radius_km} km of a high-concentration area.`)),

      d.recommendations.length > 0 && subhead('Preparedness recommendation', 'fact_check'),
      d.recommendations.length > 0 && h('ul', { class: 'list-disc pl-5 text-xs text-slate-200 space-y-1' }, ...d.recommendations.map((text) => h('li', { text }))),
      d.recommendations.length > 0 && h('p', { class: 'mt-1.5 text-[0.6875rem] text-slate-400', text: 'Recommendations only. Nothing is dispatched or moved automatically; final decisions remain with authorized personnel.' }),

      h('div', { class: 'mt-5 flex flex-wrap justify-end gap-2' },
        h('button', { type: 'button', class: 'px-4 py-2 rounded-md text-sm text-slate-300 hover:bg-white/10', onclick: () => riskDialog.close(), text: 'Close' }),
        h('button', {
          type: 'button', disabled: !window.ESRMSDispatch?.map, onclick: () => showAreaOnMap(d),
          class: 'inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-semibold bg-gradient-to-r from-[#00D9FF] to-[#1685FF] hover:brightness-110 text-[#050A12] disabled:opacity-50 disabled:pointer-events-none',
        }, icon('map', 'text-[1.125rem]'), 'View on Map')));
  }

  // ---- the existing Dispatch Center map: where past incidents were reported. Historical points and fixed bases only.

  let riskLayer = null;
  let riskControl = null;

  function clearRiskMap() {
    const map = window.ESRMSDispatch?.map;
    if (map && riskLayer) map.removeLayer(riskLayer);
    if (map && riskControl) map.removeControl(riskControl);
    riskLayer = riskControl = null;
  }

  // `draw(layer)` adds the overlay and returns the bounds to show.
  function showRiskMap(draw) {
    const map = window.ESRMSDispatch?.map;
    if (!map || !window.L) return toast('The map is not available on this page.', 'error');
    if (riskDialog.open) riskDialog.close();
    clearRiskMap();
    document.querySelector('[data-tab="dispatch"]')?.click();
    riskLayer = L.layerGroup().addTo(map);
    const bounds = draw(riskLayer);
    const Clear = L.Control.extend({
      options: { position: 'topleft' },
      onAdd() {
        const btn = L.DomUtil.create('button', 'esrms-map-btn');
        btn.type = 'button';
        btn.title = 'Remove the historical risk overlay from the map';
        btn.append(icon('layers_clear', 'text-[0.875rem]'), 'CLEAR RISK OVERLAY');
        L.DomEvent.disableClickPropagation(btn);
        L.DomEvent.on(btn, 'click', clearRiskMap);
        return btn;
      },
    });
    riskControl = new Clear().addTo(map);
    // After the tab is visible and the map has its size back.
    setTimeout(() => { map.invalidateSize(); map.flyToBounds(bounds, { padding: [50, 50], maxZoom: 16, duration: 0.8 }); }, 80);
    return undefined;
  }

  function zone(area, radiusKm) {
    const at = [area.center.latitude, area.center.longitude];
    return L.circle(at, { radius: Math.max(150, radiusKm * 1000), color: RISK_LOOK[area.level][2], weight: 1.5, dashArray: '5 5', fillColor: RISK_LOOK[area.level][2], fillOpacity: 0.14 });
  }

  function showAreaOnMap(d) {
    showRiskMap((layer) => {
      const a = d.area;
      const circle = zone(a, d.radius_km).addTo(layer)
        .bindTooltip(`${a.label} · ${plural(a.count, 'historical incident')} · ${a.level === 'insufficient' ? 'insufficient data' : `${a.level_label} risk`}`, { sticky: true });
      for (const p of d.points) {
        L.circleMarker([p.latitude, p.longitude], { radius: 5, color: '#7f1d1d', weight: 1.5, fillColor: '#ef4444', fillOpacity: 0.8, interactive: false }).addTo(layer);
      }
      const bounds = circle.getBounds();
      for (const o of d.organizations.filter((x) => x.within)) {
        L.marker([o.latitude, o.longitude], {
          icon: L.divIcon({ className: '', html: `<span class="esrms-org-dot esrms-org-dot--map${o.available ? '' : ' esrms-org-dot--dim'}">${ORG_LETTER[o.org_type] || 'O'}</span>`, iconSize: [22, 22], iconAnchor: [11, 11] }),
          keyboard: false,
        }).bindTooltip(`${o.name} · ${o.base_label} · ${o.status_label} · ${o.distance_km.toFixed(1)} km from the area centre`).addTo(layer);
        bounds.extend([o.latitude, o.longitude]);
      }
      return bounds;
    });
  }

  $('an-risk-map').addEventListener('click', () => {
    if (!risk.data || !shownAreas().length) return;
    const areas = shownAreas();
    showRiskMap((layer) => {
      let bounds = null;
      for (const a of areas) {
        const circle = zone(a, a.cell_km * 0.6).addTo(layer)
          .bindTooltip(`${a.rank}. ${a.label} · ${plural(a.count, 'historical incident')} · ${a.level === 'insufficient' ? 'insufficient data' : `${a.level_label} risk`}`, { sticky: true })
          .on('click', () => openArea(a.id));
        bounds = bounds ? bounds.extend(circle.getBounds()) : circle.getBounds();
      }
      return bounds;
    });
  });

  // ------------------------------------------------------------ date range

  const rangeSelect = $('an-range');
  const customForm = $('an-custom');

  function showCustomError(text) {
    $('an-custom-error').textContent = text;
    $('an-custom-error').classList.toggle('hidden', !text);
  }

  function changePeriod() {
    state.page = 1;
    loadAll();
  }

  // The radar's Time Range shows the same choice; a range it does not list (Today, This year) is added while selected.
  function syncRiskRange() {
    const select = riskFilters.range;
    select.querySelector('[data-extra]')?.remove();
    if (![...select.options].some((o) => o.value === rangeSelect.value)) {
      select.append(h('option', { value: rangeSelect.value, 'data-extra': true, text: cfg.options.ranges[rangeSelect.value] }));
    }
    select.value = rangeSelect.value;
  }
  syncRiskRange();

  rangeSelect.addEventListener('change', () => {
    const custom = rangeSelect.value === 'custom';
    syncRiskRange();
    customForm.classList.toggle('hidden', !custom);
    if (!custom) {
      state.range = rangeSelect.value;
      changePeriod();
      return;
    }
    // Start from the range on screen; nothing reloads until a valid range is applied.
    showCustomError('');
    if (!customForm.elements.from.value && summary) {
      customForm.elements.from.value = summary.period.from;
      customForm.elements.to.value = summary.period.to;
    }
    customForm.elements.from.focus();
  });

  customForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const from = customForm.elements.from.value;
    const to = customForm.elements.to.value;
    if (!from || !to) return showCustomError('Choose both a start date and an end date.');
    if (from > to) return showCustomError('The start date cannot be later than the end date.');
    showCustomError('');
    Object.assign(state, { range: 'custom', from, to });
    changePeriod();
  });

  // ------------------------------------------------------------ export

  const exportBtn = $('an-export');
  const exportMenu = $('an-export-menu');
  const exportItems = [...exportMenu.querySelectorAll('[data-export]')];
  const exportStatus = $('an-export-status');
  const menuOpen = () => !exportMenu.classList.contains('hidden');
  // Focus moves into the menu when it opens, arrow keys move within it, Escape returns to the button.
  function setExportMenu(open, { focus = false } = {}) {
    exportMenu.classList.toggle('hidden', !open);
    exportBtn.setAttribute('aria-expanded', String(open));
    if (open && focus) exportItems[0].focus();
  }
  exportBtn.addEventListener('click', (e) => { e.stopPropagation(); setExportMenu(!menuOpen(), { focus: true }); });
  document.addEventListener('click', (e) => { if (!exportMenu.contains(e.target)) setExportMenu(false); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !menuOpen()) return;
    setExportMenu(false);
    exportBtn.focus();
  });
  exportMenu.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const at = exportItems.indexOf(document.activeElement);
    exportItems[(at + (e.key === 'ArrowDown' ? 1 : exportItems.length - 1)) % exportItems.length].focus();
  });

  // Stays on screen after the toast has gone: what was exported, and whether it was the whole list.
  function showExportStatus(kind, glyph, text) {
    const tone = { ok: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-100', warn: 'border-amber-500/50 bg-amber-950/40 text-amber-100', error: 'border-red-500/40 bg-red-950/40 text-red-100' }[kind];
    exportStatus.className = `mt-3 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${tone}`;
    exportStatus.replaceChildren(icon(glyph, 'text-[1.125rem] shrink-0'), h('p', { class: 'flex-1', text }),
      h('button', { type: 'button', class: 'shrink-0 rounded hover:bg-white/10', 'aria-label': 'Dismiss export message', onclick: () => exportStatus.classList.add('hidden') }, icon('close', 'text-[1rem]')));
  }

  async function exportReport(format) {
    setExportMenu(false);
    if (rangeSelect.value === 'custom' && state.range !== 'custom') {
      showExportStatus('error', 'error', 'Export not started: apply the custom date range first.');
      return toast('Apply the custom date range first, then export.', 'error');
    }
    const label = exportBtn.querySelector('[data-label]');
    exportBtn.disabled = true;
    exportBtn.classList.add('opacity-60', 'cursor-wait');
    label.textContent = 'Preparing…';
    try {
      const p = filterParams();
      p.set('format', format);
      let res;
      try {
        res = await fetch(`${cfg.api.export}?${p}`, { credentials: 'same-origin' });
      } catch (_) {
        throw new Error('Network error. The report was not exported.');
      }
      if (!res.ok) {
        let message = `The report could not be exported (${res.status}).`;
        try { message = (await res.json()).error || message; } catch (_) { /* non-JSON error page */ }
        throw new Error(message);
      }
      const blob = await res.blob();
      if (!blob.size) throw new Error('The server returned an empty report. Nothing was downloaded.');
      const name = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '')?.[1] || `esrms-analytics.${format}`;
      const link = h('a', { href: URL.createObjectURL(blob), download: name });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      const count = Number(res.headers.get('X-Record-Count'));
      const matching = Number(res.headers.get('X-Record-Total'));
      const periodTotal = Number(res.headers.get('X-Period-Total'));
      const truncated = res.headers.get('X-Export-Truncated') === '1';
      const kind = format.toUpperCase();
      const scope = `Summary figures cover all ${num.format(periodTotal)} incident${periodTotal === 1 ? '' : 's'} reported in the selected range.`;
      if (truncated) {
        showExportStatus('warn', 'warning', `${kind} export is PARTIAL: ${num.format(count)} of ${num.format(matching)} matching incident records were exported (export limit). ${num.format(matching - count)} are not in the file. ${scope} Narrow the date range or filters to export the rest.`);
        toast(`${kind} downloaded, but it is a partial list: ${num.format(count)} of ${num.format(matching)} incident records.`, 'info');
      } else if (!count) {
        showExportStatus('warn', 'info', `${kind} exported with summary figures only: no incident matches the current table filters, so the file lists 0 incident records. ${scope}`);
        toast(`${kind} downloaded with summaries only; no incident records match the filters.`, 'info');
      } else {
        showExportStatus('ok', 'check_circle', `${kind} export complete: all ${num.format(count)} matching incident record${count === 1 ? '' : 's'} exported (${name}). ${scope}`);
        toast(`${kind} report downloaded with all ${num.format(count)} matching incident record${count === 1 ? '' : 's'}.`, 'success');
      }
    } catch (err) {
      showExportStatus('error', 'error', `Export failed, nothing was downloaded: ${err.message}`);
      toast(err.message, 'error');
    } finally {
      exportBtn.disabled = false;
      exportBtn.classList.remove('opacity-60', 'cursor-wait');
      label.textContent = 'Export Report';
    }
  }
  exportMenu.querySelectorAll('[data-export]').forEach((b) => b.addEventListener('click', () => exportReport(b.dataset.export)));

  // ------------------------------------------------------------ wiring

  $('an-refresh').addEventListener('click', () => loadAll());
  $('an-retry').addEventListener('click', () => loadAll());
  $('an-view-all').addEventListener('click', () => document.querySelector('[data-tab="reports"]')?.click());

  const panel = root.closest('[data-tab-panel]');
  const visible = () => !panel || !panel.classList.contains('hidden');
  document.querySelector('[data-tab="analytics"]')?.addEventListener('click', () => {
    if (!loaded || stale) loadAll({ quiet: loaded });
  });

  // Incidents changed (dispatch.js heard the realtime feed): refresh now if on screen, otherwise on next open.
  let changeTimer;
  document.addEventListener('esrms:incidents-changed', () => {
    if (!loaded) return;
    if (!visible()) { stale = true; return; }
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => loadAll({ quiet: true }), 1500);
  });

  renderHead();
  if (visible()) loadAll();
})();
