// Hospital Medical Reception Dashboard: incoming reports, hospital signal, the full report
// viewer with acknowledge / bay / arrival / handoff, completed handoffs, read-only history and
// settings. Every list and every action goes through /api/hospital (Flask), which only ever
// returns the signed-in account's own hospital. Nothing is kept in the browser.
(function () {
  const root = document.getElementById('hospital-portal');
  const cfgEl = document.getElementById('hospital-portal-config');
  if (!root || !cfgEl || !window.AnnUI || !window.HospitalUI) return;
  const { h, icon, toast, api } = window.AnnUI;
  const UI = window.HospitalUI;
  const cfg = JSON.parse(cfgEl.textContent);
  const opt = cfg.options;
  const base = cfg.api.base;
  const $ = (id) => document.getElementById(id);
  const conn = UI.connection($('hp-connection'), 'new patient reports');

  const VIEWS = {
    incoming: { title: 'Incident Reports', glyph: 'assignment', note: 'Acknowledged patients on the way or in handoff. Critical first, then soonest ETA. New reports wait in Hospital Signal.',
      filters: [['', 'All Incoming'], ['critical', 'Critical Priority'], ['stable', 'Stable / Moderate']],
      empty: 'No acknowledged incoming reports. New reports from dispatch arrive in Hospital Signal first.' },
    signals: { title: 'Hospital Signal', glyph: 'sensors', note: 'New emergency signals from EMS / Dispatch. Acknowledge one to move it to Incoming Reports.',
      filters: [], empty: 'No new emergency signals. Signals appear here the moment dispatch sends one.' },
    completed: { title: 'Completed Handoffs', glyph: 'task_alt', note: 'Patients your hospital has received.', filters: [], paged: true,
      empty: 'No completed handoffs yet.' },
    history: { title: 'Patient Report History', glyph: 'history', note: 'Archived reports: completed, unable to receive and cancelled. Read-only.', search: true, paged: true,
      filters: [['', 'All'], ['today', 'Today'], ['week', 'This Week'], ['month', 'This Month'], ['critical', 'Critical'], ['completed', 'Completed']],
      empty: 'No archived patient reports yet.' },
  };
  const state = { view: 'incoming', filter: '', q: '', page: 1, openId: null };
  let overview = null;
  let report = null;          // the report shown in the viewer
  let blocked = false;        // not hospital staff, or the module is not set up
  let knownSignals = null;    // ids already announced
  let listSeq = 0;
  let reportSeq = 0;

  async function call(path, options) {
    try {
      const data = await api(`${base}${path}`, options);
      conn.api(null);
      return data;
    } catch (err) {
      conn.api(err);
      if (err.status === 401) err.message = 'Your session has ended. Sign in again to continue.';
      throw err;
    }
  }

  function message(text, tone = 'error') {
    const el = $('hp-message');
    el.textContent = text || '';
    el.className = `${text ? '' : 'hidden '}mb-3 p-4 rounded-xl border text-sm ${tone === 'error' ? 'border-red-500/40 bg-red-950/40 text-red-200' : 'border-amber-500/40 bg-amber-950/40 text-amber-100'}`;
  }

  // ------------------------------------------------------------ overview: identity, counters, bays

  async function loadOverview() {
    if (blocked) return;
    try {
      overview = await call('/overview');
      renderOverview();
    } catch (err) {
      if (err.status === 403 || err.status === 503 || err.status === 404) {
        blocked = true;
        $('hp-hospital-name').textContent = 'No hospital';
        $('hp-stats').replaceChildren();
        $('hp-list').replaceChildren();
        $('hp-reports').classList.add('hidden');
        message(err.status === 403
          ? 'This account does not belong to a hospital. Register your hospital from the Hospital Portal, or ask your Hospital Administrator to add you as staff.'
          : err.message, 'warn');
      } else {
        message(err.message);
      }
    }
  }

  const STATS = [
    ['incoming', 'Incoming Patients', 'ambulance', 'text-brand-cyan border-brand-cyan/40'],
    ['critical', 'Critical Patients', 'emergency', 'text-red-400 border-red-500/40'],
    ['preparing', 'Preparing', 'bed', 'text-sky-400 border-sky-500/40'],
    ['arrived', 'Patients Arrived', 'airport_shuttle', 'text-amber-400 border-amber-500/40'],
    ['pending_handoffs', 'Pending Handoffs', 'handshake', 'text-violet-300 border-violet-500/40'],
    ['completed_today', 'Completed Today', 'task_alt', 'text-emerald-400 border-emerald-500/40'],
  ];

  // Organizations other than hospitals answer response requests, not patient handoffs.
  const ORG_STATS = [
    ['incoming', 'Acknowledged / Accepted', 'assignment', 'text-brand-cyan border-brand-cyan/40'],
    ['critical', 'Critical Priority', 'emergency', 'text-red-400 border-red-500/40'],
    ['responding', 'Responding', 'directions_run', 'text-amber-400 border-amber-500/40'],
    ['on_scene', 'On Scene', 'pin_drop', 'text-violet-300 border-violet-500/40'],
    ['completed_today', 'Completed Today', 'task_alt', 'text-emerald-400 border-emerald-500/40'],
  ];

  // The same portal for an EMS base, fire station, police station or barangay center: signals and responses, not patients.
  let organizationWording = false;
  function applyOrganizationWording(org) {
    if (organizationWording) return;
    organizationWording = true;
    root.querySelector('h1').textContent = `${org.org_type_label} Emergency Signal Dashboard`;
    Object.assign(VIEWS.incoming, { title: 'Active Responses', note: 'Signals your organization has acknowledged and is working on. New signals wait in Emergency Signals.',
      empty: 'No active responses. New signals from dispatch arrive in Emergency Signals first.' });
    Object.assign(VIEWS.signals, { title: 'Emergency Signals', note: 'New emergency signals from Dispatch. Acknowledge one to move it to Active Responses.' });
    Object.assign(VIEWS.completed, { title: 'Completed Responses', note: 'Responses your organization has completed.', empty: 'No completed responses yet.' });
    Object.assign(VIEWS.history, { title: 'Signal History', note: 'Archived signals: completed, unable to respond and cancelled. Read-only.', empty: 'No archived signals yet.' });
    for (const [view, name] of [['incoming', 'Active Responses'], ['signals', 'Emergency Signals'], ['completed', 'Completed Responses']]) {
      const label = [...root.querySelector(`[data-view="${view}"]`).childNodes].find((n) => n.nodeType === Node.TEXT_NODE && n.nodeValue.trim());
      if (label) label.nodeValue = name;
    }
    if (VIEWS[state.view]) renderListFrame();
  }

  function renderOverview() {
    const { hospital, me, stats } = overview;
    $('hp-hospital-name').textContent = hospital.name;
    if (hospital.org_type !== 'hospital') applyOrganizationWording(hospital);
    $('hp-me').textContent = `· ${me.name || me.email} · ${me.role_label}`;
    const availability = $('hp-availability');
    availability.value = hospital.availability;
    availability.disabled = false;
    $('hp-stats').replaceChildren(...(hospital.org_type === 'hospital' ? STATS : ORG_STATS).map(([key, label, glyph, tone]) => h('div', { class: `rounded-xl bg-[#0b1220] border ${tone.split(' ')[1]} px-3 py-3 flex items-center gap-3` },
      icon(glyph, `text-[1.625rem] ${tone.split(' ')[0]}`),
      h('div', { class: 'min-w-0' },
        h('p', { class: 'text-2xl font-bold text-white leading-none tabular-nums', text: String(stats[key]) }),
        h('p', { class: 'text-[0.6875rem] text-slate-300 mt-1 truncate', text: label })))));
    if (!hospital.verified) {
      message('Your hospital is registered but not verified yet. ESRMS dispatch has to verify it before patient reports can be sent to you. You can set up bays and staff in Settings meanwhile.', 'warn');
    } else if (!blocked) {
      message('');
    }
    if (state.view === 'settings') renderSettings();
    if (report && !viewer.contains(document.activeElement)) renderReport();   // the bay board may have changed
  }

  $('hp-availability').addEventListener('change', async (e) => {
    const select = e.currentTarget;
    select.disabled = true;
    try {
      const { item } = await call('/availability', { method: 'POST', body: { availability: select.value } });
      toast(`${item.name} is now shown to dispatchers as ${item.availability_label}.`, 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
    loadOverview();
  });

  // ------------------------------------------------------------ report lists

  function renderListFrame() {
    const v = VIEWS[state.view];
    $('hp-list-title').replaceChildren(icon(v.glyph, 'text-brand-cyan text-[1.25rem]'), v.title);
    $('hp-list-note').textContent = v.note;
    $('hp-search').classList.toggle('hidden', !v.search);
    $('hp-filters').replaceChildren(...v.filters.map(([key, text]) => h('button', {
      type: 'button', role: 'tab', 'aria-selected': String(state.filter === key),
      class: 'px-2.5 py-1 rounded border border-brand-borderSubtle text-[0.6875rem] font-mono text-slate-300 hover:text-white aria-selected:text-brand-cyan aria-selected:border-brand-cyan/60 aria-selected:bg-brand-cyan/10',
      onclick: () => { state.filter = key; state.page = 1; renderListFrame(); loadList(); },
    }, text.toUpperCase())));
  }

  async function loadList() {
    if (blocked || state.view === 'settings') return;
    const seq = ++listSeq;
    const v = VIEWS[state.view];
    const p = new URLSearchParams({ view: state.view, page: state.page });
    if (state.filter) p.set('filter', state.filter);
    if (v.search && state.q) p.set('q', state.q);
    $('hp-list').setAttribute('aria-busy', 'true');
    try {
      const data = await call(`/reports?${p}`);
      if (seq !== listSeq) return;
      state.page = data.page;
      renderList(data);
    } catch (err) {
      if (seq !== listSeq) return;
      $('hp-list').replaceChildren(h('div', { class: 'py-8 text-center' },
        h('p', { class: 'text-sm text-red-300', role: 'alert', text: err.message }),
        h('button', { type: 'button', class: 'mt-1 text-xs underline text-red-200', onclick: loadList, text: 'Retry' })));
      $('hp-pager').replaceChildren();
    } finally {
      if (seq === listSeq) $('hp-list').setAttribute('aria-busy', 'false');
    }
  }

  function card(s) {
    const selected = s.id === state.openId;
    const signal = state.view === 'signals';
    const done = s.completed_at || s.received_at;
    const response = s.kind === 'response';
    const body = [
      h('div', { class: 'flex flex-wrap items-center gap-1.5' },
        h('span', { class: 'text-xs font-bold text-white uppercase tracking-wide', text: s.medic_unit || s.signal_type_label }),
        UI.triageBadge(s),
        h('span', { class: 'ml-auto' }, UI.statusBadge(s))),
      h('p', { class: 'mt-1 text-sm font-semibold text-white break-words', text: response ? (s.incident ? s.incident.emergency_type : 'Emergency response requested') : s.chief_complaint || `${s.condition_label} patient` }),
      h('p', { class: 'text-xs text-slate-300', text: response ? (s.distance_km != null ? `${s.distance_km.toFixed(1)} km from your base` : 'Incident location unavailable') : UI.patientLine(s) }),
      s.incident && !response && h('p', { class: 'text-xs text-slate-400 truncate', text: s.incident.emergency_type }),
      h('div', { class: 'mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[0.6875rem] font-mono' },
        h('span', { class: 'text-brand-cyan', text: s.code }),
        s.incident && h('span', { class: 'text-slate-300', text: s.incident.code }),
        s.bay_name && h('span', { class: 'text-slate-300', text: s.bay_name }),
        h('span', { class: 'ml-auto text-slate-200', title: s.eta ? UI.fmtFull(s.eta) : '', text: s.read_only ? (done ? `Completed ${UI.fmtWhen(done)}` : `Closed ${UI.fmtWhen(s.updated_at)}`) : response ? `Sent ${UI.fmtWhen(s.sent_at)}` : UI.etaText(s.eta) })),
    ];
    const open = () => openReport(s.id);
    if (!signal) {
      return h('button', {
        type: 'button', 'aria-pressed': String(selected), 'data-report': s.id, onclick: open,
        class: `w-full text-left rounded-lg border-l-4 border p-3 transition ${s.critical && !s.read_only ? 'border-l-red-500' : 'border-l-brand-cyan/60'} ${selected ? 'border-brand-cyan bg-brand-cyan/10' : 'border-brand-borderSubtle bg-[#111827]/60 hover:border-brand-cyan/50'}`,
      }, ...body);
    }
    // Hospital Signal: the same report, with the quick actions on the card.
    return h('article', { class: `rounded-lg border-l-4 border p-3 ${s.critical ? 'border-l-red-500' : 'border-l-brand-cyan'} border-brand-cyan/50 bg-brand-cyan/5`, 'aria-label': `New emergency signal ${s.code}` },
      h('p', { class: 'text-[0.625rem] font-mono font-bold tracking-widest text-red-300 mb-1', text: 'NEW EMERGENCY SIGNAL' }),
      ...body,
      h('div', { class: 'mt-2.5 flex flex-wrap gap-2' },
        h('button', { type: 'button', class: 'px-3 py-1.5 rounded-md text-xs font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10', onclick: open }, 'Open Report'),
        h('button', { type: 'button', class: 'px-3 py-1.5 rounded-md text-xs font-bold bg-gradient-to-r from-[#00D9FF] to-[#1685FF] text-[#050A12] hover:brightness-110', onclick: (e) => act(s, 'acknowledge', e.currentTarget) }, 'Acknowledge'),
        !response && h('button', { type: 'button', class: 'px-3 py-1.5 rounded-md text-xs font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10', onclick: () => openReport(s.id, { focusBay: true }) }, 'Prepare Bay')));
  }

  function renderList(data) {
    const v = VIEWS[state.view];
    if (!data.items.length) {
      const filtered = Boolean(state.filter || (v.search && state.q));
      $('hp-list').replaceChildren(h('div', { class: 'py-10 text-center text-slate-400' }, icon('inbox', 'text-[2rem]'),
        h('p', { class: 'text-sm mt-1', text: filtered ? 'No reports match this search or filter.' : v.empty })));
    } else {
      $('hp-list').replaceChildren(...data.items.map(card));
    }
    if (!v.paged || !data.total) { $('hp-pager').replaceChildren(v.paged ? '' : h('span', { text: data.total ? `${data.total} report${data.total === 1 ? '' : 's'}` : '' })); return; }
    const first = (data.page - 1) * data.per_page + 1;
    const pageBtn = (label, glyph, page, disabled) => h('button', {
      type: 'button', disabled, 'aria-label': label,
      class: 'p-1 rounded border border-brand-borderSubtle text-slate-300 hover:text-brand-cyan disabled:opacity-30 disabled:pointer-events-none',
      onclick: () => { state.page = page; loadList(); },
    }, icon(glyph, 'text-[1.125rem] align-middle'));
    $('hp-pager').replaceChildren(
      h('span', { text: `Showing ${first}–${first + data.items.length - 1} of ${data.total}` }),
      h('span', { class: 'flex items-center gap-2' }, pageBtn('Previous page', 'chevron_left', data.page - 1, data.page <= 1),
        h('span', { class: 'tabular-nums', text: `Page ${data.page} of ${data.pages}` }), pageBtn('Next page', 'chevron_right', data.page + 1, data.page >= data.pages)));
  }

  let searchTimer;
  $('hp-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; loadList(); }, 350);
  });
  $('hp-search').addEventListener('submit', (e) => e.preventDefault());
  $('hp-refresh').addEventListener('click', () => { loadOverview(); loadList(); if (state.openId) loadReport(); });

  // New signals: badge on the tab and one alert per report, whichever tab is open.
  async function checkSignals() {
    if (blocked) return;
    try {
      const { items } = await call('/reports?view=signals');
      const badge = $('hp-signal-badge');
      badge.textContent = items.length ? String(items.length) : '';
      badge.classList.toggle('hidden', !items.length);
      if (knownSignals) {
        for (const s of items.filter((x) => !knownSignals.has(x.id)).slice(0, 3)) {
          const title = s.kind === 'response' ? 'NEW EMERGENCY SIGNAL' : 'NEW INCOMING PATIENT';
          const body = (s.kind === 'response' ? [s.code, s.signal_type_label, s.triage_label, s.incident && s.incident.emergency_type]
            : [s.code, s.medic_unit, s.triage_label || s.condition_label, UI.etaText(s.eta)]).filter(Boolean).join(' · ');
          if (window.ESRMSNotify) window.ESRMSNotify.notify('dispatch_updates', title, body, { kind: s.critical ? 'error' : 'info', tag: `hs-${s.id}` });
          else toast(`${title}: ${body}`, s.critical ? 'error' : 'info');
        }
      }
      knownSignals = new Set([...(knownSignals || []), ...items.map((s) => s.id)]);
    } catch (_) { /* the visible list reports load problems */ }
  }

  // ------------------------------------------------------------ report viewer

  const viewer = $('hp-viewer');

  // Back to the empty viewer. Nothing is changed on the server; the report stays in its list.
  function closeReport() {
    state.openId = null;
    report = null;
    reportSeq += 1;   // a report still loading must not reopen the viewer
    viewer.replaceChildren(h('div', { class: 'p-10 text-center text-slate-400' },
      icon('assignment', 'text-[2.5rem]'), h('p', { class: 'text-sm mt-1', text: 'Select a report to open it here.' })));
    root.querySelectorAll('[data-report]').forEach((el) => el.setAttribute('aria-pressed', 'false'));
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && state.openId && !root.querySelector('dialog[open]')) closeReport();
  });

  async function openReport(id, { focusBay = false } = {}) {
    state.openId = id;
    report = null;
    viewer.replaceChildren(h('p', { class: 'p-10 text-center text-sm text-slate-400', text: 'Loading report…' }));
    root.querySelectorAll('[data-report]').forEach((el) => el.setAttribute('aria-pressed', String(el.dataset.report === id)));
    await loadReport();
    if (window.matchMedia('(max-width: 1023px)').matches) viewer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (focusBay) $('hp-bay-select')?.focus();
  }

  async function loadReport() {
    const id = state.openId;
    const seq = ++reportSeq;
    try {
      const { item } = await call(`/reports/${id}`);
      if (seq !== reportSeq || state.openId !== id) return;
      report = item;
      renderReport();
    } catch (err) {
      if (seq !== reportSeq || state.openId !== id) return;
      report = null;
      viewer.replaceChildren(h('p', { class: 'p-10 text-center text-sm text-red-300', role: 'alert', text: err.message }));
    }
  }

  const section = (title, glyph, ...content) => h('section', { class: 'px-4 py-3 border-t border-brand-borderSubtle' },
    h('h3', { class: 'flex items-center gap-1.5 text-[0.6875rem] font-mono font-bold uppercase tracking-widest text-brand-cyan mb-2' }, icon(glyph, 'text-[0.9375rem]'), title), ...content);
  const facts = (...pairs) => h('dl', { class: 'grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-2' }, ...pairs.filter(Boolean).map(([label, value, cls]) => h('div', { class: 'min-w-0' },
    h('dt', { class: 'text-[0.625rem] font-mono uppercase tracking-wide text-slate-400', text: label }),
    h('dd', { class: `text-sm break-words ${value == null || value === '' ? 'text-slate-400' : `text-slate-100 ${cls || ''}`}` }, value == null || value === '' ? 'Not provided' : value))));

  const ACTION_BUTTONS = {
    acknowledge: ['Acknowledge Report & Prepare Bay', 'visibility', 'bg-gradient-to-r from-[#00D9FF] to-[#1685FF] text-[#050A12] hover:brightness-110'],
    arrive: ['Patient Arrived', 'airport_shuttle', 'border border-amber-500/60 text-amber-300 hover:bg-amber-500/10'],
    handoff: ['Start Handoff', 'handshake', 'bg-gradient-to-r from-[#00D9FF] to-[#1685FF] text-[#050A12] hover:brightness-110'],
    complete: ['Complete Handoff', 'task_alt', 'bg-emerald-600 text-white hover:bg-emerald-500'],
    unable: ['Unable to Receive', 'block', 'border border-red-500/60 text-red-300 hover:bg-red-500/10'],
  };

  // A response request to an organization: acknowledge, accept, responding, on scene, completed.
  const RESPONSE_BUTTONS = {
    acknowledge: ['Acknowledge', 'visibility', 'bg-gradient-to-r from-[#00D9FF] to-[#1685FF] text-[#050A12] hover:brightness-110'],
    accept: ['Accept', 'thumb_up', 'border border-brand-cyan/60 text-brand-cyan hover:bg-brand-cyan/10'],
    respond: ['Responding', 'directions_run', 'border border-amber-500/60 text-amber-300 hover:bg-amber-500/10'],
    on_scene: ['On Scene', 'pin_drop', 'border border-violet-500/60 text-violet-300 hover:bg-violet-500/10'],
    complete: ['Completed', 'task_alt', 'bg-emerald-600 text-white hover:bg-emerald-500'],
    unable: ['Unable to Respond', 'block', 'border border-red-500/60 text-red-300 hover:bg-red-500/10'],
  };

  function baySection(s) {
    if (s.read_only) return facts(['Trauma Bay / Room', s.bay_name], ['Receiving Team', s.receiving_team]);
    const bays = overview?.bays || [];
    const select = h('select', { id: 'hp-bay-select', class: 'w-full px-3 py-2 rounded-lg bg-[#0a1020] border border-brand-border text-sm text-slate-100', 'aria-label': 'Trauma bay or room' },
      h('option', { value: '', text: bays.length ? 'Select a bay' : 'No bays set up (Settings)' }),
      ...bays.map((b) => {
        const mine = b.id === s.bay_id;
        return h('option', { value: b.id, selected: mine, disabled: !mine && !b.assignable, text: `${b.name} — ${mine ? 'assigned to this patient' : b.status_label.toUpperCase()}${!mine && b.report_code ? ` (${b.report_code})` : ''}` });
      }));
    const team = h('input', { id: 'hp-bay-team', maxlength: '120', value: s.receiving_team || '', placeholder: 'e.g. Trauma Team A', 'aria-label': 'Receiving team', class: 'w-full px-3 py-2 rounded-lg bg-[#0a1020] border border-brand-border text-sm text-slate-100 placeholder-slate-400' });
    return h('div', {},
      h('div', { class: 'grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2 items-end' },
        h('label', { class: 'text-xs font-semibold text-slate-200' }, 'Trauma Bay / Room', select),
        h('label', { class: 'text-xs font-semibold text-slate-200' }, 'Receiving Team', team),
        h('button', { type: 'button', class: 'px-3.5 py-2 rounded-lg text-sm font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10', onclick: (e) => assignBay(s, select.value, team.value, e.currentTarget) }, s.bay_id ? 'Update' : 'Assign Bay')),
      h('div', { class: 'mt-2 flex flex-wrap gap-1.5' }, ...bays.map((b) => h('span', { class: `inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[0.625rem] font-mono ${BAY_STYLE[b.status]}`, title: b.report_code ? `Assigned to ${b.report_code}` : '' }, b.name, ' · ', b.status_label.toUpperCase()))));
  }
  const BAY_STYLE = {
    ready: 'border-emerald-500/40 text-emerald-300 bg-emerald-500/10', preparing: 'border-sky-500/40 text-sky-300 bg-sky-500/10',
    occupied: 'border-red-500/40 text-red-300 bg-red-500/10', cleaning: 'border-amber-500/40 text-amber-300 bg-amber-500/10',
  };

  function renderReport() {
    const s = report;
    if (!s) return;
    const inc = s.incident;
    const resp = s.kind === 'response';
    const BUTTONS = resp ? RESPONSE_BUTTONS : ACTION_BUTTONS;
    const located = inc && inc.latitude != null;
    // replaceChildren() would print a skipped section as the text "false"; keep only real nodes.
    viewer.replaceChildren(...[
      s.read_only && h('div', { class: 'px-4 py-2 rounded-t-xl bg-slate-800/80 border-b border-brand-borderSubtle flex items-center gap-2 text-[0.6875rem] font-mono font-bold tracking-widest text-slate-200' },
        icon('lock', 'text-[0.9375rem]'), 'ARCHIVED REPORT · READ-ONLY'),
      h('header', { class: 'px-4 py-3 flex flex-wrap items-center gap-x-3 gap-y-1' },
        h('h2', { class: 'text-lg font-extrabold text-white font-mono', text: `REPORT #${s.code}` }),
        s.medic_unit && h('span', { class: 'text-sm font-bold text-slate-200 uppercase', text: s.medic_unit }),
        UI.triageBadge(s),
        h('span', { class: 'ml-auto flex items-center gap-2' }, h('span', { class: 'text-[0.625rem] font-mono text-slate-400', text: 'STATUS' }), UI.statusBadge(s),
          h('button', { type: 'button', id: 'hp-viewer-close', class: 'ml-1 p-1 rounded text-slate-300 hover:text-white hover:bg-white/10', title: 'Close report (Esc)', 'aria-label': `Close report ${s.code}`, onclick: closeReport }, icon('close', 'text-[1.25rem] align-middle')))),
      !s.read_only && s.actions.length && h('div', { class: 'px-4 pb-3 flex flex-wrap gap-2' }, ...s.actions.filter((a) => BUTTONS[a]).map((a) => {
        const [label, glyph, cls] = BUTTONS[a];
        const run = (e) => (a === 'unable' ? openUnable(s) : a === 'complete' && !resp ? openComplete(s) : act(s, a, e.currentTarget));
        return h('button', { type: 'button', 'data-action': a, class: `inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-sm font-semibold ${cls}`, onclick: run }, icon(glyph, 'text-[1.125rem]'), label);
      })),
      !resp && section('Patient Information', 'person', facts(['Name', s.patient_name], ['Age', s.patient_age != null ? `${s.patient_age} years` : null], ['Sex', s.gender_label],
        ['Condition reported by EMS', s.condition_label, `font-semibold ${UI.CONDITION_STYLE[s.patient_condition] || ''}`])),
      section('Incident Information', 'report', inc
        ? facts(['Incident Type', inc.emergency_type], ['CAD Number', inc.code, 'font-mono'], ['Time Reported', UI.fmtFull(inc.created_at)],
          ['Location', located ? `${Number(inc.latitude).toFixed(5)}, ${Number(inc.longitude).toFixed(5)}` : 'Location unavailable', 'font-mono'],
          ['Incident Status', inc.status_label], ['Incident Description', inc.description])
        : h('p', { class: 'text-sm text-slate-400', text: 'This report is not linked to an ESRMS incident.' })),
      !resp && section('Transport', 'ambulance', facts(['Medic Unit', s.medic_unit], ['Transport Type', s.transport_label],
        ['ETA', s.eta ? `${UI.fmtWhen(s.eta)}${s.read_only ? '' : ` · ${UI.etaText(s.eta).replace('ETA ', '')}`}` : null], ['Destination', s.hospital_name])),
      !resp && section('Triage', 'monitor_heart', facts(['Priority', s.triage_label], ['Triage Level', s.condition_label], ['Chief Complaint', s.chief_complaint], ['Signal Type', s.signal_type_label])),
      !resp && section('Vitals', 'vital_signs', facts(...UI.vitals(s))),
      !resp && section('Field Clinician Notes', 'clinical_notes', s.additional_notes
        ? h('p', { class: 'p-3 rounded-md bg-[#0a1020] border border-brand-borderSubtle text-sm text-slate-100 whitespace-pre-line break-words', text: s.additional_notes })
        : h('p', { class: 'text-sm text-slate-400', text: 'No notes were sent with this report.' })),
      !resp && section('Destination', 'local_hospital', facts(['Receiving Hospital', s.hospital_name], ['Emergency Department', s.hospital_ed_contact]), h('div', { class: 'mt-3' }, baySection(s))),
      !resp && (s.arrived_at || s.receiving_staff || s.rejection_reason) && section('Handoff', 'handshake', facts(
        ['Arrival Time', s.arrived_at ? UI.fmtFull(s.arrived_at) : null], ['Handoff Started', s.handoff_started_at ? UI.fmtFull(s.handoff_started_at) : null],
        ['Completed', s.completed_at ? UI.fmtFull(s.completed_at) : null], ['Receiving Nurse / Doctor', s.receiving_staff],
        s.rejection_reason && ['Unable to Receive: Reason', s.rejection_reason, 'text-red-200'], ['Clinical / Handoff Notes', s.handoff_notes])),
      resp && section('Signal', 'sensors', facts(['Signal Type', s.signal_type_label], ['Priority', s.triage_label],
        ['Distance', s.distance_km != null ? `${s.distance_km.toFixed(1)} km from your base to the incident` : 'Incident location unavailable'],
        ['Sent', UI.fmtFull(s.sent_at)], ['Sent By', s.sent_by_name || 'Dispatcher'], ['Sent To', s.hospital_name])),
      resp && section('Message from Dispatch', 'chat', s.additional_notes
        ? h('p', { class: 'p-3 rounded-md bg-[#0a1020] border border-brand-borderSubtle text-sm text-slate-100 whitespace-pre-line break-words', text: s.additional_notes })
        : h('p', { class: 'text-sm text-slate-400', text: 'No message was sent with this signal.' })),
      resp && section('Response', 'directions_run', facts(
        ['Acknowledged', s.acknowledged_at ? UI.fmtFull(s.acknowledged_at) : null], ['Accepted', s.accepted_at ? UI.fmtFull(s.accepted_at) : null],
        ['Responding', s.responding_at ? UI.fmtFull(s.responding_at) : null], ['On Scene', s.on_scene_at ? UI.fmtFull(s.on_scene_at) : null],
        ['Completed', s.completed_at ? UI.fmtFull(s.completed_at) : null],
        s.rejection_reason && ['Unable to Respond: Reason', s.rejection_reason, 'text-red-200'], s.handoff_notes && ['Notes', s.handoff_notes]),
        h('p', { class: 'mt-2 text-[0.6875rem] text-slate-400', text: 'Your organization decides which of its own personnel and units to deploy. ESRMS does not track individual responders.' })),
      section('Timeline', 'timeline', UI.timeline(s.timeline))].filter(Boolean));
  }

  // ------------------------------------------------------------ actions (each one is stored before the screen changes)

  const DONE = { accept: 'accepted', respond: 'marked as responding', on_scene: 'marked on scene', acknowledge: 'acknowledged', arrive: 'marked as arrived', handoff: 'handoff started', complete: 'handoff completed and archived', unable: 'marked as unable to receive' };

  async function refreshAfterChange(item) {
    // An acknowledged signal is no longer a signal: follow it to Incoming Reports with the report open.
    if (item && state.view === 'signals' && item.status !== 'sent' && !item.read_only) {
      show('incoming');
      state.openId = item.id;
      await loadReport();
      loadOverview();
      checkSignals();
      return;
    }
    // A finished report (handoff completed, or unable to receive) is filed away, not left open as a
    // read-only page. It can be opened again from Completed Handoffs or History.
    if (item && item.read_only) closeReport();
    else if (item && state.openId === item.id) await loadReport();
    loadOverview();
    loadList();
    checkSignals();
  }

  async function act(s, action, btn, body = {}) {
    let failed = null;
    let item = null;
    await UI.busy(btn, async () => {
      try {
        ({ item } = await call(`/reports/${s.id}/${action}`, { method: 'POST', body }));
        toast(`${s.code} ${s.kind === 'response' && action === 'complete' ? 'response completed' : DONE[action]}${action === 'acknowledge' ? ' and moved to Incoming Reports' : ''}. Dispatch has been informed.`, 'success');
      } catch (err) {
        failed = err;
        if (!err.fields) toast(err.message, 'error');
      }
    });
    await refreshAfterChange(item || (failed && failed.status === 409 ? s : null));
    if (failed) throw failed;
    return item;
  }
  // Buttons call act() without awaiting it; a failure has already been shown as a toast.
  window.addEventListener('unhandledrejection', (e) => { if (e.reason && e.reason.status !== undefined) e.preventDefault(); });

  async function assignBay(s, bayId, team, btn) {
    if (!bayId) return toast('Choose a bay first.', 'error');
    let item = null;
    await UI.busy(btn, async () => {
      try {
        ({ item } = await call(`/reports/${s.id}/bay`, { method: 'POST', body: { bay_id: bayId, receiving_team: team } }));
        toast(`${s.code} → ${item.bay_name}.`, 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    await refreshAfterChange(item || s);
  }

  const unableEl = $('hp-unable');
  const unableForm = $('hp-unable-form');
  let dialogReport = null;
  const showError = (id, text) => { $(id).textContent = text || ''; $(id).classList.toggle('hidden', !text); };

  function openUnable(s) {
    dialogReport = s;
    unableForm.reset();
    showError('hp-unable-error', '');
    $('hp-unable-text').textContent = `${s.code}. The dispatcher will be told your hospital cannot receive this patient, and why.`;
    unableEl.showModal();
  }
  unableForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const choice = unableForm.elements.choice.value;
    const detail = unableForm.elements.detail.value.trim();
    // A reason is mandatory: the dispatcher has to know why to re-route the patient.
    if (!choice) return showError('hp-unable-error', 'Choose a reason before submitting.');
    if (choice === 'Other' && !detail) return showError('hp-unable-error', 'Describe the reason when choosing Other.');
    showError('hp-unable-error', '');
    try {
      await act(dialogReport, 'unable', e.submitter, { reason: choice === 'Other' ? detail : detail ? `${choice}: ${detail}` : choice });
      unableEl.close();
    } catch (err) {
      if (err.fields) showError('hp-unable-error', err.fields.reason || err.message); else unableEl.close();
    }
  });

  const completeEl = $('hp-complete');
  const completeForm = $('hp-complete-form');
  function openComplete(s) {
    dialogReport = s;
    completeForm.reset();
    showError('hp-complete-error', '');
    completeForm.querySelectorAll('[data-error-for]').forEach((p) => p.classList.add('hidden'));
    const line = (label, value) => h('div', { class: 'flex gap-3 py-1 border-b border-brand-borderSubtle/60' },
      h('dt', { class: 'w-28 shrink-0 text-[0.6875rem] font-mono uppercase text-slate-400 pt-0.5', text: label }), h('dd', { class: 'min-w-0 flex-1 text-slate-100 break-words', text: value || 'Not provided' }));
    $('hp-complete-summary').replaceChildren(line('Patient', UI.patientLine(s)), line('Report', s.code), line('CAD', s.incident ? s.incident.code : 'Not linked'),
      line('Medic Unit', s.medic_unit), line('Trauma Bay', s.bay_name), line('Arrival Time', s.arrived_at ? UI.fmtFull(s.arrived_at) : null));
    completeForm.elements.receiving_staff.value = overview?.me.name || '';
    completeEl.showModal();
    completeForm.elements.receiving_staff.select();
  }
  completeForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { receiving_staff: completeForm.elements.receiving_staff.value, handoff_notes: completeForm.elements.handoff_notes.value };
    completeForm.querySelectorAll('[data-error-for]').forEach((p) => p.classList.add('hidden'));
    if (!body.receiving_staff.trim()) {
      const p = completeForm.querySelector('[data-error-for="receiving_staff"]');
      p.textContent = 'Enter the nurse or doctor who received the patient.';
      return p.classList.remove('hidden');
    }
    try {
      await act(dialogReport, 'complete', e.submitter, body);
      completeEl.close();
      toast('Report moved to Completed Handoffs and History.', 'info');
    } catch (err) {
      if (!err.fields) return completeEl.close();
      for (const [name, text] of Object.entries(err.fields)) {
        const p = completeForm.querySelector(`[data-error-for="${name}"]`);
        if (p) { p.textContent = text; p.classList.remove('hidden'); }
      }
    }
  });

  // ------------------------------------------------------------ settings

  const profileForm = $('hp-profile');
  function renderSettings() {
    if (!overview) return;
    const { hospital, me, bays } = overview;
    const admin = me.is_admin;
    $('hp-profile-note').textContent = admin ? 'Shown to ESRMS dispatchers when they choose a receiving hospital.' : 'Only a Hospital Administrator can change the hospital profile.';
    const map = { hospital_name: hospital.name, address: hospital.address, city: hospital.city, province: hospital.province, country: hospital.country,
      contact_number: hospital.phone_number, ed_contact: hospital.ed_contact, official_email: hospital.official_email };
    for (const [name, value] of Object.entries(map)) {
      const input = profileForm.elements[name];
      if (document.activeElement !== input) input.value = value || '';
      input.disabled = !admin;
    }
    profileForm.querySelector('button[type=submit]').classList.toggle('hidden', !admin);
    $('hp-bay-add').classList.toggle('hidden', !admin);
    $('hp-staff-add').classList.toggle('hidden', !admin);
    $('hp-bay-list').replaceChildren(...(bays.length ? bays.map((b) => h('li', { class: 'flex flex-wrap items-center gap-2 rounded-md bg-[#141a2b] border border-brand-borderSubtle px-2.5 py-1.5 text-sm' },
      h('span', { class: 'min-w-0 flex-1 truncate text-slate-100', text: b.name }),
      h('span', { class: 'text-[0.6875rem] text-slate-400', text: b.kind_label }),
      h('span', { class: `px-2 py-0.5 rounded border text-[0.625rem] font-mono ${BAY_STYLE[b.status]}`, text: `${b.status_label.toUpperCase()}${b.report_code ? ` · ${b.report_code}` : ''}` }),
      !b.report_id && h('button', { type: 'button', class: 'text-xs text-brand-cyan underline', onclick: (e) => setBayStatus(b, b.status === 'ready' ? 'cleaning' : 'ready', e.currentTarget), text: b.status === 'ready' ? 'Mark cleaning' : 'Mark ready' }),
      admin && h('button', { type: 'button', class: 'text-xs text-slate-300 underline', onclick: () => openBay(b), text: 'Edit' })))
      : [h('li', { class: 'text-xs text-amber-300', text: 'No bays yet. Add your trauma bays and emergency rooms so patients can be assigned to them.' })]));
    loadStaff();
  }

  profileForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('hp-profile-error', '');
    profileForm.querySelectorAll('[data-error-for]').forEach((p) => p.classList.add('hidden'));
    await UI.busy(e.submitter, async () => {
      try {
        await call('/profile', { method: 'PUT', body: Object.fromEntries(new FormData(profileForm)) });
        toast('Hospital profile saved.', 'success');
        loadOverview();
      } catch (err) {
        showError('hp-profile-error', err.message);
        for (const [name, text] of Object.entries(err.fields || {})) {
          const p = profileForm.querySelector(`[data-error-for="${name}"]`);
          if (p) { p.textContent = text; p.classList.remove('hidden'); }
        }
      }
    });
  });

  async function setBayStatus(bay, status, btn) {
    await UI.busy(btn, async () => {
      try {
        await call(`/bays/${bay.id}/status`, { method: 'POST', body: { status } });
        toast(`${bay.name} marked ${opt.bayStored[status].toLowerCase()}.`, 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    loadOverview();
  }

  const bayDialog = $('hp-bay-dialog');
  const bayForm = $('hp-bay-form');
  let editingBay = null;
  function openBay(bay = null) {
    editingBay = bay;
    bayForm.reset();
    showError('hp-bay-error', '');
    bayForm.querySelector('[data-error-for="name"]').classList.add('hidden');
    $('hp-bay-dialog-title').textContent = bay ? 'Edit Bay' : 'Add Bay';
    $('hp-bay-delete').classList.toggle('hidden', !bay);
    if (bay) { bayForm.elements.name.value = bay.name; bayForm.elements.kind.value = bay.kind; }
    bayDialog.showModal();
    bayForm.elements.name.focus();
  }
  $('hp-bay-add').addEventListener('click', () => openBay());
  bayForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    await UI.busy(e.submitter, async () => {
      try {
        await call(editingBay ? `/bays/${editingBay.id}` : '/bays', { method: editingBay ? 'PUT' : 'POST', body: Object.fromEntries(new FormData(bayForm)) });
        bayDialog.close();
        toast('Bay saved.', 'success');
        loadOverview();
      } catch (err) {
        const p = bayForm.querySelector('[data-error-for="name"]');
        p.textContent = err.fields?.name || '';
        p.classList.toggle('hidden', !err.fields?.name);
        showError('hp-bay-error', err.fields?.name ? '' : err.message);
      }
    });
  });
  $('hp-bay-delete').addEventListener('click', async (e) => {
    if (!editingBay || !window.confirm(`Delete ${editingBay.name}?`)) return;
    await UI.busy(e.currentTarget, async () => {
      try {
        await call(`/bays/${editingBay.id}`, { method: 'DELETE' });
        bayDialog.close();
        toast('Bay deleted.', 'success');
        loadOverview();
      } catch (err) {
        showError('hp-bay-error', err.message);
      }
    });
  });

  const staffDialog = $('hp-staff-dialog');
  const staffForm = $('hp-staff-form');
  let editingStaff = null;
  async function loadStaff() {
    try {
      const { items, can_manage: canManage } = await call('/staff');
      $('hp-staff-list').replaceChildren(...items.map((m) => h('li', { class: 'flex flex-wrap items-center gap-2 rounded-md bg-[#141a2b] border border-brand-borderSubtle px-2.5 py-1.5 text-sm' },
        h('span', { class: 'min-w-0 flex-1' },
          h('span', { class: 'block truncate text-slate-100', text: m.name || m.email || 'Account removed' }),
          h('span', { class: 'block truncate text-[0.6875rem] text-slate-400', text: [m.email, m.position].filter(Boolean).join(' · ') })),
        h('span', { class: 'px-2 py-0.5 rounded border border-brand-cyan/40 text-[0.625rem] font-mono text-brand-cyan', text: m.role_label }),
        canManage && h('button', { type: 'button', class: 'text-xs text-slate-300 underline', onclick: () => openStaff(m), text: 'Edit' }),
        canManage && h('button', { type: 'button', class: 'text-xs text-red-300 underline', onclick: () => removeStaff(m), text: 'Remove' }))));
    } catch (err) {
      $('hp-staff-list').replaceChildren(h('li', { class: 'text-xs text-red-300', role: 'alert', text: err.message }));
    }
  }
  function openStaff(member = null) {
    editingStaff = member;
    staffForm.reset();
    showError('hp-staff-error', '');
    staffForm.querySelectorAll('[data-error-for]').forEach((p) => p.classList.add('hidden'));
    staffForm.querySelectorAll('[data-new-only]').forEach((el) => el.classList.toggle('hidden', Boolean(member)));
    $('hp-staff-dialog-title').textContent = member ? `Edit ${member.name || member.email}` : 'Add Staff Account';
    if (member) { staffForm.elements.staff_role.value = member.staff_role; staffForm.elements.position.value = member.position || ''; }
    staffDialog.showModal();
  }
  $('hp-staff-add').addEventListener('click', () => openStaff());
  staffForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('hp-staff-error', '');
    staffForm.querySelectorAll('[data-error-for]').forEach((p) => p.classList.add('hidden'));
    await UI.busy(e.submitter, async () => {
      try {
        const body = Object.fromEntries(new FormData(staffForm));
        const data = await call(editingStaff ? `/staff/${editingStaff.user_id}` : '/staff', { method: editingStaff ? 'PUT' : 'POST', body });
        staffDialog.close();
        toast(editingStaff ? 'Staff role updated.' : data.created ? 'Staff account created. Share the temporary password privately.' : 'Existing ESRMS account added to your staff.', 'success');
        loadStaff();
      } catch (err) {
        let shown = false;
        for (const [name, text] of Object.entries(err.fields || {})) {
          const p = staffForm.querySelector(`[data-error-for="${name}"]`);
          if (p) { p.textContent = text; p.classList.remove('hidden'); shown = true; }
        }
        if (!shown) showError('hp-staff-error', err.message);
      }
    });
  });
  async function removeStaff(member) {
    if (!window.confirm(`Remove ${member.name || member.email} from your hospital's staff? They will no longer see patient reports.`)) return;
    try {
      await call(`/staff/${member.user_id}`, { method: 'DELETE' });
      toast('Staff member removed.', 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
    loadStaff();
  }

  // ------------------------------------------------------------ navigation + live updates

  function show(view) {
    if (view !== state.view) closeReport();   // each tab starts with an empty viewer
    Object.assign(state, { view, filter: '', q: '', page: 1 });
    $('hp-search').reset();
    root.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === view)));
    const settings = view === 'settings';
    $('hp-reports').classList.toggle('hidden', settings || blocked);
    $('hp-settings').classList.toggle('hidden', !settings || blocked);
    $('hp-stats').classList.toggle('hidden', settings);
    if (settings) return renderSettings();
    renderListFrame();
    loadList();
  }
  root.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
  root.querySelectorAll('dialog [data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));

  // One refetch per burst of realtime events. An open dialog is never refreshed away.
  let changeTimer;
  function onChange() {
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => {
      loadOverview();
      checkSignals();
      if (state.view !== 'settings') loadList();
      if (state.openId && !root.querySelector('dialog[open]') && !viewer.querySelector('input:focus, select:focus')) loadReport();
    }, 250);
  }
  if (window.AnnouncementLive) {
    window.AnnouncementLive.start({ ...opt.realtime, onChange, onStatus: (s) => conn.realtime(s) });
  } else {
    conn.realtime('polling');
    setInterval(onChange, 15000);
  }
  setInterval(() => { if (state.view === 'incoming' || state.view === 'signals') loadList(); }, 60000);   // keep "ETA 4 min" honest

  loadOverview().then(() => { if (!blocked) { show('incoming'); checkSignals(); } });
})();
