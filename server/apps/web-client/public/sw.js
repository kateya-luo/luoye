const CACHE = 'clear-meeting-v6';
self.addEventListener('install', (event) => { self.skipWaiting(); event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(['/manifest.webmanifest']))); });
self.addEventListener('activate', (event) => { event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate') { event.respondWith(fetch(event.request).catch(() => caches.match('/'))); return; }
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
