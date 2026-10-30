/* Deputat – in-app reminders (slot #reminders in base.html, loaded via HTMX from /hinweise).
 *
 * - Dismissing a banner hides its key for the rest of the browser session (sessionStorage).
 *   Keys encode the situation (e.g. the month or the entry), so a new situation shows again.
 * - Network/server errors of the reminder request stay silent (no global error flash): reminders
 *   are optional, e.g. on the offline page.
 */
(function () {
  "use strict";

  const STORAGE_KEY = "deputat.reminders.dismissed";

  function readDismissed() {
    try {
      const raw = window.sessionStorage.getItem(STORAGE_KEY);
      const list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (_) {
      return [];
    }
  }

  function writeDismissed(list) {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(-100)));
    } catch (_) { /* storage blocked: dismissal only lasts until the next load */ }
  }

  function applyDismissed(root) {
    const dismissed = new Set(readDismissed());
    (root || document).querySelectorAll("[data-reminder]").forEach((el) => {
      if (dismissed.has(el.dataset.reminder)) el.remove();
    });
  }

  function isReminderRequest(event) {
    const elt = event.detail && event.detail.elt;
    return Boolean(elt && elt.id === "reminders");
  }

  // Capture phase on document runs before app.js' bubble listeners -> no "Keine Verbindung" flash.
  ["htmx:sendError", "htmx:responseError", "htmx:timeout"].forEach((name) => {
    document.addEventListener(name, (event) => {
      if (isReminderRequest(event)) event.stopPropagation();
    }, true);
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail && event.detail.target && event.detail.target.id === "reminders") {
      applyDismissed(event.detail.target);
    }
  });

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-reminder-close]");
    if (!btn) return;
    const banner = btn.closest("[data-reminder]");
    if (!banner) return;
    const list = readDismissed();
    if (!list.includes(banner.dataset.reminder)) list.push(banner.dataset.reminder);
    writeDismissed(list);
    banner.remove();
  });
})();
