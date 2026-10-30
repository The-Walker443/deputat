/* Deputat – shared behaviour of the Kalender / Einstellungen / Stundenplan screens (no inline handlers, CSP).
 *
 *   <form data-confirm="Frage?">          asks before submitting (also for the clicked submitter's
 *                                         own data-confirm, which takes precedence)
 *   <form data-autosubmit>                submits when a <select> / checkbox inside changes
 *   <button data-copy="#id">              copies the text (or value) of the target to the clipboard
 *   <a data-panel-close="#panel">         empties the panel (HTMX editor) instead of navigating
 */
(function () {
  "use strict";

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      const submitter = event.submitter;
      const question = (submitter && submitter.dataset && submitter.dataset.confirm) || form.dataset.confirm;
      if (question && !window.confirm(question)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true,
  );

  document.addEventListener("change", (event) => {
    const el = event.target;
    if (!(el instanceof HTMLSelectElement || (el instanceof HTMLInputElement && el.type === "checkbox"))) return;
    const form = el.closest("form[data-autosubmit]");
    if (!form) return;
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  });

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  document.addEventListener("click", (event) => {
    const copyBtn = event.target.closest("[data-copy]");
    if (copyBtn) {
      event.preventDefault();
      const target = document.querySelector(copyBtn.dataset.copy);
      if (!target) return;
      const text = "value" in target && target.value !== undefined ? target.value : target.textContent;
      copyText((text || "").trim())
        .then(() => {
          const old = copyBtn.dataset.label || copyBtn.textContent;
          copyBtn.dataset.label = old;
          copyBtn.textContent = "Kopiert ✓";
          setTimeout(() => { copyBtn.textContent = old; }, 2000);
        })
        .catch(() => {
          if (window.deputat) window.deputat.flash("Kopieren nicht möglich – bitte manuell markieren.", "warn");
        });
      return;
    }
    const closer = event.target.closest("[data-panel-close]");
    if (closer) {
      const panel = closer.closest(closer.dataset.panelClose || "[data-panel]");
      if (panel) {
        event.preventDefault();
        panel.innerHTML = "";
        document.querySelectorAll(".is-selected[data-select]").forEach((el) => el.classList.remove("is-selected"));
      }
    }
  });

  // Mark the tapped calendar day / timetable cell while its editor loads into the panel.
  document.addEventListener("click", (event) => {
    const cell = event.target.closest("[data-select]");
    if (!cell) return; // HTMX already called preventDefault on hx-get links, so do not check it here
    document.querySelectorAll(".is-selected[data-select]").forEach((el) => el.classList.remove("is-selected"));
    cell.classList.add("is-selected");
  });

  // After an editor was swapped into a panel, move focus to its first field (keyboard / screen readers).
  document.addEventListener("htmx:afterSwap", (event) => {
    const panel = event.detail && event.detail.target;
    if (!panel || !panel.matches || !panel.matches("[data-panel]")) return;
    const heading = panel.querySelector("h2, h3");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  });
})();
