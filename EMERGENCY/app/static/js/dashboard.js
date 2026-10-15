// Organization dispatch dashboard behaviour.

// UTC companion to the PHT clock driven by esrms.js.
(function () {
  const utc = document.getElementById('utc-clock');
  if (!utc) return;
  const tick = () => { utc.textContent = `| UTC ${new Date().toISOString().slice(11, 19)}`; };
  tick();
  setInterval(tick, 1000);
})();

// Account menu: toggle on avatar click, close on outside click or Escape.
(function () {
  const trigger = document.getElementById('account-trigger');
  const menu = document.getElementById('account-menu');
  if (!trigger || !menu) return;

  function setOpen(open) {
    menu.dataset.open = String(open);
    trigger.setAttribute('aria-expanded', String(open));
    if (open) menu.querySelector('[role^=menuitem]')?.focus();
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(menu.dataset.open !== 'true');
  });
  document.addEventListener('click', (e) => {
    if (menu.dataset.open === 'true' && !menu.contains(e.target)) setOpen(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menu.dataset.open === 'true') {
      setOpen(false);
      trigger.focus();
    }
  });

  // Console duty status, saved per dispatcher account on the server (/api/dispatch/duty).
  // The menu only shows a status the server confirmed; only On Duty dispatchers receive new incidents.
  const dutyMenu = menu.querySelector('[data-duty-menu]');
  if (dutyMenu) {
    const dot = document.querySelector('[data-duty-dot]');
    const options = dutyMenu.querySelectorAll('[data-duty]');
    const url = dutyMenu.dataset.dutyUrl;
    const LABEL = { on_duty: 'On duty', on_break: 'On break', off_duty: 'Off duty' };
    let current = null;
    let saving = false;
    const notify = (text, kind) => (window.AnnUI ? window.AnnUI.toast(text, kind) : console[kind === 'error' ? 'error' : 'log'](text));
    const confirmBox = (title, text, ok) => (window.ESRMSDispatch?.confirm
      ? window.ESRMSDispatch.confirm(title, text, ok)
      : Promise.resolve(window.confirm(`${title}\n\n${text}`)));

    function applyDuty(value) {
      current = value;
      options.forEach((opt) => {
        const active = opt.dataset.duty === value;
        opt.setAttribute('aria-checked', String(active));
        opt.querySelector('[data-check]').classList.toggle('hidden', !active);
        if (active && dot) dot.className = dot.className.replace(/bg-(?!brand)\S+/, opt.dataset.dot);
      });
      if (dot) dot.title = `Console status: ${LABEL[value] || value}`;
    }

    async function request(method, body) {
      const res = await fetch(url, {
        method, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, data };
    }

    async function load() {
      try {
        const { ok, data } = await request('GET');
        if (ok) applyDuty(data.status);
        else notify(data.error || 'Could not load your console status.', 'error');
      } catch (_) { notify('Could not load your console status.', 'error'); }
    }

    async function change(value, confirmed = false) {
      if (saving || (value === current && !confirmed)) return;
      saving = true;
      dutyMenu.setAttribute('aria-busy', 'true');
      try {
        const { ok, status, data } = await request('POST', { status: value, confirm: confirmed });
        if (ok) {
          applyDuty(data.status);
          notify(`Console status set to ${LABEL[data.status]}.${data.status === 'on_duty' ? ' You will receive new incidents.' : ' New incidents are paused for you.'}`, 'success');
          document.dispatchEvent(new CustomEvent('esrms:duty-changed', { detail: { status: data.status } }));
        } else if (status === 409 && data.code === 'active_incidents') {
          saving = false;
          const list = (data.active || []).map((i) => `${i.code} (${i.emergency_type})`).join(', ');
          if (await confirmBox(`Switch to ${LABEL[value]}?`, `${data.error} ${list}`, `Switch to ${LABEL[value]}`)) return change(value, true);
        } else {
          notify(data.error || 'Could not update your console status.', 'error');
        }
      } catch (_) {
        notify('Could not update your console status. Check your connection.', 'error');
      } finally {
        saving = false;
        dutyMenu.setAttribute('aria-busy', 'false');
        if (current) applyDuty(current);   // a failed change leaves the previous, confirmed status showing
      }
    }

    options.forEach((opt) => opt.addEventListener('click', () => change(opt.dataset.duty)));
    try { localStorage.removeItem('esrms.duty'); } catch (_) {}   // the old browser-only setting
    load();
    // Another tab or device may have changed it; recheck whenever this tab comes back.
    document.addEventListener('visibilitychange', () => { if (!document.hidden && !saving) load(); });
    document.addEventListener('esrms:duty-observed', (e) => { if (!saving && e.detail?.status && e.detail.status !== current) applyDuty(e.detail.status); });
  }
})();

// Primary nav tabs.
(function () {
  const tabs = document.querySelectorAll('[data-tab]');
  const panels = document.querySelectorAll('[data-tab-panel]');
  tabs.forEach((tab) => tab.addEventListener('click', () => {
    tabs.forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
    panels.forEach((p) => p.classList.toggle('hidden', p.dataset.tabPanel !== tab.dataset.tab));
  }));
})();

// "+ Note" appends a dispatcher entry to the CAD narrative log.
(function () {
  const toggle = document.getElementById('cad-note-toggle');
  const form = document.getElementById('cad-note-form');
  const log = document.getElementById('cad-log');
  if (!toggle || !form || !log) return;

  toggle.addEventListener('click', () => {
    form.classList.toggle('hidden');
    if (!form.classList.contains('hidden')) form.note.focus();
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = form.note.value.trim();
    if (!text) return;
    const entry = document.createElement('div');
    entry.className = 'border-l-2 border-brand-cyan pl-2';
    const stamp = document.createElement('span');
    stamp.className = 'text-slate-400 text-[0.625rem]';
    stamp.textContent = `${new Date().toTimeString().slice(0, 8)} | Dispatcher:`;
    const body = document.createElement('p');
    body.className = 'text-slate-200';
    body.textContent = text;
    entry.append(stamp, body);
    log.append(entry);
    log.scrollTop = log.scrollHeight;
    form.reset();
  });
})();
