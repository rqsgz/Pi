/**
 * NeuroLux Service Worker
 * Enables PWA installability + offline caching for the /app mobile panel.
 */

const CACHE_NAME = "neuroflux-v2";
const APP_START_URL = "/app";

const PRECACHE_URLS = [
  APP_START_URL,
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

// ── Install: pre-cache core assets ─────────────────────────────────
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(PRECACHE_URLS).catch((err) => {
        // Non-critical — app works online without pre-cache
        console.warn("SW pre-cache partial:", err);
      });
    })
  );
  // Activate immediately (don't wait for old SW to unload)
  self.skipWaiting();
});

// ── Activate: clean old caches ─────────────────────────────────────
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      );
    })
  );
  // Claim all clients so the SW controls pages immediately
  self.clients.claim();
});

// ── Fetch: network-first for API, cache-first for static ───────────
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API calls — network only (no stale data)
  if (url.pathname.startsWith("/api/")) {
    return; // let the browser handle normally
  }

  // Static assets & page — stale-while-revalidate
  if (
    url.pathname === APP_START_URL ||
    url.pathname.startsWith("/static/")
  ) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) => {
        return cache.match(event.request).then((cached) => {
          const fetchPromise = fetch(event.request)
            .then((response) => {
              if (response.ok) {
                cache.put(event.request, response.clone());
              }
              return response;
            })
            .catch(() => cached); // offline → use cache
          return cached || fetchPromise;
        });
      })
    );
  }
});
