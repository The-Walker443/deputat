/* Deputat – screen "Woche": swipe and arrow-key navigation (Konzept §9.2).
 *
 *   <div data-week data-prev="/woche/2026-W43" data-next="/woche/2026-W45"> … </div>
 *
 * A horizontal swipe (≥ 60 px, clearly more horizontal than vertical, < 700 ms) anywhere on the
 * week container navigates: swipe left -> next week, swipe right -> previous week. Swipes that start
 * inside a horizontally scrollable element, a form control or with more than one finger are ignored.
 * ArrowLeft / ArrowRight do the same on desktop (not while typing). The benchmark checkbox
 * [data-benchmark-toggle] toggles the class "show-bench" on its card (CSS fallback for :has()).
 */
(function () {
  "use strict";

  const MIN_DX = 60;
  const MAX_MS = 700;

  function isScrollableX(el, root) {
    while (el && el !== root && el !== document.body) {
      if (el.scrollWidth > el.clientWidth + 2) {
        const ox = getComputedStyle(el).overflowX;
        if (ox === "auto" || ox === "scroll") return true;
      }
      el = el.parentElement;
    }
    return false;
  }

  function isFormControl(el) {
    return !!(el && el.closest && el.closest("input, textarea, select, [contenteditable='true']"));
  }

  function go(url) {
    if (!url) return;
    document.documentElement.classList.add("wk-navigating");
    window.location.assign(url);
  }

  function init(root) {
    const prev = root.getAttribute("data-prev");
    const next = root.getAttribute("data-next");
    let start = null;

    root.addEventListener(
      "touchstart",
      (ev) => {
        if (ev.touches.length !== 1 || isFormControl(ev.target) || isScrollableX(ev.target, root)) {
          start = null;
          return;
        }
        const t = ev.touches[0];
        start = { x: t.clientX, y: t.clientY, time: Date.now() };
      },
      { passive: true }
    );

    root.addEventListener(
      "touchend",
      (ev) => {
        if (!start) return;
        const t = ev.changedTouches[0];
        const dx = t.clientX - start.x;
        const dy = t.clientY - start.y;
        const dt = Date.now() - start.time;
        start = null;
        if (dt > MAX_MS || Math.abs(dx) < MIN_DX || Math.abs(dx) < 1.5 * Math.abs(dy)) return;
        go(dx < 0 ? next : prev);
      },
      { passive: true }
    );
    root.addEventListener("touchcancel", () => { start = null; }, { passive: true });

    document.addEventListener("keydown", (ev) => {
      if (ev.defaultPrevented || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
      if (isFormControl(document.activeElement)) return;
      if (ev.key === "ArrowLeft") go(prev);
      else if (ev.key === "ArrowRight") go(next);
    });

    root.querySelectorAll("[data-benchmark-toggle]").forEach((box) => {
      const card = box.closest(".card") || root;
      const sync = () => card.classList.toggle("show-bench", box.checked);
      box.addEventListener("change", sync);
      sync();
    });

    // Bring today's column into view inside the grid if it scrolls horizontally (wide fonts / zoom).
    const grid = root.querySelector("[data-week-grid]");
    const todayCol = grid && grid.querySelector(".wk-col.is-today");
    if (grid && todayCol && grid.scrollWidth > grid.clientWidth) {
      grid.scrollLeft = Math.max(0, todayCol.offsetLeft - grid.clientWidth / 2);
    }
  }

  function boot() {
    document.querySelectorAll("[data-week]").forEach(init);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
