/* Deputat – Stundenplan (app/web/timetable.py). Shared behaviour (confirm, panels, selection) lives in admin.js.
 *
 *   [data-slot-kind]   hides Klasse/Fach/Kategorie for "leer" and "Freistunde" (fields stay in the form)
 */
(function () {
  "use strict";

  function syncKind(form) {
    const select = form.querySelector("[data-slot-kind]");
    if (!select) return;
    const hide = select.value === "" || select.value === "frei";
    form.querySelectorAll("[data-slot-details]").forEach((el) => { el.hidden = hide; });
  }

  document.addEventListener("change", (event) => {
    const select = event.target.closest("[data-slot-kind]");
    if (select) syncKind(select.closest("form"));
  });

  function init(root) {
    root.querySelectorAll("form[data-slot-form]").forEach(syncKind);
  }

  document.addEventListener("DOMContentLoaded", () => init(document));
  document.addEventListener("htmx:afterSwap", (event) => init(event.detail.target || document));
})();
