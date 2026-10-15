// Notification delivery that respects the user's saved preferences (Notifications settings page).
//
// Every in-page alert goes through ESRMSNotify.notify(category, ...). A category the user
// switched off is dropped. Otherwise it is shown as an in-console toast and, when "Desktop
// alerts while open" is on and the console is in the background, as a desktop notification.
//
// Email and push (which also arrive when ESRMS is closed) are sent by the server from the same
// events (app/delivery.py). If this browser has a push subscription, the service worker shows
// the system notification, so the tab does not raise a second one.
window.ESRMSNotify = (function () {
  const cfgEl = document.getElementById('notify-config');
  const cfg = cfgEl ? JSON.parse(cfgEl.textContent) : null;
  const { toast, api } = window.AnnUI;

  // null until loaded (or if loading fails): alerts stay on rather than silently hiding an emergency.
  let prefs = null;

  let pushActive = false;   // this browser is subscribed to server push
  if ('serviceWorker' in navigator && 'PushManager' in window) {
    navigator.serviceWorker.getRegistration('/')
      .then((reg) => (reg ? reg.pushManager.getSubscription() : null))
      .then((sub) => { pushActive = Boolean(sub); })
      .catch(() => {});
  }

  const supported = 'Notification' in window;
  const permission = () => (supported ? Notification.permission : 'unsupported');
  const allows = (category) => !prefs || prefs[category] !== false;

  // Hands the notification to the browser. Returns false if it could not even be attempted.
  function desktop(title, body, tag) {
    if (permission() !== 'granted') return false;
    try {
      const n = new Notification(title, { body, tag, icon: cfg?.icon });
      n.onclick = () => { window.focus(); n.close(); };
      return true;
    } catch (_) {
      return false;  // some mobile browsers only allow notifications from a service worker
    }
  }

  function notify(category, title, body, { kind = 'info', tag } = {}) {
    if (!allows(category)) return false;
    toast(body ? `${title}: ${body}` : title, kind);
    if (prefs?.browser_enabled && !pushActive && (document.hidden || !document.hasFocus())) desktop(`ESRMS · ${title}`, body, tag);
    return true;
  }

  // ---- Security alerts: account changes made since this browser last looked.
  const markerKey = cfg ? `esrms.securityEvent.${cfg.userId}` : null;
  function seen(eventId) {
    if (!markerKey || !eventId) return;
    try {
      if (Number(localStorage.getItem(markerKey) || 0) < eventId) localStorage.setItem(markerKey, String(eventId));
    } catch (_) {}
  }
  async function checkSecurityEvents() {
    let marker = null;
    try { marker = localStorage.getItem(markerKey); } catch (_) { return; }
    try {
      const { items } = await api(`${cfg.securityEvents}?after=${marker || 0}`);
      if (!items.length) return;
      seen(items[0].id);
      if (marker === null) return;  // first visit from this browser: nothing is "new" yet
      for (const e of items.slice(0, 3).reverse()) {
        notify('security_alerts', 'Security alert', `${e.label} on ${new Date(e.created_at).toLocaleString()}. If this wasn't you, change your password.`, { kind: 'error', tag: `sec-${e.id}` });
      }
    } catch (_) { /* the Security page reports load problems itself */ }
  }

  // ---- System announcements: newly published ones this user may see.
  let knownAnnouncements = null;
  async function checkAnnouncements() {
    try {
      const { items } = await api(cfg.announcements);
      const fresh = knownAnnouncements ? items.filter((a) => !knownAnnouncements.has(a.id)) : [];
      knownAnnouncements = new Set(items.map((a) => a.id));
      for (const a of fresh.slice(0, 3)) {
        notify('system_announcements', 'New announcement', a.title, { kind: a.priority === 'critical' ? 'error' : 'info', tag: `ann-${a.id}` });
      }
    } catch (_) { /* retried on the next change or poll */ }
  }

  const ready = (async () => {
    if (!cfg) return;
    try {
      prefs = (await api(cfg.preferences)).preferences;
    } catch (err) {
      console.warn('Notification preferences unavailable; using defaults.', err);
    }
    if (!cfg.watch) return;
    checkSecurityEvents();
    setInterval(checkSecurityEvents, 5 * 60 * 1000);
    if (allows('system_announcements') && window.AnnouncementLive) {
      checkAnnouncements();
      window.AnnouncementLive.start({ ...cfg.realtime, table: 'announcement_feed', onChange: checkAnnouncements });
    }
  })();

  return {
    ready, notify, desktop, allows, permission, supported, seen,
    setPreferences(next) { prefs = next; },
    setPushActive(on) { pushActive = on; },
  };
})();
