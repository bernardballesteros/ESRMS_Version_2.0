// Dispatch Center: organization bases. The Organization Locations panel, the blue base markers on
// the existing map, the Nearby Organizations section of an incident and Send Signal.
// An organization is a fixed base (latitude/longitude entered by a dispatcher). No individual
// responder is located or tracked anywhere: every distance is incident -> organization base,
// calculated by Flask (/api/organizations/nearby). Nothing is kept in the browser.
(function () {
  const cfgEl = document.getElementById('org-config');
  const D = window.ESRMSDispatch;
  if (!cfgEl || !D || !D.map || !window.L || !window.AnnUI) return;
  const { h, icon, toast, api } = window.AnnUI;
  const cfg = JSON.parse(cfgEl.textContent);
  const opt = cfg.options;
  const map = D.map;
  const $ = (id) => document.getElementById(id);
  const LETTER = { ems: 'E', fire: 'F', police: 'P', barangay: 'B', hospital: 'H', other: 'O' };
  const STATUS_STYLE = {
    ready: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/40', limited: 'bg-amber-500/10 text-amber-300 border-amber-500/40',
    full: 'bg-red-500/10 text-red-300 border-red-500/40', offline: 'bg-slate-600/30 text-slate-300 border-slate-500',
  };
  const SIGNAL_STYLE = {
    sent: 'text-emerald-300 border-emerald-500/40', acknowledged: 'text-blue-300 border-blue-500/40', accepted: 'text-brand-cyan border-brand-cyan/40',
    responding: 'text-amber-300 border-amber-500/40', on_scene: 'text-violet-300 border-violet-500/40', arrived: 'text-amber-300 border-amber-500/40',
    handoff: 'text-violet-300 border-violet-500/40', completed: 'text-emerald-200 border-emerald-400/60', received: 'text-emerald-200 border-emerald-400/60',
    unable: 'text-red-300 border-red-500/40', cancelled: 'text-slate-300 border-slate-500',
  };

  let organizations = [];
  let nearby = null;            // last /nearby answer for the selected incident
  let nearbySeq = 0;
  let showBusy = false;
  let showOthers = false;

  const statusBadge = (org) => h('span', { class: `inline-flex items-center px-1.5 py-px rounded border text-[0.625rem] font-mono font-bold uppercase tracking-wide whitespace-nowrap ${STATUS_STYLE[org.availability] || STATUS_STYLE.offline}`, text: org.is_active === false ? 'Disabled' : org.status_label });
  const dot = (org, extra = '') => h('span', { class: `esrms-org-dot ${extra}`, 'aria-hidden': 'true', text: LETTER[org.org_type] || 'O' });
  const km = (d) => (d == null ? 'Distance unknown' : `${d.toFixed(1)} km`);
  // The organization's hotline as a link that dials it; shown where Send Signal used to be.
  const hotline = (org, cls) => (org.phone_number
    ? h('a', { href: `tel:${org.phone_number.replace(/[^\d+]/g, '')}`, class: `inline-flex items-center gap-1 ${cls}`, 'aria-label': `Call ${org.name} hotline ${org.phone_number}` }, icon('call', 'text-[0.8125rem]'), org.phone_number)
    : h('span', { class: 'text-[0.625rem] font-mono text-slate-400 whitespace-nowrap', text: 'No hotline recorded' }));
  const hasBase = (org) => org.has_location && org.latitude != null;

  // ------------------------------------------------------------ panel

  const listEl = $('org-list');

  async function loadOrganizations() {
    listEl.setAttribute('aria-busy', 'true');
    try {
      organizations = (await api(`${cfg.api.organizations}?all=1`)).items;
      renderPanel();
      renderMarkers();
    } catch (err) {
      if (!organizations.length) {
        listEl.replaceChildren(h('p', { class: 'text-xs text-red-300 p-2', role: 'alert', text: err.message }),
          h('button', { type: 'button', class: 'self-start ml-2 text-xs underline text-red-200', onclick: loadOrganizations, text: 'Retry' }));
      }
    } finally {
      listEl.setAttribute('aria-busy', 'false');
    }
  }

  function renderPanel() {
    if (!organizations.length) {
      listEl.replaceChildren(h('div', { class: 'flex-1 flex flex-col items-center justify-center text-center py-6 text-slate-400' },
        icon('location_off', 'text-[1.875rem]'),
        h('p', { class: 'text-xs mt-1', text: 'No organization locations have been registered yet.' }),
        cfg.canDispatch && h('button', { type: 'button', class: 'mt-2 text-xs text-brand-cyan underline', onclick: () => openOrg(), text: 'Add the first organization' })));
      return;
    }
    listEl.replaceChildren(...organizations.map((org) => h('div', { class: `shrink-0 flex items-start gap-2 rounded-md bg-[#141a2b] border border-brand-borderSubtle px-2.5 py-2 ${org.is_active ? '' : 'opacity-60'}` },
      h('button', {
        type: 'button', class: 'min-w-0 flex-1 flex items-start gap-2 text-left rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan',
        title: hasBase(org) ? 'Show this base on the map' : 'No base coordinates recorded', 'aria-label': `Show ${org.name} on the map`, onclick: () => focusOrg(org),
      },
        dot(org, 'mt-0.5'),
        h('span', { class: 'min-w-0 flex-1' },
          h('span', { class: 'block text-sm font-semibold text-white truncate', text: org.name }),
          h('span', { class: 'block text-[0.625rem] font-mono text-slate-400 uppercase tracking-wide', text: `${org.org_type_label}${org.verified ? '' : ' · not verified'}` }),
          h('span', { class: 'block text-[0.6875rem] text-slate-300 truncate', text: org.base_name || org.address || (hasBase(org) ? `${Number(org.latitude).toFixed(5)}, ${Number(org.longitude).toFixed(5)}` : 'No base location') }))),
      h('div', { class: 'shrink-0 flex flex-col items-end gap-1' }, statusBadge(org),
        cfg.canDispatch && h('button', { type: 'button', class: 'p-0.5 text-slate-400 hover:text-brand-cyan', title: 'View / edit organization', 'aria-label': `Edit ${org.name}`, onclick: () => openOrg(org) }, icon('edit', 'text-[1rem]'))))));
  }

  // ------------------------------------------------------------ map: blue base markers, legend, line to the recommendation

  const baseLayer = L.layerGroup().addTo(map);
  const lineLayer = L.layerGroup().addTo(map);
  const markers = new Map();
  const NEAR_KM = 10;           // a base farther than this from the emergency is not drawn
  let focusedId = null;         // base opened from a list: drawn even if it is farther away

  function distanceTo(org) {
    if (!nearby) return undefined;
    const entry = [...nearby.items, ...nearby.others].find((e) => e.org.id === org.id);
    return entry ? entry.distance_km : undefined;
  }

  function popup(org) {
    const incident = D.selected();
    const distance = incident ? distanceTo(org) : undefined;
    const line = (label, value) => value && h('div', {}, h('span', { class: 'text-slate-400', text: `${label}: ` }), value);
    return h('div', { class: 'space-y-0.5 min-w-[11.25rem]' },
      h('div', { class: 'font-semibold text-white text-sm', text: org.name }),
      h('div', { class: 'font-mono font-bold text-blue-300 text-[0.6875rem] uppercase', text: `${org.org_type_label} · organization base` }),
      line('Base', org.base_name), line('Address', [org.address, org.city].filter(Boolean).join(', ')),
      line('Status', h('b', { text: org.status_label })),
      distance !== undefined && line('Distance', distance == null ? 'unknown (incident has no GPS location)' : `${distance.toFixed(1)} km from ${incident.code}`),
      h('div', { class: 'pt-1.5 flex gap-3' },
        cfg.canDispatch && h('button', { type: 'button', class: 'text-brand-cyan underline', onclick: () => openOrg(org), text: 'View Organization' }),
        org.phone_number && hotline(org, 'text-brand-cyan underline font-semibold')));
  }

  // A base is drawn only while there is an emergency, and only if it is near it: the selected incident,
  // or any open one when none is selected. The recommended base and one the dispatcher asked to see
  // are drawn even when farther away.
  function emergencyPoints() {
    const incident = D.selected();
    return (incident ? [incident] : D.active()).map(D.coords).filter(Boolean);
  }

  function shownOnMap(org, points) {
    if (!points.length) return false;
    if (org.id === focusedId) return true;
    if (D.selected() && nearby && nearby.items.some((e) => e.recommended && e.org.id === org.id)) return true;
    return points.some((p) => map.distance(p, [org.latitude, org.longitude]) <= NEAR_KM * 1000);
  }

  function renderMarkers() {
    const open = [...markers].find(([, m]) => m.isPopupOpen())?.[0];   // redrawn on every incident refresh
    baseLayer.clearLayers();
    markers.clear();
    const points = emergencyPoints();
    for (const org of organizations) {
      if (!org.is_active || !hasBase(org) || !shownOnMap(org, points)) continue;
      const dim = org.availability === 'ready' ? '' : ' esrms-org-dot--dim';
      const marker = L.marker([org.latitude, org.longitude], {
        icon: L.divIcon({ className: '', html: `<span class="esrms-org-dot esrms-org-dot--map${dim}">${LETTER[org.org_type] || 'O'}</span>`, iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -12] }),
        title: `${org.name} (${org.org_type_label} base)`, zIndexOffset: 400,
      }).bindPopup(() => popup(org), { className: 'esrms-popup esrms-org-popup' }).addTo(baseLayer);
      markers.set(org.id, marker);
    }
    markers.get(open)?.openPopup();
    drawLine();
  }

  function drawLine() {
    lineLayer.clearLayers();
    const incident = D.selected();
    const best = nearby && nearby.items.find((e) => e.recommended);
    const from = incident && D.coords(incident);
    if (!from || !best || !hasBase(best.org)) return;
    L.polyline([from, [best.org.latitude, best.org.longitude]], { color: '#3b82f6', weight: 2.5, dashArray: '6 7', interactive: false }).addTo(lineLayer);
  }

  function focusOrg(org) {
    if (!hasBase(org)) return toast(`${org.name} has no base coordinates yet. Edit it to add them.`, 'info');
    if (!emergencyPoints().length) return toast('Organization bases are shown on the map only while there is an active emergency with a location.', 'info');
    focusedId = org.id;
    renderMarkers();
    const incident = D.selected();
    const from = incident && D.coords(incident);
    if (from) map.flyToBounds([from, [org.latitude, org.longitude]], { padding: [70, 70], maxZoom: 16, duration: 0.8 });
    else map.flyTo([org.latitude, org.longitude], Math.max(15, map.getZoom()), { duration: 0.8 });
    markers.get(org.id)?.openPopup();
  }

  const Legend = L.Control.extend({
    options: { position: 'topright' },   // the incident detail panel covers the bottom-left corner
    onAdd() {
      const box = L.DomUtil.create('div', 'esrms-map-legend');
      const row = (mark, text) => h('div', { class: 'esrms-map-legend__row' }, mark, h('span', { text }));
      const body = h('div', { id: 'map-legend-body' });
      const toggle = h('button', { type: 'button', class: 'esrms-map-legend__toggle', 'aria-controls': 'map-legend-body' });
      const setOpen = (open) => {
        body.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        toggle.title = open ? 'Hide the legend' : 'Show the legend';
        toggle.replaceChildren(h('span', { text: 'LEGEND' }), icon(open ? 'expand_less' : 'expand_more', 'text-[1rem]'));
      };
      toggle.addEventListener('click', () => setOpen(body.hidden));
      setOpen(true);
      box.append(toggle, body);
      body.append(
        row(h('span', { class: 'esrms-legend-sos' }), 'Active Emergency'),
        ...Object.entries(opt.baseLabels).map(([key, text]) => row(h('span', { class: 'esrms-org-dot', text: LETTER[key] }), text)),
        h('div', { class: 'esrms-map-legend__note', text: `Blue = fixed organization base, not a person. Shown only within ${NEAR_KM} km of an active emergency` }));
      L.DomEvent.disableClickPropagation(box);
      return box;
    },
  });
  new Legend().addTo(map);

  // ------------------------------------------------------------ nearby organizations for the selected incident

  const nearbyEl = D.nearbyEl;

  async function loadNearby() {
    const incident = D.selected();
    const seq = ++nearbySeq;
    if (!incident || !cfg.canDispatch) { nearby = null; nearbyEl.replaceChildren(); drawLine(); return; }
    if (!nearby || nearby.incident.id !== incident.id) {
      nearby = null;
      drawLine();
      nearbyEl.replaceChildren(heading(), h('p', { class: 'text-[0.6875rem] text-slate-400', text: 'Calculating nearby organizations…' }));
    }
    try {
      const data = await api(`${cfg.api.nearby}?${new URLSearchParams({ incident_id: incident.id })}`);
      if (seq !== nearbySeq) return;
      nearby = data;
      renderNearby();
    } catch (err) {
      if (seq !== nearbySeq) return;
      nearbyEl.replaceChildren(heading(), h('p', { class: 'text-[0.6875rem] text-red-300', role: 'alert', text: err.message }),
        h('button', { type: 'button', class: 'text-[0.6875rem] underline text-red-200', onclick: loadNearby, text: 'Retry' }));
    }
    renderMarkers();   // the recommended base is drawn even when it is not near
  }

  const heading = () => h('h4', { class: 'flex items-center gap-1.5 text-[0.6875rem] font-mono font-bold uppercase tracking-widest text-brand-cyan mb-1.5' },
    icon('hub', 'text-[0.9375rem]'), 'Nearby Organizations');

  function entryRow(e, rank) {
    const org = e.org;
    const signal = e.signal;
    return h('li', { class: `rounded-md border p-2 ${e.recommended ? 'border-brand-cyan/70 bg-brand-cyan/5' : 'border-brand-borderSubtle bg-[#141a2b]'}` },
      e.recommended && h('p', { class: 'mb-1 text-[0.5625rem] font-mono font-bold tracking-widest text-brand-cyan', text: 'RECOMMENDED' }),
      h('div', { class: 'flex items-start gap-2' },
        rank != null && !e.recommended && h('span', { class: 'mt-0.5 w-4 text-center text-[0.6875rem] font-mono text-slate-400', text: String(rank) }),
        h('button', { type: 'button', class: 'min-w-0 flex-1 flex items-start gap-2 text-left rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-cyan', 'aria-label': `Show ${org.name} on the map`, onclick: () => focusOrg(org) },
          dot(org, 'mt-0.5'),
          h('span', { class: 'min-w-0 flex-1' },
            h('span', { class: 'block text-xs font-semibold text-white break-words', text: org.name }),
            h('span', { class: 'block text-[0.625rem] font-mono text-slate-400 uppercase', text: org.org_type_label }),
            h('span', { class: 'block text-[0.6875rem] text-slate-200' }, h('b', { class: 'font-mono', text: km(e.distance_km) }), e.distance_km != null ? ' from incident' : ''),
            h('span', { class: 'block text-[0.625rem] text-slate-400', text: e.reason || e.note }))),
        h('div', { class: 'shrink-0 flex flex-col items-end gap-1' }, statusBadge(org),
          signal && h('span', { class: `inline-flex px-1.5 py-px rounded border text-[0.625rem] font-mono whitespace-nowrap ${SIGNAL_STYLE[signal.status] || ''}`, title: `${signal.signal_type_label} sent for this incident`, text: `${signal.code} · ${signal.status_label}` }),
          hotline(org, 'px-2 py-1 rounded-md text-[0.6875rem] font-mono font-bold bg-gradient-to-r from-[#00D9FF] to-[#1685FF] text-[#050A12] hover:brightness-110 whitespace-nowrap'))));
  }

  function renderNearby() {
    const n = nearby;
    // Only the recommended responder is listed; the other suitable ones (available or busy) sit behind a toggle.
    const best = n.items.find((e) => e.recommended);
    const busy = n.items.filter((e) => !e.recommended);
    const toggle = (label, on, flip) => h('button', { type: 'button', 'aria-expanded': String(on), class: 'px-2 py-1 rounded border border-brand-cyan/50 text-[0.625rem] font-mono font-bold uppercase text-brand-cyan hover:bg-brand-cyan/10', onclick: () => { flip(); renderNearby(); } }, label);
    const parts = [heading()];
    if (n.state === 'none_registered') {
      parts.push(h('p', { class: 'text-[0.6875rem] text-slate-300', text: 'No organization locations have been registered yet.' }),
        h('button', { type: 'button', class: 'mt-1 text-[0.6875rem] text-brand-cyan underline', onclick: () => openOrg(), text: 'Add an organization' }));
    } else {
      parts.push(h('p', { class: 'mb-1.5 text-[0.625rem] text-slate-400', text: `${n.category}: recommends ${n.primary.join(', ')}${n.support.length ? ` · support ${n.support.join(', ')}` : ''}. Distance is straight-line from the incident to each organization's base.${n.located ? '' : ' This incident has no GPS location, so no distance can be calculated.'}` }));
      if (!best) {
        parts.push(h('p', { class: 'p-2 rounded-md border border-red-500/50 bg-red-950/40 text-xs font-mono font-bold text-red-200', role: 'alert', text: 'NO AVAILABLE ORGANIZATION' }));
      } else {
        parts.push(h('ol', { class: 'space-y-1.5' }, entryRow(best, null)));
      }
      parts.push(h('div', { class: 'mt-2 flex flex-wrap gap-1.5' },
        busy.length ? toggle(`${showBusy ? 'Hide' : 'View'} other suitable organizations (${busy.length})`, showBusy, () => { showBusy = !showBusy; }) : null,
        n.others.length ? toggle(`Manual organization selection (${n.others.length})`, showOthers, () => { showOthers = !showOthers; }) : null));
      if (showBusy && busy.length) parts.push(h('ol', { class: 'mt-1.5 space-y-1.5' }, ...busy.map((e) => entryRow(e, null))));
      if (showOthers && n.others.length) parts.push(h('ol', { class: 'mt-1.5 space-y-1.5' }, ...n.others.map((e) => entryRow(e, null))));
    }
    nearbyEl.replaceChildren(...parts.filter(Boolean));
  }

  // ------------------------------------------------------------ send signal

  const sendEl = $('org-send');
  const sendForm = $('org-send-form');
  let sending = null;   // { org, incident, requestId }

  function startSend(org) {
    const incident = D.selected();
    if (!incident) return toast('Select an emergency report first.', 'info');
    map.closePopup();
    if (org.org_type === 'hospital') {
      // A hospital receives a patient report: the existing Hospital Signal form, with both already chosen.
      if (window.ESRMSHospitalSignal) window.ESRMSHospitalSignal.prefill({ hospitalId: org.id, incident: { id: incident.id, code: incident.code, emergency_type: incident.emergency_type, status_label: D.statusLabel(incident.status) } });
      return undefined;
    }
    sending = { org, incident, requestId: crypto.randomUUID() };   // one id per signal: a double click cannot create two
    sendForm.reset();
    $('org-send-error').classList.add('hidden');
    const distance = distanceTo(org);
    $('org-send-summary').replaceChildren(
      h('p', {}, h('b', { class: 'text-white', text: org.name }), ` · ${org.org_type_label}${org.base_name ? ` · ${org.base_name}` : ''}`),
      h('p', { class: 'text-slate-300', text: `${incident.code} · ${incident.emergency_type}` }),
      distance != null && h('p', { class: 'text-slate-300', text: `${distance.toFixed(1)} km from the incident to this base` }));
    sendForm.elements.priority.value = String(incident.priority || 2);
    sendForm.elements.message.value = `${incident.emergency_type} reported. ${org.org_type_label} assistance requested.`;
    const warnings = [];
    if (org.availability === 'limited') warnings.push(`${org.name} is marked Busy.`);
    if (org.availability === 'full') warnings.push(`${org.name} is marked Unavailable.`);
    if (org.staff_count === 0) warnings.push(`No account is linked to ${org.name}, so nobody there will see this signal in ESRMS. Phone the organization as well.`);
    $('org-send-warning').replaceChildren(...warnings.map((w) => h('p', { text: w })));
    $('org-send-warning').classList.toggle('hidden', !warnings.length);
    sendEl.showModal();
    sendForm.elements.message.focus();
  }

  sendForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!sending || ev.submitter?.disabled) return;
    const { org, incident, requestId } = sending;
    const btn = ev.submitter;
    if (btn) btn.disabled = true;
    try {
      const data = await api(`${cfg.api.send}/${org.id}/signals`, { method: 'POST', body: { incident_id: incident.id, priority: sendForm.elements.priority.value, message: sendForm.elements.message.value, client_request_id: requestId } });
      // Only reached after the database stored the signal.
      sendEl.close();
      sending = null;
      toast(`${data.item.code}: ${data.item.signal_type_label} sent to ${org.name}.${data.staff_notified === 0 ? ' No account is linked there; phone them too.' : ''}`, 'success');
      document.dispatchEvent(new CustomEvent('esrms:signals-changed'));
    } catch (err) {
      const box = $('org-send-error');
      box.textContent = err.status === 0 ? 'Network error. The signal was NOT sent. Check your connection and try again.' : (err.fields && Object.values(err.fields)[0]) || err.message;
      box.classList.remove('hidden');
    } finally {
      if (btn) btn.disabled = false;
    }
    loadNearby();
  });

  // ------------------------------------------------------------ add / edit organization

  const orgEl = $('org-dialog');
  const orgForm = $('org-form');
  const OF = orgForm.elements;
  let editing = null;

  function orgErrors(fields, message) {
    const box = $('org-form-error');
    box.textContent = message || '';
    box.classList.toggle('hidden', !message);
    orgForm.querySelectorAll('[data-error-for]').forEach((p) => {
      const text = (fields || {})[p.dataset.errorFor];
      p.textContent = text || '';
      p.classList.toggle('hidden', !text);
    });
  }

  function openOrg(org = null) {
    if (!cfg.canDispatch) return;
    map.closePopup();
    editing = org;
    orgForm.reset();
    orgErrors();
    $('org-form-title').textContent = org ? 'Organization' : 'Add Organization';
    for (const id of ['org-delete', 'org-verified-row']) $(id).classList.toggle('hidden', !org);
    if (org) {
      for (const k of ['name', 'org_type', 'base_name', 'address', 'city', 'province', 'phone_number', 'latitude', 'longitude', 'availability']) OF[k].value = org[k] ?? '';
      OF.is_active.checked = org.is_active;
      OF.verified.checked = org.verified;
    }
    orgEl.showModal();
    OF.name.focus();
  }

  orgForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    orgErrors();
    const body = Object.fromEntries(new FormData(orgForm));
    body.is_active = OF.is_active.checked;
    if (editing) body.verified = OF.verified.checked; else delete body.verified;
    const errs = {};
    if (!body.name.trim()) errs.name = 'Organization name is required.';
    if (!body.org_type) errs.org_type = 'Choose an organization type.';
    const lat = Number(body.latitude);
    const lng = Number(body.longitude);
    if (body.latitude.trim() === '' || body.longitude.trim() === '') errs.latitude = "Enter the base's latitude and longitude.";
    else if (!Number.isFinite(lat) || !Number.isFinite(lng) || lat < -90 || lat > 90 || lng < -180 || lng > 180 || (lat === 0 && lng === 0)) errs.latitude = 'Enter valid coordinates, for example 7.0731 and 125.6128.';
    if (Object.keys(errs).length) return orgErrors(errs, 'Please fix the highlighted fields.');
    const btn = ev.submitter;
    if (btn) btn.disabled = true;
    try {
      const { item } = await api(editing ? `${cfg.api.organizations}/${editing.id}` : cfg.api.organizations, { method: editing ? 'PUT' : 'POST', body });
      toast(editing ? `${item.name} updated.` : `${item.name} added.`, 'success');
      orgEl.close();
      await loadOrganizations();
      loadNearby();
    } catch (err) {
      orgErrors(err.fields, err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
    return undefined;
  });

  $('org-delete').addEventListener('click', async () => {
    if (!editing || !(await D.confirm('Delete organization?', `${editing.name} and its account links will be removed. An organization with signal history cannot be deleted; disable it instead.`, 'Delete'))) return;
    try {
      await api(`${cfg.api.organizations}/${editing.id}`, { method: 'DELETE' });
      toast(`${editing.name} deleted.`, 'success');
      orgEl.close();
      await loadOrganizations();
      loadNearby();
    } catch (err) {
      orgErrors(null, err.message);
    }
  });

  // ------------------------------------------------------------ wiring

  $('org-add')?.addEventListener('click', () => openOrg());
  for (const dialog of [orgEl, sendEl]) dialog.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => dialog.close()));
  document.addEventListener('esrms:incident-selected', () => { showBusy = false; showOthers = false; focusedId = null; loadNearby(); });
  document.addEventListener('esrms:incidents-rendered', renderMarkers);

  // An organization changed its status, or answered a signal: raised from the hospital_signal_feed
  // realtime channel (hospital-signal.js). Polling covers accounts without that script.
  let timer;
  function refresh() {
    clearTimeout(timer);
    timer = setTimeout(() => { loadOrganizations(); if (D.selected()) loadNearby(); }, 300);
  }
  document.addEventListener('esrms:signals-changed', refresh);
  if (!window.HospitalUI) setInterval(refresh, 30000);

  loadOrganizations();
})();
