// Nebula service worker. Network-first for pages (data must be live — a cached "nothing
// due" is the failure this whole system exists to prevent); cache-first for static files.
// When offline, a cached page is shown WITH a banner saying it is stale.
const CACHE = "nebula-v2";          // v2: static names are content-hashed, so cache-first is safe

self.addEventListener("install", (e) => {
  e.waitUntil(self.skipWaiting());
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    })));
    return;
  }
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => caches.match(req).then((hit) => {
      if (!hit) return new Response("<h1>Nebula is offline</h1><p>RE is off or you're off the tailnet. " +
        "Google Calendar still has your alerts.</p>", { headers: { "Content-Type": "text/html" } });
      return hit.text().then((html) => new Response(html.replace("<main class=\"main\">",
        "<main class=\"main\"><div class=\"flash error\">⚠ OFFLINE — this is an old copy. Do not read it as current.</div>"),
        { headers: { "Content-Type": "text/html" } }));
    })));
  }
});
