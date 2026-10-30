/* Deputat – offline queue for timer actions (Konzept §8.4).
 *
 * Every timer action is first written to IndexedDB ("deputat" / store "actions", ordered by an
 * auto-increment key) and then sent in order. The device time of the tap travels in the body, so a
 * late sync never shifts start/stop times; the server dedupes via body.client_action_id.
 *
 *   window.deputatQueue.enqueue(item)   item = {id, type, url, body, label, created_at} -> Promise<item>
 *   window.deputatQueue.list()          pending items in order
 *   window.deputatQueue.flush()         send in order; stops at the first failure
 *   window.deputatQueue.resolve(mode)   after a conflict: "discard" (drop the item) | "retry"
 *   window.deputatQueue.unblock()       after "auth": try again (e.g. after logging in elsewhere)
 *   window.deputatQueue.on(evt, fn)     events:
 *        change   {count}                  queue length changed
 *        applied  {action, data}           server accepted an action (data = timer status JSON)
 *        synced   {sent}                   queue is empty after a flush (sent = actions sent now)
 *        offline  {}                       network error, will retry
 *        conflict {action, status, message}  409/400/422/423: needs the user's decision (queue blocked)
 *        auth     {}                       Cloudflare Access session expired (redirect / non-JSON);
 *                                          queue is KEPT and blocked until unblock() or reload
 *        error    {action, status, message}  5xx: retried later
 *   window.deputatQueue.classify(response) shared response classification (also used by timer.js)
 *
 * Flushing happens on enqueue, page load, the "online" event, when the page becomes visible and
 * every 30 s while items are pending. Without IndexedDB (private mode) an in-memory list is used.
 */
