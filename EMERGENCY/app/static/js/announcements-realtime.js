// Live updates for the announcement dashboard and the Citizen Announcement Viewer.
//
// Subscribes to public.announcement_feed (a change counter with no content) over
// Supabase Realtime and calls onChange() whenever an announcement is created,
// edited, archived or deleted. The caller then refetches from the Flask API,
// which decides what this viewer may see. Falls back to polling when Realtime is
// unavailable, and always polls slowly as a safety net.
window.AnnouncementLive = (function () {
  const FAST_POLL_MS = 15000;  // Realtime down
  const SLOW_POLL_MS = 60000;  // Realtime up, safety net only

  // `table` is the change-counter table to watch (announcement_feed or dispatch_feed).
  function start({ url, key, onChange, onStatus, table = 'announcement_feed' }) {
    let channel = null;
    let client = null;
    let timer = null;
    let live = false;
    let stopped = false;

    function setStatus(state) {
      live = state === 'live';
      onStatus?.(state);
      schedulePoll();
    }

    function schedulePoll() {
      clearInterval(timer);
      if (!stopped) timer = setInterval(onChange, live ? SLOW_POLL_MS : FAST_POLL_MS);
    }

    if (url && key && window.supabase?.createClient) {
      try {
        client = window.supabase.createClient(url, key, {
          auth: { persistSession: false, autoRefreshToken: false },
        });
        channel = client
          .channel('feed-' + table)
          .on('postgres_changes', { event: '*', schema: 'public', table }, () => onChange())
          .subscribe((status) => {
            if (stopped) return;
            if (status === 'SUBSCRIBED') setStatus('live');
            else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') setStatus('polling');
          });
        setStatus('connecting');
      } catch (err) {
        console.warn('Realtime unavailable, polling instead.', err);
        setStatus('polling');
      }
    } else {
      setStatus('polling');
    }

    // Catch up after the tab was hidden or the network dropped.
    const onVisible = () => { if (document.visibilityState === 'visible') onChange(); };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('online', onChange);

    function stop() {
      stopped = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('online', onChange);
      if (client && channel) client.removeChannel(channel);
      channel = null;
    }
    window.addEventListener('pagehide', stop, { once: true });
    return stop;
  }

  return { start };
})();

// Small helpers shared by both announcement pages.
window.AnnUI = (function () {
  // Build DOM safely: text always goes through textContent, never innerHTML.
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const icon = (name, cls = '') => h('span', { class: `material-symbols-outlined ${cls}`, 'aria-hidden': 'true', text: name });

  const dateFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', month: 'short', day: 'numeric', year: 'numeric' });
  const timeFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', hour: '2-digit', minute: '2-digit', hour12: true });
  const fmtDate = (iso) => (iso ? dateFmt.format(new Date(iso)) : '—');
  const fmtTime = (iso) => (iso ? timeFmt.format(new Date(iso)) : '');

  // Category styling, matching the reference design: red for urgent, blue for informational.
  const CATEGORY_STYLE = {
    emergency:   { icon: 'warning',       tile: 'bg-red-600/90 text-white',          label: 'text-red-400' },
    important:   { icon: 'campaign',      tile: 'bg-red-600/80 text-white',          label: 'text-red-400' },
    advisory:    { icon: 'thunderstorm',  tile: 'bg-amber-500/80 text-white',        label: 'text-amber-300' },
    general:     { icon: 'info',          tile: 'bg-blue-600/80 text-white',         label: 'text-blue-400' },
    maintenance: { icon: 'build',         tile: 'bg-slate-600/80 text-white',        label: 'text-slate-300' },
  };
  const PRIORITY_STYLE = {
    critical: 'bg-red-600 text-white border-red-500',
    high:     'bg-red-500/15 text-red-300 border-red-500/40',
    medium:   'bg-amber-500/10 text-amber-300 border-amber-500/30',
    low:      'bg-slate-500/10 text-slate-300 border-slate-500/30',
  };

  let toastWrap;
  function toast(message, kind = 'success') {
    if (!toastWrap) {
      toastWrap = h('div', { class: 'fixed z-[100] bottom-4 right-4 left-4 sm:left-auto sm:w-96 flex flex-col gap-2', 'aria-live': 'polite' });
      document.body.append(toastWrap);
    }
    const styles = {
      success: ['check_circle', 'border-emerald-500/40 bg-emerald-950/90 text-emerald-100'],
      error:   ['error',        'border-red-500/50 bg-red-950/95 text-red-100'],
      info:    ['info',         'border-sky-500/40 bg-slate-900/95 text-sky-100'],
    }[kind];
    const el = h('div', { class: `flex items-start gap-2 rounded-xl border px-4 py-3 text-sm shadow-2xl ${styles[1]}`, role: kind === 'error' ? 'alert' : 'status' },
      icon(styles[0], 'text-[1.25rem]'), h('p', { class: 'flex-1', text: message }));
    toastWrap.append(el);
    setTimeout(() => el.remove(), kind === 'error' ? 8000 : 4500);
  }

  // fetch wrapper: resolves with JSON on 2xx, otherwise throws an Error carrying the server message.
  async function api(url, { method = 'GET', body } = {}) {
    let res;
    try {
      res = await fetch(url, {
        method,
        credentials: 'same-origin',
        headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (_) {
      throw Object.assign(new Error('Network error. Check your connection and try again.'), { status: 0 });
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) {
      throw Object.assign(new Error(data.error || `Request failed (${res.status}).`), { status: res.status, fields: data.fields });
    }
    return data;
  }

  return { h, icon, fmtDate, fmtTime, CATEGORY_STYLE, PRIORITY_STYLE, toast, api };
})();
