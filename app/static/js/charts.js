/* Deputat – charts for the screens Konto and Jahr (Chart.js 4 UMD, global `Chart`, no date adapter).
 *
 * Elements (all colors come from CSS custom properties, so light and dark mode both work; charts are
 * rebuilt when the color scheme changes):
 *
 *   <div data-saldo-chart data-src="/konto/daten.json?..."><canvas></canvas></div>
 *       Saldo line over the period (x = day index since period start, y = hours), dashed forecast,
 *       ferien bands, darker leave days, holiday markers, zero line "Referenz 40h-Job",
 *       optional Geltendmachung marker. Reloaded on the body event "deputat-konto-changed".
 *   <div data-donut-chart data-chart='{"labels":[], "values":[], "colors":[], "seconds":[]}'>
 *       Category distribution (Jahr).
 *   <div data-histogram-chart data-chart='{"labels":[], "ranges":[], "counts":[], "edges":[], "markers":[], "benchmarks":[]}'>
 *       School-week histogram with vertical markers (40 h / 48 h) and optional study values, toggled by
 *       <input type="checkbox" data-benchmark-toggle>; inside [data-year] the toggle also sets the class
 *       "show-bench" on its card (shows the study-value list).
 *   <div data-heatmap> … .yr-heat-col.is-current … </div>
 *       Server-rendered heatmap (Jahr); scrolled so the current week is visible.
 *
 * Bands and markers are drawn by a small local plugin (no external Chart.js plugins).
 */
