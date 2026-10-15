// Citizen Announcement Viewer. Shows only what /api/announcements/public returns:
// the server applies the status, publication window and audience/barangay rules.
(function () {
  const root = document.getElementById('ann-viewer');
  if (!root) return;
  const { h, icon, fmtDate, fmtTime, CATEGORY_STYLE, PRIORITY_STYLE, api } = window.AnnUI;
  const cfg = JSON.parse(document.getElementById('ann-viewer-config').textContent);
  const listEl = document.getElementById('ann-viewer-list');
  const updatedEl = document.getElementById('ann-updated');
  const barangayForm = document.getElementById('ann-barangay-form');
  const barangayInput = document.getElementById('ann-barangay');
  const STORE_KEY = 'esrms.viewer.barangay';  // per-browser convenience only

  let seen = null;      // ids from the previous load, to highlight new posts
  let timer = null;     // refetch when the next scheduled post goes live / one expires
  let loadSeq = 0;
  let lastOk = false;

  if (barangayInput) {
    try { barangayInput.value = localStorage.getItem(STORE_KEY) || ''; } catch (_) {}
    barangayForm.addEventListener('submit', (e) => {
      e.preventDefault();
      try { localStorage.setItem(STORE_KEY, barangayInput.value.trim()); } catch (_) {}
      load();
    });
  } else {
    barangayForm.addEventListener('submit', (e) => e.preventDefault());
  }

  async function load() {
    const seq = ++loadSeq;
    const params = new URLSearchParams();
    const barangay = barangayInput?.value.trim();
    if (barangay) params.set('barangay', barangay);
    listEl.setAttribute('aria-busy', 'true');
    try {
      const data = await api(`${root.dataset.api}?${params}`);
      if (seq !== loadSeq) return;
      render(data.items);
      scheduleNext(data.next_change_at);
      lastOk = true;
      updatedEl.textContent = `Updated ${fmtTime(new Date().toISOString())}`;
    } catch (err) {
      if (seq !== loadSeq) return;
      lastOk = false;
      showError(err.message);
    } finally {
      if (seq === loadSeq) listEl.setAttribute('aria-busy', 'false');
    }
  }

  function scheduleNext(iso) {
    clearTimeout(timer);
    if (!iso) return;
    const ms = new Date(iso) - Date.now() + 1500;
    // setTimeout overflows past ~24.8 days; the periodic poll covers anything later.
    if (ms > 0 && ms < 2 ** 31 - 1) timer = setTimeout(load, ms);
  }

  function showError(message) {
    const banner = h('div', { class: 'rounded-xl border border-red-500/40 bg-red-950/50 p-4 flex items-start gap-3', role: 'alert', 'data-error': true },
      icon('cloud_off', 'text-red-400'),
      h('div', { class: 'flex-1' },
        h('p', { class: 'text-sm text-red-100', text: message }),
        h('p', { class: 'text-xs text-red-200/70 mt-1', text: 'Retrying automatically. In an emergency, call 911.' })),
      h('button', { type: 'button', class: 'text-xs underline text-red-200', onclick: load, text: 'Retry' }));
    // Keep showing the last good list under the banner.
    listEl.querySelector('[data-error]')?.remove();
    if (listEl.querySelector('article')) listEl.prepend(banner);
    else listEl.replaceChildren(banner);
  }

  function render(items) {
    const ids = new Set(items.map((a) => a.id));
    if (!items.length) {
      listEl.replaceChildren(h('div', { class: 'rounded-2xl border border-dashed border-white/10 bg-[#0f1729]/60 p-10 text-center' },
        icon('notifications_off', 'text-slate-500 text-[2.5rem]'),
        h('p', { class: 'text-slate-300 mt-2', text: 'No active announcements right now.' }),
        h('p', { class: 'text-xs text-slate-500 mt-1', text: 'New advisories appear here automatically.' })));
    } else {
      listEl.replaceChildren(...items.map((a) => card(a, seen && !seen.has(a.id))));
    }
    seen = ids;
  }

  function card(a, isNew) {
    const style = CATEGORY_STYLE[a.category] || CATEGORY_STYLE.general;
    const critical = a.priority === 'critical' || a.category === 'emergency';
    const important = critical || a.priority === 'high' || a.category === 'important';
    return h('article', {
      class: `relative rounded-2xl p-5 border transition ${critical
        ? 'border-red-600 bg-gradient-to-br from-[#3a0b14] to-[#0f1729] shadow-[0_0_30px_-12px_rgba(220,38,38,0.8)]'
        : important ? 'border-red-500/40 bg-[#0f1729]' : 'border-white/[0.07] bg-[#0f1729]'} ${isNew ? 'ring-2 ring-red-400/70' : ''}`,
      'aria-label': `${important ? 'Important: ' : ''}${a.title}`,
    },
      h('div', { class: 'flex gap-4' },
        h('span', { class: `shrink-0 w-12 h-12 rounded-xl flex items-center justify-center ${style.tile} ${critical ? 'animate-pulse' : ''}` }, icon(style.icon, 'text-[1.625rem]')),
        h('div', { class: 'flex-1 min-w-0' },
          h('div', { class: 'flex flex-wrap items-center gap-2' },
            h('span', { class: `text-[0.6875rem] font-bold uppercase tracking-wide ${style.label}`, text: cfg.categories[a.category] }),
            h('span', { class: `px-1.5 py-px rounded border text-[0.625rem] font-semibold ${PRIORITY_STYLE[a.priority]}`, text: `${cfg.priorities[a.priority]} priority` }),
            a.is_pinned && h('span', { class: 'flex items-center gap-0.5 text-[0.6875rem] text-slate-300' }, icon('push_pin', 'text-[0.875rem]'), 'Featured'),
            isNew && h('span', { class: 'px-1.5 py-px rounded bg-red-600 text-white text-[0.625rem] font-bold', text: 'NEW' })),
          h('h2', { class: `font-bold text-white mt-1 break-words ${critical ? 'text-xl' : 'text-lg'}`, text: a.title }),
          h('p', { class: 'text-sm text-slate-200 mt-2 whitespace-pre-line break-words', text: a.message }),
          h('div', { class: 'flex flex-wrap gap-x-4 gap-y-1 mt-3 text-xs text-slate-400' },
            h('span', { class: 'flex items-center gap-1' }, icon('calendar_month', 'text-[1rem] text-red-400'), `${fmtDate(a.published_at)} • ${fmtTime(a.published_at)}`),
            a.barangay && h('span', { class: 'flex items-center gap-1' }, icon('location_on', 'text-[1rem]'), `Brgy. ${a.barangay}`),
            a.expires_at && h('span', { class: 'flex items-center gap-1' }, icon('timer', 'text-[1rem]'), `Until ${fmtDate(a.expires_at)} ${fmtTime(a.expires_at)}`)))));
  }

  const liveEl = document.getElementById('ann-live');
  window.AnnouncementLive.start({
    ...cfg.realtime,
    onChange: load,
    onStatus: (s) => {
      const [dot, text] = { live: ['bg-emerald-400', 'Live updates'], connecting: ['bg-amber-400 animate-pulse', 'Connecting…'], polling: ['bg-amber-400', 'Auto-refreshing'] }[s];
      liveEl.firstElementChild.className = `w-2 h-2 rounded-full ${dot}`;
      liveEl.lastElementChild.textContent = text;
    },
  });
  // Retry quickly after an error without hammering the server.
  setInterval(() => { if (!lastOk) load(); }, 10000);

  load();
})();
