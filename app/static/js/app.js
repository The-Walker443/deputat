/* Deputat – global helpers (loaded on every page, before Alpine).
 *
 * window.deputat.csrfToken()            -> current CSRF token (from <meta name="csrf-token">)
 * window.deputat.fetch(url, options)    -> fetch() with same-origin credentials + X-CSRF-Token header
 * window.deputat.fetchJSON(url, opts)   -> JSON helper: body object is serialized, response parsed,
 *                                          non-2xx rejects with Error(detail)
 * window.deputat.flash(message, kind)   -> show a dismissible banner (info|success|warn|danger)
 *
 * On plain http outside localhost (LAN access) a dismissible banner points to the https BASE_URL
 * (<meta name="deputat-base-url">): service worker (offline timer) and installation need https.
 *
 * Alpine components must be registered from external files via
 *   document.addEventListener("alpine:init", () => Alpine.data("name", () => ({ ... })));
 * because the CSP build of Alpine cannot evaluate inline expressions.
 */
(function () {
  "use strict";

  const FLASH_TIMEOUT_MS = 6000;

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function apiFetch(url, options) {
    const opts = Object.assign({ credentials: "same-origin" }, options || {});
    const headers = new Headers(opts.headers || {});
    const method = (opts.method || "GET").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", csrfToken());
    }
    opts.headers = headers;
    return fetch(url, opts);
  }

  async function fetchJSON(url, options) {
    const opts = Object.assign({}, options || {});
    const headers = new Headers(opts.headers || {});
    headers.set("Accept", "application/json");
    if (opts.body !== undefined && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      headers.set("Content-Type", "application/json");
      opts.body = JSON.stringify(opts.body);
    }
    opts.headers = headers;
    const res = await apiFetch(url, opts);
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : res.statusText;
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- flash messages

  function dismiss(el) {
    if (!el || el.classList.contains("is-leaving")) return;
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 320);
  }

  function armFlash(el) {
    if (el.dataset.flashArmed) return;
    el.dataset.flashArmed = "1";
    const kind = Array.from(el.classList).find((c) => c.startsWith("banner-"));
    // Errors and warnings stay until dismissed; info/success fade out.
    if (kind !== "banner-danger" && kind !== "banner-warn") {
      setTimeout(() => dismiss(el), FLASH_TIMEOUT_MS);
    }
  }

  function flash(message, kind) {
    const area = document.getElementById("flash-area");
    if (!area) return;
    const el = document.createElement("div");
    el.className = "banner banner-" + (kind || "info") + " flash";
    el.setAttribute("role", "status");
    el.dataset.flash = "";
    const body = document.createElement("span");
    body.className = "banner-body";
    body.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "banner-close";
    close.dataset.flashClose = "";
    close.setAttribute("aria-label", "Hinweis schließen");
    close.textContent = "×";
    el.append(body, close);
    area.append(el);
    armFlash(el);
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-flash-close]");
    if (btn) dismiss(btn.closest("[data-flash]"));
  });

  function initFlashes(root) {
    (root || document).querySelectorAll("[data-flash]").forEach(armFlash);
  }

  // ---------------------------------------------------------------- htmx integration

  document.addEventListener("htmx:configRequest", (event) => {
    // hx-headers on <body> already sends the token; this keeps it in sync if the meta tag changes.
    event.detail.headers["X-CSRF-Token"] = csrfToken();
  });

  document.addEventListener("htmx:afterSwap", (event) => initFlashes(event.target));

  document.addEventListener("htmx:responseError", (event) => {
    const xhr = event.detail.xhr;
    let message = "Aktion fehlgeschlagen (" + xhr.status + ").";
    try {
      const data = JSON.parse(xhr.responseText);
      if (data && typeof data.detail === "string") message = data.detail;
    } catch (_) { /* not JSON */ }
    flash(message, "danger");
  });

  document.addEventListener("htmx:sendError", () => {
    flash("Keine Verbindung zum Server.", "warn");
  });

  // ---------------------------------------------------------------- online/offline banner

  function updateOnlineState() {
    const banner = document.querySelector("[data-offline-banner]");
    if (banner) banner.hidden = navigator.onLine;
    document.documentElement.classList.toggle("is-offline", !navigator.onLine);
  }
  window.addEventListener("online", updateOnlineState);
  window.addEventListener("offline", updateOnlineState);

  // ---------------------------------------------------------------- http (LAN) hint

  const HTTPS_HINT_KEY = "deputat.httpsHintDismissedAt";
  const HTTPS_HINT_SNOOZE_MS = 7 * 24 * 3600 * 1000;

  function isLocalHost(host) {
    return host === "localhost" || host.endsWith(".localhost") || host === "127.0.0.1" ||
      host === "[::1]" || host === "::1";
  }

  function httpsHintDismissed() {
    try {
      const at = Number(window.localStorage.getItem(HTTPS_HINT_KEY) || 0);
      return at > 0 && Date.now() - at < HTTPS_HINT_SNOOZE_MS;
    } catch (_) {
      return false;
    }
  }

  function initHttpsHint() {
    const banner = document.querySelector("[data-https-banner]");
    if (!banner) return;
    const meta = document.querySelector('meta[name="deputat-base-url"]');
    const base = meta ? (meta.getAttribute("content") || "").replace(/\/+$/, "") : "";
    if (window.location.protocol !== "http:" || isLocalHost(window.location.hostname)) return;
    if (!/^https:\/\//i.test(base) || httpsHintDismissed()) return;
    const link = banner.querySelector("[data-https-link]");
    if (link) link.href = base + window.location.pathname + window.location.search;
    banner.hidden = false;
    const close = banner.querySelector("[data-https-banner-close]");
    if (close) {
      close.addEventListener("click", () => {
        banner.hidden = true;
        try { window.localStorage.setItem(HTTPS_HINT_KEY, String(Date.now())); } catch (_) { /* private mode */ }
      });
    }
  }

  // ---------------------------------------------------------------- service worker

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    // Secure context is required (https or localhost).
    if (!window.isSecureContext) return;
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((err) => {
      console.warn("Service worker registration failed:", err);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initFlashes(document);
    updateOnlineState();
    initHttpsHint();
    registerServiceWorker();
  });

  window.deputat = Object.assign(window.deputat || {}, {
    csrfToken,
    fetch: apiFetch,
    fetchJSON,
    flash,
  });
})();
