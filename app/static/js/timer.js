/* Deputat – screen "Heute": timer UI (Konzept §8.1, §8.4).
 *
 * State = server state (data-state on [data-today], later /api/timer responses) with all pending
 * offline-queue actions applied on top, so the UI is optimistic and survives reloads/offline.
 * Every action carries the device time of the tap (ISO with offset) and a client_action_id and goes
 * through window.deputatQueue (offline-queue.js) to /api/timer/{start|switch|stop|pause|resume}.
 *
 * After a successful sync the body event "deputat-refresh" makes HTMX reload tiles, entries and
 * suggestions. Tiles marked data-live-base get the running timer's time added every second.
 * Optional "Kennzeichen" toggles ([data-timer-flag]) go with the next start as body.flags; the Vertretung
 * flags ([data-flag-unterricht]) only for Unterricht chips (data-unterricht="1"), as the server requires.
 * No inline scripts / handlers (CSP): everything is wired here via event delegation.
 */
(function () {
  "use strict";

  const LONG_RUNNING_MS = 6 * 3600 * 1000;
  const REFRESH_EVENT = "deputat-refresh";
  // Flags a timer start may carry (app/services/timer.py TIMER_FLAGS) and their meta labels.
  const TIMER_FLAGS = {
    klassenfahrt: "Klassenfahrt",
    nachtbereitschaft: "Nachtbereitschaft",
    vertretung_angeordnet: "Vertretung angeordnet",
    vertretung_genehmigt: "Vertretung genehmigt",
  };

  let root = null;
  let Q = null;
  let server = { running: null, paused: null };
  let pending = [];
  let state = { running: null, paused: null };
  let segments = []; // [{start: ms, end: ms|null}] time tracked since the tiles were rendered
  let chipInfo = {}; // category_id -> {category_id, code, name, color, unterricht}
  let syncProblem = null; // null | {kind: "conflict"|"auth", ...}

  // ------------------------------------------------------------------ helpers

  function uuid() {
    if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    // crypto.randomUUID needs a secure context; the LAN may use plain http.
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function pad(n) { return String(n).padStart(2, "0"); }

  function timerFlags(list) {
    return (Array.isArray(list) ? list : []).filter((f) => Object.prototype.hasOwnProperty.call(TIMER_FLAGS, f));
  }

  /* Device time as ISO 8601 with the local UTC offset, e.g. 2026-10-29T08:12:03+01:00. */
  function isoLocal(d) {
    const off = -d.getTimezoneOffset();
    const sign = off >= 0 ? "+" : "-";
    const abs = Math.abs(off);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) +
      sign + pad(Math.floor(abs / 60)) + ":" + pad(abs % 60);
  }

  function parseTime(value) {
    if (!value) return null;
    const t = Date.parse(value);
    return Number.isNaN(t) ? null : t;
  }

  function localDayKey(ms) {
    const d = new Date(ms);
    return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
  }

  function startOfLocalDay(ms) {
    const d = new Date(ms);
    d.setHours(0, 0, 0, 0);
    return d.getTime();
  }

  function fmtClock(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 3600) + ":" + pad(Math.floor((s % 3600) / 60)) + ":" + pad(s % 60);
  }

  function fmtHM(seconds) {
    const total = Math.floor((Math.abs(seconds) + 30) / 60);
    const sign = seconds < 0 && total ? "-" : "";
    return sign + Math.floor(total / 60) + ":" + pad(total % 60);
  }

  function fmtTime(ms) {
    const d = new Date(ms);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function $(sel) { return root.querySelector(sel); }

  function flash(message, kind) {
    if (window.deputat && window.deputat.flash) window.deputat.flash(message, kind);
  }

  function refreshPartials() {
    if (navigator.onLine) document.body.dispatchEvent(new CustomEvent(REFRESH_EVENT, { bubbles: true }));
  }

  // ------------------------------------------------------------------ state

  function categoryOf(id, fallback) {
    const info = chipInfo[String(id)];
    if (info) return info;
    return {
      category_id: id,
      code: (fallback && (fallback.code || fallback.category_code)) || "?",
      name: (fallback && (fallback.name || fallback.category_name)) || "Unbekannt",
      color: (fallback && fallback.color) || "#64748B",
    };
  }

  /* /api/timer response -> {running, paused} in the shape the UI uses. */
  function fromApi(data) {
    const r = data && data.running;
    const p = data && data.paused;
    return {
      running: r ? Object.assign({}, categoryOf(r.category_id, r), {
        entry_id: r.id,
        started_at: r.start_at, subcategory_id: r.subcategory_id || null, note: r.note || null,
        klasse: r.klasse, fach: r.fach, location: r.location, flags: timerFlags(r.flags),
      }) : null,
      paused: !r && p && p.category_id ? Object.assign({}, categoryOf(p.category_id), {
        klasse: p.klasse, fach: p.fach,
        location: p.location_explicit ? p.location : null, flags: timerFlags(p.flags),
      }) : null,
    };
  }

  function applyAction(s, item) {
    const b = item.body || {};
    if (item.type === "start" || item.type === "switch") {
      s.running = Object.assign({}, categoryOf(b.category_id), {
        started_at: b.started_at, subcategory_id: b.subcategory_id || null, note: b.note || null,
        klasse: b.klasse || null, fach: b.fach || null,
        location: b.location || null, flags: timerFlags(b.flags), pending: true,
      });
      s.paused = null;
    } else if (item.type === "stop") {
      s.running = null;
      s.paused = null;
    } else if (item.type === "pause") {
      if (s.running) {
        const r = s.running;
        s.paused = { category_id: r.category_id, code: r.code, name: r.name, color: r.color,
                     klasse: r.klasse, fach: r.fach, location: r.location, flags: r.flags || [],
                     pending: true };
        s.running = null;
      }
    } else if (item.type === "resume") {
      if (s.paused) {
        s.running = Object.assign({}, s.paused, { started_at: b.at, pending: true });
        s.paused = null;
      }
    }
    return s;
  }

  function derive() {
    const s = { running: server.running ? Object.assign({}, server.running) : null,
                paused: server.paused ? Object.assign({}, server.paused) : null };
    pending.forEach((item) => applyAction(s, item));
    state = s;
  }

  /* Live tiles: track the timer's running segments since the tiles were rendered. */
  function actionTime(item) {
    const b = item.body || {};
    return parseTime(b.started_at || b.stopped_at || b.at) || Date.now();
  }

  function segmentApply(item) {
    const t = actionTime(item);
    const open = segments.length && segments[segments.length - 1].end === null ? segments[segments.length - 1] : null;
    if (item.type === "start" || item.type === "switch" || item.type === "resume") {
      if (open) open.end = t;
      segments.push({ start: t, end: null });
    } else if (open) {
      open.end = t;
    }
  }

  function resetSegments() {
    segments = [];
    const r = state.running;
    const start = r ? parseTime(r.started_at) : null;
    if (start !== null && localDayKey(start) === localDayKey(Date.now())) segments.push({ start, end: null });
  }

  function liveSeconds() {
    const now = Date.now();
    const dayStart = startOfLocalDay(now);
    let ms = 0;
    segments.forEach((seg) => {
      const a = Math.max(seg.start, dayStart);
      const b = seg.end === null ? now : Math.min(seg.end, now);
      if (b > a) ms += b - a;
    });
    return Math.floor(ms / 1000);
  }

  // ------------------------------------------------------------------ rendering

  function show(el, visible) { if (el) el.hidden = !visible; }

  function firstChip() {
    const chip = root.querySelector("[data-chip]");
    return chip ? chipInfo[chip.dataset.categoryId] : null;
  }

  function render() {
    const r = state.running;
    const p = state.paused;
    const main = $("[data-timer-main]");
    if (!main) return;
    const cat = r || p || firstChip();
    main.classList.toggle("is-running", !!r);
    main.classList.toggle("is-paused", !r && !!p);
    const dot = $("[data-timer-dot]");
    if (dot && cat) dot.style.setProperty("--chip-color", cat.color);
    const label = $("[data-timer-cat-label]");
    if (label) label.textContent = cat ? cat.code + " · " + cat.name : "Keine Kategorie";
    const sub = $("[data-timer-sub]");
    if (sub) {
      sub.textContent = r ? "Tippen zum Beenden" : p ? "Pausiert – tippen zum Fortsetzen" : "Tippen zum Starten";
    }
    main.setAttribute("aria-label", r ? "Timer beenden" : p ? "Timer fortsetzen" : "Timer starten");

    show($("[data-timer-pause]"), !!r);
    show($("[data-timer-resume]"), !r && !!p);
    show($("[data-timer-stop]"), !!r || !!p);

    const meta = $("[data-timer-meta]");
    if (meta) {
      const parts = [];
      if (r) {
        const t = parseTime(r.started_at);
        if (t !== null) parts.push("seit " + fmtTime(t));
        if (r.klasse) parts.push(r.klasse);
        if (r.fach) parts.push(r.fach);
        if (r.location) parts.push(r.location.charAt(0).toUpperCase() + r.location.slice(1));
        timerFlags(r.flags).forEach((f) => parts.push(TIMER_FLAGS[f]));
        if (r.pending) parts.push("noch nicht synchronisiert");
      } else if (p && p.pending) {
        parts.push("Pause noch nicht synchronisiert");
      }
      meta.textContent = parts.join(" · ");
      show(meta, parts.length > 0);
    }

    root.querySelectorAll("[data-chip]").forEach((chip) => {
      chip.setAttribute("aria-pressed", r && String(r.category_id) === chip.dataset.categoryId ? "true" : "false");
    });
    const chipsTitle = root.querySelector("#chips-title");
    if (chipsTitle) chipsTitle.textContent = r ? "Kategorie wechseln" : "Kategorie starten";
    if (r) show($("[data-start-hint]"), false);
    tick();
  }

  function tick() {
    const r = state.running;
    const now = Date.now();
    const clock = $("[data-timer-clock]");
    let started = r ? parseTime(r.started_at) : null;
    if (clock) {
      if (r && started !== null) clock.textContent = fmtClock(now - started);
      else clock.textContent = state.paused ? "Weiter" : "Start";
    }
    // > 6 h banner and midnight prompt
    show($("[data-long-banner]"), !!(r && started !== null && now - started > LONG_RUNNING_MS));
    const crossed = !!(r && started !== null && localDayKey(started) < localDayKey(now));
    show($("[data-midnight-banner]"), crossed);
    if (crossed) {
      const yesterday = localDayKey(startOfLocalDay(now) - 1);
      show($("[data-midnight-continue]"), localDayKey(started) === yesterday);
    }
    // live tiles
    const add = liveSeconds();
    root.querySelectorAll("[data-live-base]").forEach((el) => {
      const base = parseInt(el.dataset.liveBase, 10);
      if (!Number.isNaN(base)) el.textContent = fmtHM(base + add);
    });
    // running entry in the list
    root.querySelectorAll("[data-running-start]").forEach((el) => {
      const t = parseTime(el.dataset.runningStart);
      if (t !== null && r && !r.pending) el.textContent = fmtHM((now - t) / 1000);
    });
  }

  // ------------------------------------------------------------------ sync UI

  function setBadge(count) {
    const badge = document.querySelector("[data-sync-badge]");
    if (!badge) return;
    const text = badge.querySelector("[data-sync-text]");
    const blocked = Q && Q.blocked;
    if (count > 0) {
      const what = count === 1 ? "1 Aktion" : count + " Aktionen";
      let label = navigator.onLine ? what + " – wird synchronisiert" : "offline – wird synchronisiert (" + count + ")";
      if (blocked === "conflict") label = what + " – Konflikt";
      else if (blocked === "auth") label = what + " gespeichert – Anmeldung nötig";
      if (text) text.textContent = label;
    }
    const spinner = badge.querySelector(".sync-spinner");
    if (spinner) spinner.hidden = !!blocked;
    badge.hidden = count === 0;
  }

  function button(label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-sm " + cls;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function renderSyncProblem() {
    const area = $("[data-sync-area]");
    if (!area) return;
    area.replaceChildren();
    if (!syncProblem) return;
    const box = document.createElement("div");
    box.setAttribute("role", "alertdialog");
    const body = document.createElement("div");
    body.className = "banner-body stack-sm";
    const title = document.createElement("p");
    const strong = document.createElement("strong");
    const text = document.createElement("p");
    const actions = document.createElement("div");
    actions.className = "btn-group";
    title.append(strong);
    body.append(title, text, actions);
    box.append(body);

    if (syncProblem.kind === "auth") {
      box.className = "banner banner-danger";
      strong.textContent = "Sitzung abgelaufen – bitte Seite neu laden / neu anmelden.";
      text.textContent = "Nicht übertragene Timer-Aktionen bleiben auf diesem Gerät gespeichert und werden danach gesendet.";
      actions.append(
        button("Seite neu laden", "btn-primary", () => window.location.reload()),
        button("Erneut versuchen", "btn-secondary", () => { syncProblem = null; renderSyncProblem(); Q.unblock(); }),
      );
    } else {
      const a = syncProblem.action || {};
      box.className = "banner banner-warn";
      strong.textContent = "Timer-Aktion nicht möglich";
      const when = parseTime((a.body || {}).started_at || (a.body || {}).stopped_at || (a.body || {}).at);
      text.textContent = (a.label || "Timer-Aktion") + (when !== null ? " (" + fmtTime(when) + ")" : "") +
        ": " + (syncProblem.message || "Der Server hat die Aktion abgelehnt.");
      const link = document.createElement("a");
      link.className = "btn btn-ghost btn-sm";
      link.href = "/eintraege";
      link.textContent = "Einträge prüfen";
      actions.append(
        button("Aktion verwerfen", "btn-danger", () => resolveConflict("discard")),
        button("Erneut versuchen", "btn-secondary", () => resolveConflict("retry")),
        link,
      );
    }
    area.append(box);
  }

  async function resolveConflict(mode) {
    syncProblem = null;
    renderSyncProblem();
    await Q.resolve(mode);
    await reloadPending();
  }

  async function reloadPending() {
    pending = await Q.list();
    derive();
    render();
  }

  async function fetchStatus() {
    try {
      const res = await fetch("/api/timer/status", {
        credentials: "same-origin", redirect: "manual", cache: "no-store", headers: { Accept: "application/json" },
      });
      const kind = Q.classify(res);
      if (kind === "auth") { syncProblem = { kind: "auth" }; renderSyncProblem(); return; }
      if (kind !== "ok") return;
      server = fromApi(await res.json());
      await reloadPending();
    } catch (_) { /* offline: keep the optimistic state */ }
  }

  // ------------------------------------------------------------------ actions

  function detailFields(cat) {
    const out = {};
    root.querySelectorAll("[data-timer-field]").forEach((el) => {
      if (el.value) out[el.dataset.timerField] = el.value;
    });
    const flags = [];
    root.querySelectorAll("[data-timer-flag]").forEach((el) => {
      // Vertretung only for Unterricht (the server rejects it otherwise; a queued 400 would be lost).
      if (el.checked && (!el.hasAttribute("data-flag-unterricht") || (cat && cat.unterricht))) {
        flags.push(el.dataset.timerFlag);
      }
    });
    if (flags.length) out.flags = flags;
    return out;
  }

  function resetDetails() {
    root.querySelectorAll("[data-timer-field]").forEach((el) => { el.value = ""; });
    root.querySelectorAll("[data-timer-flag]").forEach((el) => { el.checked = false; });
    const details = $("[data-timer-details]");
    if (details) details.open = false;
  }

  async function act(type, body, label) {
    body.client_action_id = uuid();
    const item = { id: body.client_action_id, type, url: "/api/timer/" + type, body, label };
    // Optimistic: show the new state immediately, then persist + send.
    pending.push(item);
    segmentApply(item);
    derive();
    render();
    try {
      await Q.enqueue(item);
    } catch (err) {
      console.error(err);
      flash("Aktion konnte nicht gespeichert werden.", "danger");
    }
    pending = await Q.list();
    derive();
    render();
  }

  function startCategory(cat, extra) {
    if (!cat) return;
    const r = state.running;
    if (r && String(r.category_id) === String(cat.category_id)) {
      flash(cat.name + " läuft bereits.", "info");
      return;
    }
    const fields = extra || detailFields(cat);
    if (cat.requiresNote && !(fields.note && String(fields.note).trim())) {
      // Category with mandatory note (Sonstiges): the server would reject the start – ask for the note first.
      const details = $("[data-timer-details]");
      if (details) details.open = true;
      const note = $("[data-timer-field='note']");
      if (note) { note.scrollIntoView({ block: "center" }); note.focus({ preventScroll: true }); }
      flash("Für „" + cat.name + "“ bitte zuerst eine Notiz eintragen, dann erneut antippen.", "warn");
      return;
    }
    const body = Object.assign({ category_id: Number(cat.category_id), started_at: isoLocal(new Date()) }, fields);
    const type = r ? "switch" : "start";
    resetDetails();
    act(type, body, (r ? "Wechsel zu " : "Start ") + cat.code);
  }

  function stop(at) {
    act("stop", { stopped_at: isoLocal(at || new Date()) }, "Stopp");
  }

  function onMainClick() {
    if (state.running) stop();
    else if (state.paused) act("resume", { at: isoLocal(new Date()) }, "Weiter " + state.paused.code);
    else startCategory(firstChip());
  }

  function midnightContinue() {
    const r = state.running;
    if (!r) return;
    const midnight = new Date(startOfLocalDay(Date.now()));
    // Carry everything over, so the part after midnight is the same activity (incl. subcategory/note).
    const extra = {};
    if (r.subcategory_id) extra.subcategory_id = Number(r.subcategory_id);
    if (r.note) extra.note = r.note;
    if (r.klasse) extra.klasse = r.klasse;
    if (r.fach) extra.fach = r.fach;
    if (r.location) extra.location = r.location;
    if (timerFlags(r.flags).length) extra.flags = timerFlags(r.flags);
    // Switch at 00:00: the server ends the running entry at midnight and starts a new one.
    const body = Object.assign({ category_id: Number(r.category_id), started_at: isoLocal(midnight) }, extra);
    act("switch", body, "Fortsetzen ab 00:00 " + r.code);
  }

  function midnightEnd() {
    const r = state.running;
    const started = r ? parseTime(r.started_at) : null;
    if (started === null) return;
    const end = new Date(started);
    end.setHours(23, 59, 59, 0);
    stop(end);
  }

  async function confirmDay(btn) {
    const day = btn.dataset.date;
    if (!navigator.onLine) {
      flash("Offline – „Tag wie geplant“ bitte später bestätigen.", "warn");
      return;
    }
    btn.disabled = true;
    try {
      const result = await Q.postJSON("/api/days/" + encodeURIComponent(day) + "/confirm", {});
      if (result.kind === "ok") {
        flash("Tag wie geplant bestätigt.", "success");
        refreshPartials();
      } else if (result.kind === "auth") {
        syncProblem = { kind: "auth" };
        renderSyncProblem();
      } else if (result.kind === "network") {
        flash("Keine Verbindung – bitte später erneut versuchen.", "warn");
      } else if (result.status === 404 || result.status === 405) {
        flash("Bestätigen ist noch nicht verfügbar.", "warn");
      } else {
        flash(result.message || "Bestätigen fehlgeschlagen.", "danger");
      }
    } finally {
      btn.disabled = false;
    }
  }

  function onClick(event) {
    const t = event.target;
    const chip = t.closest("[data-chip]");
    if (chip) { startCategory(chipInfo[chip.dataset.categoryId]); return; }
    if (t.closest("[data-timer-main]")) { onMainClick(); return; }
    if (t.closest("[data-timer-pause]")) { act("pause", { at: isoLocal(new Date()) }, "Pause"); return; }
    if (t.closest("[data-timer-resume]")) {
      if (state.paused) act("resume", { at: isoLocal(new Date()) }, "Weiter " + state.paused.code);
      return;
    }
    if (t.closest("[data-timer-stop]")) { stop(); return; }
    if (t.closest("[data-midnight-continue]")) { midnightContinue(); return; }
    if (t.closest("[data-midnight-end]")) { midnightEnd(); return; }
    const confirmBtn = t.closest("[data-confirm-day]");
    if (confirmBtn) confirmDay(confirmBtn);
  }

  // ------------------------------------------------------------------ init

  function readChips() {
    chipInfo = {};
    root.querySelectorAll("[data-chip]").forEach((chip) => {
      chipInfo[chip.dataset.categoryId] = {
        category_id: Number(chip.dataset.categoryId),
        code: chip.dataset.code, name: chip.dataset.name, color: chip.dataset.color,
        unterricht: chip.dataset.unterricht === "1",
        requiresNote: chip.dataset.requiresNote === "1",
      };
    });
  }

  function handleShortcut() {
    // Manifest shortcut "Timer starten" -> /heute?aktion=start: ask for the category (one tap).
    if (root.dataset.aktion !== "start") return;
    try { history.replaceState(null, "", "/heute"); } catch (_) { /* ignore */ }
    if (state.running) return;
    show($("[data-start-hint]"), true);
    const chip = root.querySelector("[data-chip]");
    if (chip) {
      chip.scrollIntoView({ block: "center" });
      chip.focus({ preventScroll: true });
    }
  }

  async function init() {
    root = document.querySelector("[data-today]");
    Q = window.deputatQueue;
    if (!root || !Q) return;
    readChips();
    try {
      const initial = JSON.parse(root.dataset.state || "{}");
      server = { running: initial.running || null, paused: initial.paused || null };
    } catch (_) {
      server = { running: null, paused: null };
    }
    root.addEventListener("click", onClick);
    setInterval(tick, 1000);

    Q.on("change", (d) => setBadge(d.count));
    Q.on("applied", (d) => {
      if (d.data && Object.prototype.hasOwnProperty.call(d.data, "running")) server = fromApi(d.data);
      reloadPending();
      if (d.data && d.data.stopped && d.data.stopped.length > 1) {
        flash("Der Eintrag lief über Mitternacht und wurde automatisch geteilt.", "info");
      }
      if (d.data && Array.isArray(d.data.notices)) d.data.notices.forEach((m) => flash(m, "info"));
    });
    Q.on("synced", (d) => {
      if (syncProblem && syncProblem.kind !== "auth") { syncProblem = null; renderSyncProblem(); }
      if (d.sent > 0) refreshPartials();
    });
    Q.on("conflict", (d) => {
      syncProblem = { kind: "conflict", action: d.action, message: d.message, status: d.status };
      renderSyncProblem();
      Q.count().then(setBadge);
    });
    Q.on("auth", () => { syncProblem = { kind: "auth" }; renderSyncProblem(); Q.count().then(setBadge); });
    Q.on("error", (d) => flash("Server nicht erreichbar (" + (d.status || "?") + ") – neuer Versuch folgt.", "warn"));

    derive();
    resetSegments();
    pending = await Q.list();
    pending.forEach(segmentApply);
    derive();
    render();
    handleShortcut();

    window.addEventListener("online", () => Q.count().then(setBadge));
    window.addEventListener("offline", () => Q.count().then(setBadge));
    document.addEventListener("visibilitychange", async () => {
      if (document.visibilityState !== "visible") return;
      if ((await Q.count()) === 0) { await fetchStatus(); refreshPartials(); }
    });
    document.body.addEventListener("htmx:afterSwap", (event) => {
      const target = event.detail && event.detail.target;
      if (target && target.id === "today-tiles") { resetSegments(); tick(); }
      if (target && target.id === "today-entries") tick();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
