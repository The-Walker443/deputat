# Vendored JavaScript libraries

All third-party JS is served from this directory; the app makes no runtime requests to CDNs.

| File | Package | Version | Source URL | SHA-256 |
|---|---|---|---|---|
| `htmx.min.js` | htmx.org | 2.0.4 | https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `alpine.min.js` | @alpinejs/csp | 3.14.8 | https://cdn.jsdelivr.net/npm/@alpinejs/csp@3.14.8/dist/cdn.min.js | `c98f46291087f0f93c9cdefb682d120fe1d2b3dcb649de83cfc1ad5b1fac58e4` |
| `chart.umd.min.js` | chart.js | 4.4.7 | https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js | `206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e` |

## Notes

- **Alpine CSP build.** The Content-Security-Policy forbids `unsafe-eval`, so the standard Alpine build
  does not work. The CSP build only accepts simple property paths / method names in directives
  (`x-data="timer"`, `@click="start"`, `x-text="label"`), no inline JS expressions. Every component must be
  registered in an external file via `Alpine.data('name', () => ({...}))` inside an
  `alpine:init` listener. Scripts that register components must load *before* `alpine.min.js`
  (all are `defer`, so document order decides).
- **htmx.** `base.html` sets `htmx-config` with `allowEval: false`, `allowScriptTags: false`,
  `includeIndicatorStyles: false` (indicator CSS lives in `app.css`). `hx-on:*` attributes and
  `js:`-prefixed `hx-vals` therefore do not work; use event listeners in external JS.
- **Chart.js.** UMD build, exposes global `Chart`. No date adapter is bundled; use `category`/`linear` axes
  with pre-formatted labels.
- To update: download the new pinned version from the same CDN path, replace the file, update this table.
