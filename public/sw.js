const CACHE_NAME = 'evolt-v1';
const ASSETS = [
  '/pwa',
  '/index.html',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
});

self.addEventListener('fetch', (e) => {
    // Якщо запит іде до нашого API, повністю ігноруємо кеш і беремо дані з мережі
    if (e.request.url.includes('/api/')) {
        return e.respondWith(fetch(e.request));
    }

    // Для всіх інших файлів (карти, стилі) залишаємо роботу з кешем
    e.respondWith(
        caches.match(e.request).then((response) => {
            return response || fetch(e.request);
        })
    );
});