// Service Worker dla OML Alert PWA
// - obsługuje przychodzące Web Push
// - kliknięcie powiadomienia otwiera/aktywuje aplikację

const CACHE_NAME = 'oml-alert-v6';
const PRECACHE = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // network-first dla nawigacji (HTML), żeby update PWA był szybki
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('./index.html')));
    return;
  }
  // bez cache dla danych dynamicznych (last_run.json itp.) i klucza VAPID
  if (url.pathname.includes('/data/') || url.pathname.endsWith('vapid-public-key.txt')) {
    event.respondWith(fetch(req));
    return;
  }
  // cache-first dla reszty (assety: ikony, sw, manifest)
  event.respondWith(caches.match(req).then((res) => res || fetch(req)));
});

self.addEventListener('push', (event) => {
  let payload = { title: 'OML Alert', body: 'Sprawdź kurs OML', data: {} };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch (_) {
      payload.body = event.data.text();
    }
  }

  const options = {
    body: payload.body || '',
    icon: './icons/icon-192.png',
    badge: './icons/icon-192.png',
    tag: payload.tag || 'oml-price',
    renotify: true,
    data: payload.data || {},
    vibrate: [80, 40, 80]
  };

  event.waitUntil(self.registration.showNotification(payload.title || 'OML Alert', options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) return w.focus();
      }
      if (clients.openWindow) return clients.openWindow('./');
    })
  );
});
