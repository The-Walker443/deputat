/* Deputat – home screen "Erfassen": one duration per weekday (app/web/erfassen.py).
 *
 * - Fields accept "8", "7,5" / "7.5" (decimal hours), "7,30" / "7.30" / "7 30" / "7:30" (hours and minutes;
 *   ",25" and ",50" stay quarter hours), "7h30"; empty or 0 = nothing recorded. The rules mirror
 *   app/services/quick_entry.parse_total. Saved on blur, Enter (moves to the next day, so a whole week is
 *   typed in one go; after Friday it closes the keyboard unless the weekend has values), before a link is
 *   followed, when the page is hidden and when the week is switched. Escape restores the last value.
 *   No save button, no reload. A field that was only visited (value unchanged as displayed) sends nothing.
 * - Every save is a PUT /api/days/{date}/total through window.deputatQueue (offline-queue.js) with
 *   coalesce "day_total:<date>" (last write wins), discardOnConflict (a 409/422/423 shows the error on the
 *   row instead of blocking the queue) and the device time of the input ("at"). While a value is queued the
 *   week sums are computed on the device and marked "vorläufig"; after a network error the row says
 *   "nicht verbunden – wird übertragen". An expired Cloudflare Access session blocks the queue ("auth")
 *   and the values stay on the device.
 * - Entries added after a day total: the row asks once ("Auf 8:00 zurück" / "13:00 stimmt").
 * - Week switching: arrows / "Diese Woche" / swipe / ArrowLeft+ArrowRight fetch the week block with the
 *   header X-Erfassen-Partial: 1 and swap it in (history.replaceState: "back" leaves the screen instead of
 *   walking through every week); offline it falls back to navigation. A field with an invalid value keeps
 *   the week until it is fixed.
 * No inline scripts or handlers (CSP); everything is delegated from [data-qe-root].
 */
