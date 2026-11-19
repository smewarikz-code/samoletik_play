// Service Worker for Самолётик: Эра приключений
// Version bump forces mobile browsers to discard the previous shell and stale game assets.
const CACHE_NAME = "samoletik-shell-v3";
const APP_SHELL = [
  "/", "/index.html", "/manifest.json", "/icon-192.png", "/icon-512.png",
  ...Array.from({length: 6}, (_, i) => [
    "/backgrounds/" + ["tropical.jpg", "ice.jpg", "neon-city.jpg", "desert.jpg", "valley.jpg", "storm.jpg"][i]
  ]).flat(),
  ...["classic", "gold", "diamond", "cosmic", "emerald", "storm", "phoenix"].map(id => "/planes/" + id + ".png"),
  ...Array.from({length: 12}, (_, i) => "/pillars/pillar-" + i + ".png")
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))
  ));
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || request.method !== "GET") return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).then(response => {
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put("/", copy));
      return response;
    }).catch(async () => (await caches.match("/")) || (await caches.match("/index.html"))));
    return;
  }

  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(request).then(cached => cached || fetch(request)));
  }
});
