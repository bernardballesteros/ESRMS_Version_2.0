// Incident & Signal History tab: the Incident Reports / Signal History sub-tabs, and the Signal History archive.
// Signals are the same hospital_signals records the Hospital Signal tab sends, shown here once archived, read from
// /api/hospital-signals/signals (Flask). Nothing is copied or kept in the browser.
(function () {
  const root = document.getElementById('incident-archive');
  if (!root || !window.AnnUI) return;
  const { h, icon, toast, api } = window.AnnUI;

  // ------------------------------------------------------------ sub-tabs

  const tabs = [...root.querySelectorAll('[data-archive-tab]')];
  let current = 'incidents';
  let reload = () => {};
  function show(name) {
    current = name;
    tabs.forEach((t) => { t.setAttribute('aria-selected', String(t.dataset.archiveTab === name)); t.tabIndex = t.dataset.archiveTab === name ? 0 : -1; });
    root.querySelectorAll('[data-archive-panel]').forEach((p) => p.classList.toggle('hidden', p.dataset.archivePanel !== name));
    if (name === 'signals') reload();
  }
  tabs.forEach((t) => {
    t.addEventListener('click', () => show(t.dataset.archiveTab));
    t.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const next = tabs[(tabs.indexOf(t) + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      show(next.dataset.archiveTab);
    });
  });
  show('incidents');

  const cfgEl = document.getElementById('hospital-signal-config');
  const filters = document.getElementById('signal-archive-filters');
  if (!cfgEl || !filters || !window.HospitalUI) return;   // this account cannot view hospital signals
  const UI = window.HospitalUI;
  const cfg = JSON.parse(cfgEl.textContent);
  const PAGE = cfg.options.pageSizes[cfg.options.pageSizes.length > 1 ? 1 : 0];

  const whenFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
  const fmt = (iso) => (iso ? whenFmt.format(new Date(iso)) : '—');
  const visible = () => current === 'signals' && !document.querySelector('[data-tab-panel="reports"]').classList.contains('hidden');

  // ------------------------------------------------------------ list

  const body = document.getElementById('signal-archive-body');
  const more = document.getElementById('signal-archive-more');
  let items = [];
  let pages = 0;      // pages currently shown
  let total = 0;
  let seq = 0;

  const message = (text, cls = 'text-slate-400', role = null) => h('tr', {}, h('td', { colspan: '9', class: `p-6 text-sm text-center ${cls}`, role, text }));
  const td = (...c) => h('td', { class: 'px-3 py-2 align-top' }, ...c);

  function row(s) {
    const open = () => openDetail(s.id);
    return h('tr', {
      tabindex: '0', 'aria-label': `Open signal ${s.code}`,
      class: 'border-b border-brand-borderSubtle/60 cursor-pointer hover:bg-white/[0.02] focus-visible:bg-brand-cyan/10',
      onclick: open,
      onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(); } },
    },
      td(h('span', { class: 'font-mono text-xs text-brand-cyan whitespace-nowrap', text: s.code })),
      td(h('div', { class: 'text-white whitespace-nowrap', text: s.signal_type_label }),
        s.patient_name && h('div', { class: 'text-[0.6875rem] text-slate-400 line-clamp-1', text: s.patient_name })),
      td(h('span', { class: 'text-slate-200', text: s.hospital_name })),
      td(h('span', { class: 'font-mono text-xs text-slate-200 whitespace-nowrap', text: s.incident ? s.incident.code : '—' })),
      td(UI.triageBadge(s)),
      td(UI.statusBadge(s)),
      td(h('span', { class: 'text-xs text-slate-300 whitespace-nowrap', title: UI.fmtFull(s.sent_at), text: fmt(s.sent_at) })),
      td(h('span', { class: 'text-xs text-slate-300 whitespace-nowrap', title: UI.fmtFull(s.archived_at), text: fmt(s.archived_at) })),
      td(h('div', { class: 'flex gap-2' },
        h('button', { type: 'button', class: 'text-xs text-brand-cyan underline whitespace-nowrap', onclick: (e) => { e.stopPropagation(); open(); }, text: 'View Timeline' }),
        h('button', { type: 'button', class: 'text-xs text-slate-300 underline', 'aria-label': `Restore ${s.code} to Hospital Signal`, onclick: (e) => { e.stopPropagation(); restore(s, e.currentTarget); }, onkeydown: (e) => e.stopPropagation(), text: 'Restore' }))));
  }

  async function restore(s, btn) {
    await UI.busy(btn, async () => {
      try {
        await api(`${cfg.api.signals}/${s.id}/restore`, { method: 'POST', body: {} });
        toast(`${s.code} restored to Hospital Signal.`, 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    document.dispatchEvent(new CustomEvent('esrms:signals-restored'));
    reload();
  }

  function query(page) {
    const p = new URLSearchParams({ page, per_page: PAGE, archived: 1 });
    for (const [k, v] of new FormData(filters)) if (v.trim()) p.set(k, v.trim());
    return api(`${cfg.api.signals}?${p}`);
  }

  function render() {
    const filtered = [...new FormData(filters).values()].some((v) => v.trim());
    body.replaceChildren(...(items.length ? items.map(row)
      : [message(filtered ? 'No signals match this search or filter.' : 'No archived signals yet. Press Archive on a finished signal in the Hospital Signal tab to move it here.')]));
    more.replaceChildren(...[
      h('span', { text: total ? `Showing ${items.length} of ${total} signal${total === 1 ? '' : 's'}, newest first` : '' }),
      items.length < total && h('button', {
        type: 'button', class: 'px-4 py-2 rounded-md border border-brand-cyan/50 text-brand-cyan font-semibold hover:bg-brand-cyan/10 disabled:opacity-60',
        onclick: (e) => UI.busy(e.currentTarget, loadMore), text: 'Load More',
      })].filter(Boolean));
  }

  // Refetch every page already on screen, so a refresh never drops what the user had scrolled to.
  reload = async function (reset = false) {
    const mine = ++seq;
    const want = reset ? 1 : Math.max(1, pages);
    if (!items.length) body.replaceChildren(message('Loading signals…'));
    try {
      const fresh = [];
      let data;
      for (let page = 1; page <= want; page += 1) {
        data = await query(page);
        if (mine !== seq) return;
        fresh.push(...data.items);
        if (page >= data.pages) break;
      }
      const seen = new Set();
      items = fresh.filter((s) => !seen.has(s.id) && seen.add(s.id));
      total = data.total;
      pages = Math.max(1, Math.ceil(items.length / PAGE));
      render();
      if (openId) refreshDetail();
    } catch (err) {
      if (mine !== seq) return;
      items = [];
      pages = 0;
      body.replaceChildren(message(err.status === 401 ? 'Your session has ended. Sign in again to continue.' : err.message, 'text-red-300', 'alert'));
      more.replaceChildren();
    }
  };

  async function loadMore() {
    const mine = seq;
    try {
      const data = await query(pages + 1);
      if (mine !== seq) return;
      const have = new Set(items.map((s) => s.id));
      items.push(...data.items.filter((s) => !have.has(s.id)));   // a signal sent meanwhile shifts the pages by one
      total = data.total;
      pages += 1;
      render();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  let filterTimer;
  filters.addEventListener('input', (e) => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => reload(true), e.target.name === 'q' ? 350 : 0);
  });
  filters.addEventListener('submit', (e) => { e.preventDefault(); reload(true); });

  // ------------------------------------------------------------ detail

  const dialog = document.getElementById('signal-archive-detail');
  const detailBody = document.getElementById('signal-archive-detail-body');
  const detailActions = document.getElementById('signal-archive-detail-actions');
  let openId = null;

  const line = (label, value) => h('div', { class: 'flex gap-3 py-1.5 border-b border-brand-borderSubtle/60 text-sm' },
    h('dt', { class: 'w-40 shrink-0 text-[0.6875rem] font-mono uppercase tracking-wide text-slate-400 pt-0.5', text: label }),
    h('dd', { class: 'min-w-0 flex-1 text-slate-100 break-words whitespace-pre-line' }, value == null || value === '' ? h('span', { class: 'text-slate-400', text: 'Not recorded' }) : value));
  const section = (title, ...children) => h('section', { class: 'mb-4' },
    h('h3', { class: 'mb-1 text-[0.6875rem] font-mono font-bold uppercase tracking-widest text-brand-cyan', text: title }), ...children);

  function detail(s) {
    const i = s.incident;
    const measured = UI.vitals(s).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ');
    return h('div', {},
      section('Signal Information', h('dl', {},
        line('Signal ID', h('span', { class: 'font-mono text-brand-cyan', text: s.code })),
        line('Signal Type', s.signal_type_label),
        line('Priority', UI.triageBadge(s)),
        line('Status', UI.statusBadge(s)),
        line('Sent', `${UI.fmtFull(s.sent_at)}${s.sent_by_name ? ` · ${s.sent_by_name}` : ''}`),
        line('Archived', s.archived_at ? UI.fmtFull(s.archived_at) : 'Not archived: still in Hospital Signal'),
        s.rejection_reason && line('Unable Reason', s.rejection_reason))),
      section('Hospital', h('dl', {},
        line('Hospital Name', s.hospital_name),
        line('Emergency Department', s.hospital_ed_contact),
        line('Hospital Contact', s.hospital_phone),
        s.hospital_address && line('Address', s.hospital_address),
        s.bay_name && line('Bay', s.bay_name),
        s.receiving_staff && line('Received By', s.receiving_staff))),
      section('Incident', i ? h('dl', {},
        line('Incident ID', h('span', { class: 'font-mono text-brand-cyan', text: i.code })),
        line('Emergency Type', i.emergency_type),
        line('Incident Status', `${i.status_label}${i.archived ? ' · archived' : ''}`),
        line('Description', i.description),
        line('Coordinates', i.has_location && i.latitude != null ? `${Number(i.latitude).toFixed(5)}, ${Number(i.longitude).toFixed(5)}` : 'Unavailable'))
        : h('p', { class: 'text-sm text-slate-400', text: 'This signal was sent without a linked incident.' })),
      section('Patient', h('dl', {},
        line('Patient Name', s.patient_name),
        line('Age', s.patient_age != null ? `${s.patient_age}` : null),
        line('Sex', s.gender_label),
        line('Condition', h('span', { class: `font-semibold ${UI.CONDITION_STYLE[s.patient_condition] || ''}`, text: s.condition_label })),
        line('Chief Complaint', s.chief_complaint),
        measured && line('Vitals', measured),
        (s.medic_unit || s.transport_label) && line('Transport', [s.medic_unit, s.transport_label].filter(Boolean).join(' · ')),
        s.eta && line('ETA', UI.fmtFull(s.eta)))),
      section('Message', h('p', { class: `text-sm whitespace-pre-line break-words ${s.additional_notes ? 'text-slate-100' : 'text-slate-400'}`, text: s.additional_notes || 'No message was written for this signal.' }),
        s.handoff_notes && h('p', { class: 'mt-2 text-sm text-slate-200 whitespace-pre-line break-words', text: `Handoff notes: ${s.handoff_notes}` })),
      section('Timeline', UI.timeline(s.timeline)));
  }

  function viewIncident(i) {
    dialog.close();
    if (i.archived) show('incidents');
    const opened = window.ESRMSDispatch && window.ESRMSDispatch.open({ id: i.id, code: i.code, archived: i.archived });
    if (!opened) toast(`${i.code} is not in the Dispatch Center list right now. Refresh the Dispatch Center and try again.`, 'error');
  }

  async function openDetail(id) {
    openId = id;
    detailBody.replaceChildren(h('p', { class: 'py-8 text-center text-sm text-slate-400', text: 'Loading…' }));
    detailActions.replaceChildren();
    if (!dialog.open) dialog.showModal();
    await refreshDetail();
  }

  async function refreshDetail() {
    const id = openId;
    try {
      const { item } = await api(`${cfg.api.signals}/${id}`);
      if (openId !== id) return;
      detailBody.replaceChildren(detail(item));
      detailActions.replaceChildren(...[
        item.incident && h('button', { type: 'button', class: 'inline-flex items-center gap-2 px-4 py-2 rounded-md text-sm font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10', onclick: () => viewIncident(item.incident) }, icon('description', 'text-[1rem]'), 'View Incident'),
        h('button', { type: 'button', class: 'px-4 py-2 rounded-md text-sm text-slate-300 hover:bg-white/10', onclick: () => dialog.close(), text: 'Close' })].filter(Boolean));
    } catch (err) {
      if (openId !== id) return;
      detailBody.replaceChildren(h('p', { class: 'py-6 text-sm text-red-300', role: 'alert', text: err.message }));
      detailActions.replaceChildren(h('button', { type: 'button', class: 'px-4 py-2 rounded-md text-sm text-slate-300 hover:bg-white/10', onclick: () => dialog.close(), text: 'Close' }));
    }
  }
  dialog.addEventListener('close', () => { openId = null; });
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());

  // ------------------------------------------------------------ wiring

  document.querySelector('[data-tab="reports"]')?.addEventListener('click', () => { if (current === 'signals') reload(); });
  // Raised by the Hospital Signal tab whenever a signal is sent or a hospital responds.
  document.addEventListener('esrms:signals-changed', () => { if (visible()) reload(); });

  // Used by the incident timeline (dispatch.js): every signal sent for one incident, oldest first.
  window.ESRMSSignalArchive = {
    async forIncident(incidentId) {
      const { items: found } = await api(`${cfg.api.signals}?${new URLSearchParams({ incident_id: incidentId, per_page: cfg.options.pageSizes[cfg.options.pageSizes.length - 1] })}`);
      if (!found.length) return null;
      return h('div', { class: 'mt-3' },
        h('p', { class: 'mb-1 font-mono text-[0.625rem] uppercase tracking-wider text-slate-400', text: `Hospital signals (${found.length})` }),
        h('ol', { class: 'space-y-1' }, ...found.slice().reverse().map((s) => h('li', {},
          h('span', { class: 'font-mono text-slate-500', text: `${fmt(s.sent_at)}  ` }),
          h('button', { type: 'button', class: 'font-mono text-brand-cyan underline', onclick: () => openDetail(s.id), text: s.code }),
          ` · ${s.signal_type_label} · ${s.hospital_name} · ${s.status_label}`))));
    },
  };
})();