(function () {
  "use strict";

  const registry = [];

  // ---------------------------------------------------------------- helpers

  function css(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback || "#888";
  }

  function palette() {
    return {
      text: css("--text-muted"),
      textStrong: css("--text"),
      grid: css("--chart-grid", css("--border")),
      surface: css("--surface"),
      saldo: css("--chart-saldo", css("--primary")),
      forecast: css("--chart-forecast", css("--text-muted")),
      zero: css("--chart-zero", css("--text-faint")),
      ferien: css("--chart-ferien", "rgba(36, 83, 201, 0.10)"),
      urlaub: css("--chart-urlaub", "rgba(36, 83, 201, 0.25)"),
      holiday: css("--chart-holiday", css("--warn")),
      gm: css("--chart-gm", css("--success")),
      today: css("--chart-today", css("--text-faint")),
      bar: css("--chart-bar", css("--primary")),
      barWarn: css("--chart-bar-warn", css("--warn")),
      barDanger: css("--chart-bar-danger", css("--danger")),
      bench: css("--chart-bench", css("--info")),
      font: css("--font", "system-ui, sans-serif"),
    };
  }

  function fmtHm(hours) {
    if (hours === null || hours === undefined || isNaN(hours)) return "–";
    const neg = hours < 0;
    const totalMin = Math.round(Math.abs(hours) * 60);
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    const sign = totalMin === 0 ? "" : neg ? "−" : "+";
    return sign + h + ":" + String(m).padStart(2, "0");
  }

  function fmtHmPlain(hours) {
    return fmtHm(hours).replace(/^[+−]/, "");
  }

  function parseIso(iso) {
    const p = iso.split("-").map(Number);
    return new Date(Date.UTC(p[0], p[1] - 1, p[2]));
  }

  function dateOf(x0, index) {
    const d = new Date(parseIso(x0).getTime() + Math.round(index) * 86400000);
    const dd = String(d.getUTCDate()).padStart(2, "0");
    const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
    const wd = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"][d.getUTCDay()];
    return wd + " " + dd + "." + mm + "." + d.getUTCFullYear();
  }

  function readJsonAttr(el) {
    try {
      return JSON.parse(el.getAttribute("data-chart") || "null");
    } catch (_) {
      return null;
    }
  }

  function showFallback(el, text) {
    const fb = el.querySelector("[data-chart-fallback]");
    if (fb) {
      fb.textContent = text;
      fb.hidden = false;
    }
  }

  function hideFallback(el) {
    const fb = el.querySelector("[data-chart-fallback]");
    if (fb) fb.hidden = true;
  }

  function baseOptions(c) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 250 },
      color: c.text,
      font: { family: c.font },
    };
  }

  // ---------------------------------------------------------------- plugin: bands and markers

  function xPixel(scale, value) {
    return scale.getPixelForValue(value);
  }

  // Label placement: right of the line unless align "left" or no room; returns [x0, x1] of the label.
  function labelBox(ctx, area, x, label, align) {
    const w = ctx.measureText(label).width;
    let tx = x + 4;
    if (align === "left" || tx + w > area.right) tx = Math.max(area.left + 2, x - 4 - w);
    return [tx, tx + w];
  }

  function drawVLine(ctx, area, x, color, dash, label, labelColor, align, row) {
    if (x < area.left - 1 || x > area.right + 1) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    ctx.moveTo(x, area.top);
    ctx.lineTo(x, area.bottom);
    ctx.stroke();
    if (label) {
      ctx.setLineDash([]);
      ctx.fillStyle = labelColor || color;
      ctx.font = "600 11px " + ctx.__fontFamily;
      const box = labelBox(ctx, area, x, label, align);
      const ty = area.top + 12 + 13 * (row || 0);
      if (ctx.__surface) {
        // Backdrop so the label stays readable on top of a bar.
        ctx.fillStyle = ctx.__surface;
        ctx.globalAlpha = 0.85;
        ctx.fillRect(box[0] - 2, ty - 10, box[1] - box[0] + 4, 13);
        ctx.globalAlpha = 1;
        ctx.fillStyle = labelColor || color;
      }
      ctx.fillText(label, box[0], ty);
    }
    ctx.restore();
  }

  const bandsPlugin = {
    id: "deputatBands",
    beforeDatasetsDraw(chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      const { ctx, chartArea: area } = chart;
      const xs = chart.scales.x;
      ctx.save();
      ctx.beginPath();
      ctx.rect(area.left, area.top, area.width, area.height);
      ctx.clip();
      (opts.ferien || []).forEach((f) => {
        const x1 = xPixel(xs, f.from - 0.5);
        const x2 = xPixel(xs, f.to + 0.5);
        ctx.fillStyle = opts.colors.ferien;
        ctx.fillRect(x1, area.top, x2 - x1, area.height);
      });
      // Leave days: merge consecutive indices into one rectangle.
      const days = (opts.urlaub || []).slice().sort((a, b) => a - b);
      let i = 0;
      ctx.fillStyle = opts.colors.urlaub;
      while (i < days.length) {
        let j = i;
        while (j + 1 < days.length && days[j + 1] <= days[j] + 1) j++;
        const x1 = xPixel(xs, days[i] - 0.5);
        const x2 = xPixel(xs, days[j] + 0.5);
        ctx.fillRect(x1, area.top, x2 - x1, area.height);
        i = j + 1;
      }
      ctx.restore();
    },
    afterDatasetsDraw(chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      const { ctx, chartArea: area } = chart;
      ctx.__fontFamily = opts.colors.font;
      ctx.__surface = opts.colors.surface;
      const ys = chart.scales.y;
      const xs = chart.scales.x;
      // Zero line = reference 40h job.
      const y0 = ys.getPixelForValue(0);
      if (y0 >= area.top - 1 && y0 <= area.bottom + 1) {
        ctx.save();
        ctx.strokeStyle = opts.colors.zero;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(area.left, y0);
        ctx.lineTo(area.right, y0);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = opts.colors.text;
        ctx.font = "600 11px " + opts.colors.font;
        const label = opts.zeroLabel || "";
        // Put the label on the side of the zero line where the curves are not (they start at 0).
        let ty = opts.zeroLabelBelow ? y0 + 14 : y0 - 5;
        if (ty < area.top + 12) ty = y0 + 14;
        if (ty > area.bottom - 2) ty = y0 - 5;
        ctx.fillText(label, area.left + 4, ty);
        ctx.restore();
      }
      if (opts.today !== null && opts.today !== undefined) {
        drawVLine(ctx, area, xPixel(xs, opts.today), opts.colors.today, [1, 3], null);
      }
      if (opts.geltendmachung) {
        drawVLine(ctx, area, xPixel(xs, opts.geltendmachung.i - 0.5), opts.colors.gm, [5, 4],
          opts.geltendmachung.label, opts.colors.gm);
      }
      // Histogram markers (40 h / 48 h) and optional study values, positioned by hours inside the bins.
      // Labels that would overlap (study values lie close to 48 h) move down one row each.
      ctx.font = "600 11px " + opts.colors.font;
      const rows = [];
      (opts.hourMarkers || []).forEach((m) => {
        const x = hoursToPixel(chart, opts.edges, m.h);
        if (x === null) return;
        const box = labelBox(ctx, area, x, m.label || "", m.align);
        let row = 0;
        while (rows[row] && rows[row].some((b) => box[0] < b[1] + 4 && b[0] < box[1] + 4)) row++;
        (rows[row] = rows[row] || []).push(box);
        drawVLine(ctx, area, x, m.color, m.dash, m.label, m.color, m.align, row);
      });
    },
  };

  function hoursToPixel(chart, edges, h) {
    if (!edges || edges.length < 2) return null;
    const xs = chart.scales.x;
    const n = edges.length - 1;
    let k = 0;
    while (k < n - 1 && h >= edges[k + 1]) k++;
    const frac = Math.min(1, Math.max(0, (h - edges[k]) / (edges[k + 1] - edges[k])));
    const width = xs.width / n;
    const left = xs.getPixelForValue(k) - width / 2;
    return left + frac * width;
  }

  // ---------------------------------------------------------------- saldo chart (Konto)

  function mostlyAbove(data) {
    let above = 0;
    let below = 0;
    (data.saldo || []).concat(data.prognose || []).forEach((p) => {
      if (p[1] > 0) above++;
      else if (p[1] < 0) below++;
    });
    return above >= below;
  }

  function buildSaldo(el, data) {
    const c = palette();
    const canvas = el.querySelector("canvas");
    const monthLabels = {};
    (data.months || []).forEach((m) => { monthLabels[m.i] = m.label; });
    const pts = (arr) => (arr || []).map((p) => ({ x: p[0], y: p[1] }));
    const datasets = [
      {
        key: "saldo",
        label: data.labels.saldo,
        data: pts(data.saldo),
        borderColor: c.saldo,
        backgroundColor: c.saldo,
        borderWidth: 2.2,
        pointRadius: 0,
        pointHoverRadius: 4,
        tension: 0,
        order: 1,
      },
      {
        key: "prognose",
        label: data.labels.prognose,
        data: pts(data.prognose),
        borderColor: c.forecast,
        backgroundColor: c.forecast,
        borderDash: [6, 5],
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 3,
        tension: 0,
        order: 2,
      },
      {
        key: "feiertag",
        label: data.labels.feiertag,
        data: (data.feiertage || []).map((f) => ({ x: f.i, y: f.y, name: f.name })),
        showLine: false,
        pointStyle: "triangle",
        pointRadius: 5,
        pointHoverRadius: 7,
        borderColor: c.holiday,
        backgroundColor: c.holiday,
        order: 0,
      },
    ];
    return new Chart(canvas, {
      type: "line",
      data: { datasets },
      plugins: [bandsPlugin],
      options: Object.assign(baseOptions(c), {
        parsing: false,
        normalized: true,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales: {
          x: {
            type: "linear",
            min: 0,
            max: Math.max(1, data.n_days - 1),
            grid: { color: c.grid, drawTicks: false },
            border: { color: c.grid },
            afterBuildTicks: (axis) => {
              axis.ticks = (data.months || []).map((m) => ({ value: m.i }));
            },
            ticks: {
              color: c.text,
              autoSkip: true,
              maxRotation: 0,
              padding: 6,
              callback: (value) => monthLabels[value] || "",
            },
          },
          y: {
            suggestedMin: 0,
            suggestedMax: 0,
            grace: "8%",
            grid: { color: c.grid },
            border: { color: c.grid },
            ticks: { color: c.text, callback: (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v) },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => (items.length ? dateOf(data.x0, items[0].parsed.x) : ""),
              label: (item) => {
                const ds = item.dataset;
                if (ds.key === "feiertag") return "Feiertag: " + item.raw.name;
                return ds.label + ": " + fmtHm(item.parsed.y) + " h";
              },
            },
          },
          deputatBands: {
            enabled: true,
            ferien: data.ferien,
            urlaub: data.urlaub,
            today: data.today,
            zeroLabel: data.labels.zero,
            zeroLabelBelow: mostlyAbove(data),
            geltendmachung: data.geltendmachung,
            colors: c,
          },
        },
      }),
    });
  }

  function initSaldo(el) {
    const entry = { el, chart: null, data: null };
    entry.build = () => {
      if (entry.chart) entry.chart.destroy();
      entry.chart = entry.data ? buildSaldo(el, entry.data) : null;
    };
    entry.load = () => {
      const src = el.getAttribute("data-src");
      return fetch(src, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then((res) => {
          if (!res.ok) throw new Error(String(res.status));
          return res.json();
        })
        .then((data) => {
          entry.data = data;
          hideFallback(el);
          entry.build();
        })
        .catch(() => showFallback(el, "Diagramm konnte nicht geladen werden."));
    };
    registry.push(entry);
    entry.load();
    document.body.addEventListener("deputat-konto-changed", () => entry.load());
  }

  // ---------------------------------------------------------------- donut (Jahr)

  function initDonut(el) {
    const data = readJsonAttr(el);
    if (!data || !data.values || !data.values.length) {
      showFallback(el, "Noch keine Einträge.");
      return;
    }
    const entry = { el, chart: null };
    entry.build = () => {
      if (entry.chart) entry.chart.destroy();
      const c = palette();
      const total = data.values.reduce((a, b) => a + b, 0) || 1;
      entry.chart = new Chart(el.querySelector("canvas"), {
        type: "doughnut",
        data: {
          labels: data.labels,
          datasets: [{ data: data.values, backgroundColor: data.colors, borderColor: c.surface, borderWidth: 2 }],
        },
        options: Object.assign(baseOptions(c), {
          cutout: "62%",
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (item) =>
                  item.label + ": " + fmtHmPlain(item.parsed) + " h (" +
                  Math.round((100 * item.parsed) / total) + " %)",
              },
            },
          },
        }),
      });
    };
    hideFallback(el);
    registry.push(entry);
    entry.build();
  }

  // ---------------------------------------------------------------- histogram (Jahr)

  function initHistogram(el) {
    const data = readJsonAttr(el);
    if (!data || !data.counts || !data.counts.length) {
      showFallback(el, "Noch keine abgeschlossenen Schulwochen.");
      return;
    }
    const toggle = document.querySelector("[data-benchmark-toggle]");
    const entry = { el, chart: null };
    entry.build = () => {
      if (entry.chart) entry.chart.destroy();
      const c = palette();
      const colors = data.edges.slice(0, -1).map((lo) => (lo >= 48 ? c.barDanger : lo >= 40 ? c.barWarn : c.bar));
      const markers = (data.markers || []).map((m) => ({
        h: m.h, label: m.label, color: c.textStrong, dash: [4, 3], align: m.h >= 48 ? "right" : "left",
      }));
      if (toggle && toggle.checked) {
        (data.benchmarks || []).forEach((b) => {
          markers.push({ h: b.h, label: b.short, color: c.bench, dash: [1, 3], align: "right" });
        });
      }
      entry.chart = new Chart(el.querySelector("canvas"), {
        type: "bar",
        data: {
          labels: data.labels,
          datasets: [{ label: "Schulwochen", data: data.counts, backgroundColor: colors, borderRadius: 3, maxBarThickness: 36 }],
        },
        plugins: [bandsPlugin],
        options: Object.assign(baseOptions(c), {
          scales: {
            x: {
              grid: { display: false },
              border: { color: c.grid },
              ticks: { color: c.text, maxRotation: 0, autoSkip: true },
              title: { display: true, text: "Stunden je Schulwoche (erfasst)", color: c.text },
            },
            y: {
              beginAtZero: true,
              grace: "30%", // room above the bars for the marker labels
              grid: { color: c.grid },
              border: { color: c.grid },
              ticks: { color: c.text, precision: 0 },
              title: { display: true, text: "Wochen", color: c.text },
            },
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: (items) => (items.length && data.ranges ? data.ranges[items[0].dataIndex] : ""),
                label: (item) => item.parsed.y + (item.parsed.y === 1 ? " Woche" : " Wochen"),
              },
            },
            deputatBands: { enabled: true, edges: data.edges, hourMarkers: markers, colors: c, today: null },
          },
        }),
      });
    };
    hideFallback(el);
    registry.push(entry);
    entry.build();
    if (toggle) toggle.addEventListener("change", () => entry.build());
  }

  // ---------------------------------------------------------------- benchmark toggle + heatmap (Jahr)

  function initBenchmarkToggles() {
    // Shows the study-value list next to a toggle (CSS fallback for :has()).
    document.querySelectorAll("[data-year] [data-benchmark-toggle]").forEach((box) => {
      const card = box.closest(".card");
      if (!card) return;
      const sync = () => card.classList.toggle("show-bench", box.checked);
      box.addEventListener("change", sync);
      sync();
    });
  }

  function initHeatmap(wrap) {
    // Scroll the heatmap so the current week is visible (it starts in August).
    const current = wrap.querySelector(".yr-heat-col.is-current");
    if (current && wrap.scrollWidth > wrap.clientWidth) {
      wrap.scrollLeft = Math.max(0, current.offsetLeft - wrap.clientWidth * 0.75);
    }
  }

  // ---------------------------------------------------------------- boot

  function init() {
    initBenchmarkToggles();
    document.querySelectorAll("[data-heatmap]").forEach(initHeatmap);
    if (typeof window.Chart === "undefined") {
      document.querySelectorAll("[data-saldo-chart], [data-donut-chart], [data-histogram-chart]").forEach((el) =>
        showFallback(el, "Diagramme sind nicht verfügbar."));
      return;
    }
    document.querySelectorAll("[data-saldo-chart]").forEach(initSaldo);
    document.querySelectorAll("[data-donut-chart]").forEach(initDonut);
    document.querySelectorAll("[data-histogram-chart]").forEach(initHistogram);

    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const rebuild = () => registry.forEach((e) => e.build && e.build());
    if (mq.addEventListener) mq.addEventListener("change", rebuild);
    else if (mq.addListener) mq.addListener(rebuild);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.deputat = Object.assign(window.deputat || {}, { charts: { fmtHm, registry } });
})();