(function () {
  "use strict";

  const DEFAULT_MAX = 24 * 3600;
  const SWIPE_MIN_DX = 60;
  const SWIPE_MAX_MS = 700;
  const OK_VISIBLE_MS = 3000;
  const SETTLE_MS = 2500;
  const PARTIAL_HEADER = "X-Erfassen-Partial";
  const INVALID = "Bitte z. B. 7,5 oder 7,30 eingeben.";
  const DECIMAL_TWO_DIGITS = ["25", "50"];

  let root = null;
  let Q = null;
  let navigating = false;
  const enqueuing = new Set(); // Q.enqueue promises not settled yet (IndexedDB write in progress)

  // ------------------------------------------------------------------ helpers

  function uuid() {
    if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function pad(n) { return String(n).padStart(2, "0"); }

  /* seconds -> whole minutes in seconds, rounded like the display (half up). */
  function roundMinute(seconds) { return Math.floor((Math.abs(seconds) + 30) / 60) * 60 * (seconds < 0 ? -1 : 1); }

  /* seconds -> "h:mm" (rounded to the minute), signed with plus=true. */
  function fmtHM(seconds, plus) {
    const total = Math.floor((Math.abs(seconds) + 30) / 60);
    let sign = "";
    if (total && seconds < 0) sign = "-";
    else if (total && plus) sign = "+";
    return sign + Math.floor(total / 60) + ":" + pad(total % 60);
  }

  function tooLong(max) {
    return max === DEFAULT_MAX ? "Ein Tag hat höchstens 24 Stunden."
      : "Dieser Tag hat " + Math.floor(max / 3600) + " Stunden (Zeitumstellung).";
  }

  /* User input -> {seconds} | {error}. Mirrors app/services/quick_entry.parse_total. */
  function parseDuration(raw, max) {
    max = max || DEFAULT_MAX;
    const text = String(raw || "").trim().toLowerCase();
    if (!text) return { seconds: 0 };
    if (text.startsWith("-")) return { error: "Die Dauer darf nicht negativ sein." };
    let seconds = null;
    let hint = null;
    let m = /^(\d{1,2})\s+(\d{2})$/.exec(text);
    if (!m) {
      const m2 = /^(\d{1,2})?[.,](\d{2})\s*(?:h|std\.?|stunden?)?$/.exec(text);
      if (m2 && DECIMAL_TWO_DIGITS.indexOf(m2[2]) < 0 && Number(m2[2]) <= 59) m = m2;
    }
    if (m) {
      if (Number(m[2]) > 59) return { error: "Minuten bitte von 00 bis 59 angeben, z. B. 7,30." };
      seconds = Number(m[1] || 0) * 3600 + Number(m[2]) * 60;
    } else {
      const c = text.replace(/\s+/g, "");
      let hours = null;
      if ((m = /^(\d+):([0-5]\d)(?::([0-5]\d))?(?:h|std\.?)?$/.exec(c))) {
        hours = Number(m[1]) + Number(m[2]) / 60 + Number(m[3] || 0) / 3600;
      } else if ((m = /^(\d+(?:[.,]\d+)?)(?:m|min\.?|minuten?)$/.exec(c))) {
        hours = Number(m[1].replace(",", ".")) / 60;
      } else if ((m = /^(\d+)(?:h|std\.?)(\d+)(?:m|min\.?|minuten?)?$/.exec(c))) {
        hours = Number(m[1]) + Number(m[2]) / 60;
      } else if ((m = /^(\d+(?:[.,]\d+)?|[.,]\d+)(?:h|std\.?|stunden?)?$/.exec(c))) {
        hours = Number(m[1].replace(",", "."));
      }
      if (hours === null || !Number.isFinite(hours)) return { error: INVALID };
      seconds = Math.round(hours * 3600);
      const hm = /^(\d{1,2})(\d{2})$/.exec(c);
      if (hm && Number(hm[1]) <= 24 && Number(hm[2]) <= 59) hint = [Number(hm[1]), hm[2]];
    }
    seconds = roundMinute(seconds);
    if (seconds > max) {
      if (hint) return { error: tooLong(max) + " Meintest du " + hint[0] + ":" + hint[1] + "? Dann bitte " + hint[0] + "," + hint[1] + " eingeben." };
      return { error: tooLong(max) };
    }
    return { seconds };
  }

  function rowOf(el) { return el && el.closest ? el.closest("[data-qe-day]") : null; }
  function inputOf(row) { return row.querySelector("[data-qe-input]"); }
  function rowByDate(date) { return root ? root.querySelector('[data-qe-day][data-date="' + date + '"]') : null; }
  function weekEl() { return root ? root.querySelector("[data-qe-week]") : null; }
  function isInput(el) { return !!(el && el.matches && el.matches("[data-qe-input]") && root && root.contains(el)); }
  function rowMax(row) { return Number(row.dataset.max) || DEFAULT_MAX; }

  /* Last value of the row: queued (data-value) or saved on the server (data-saved). */
  function lastValue(row) {
    return row.dataset.value !== undefined ? Number(row.dataset.value) : Number(row.dataset.saved || 0);
  }

  function setStatus(row, kind, text, link) {
    const el = row.querySelector("[data-qe-status]");
    row.classList.toggle("is-saved", kind === "ok");
    row.classList.toggle("is-pending", kind === "pending");
    row.classList.toggle("is-error", kind === "error");
    const input = inputOf(row);
    if (input) input.setAttribute("aria-invalid", kind === "error" ? "true" : "false");
    if (row._okTimer) { clearTimeout(row._okTimer); row._okTimer = null; }
    if (!el) return;
    el.className = "qe-status" + (kind ? " is-" + kind : "");
    el.replaceChildren();
    if (!text) return;
    el.append(document.createTextNode(text));
    if (link) {
      const a = document.createElement("a");
      a.href = link.href;
      a.textContent = link.label;
      el.append(document.createTextNode(" "), a);
    }
    if (kind === "ok") {
      row._okTimer = setTimeout(() => setStatus(row, null, ""), OK_VISIBLE_MS);
    }
  }

  function pendingText() {
    if (!navigator.onLine || (Q && Q.unreachable)) return "nicht verbunden – wird übertragen, sobald es geht";
    return "wird gespeichert …";
  }

  // ------------------------------------------------------------------ provisional week sums

  /* Week sums of the server (data-base-*) plus the queued changes of this page, marked "vorläufig". */
  function recomputeSummary() {
    const w = weekEl();
    const sum = w && w.querySelector("[data-qe-summary]");
    if (!sum) return;
    let dIst = 0;
    let dDelta = 0;
    let dKonto = 0;
    let any = false;
    let todayJoins = false;
    w.querySelectorAll("[data-qe-day]").forEach((row) => {
      if (row.dataset.value === undefined) return;
      const value = Number(row.dataset.value);
      const saved = Number(row.dataset.saved || 0);
      if (value === saved) return;
      any = true;
      dIst += value - saved;
      if (row.hasAttribute("data-in-konto")) dKonto += value - saved;
      if (row.hasAttribute("data-in-saldo")) dDelta += value - saved;
      else if (row.hasAttribute("data-today") && row.dataset.soll && value > 0) {
        dDelta += value - Number(row.dataset.soll);
        todayJoins = true;
      }
    });
    const ist = sum.querySelector("[data-qe-sum-ist]");
    if (ist && sum.dataset.baseIst !== undefined) ist.textContent = fmtHM(Number(sum.dataset.baseIst) + dIst);
    const saldo = sum.querySelector("[data-qe-saldo]");
    const delta = sum.querySelector("[data-qe-sum-delta]");
    const hasDelta = sum.dataset.baseDelta !== undefined;
    if (saldo && delta && (hasDelta || todayJoins)) {
      const value = Number(sum.dataset.baseDelta || 0) + dDelta;
      delta.textContent = fmtHM(value, true);
      delta.classList.toggle("is-positive", value >= 60);
      delta.classList.toggle("is-negative", value <= -60);
      saldo.hidden = false;
    }
    const konto = sum.querySelector("[data-qe-sum-konto]");
    if (konto && sum.dataset.baseKonto !== undefined) konto.textContent = fmtHM(Number(sum.dataset.baseKonto) + dKonto, true);
    sum.classList.toggle("is-provisional", any);
    const flag = sum.querySelector("[data-qe-provisional]");
    if (flag) flag.hidden = !any;
  }

  // ------------------------------------------------------------------ saving

  /* Save the field's value. opts.seconds: explicit value (prompt buttons); opts.force: send even if equal. */
  async function commit(input, opts) {
    opts = opts || {};
    const row = rowOf(input);
    if (!row || input.readOnly) return;
    let seconds = opts.seconds;
    const raw = String(input.value || "").trim();
    if (seconds === undefined) {
      const parsed = parseDuration(raw, rowMax(row));
      if (parsed.error) {
        row.dataset.invalid = "1";
        setStatus(row, "error", parsed.error);
        return;
      }
      seconds = parsed.seconds;
    }
    delete row.dataset.invalid;
    input.value = seconds ? fmtHM(seconds) : "";
    // Compare as displayed (whole minutes): a field that was only visited must not send the rounded value
    // of second-precise timer entries back as a new total.
    if (!opts.force && seconds === roundMinute(lastValue(row))) {
      if (row.classList.contains("is-error")) setStatus(row, null, "");
      return;
    }
    row.dataset.value = String(seconds);
    row._raw = /[.,\s]/.test(raw) && raw !== fmtHM(seconds) ? raw : null;
    const date = row.dataset.date;
    const id = uuid();
    const item = {
      id,
      type: "day_total",
      method: "PUT",
      url: "/api/days/" + date + "/total",
      body: { seconds, client_action_id: id, at: new Date().toISOString() },
      date,
      label: "Tagessumme " + date,
      coalesce: "day_total:" + date,
      discardOnConflict: true,
    };
    setStatus(row, "pending", pendingText());
    recomputeSummary();
    const queued = Q.enqueue(item);
    enqueuing.add(queued);
    try {
      await queued;
    } catch (err) {
      console.error(err);
      setStatus(row, "error", "Konnte nicht gespeichert werden.");
    } finally {
      enqueuing.delete(queued);
    }
  }

  function isDirty(input) {
    const row = rowOf(input);
    if (!row || input.readOnly) return false;
    const parsed = parseDuration(input.value, rowMax(row));
    return !!parsed.error || parsed.seconds !== roundMinute(lastValue(row));
  }

  /* Save the focused field now (without waiting for the blur). */
  async function commitFocused(blur) {
    const active = document.activeElement;
    if (!isInput(active)) return;
    const done = commit(active);
    if (blur) active.blur(); // the focusout sees the value as queued and sends nothing more
    await done;
  }

  function hasQueued() {
    return !!(root && root.querySelector("[data-qe-day][data-value]"));
  }

  /* Wait (bounded) until the queued values reached the server – before leaving the page. */
  async function settle() {
    const deadline = new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    // A tap on a link blurs the field first: its save may still be writing to IndexedDB – wait for that,
    // otherwise the flush below would find an empty queue and the next page would miss the value.
    await Promise.race([Promise.allSettled(Array.from(enqueuing)), deadline]);
    // Twice: a flush already running may have looked at the queue just before the new item arrived.
    for (let i = 0; i < 2 && hasQueued() && !Q.blocked && !Q.unreachable; i += 1) {
      const result = await Promise.race([Q.flush(), deadline.then(() => "timeout")]);
      if (result === "timeout") return;
    }
  }

  function nextInput(input) {
    const row = rowOf(input);
    if (row && row.hasAttribute("data-last")) return null; // Friday with an empty weekend: done
    const all = Array.from(root.querySelectorAll("[data-qe-input]"));
    for (let i = all.indexOf(input) + 1; i < all.length; i += 1) {
      if (!all[i].readOnly) return all[i];
    }
    return null;
  }

  function onKeydown(ev) {
    const input = ev.target.closest && ev.target.closest("[data-qe-input]");
    if (!input) return;
    if (ev.key === "Enter") {
      ev.preventDefault();
      const row = rowOf(input);
      const parsed = parseDuration(input.value, rowMax(row));
      if (parsed.error) { // stay on the day until the value is fixed
        row.dataset.invalid = "1";
        setStatus(row, "error", parsed.error);
        input.select();
        return;
      }
      const next = nextInput(input);
      if (next) next.focus(); // the blur of this field saves it
      else input.blur();
    } else if (ev.key === "Escape") {
      const row = rowOf(input);
      const value = lastValue(row);
      input.value = value ? fmtHM(value) : "";
      delete row.dataset.invalid;
      setStatus(row, null, "");
      input.blur();
    }
  }

  function onFocusIn(ev) {
    const input = ev.target.closest && ev.target.closest("[data-qe-input]");
    if (!input || input.readOnly) return;
    // Select everything so typing replaces the value (iOS needs the delay). Only while the field still has
    // the focus: select() on another field would pull the focus back (fast Enter -> next day). Not once typing
    // has started either: selecting then would let the next key replace what was already typed ("9:15" -> "15").
    const atFocus = input.value;
    setTimeout(() => {
      if (document.activeElement !== input || input.value !== atFocus) return;
      try { input.select(); } catch (_) { /* detached */ }
    }, 0);
  }

  function onFocusOut(ev) {
    const input = ev.target.closest && ev.target.closest("[data-qe-input]");
    if (input) commit(input);
  }

  // ------------------------------------------------------------------ server answers

  function renderPrompt(row, prompt) {
    const box = row.querySelector("[data-qe-prompt]");
    if (!box) return;
    box.replaceChildren();
    box.hidden = !prompt;
    if (!prompt) return;
    const p = document.createElement("p");
    p.className = "qe-prompt-text";
    p.textContent = "Nach deiner Tagessumme kamen " + fmtHM(prompt.later) + " h einzeln dazu.";
    const actions = document.createElement("div");
    actions.className = "qe-prompt-actions";
    const keep = document.createElement("button");
    keep.type = "button";
    keep.className = "btn btn-sm btn-secondary";
    keep.dataset.qeSet = String(prompt.keep);
    keep.textContent = "Auf " + fmtHM(prompt.keep) + " zurück";
    const total = document.createElement("button");
    total.type = "button";
    total.className = "btn btn-sm btn-ghost";
    total.dataset.qeSet = String(prompt.total);
    total.textContent = fmtHM(prompt.total) + " stimmt";
    actions.append(keep, total);
    box.append(p, actions);
  }

  function applyDay(row, day, force) {
    row.dataset.saved = String(day.ist);
    row.toggleAttribute("data-in-saldo", !!day.in_saldo);
    const input = inputOf(row);
    const pendingValue = row.dataset.value !== undefined ? Number(row.dataset.value) : null;
    if (force || pendingValue === null || pendingValue === day.ist) {
      delete row.dataset.value;
      if (input && document.activeElement !== input) input.value = day.ist ? fmtHM(day.ist) : "";
    }
    const detail = row.querySelector("[data-qe-detail] .num");
    if (detail) detail.textContent = fmtHM(day.detail_seconds);
    renderPrompt(row, day.prompt);
  }

  function applyWeek(week) {
    const el = weekEl();
    if (!el || !week || week.ref !== el.dataset.ref) return;
    const sum = el.querySelector("[data-qe-summary]");
    if (sum) {
      sum.dataset.baseIst = String(week.ist);
      if (week.delta !== null && week.delta !== undefined) sum.dataset.baseDelta = String(week.delta);
      else delete sum.dataset.baseDelta;
      if (week.saldo_konto !== null && week.saldo_konto !== undefined) sum.dataset.baseKonto = String(week.saldo_konto);
      const label = sum.querySelector("[data-qe-saldo-label]");
      if (label) label.textContent = "Saldo" + (week.delta_until ? " bis " + week.delta_until : "");
      const saldo = sum.querySelector("[data-qe-saldo]");
      if (saldo) saldo.hidden = week.delta === null || week.delta === undefined;
      const delta = sum.querySelector("[data-qe-sum-delta]");
      if (delta && week.delta !== null && week.delta !== undefined) {
        delta.textContent = fmtHM(week.delta, true);
        delta.classList.toggle("is-positive", week.delta >= 60);
        delta.classList.toggle("is-negative", week.delta <= -60);
      }
    }
    recomputeSummary();
  }

  async function hasNewer(date) {
    const items = await Q.list();
    return items.some((it) => it.type === "day_total" && it.date === date);
  }

  async function onApplied(d) {
    const action = d.action || {};
    if (action.type !== "day_total") return;
    const data = d.data || {};
    const row = rowByDate(action.date);
    if (row && data.day && !(await hasNewer(action.date))) { // else a newer value for this day is on its way
      const stale = data.action === "veraltet";
      applyDay(row, data.day, stale);
      if (stale) {
        setStatus(row, "error", "Nicht übernommen: inzwischen wurde " + (data.day.ist ? fmtHM(data.day.ist) + " h" : "nichts") +
          " eingetragen (anderes Gerät).");
      } else {
        const shown = data.day.ist ? fmtHM(data.day.ist) + " h" : "leer";
        setStatus(row, "ok", row._raw ? "✓ " + row._raw + " = " + shown + " gespeichert" : "✓ gespeichert");
        row._raw = null;
      }
    }
    applyWeek(data);
    if (!hasQueued()) markPending();
  }

  function onRejected(d) {
    const action = d.action || {};
    if (action.type !== "day_total") return;
    const row = rowByDate(action.date);
    if (!row) {
      if (window.deputat && window.deputat.flash) {
        window.deputat.flash("Nicht gespeichert (" + action.date + "): " + (d.message || "abgelehnt"), "warn");
      }
      return;
    }
    delete row.dataset.value;
    const input = inputOf(row);
    const saved = Number(row.dataset.saved || 0);
    if (input && document.activeElement !== input) input.value = saved ? fmtHM(saved) : "";
    const link = d.status === 409 || d.status === 423 ? { href: row.dataset.entriesUrl, label: "Einträge" } : null;
    setStatus(row, "error", d.message || "Nicht gespeichert.", link);
    recomputeSummary();
  }

  async function markPending() {
    if (!root) return;
    const items = (await Q.list()).filter((it) => it.type === "day_total");
    items.forEach((it) => {
      const row = rowByDate(it.date);
      if (!row) return;
      const seconds = Number((it.body || {}).seconds || 0);
      row.dataset.value = String(seconds);
      const input = inputOf(row);
      if (input && document.activeElement !== input) input.value = seconds ? fmtHM(seconds) : "";
      let text = pendingText();
      if (Q.blocked === "auth") text = "auf dem Gerät gespeichert – Anmeldung nötig";
      else if (Q.blocked === "conflict") text = "wartet – Timer-Aktion klären";
      setStatus(row, "pending", text);
    });
    recomputeSummary();
  }

  function renderSyncProblem(kind) {
    const area = root && root.querySelector("[data-qe-sync]");
    if (!area) return;
    area.replaceChildren();
    if (!kind) return;
    const box = document.createElement("div");
    box.className = "banner " + (kind === "auth" ? "banner-danger" : "banner-warn");
    box.setAttribute("role", "alert");
    const body = document.createElement("span");
    body.className = "banner-body";
    const a = document.createElement("a");
    if (kind === "auth") {
      body.append(document.createTextNode("Sitzung abgelaufen – bitte Seite neu laden. Deine Eingaben bleiben auf dem Gerät gespeichert. "));
      a.href = window.location.pathname;
      a.textContent = "Neu laden";
    } else {
      body.append(document.createTextNode("Eine Timer-Aktion wartet auf Klärung, danach werden die Stunden übertragen. "));
      a.href = "/heute";
      a.textContent = "Zum Timer";
    }
    body.append(a);
    box.append(body);
    area.append(box);
  }

  // ------------------------------------------------------------------ week switching

  function invalidRow() {
    return root ? root.querySelector("[data-qe-day][data-invalid]") : null;
  }

  async function navigate(url, opts) {
    opts = opts || {};
    if (!url || navigating) return;
    await commitFocused(true);
    const bad = invalidRow();
    if (bad) { // keep the week: the typed value would be lost
      const input = inputOf(bad);
      if (input) { input.focus(); }
      return;
    }
    if (!navigator.onLine) {
      window.location.assign(url);
      return;
    }
    navigating = true;
    root.classList.add("is-loading");
    try {
      const res = await fetch(url, {
        credentials: "same-origin", redirect: "manual", cache: "no-store",
        headers: { [PARTIAL_HEADER]: "1", Accept: "text/html" },
      });
      const type = (res.headers.get("content-type") || "").toLowerCase();
      if (res.type === "opaqueredirect" || !res.ok || !type.includes("text/html")) {
        window.location.assign(url); // Access login, error page, ...
        return;
      }
      const html = await res.text();
      const tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      const fresh = tpl.content.querySelector("[data-qe-week]");
      const old = weekEl();
      if (!fresh || !old) {
        window.location.assign(url);
        return;
      }
      old.replaceWith(fresh);
      if (opts.history !== false) history.replaceState({ qe: true }, "", url);
      slide(opts.direction);
      await markPending();
      const label = fresh.querySelector("#qe-week-label");
      if (label) label.focus({ preventScroll: true });
    } catch (_) {
      window.location.assign(url);
    } finally {
      navigating = false;
      root.classList.remove("is-loading");
    }
  }

  function slide(direction) {
    if (!direction) return;
    const cls = direction === "next" ? "is-sliding-next" : "is-sliding-prev";
    root.classList.remove("is-sliding-next", "is-sliding-prev");
    void root.offsetWidth; // restart the animation
    root.classList.add(cls);
    setTimeout(() => root.classList.remove(cls), 250);
  }

  function onClick(ev) {
    const set = ev.target.closest && ev.target.closest("[data-qe-set]");
    if (set) {
      const row = rowOf(set);
      const input = row && inputOf(row);
      if (input) commit(input, { seconds: Number(set.dataset.qeSet), force: true });
      return;
    }
    const link = ev.target.closest && ev.target.closest("a[data-qe-nav]");
    if (!link || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
    ev.preventDefault();
    const w = weekEl();
    const href = link.getAttribute("href");
    const direction = w && href === w.dataset.next ? "next" : (w && href === w.dataset.prev ? "prev" : null);
    navigate(href, { direction });
  }

  /* Any other link (tab bar, Konto, Einträge, ...): save the focused field first and give queued values a
   * moment to reach the server, so the next page already shows them. */
  function onLinkCapture(ev) {
    const a = ev.target.closest && ev.target.closest("a[href]");
    if (!a || !root || ev.defaultPrevented || a.hasAttribute("data-qe-nav")) return;
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey || ev.button !== 0 || a.target === "_blank") return;
    const href = a.getAttribute("href") || "";
    if (!href || href.startsWith("#") || a.hasAttribute("download")) return;
    const active = document.activeElement;
    const dirty = isInput(active) && isDirty(active);
    if (!dirty && !hasQueued() && !enqueuing.size && !invalidRow()) return;
    ev.preventDefault();
    (async () => {
      await commitFocused(false);
      const bad = invalidRow();
      if (bad) { const input = inputOf(bad); if (input) input.focus(); return; }
      await settle();
      window.location.assign(a.href);
    })();
  }

  function initSwipe() {
    let start = null;
    root.addEventListener("touchstart", (ev) => {
      const t = ev.touches[0];
      const target = ev.target.closest ? ev.target : null;
      // Swipes may start on a field (it fills the right third of each row, where the thumb is) – but not
      // on the focused one (moving the caret) or on links/buttons.
      const focusedField = target && target.closest("[data-qe-input]") && target.closest("[data-qe-input]") === document.activeElement;
      if (ev.touches.length !== 1 || !target || target.closest("a, button") || focusedField) {
        start = null;
        return;
      }
      start = { x: t.clientX, y: t.clientY, time: Date.now() };
    }, { passive: true });
    root.addEventListener("touchend", (ev) => {
      if (!start) return;
      const t = ev.changedTouches[0];
      const dx = t.clientX - start.x;
      const dy = t.clientY - start.y;
      const dt = Date.now() - start.time;
      start = null;
      if (dt > SWIPE_MAX_MS || Math.abs(dx) < SWIPE_MIN_DX || Math.abs(dx) < 1.5 * Math.abs(dy)) return;
      const w = weekEl();
      if (w) navigate(dx < 0 ? w.dataset.next : w.dataset.prev, { direction: dx < 0 ? "next" : "prev" });
    }, { passive: true });
    root.addEventListener("touchcancel", () => { start = null; }, { passive: true });
  }

  function onGlobalKey(ev) {
    if (ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
    const el = document.activeElement;
    if (el && el.closest && el.closest("input, textarea, select, [contenteditable='true']")) return;
    const w = weekEl();
    if (!w) return;
    if (ev.key === "ArrowLeft") navigate(w.dataset.prev, { direction: "prev" });
    else if (ev.key === "ArrowRight") navigate(w.dataset.next, { direction: "next" });
  }

  // ------------------------------------------------------------------ init

  function init() {
    root = document.querySelector("[data-qe-root]");
    Q = window.deputatQueue;
    if (!root || !Q) return;
    root.addEventListener("keydown", onKeydown);
    root.addEventListener("focusin", onFocusIn);
    root.addEventListener("focusout", onFocusOut);
    root.addEventListener("click", onClick);
    root.addEventListener("submit", (ev) => ev.preventDefault());
    document.addEventListener("click", onLinkCapture, true);
    document.addEventListener("keydown", onGlobalKey);
    // Leaving the app (home button, screen lock, app switch) without tapping elsewhere: save what is typed.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") commitFocused(false);
    });
    window.addEventListener("pagehide", () => { commitFocused(false); });
    window.addEventListener("popstate", () => navigate(window.location.pathname, { history: false }));
    initSwipe();

    Q.on("applied", onApplied);
    Q.on("rejected", onRejected);
    Q.on("offline", markPending);
    Q.on("change", markPending);
    Q.on("auth", () => { renderSyncProblem("auth"); markPending(); });
    Q.on("conflict", () => { renderSyncProblem("conflict"); markPending(); });
    Q.on("synced", () => { if (!Q.blocked) renderSyncProblem(null); });
    window.addEventListener("online", markPending);
    window.addEventListener("offline", markPending);
    markPending();
    focusRequested();
  }

  /* ?tag=YYYY-MM-DD (reminder "Nachtragen"): put the cursor into that day's field. */
  function focusRequested() {
    const w = weekEl();
    const row = w && w.dataset.focus ? rowByDate(w.dataset.focus) : null;
    const input = row ? inputOf(row) : null;
    if (input && !input.readOnly) {
      row.scrollIntoView({ block: "center" });
      input.focus({ preventScroll: true });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
