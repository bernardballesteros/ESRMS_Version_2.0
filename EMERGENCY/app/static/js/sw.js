// ESRMS push service worker (served at /sw.js so it covers the whole site).
// It only handles push notifications; it does not cache pages or intercept requests.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

// Only ever open pages of this site, whatever the payload says.
function safeUrl(path) {
  try {
    const url = new URL(path || '/dashboard', self.location.origin);
    return url.origin === self.location.origin ? url.href : `${self.location.origin}/dashboard`;
  } catch (_) {
    return `${self.location.origin}/dashboard`;
  }
}

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { /* show the generic notification */ }
  const urgent = data.category === 'emergency_incidents' || data.category === 'new_reports';
  event.waitUntil(self.registration.showNotification(data.title ? `ESRMS · ${data.title}` : 'ESRMS notification', {
    body: data.body || '',
    tag: data.tag || undefined,          // a retried delivery replaces the earlier copy instead of stacking
    icon: '/static/img/emblem.svg',
    requireInteraction: urgent,
    data: { url: safeUrl(data.url) },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data?.url || safeUrl('/dashboard');
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = windows.find((w) => w.url.startsWith(self.location.origin));
    if (open) {
      await open.focus();
      if ('navigate' in open && open.url !== target) await open.navigate(target).catch(() => {});
      return;
    }
    await self.clients.openWindow(target);
  })());
});

// The browser replaced the subscription (keys rotated): tell the server, using the user's session cookie.
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const options = event.oldSubscription?.options;
    if (!options) return;
    const subscription = await self.registration.pushManager.subscribe(options);
    if (event.oldSubscription) {
      await fetch('/api/account/push/unsubscribe', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: event.oldSubscription.endpoint }),
      }).catch(() => {});
    }
    await fetch('/api/account/push/subscribe', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscription: subscription.toJSON() }),
    });
  })());
});
