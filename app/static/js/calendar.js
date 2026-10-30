/* Deputat – Kalender screens (app/web/calendar.py). Shared behaviour (confirm, autosubmit, panels) lives
 * in admin.js; this file only adds the calendar-specific bits.
 *
 *   <button data-check-all="name" data-value="1|0">   (un)checks all enabled checkboxes named ``name``
 *   [data-leave-toggle]                                 shows the live "noch zuzuordnen" hint while toggling
 */
(function () {
  "use strict";

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-check-all]");
    if (!btn) return;
    event.preventDefault();
    const form = btn.closest("form") || document;
    const checked = btn.dataset.value !== "0";
    form.querySelectorAll('input[type="checkbox"][name="' + btn.dataset.checkAll + '"]').forEach((box) => {
      if (!box.disabled) box.checked = checked;
    });
  });

  // Manual leave mode: preview the counter before saving (server stays authoritative).
  document.addEventListener("change", (event) => {
    const box = event.target.closest("[data-leave-toggle]");
    if (!box) return;
    const out = box.closest("form").querySelector("[data-leave-remaining]");
    if (!out) return;
    const base = parseInt(out.dataset.base || "0", 10);
    const wasChecked = out.dataset.initial === "1";
    let remaining = base;
    if (box.checked && !wasChecked) remaining -= 1;
    if (!box.checked && wasChecked) remaining += 1;
    out.textContent = String(Math.max(remaining, 0));
    const warn = box.closest("form").querySelector("[data-leave-excess]");
    if (warn) {
      warn.hidden = remaining >= 0;
      if (remaining < 0) warn.textContent = -remaining + " Urlaubstag(e) zu viel ausgewählt – bitte andere Tage abwählen.";
    }
  });
})();
