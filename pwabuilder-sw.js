// Service Worker for Самолётик: Эра приключений
// Caches the game shell only. Requests to the leaderboard API are never cached.
const CACHE_NAME = "samoletik-shell-v2";
const APP_SHELL = ["/", "/index.html", "/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Never intercept cross-origin requests (including samoletik-server API).
  if (url.origin !== self.location.origin || request.method !== "GET") return;

  // Navigation: prefer fresh game from network; use cached shell when offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put("/", copy));
        return response;
      }).catch(async () => {
        return (await caches.match("/")) || (await caches.match("/index.html"));
      })
    );
    return;
  }

  // Cache only known app-shell files; let other same-origin requests go to network.
  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request))
    );
  }
});
