/* Deputat service worker (served from /sw.js so its scope is the whole app).
 *
 * - App shell (CSS/JS/vendor/icons, /heute, /offline) is precached under a versioned cache name.
 * - Served by GET /sw.js (app/web/assets.py), which injects the asset version (a hash over all static
 *   files) and the per-file content hashes: any asset change yields a new cache name, so the browser
 *   installs the new worker, which re-precaches the shell and drops the old cache.
 * - Static assets: cache-first by exact URL (the ?v=<content hash> query from static_url() busts it when
 *   the file changes), then network (stored), offline fallback ignoring the query string.
 * - Page navigations (HTML): network-first; successful same-origin pages are stored, offline falls back
 *   to the cached page ("/" -> "/heute") or /offline.
 * - API GET: network-first with cache fallback (never /api/csrf, /api/timer – a stale timer status
 *   would undo optimistic state –, exports, backups, sensors).
 * - HTMX partial GETs: network only (stale fragments are worse than a failed refresh).
 * - Never touches non-GET requests: timer POSTs are queued by static/js/offline-queue.js.
 *
 * Redirects are passed through untouched (navigations and the page's fetch(redirect: "manual") get
 * the opaqueredirect they asked for), so an expired Cloudflare Access session is detected by the page
 * and is never cached. Only plain 200 same-origin responses ("basic", not redirected) are stored.
 *
 * The cache name changes automatically with every asset (and with this file); no manual bump needed.
 */
const ASSET_VERSION = "__ASSET_VERSION__"; // injected by the /sw.js route
const ASSET_HASHES = {}; // injected by the /sw.js route: {"/static/css/app.css": "<hash>", ...}
const CACHE_VERSION = "deputat-shell-" + ASSET_VERSION;

// Same URLs as the pages use (templates: static_url() -> /static/...?v=<hash>). Icons stay unversioned:
// the web manifest references them without a query.
function versioned(url) {
  const hash = ASSET_HASHES[url];
  return hash ? url + "?v=" + hash : url;
}

const SHELL_ASSETS = [
  "/heute",
  "/offline",
  versioned("/static/css/app.css"),
  versioned("/static/js/app.js"),
  versioned("/static/js/timer.js"),
  versioned("/static/js/offline-queue.js"),
  versioned("/static/vendor/htmx.min.js"),
  versioned("/static/vendor/alpine.min.js"),
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/manifest.webmanifest",
];

const API_NO_CACHE = ["/api/csrf", "/api/timer", "/api/export", "/api/backup", "/api/sensors", "/api/docs", "/api/openapi.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) =>
        // Individually, so one failing entry (e.g. /heute while the server is busy) does not abort install.
        Promise.all(SHELL_ASSETS.map((url) =>
          fetch(url, { credentials: "same-origin", redirect: "manual", cache: "no-cache" })
            .then((res) => (cacheable(res) ? cache.put(url, res) : null))
            .catch(() => null)
        ))
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function cacheable(res) {
  return !!res && res.ok && res.status === 200 && res.type === "basic" && !res.redirected;
}

function isStatic(url) {
  return url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest";
}

function stripSearch(url) {
  return url.origin + url.pathname;
}

async function put(key, res) {
  try {
    const cache = await caches.open(CACHE_VERSION);
    await cache.put(key, res);
  } catch (_) { /* quota etc. */ }
}

async function staticFirst(request, url) {
  const exact = await caches.match(request);
  if (exact) return exact;
  try {
    const res = await fetch(request);
    if (cacheable(res)) put(request, res.clone());
    return res;
  } catch (err) {
    const loose = await caches.match(request, { ignoreSearch: true });
    if (loose) return loose;
    throw err;
  }
}

async function pageNetworkFirst(request, url) {
  const key = stripSearch(url);
  try {
    const res = await fetch(request);
    const type = res.headers.get("content-type") || "";
    if (cacheable(res) && type.includes("text/html")) put(key, res.clone());
    return res; // includes opaqueredirect (Cloudflare Access login) untouched
  } catch (_) {
    const cached = (await caches.match(key)) ||
      (url.pathname === "/" ? await caches.match(url.origin + "/heute") : undefined) ||
      (await caches.match(url.origin + "/offline"));
    if (cached) return cached;
    return new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

async function apiNetworkFirst(request) {
  try {
    const res = await fetch(request);
    const type = res.headers.get("content-type") || "";
    if (cacheable(res) && type.includes("application/json")) put(request, res.clone());
    return res;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // never cache POST/PATCH/DELETE
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === "/healthz" || url.pathname === "/sw.js") return;

  if (isStatic(url)) {
    event.respondWith(staticFirst(request, url));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    if (API_NO_CACHE.some((p) => url.pathname === p || url.pathname.startsWith(p + "/"))) return;
    event.respondWith(apiNetworkFirst(request));
    return;
  }
  if (request.headers.get("HX-Request")) return; // HTMX partials: network only
  if (request.mode === "navigate") {
    event.respondWith(pageNetworkFirst(request, url));
  }
});
