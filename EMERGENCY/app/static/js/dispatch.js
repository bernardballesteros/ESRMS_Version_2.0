// Dispatch Center console: incoming emergency reports, citizen location on the
// existing Leaflet map, incident status updates and the
// Incident Report history. All data comes from /api/dispatch (Flask), which reads
// the shared Supabase database; Realtime on dispatch_feed tells us when to refetch.
(function () {
  const cfgEl = document.getElementById('dispatch-config');
  const map = window.ESRMS_MAP;
  if (!cfgEl || !map || !window.L) return;
  const { h, icon, toast, api } = window.AnnUI;
  // Pop-up alerts honour the user's notification preferences (static/js/notifications.js).
  const notifyUser = (category, title, body, opts = {}) => (window.ESRMSNotify
    ? window.ESRMSNotify.notify(category, title, body, opts)
    : toast(`${title}: ${body}`, opts.kind || 'info'));
  const cfg = JSON.parse(cfgEl.textContent);
  const STATUS = cfg.options.statuses;
  const CLOSED = new Set(cfg.options.closed);
  const PRIORITY = cfg.options.priorities;
  const STREET_ZOOM = 17;

  const listEl = document.getElementById('incident-list');
  const detailEl = document.getElementById('incident-detail');
  const alertEl = document.getElementById('map-location-alert');
  // Filled by organizations.js. One node, re-attached on every render of the detail, so its content survives.
  const nearbyEl = h('section', { id: 'incident-nearby', class: 'mt-3 pt-3 border-t border-brand-borderSubtle empty:hidden', 'aria-label': 'Nearby organizations' });
  const announceSelection = () => document.dispatchEvent(new CustomEvent('esrms:incident-selected'));

  const incidents = new Map();   // id -> incident; keyed by id so realtime bursts never duplicate cards
  let knownIds = null;           // ids seen so far, to announce genuinely new reports
  let filter = 'open';
  let selectedId = null;
  let detail = null;             // full record (with contact number) of the selected incident
  let loadSeq = 0;
  const ownChanges = new Set();  // "id:status" this dispatcher just set, so it isn't announced back to them
  let duty = null;               // this dispatcher's server-confirmed console status
  const DUTY_LABEL = { on_duty: 'On duty', on_break: 'On break', off_duty: 'Off duty' };

  // ------------------------------------------------------------ formatting

  const timeFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', hour: '2-digit', minute: '2-digit', hour12: false });
  const dateTimeFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const fmtTime = (iso) => (iso ? timeFmt.format(new Date(iso)) : '—');
  const fmtDateTime = (iso) => (iso ? dateTimeFmt.format(new Date(iso)) : '—');
  function ago(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }
  const urgent = (i) => i.priority === 1 || i.priority === 2;

  const STATUS_STYLE = {
    new:          'bg-red-600 text-white animate-pulse',
    acknowledged: 'bg-brand-cyan/15 text-brand-cyan border border-brand-cyan/40',
    dispatched:   'bg-blue-500/15 text-blue-300 border border-blue-500/40',
    en_route:     'bg-amber-500/15 text-amber-300 border border-amber-500/40',
    on_scene:     'bg-violet-500/15 text-violet-300 border border-violet-500/40',
    resolved:     'bg-emerald-500/15 text-emerald-300 border border-emerald-500/40',
    cancelled:    'bg-slate-600/30 text-slate-400 border border-slate-600',
  };
  const statusBadge = (s) => h('span', { class: `px-1.5 py-px rounded text-[0.5625rem] font-mono font-bold uppercase tracking-wide whitespace-nowrap ${STATUS_STYLE[s] || ''}`, text: STATUS[s] || s });

  // Same rule as app/dispatch.py valid_coordinates(): finite, in range, and not (0, 0).
  function coords(i) {
    if (i.latitude == null || i.longitude == null) return null;
    const lat = Number(i.latitude);
    const lng = Number(i.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
    if (lat === 0 && lng === 0) return null;
    return [lat, lng];
  }

  // ------------------------------------------------------------ incidents: load + render

  async function loadIncidents() {
    const seq = ++loadSeq;
    listEl.setAttribute('aria-busy', 'true');
    try {
      const data = await api(cfg.api.incidents);
      if (seq !== loadSeq) return;
      if (data.duty && data.duty !== duty) {
        duty = data.duty;
        document.dispatchEvent(new CustomEvent('esrms:duty-observed', { detail: { status: duty } }));
      }
      const fresh = duty === 'on_duty' ? data.items.filter((i) => knownIds && !knownIds.has(i.id)) : [];
      announceChanges(data.items);
      incidents.clear();
      for (const i of data.items) incidents.set(i.id, i);
      knownIds = new Set([...(knownIds || []), ...incidents.keys()]);
      renderCounts(data.counts);
      renderList(new Set(fresh.map((i) => i.id)));
      renderMarkers();
      if (fresh.length) announce(fresh);
      if (selectedId) {
        const sel = incidents.get(selectedId);
        if (!sel) clearSelection();          // archived or deleted elsewhere
        else refreshSelected(sel);
      }
    } catch (err) {
      if (seq !== loadSeq) return;
      if (!incidents.size) {
        listEl.replaceChildren(h('div', { class: 'p-4 rounded-md border border-red-500/40 bg-red-950/40 text-center' },
          icon('cloud_off', 'text-red-400'),
          h('p', { class: 'text-xs text-red-200 mt-1', text: err.message }),
          h('button', { type: 'button', class: 'mt-2 text-xs underline text-red-200', onclick: loadIncidents, text: 'Retry' })));
      }
      toast(err.message, 'error');
    } finally {
      if (seq === loadSeq) listEl.setAttribute('aria-busy', 'false');
    }
  }

  function announce(fresh) {
    const ping = document.querySelector('[data-new-ping]');
    ping?.classList.remove('hidden');
    setTimeout(() => ping?.classList.add('hidden'), 15000);
    for (const i of fresh.slice(0, 3)) {
      notifyUser(urgent(i) ? 'emergency_incidents' : 'new_reports', `New emergency report ${i.code}`, i.emergency_type,
        { kind: urgent(i) ? 'error' : 'info', tag: `incident-${i.id}` });
    }
  }

  // Changes made by other dispatchers since the last load: escalations and status moves.
  const DISPATCH_STATUSES = new Set(['dispatched', 'en_route', 'on_scene']);
  function announceChanges(items) {
    let shown = 0;
    for (const i of items) {
      const prev = incidents.get(i.id);
      if (!prev || shown >= 3) continue;
      if (urgent(i) && !urgent(prev) && !CLOSED.has(i.status)) {
        notifyUser('emergency_incidents', `${i.code} escalated to P${i.priority}`, i.emergency_type, { kind: 'error', tag: `incident-${i.id}` });
        shown += 1;
      } else if (prev.status !== i.status && !ownChanges.has(`${i.id}:${i.status}`)) {
        notifyUser(DISPATCH_STATUSES.has(i.status) ? 'dispatch_updates' : 'status_updates',
          `${i.code} is now ${STATUS[i.status] || i.status}`, i.emergency_type, { tag: `incident-${i.id}` });
        shown += 1;
      }
    }
  }

  function renderCounts(c) {
    for (const el of document.querySelectorAll('[data-dispatch-count]')) el.textContent = c[el.dataset.dispatchCount] ?? 0;
    const all = [...incidents.values()];
    const n = { open: all.filter((i) => !CLOSED.has(i.status)).length, new: c.new, closed: all.filter((i) => CLOSED.has(i.status)).length, all: all.length };
    for (const el of document.querySelectorAll('[data-filter-count]')) el.textContent = n[el.dataset.filterCount] ? `(${n[el.dataset.filterCount]})` : '';
  }

  function visible() {
    // The server already sorted: new first, then other unresolved, then closed; priority; newest.
    const all = [...incidents.values()];
    if (filter === 'open') return all.filter((i) => !CLOSED.has(i.status));
    if (filter === 'new') return all.filter((i) => i.status === 'new');
    if (filter === 'closed') return all.filter((i) => CLOSED.has(i.status));
    return all;
  }

  // Shown above the list while this dispatcher is not On Duty: the server only sends their own incidents.
  function dutyBanner() {
    if (!duty || duty === 'on_duty') return null;
    return h('div', { class: 'shrink-0 flex items-start gap-2 p-2.5 rounded-md border border-amber-500/40 bg-amber-950/30 text-[0.6875rem] text-amber-200', role: 'status' },
      icon(duty === 'on_break' ? 'coffee' : 'do_not_disturb_on', 'text-[1rem] text-amber-300'),
      h('span', {}, h('b', { text: `You are ${DUTY_LABEL[duty]}. ` }),
        'New reports are not sent to you. Only incidents still assigned to you are shown. Set Console status to On duty to receive new incidents.'));
  }

  function renderList(freshIds = new Set()) {
    const items = visible();
    const banner = dutyBanner();
    if (!items.length) {
      const text = { open: 'No open emergency reports.', new: 'No unacknowledged reports.', closed: 'No resolved or cancelled reports waiting to be archived.', all: 'No emergency reports received yet.' }[filter];
      listEl.replaceChildren(h('div', { class: 'flex-1 flex flex-col items-center justify-center text-center py-8 text-slate-500' },
        icon('notifications_paused', 'text-[2rem]'),
        h('p', { class: 'text-xs mt-1', text }),
        h('p', { class: 'text-[0.625rem] mt-1 font-mono', text: 'Reports from the Citizen Emergency App appear here automatically.' })));
      if (banner) listEl.prepend(banner);
      return;
    }
    listEl.replaceChildren(...[banner, ...items.map((i) => card(i, freshIds.has(i.id)))].filter(Boolean));
  }

  function card(i, fresh) {
    const isNew = i.status === 'new';
    const closed = CLOSED.has(i.status);
    const hot = urgent(i) && !closed;
    const selected = i.id === selectedId;
    const loc = i.has_location
      ? h('span', { class: `flex items-center gap-0.5 ${i.location_verified ? 'text-emerald-400' : 'text-slate-400'}`, title: i.location_verified ? 'GPS location verified' : 'GPS location (unverified)' },
          icon(i.location_verified ? 'where_to_vote' : 'location_on', 'text-[0.8125rem]'), i.location_verified ? 'GPS ✓' : 'GPS')
      : h('span', { class: 'flex items-center gap-0.5 text-red-400', title: 'No usable GPS coordinates' }, icon('location_off', 'text-[0.8125rem]'), 'NO LOC');

    return h('button', {
      type: 'button',
      'data-incident-id': i.id,
      'aria-pressed': String(selected),
      onclick: () => select(i.id),
      class: [
        'shrink-0 w-full text-left rounded-md border-l-4 border p-2.5 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan',
        hot ? 'border-l-red-500' : isNew ? 'border-l-red-400' : closed ? 'border-l-slate-600' : 'border-l-brand-cyan',
        isNew ? 'bg-red-950/40 border-red-500/40' : closed ? 'bg-[#11151f] border-brand-borderSubtle opacity-70' : 'bg-[#141a2b] border-brand-borderSubtle',
        selected ? '!border-brand-cyan ring-1 ring-brand-cyan shadow-[0_0_12px_rgba(0,229,255,0.35)]' : 'hover:border-brand-cyan/50',
        fresh ? 'animate-[pulse_1s_ease-in-out_3]' : '',
      ].join(' '),
    },
      h('div', { class: 'flex items-center gap-1.5 flex-wrap' },
        h('span', { class: `font-mono text-[0.6875rem] font-bold ${hot || isNew ? 'text-red-400' : 'text-brand-cyan'}`, text: i.code }),
        statusBadge(i.status),
        i.priority && h('span', { class: `px-1 rounded text-[0.5625rem] font-mono font-bold ${i.priority === 1 ? 'bg-red-600 text-white' : i.priority === 2 ? 'bg-red-500/20 text-red-300' : 'bg-slate-700/60 text-slate-300'}`, text: `P${i.priority}` }),
        h('span', { class: 'ml-auto text-[0.625rem] font-mono text-slate-400 whitespace-nowrap', title: fmtDateTime(i.created_at), text: `${fmtTime(i.created_at)} · ${ago(i.created_at)}` })),
      h('div', { class: 'flex items-center justify-between gap-2 mt-1' },
        h('span', { class: 'text-sm font-semibold text-white truncate', text: i.emergency_type }),
        h('span', { class: 'text-[0.625rem] font-mono shrink-0' }, loc)),
      i.description && h('p', { class: 'text-[0.6875rem] text-slate-400 mt-0.5 line-clamp-1 break-words', text: i.description }),
      !closed && h('p', { class: `mt-1 flex items-center gap-1 text-[0.625rem] font-mono ${i.assigned_to ? 'text-slate-400' : 'text-amber-300'}` },
        icon(i.assigned_to ? 'person' : 'person_off', 'text-[0.8125rem]'),
        i.assigned_to ? (i.assigned_to === cfg.me ? 'Assigned to you' : `Assigned to ${i.assigned_to_name || 'a dispatcher'}`) : 'Unassigned'));
  }

  // ------------------------------------------------------------ map

  const otherLayer = L.layerGroup().addTo(map);   // small dots for other open incidents
  let selectedMarker = null;
  let accuracyCircle = null;

  function renderMarkers() {
    otherLayer.clearLayers();
    for (const i of incidents.values()) {
      if (CLOSED.has(i.status) || i.id === selectedId) continue;
      const c = coords(i);
      if (!c) continue;
      L.circleMarker(c, { radius: 6, color: '#7f1d1d', weight: 2, fillColor: '#f87171', fillOpacity: 0.85 })
        .bindTooltip(`${i.code} · ${i.emergency_type}`, { direction: 'top' })
        .on('click', () => select(i.id))
        .addTo(otherLayer);
    }
    document.dispatchEvent(new CustomEvent('esrms:incidents-rendered'));   // organizations.js redraws its bases
  }

  function popupContent(i) {
    return h('div', { class: 'space-y-0.5' },
      h('div', { class: 'font-mono font-bold text-red-400', text: i.code }),
      h('div', { class: 'font-semibold text-white text-sm', text: i.emergency_type }),
      h('div', { text: `Reported ${fmtDateTime(i.created_at)}` }),
      h('div', {}, 'Status: ', h('b', { text: STATUS[i.status] || i.status })));
  }

  function removeSelectedMarker() {
    if (selectedMarker) map.removeLayer(selectedMarker);
    if (accuracyCircle) map.removeLayer(accuracyCircle);
    selectedMarker = accuracyCircle = null;
  }

  function locate(i, { move = true } = {}) {
    const c = coords(i);
    removeSelectedMarker();
    if (!c) {
      // Never fall back to the city centre: that would look like a real location.
      showLocationAlert(`Citizen location unavailable for ${i.code}.`);
      return false;
    }
    hideLocationAlert();
    selectedMarker = L.marker(c, {
      icon: L.divIcon({ className: '', html: '<span class="esrms-sos-dot"></span>', iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -16] }),
      zIndexOffset: 1000,
      keyboard: false,
      title: `${i.code} citizen location`,
    }).bindPopup(popupContent(i), { className: 'esrms-popup', autoClose: false, closeOnClick: false }).addTo(map);
    const acc = Number(i.location_accuracy_m);
    if (Number.isFinite(acc) && acc > 0 && acc < 5000) {
      accuracyCircle = L.circle(c, { radius: acc, color: '#ef4444', weight: 1, fillOpacity: 0.08, interactive: false }).addTo(map);
    }
    if (move) map.flyTo(c, Math.max(STREET_ZOOM, map.getZoom()), { duration: 0.8 });
    selectedMarker.openPopup();
    return true;
  }

  let alertTimer;
  function showLocationAlert(text) {
    alertEl.querySelector('[data-text]').textContent = text;
    alertEl.classList.remove('hidden');
    clearTimeout(alertTimer);
    alertTimer = setTimeout(hideLocationAlert, 8000);
  }
  function hideLocationAlert() { alertEl.classList.add('hidden'); }

  // "Davao City" button on the existing map: back to the default view.
  const DefaultView = L.Control.extend({
    options: { position: 'topleft' },
    onAdd() {
      const btn = L.DomUtil.create('button', 'esrms-map-btn');
      btn.type = 'button';
      btn.title = 'Return to the default Davao City view';
      btn.append(icon('home', 'text-[0.875rem]'), 'DAVAO CITY');
      L.DomEvent.disableClickPropagation(btn);
      L.DomEvent.on(btn, 'click', () => map.flyTo(cfg.map.center, cfg.map.zoom, { duration: 0.8 }));
      return btn;
    },
  });
  new DefaultView().addTo(map);

  // ------------------------------------------------------------ selection + detail

  async function select(id) {
    const i = incidents.get(id);
    if (!i) return;
    selectedId = id;
    detail = null;
    renderList();
    renderMarkers();
    locate(i);
    renderDetail(i, { loading: true });
    announceSelection();
    try {
      const { item } = await api(`${cfg.api.incidents}/${id}`);
      if (selectedId !== id) return;
      detail = item;
      renderDetail(item);
    } catch (err) {
      if (selectedId !== id) return;
      renderDetail(i, { error: err.message });
    }
  }

  function refreshSelected(i) {
    // Re-render with list data; keep the contact number/history we already fetched.
    detail = detail && detail.id === i.id ? { ...detail, ...i } : null;
    renderDetail(detail || i);
    if (selectedMarker) selectedMarker.setPopupContent(popupContent(i));
    else if (coords(i)) locate(i, { move: false });   // coordinates arrived after selection
  }

  function clearSelection() {
    selectedId = null;
    detail = null;
    removeSelectedMarker();
    hideLocationAlert();
    detailEl.classList.add('hidden');
    detailEl.replaceChildren();
    renderList();
    renderMarkers();
    announceSelection();
  }

  function renderDetail(i, { loading = false, error = null } = {}) {
    const c = coords(i);
    const closed = CLOSED.has(i.status);
    const row = (k, v) => v != null && v !== '' && h('div', { class: 'flex gap-2 text-xs py-0.5' },
      h('dt', { class: 'w-24 shrink-0 text-slate-500 font-mono text-[0.625rem] uppercase pt-px', text: k }),
      h('dd', { class: 'text-slate-200 min-w-0 break-words' }, v));

    const controls = cfg.canDispatch && h('div', { class: 'mt-3 pt-3 border-t border-brand-borderSubtle flex flex-col gap-2' },
      i.status === 'new' && h('button', {
        type: 'button', class: 'w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-md text-sm font-bold bg-gradient-to-r from-[#00D9FF] to-[#1685FF] hover:brightness-110 text-[#050A12]',
        onclick: (e) => setStatus(i, 'acknowledged', e.currentTarget),
      }, icon('task_alt', 'text-[1.125rem]'), i.assigned_to ? 'Acknowledge' : 'Acknowledge & take'),
      !closed && assignControls(i),
      h('div', { class: 'flex gap-2' },
        h('label', { class: 'sr-only', for: 'incident-status-select', text: 'Status' }),
        h('select', { id: 'incident-status-select', class: 'flex-1 px-2 py-1.5 rounded-md bg-[#0a1020] border border-brand-borderSubtle text-xs text-white focus:outline-none focus:border-brand-cyan' },
          ...Object.entries(STATUS).map(([k, v]) => h('option', { value: k, selected: k === i.status, text: v }))),
        h('button', {
          type: 'button', class: 'px-3 py-1.5 rounded-md text-xs font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10',
          onclick: (e) => setStatus(i, document.getElementById('incident-status-select').value, e.currentTarget),
        }, 'Update status')),
      closed && h('button', {
        type: 'button', class: 'w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-semibold border border-slate-500/50 text-slate-200 hover:bg-white/5',
        onclick: (e) => archive(i, e.currentTarget),
      }, icon('inventory_2', 'text-[1rem]'), 'Archive to Incident Report'));

    const events = Boolean(detail?.events?.length) && h('ol', { class: 'mt-2 border-l border-brand-borderSubtle ml-1 space-y-1' },
      ...detail.events.map((e) => h('li', { class: 'pl-3 text-[0.6875rem] text-slate-400 relative' },
        h('span', { class: 'absolute -left-[0.1875rem] top-1.5 w-1.5 h-1.5 rounded-full bg-brand-cyan' }),
        h('span', { class: 'font-mono text-slate-500', text: `${fmtDateTime(e.created_at)} ` }),
        eventText(e), e.actor_name ? ` · ${e.actor_name}` : '')));

    detailEl.replaceChildren(h('div', { class: 'p-3.5' },
      h('div', { class: 'flex items-start justify-between gap-2' },
        h('div', { class: 'min-w-0' },
          h('div', { class: 'flex items-center gap-1.5 flex-wrap' },
            h('span', { class: 'font-mono text-xs font-bold text-red-400', text: i.code }), statusBadge(i.status),
            i.priority && h('span', { class: 'text-[0.625rem] font-mono text-slate-300', text: PRIORITY[i.priority] })),
          h('h3', { class: 'text-base font-bold text-white mt-0.5 break-words', text: i.emergency_type })),
        h('button', { type: 'button', class: 'p-1 rounded text-slate-400 hover:text-white hover:bg-white/10', 'aria-label': 'Close incident details', onclick: clearSelection }, icon('close', 'text-[1.125rem]'))),
      i.description && h('p', { class: 'text-xs text-slate-300 mt-1.5 whitespace-pre-line break-words', text: i.description }),
      error && h('p', { class: 'text-xs text-red-300 mt-2', role: 'alert', text: error }),
      h('dl', { class: 'mt-2' },
        row('Reported', fmtDateTime(i.created_at)),
        row('Location', c
          ? h('span', {}, h('span', { class: 'font-mono', text: `${c[0].toFixed(6)}, ${c[1].toFixed(6)}` }),
              h('span', { class: i.location_verified ? 'text-emerald-400' : 'text-amber-300', text: i.location_verified ? ' · verified' : ' · unverified' }),
              i.location_accuracy_m ? ` · ±${Math.round(i.location_accuracy_m)} m` : '',
              h('button', { type: 'button', class: 'ml-2 text-brand-cyan underline', onclick: () => locate(i), text: 'Locate' }))
          : h('span', { class: 'text-red-400 font-semibold', text: 'Citizen location unavailable' })),
        row('Citizen', i.citizen_name),
        row('Contact', loading ? h('span', { class: 'text-slate-500', text: 'Loading…' })
          : detail?.contact_number ? h('a', { href: `tel:${detail.contact_number.replace(/[^\d+]/g, '')}`, class: 'font-mono text-brand-cyan underline', text: detail.contact_number }) : null),
        row('Acknowledged', detail?.acknowledged_at && `${fmtDateTime(detail.acknowledged_at)}${detail.acknowledged_by_name ? ` by ${detail.acknowledged_by_name}` : ''}`),
        !closed && row('Assigned', i.assigned_to
          ? `${i.assigned_to === cfg.me ? 'You' : (detail?.assigned_to_name || i.assigned_to_name || 'A dispatcher')}${i.assigned_at ? ` · since ${fmtTime(i.assigned_at)}` : ''}`
          : h('span', { class: 'text-amber-300', text: 'Unassigned' }))),
      photoSection(i),
      events,
      nearbyEl,
      controls));
    detailEl.classList.remove('hidden');
  }

  // ------------------------------------------------------------ incident photos

  // Photos the citizen attached to the report (item.photos, public URLs in the media bucket).
  // Like nearbyEl, one node is kept per photo list and re-attached, so a re-render doesn't reload the images.
  let photosEl = null;
  let photosKey = null;

  function photoSection(i) {
    const urls = i.photos || [];
    const key = `${i.id}|${urls.join('|')}`;
    if (key === photosKey) return photosEl;
    photosKey = key;
    photosEl = h('section', { class: 'mt-3 pt-3 border-t border-brand-borderSubtle', 'aria-label': 'Incident photo' },
      h('h4', { class: 'flex items-center gap-1.5 text-[0.6875rem] font-mono font-bold uppercase tracking-widest text-brand-cyan mb-1.5' },
        icon('photo_camera', 'text-[0.9375rem]'), urls.length > 1 ? `Incident Photos (${urls.length})` : 'Incident Photo'),
      urls.length
        ? h('div', { class: 'flex flex-wrap items-start gap-2' }, ...urls.map((_, n) => photoThumb(urls, n)))
        : h('p', { class: 'text-xs text-slate-500', text: 'No photo attached.' }));
    return photosEl;
  }

  function photoThumb(urls, n) {
    const state = h('span', { class: 'flex items-center justify-center h-20 px-2 text-center text-[0.625rem] font-mono text-slate-500 animate-pulse', role: 'status', text: 'Loading photo…' });
    const img = h('img', { src: urls[n], alt: `Incident photo ${n + 1}`, class: 'hidden w-full h-auto max-h-44 object-contain' });
    const btn = h('button', {
      type: 'button', title: 'Click to enlarge', 'aria-label': `Enlarge incident photo ${n + 1}`,
      class: 'w-32 shrink-0 overflow-hidden rounded-md border border-brand-borderSubtle bg-[#0a1020] cursor-pointer transition hover:border-brand-cyan hover:brightness-110 hover:shadow-[0_0_10px_rgba(0,229,255,0.3)] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan disabled:cursor-default disabled:hover:brightness-100 disabled:hover:shadow-none disabled:hover:border-red-500/40',
      onclick: () => openViewer(urls, n),
    }, state, img);
    img.addEventListener('load', () => { state.remove(); img.classList.remove('hidden'); });
    img.addEventListener('error', () => {
      btn.disabled = true;
      btn.title = '';
      btn.classList.add('border-red-500/40');
      state.className = 'flex items-center justify-center h-20 px-2 text-center text-[0.625rem] text-red-300';
      state.setAttribute('role', 'alert');
      state.textContent = 'Photo could not be loaded.';
    });
    return btn;
  }

  // Enlarged photo. A modal <dialog>: Escape closes it and focus returns to the thumbnail.
  let viewerUrls = [];
  let viewerAt = 0;
  const viewerImg = h('img', { alt: '', class: 'hidden max-w-full max-h-full object-contain rounded-lg shadow-2xl' });
  const viewerState = h('p', { class: 'text-sm font-mono text-slate-300', role: 'status' });
  const viewerCount = h('span', { class: 'absolute bottom-3 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded bg-black/70 text-xs font-mono text-slate-200' });
  const viewerBtn = (label, name, cls, onclick) => h('button', {
    type: 'button', 'aria-label': label, title: label, onclick,
    class: `absolute ${cls} flex items-center justify-center w-10 h-10 rounded-full bg-black/70 border border-white/30 text-white hover:bg-brand-cyan hover:text-[#050A12] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan`,
  }, icon(name, 'text-[1.5rem]'));
  const viewerPrev = viewerBtn('Previous photo', 'chevron_left', 'left-3 top-1/2 -translate-y-1/2', () => showPhoto(viewerAt - 1));
  const viewerNext = viewerBtn('Next photo', 'chevron_right', 'right-3 top-1/2 -translate-y-1/2', () => showPhoto(viewerAt + 1));
  const viewerStage = h('div', { class: 'w-full h-full flex items-center justify-center p-4 sm:p-14' }, viewerState, viewerImg);
  const viewerEl = h('dialog', { class: 'm-0 p-0 w-screen h-screen max-w-none max-h-none bg-black/85 backdrop:bg-black/60 text-slate-100', 'aria-label': 'Incident photo' },
    viewerStage, viewerPrev, viewerNext, viewerCount,
    viewerBtn('Close photo', 'close', 'top-3 right-3', () => viewerEl.close()));
  // Anywhere outside the photo and the controls closes the viewer.
  viewerStage.addEventListener('click', (e) => { if (e.target !== viewerImg) viewerEl.close(); });
  viewerEl.addEventListener('keydown', (e) => {
    if (viewerUrls.length < 2) return;
    if (e.key === 'ArrowLeft') showPhoto(viewerAt - 1);
    if (e.key === 'ArrowRight') showPhoto(viewerAt + 1);
  });
  // As large as the screen allows, at the photo's own aspect ratio.
  function fitViewer() {
    if (!viewerEl.open || !viewerImg.naturalWidth) return;
    const pad = getComputedStyle(viewerStage);
    const w = viewerStage.clientWidth - parseFloat(pad.paddingLeft) - parseFloat(pad.paddingRight);
    const ht = viewerStage.clientHeight - parseFloat(pad.paddingTop) - parseFloat(pad.paddingBottom);
    const scale = Math.min(w / viewerImg.naturalWidth, ht / viewerImg.naturalHeight);
    viewerImg.style.width = `${Math.floor(viewerImg.naturalWidth * scale)}px`;
    viewerImg.style.height = `${Math.floor(viewerImg.naturalHeight * scale)}px`;
  }
  window.addEventListener('resize', fitViewer);
  viewerImg.addEventListener('load', () => { fitViewer(); viewerState.classList.add('hidden'); viewerImg.classList.remove('hidden'); });
  viewerImg.addEventListener('error', () => {
    viewerState.textContent = 'This photo could not be loaded. Check your connection and try again.';
    viewerState.className = 'text-sm text-red-300';
  });
  document.body.append(viewerEl);

  function showPhoto(n) {
    viewerAt = (n + viewerUrls.length) % viewerUrls.length;
    const many = viewerUrls.length > 1;
    for (const el of [viewerPrev, viewerNext, viewerCount]) el.classList.toggle('hidden', !many);
    viewerCount.textContent = `${viewerAt + 1} / ${viewerUrls.length}`;
    viewerImg.classList.add('hidden');
    viewerState.className = 'text-sm font-mono text-slate-300 animate-pulse';
    viewerState.textContent = 'Loading photo…';
    viewerImg.alt = `Incident photo ${viewerAt + 1} of ${viewerUrls.length}`;
    viewerImg.src = viewerUrls[viewerAt];
  }

  function openViewer(urls, n) {
    viewerUrls = urls;
    showPhoto(n);
    if (!viewerEl.open) viewerEl.showModal();
  }

  function eventText(e) {
    if (e.event === 'reported') return 'Report received';
    if (e.event === 'assigned') return `Assigned to ${e.to_assignee_name || 'a dispatcher'}`;
    if (e.event === 'reassigned') return `Reassigned from ${e.from_assignee_name || 'a dispatcher'} to ${e.to_assignee_name || 'a dispatcher'}`;
    if (e.event === 'unassigned') return `Unassigned from ${e.from_assignee_name || 'a dispatcher'}`;
    if (e.event === 'archived') return 'Archived';
    if (e.event === 'restored') return 'Restored from archive';
    return `${STATUS[e.from_status] || e.from_status || '—'} → ${STATUS[e.to_status] || e.to_status}`;
  }

  // ------------------------------------------------------------ incident actions

  async function busy(btn, fn) {
    if (btn) { btn.disabled = true; btn.classList.add('opacity-60', 'cursor-wait'); }
    try { return await fn(); } finally { if (btn) { btn.disabled = false; btn.classList.remove('opacity-60', 'cursor-wait'); } }
  }

  async function setStatus(i, status, btn) {
    if (status === i.status) return toast(`Already ${STATUS[status]}.`, 'info');
    ownChanges.add(`${i.id}:${status}`);
    setTimeout(() => ownChanges.delete(`${i.id}:${status}`), 15000);
    await busy(btn, async () => {
      try {
        const { item } = await api(`${cfg.api.incidents}/${i.id}/status`, { method: 'POST', body: { status } });
        // Only reached after the database confirmed the update.
        incidents.set(item.id, { ...incidents.get(item.id), ...item });
        if (detail?.id === item.id) detail = { ...detail, ...item };
        renderList();
        if (selectedId === item.id) refreshSelected(incidents.get(item.id));
        toast(`${item.code} marked ${STATUS[item.status]}.`, 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    loadIncidents();
    if (selectedId === i.id) reloadDetail(i.id);
  }

  // Reassignment: only to dispatchers who are On Duty right now. The server re-checks that and the
  // current assignee in one database transaction, so a stale list can never assign to an unavailable dispatcher.
  function assignControls(i) {
    const wrap = h('div', { class: 'flex gap-2' });
    const open = h('button', {
      type: 'button', class: 'w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-semibold border border-brand-borderSubtle text-slate-200 hover:bg-white/5',
      onclick: () => showAssign(i, wrap),
    }, icon('swap_horiz', 'text-[1rem]'), i.assigned_to ? 'Reassign…' : 'Assign…');
    wrap.append(open);
    return wrap;
  }

  async function showAssign(i, wrap) {
    wrap.replaceChildren(h('span', { class: 'text-xs text-slate-400 py-1.5', text: 'Loading On Duty dispatchers…' }));
    let items;
    try {
      ({ items } = await api(cfg.api.available));
    } catch (err) {
      toast(err.message, 'error');
      wrap.replaceChildren(assignControls(i));
      return;
    }
    const choices = items.filter((d) => d.id !== i.assigned_to);
    if (!choices.length) {
      wrap.replaceChildren(h('p', { class: 'text-xs text-amber-300 py-1', text: 'No other dispatcher is On Duty right now.' }));
      return;
    }
    const sel = h('select', { class: 'flex-1 px-2 py-1.5 rounded-md bg-[#0a1020] border border-brand-borderSubtle text-xs text-white focus:outline-none focus:border-brand-cyan', 'aria-label': 'Assign to' },
      ...choices.map((d) => h('option', { value: d.id, text: d.me ? `${d.name} (you)` : d.name })));
    wrap.replaceChildren(sel, h('button', {
      type: 'button', class: 'px-3 py-1.5 rounded-md text-xs font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10',
      onclick: (e) => assignTo(i, sel.value, sel.selectedOptions[0].textContent, e.currentTarget),
    }, 'Assign'));
  }

  async function assignTo(i, assigneeId, name, btn) {
    const from = i.assigned_to ? (i.assigned_to === cfg.me ? 'you' : (i.assigned_to_name || 'the current dispatcher')) : null;
    const text = from ? `${i.code} will move from ${from} to ${name}. The change is recorded in the incident history.` : `${i.code} will be assigned to ${name}.`;
    if (!(await confirmDialog(from ? 'Reassign this incident?' : 'Assign this incident?', text, from ? 'Reassign' : 'Assign'))) return;
    await busy(btn, async () => {
      try {
        const { item } = await api(`${cfg.api.incidents}/${i.id}/assign`, { method: 'POST', body: { assignee_id: assigneeId, expected_assignee_id: i.assigned_to || null } });
        toast(`${item.code} assigned to ${item.assigned_to_name || name}.`, 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    loadIncidents();
    if (selectedId === i.id) reloadDetail(i.id);
  }

  // Refresh contact/history of the selected incident without moving the map.
  async function reloadDetail(id) {
    try {
      const { item } = await api(`${cfg.api.incidents}/${id}`);
      if (selectedId !== id) return;
      detail = item;
      renderDetail(item);
    } catch (_) { /* the list refresh already reported any error */ }
  }

  async function archive(i, btn) {
    if (!(await confirmDialog('Archive this incident?', `${i.code} will move to the Incident Report tab. You can restore it from there.`, 'Archive'))) return;
    await busy(btn, async () => {
      try {
        await api(`${cfg.api.incidents}/${i.id}/archive`, { method: 'POST', body: {} });
        toast(`${i.code} archived to Incident Report.`, 'success');
        if (selectedId === i.id) clearSelection();
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    loadIncidents();
    if (historyLoaded) loadHistory();
  }

  const confirmEl = document.getElementById('dispatch-confirm');
  function confirmDialog(title, text, ok) {
    confirmEl.querySelector('[data-title]').textContent = title;
    confirmEl.querySelector('[data-text]').textContent = text;
    confirmEl.querySelector('[data-ok]').textContent = ok;
    confirmEl.returnValue = '';
    confirmEl.showModal();
    return new Promise((resolve) => confirmEl.addEventListener('close', () => resolve(confirmEl.returnValue === 'ok'), { once: true }));
  }

  // ------------------------------------------------------------ Incident Report history

  const historyList = document.getElementById('history-list');
  const historyFilters = document.getElementById('history-filters');
  let historyLoaded = false;

  async function loadHistory() {
    historyLoaded = true;
    const params = new URLSearchParams();
    for (const [k, v] of new FormData(historyFilters)) if (v) params.set(k, v);
    try {
      const { items } = await api(`${cfg.api.history}?${params}`);
      if (!items.length) {
        historyList.replaceChildren(h('p', { class: 'p-6 text-sm text-slate-400 text-center', text: 'No archived incidents yet. Resolve or cancel an incident, then archive it from the Dispatch Center.' }));
        return;
      }
      const th = (t) => h('th', { class: 'px-3 py-2 text-left font-mono text-[0.625rem] uppercase tracking-wider text-slate-500', text: t });
      historyList.replaceChildren(h('table', { class: 'w-full text-sm' },
        h('thead', { class: 'border-b border-brand-borderSubtle' }, h('tr', {}, th('Incident'), th('Type'), th('Outcome'), th('Reported'), th('Archived'), th('Location'), th(''))),
        h('tbody', {}, ...items.map(historyRow))));
    } catch (err) {
      historyList.replaceChildren(h('p', { class: 'p-6 text-sm text-red-300', role: 'alert', text: err.message }));
    }
  }

  function historyRow(i) {
    const td = (...c) => h('td', { class: 'px-3 py-2 align-top' }, ...c);
    const tr = h('tr', { class: 'border-b border-brand-borderSubtle/60 hover:bg-white/[0.02]' },
      td(h('span', { class: 'font-mono text-xs text-brand-cyan', text: i.code })),
      td(h('div', { class: 'text-white', text: i.emergency_type }), i.description && h('div', { class: 'text-[0.6875rem] text-slate-500 line-clamp-1', text: i.description })),
      td(statusBadge(i.status)),
      td(h('span', { class: 'text-xs text-slate-300 whitespace-nowrap', text: fmtDateTime(i.created_at) })),
      td(h('span', { class: 'text-xs text-slate-300 whitespace-nowrap', text: fmtDateTime(i.archived_at) }), i.archived_by_name && h('div', { class: 'text-[0.625rem] text-slate-500', text: i.archived_by_name })),
      td(h('span', { class: `text-xs font-mono ${i.has_location ? 'text-slate-300' : 'text-red-400'}`, text: i.has_location ? `${Number(i.latitude).toFixed(5)}, ${Number(i.longitude).toFixed(5)}` : 'Unavailable' })),
      td(h('div', { class: 'flex gap-2 justify-end' },
        h('button', { type: 'button', class: 'text-xs text-brand-cyan underline', onclick: () => toggleTimeline(i, tr), text: 'Timeline' }),
        cfg.canDispatch && h('button', { type: 'button', class: 'text-xs text-slate-300 underline', onclick: () => restore(i), text: 'Restore' }),
        cfg.canDispatch && h('button', { type: 'button', class: 'text-xs text-red-400 underline', onclick: () => removeIncident(i), text: 'Delete' }))));
    return tr;
  }

  async function toggleTimeline(i, tr) {
    const next = tr.nextElementSibling;
    if (next?.dataset.timelineFor === i.id) { next.remove(); return; }
    const cell = h('td', { colspan: '7', class: 'px-6 py-3 bg-[#0a0f1a] text-xs text-slate-400', text: 'Loading…' });
    tr.after(h('tr', { 'data-timeline-for': i.id }, cell));
    try {
      const { item } = await api(`${cfg.api.incidents}/${i.id}`);
      cell.replaceChildren(h('ol', { class: 'space-y-1' }, ...item.events.map((e) => h('li', {},
        h('span', { class: 'font-mono text-slate-500', text: `${fmtDateTime(e.created_at)}  ` }), eventText(e), e.actor_name ? ` · ${e.actor_name}` : ''))),
        item.contact_number && h('p', { class: 'mt-2' }, 'Contact: ', h('a', { href: `tel:${item.contact_number.replace(/[^\d+]/g, '')}`, class: 'text-brand-cyan underline font-mono', text: item.contact_number })));
    } catch (err) {
      cell.textContent = err.message;
      return;
    }
    // Every hospital signal sent for this incident (signal-archive.js); the timeline stands without it.
    try {
      const signals = await window.ESRMSSignalArchive?.forIncident(i.id);
      if (signals && cell.isConnected) cell.append(signals);
    } catch (err) { /* shown in Recent Signals instead */ }
  }

  async function restore(i) {
    try {
      await api(`${cfg.api.incidents}/${i.id}/restore`, { method: 'POST', body: {} });
      toast(`${i.code} restored to the Dispatch Center.`, 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
    loadHistory();
    loadIncidents();
  }

  async function removeIncident(i) {
    if (!window.confirm(`Permanently delete ${i.code} and its timeline? This cannot be undone.`)) return;
    try {
      await api(`${cfg.api.incidents}/${i.id}`, { method: 'DELETE' });
      toast(`${i.code} deleted.`, 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
    loadHistory();
  }

  let historyDebounce;
  historyFilters.addEventListener('input', () => { clearTimeout(historyDebounce); historyDebounce = setTimeout(loadHistory, 300); });
  historyFilters.addEventListener('submit', (e) => { e.preventDefault(); loadHistory(); });

  // ------------------------------------------------------------ wiring

  document.querySelectorAll('[data-incident-filter]').forEach((btn) => btn.addEventListener('click', () => {
    filter = btn.dataset.incidentFilter;
    document.querySelectorAll('[data-incident-filter]').forEach((b) => b.setAttribute('aria-selected', String(b === btn)));
    renderList();
  }));
  document.getElementById('incident-refresh').addEventListener('click', () => loadIncidents());

  document.querySelectorAll('[data-tab]').forEach((tab) => tab.addEventListener('click', () => {
    if (tab.dataset.tab === 'reports') loadHistory();
    // Leaflet needs a size recalculation after its container was hidden.
    if (tab.dataset.tab === 'dispatch') setTimeout(() => map.invalidateSize(), 0);
  }));

  // Lets the Analytics tab open an incident in its existing view: the Dispatch Center
  // detail panel while it is live, the Incident Report once it has been archived.
  window.ESRMSDispatch = {
    // For organizations.js: the existing map, the selected incident and where its Nearby Organizations go.
    map,
    nearbyEl,
    coords,
    confirm: confirmDialog,
    selected: () => (selectedId && incidents.get(selectedId)) || null,
    active: () => [...incidents.values()].filter((i) => !CLOSED.has(i.status)),
    statusLabel: (status) => STATUS[status] || status,
    open({ id, code, archived }) {
      const openTab = (name) => document.querySelector(`[data-tab="${name}"]`)?.click();
      if (archived) {
        historyFilters.elements.q.value = code;
        historyFilters.elements.status.value = '';
        openTab('reports');   // the tab's click handler loads the history with this search
        return true;
      }
      if (!incidents.has(id)) return false;
      openTab('dispatch');
      setTimeout(() => select(id), 50);   // after the map has been resized
      return true;
    },
  };

  // One refetch per burst of realtime events.
  let changeTimer;
  function onChange() {
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => {
      loadIncidents();
      document.dispatchEvent(new CustomEvent('esrms:incidents-changed'));
      if (historyLoaded && !document.querySelector('[data-tab-panel="reports"]').classList.contains('hidden')) loadHistory();
    }, 250);
  }

  const liveEl = document.getElementById('dispatch-live');
  const stopLive = window.AnnouncementLive.start({
    ...cfg.realtime,
    onChange,
    onStatus: (s) => {
      const [dot, text, title] = {
        live: ['bg-emerald-400', 'LIVE', 'Receiving new reports in real time.'],
        connecting: ['bg-amber-400 animate-pulse', 'CONNECTING', 'Connecting to realtime…'],
        polling: ['bg-amber-400', 'POLLING 15s', 'Realtime unavailable; checking for new reports every 15 seconds.'],
      }[s];
      liveEl.firstElementChild.className = `w-1.5 h-1.5 rounded-full ${dot}`;
      liveEl.lastElementChild.textContent = text;
      liveEl.title = title;
    },
  });
  window.addEventListener('pagehide', () => { stopLive(); otherLayer.remove(); removeSelectedMarker(); }, { once: true });

  // Console status changed in the account menu: refetch under the new rules, without announcing
  // every report that becomes visible again as "new".
  document.addEventListener('esrms:duty-changed', () => {
    knownIds = null;
    if (selectedId) clearSelection();
    loadIncidents();
  });

  // Keep "x minutes ago" honest.
  setInterval(() => renderList(), 30000);

  loadIncidents();
})();
