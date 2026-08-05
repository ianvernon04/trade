/* Service worker: cache the app shell so the PWA opens instantly;
   market data (/api/*) always goes to the network. Shell responses are
   served stale-while-revalidate: the cached copy renders immediately and a
   background fetch refreshes the cache, so UI updates land on the next
   visit without needing a cache-name bump. */
const CACHE = "ota-v4";
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
    caches.match(e.request).then((hit) => {
      const refresh = fetch(e.request).then((r) => {
        if (r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return r;
      }).catch(() => hit || caches.match("/"));
      return hit || refresh;
    })
  );
});
