/* Deputat – "Einträge" screens (list, new, edit). Plain JS, no Alpine needed; CSP-safe (no inline code).
 *
 * Entry form ([data-entry-form]):
 *   - dependent subcategory select (options from data-subcategories JSON on the select)
 *   - Vertretung flags only for Unterricht ([data-unterricht-only]); hidden flags are unchecked
 *   - note required hint for categories with data-requires-note="1"
 *   - "nachträglich" warning when the date is > 48 h ago ([data-late-warning])
 *   - reason field for closed months (form data-closed-months JSON, [data-reason-field])
 *   - default location from data-location-rule while the Ort select still has data-auto-location
 * Any form with data-confirm asks before submitting.
 * The Von/Bis vs. Dauer switch is pure CSS (:has()), see app.css "Einträge".
 */
(function () {
  "use strict";

  function parseJSON(text, fallback) {
    try { return text ? JSON.parse(text) : fallback; } catch (_) { return fallback; }
  }

  function selectedOption(select) {
    return select && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
  }

  // ------------------------------------------------------------ category / subcategory

  function rebuildSubcategories(form) {
    const main = form.querySelector("[data-category-select]");
    const sub = form.querySelector("[data-subcategory-select]");
    if (!main || !sub) return;
    const map = parseJSON(sub.dataset.subcategories, {});
    const rows = map[main.value] || [];
    const keep = sub.value;
    while (sub.options.length > 1) sub.remove(1);
    rows.forEach(function (row) {
      const opt = document.createElement("option");
      opt.value = row[0];
      opt.textContent = row[1];
      if (row[0] === keep) opt.selected = true;
      sub.append(opt);
    });
    sub.disabled = rows.length === 0;
  }

  function applyCategoryRules(form) {
    const opt = selectedOption(form.querySelector("[data-category-select]"));
    const isU = !!(opt && opt.dataset.unterricht === "1");
    const needsNote = !!(opt && opt.dataset.requiresNote === "1");

    const uBox = form.querySelector("[data-unterricht-only]");
    if (uBox) {
      uBox.hidden = !isU;
      if (!isU) uBox.querySelectorAll("input[type=checkbox]").forEach(function (cb) { cb.checked = false; });
    }
    const hint = form.querySelector("[data-note-required-hint]");
    if (hint) hint.hidden = !needsNote;
    const note = form.querySelector("[data-note-field]");
    if (note) note.required = needsNote;
  }

  // ------------------------------------------------------------ date-dependent hints

  function parseDate(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    return m ? { y: +m[1], m: +m[2], d: +m[3] } : null;
  }

  function isLate(parts) {
    // > 48 h after the end of the (local) day -> "nachträglich"
    const dayEnd = new Date(parts.y, parts.m - 1, parts.d + 1, 0, 0, 0);
    return Date.now() > dayEnd.getTime() + 48 * 3600 * 1000;
  }

  function applyDateRules(form) {
    const input = form.querySelector("[data-entry-date]");
    if (!input) return;
    const parts = parseDate(input.value);

    const warn = form.querySelector("[data-late-warning]");
    if (warn) warn.hidden = !(parts && isLate(parts));
    const info = form.querySelector("[data-late-info]");
    if (info && warn) info.hidden = !warn.hidden; // the warning replaces the general hint

    const reasonBox = form.querySelector("[data-reason-field]");
    if (reasonBox) {
      const closed = parseJSON(form.dataset.closedMonths, []);
      const ym = parts ? parts.y + "-" + String(parts.m).padStart(2, "0") : "";
      const needed = !!ym && closed.indexOf(ym) !== -1;
      const forced = reasonBox.querySelector("[aria-invalid=true]") !== null;
      reasonBox.hidden = !(needed || forced);
      const field = reasonBox.querySelector("input");
      if (field) field.required = needed || forced;
    }
    applyLocationRule(form);
  }

  // ------------------------------------------------------------ default location

  function minutes(hhmm) {
    const m = /^(\d{1,2}):(\d{2})/.exec(hhmm || "");
    return m ? +m[1] * 60 + +m[2] : null;
  }

  function applyLocationRule(form) {
    const select = form.querySelector("[data-location-select]");
    if (!select || select.dataset.autoLocation !== "1") return;
    const rule = parseJSON(form.dataset.locationRule, null);
    if (!rule || !rule.inside) return;
    const parts = parseDate((form.querySelector("[data-entry-date]") || {}).value);
    const from = form.querySelector("[data-entry-from]");
    const t = minutes(from && from.value);
    if (!parts || t === null) return;
    const jsDay = new Date(parts.y, parts.m - 1, parts.d).getDay();
    const iso = jsDay === 0 ? 7 : jsDay;
    const inDays = (rule.weekdays || [1, 2, 3, 4, 5]).indexOf(iso) !== -1;
    const lo = minutes(rule.from || "07:00");
    const hi = minutes(rule.to || "14:00");
    select.value = inDays && t >= lo && t < hi ? rule.inside : (rule.outside || "");
  }

  // ------------------------------------------------------------ wiring

  function initForm(form) {
    if (form.dataset.entryFormReady) return;
    form.dataset.entryFormReady = "1";
    const sub = form.querySelector("[data-subcategory-select]");
    if (sub) {
      const main = form.querySelector("[data-category-select]");
      sub.disabled = !main || !main.value || !(parseJSON(sub.dataset.subcategories, {})[main.value] || []).length;
    }
    applyCategoryRules(form);
    applyDateRules(form);
  }

  function initAll(root) {
    (root || document).querySelectorAll("[data-entry-form]").forEach(initForm);
  }

  document.addEventListener("change", function (event) {
    const form = event.target.closest("[data-entry-form]");
    if (!form) return;
    const t = event.target;
    if (t.matches("[data-category-select]")) {
      rebuildSubcategories(form);
      applyCategoryRules(form);
    } else if (t.matches("[data-entry-date]")) {
      applyDateRules(form);
    } else if (t.matches("[data-entry-from]")) {
      applyLocationRule(form);
    } else if (t.matches("[data-location-select]")) {
      delete t.dataset.autoLocation; // the user decided
    }
  });

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });

  document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  document.addEventListener("htmx:afterSwap", function (event) { initAll(event.target); });
})();
