/* Deputat – Einstellungen (app/web/settings.py). Shared behaviour (confirm, copy) lives in admin.js.
 *
 *   [data-age-toggle]   enables the Altersermäßigung input only while "von Hand festlegen" is checked
 *   [data-zeitraster]   zeitraster editor: "Weitere Stunde" clones an empty row, [data-zr-remove] empties/removes one
 */
(function () {
  "use strict";

  function syncAgeOverride(root) {
    root.querySelectorAll("[data-age-override]").forEach((box) => {
      const toggle = box.querySelector("[data-age-toggle]");
      const wrap = box.querySelector("[data-age-input]");
      if (!toggle || !wrap) return;
      const input = wrap.querySelector("input");
      if (input) input.disabled = !toggle.checked;
      wrap.classList.toggle("text-muted", !toggle.checked);
    });
  }

  function rowIsEmpty(row) {
    return Array.from(row.querySelectorAll("input")).every((input) => !input.value.trim());
  }

  function nextSlotNo(list) {
    let max = 0;
    list.querySelectorAll('input[name="zr_slot_no"]').forEach((input) => {
      const n = parseInt(input.value, 10);
      if (!Number.isNaN(n) && n > max) max = n;
    });
    return max + 1;
  }

  let rowCounter = 1000;

  function lastEnd(list) {
    let value = "";
    list.querySelectorAll('input[name="zr_end"]').forEach((input) => {
      if (input.value.trim()) value = input.value.trim();
    });
    return value;
  }

  function addRow(form) {
    const list = form.querySelector("[data-zr-list]");
    const rows = list.querySelectorAll("[data-zr-row]");
    const template = rows[rows.length - 1];
    if (!template) return;
    // Reuse a trailing blank row instead of stacking empty ones.
    let row = template;
    if (!rowIsEmpty(template)) {
      row = template.cloneNode(true);
      rowCounter += 1;
      row.querySelectorAll("input").forEach((input) => {
        input.value = "";
        const oldId = input.id;
        input.id = oldId.replace(/-\d+$/, "-" + rowCounter);
        const label = row.querySelector('label[for="' + oldId + '"]');
        if (label) label.setAttribute("for", input.id);
      });
    }
    const no = row.querySelector('input[name="zr_slot_no"]');
    if (no && !no.value) no.value = String(nextSlotNo(list));
    const start = row.querySelector('input[name="zr_start"]');
    const prevEnd = lastEnd(list);
    if (start && !start.value && prevEnd) start.value = prevEnd;
    if (row !== template) list.append(row);
    if (start) start.focus();
  }

  document.addEventListener("click", (event) => {
    const add = event.target.closest("[data-zr-add]");
    if (add) {
      event.preventDefault();
      addRow(add.closest("form"));
      return;
    }
    const remove = event.target.closest("[data-zr-remove]");
    if (remove) {
      event.preventDefault();
      const row = remove.closest("[data-zr-row]");
      const list = row.parentElement;
      if (list.querySelectorAll("[data-zr-row]").length > 1) row.remove();
      else row.querySelectorAll("input").forEach((input) => { input.value = ""; });
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.closest("[data-age-toggle]")) syncAgeOverride(document);
  });

  document.addEventListener("DOMContentLoaded", () => {
    syncAgeOverride(document);
    document.querySelectorAll("[data-zeitraster]").forEach((form) => {
      const add = form.querySelector("[data-zr-add]");
      if (add) add.hidden = false;
      // With JS, keep only one trailing blank row; "Weitere Stunde" adds more.
      const rows = Array.from(form.querySelectorAll("[data-zr-row]"));
      let blanks = rows.filter(rowIsEmpty);
      while (blanks.length > 1 && rows.length > 1) {
        blanks.pop().remove();
        blanks = Array.from(form.querySelectorAll("[data-zr-row]")).filter(rowIsEmpty);
      }
    });
  });
})();
