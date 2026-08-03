/* Service worker: cache the app shell so the PWA opens instantly;
   market data (/api/*) always goes to the network. */
const CACHE = "ota-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js",
  "/static/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    caches.match(e.request).then((hit) =>
      hit ||
      fetch(e.request).then((r) => {
        if (r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return r;
      }).catch(() => caches.match("/"))
    )
  );
});