(function () {
  "use strict";

  const DB_NAME = "deputat";
  const STORE = "actions";
  const RETRY_MS = 30000;
  const CONFLICT_STATUSES = [400, 404, 405, 409, 410, 422, 423];

  const listeners = {};
  let memory = []; // fallback store
  let memorySeq = 0;
  let dbPromise = null;
  let flushing = null;
  let blocked = null; // null | "auth" | "conflict"
  let conflictItem = null;

  function emit(name, detail) {
    (listeners[name] || []).forEach((fn) => {
      try { fn(detail || {}); } catch (err) { console.error(err); }
    });
  }

  function on(name, fn) {
    (listeners[name] = listeners[name] || []).push(fn);
  }

  // ------------------------------------------------------------------ storage

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve) => {
      if (!("indexedDB" in window)) { resolve(null); return; }
      let req;
      try {
        req = indexedDB.open(DB_NAME, 1);
      } catch (_) { resolve(null); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "seq", autoIncrement: true });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
    return dbPromise;
  }

  function tx(db, mode, fn) {
    return new Promise((resolve, reject) => {
      const t = db.transaction(STORE, mode);
      const store = t.objectStore(STORE);
      let result;
      Promise.resolve(fn(store, (r) => { result = r; })).catch(reject);
      t.oncomplete = () => resolve(result);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  }

  async function list() {
    const db = await openDb();
    if (!db) return memory.slice();
    return tx(db, "readonly", (store, done) => {
      const req = store.getAll();
      req.onsuccess = () => done((req.result || []).sort((a, b) => a.seq - b.seq));
    });
  }

  async function count() {
    return (await list()).length;
  }

  async function enqueue(item) {
    const record = Object.assign({ created_at: Date.now() }, item);
    delete record.seq;
    const db = await openDb();
    if (!db) {
      record.seq = ++memorySeq;
      memory.push(record);
    } else {
      record.seq = await tx(db, "readwrite", (store, done) => {
        const req = store.add(record);
        req.onsuccess = () => done(req.result);
      });
    }
    emit("change", { count: await count() });
    flush();
    return record;
  }

  async function remove(seq) {
    const db = await openDb();
    if (!db) {
      memory = memory.filter((r) => r.seq !== seq);
    } else {
      await tx(db, "readwrite", (store) => { store.delete(seq); });
    }
    emit("change", { count: await count() });
  }

  // ------------------------------------------------------------------ network

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  /* Classify a fetch Response (fetched with redirect: "manual").
   * -> "ok" | "auth" (Access login / redirect / HTML instead of JSON) | "retry" (5xx) | "client" (4xx JSON) */
  function classify(res) {
    if (res.type === "opaqueredirect" || res.redirected || res.status === 0) return "auth";
    if (res.status >= 300 && res.status < 400) return "auth";
    if (res.status >= 500) return "retry";
    const type = (res.headers.get("content-type") || "").toLowerCase();
    if (!type.includes("application/json")) return "auth";
    if (res.ok) return "ok";
    return "client";
  }

  async function readDetail(res) {
    try {
      const data = await res.json();
      if (data && typeof data.detail === "string") return { data, message: data.detail };
      if (data && data.detail) return { data, message: JSON.stringify(data.detail) };
      return { data, message: res.statusText };
    } catch (_) {
      return { data: null, message: res.statusText || "Unbekannter Fehler" };
    }
  }

  async function refreshCsrf() {
    try {
      const res = await fetch("/api/csrf", {
        credentials: "same-origin", redirect: "manual", cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (classify(res) !== "ok") return false;
      const data = await res.json();
      const meta = document.querySelector('meta[name="csrf-token"]');
      if (meta && data && data.csrf_token) {
        meta.setAttribute("content", data.csrf_token);
        return true;
      }
    } catch (_) { /* offline */ }
    return false;
  }

  async function postJSON(url, body, retried) {
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        redirect: "manual",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "X-CSRF-Token": csrfToken(),
        },
        body: JSON.stringify(body || {}),
      });
    } catch (_) {
      return { kind: "network" };
    }
    const kind = classify(res);
    if (kind === "auth") return { kind: "auth", status: res.status };
    const { data, message } = await readDetail(res);
    if (kind === "ok") return { kind: "ok", status: res.status, data };
    if (kind === "retry") return { kind: "retry", status: res.status, message };
    if (res.status === 403 && /csrf/i.test(message) && !retried) {
      // Session cookie rotated (e.g. expired): fetch a fresh token once and resend.
      if (await refreshCsrf()) return postJSON(url, body, true);
      return { kind: "auth", status: res.status };
    }
    if (res.status === 401 || res.status === 403) return { kind: "auth", status: res.status, message };
    if (CONFLICT_STATUSES.includes(res.status)) return { kind: "conflict", status: res.status, message, data };
    return { kind: "retry", status: res.status, message };
  }

  // ------------------------------------------------------------------ flush

  function flush() {
    if (flushing) return flushing;
    flushing = (async () => {
      if (blocked) return { status: blocked };
      let sent = 0;
      let lastSeq = null;
      // Re-read the queue after every action: items enqueued while a request is in flight (fast
      // start -> switch on a slow network) must go out in this run, not 30 s later.
      for (;;) {
        const item = (await list())[0];
        if (!item || item.seq === lastSeq) break; // guard: never resend an item we just removed
        lastSeq = item.seq;
        const result = await postJSON(item.url, item.body);
        if (result.kind === "ok") {
          await remove(item.seq);
          sent += 1;
          emit("applied", { action: item, data: result.data });
          continue;
        }
        if (result.kind === "network") {
          emit("offline", {});
          return { status: "offline" };
        }
        if (result.kind === "auth") {
          blocked = "auth";
          emit("auth", {});
          return { status: "auth" };
        }
        if (result.kind === "conflict") {
          blocked = "conflict";
          conflictItem = item;
          emit("conflict", { action: item, status: result.status, message: result.message });
          return { status: "conflict" };
        }
        emit("error", { action: item, status: result.status, message: result.message });
        return { status: "retry" };
      }
      emit("synced", { sent });
      return { status: "ok", sent };
    })().finally(() => {
      flushing = null;
    });
    return flushing;
  }

  async function resolve(mode) {
    if (blocked !== "conflict") return;
    const item = conflictItem;
    blocked = null;
    conflictItem = null;
    if (mode === "discard" && item) await remove(item.seq);
    return flush();
  }

  function unblock() {
    if (blocked === "auth") blocked = null;
    return flush();
  }

  // ------------------------------------------------------------------ triggers

  window.addEventListener("online", () => flush());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") flush();
  });
  setInterval(async () => {
    if (!blocked && !flushing && (await count()) > 0) flush();
  }, RETRY_MS);
  document.addEventListener("DOMContentLoaded", async () => {
    emit("change", { count: await count() });
    flush();
  });

  window.deputatQueue = {
    enqueue, list, count, flush, resolve, unblock, on, classify, postJSON, refreshCsrf,
    get blocked() { return blocked; },
  };
})();
