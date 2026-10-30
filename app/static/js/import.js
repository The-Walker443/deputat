/* Deputat – import wizard (vanilla JS, CSP-safe). Progressive enhancement only; every step works without JS.
 *
 *   <form data-import-mapping>  marks mapped selects (.is-mapped) and shows a hint when neither Von+Bis nor
 *                               Dauer is mapped or a column is used twice for time fields
 *   <form data-busy-form>       submit button disabled after submit (no double uploads / commits)
 *   select.import-cat-select    aria-invalid follows "is a category chosen"
 */
(function () {
  "use strict";

  const TIME_FIELDS = ["datum", "von", "bis", "dauer"];

  function value(form, key) {
    const el = form.querySelector('select[name="map_' + key + '"]');
    return el ? el.value : "";
  }

  function syncMapping(form) {
    form.querySelectorAll("[data-map-field]").forEach((field) => {
      const select = field.querySelector("select");
      field.classList.toggle("is-mapped", !!(select && select.value !== ""));
    });
    const hint = form.querySelector("[data-map-hint]");
    if (!hint) return;
    if (!hint.dataset.defaultText) hint.dataset.defaultText = hint.textContent;
    let problem = "";
    if (!value(form, "datum")) {
      problem = "Bitte die Spalte Datum zuordnen.";
    } else if (!value(form, "dauer") && !(value(form, "von") && value(form, "bis"))) {
      problem = "Bitte Von und Bis oder die Spalte Dauer zuordnen.";
    } else {
      const used = TIME_FIELDS.map((k) => value(form, k)).filter((v) => v !== "");
      if (new Set(used).size !== used.length) problem = "Datum, Von, Bis und Dauer müssen unterschiedliche Spalten sein.";
    }
    hint.textContent = problem || hint.dataset.defaultText;
    hint.classList.toggle("field-error", !!problem);
    hint.classList.toggle("field-hint", !problem);
  }

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!target.matches) return;
    const form = target.closest("form[data-import-mapping]");
    if (form) syncMapping(form);
    if (target.matches("select.import-cat-select")) {
      target.setAttribute("aria-invalid", target.value ? "false" : "true");
      const row = target.closest("tr");
      if (row) row.classList.toggle("is-unmapped", !target.value);
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches("form[data-busy-form]") || event.defaultPrevented) return;
    const btn = event.submitter || form.querySelector('button[type="submit"]');
    if (btn) setTimeout(() => { btn.disabled = true; btn.classList.add("is-busy"); }, 0);
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("form[data-import-mapping]").forEach(syncMapping);
  });
})();
