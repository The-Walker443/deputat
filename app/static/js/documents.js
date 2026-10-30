/* Deputat – Screen "Schreiben" (letter generator). No inline handlers (strict CSP).
 *
 * - [data-print]: shows the button (hidden without JS) and opens the browser print dialog
 *   (print CSS in css/documents.css prints only the letter; "Als PDF speichern" gives the PDF).
 * - Preview from edited text runs via HTMX (hx-post="/schreiben/vorschau"); nothing to do here.
 * - Warn before leaving the page with unsaved edits in the letter text.
 */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-print]").forEach((btn) => {
      btn.hidden = false;
      btn.addEventListener("click", () => window.print());
    });

    const text = document.getElementById("f-text");
    if (!text) return;
    const initial = text.value;
    let submitting = false;
    const form = text.closest("form");
    if (form) form.addEventListener("submit", () => { submitting = true; });
    window.addEventListener("beforeunload", (event) => {
      if (!submitting && text.value !== initial) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  });
})();
