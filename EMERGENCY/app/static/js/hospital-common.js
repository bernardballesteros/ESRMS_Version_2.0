// Shared by the dispatcher's Hospital Signal tab and the Hospital Portal dashboard:
// formatting, status badges, the connection indicator and the report timeline.
window.HospitalUI = (function () {
  const { h, icon } = window.AnnUI;

  const timeFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', hour: '2-digit', minute: '2-digit', hour12: false });
  const dayFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', month: 'short', day: 'numeric' });
  const fullFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
  const isoDay = (d) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Manila' }).format(d);
  const fmtTime = (iso) => (iso ? timeFmt.format(new Date(iso)) : '—');
  const fmtFull = (iso) => (iso ? fullFmt.format(new Date(iso)) : '—');
  // "14:28" today, "Oct 4, 14:28" on other days.
  const fmtWhen = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return isoDay(d) === isoDay(new Date()) ? timeFmt.format(d) : `${dayFmt.format(d)}, ${timeFmt.format(d)}`;
  };
  // "ETA 4 min", "ETA 1 h 10 min", or "ETA passed 6 min ago" once the estimate is behind us.
  function etaText(iso) {
    if (!iso) return 'ETA not given';
    const minutes = Math.round((new Date(iso) - Date.now()) / 60000);
    const span = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`);
    if (minutes < 0) return `ETA passed ${span(-minutes)} ago`;
    return minutes === 0 ? 'ETA now' : `ETA ${span(minutes)}`;
  }

  // Every badge carries its label and an icon, so status never depends on colour alone.
  const STATUS_STYLE = {
    sent:         ['send', 'bg-emerald-500/10 text-emerald-300 border-emerald-500/40'],
    acknowledged: ['visibility', 'bg-blue-500/15 text-blue-300 border-blue-500/40'],
    accepted:     ['bed', 'bg-brand-cyan/10 text-brand-cyan border-brand-cyan/40'],
    arrived:      ['airport_shuttle', 'bg-amber-500/15 text-amber-300 border-amber-500/40'],
    handoff:      ['handshake', 'bg-violet-500/15 text-violet-300 border-violet-500/40'],
    responding:   ['directions_run', 'bg-amber-500/15 text-amber-300 border-amber-500/40'],
    on_scene:     ['pin_drop', 'bg-violet-500/15 text-violet-300 border-violet-500/40'],
    completed:    ['check_circle', 'bg-emerald-600/30 text-emerald-200 border-emerald-400/60'],
    received:     ['check_circle', 'bg-emerald-600/30 text-emerald-200 border-emerald-400/60'],
    unable:       ['block', 'bg-red-500/15 text-red-300 border-red-500/40'],
    cancelled:    ['cancel', 'bg-slate-600/30 text-slate-300 border-slate-500'],
  };
  const statusBadge = (s) => {
    let [glyph, cls] = STATUS_STYLE[s.status] || ['help', ''];
    if (s.kind === 'response' && s.status === 'accepted') glyph = 'thumb_up';   // no bay is being prepared
    return h('span', { class: `inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[0.6875rem] font-semibold whitespace-nowrap ${cls}` },
      icon(glyph, 'text-[0.8125rem]'), s.status_label);
  };

  const AVAILABILITY_STYLE = {
    ready:   ['bg-emerald-400', 'bg-emerald-500/10 text-emerald-300 border-emerald-500/40'],
    limited: ['bg-amber-400', 'bg-amber-500/10 text-amber-300 border-amber-500/40'],
    full:    ['bg-red-400', 'bg-red-500/10 text-red-300 border-red-500/40'],
    offline: ['bg-slate-500', 'bg-slate-600/30 text-slate-300 border-slate-500'],
  };
  const availabilityBadge = (hospital) => {
    const [dot, cls] = AVAILABILITY_STYLE[hospital.availability] || AVAILABILITY_STYLE.offline;
    return h('span', { class: `inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border text-[0.6875rem] font-semibold whitespace-nowrap ${cls}`, title: hospital.availability_updated_at ? `Declared ${fmtFull(hospital.availability_updated_at)}` : 'Availability as recorded in ESRMS' },
      h('span', { class: `w-1.5 h-1.5 rounded-full ${dot}` }), hospital.availability_label);
  };

  // P1 red, P2 amber, P3/P4 neutral. Falls back to the reported condition when no priority was given.
  const TRIAGE_STYLE = { 1: 'bg-red-700 text-white border-red-500', 2: 'bg-amber-500/15 text-amber-300 border-amber-500/50', 3: 'bg-sky-500/10 text-sky-300 border-sky-500/40', 4: 'bg-slate-600/30 text-slate-300 border-slate-500' };
  const triageBadge = (s) => {
    const text = s.triage_label ? s.triage_label.toUpperCase().replace('·', '•') : s.condition_label.toUpperCase();
    const cls = s.triage_priority ? TRIAGE_STYLE[s.triage_priority] : (s.critical ? TRIAGE_STYLE[1] : TRIAGE_STYLE[4]);
    return h('span', { class: `inline-flex items-center px-1.5 py-0.5 rounded border text-[0.625rem] font-mono font-bold whitespace-nowrap ${cls}`, text });
  };

  const CONDITION_STYLE = { critical: 'text-red-300', unconscious: 'text-red-300', serious: 'text-amber-300' };

  // "Male, 45y" in the report's own words; never invents what EMS did not send.
  function patientLine(s) {
    const parts = [s.gender_label, s.patient_age != null ? `${s.patient_age}y` : null].filter(Boolean).join(', ');
    return [s.patient_name, parts].filter(Boolean).join(' · ') || 'Patient details not given';
  }

  function vitals(s) {
    const bp = s.bp_systolic != null ? `${s.bp_systolic}${s.bp_diastolic != null ? `/${s.bp_diastolic}` : ''} mmHg` : null;
    return [
      ['Heart Rate', s.heart_rate != null ? `${s.heart_rate} bpm` : null], ['Blood Pressure', bp],
      ['SpO2', s.spo2 != null ? `${s.spo2}%` : null], ['Temperature', s.temperature != null ? `${Number(s.temperature).toFixed(1)} °C` : null],
      ['Respiratory Rate', s.respiratory_rate != null ? `${s.respiratory_rate} /min` : null],
    ];
  }

  function timeline(entries) {
    if (!entries || !entries.length) return h('p', { class: 'text-xs text-slate-400', text: 'No events recorded yet.' });
    return h('ol', { class: 'space-y-1.5 border-l border-brand-borderSubtle ml-0.5' }, ...entries.map((e) => h('li', { class: 'relative pl-4 text-xs text-slate-300' },
      h('span', { class: 'absolute left-[-0.1875rem] top-1.5 w-1.5 h-1.5 rounded-full bg-brand-cyan' }),
      h('span', { class: 'font-mono text-slate-400', title: fmtFull(e.at), text: `${fmtWhen(e.at)} — ` }),
      h('span', { class: 'font-semibold text-white', text: e.label }),
      e.detail ? ` · ${e.detail}` : '', e.actor ? h('span', { class: 'text-slate-400', text: ` · ${e.actor}` }) : '')));
  }

  // Compact detail used by the dispatcher: what was sent, to whom, and every step since.
  function detail(s) {
    const row = (label, value) => value != null && value !== '' && h('div', { class: 'flex gap-3 py-1.5 border-b border-brand-borderSubtle/60 text-sm' },
      h('dt', { class: 'w-32 shrink-0 text-[0.6875rem] font-mono uppercase tracking-wide text-slate-400 pt-0.5', text: label }),
      h('dd', { class: 'min-w-0 flex-1 text-slate-100 break-words whitespace-pre-line' }, value));
    const measured = vitals(s).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(' · ');
    return h('div', {},
      h('dl', {},
        row('Report', h('span', { class: 'font-mono text-brand-cyan', text: s.code })),
        row('Status', statusBadge(s)),
        row('Hospital', s.hospital_name),
        row('Signal type', s.signal_type_label),
        row('Incident', s.incident ? `${s.incident.code} · ${s.incident.emergency_type}` : 'Not linked to an incident'),
        row('Patient', patientLine(s)),
        row('Condition', h('span', { class: `font-semibold ${CONDITION_STYLE[s.patient_condition] || ''}`, text: s.condition_label })),
        row('Priority', s.triage_label),
        row('Chief complaint', s.chief_complaint),
        row('Medic unit', [s.medic_unit, s.transport_label].filter(Boolean).join(' · ')),
        row('Vitals', measured),
        row('ETA', s.eta ? fmtFull(s.eta) : 'Not given'),
        row('Notes', s.additional_notes),
        row('Bay', s.bay_name),
        row('Received by', s.receiving_staff),
        row('Handoff notes', s.handoff_notes),
        row('Unable reason', s.rejection_reason),
        row('Sent by', s.sent_by_name || 'Dispatcher')),
      h('h3', { class: 'mt-4 mb-1.5 text-[0.6875rem] font-mono uppercase tracking-wide text-slate-400', text: 'Timeline' }),
      timeline(s.timeline));
  }

  // One indicator, two pages: Connected to ESRMS / Connection Lost.
  function connection(el, subject) {
    let realtime = 'connecting';
    let apiDown = false;
    function render() {
      const offline = !navigator.onLine || apiDown;
      const [text, glyph, tone, dot, title] = offline
        ? ['Connection Lost', 'wifi_off', 'border-red-500/50 bg-red-950/40 text-red-200', 'bg-red-500', 'ESRMS cannot be reached. Nothing can be sent or updated until the connection returns.']
        : realtime === 'live'
          ? ['Connected to ESRMS', 'wifi', 'border-emerald-500/40 bg-emerald-500/5 text-emerald-200', 'bg-emerald-400', `Live: ${subject} appear as they happen.`]
          : realtime === 'polling'
            ? ['Connection Lost', 'wifi_off', 'border-amber-500/50 bg-amber-950/40 text-amber-200', 'bg-amber-400', `Live updates are unavailable. ESRMS is checking for ${subject} every 15 seconds.`]
            : ['Connecting…', 'wifi_find', 'border-amber-500/40 bg-amber-500/5 text-amber-200', 'bg-amber-400', 'Connecting to live updates…'];
      el.className = `inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-mono font-semibold ${tone}`;
      el.querySelector('[data-text]').textContent = text;
      el.querySelector('[data-icon]').textContent = glyph;
      el.querySelector('[data-dot]').className = `w-2 h-2 rounded-full ${dot}${realtime === 'live' && !offline ? ' animate-pulse' : ''}`;
      el.title = title;
    }
    window.addEventListener('online', render);
    window.addEventListener('offline', render);
    render();
    return {
      realtime(state) { realtime = state; render(); },
      // Unreachable server or gateway. A 503 "not set up yet" is a configuration message, not a lost connection.
      api(err) { const down = Boolean(err) && (err.status === 0 || err.status === 502 || err.status === 504); if (down !== apiDown) { apiDown = down; render(); } },
    };
  }

  async function busy(btn, fn) {
    if (btn) { btn.disabled = true; btn.classList.add('opacity-60', 'cursor-wait'); }
    try { return await fn(); } finally { if (btn) { btn.disabled = false; btn.classList.remove('opacity-60', 'cursor-wait'); } }
  }

  return { fmtTime, fmtFull, fmtWhen, etaText, statusBadge, availabilityBadge, triageBadge, patientLine, vitals, timeline, detail, connection, busy, CONDITION_STYLE };
})();
