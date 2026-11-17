# Vendored front-end libraries (IP-035)

Committed, not built: the console pages load these from `static/console/vendor/` through WhiteNoise, so the
image needs no Node toolchain and the admin works without any CDN. Update by replacing the file with the
pinned version from jsdelivr and changing the version here.

| File | Package | Version | Licence |
|---|---|---|---|
| `alpine.min.js` | `alpinejs` (`dist/cdn.min.js`) | 3.14.9 | MIT (Caleb Porzio and contributors) |
| `tabulator.min.js`, `tabulator.min.css` | `tabulator-tables` | 6.3.1 | MIT, `LICENSE-tabulator.txt` |
| `cytoscape.min.js` | `cytoscape` | 3.30.4 | MIT, `LICENSE-cytoscape.txt` |
| `echarts.min.js` | `echarts` | 5.6.0 | Apache-2.0, `LICENSE-echarts.txt` |

Fonts in `../fonts/` (latin and latin-ext subsets, from Fontsource): Literata (variable weight) and
IBM Plex Sans 400/500/600, IBM Plex Mono 400, all SIL Open Font License 1.1 (`OFL-*.txt`). They are the
faces of <https://elvira.digital>.
