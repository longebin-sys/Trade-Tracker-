// sw.js — Trade Tracker service worker
// Only caches the app shell (this page itself) so it can open offline.
// Everything else — OKX prices, Telegram, ForexFactory news, Google
// Sheets — always goes straight to the network, never cached, since
// stale prices/news would be actively harmful here.

const CACHE_NAME = 'tradetracker-v1';
const APP_SHELL = [
  './',
  './TradeTracker_Fixed.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.addAll(APP_SHELL).catch(() => {}) // don't fail install if one asset 404s
    )
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Only handle same-origin GET requests for the app shell itself.
  // Cross-origin calls (OKX, Telegram, ForexFactory, Sheets webhook)
  // are left completely alone — no interception, no caching.
  if (url.origin !== self.location.origin || event.request.method !== 'GET') {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const networkFetch = fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached); // offline fallback to last cached copy

      // Stale-while-revalidate: serve the cached page instantly if we
      // have one, and refresh the cache in the background either way
      return cached || networkFetch;
    })
  );
});
