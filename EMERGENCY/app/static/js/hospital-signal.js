// Hospital Signal tab (dispatcher side): send a patient signal to a receiving hospital,
// watch the hospital's response, and manage the hospital list. All data comes from
// /api/hospital-signals (Flask); Realtime on hospital_signal_feed says when to refetch.
(function () {
  const root = document.getElementById('hospital-signal');
  const cfgEl = document.getElementById('hospital-signal-config');
  if (!root || !cfgEl || !window.AnnUI || !window.HospitalUI) return;
  const { h, icon, toast, api } = window.AnnUI;
  const UI = window.HospitalUI;
  const cfg = JSON.parse(cfgEl.textContent);
  const opt = cfg.options;
  const $ = (id) => document.getElementById(id);
  const notifyUser = (title, body, kind) => (window.ESRMSNotify
    ? window.ESRMSNotify.notify('dispatch_updates', title, body, { kind, tag: `hs-${title}` })
    : toast(`${title}: ${body}`, kind || 'info'));

  const form = $('hs-form');
  const F = form.elements;
  let hospitals = [];
  let incident = null;          // incident chosen for this signal
  let known = null;             // signal id -> status, to announce hospital responses
  let sending = false;
  let pendingRequest = null;    // { id, body } for the signal awaiting confirmation
  let openDetailId = null;

  // ------------------------------------------------------------ connection indicator

  const conn = $('hs-connection');
  let realtime = 'connecting';
  let apiDown = false;
  function renderConnection() {
    const offline = !navigator.onLine || apiDown;
    const [text, glyph, tone, dot, title] = offline
      ? ['Connection Lost', 'wifi_off', 'border-red-500/50 bg-red-950/40 text-red-200', 'bg-red-500', 'ESRMS cannot be reached. Signals cannot be sent until the connection returns.']
      : realtime === 'live'
        ? ['Connected to ESRMS', 'wifi', 'border-emerald-500/40 bg-emerald-500/5 text-emerald-200', 'bg-emerald-400', 'Live: hospital responses appear as they happen.']
        : realtime === 'polling'
          ? ['Connection Lost', 'wifi_off', 'border-amber-500/50 bg-amber-950/40 text-amber-200', 'bg-amber-400', 'Live updates are unavailable. ESRMS is checking for hospital responses every 15 seconds.']
          : ['Connecting…', 'wifi_find', 'border-amber-500/40 bg-amber-500/5 text-amber-200', 'bg-amber-400', 'Connecting to live updates…'];
    conn.className = `inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-mono font-semibold ${tone}`;
    conn.querySelector('[data-text]').textContent = text;
    conn.querySelector('[data-icon]').textContent = glyph;
    conn.querySelector('[data-dot]').className = `w-2 h-2 rounded-full ${dot}${realtime === 'live' && !offline ? ' animate-pulse' : ''}`;
    conn.title = title;
  }
  window.addEventListener('online', renderConnection);
  window.addEventListener('offline', renderConnection);

  // Every request goes through here so a failed one flips the indicator and a good one clears it.
  async function call(url, options) {
    try {
      const data = await api(url, options);
      if (apiDown) { apiDown = false; renderConnection(); }
      return data;
    } catch (err) {
      // Unreachable server or gateway. A 503 "not set up yet" is a configuration message, not a lost connection.
      if (err.status === 0 || err.status === 502 || err.status === 504) { apiDown = true; renderConnection(); }
      if (err.status === 401) err.message = 'Your session has ended. Sign in again to continue.';
      throw err;
    }
  }

  function showLoadError(message) {
    $('hs-error').querySelector('[data-text]').textContent = message;
    $('hs-error').classList.remove('hidden');
  }

  // ------------------------------------------------------------ hospitals

  async function loadHospitals() {
    const p = new URLSearchParams({ type: 'hospital' });
    if (incident) p.set('incident_id', incident.id);
    try {
      const data = await call(`${cfg.api.hospitals}?${p}`);
      hospitals = data.items;
      $('hs-distance-note').classList.toggle('hidden', !data.distance_from_incident);
      $('hs-error').classList.add('hidden');
      renderHospitals();
    } catch (err) {
      showLoadError(err.message);
      $('hs-hospitals').replaceChildren(h('p', { class: 'py-6 text-center text-sm text-red-300', role: 'alert', text: 'Hospitals could not be loaded.' }));
    }
  }

  const staffNote = (hp) => (hp.staff_count
    ? h('span', { class: 'inline-flex items-center gap-1 text-emerald-300', title: 'Hospital accounts that will see the signal in ESRMS' }, h('span', { class: 'w-1.5 h-1.5 rounded-full bg-emerald-400' }), `${hp.staff_count} staff account${hp.staff_count === 1 ? '' : 's'} linked`)
    : h('span', { class: 'inline-flex items-center gap-1 text-amber-300', title: 'No hospital account is linked, so nobody at this hospital will see a signal in ESRMS.' }, icon('warning', 'text-[0.8125rem]'), 'No staff account linked'));

  // A self-registered hospital has to be verified by dispatch before patient information can go to it.
  const selectable = (hp) => hp.verified && hp.availability !== 'offline';

  function renderHospitals() {
    const chosen = F.hospital_id.value;
    F.hospital_id.replaceChildren(h('option', { value: '', text: hospitals.length ? 'Select hospital' : 'No hospitals available' }),
      ...hospitals.map((hp) => h('option', { value: hp.id, disabled: !selectable(hp), text: `${hp.name} — ${hp.verified ? hp.availability_label : 'Not verified'}${hp.staff_count ? '' : ' (no staff account)'}` })));
    if (hospitals.some((hp) => hp.id === chosen && selectable(hp))) F.hospital_id.value = chosen;

    const filter = $('hs-all-filters').elements.hospital_id;
    const filtered = filter.value;
    filter.replaceChildren(h('option', { value: '', text: 'All hospitals' }), ...hospitals.map((hp) => h('option', { value: hp.id, text: hp.name })));
    filter.value = filtered;

    if (!hospitals.length) {
      $('hs-hospitals').replaceChildren(h('div', { class: 'py-8 text-center text-slate-400' },
        icon('domain_disabled', 'text-[2rem]'),
        h('p', { class: 'text-sm mt-1', text: 'No receiving hospitals have been added yet.' }),
        h('button', { type: 'button', class: 'mt-2 text-xs text-brand-cyan underline', onclick: () => openHospital(), text: 'Add the first hospital' })));
      return;
    }
    $('hs-hospitals').replaceChildren(...hospitals.map((hp) => {
      const selected = F.hospital_id.value === hp.id;
      const offline = !selectable(hp);
      return h('div', { class: `flex items-center gap-2 py-2.5 px-1 ${selected ? 'bg-brand-cyan/5' : ''}` },
        h('div', { class: 'min-w-0 flex-1' },
          h('div', { class: 'text-sm font-semibold text-white truncate', text: hp.name }),
          h('div', { class: 'mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[0.6875rem]' }, staffNote(hp),
            hp.address && h('span', { class: 'text-slate-400 truncate max-w-[14rem]', text: hp.address }))),
        hp.distance_km != null && h('span', { class: 'text-xs font-mono text-slate-300 whitespace-nowrap', title: 'Straight-line distance from the selected incident', text: `${hp.distance_km.toFixed(1)} km` }),
        hp.verified ? UI.availabilityBadge(hp)
          : h('button', { type: 'button', class: 'inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-amber-500/50 bg-amber-500/10 text-amber-300 text-[0.6875rem] font-semibold whitespace-nowrap', title: 'This hospital registered itself. Open it to verify it before sending patient information.', onclick: () => openHospital(hp) }, icon('gpp_maybe', 'text-[0.8125rem]'), 'Verify'),
        h('button', { type: 'button', class: 'p-1 rounded text-slate-400 hover:text-brand-cyan', title: 'Edit hospital and staff', 'aria-label': `Edit ${hp.name}`, onclick: () => openHospital(hp) }, icon('edit', 'text-[1rem]')),
        h('button', {
          type: 'button', disabled: offline, 'aria-pressed': String(selected), 'aria-label': `Select ${hp.name} for this signal`,
          title: !hp.verified ? 'Not verified yet: cannot receive signals' : offline ? 'Offline: cannot receive signals' : 'Select for this signal',
          class: 'p-1 rounded border border-brand-borderSubtle text-slate-300 hover:text-brand-cyan hover:border-brand-cyan/50 aria-pressed:bg-brand-cyan/20 aria-pressed:text-brand-cyan aria-pressed:border-brand-cyan disabled:opacity-30 disabled:pointer-events-none',
          onclick: () => { F.hospital_id.value = hp.id; clearError('hospital_id'); renderHospitals(); F.signal_type.focus(); },
        }, icon(selected ? 'check' : 'chevron_right', 'text-[1.125rem]')));
    }));
  }
  F.hospital_id.addEventListener('change', renderHospitals);

  // ------------------------------------------------------------ incident picker

  const search = $('hs-incident-search');
  const list = $('hs-incident-list');
  const chosenEl = $('hs-incident-chosen');
  let searchSeq = 0;
  let searchTimer;

  function setListOpen(open) {
    list.classList.toggle('hidden', !open);
    search.setAttribute('aria-expanded', String(open));
  }

  async function searchIncidents() {
    const seq = ++searchSeq;
    try {
      const { items } = await call(`${cfg.api.incidents}?${new URLSearchParams({ q: search.value.trim() })}`);
      if (seq !== searchSeq) return;
      list.replaceChildren(...(items.length ? items.map((i) => h('li', { role: 'option' },
        h('button', { type: 'button', class: 'w-full text-left px-3 py-2 hover:bg-white/5 focus-visible:bg-white/5', onclick: () => chooseIncident(i) },
          h('span', { class: 'font-mono text-xs text-brand-cyan', text: i.code }),
          h('span', { class: 'ml-2 text-sm text-slate-100', text: i.emergency_type }),
          h('span', { class: 'block text-[0.6875rem] text-slate-400', text: `${i.status_label}${i.archived ? ' · archived' : ''} · reported ${UI.fmtWhen(i.created_at)}${i.has_location ? '' : ' · no GPS'}` }))))
        : [h('li', { class: 'px-3 py-3 text-sm text-slate-400', text: search.value.trim() ? 'No incident matches that search.' : 'No incidents have been reported yet.' })]));
      setListOpen(true);
    } catch (err) {
      if (seq !== searchSeq) return;
      list.replaceChildren(h('li', { class: 'px-3 py-3 text-sm text-red-300', role: 'alert', text: err.message }));
      setListOpen(true);
    }
  }

  function chooseIncident(i) {
    incident = i;
    F.incident_id.value = i ? i.id : '';
    setListOpen(false);
    search.value = '';
    search.classList.toggle('hidden', Boolean(i));
    chosenEl.classList.toggle('hidden', !i);
    chosenEl.classList.toggle('flex', Boolean(i));
    if (i) {
      chosenEl.replaceChildren(
        h('span', { class: 'font-mono text-xs text-brand-cyan', text: i.code }),
        h('span', { class: 'min-w-0 flex-1 truncate text-slate-100', title: i.emergency_type, text: i.emergency_type }),
        h('span', { class: 'text-[0.6875rem] text-slate-300 whitespace-nowrap', text: i.status_label }),
        h('button', { type: 'button', class: 'rounded hover:bg-white/10', 'aria-label': `Remove ${i.code} from this signal`, onclick: () => { chooseIncident(null); search.focus(); } }, icon('close', 'text-[1rem]')));
    }
    clearError('incident_id');
    return loadHospitals();   // distances are measured from this incident
  }

  search.addEventListener('focus', searchIncidents);
  search.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchIncidents, 250); });
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setListOpen(false);
    if (e.key === 'ArrowDown') { e.preventDefault(); list.querySelector('button')?.focus(); }
    if (e.key === 'Enter') e.preventDefault();   // never submit the form from the search box
  });
  list.addEventListener('keydown', (e) => {
    const items = [...list.querySelectorAll('button')];
    const at = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); items[Math.min(at + 1, items.length - 1)]?.focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); (at <= 0 ? search : items[at - 1]).focus(); }
    if (e.key === 'Escape') { setListOpen(false); search.focus(); }
  });
  document.addEventListener('click', (e) => { if (!list.contains(e.target) && e.target !== search) setListOpen(false); });

  // ------------------------------------------------------------ form

  const formError = $('hs-form-error');
  function clearError(name) {
    const p = form.querySelector(`[data-error-for="${name}"]`);
    if (p) { p.textContent = ''; p.classList.add('hidden'); }
    F[name]?.removeAttribute?.('aria-invalid');
  }
  function clearErrors() {
    formError.classList.add('hidden');
    form.querySelectorAll('[data-error-for]').forEach((p) => { p.textContent = ''; p.classList.add('hidden'); });
    form.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
  }
  function showErrors(fields, message) {
    formError.textContent = message;
    formError.classList.remove('hidden');
    let first = null;
    for (const [name, text] of Object.entries(fields || {})) {
      const p = form.querySelector(`[data-error-for="${name}"]`);
      if (p) { p.textContent = text; p.classList.remove('hidden'); }
      const el = name === 'incident_id' ? search : F[name];
      if (el?.closest?.('details')) el.closest('details').open = true;   // errors in the optional EMS section must be visible
      if (el?.setAttribute) { el.setAttribute('aria-invalid', 'true'); first = first || el; }
    }
    (first && !first.classList.contains('hidden') ? first : formError).focus();
  }

  const notes = F.additional_notes;
  const counter = $('hs-notes-count');
  function updateCounter() {
    counter.textContent = `${notes.value.length}/${opt.notesLimit}`;
    counter.classList.toggle('text-amber-300', notes.value.length >= opt.notesLimit * 0.9);
  }
  notes.addEventListener('input', updateCounter);
  form.addEventListener('input', (e) => { if (e.target.name) clearError(e.target.name); });

  function resetForm() {
    form.reset();
    F.eta_date.value = opt.today;
    clearErrors();
    updateCounter();
    if (incident) chooseIncident(null); else renderHospitals();
  }
  $('hs-clear').addEventListener('click', resetForm);

  function readForm() {
    const body = Object.fromEntries(new FormData(form));
    const errors = {};
    if (!body.hospital_id) errors.hospital_id = 'Select the receiving hospital.';
    if (!body.signal_type) errors.signal_type = 'Choose a signal type.';
    if (!body.patient_condition) errors.patient_condition = "Select the patient's condition.";
    if (body.patient_age !== '' && !(Number.isInteger(Number(body.patient_age)) && Number(body.patient_age) >= 0 && Number(body.patient_age) <= 130)) {
      errors.patient_age = 'Enter an age between 0 and 130, or leave it blank.';
    }
    if (F.patient_age.validity.badInput) errors.patient_age = 'Enter the age as a number.';
    if (!body.eta_time) { body.eta_date = ''; }   // a date alone is not an ETA
    return { body, errors };
  }

  const confirmEl = $('hs-confirm');
  const line = (label, value, cls = '') => h('div', { class: 'flex gap-3 py-1.5 border-b border-brand-borderSubtle/60' },
    h('dt', { class: 'w-24 shrink-0 text-[0.6875rem] font-mono uppercase tracking-wide text-slate-400 pt-0.5', text: label }),
    h('dd', { class: `min-w-0 flex-1 text-slate-100 break-words ${cls}`, text: value }));

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (sending) return;
    clearErrors();
    const { body, errors } = readForm();
    if (Object.keys(errors).length) return showErrors(errors, 'Please fix the highlighted fields before sending.');
    const hp = hospitals.find((x) => x.id === body.hospital_id);
    if (!hp) return showErrors({ hospital_id: 'Select the receiving hospital.' }, 'Please fix the highlighted fields before sending.');

    // One id per confirmed signal: a double click or a retry cannot create a second one.
    pendingRequest = { body: { ...body, client_request_id: crypto.randomUUID() }, hospital: hp };
    const patient = [body.patient_name || 'Name not given', body.patient_age !== '' ? `${body.patient_age} y/o` : null, opt.genders[body.patient_gender]].filter(Boolean).join(' · ');
    $('hs-confirm-body').replaceChildren(...[
      line('Hospital', hp.name),
      line('Signal', opt.signalTypes[body.signal_type]),
      line('Incident', incident ? `${incident.code} · ${incident.emergency_type}` : 'Not linked to an incident'),
      line('Patient', patient),
      line('Condition', opt.conditions[body.patient_condition], UI.CONDITION_STYLE[body.patient_condition] || ''),
      body.triage_priority && line('Priority', opt.triage[body.triage_priority]),
      body.chief_complaint && line('Complaint', body.chief_complaint),
      (body.medic_unit || body.transport_type) && line('Transport', [body.medic_unit, opt.transportTypes[body.transport_type]].filter(Boolean).join(' · ')),
      line('ETA', body.eta_time ? `${body.eta_time}${body.eta_date && body.eta_date !== opt.today ? ` on ${body.eta_date}` : ''} PHT` : 'Not given'),
      body.additional_notes && line('Notes', body.additional_notes)].filter(Boolean));
    const warnings = [];
    if (!hp.staff_count) warnings.push(`No staff account is linked to ${hp.name}, so nobody there will see this signal in ESRMS. Phone the hospital as well.`);
    if (hp.availability === 'full') warnings.push(`${hp.name} is marked Full.`);
    if (hp.availability === 'limited') warnings.push(`${hp.name} is marked Limited.`);
    $('hs-confirm-warning').replaceChildren(...warnings.map((w) => h('p', { text: w })));
    $('hs-confirm-warning').classList.toggle('hidden', !warnings.length);
    confirmEl.showModal();
    $('hs-confirm-send').focus();
  });

  $('hs-confirm-send').addEventListener('click', async () => {
    if (sending || !pendingRequest) return;   // second click while the first is in flight
    sending = true;
    const buttons = [$('hs-confirm-send'), $('hs-send')];
    buttons.forEach((b) => { b.disabled = true; b.querySelector('[data-label]').textContent = 'Sending…'; });
    const { body, hospital } = pendingRequest;
    try {
      const data = await call(cfg.api.signals, { method: 'POST', body });
      // Only reached after the database stored the signal.
      pendingRequest = null;
      confirmEl.close();
      resetForm();
      showSuccess(data.item, hospital, data);
      loadRecent();
    } catch (err) {
      confirmEl.close();
      if (err.status === 422 || err.status === 409) showErrors(err.fields, err.message);
      else showErrors(null, err.status === 0 ? 'Network error. The signal was NOT sent. Check your connection and press Send Signal again.' : err.message);
      if (err.status === 409) loadHospitals();
    } finally {
      sending = false;
      buttons.forEach((b) => { b.disabled = false; b.querySelector('[data-label]').textContent = 'Send Signal'; });
    }
  });

  let successSignal = null;
  function showSuccess(item, hospital, data) {
    successSignal = item;
    $('hs-success-text').textContent = `Patient information has been successfully sent to ${item.hospital_name}.`;
    $('hs-success-body').replaceChildren(
      line('Signal ID', item.code, 'font-mono text-brand-cyan'), line('Hospital', item.hospital_name),
      line('Incident', item.incident ? item.incident.code : 'Not linked'), line('Status', item.status_label), line('Sent', UI.fmtFull(item.sent_at)));
    const note = $('hs-success-note');
    const none = data.staff_notified === 0;
    note.textContent = data.duplicate ? 'This signal had already been sent; no second copy was created.'
      : none ? `No staff account is linked to ${item.hospital_name}, so nobody has been notified in ESRMS. Phone the hospital to confirm.` : '';
    note.classList.toggle('hidden', !note.textContent);
    $('hs-success').showModal();
    toast(`Hospital signal sent successfully. ${item.signal_type_label} notification sent to ${item.hospital_name}.`, 'success');
  }
  $('hs-success-view').addEventListener('click', () => { $('hs-success').close(); if (successSignal) openDetail(successSignal.id); });

  // ------------------------------------------------------------ recent signals

  const cell = (...c) => h('td', { class: 'px-2 py-2 align-middle' }, ...c);
  const rowProps = (s) => ({
    tabindex: '0', role: 'link', 'aria-label': `Open signal ${s.code}`,
    class: 'border-b border-brand-borderSubtle/60 cursor-pointer hover:bg-brand-cyan/5 focus-visible:bg-brand-cyan/10',
    onclick: () => openDetail(s.id),
    onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(s.id); } },
  });

  async function loadRecent() {
    try {
      const { items } = await call(`${cfg.api.signals}?per_page=10&archived=0`);
      announce(items);
      $('hs-recent').replaceChildren(...(items.length ? items.slice(0, 8).map((s) => h('tr', rowProps(s),
        cell(h('span', { class: 'font-mono text-xs text-slate-200 whitespace-nowrap', title: UI.fmtFull(s.sent_at), text: UI.fmtWhen(s.sent_at) })),
        cell(h('span', { class: 'text-slate-100', text: s.hospital_name })),
        cell(h('span', { class: 'text-slate-200 whitespace-nowrap', text: s.signal_type_label })),
        cell(h('span', { class: 'font-mono text-xs text-brand-cyan whitespace-nowrap', text: s.incident ? s.incident.code : '—' })),
        cell(h('div', { class: 'flex flex-col items-start gap-1.5' }, ...[UI.statusBadge(s), archiveButton(s)].filter(Boolean)))))
        : [h('tr', {}, h('td', { colspan: '5', class: 'px-2 py-8 text-center text-sm text-slate-400', text: 'No signals here. Archived signals are in Incident & Signal History → Signal History.' }))]));
      $('hs-error').classList.add('hidden');
      if (openDetailId) refreshDetail();
      if ($('hs-all').open) loadAll();
    } catch (err) {
      showLoadError(err.message);
      if (!known) $('hs-recent').replaceChildren(h('tr', {}, h('td', { colspan: '5', class: 'px-2 py-8 text-center text-sm text-red-300', role: 'alert', text: 'Signals could not be loaded.' })));
    }
  }

  // A finished signal can be moved to Incident & Signal History → Signal History. Open ones stay here.
  function archiveButton(s) {
    if (!s.read_only) return null;
    return h('button', {
      type: 'button', 'aria-label': `Archive ${s.code} to Signal History`, title: 'Move to Incident & Signal History → Signal History',
      class: 'inline-flex items-center gap-1 px-2 py-1 rounded-md border border-brand-cyan/50 text-brand-cyan text-[0.6875rem] font-semibold whitespace-nowrap hover:bg-brand-cyan/10',
      onclick: (e) => { e.stopPropagation(); archiveSignal(s, e.currentTarget); },
      onkeydown: (e) => e.stopPropagation(),
    }, icon('inventory_2', 'text-[0.875rem]'), 'Archive');
  }

  async function archiveSignal(s, btn) {
    await UI.busy(btn, async () => {
      try {
        await call(`${cfg.api.signals}/${s.id}/archive`, { method: 'POST', body: {} });
        toast(`${s.code} moved to Signal History.`, 'success');
        if (openDetailId === s.id) detailEl.close();
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    loadRecent();
    document.dispatchEvent(new CustomEvent('esrms:signals-changed'));
  }

  // Tell the dispatcher when a hospital answers, whichever tab is open.
  function announce(items) {
    if (known) {
      for (const s of items) {
        const before = known.get(s.id);
        if (before && before !== s.status && s.status !== 'cancelled') {
          notifyUser(`${s.code}: ${s.hospital_name}`, s.status === 'unable' ? `Unable to receive. ${s.rejection_reason || ''}` : s.status_label, s.status === 'unable' ? 'error' : 'success');
        }
      }
    }
    known = new Map([...(known || []), ...items.map((s) => [s.id, s.status])]);
  }

  // ------------------------------------------------------------ details

  const detailEl = $('hs-detail');
  async function openDetail(id) {
    openDetailId = id;
    $('hs-detail-body').replaceChildren(h('p', { class: 'py-8 text-center text-sm text-slate-400', text: 'Loading…' }));
    $('hs-detail-actions').replaceChildren();
    if (!detailEl.open) detailEl.showModal();
    await refreshDetail();
  }
  async function refreshDetail() {
    const id = openDetailId;
    try {
      const { item } = await call(`${cfg.api.signals}/${id}`);
      if (openDetailId !== id) return;
      $('hs-detail-body').replaceChildren(UI.detail(item));
      $('hs-detail-actions').replaceChildren(...[
        item.read_only && !item.archived_at && h('button', { type: 'button', class: 'inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-semibold border border-brand-cyan/50 text-brand-cyan hover:bg-brand-cyan/10', onclick: (e) => archiveSignal(item, e.currentTarget) }, icon('inventory_2', 'text-[1rem]'), 'Archive'),
        item.actions.includes('cancel') && h('button', { type: 'button', class: 'px-4 py-2 rounded-md text-sm font-semibold border border-red-500/50 text-red-300 hover:bg-red-500/10', onclick: (e) => cancelSignal(item, e.currentTarget) }, 'Cancel Signal'),
        h('button', { type: 'button', class: 'px-4 py-2 rounded-md text-sm text-slate-300 hover:bg-white/10', onclick: () => detailEl.close(), text: 'Close' })].filter(Boolean));
    } catch (err) {
      if (openDetailId !== id) return;
      $('hs-detail-body').replaceChildren(h('p', { class: 'py-6 text-sm text-red-300', role: 'alert', text: err.message }));
    }
  }
  detailEl.addEventListener('close', () => { openDetailId = null; });

  async function cancelSignal(item, btn) {
    if (!window.confirm(`Cancel ${item.code} to ${item.hospital_name}? The hospital will be told the signal is cancelled.`)) return;
    await UI.busy(btn, async () => {
      try {
        await call(`${cfg.api.signals}/${item.id}/cancel`, { method: 'POST', body: {} });
        toast(`${item.code} cancelled.`, 'success');
      } catch (err) {
        toast(err.message, 'error');
      }
    });
    loadRecent();
  }

  // ------------------------------------------------------------ all signals

  const allEl = $('hs-all');
  const allFilters = $('hs-all-filters');
  const view = { page: 1, perPage: opt.pageSizes[0] };
  let allSeq = 0;

  async function loadAll() {
    const seq = ++allSeq;
    const p = new URLSearchParams({ page: view.page, per_page: view.perPage });
    for (const [k, v] of new FormData(allFilters)) if (v) p.set(k, v);
    const body = $('hs-all-body');
    body.classList.add('opacity-50');
    try {
      const data = await call(`${cfg.api.signals}?${p}`);
      if (seq !== allSeq) return;
      view.page = data.page;
      const filtered = [...new FormData(allFilters).values()].some(Boolean);
      body.replaceChildren(...(data.items.length ? data.items.map((s) => h('tr', rowProps(s),
        cell(h('span', { class: 'font-mono text-xs text-brand-cyan whitespace-nowrap', text: s.code })),
        cell(h('span', { class: 'text-xs text-slate-200 whitespace-nowrap', title: UI.fmtFull(s.sent_at), text: UI.fmtWhen(s.sent_at) })),
        cell(h('span', { class: 'text-slate-100', text: s.hospital_name })),
        cell(h('span', { class: 'text-slate-200 whitespace-nowrap', text: s.signal_type_label })),
        cell(h('span', { class: 'font-mono text-xs text-slate-200 whitespace-nowrap', text: s.incident ? s.incident.code : '—' })),
        cell(h('span', { class: `text-xs ${UI.CONDITION_STYLE[s.patient_condition] || 'text-slate-200'}`, text: s.condition_label })),
        cell(h('span', { class: 'text-xs text-slate-200 whitespace-nowrap', text: s.eta ? UI.fmtWhen(s.eta) : '—' })),
        cell(UI.statusBadge(s))))
        : [h('tr', {}, h('td', { colspan: '8', class: 'px-2 py-10 text-center text-sm text-slate-400', text: filtered ? 'No signals match these filters.' : 'No hospital signals have been sent yet.' }))]));
      const first = (data.page - 1) * data.per_page + 1;
      const pageBtn = (label, glyph, page, disabled) => h('button', {
        type: 'button', disabled, 'aria-label': label,
        class: 'p-1 rounded border border-brand-borderSubtle text-slate-300 hover:text-brand-cyan hover:border-brand-cyan/50 disabled:opacity-30 disabled:pointer-events-none',
        onclick: () => { view.page = page; loadAll(); },
      }, icon(glyph, 'text-[1.125rem] align-middle'));
      $('hs-all-pager').replaceChildren(
        h('span', { text: data.total ? `Showing ${first}–${first + data.items.length - 1} of ${data.total}` : 'No signals to show' }),
        h('div', { class: 'flex items-center gap-2' },
          h('label', { class: 'flex items-center gap-1.5' }, 'Rows', h('select', {
            class: 'px-1.5 py-1 rounded bg-[#0a1020] border border-brand-borderSubtle text-xs text-slate-100',
            onchange: (e) => { view.perPage = Number(e.target.value); view.page = 1; loadAll(); },
          }, ...opt.pageSizes.map((n) => h('option', { value: n, selected: n === data.per_page, text: n })))),
          pageBtn('Previous page', 'chevron_left', data.page - 1, data.page <= 1),
          h('span', { class: 'tabular-nums', text: `Page ${data.page} of ${data.pages}` }),
          pageBtn('Next page', 'chevron_right', data.page + 1, data.page >= data.pages)));
    } catch (err) {
      if (seq !== allSeq) return;
      body.replaceChildren(h('tr', {}, h('td', { colspan: '8', class: 'px-2 py-8 text-center text-sm text-red-300', role: 'alert', text: err.message })));
      $('hs-all-pager').replaceChildren();
    } finally {
      if (seq === allSeq) body.classList.remove('opacity-50');
    }
  }
  let filterTimer;
  allFilters.addEventListener('input', (e) => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { view.page = 1; loadAll(); }, e.target.name === 'q' ? 350 : 0);
  });
  allFilters.addEventListener('submit', (e) => e.preventDefault());
  $('hs-all-clear').addEventListener('click', () => { allFilters.reset(); view.page = 1; loadAll(); });
  $('hs-view-all').addEventListener('click', () => { view.page = 1; allEl.showModal(); loadAll(); });

  // ------------------------------------------------------------ hospital + staff management

  const hospitalDialog = $('hs-hospital-dialog');
  const hospitalForm = $('hs-hospital-form');
  const HF = hospitalForm.elements;
  let editing = null;

  function hospitalErrors(fields, message) {
    const box = $('hs-hospital-error');
    box.textContent = message || '';
    box.classList.toggle('hidden', !message);
    hospitalForm.querySelectorAll('[data-error-for]').forEach((p) => {
      const text = (fields || {})[p.dataset.errorFor];
      p.textContent = text || '';
      p.classList.toggle('hidden', !text);
    });
  }

  function openHospital(hp = null) {
    editing = hp;
    hospitalForm.reset();
    hospitalErrors();
    $('hs-hospital-title').textContent = hp ? 'Edit Hospital' : 'Add Hospital';
    $('hs-hospital-delete').classList.toggle('hidden', !hp);
    $('hs-staff').classList.toggle('hidden', !hp);
    $('hs-hospital-verify').classList.toggle('hidden', !hp);
    $('hs-hospital-registered').classList.toggle('hidden', !hp || hp.verified);
    $('hs-staff-error').classList.add('hidden');
    if (hp) {
      for (const k of ['name', 'address', 'phone_number', 'availability', 'latitude', 'longitude']) HF[k].value = hp[k] ?? '';
      HF.is_active.checked = hp.is_active;
      HF.verified.checked = hp.verified;
      loadStaff();
    }
    hospitalDialog.showModal();
    HF.name.focus();
  }
  $('hs-hospital-add').addEventListener('click', () => openHospital());

  hospitalForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hospitalErrors();
    const body = Object.fromEntries(new FormData(hospitalForm));
    body.is_active = HF.is_active.checked;
    if (editing) body.verified = HF.verified.checked; else delete body.verified;
    if (!body.name.trim()) return hospitalErrors({ name: 'Hospital name is required.' }, 'Please fix the highlighted fields.');
    await UI.busy(e.submitter, async () => {
      try {
        const { item } = await call(editing ? `${cfg.api.hospitals}/${editing.id}` : cfg.api.hospitals, { method: editing ? 'PUT' : 'POST', body });
        toast(editing ? 'Hospital updated.' : `${item.name} added. Link a staff account so it can receive signals.`, 'success');
        await loadHospitals();
        if (editing) hospitalDialog.close(); else openHospital(hospitals.find((x) => x.id === item.id) || { ...item, staff_count: 0 });
      } catch (err) {
        hospitalErrors(err.fields, err.message);
      }
    });
  });

  $('hs-hospital-delete').addEventListener('click', async (e) => {
    if (!editing || !window.confirm(`Delete ${editing.name}? Its staff links are removed too.`)) return;
    await UI.busy(e.currentTarget, async () => {
      try {
        await call(`${cfg.api.hospitals}/${editing.id}`, { method: 'DELETE' });
        toast(`${editing.name} deleted.`, 'success');
        hospitalDialog.close();
        loadHospitals();
      } catch (err) {
        hospitalErrors(null, err.message);
      }
    });
  });

  async function loadStaff() {
    const listEl = $('hs-staff-list');
    const hospitalId = editing.id;
    listEl.replaceChildren(h('li', { class: 'text-xs text-slate-400', text: 'Loading…' }));
    try {
      const { items } = await call(`${cfg.api.hospitals}/${hospitalId}/staff`);
      if (editing?.id !== hospitalId) return;
      listEl.replaceChildren(...(items.length ? items.map((m) => h('li', { class: 'flex items-center gap-2 rounded-md bg-[#141a2b] border border-brand-borderSubtle px-2.5 py-1.5 text-sm' },
        h('span', { class: 'min-w-0 flex-1 truncate text-slate-100', text: m.name ? `${m.name} · ${m.email}` : m.email || 'Account removed' }),
        h('button', { type: 'button', class: 'text-xs text-red-300 underline', onclick: () => removeStaff(m) }, 'Remove')))
        : [h('li', { class: 'text-xs text-amber-300', text: 'No staff account linked yet. Signals to this hospital will not be seen in ESRMS.' })]));
    } catch (err) {
      listEl.replaceChildren(h('li', { class: 'text-xs text-red-300', role: 'alert', text: err.message }));
    }
  }
  async function removeStaff(member) {
    try {
      await call(`${cfg.api.hospitals}/${editing.id}/staff/${member.user_id}`, { method: 'DELETE' });
      toast('Staff account removed from this hospital.', 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
    loadStaff();
    loadHospitals();
  }
  $('hs-staff-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = e.currentTarget.elements.email;
    const errorEl = $('hs-staff-error');
    errorEl.classList.add('hidden');
    await UI.busy(e.submitter, async () => {
      try {
        const { item } = await call(`${cfg.api.hospitals}/${editing.id}/staff`, { method: 'POST', body: { email: input.value } });
        input.value = '';
        toast(`${item.email} can now receive this hospital's signals.`, 'success');
        loadStaff();
        loadHospitals();
      } catch (err) {
        errorEl.textContent = err.fields?.email || err.message;
        errorEl.classList.remove('hidden');
      }
    });
  });

  // The Dispatch Center's Nearby Organizations sends a hospital here with the incident already linked.
  window.ESRMSHospitalSignal = {
    async prefill({ hospitalId, incident: chosen }) {
      document.querySelector('[data-tab="hospital"]')?.click();
      await chooseIncident(chosen);
      const hp = hospitals.find((x) => x.id === hospitalId);
      if (hp && selectable(hp)) { F.hospital_id.value = hp.id; clearError('hospital_id'); renderHospitals(); }
      else toast(`${hp ? hp.name : 'That hospital'} cannot be selected right now. Choose another hospital.`, 'info');
      F.signal_type.focus();
    },
  };

  // ------------------------------------------------------------ wiring

  document.addEventListener('esrms:signals-restored', () => loadRecent());
  root.querySelectorAll('dialog [data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));
  $('hs-retry').addEventListener('click', () => { loadHospitals(); loadRecent(); });
  document.querySelector('[data-tab="hospital"]')?.addEventListener('click', () => { loadHospitals(); loadRecent(); });

  // One refetch per burst of realtime events.
  let changeTimer;
  function onChange() {
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => { loadRecent(); loadHospitals(); document.dispatchEvent(new CustomEvent('esrms:signals-changed')); }, 250);
  }
  if (window.AnnouncementLive) {
    window.AnnouncementLive.start({ ...opt.realtime, onChange, onStatus: (s) => { realtime = s; renderConnection(); } });
  } else {
    realtime = 'polling';
    setInterval(onChange, 15000);
  }
  renderConnection();
  updateCounter();
  loadHospitals();
  loadRecent();
})();
