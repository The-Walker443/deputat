/* Deputat – screens Monate & Berichte, Export & Backup (vanilla JS, CSP-safe: no inline code).
 *
 *   <form data-autosubmit>              select change submits; [data-autosubmit-fallback] button is hidden
 *   <a data-dialog-open="id">           opens <dialog id> with showModal() (the href is the no-JS fallback page)
 *   [data-dialog-close]                 inside a dialog: closes it instead of navigating
 *   <form data-close-form>              checkbox [data-close-force] toggles the [data-close-note] field (required)
 *   <button data-copy="text">           copies the text to the clipboard
 *   <button data-print>                 window.print() (report toolbar)
 *   <canvas data-months-chart data-chart='[{label, saldo, kumuliert}]'>   Chart.js bars + line, colors from CSS
 *   <form data-restore-form data-restore-word="ERSETZEN">  submit enabled only with checkbox + word
 *   <form data-busy-form>               submit button disabled after submit (no double posts)
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- small helpers

  function css(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function flash(message, kind) {
    if (window.deputat && window.deputat.flash) window.deputat.flash(message, kind);
  }

  function fmtHm(hours, signed) {
    if (hours === null || hours === undefined || isNaN(hours)) return "–";
    const totalMin = Math.round(Math.abs(hours) * 60);
    const text = Math.floor(totalMin / 60) + ":" + String(totalMin % 60).padStart(2, "0");
    if (!signed || totalMin === 0) return text;
    return (hours < 0 ? "−" : "+") + text;
  }

  // ---------------------------------------------------------------- autosubmit

  function initAutosubmit(root) {
    root.querySelectorAll("form[data-autosubmit]").forEach((form) => {
      form.querySelectorAll("[data-autosubmit-fallback]").forEach((btn) => { btn.hidden = true; });
    });
  }

  document.addEventListener("change", (event) => {
    const form = event.target.closest && event.target.closest("form[data-autosubmit]");
    if (form && event.target.matches("select")) form.requestSubmit ? form.requestSubmit() : form.submit();
  });

  // ---------------------------------------------------------------- dialogs

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-dialog-open]");
    if (opener) {
      const dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
      if (dialog && typeof dialog.showModal === "function") {
        event.preventDefault();
        dialog.showModal();
        const first = dialog.querySelector("input, textarea, select, button");
        if (first) first.focus();
      }
      return;
    }
    const closer = event.target.closest("[data-dialog-close]");
    if (closer) {
      const dialog = closer.closest("dialog");
      if (dialog) {
        event.preventDefault();
        dialog.close();
      }
    }
  });

  // Click on the backdrop closes the dialog.
  document.addEventListener("click", (event) => {
    if (event.target instanceof HTMLDialogElement && event.target.open) {
      const r = event.target.getBoundingClientRect();
      const inside = event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom;
      if (!inside) event.target.close();
    }
  });

  // ---------------------------------------------------------------- close form: force + note

  function syncCloseForm(form) {
    const force = form.querySelector("[data-close-force]");
    const note = form.querySelector("[data-close-note]");
    if (!force || !note) return;
    note.hidden = !force.checked;
    const area = note.querySelector("textarea");
    if (area) area.required = force.checked;
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches && event.target.matches("[data-close-force]")) {
      syncCloseForm(event.target.closest("form"));
      if (event.target.checked) {
        const area = event.target.closest("form").querySelector("[data-close-note] textarea");
        if (area) area.focus();
      }
    }
  });

  // ---------------------------------------------------------------- copy / print

  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const text = copy.getAttribute("data-copy");
      try {
        await navigator.clipboard.writeText(text);
        flash("Hash kopiert.", "success");
      } catch (_) {
        window.prompt("Hash kopieren:", text);
      }
      return;
    }
    if (event.target.closest("[data-print]")) {
      event.preventDefault();
      window.print();
    }
  });

  // ---------------------------------------------------------------- restore confirmation

  function syncRestore(form) {
    const word = (form.getAttribute("data-restore-word") || "").toUpperCase();
    const check = form.querySelector("[data-restore-check]");
    const input = form.querySelector("[data-restore-input]");
    const file = form.querySelector('input[type="file"]');
    const submit = form.querySelector("[data-restore-submit]");
    if (!submit) return;
    const ok = (!check || check.checked) && (!input || input.value.trim().toUpperCase() === word)
      && (!file || file.files.length > 0);
    submit.disabled = !ok;
  }

  document.addEventListener("input", (event) => {
    const form = event.target.closest && event.target.closest("form[data-restore-form]");
    if (form) syncRestore(form);
  });
  document.addEventListener("change", (event) => {
    const form = event.target.closest && event.target.closest("form[data-restore-form]");
    if (form) syncRestore(form);
  });
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.matches("form[data-restore-form]")) {
      if (!window.confirm("Wirklich ALLE aktuellen Daten durch die Sicherung ersetzen?")) {
        event.preventDefault();
        return;
      }
    }
    if (form.matches("form[data-busy-form], form[data-restore-form]") && !event.defaultPrevented) {
      const btn = event.submitter || form.querySelector('button[type="submit"]');
      if (btn) setTimeout(() => { btn.disabled = true; btn.classList.add("is-busy"); }, 0);
    }
  });

  // ---------------------------------------------------------------- month chart

  const charts = [];

  function readChartData(canvas) {
    try {
      return JSON.parse(canvas.getAttribute("data-chart") || "[]");
    } catch (_) {
      return [];
    }
  }

  function buildChart(canvas) {
    const rows = readChartData(canvas);
    if (!rows.length) {
      const box = canvas.closest(".chart-box");
      if (box) box.hidden = true;
      return null;
    }
    const pos = css("--months-chart-pos", "#18794e");
    const neg = css("--months-chart-neg", "#c4302b");
    const line = css("--months-chart-line", "#2453c9");
    const grid = css("--months-chart-grid", "rgba(0,0,0,0.08)");
    const text = css("--months-chart-text", "#5b6573");
    const font = css("--font", "system-ui, sans-serif");
    return new window.Chart(canvas, {
      data: {
        labels: rows.map((r) => r.label),
        datasets: [
          {
            type: "bar",
            label: "Saldo Monat",
            data: rows.map((r) => r.saldo),
            backgroundColor: rows.map((r) => (r.saldo >= 0 ? pos : neg)),
            borderRadius: 4,
            maxBarThickness: 36,
            order: 2,
          },
          {
            type: "line",
            label: "Saldo kumuliert",
            data: rows.map((r) => r.kumuliert),
            borderColor: line,
            backgroundColor: line,
            borderWidth: 2,
            pointRadius: 3,
            cubicInterpolationMode: "monotone",  // no overshoot between months
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: text,
              font: { family: font },
              boxWidth: 12,
              // The bar dataset has one color per bar (pos/neg): show a neutral swatch instead
              // of whatever color the first bar happens to have.
              generateLabels: (chart) => window.Chart.defaults.plugins.legend.labels.generateLabels(chart)
                .map((item) => (item.datasetIndex === 0
                  ? Object.assign(item, { fillStyle: text, strokeStyle: text, lineWidth: 0 })
                  : item)),
            },
          },
          tooltip: {
            callbacks: {
              label: (ctx) => ctx.dataset.label + ": " + fmtHm(ctx.parsed.y, true) + " h",
              afterBody: (items) => {
                const r = rows[items[0].dataIndex];
                return r ? ["Ist " + fmtHm(r.ist) + " · Soll " + fmtHm(r.soll)] : [];
              },
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: text, font: { family: font } } },
          y: {
            grid: { color: grid },
            ticks: { color: text, font: { family: font }, callback: (v) => fmtHm(v, true) },
            title: { display: true, text: "Stunden", color: text, font: { family: font } },
          },
        },
      },
    });
  }

  function initCharts(root) {
    if (typeof window.Chart === "undefined") return;
    root.querySelectorAll("canvas[data-months-chart]").forEach((canvas) => {
      if (canvas.dataset.chartReady) return;
      canvas.dataset.chartReady = "1";
      const chart = buildChart(canvas);
      if (chart) charts.push({ canvas, chart });
    });
  }

  function rebuildCharts() {
    charts.splice(0).forEach(({ canvas, chart }) => {
      chart.destroy();
      delete canvas.dataset.chartReady;
    });
    initCharts(document);
  }

  // ---------------------------------------------------------------- init

  function init(root) {
    initAutosubmit(root);
    root.querySelectorAll("form[data-close-form]").forEach(syncCloseForm);
    root.querySelectorAll("form[data-restore-form]").forEach(syncRestore);
    initCharts(root);
  }

  document.addEventListener("DOMContentLoaded", () => {
    init(document);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", rebuildCharts);
  });
  document.addEventListener("htmx:afterSwap", (event) => init(event.target));
})();
