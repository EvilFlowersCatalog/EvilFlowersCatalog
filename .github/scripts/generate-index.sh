#!/usr/bin/env bash
# Generate the EvilFlowers Catalog docs landing page (publish/index.html).
#
# Inputs: publish/branch/<slug>/ and publish/release/<slug>/ directories,
#         each optionally containing meta.json (written by the docs workflow).
# Output: publish/index.html plus publish/logo.png and publish/assets/index.css.

set -euo pipefail

PUBLISH_DIR="${PUBLISH_DIR_OVERRIDE:-publish}"
mkdir -p "$PUBLISH_DIR/assets"

# Bundle the project logo if present so the landing page can show it.
if [ -f "docs/images/logo.png" ]; then
  cp docs/images/logo.png "$PUBLISH_DIR/logo.png"
fi

REPO_URL="${REPO_URL:-https://github.com/EvilFlowersCatalog/EvilFlowersCatalog}"
PROTOCOL_URL="${PROTOCOL_URL:-https://github.com/EvilFlowersCatalog/evilflowers-protocol}"
SITE_TITLE="${SITE_TITLE:-EvilFlowers Catalog}"
SITE_SUBTITLE="${SITE_SUBTITLE:-OPDS 1.2 digital publication catalog server}"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# ── Collect entries (branches + releases) as JSON for templating ────────────
collect_entries() {
  local kind="$1" base="$2"
  [ -d "$base" ] || return 0
  for dir in "$base"/*/; do
    [ -d "$dir" ] || continue
    local slug ref deployed sha short msg author
    slug="$(basename "$dir")"
    if [ -f "$dir/meta.json" ]; then
      ref="$(jq -r '.ref // ""'              "$dir/meta.json")"
      deployed="$(jq -r '.deployed_at // ""'  "$dir/meta.json")"
      sha="$(jq -r '.commit.sha // ""'        "$dir/meta.json")"
      short="$(jq -r '.commit.short // ""'    "$dir/meta.json")"
      msg="$(jq -r '.commit.message // ""'    "$dir/meta.json")"
      author="$(jq -r '.commit.author // ""'  "$dir/meta.json")"
    else
      ref=""; deployed=""; sha=""; short=""; msg=""; author=""
    fi
    jq -n \
      --arg kind     "$kind" \
      --arg slug     "$slug" \
      --arg ref      "$ref" \
      --arg deployed "$deployed" \
      --arg sha      "$sha" \
      --arg short    "$short" \
      --arg msg      "$msg" \
      --arg author   "$author" \
      '{kind:$kind, slug:$slug, ref:($ref // $slug), deployed_at:$deployed, sha:$sha, short:$short, message:$msg, author:$author}'
  done
}

ENTRIES_JSON="$(
  {
    collect_entries branch  "$PUBLISH_DIR/branch"
    collect_entries release "$PUBLISH_DIR/release"
  } | jq -s '.'
)"

BRANCHES_JSON="$(jq '
  [.[] | select(.kind == "branch")] as $branches
  | (
      ($branches | map(select(.slug == "develop")))
      + ($branches | map(select(.slug != "develop")) | sort_by(.deployed_at // "") | reverse)
    )
' <<<"$ENTRIES_JSON")"
RELEASES_JSON="$(jq '[.[] | select(.kind == "release")] | sort_by(.slug) | reverse' <<<"$ENTRIES_JSON")"
BRANCH_COUNT=$(jq 'length' <<<"$BRANCHES_JSON")
RELEASE_COUNT=$(jq 'length' <<<"$RELEASES_JSON")

# ── Render entry list items (branches / releases) ───────────────────────────
render_items() {
  local list_json="$1" kind="$2" prefix="$3"
  if [ "$(jq 'length' <<<"$list_json")" = "0" ]; then
    printf '<li class="entry entry--empty">No %s available yet.</li>\n' "$kind"
    return
  fi
  jq -r --arg prefix "$prefix" '
    .[] |
    @json "ENTRY"
  ' <<<"$list_json" >/dev/null  # validate JSON syntax

  jq -c '.[]' <<<"$list_json" | while read -r entry; do
    local slug ref deployed_at short sha message is_main badge
    slug="$(jq -r '.slug' <<<"$entry")"
    ref="$(jq -r '.ref // .slug' <<<"$entry")"
    deployed_at="$(jq -r '.deployed_at // ""' <<<"$entry")"
    short="$(jq -r '.short // ""' <<<"$entry")"
    sha="$(jq -r '.sha // ""' <<<"$entry")"
    message="$(jq -r '.message // ""' <<<"$entry")"

    is_main=""
    badge=""
    if [ "$kind" = "branches" ] && [ "$slug" = "develop" ]; then
      is_main=" entry--primary"
      badge='<span class="badge badge--accent">main</span>'
    elif [ "$kind" = "releases" ]; then
      badge='<span class="badge badge--muted">release</span>'
    fi

    # Pre-escape values that may contain HTML metacharacters.
    safe_message="$(printf '%s' "$message" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
    safe_ref="$(printf '%s' "$ref" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"

    meta_html=""
    if [ -n "$short" ]; then
      meta_html+="<span class=\"meta__chip\" title=\"$sha\"><svg viewBox=\"0 0 16 16\" aria-hidden=\"true\"><path d=\"M11.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm-2.25.75a2.25 2.25 0 1 1 3 2.122v1.256a2.251 2.251 0 0 1-1.875 2.218l-1.5.25a.75.75 0 0 0-.625.74V11.378a2.251 2.251 0 1 1-1.5 0V8.622A2.25 2.25 0 0 1 8.625 6.4l1.5-.25a.751.751 0 0 0 .625-.74V4.372A2.251 2.251 0 0 1 9.5 3.25ZM4.25 12a.75.75 0 1 1 1.5 0 .75.75 0 0 1-1.5 0ZM5 2.5a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5Zm-.75 2.872a2.251 2.251 0 1 1 1.5 0v5.256a2.251 2.251 0 1 1-1.5 0Z\"/></svg>$short</span>"
    fi
    if [ -n "$deployed_at" ]; then
      meta_html+="<span class=\"meta__chip\"><time datetime=\"$deployed_at\" data-relative></time></span>"
    fi
    if [ -n "$safe_message" ]; then
      meta_html+="<span class=\"meta__message\">$safe_message</span>"
    fi
    if [ -z "$meta_html" ]; then
      meta_html='<span class="meta__chip meta__chip--muted">No deploy metadata</span>'
    fi

    cat <<HTML
        <li class="entry$is_main">
          <a class="entry__link" href="$prefix/$slug/">
            <div class="entry__row">
              <span class="entry__name">$safe_ref</span>
              $badge
            </div>
            <div class="entry__meta">
              $meta_html
            </div>
            <svg class="entry__arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M6.22 4.22a.75.75 0 0 1 1.06 0l3.25 3.25a.75.75 0 0 1 0 1.06l-3.25 3.25a.75.75 0 0 1-1.06-1.06L8.94 8 6.22 5.28a.75.75 0 0 1 0-1.06Z"/></svg>
          </a>
        </li>
HTML
  done
}

BRANCHES_HTML="$(render_items "$BRANCHES_JSON" branches /EvilFlowersCatalog/branch)"
RELEASES_HTML="$(render_items "$RELEASES_JSON" releases /EvilFlowersCatalog/release)"

# ── CSS ─────────────────────────────────────────────────────────────────────
cat > "$PUBLISH_DIR/assets/index.css" <<'CSS'
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --bg-elev: #ffffff;
  --bg-subtle: #f7f7f8;
  --border: #e5e7eb;
  --border-strong: #d1d5db;
  --text: #111827;
  --text-muted: #6b7280;
  --text-subtle: #9ca3af;
  --accent: #4f46e5;
  --accent-soft: #eef2ff;
  --accent-fg: #4338ca;
  --danger: #b91c1c;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.04);
  --shadow-hover: 0 4px 12px rgba(15, 23, 42, 0.08);
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", "Fira Code", Menlo, Consolas, monospace;
  --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}

html[data-theme="dark"],
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0b0d10;
    --bg-elev: #11141a;
    --bg-subtle: #161a22;
    --border: #1f242d;
    --border-strong: #2a313c;
    --text: #e5e7eb;
    --text-muted: #9ca3af;
    --text-subtle: #6b7280;
    --accent: #818cf8;
    --accent-soft: rgba(129, 140, 248, 0.12);
    --accent-fg: #c7d2fe;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
    --shadow-hover: 0 6px 24px rgba(0, 0, 0, 0.45);
  }
}
html[data-theme="dark"] {
  --bg: #0b0d10;
  --bg-elev: #11141a;
  --bg-subtle: #161a22;
  --border: #1f242d;
  --border-strong: #2a313c;
  --text: #e5e7eb;
  --text-muted: #9ca3af;
  --text-subtle: #6b7280;
  --accent: #818cf8;
  --accent-soft: rgba(129, 140, 248, 0.12);
  --accent-fg: #c7d2fe;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-hover: 0 6px 24px rgba(0, 0, 0, 0.45);
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  font-feature-settings: "cv02", "cv11", "ss01";
  color: var(--text);
  background: var(--bg);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  line-height: 1.55;
}

.page {
  max-width: 880px;
  margin: 0 auto;
  padding: 64px 24px 96px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 56px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  color: inherit;
}
.brand__logo {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  object-fit: cover;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
}
.brand__title {
  font-weight: 600;
  letter-spacing: -0.01em;
  font-size: 15px;
}
.brand__sub {
  color: var(--text-muted);
  font-size: 13px;
}

.theme-toggle {
  appearance: none;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text-muted);
  width: 36px;
  height: 36px;
  border-radius: 8px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}
.theme-toggle:hover { color: var(--text); border-color: var(--border-strong); }
.theme-toggle svg { width: 16px; height: 16px; fill: currentColor; }
html[data-theme="dark"] .theme-toggle .icon-sun { display: block; }
html[data-theme="dark"] .theme-toggle .icon-moon { display: none; }
.theme-toggle .icon-sun { display: none; }
.theme-toggle .icon-moon { display: block; }
@media (prefers-color-scheme: dark) {
  html:not([data-theme="light"]) .theme-toggle .icon-sun { display: block; }
  html:not([data-theme="light"]) .theme-toggle .icon-moon { display: none; }
}

.hero { margin-bottom: 56px; }
.hero h1 {
  margin: 0 0 12px;
  font-size: clamp(32px, 4vw, 44px);
  font-weight: 700;
  letter-spacing: -0.025em;
  line-height: 1.15;
}
.hero p {
  margin: 0;
  max-width: 60ch;
  color: var(--text-muted);
  font-size: 17px;
}
.hero__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 20px;
}
.hero__chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 12.5px;
  font-family: var(--mono);
  text-decoration: none;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.hero__chip:hover { color: var(--text); border-color: var(--border-strong); }
.hero__chip svg { width: 12px; height: 12px; fill: currentColor; }

.section { margin-top: 40px; }
.section__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 4px 12px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
}
.section__title {
  font-size: 11.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-muted);
  font-weight: 600;
  margin: 0;
}
.section__count {
  font-size: 12px;
  color: var(--text-subtle);
  font-family: var(--mono);
}

.entries {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.entry { position: relative; }
.entry--empty {
  padding: 16px;
  color: var(--text-subtle);
  font-style: italic;
  font-size: 14px;
  border: 1px dashed var(--border);
  border-radius: var(--radius-sm);
}

.entry__link {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: 8px 16px;
  padding: 14px 16px;
  border-radius: var(--radius-sm);
  text-decoration: none;
  color: inherit;
  border: 1px solid transparent;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
}
.entry__link:hover {
  background: var(--bg-subtle);
  border-color: var(--border);
}
.entry--primary .entry__link {
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 25%, transparent);
}
.entry--primary .entry__link:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
}

.entry__row {
  display: flex;
  align-items: center;
  gap: 10px;
  grid-column: 1 / 2;
}
.entry__name {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 500;
}
.entry--primary .entry__name { color: var(--accent-fg); }

.badge {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-muted);
}
.badge--accent {
  background: var(--accent);
  color: white;
  border-color: transparent;
}
.badge--muted { color: var(--text-muted); }

.entry__meta {
  grid-column: 1 / 2;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  align-items: center;
  font-size: 12.5px;
  color: var(--text-muted);
}
.meta__chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--mono);
  font-size: 12px;
}
.meta__chip svg { width: 12px; height: 12px; fill: currentColor; opacity: 0.7; }
.meta__chip--muted { font-style: italic; }
.meta__message {
  color: var(--text-subtle);
  font-size: 12.5px;
  max-width: 50ch;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.entry__arrow {
  grid-row: 1 / span 2;
  grid-column: 2 / 3;
  width: 16px;
  height: 16px;
  fill: var(--text-subtle);
  transition: transform 0.15s ease, fill 0.15s ease;
}
.entry__link:hover .entry__arrow {
  transform: translateX(2px);
  fill: var(--text);
}

.footer {
  margin-top: 64px;
  padding-top: 24px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 12px;
  color: var(--text-subtle);
  font-size: 12.5px;
}
.footer a {
  color: var(--text-muted);
  text-decoration: none;
  transition: color 0.15s ease;
}
.footer a:hover { color: var(--text); }
.footer__links { display: flex; gap: 16px; flex-wrap: wrap; }

@media (max-width: 560px) {
  .page { padding: 40px 18px 64px; }
  .topbar { margin-bottom: 36px; }
  .hero { margin-bottom: 36px; }
  .entry__link { padding: 12px 14px; }
  .meta__message { max-width: 28ch; }
}
CSS

# ── HTML ────────────────────────────────────────────────────────────────────
LOGO_TAG=""
if [ -f "$PUBLISH_DIR/logo.png" ]; then
  LOGO_TAG='<img class="brand__logo" src="/EvilFlowersCatalog/logo.png" alt="EvilFlowers logo">'
fi

cat > "$PUBLISH_DIR/index.html" <<HTML
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0b0d10" media="(prefers-color-scheme: dark)">
  <meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
  <title>${SITE_TITLE} — Documentation</title>
  <meta name="description" content="${SITE_SUBTITLE} — OpenAPI documentation across branches and releases.">
  <link rel="icon" href="/EvilFlowersCatalog/logo.png" type="image/png">
  <link rel="preconnect" href="https://rsms.me/">
  <link rel="stylesheet" href="https://rsms.me/inter/inter.css">
  <link rel="stylesheet" href="/EvilFlowersCatalog/assets/index.css">
  <script>
    // Theme preference must apply before paint to avoid flashes.
    (function () {
      try {
        var saved = localStorage.getItem("efc-theme");
        if (saved === "light" || saved === "dark") {
          document.documentElement.setAttribute("data-theme", saved);
        }
      } catch (_) {}
    })();
  </script>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <a class="brand" href="/EvilFlowersCatalog/">
        ${LOGO_TAG}
        <span>
          <span class="brand__title">${SITE_TITLE}</span><br>
          <span class="brand__sub">Documentation</span>
        </span>
      </a>
      <button type="button" class="theme-toggle" aria-label="Toggle color theme" data-theme-toggle>
        <svg class="icon-moon" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 .278a.77.77 0 0 1 .08.858 7.2 7.2 0 0 0-.878 3.46c0 4.021 3.278 7.277 7.318 7.277.527 0 1.04-.055 1.533-.16a.77.77 0 0 1 .81 1.094A8 8 0 1 1 6 .278M4.858 1.311A7.27 7.27 0 0 0 1.025 7.752a7.265 7.265 0 0 0 7.318 7.252 7.27 7.27 0 0 0 6.42-3.821 8.6 8.6 0 0 1-1.36.105c-4.872 0-8.825-3.927-8.825-8.776 0-.486.04-.965.115-1.434q.21-.078.16-.067Z"/></svg>
        <svg class="icon-sun" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0m0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13m8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2a.5.5 0 0 1 .5.5M3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8m10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0m-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0m9.193 2.121a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707M4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708"/></svg>
      </button>
    </header>

    <section class="hero">
      <h1>${SITE_TITLE}</h1>
      <p>${SITE_SUBTITLE}. Browse the OpenAPI reference for the active development branch, feature branches, and tagged releases.</p>
      <div class="hero__chips">
        <a class="hero__chip" href="${REPO_URL}">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8a8 8 0 0 0 5.47 7.59c.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8"/></svg>
          GitHub
        </a>
        <a class="hero__chip" href="${PROTOCOL_URL}">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2zm2-1a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1zm9.5 1a.5.5 0 0 1 .5.5v8a2.5 2.5 0 0 1-2.5 2.5h-5a.5.5 0 0 1 0-1h5A1.5 1.5 0 0 0 13 12.5v-8a.5.5 0 0 1 .5-.5"/></svg>
          Protocol
        </a>
      </div>
    </section>

    <section class="section">
      <header class="section__header">
        <h2 class="section__title">Branches</h2>
        <span class="section__count">${BRANCH_COUNT}</span>
      </header>
      <ul class="entries">
${BRANCHES_HTML}
      </ul>
    </section>

    <section class="section">
      <header class="section__header">
        <h2 class="section__title">Releases</h2>
        <span class="section__count">${RELEASE_COUNT}</span>
      </header>
      <ul class="entries">
${RELEASES_HTML}
      </ul>
    </section>

    <footer class="footer">
      <span>Generated <time datetime="${GENERATED_AT}" data-relative>${GENERATED_AT}</time></span>
      <span class="footer__links">
        <a href="${REPO_URL}/issues">Issues</a>
        <a href="${REPO_URL}/blob/develop/CHANGELOG.md">Changelog</a>
        <a href="${REPO_URL}">Source</a>
      </span>
    </footer>
  </main>

  <script>
    (function () {
      var root = document.documentElement;
      var btn = document.querySelector("[data-theme-toggle]");
      function preferred() {
        try { return localStorage.getItem("efc-theme"); } catch (_) { return null; }
      }
      btn && btn.addEventListener("click", function () {
        var current = root.getAttribute("data-theme")
          || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
        var next = current === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try { localStorage.setItem("efc-theme", next); } catch (_) {}
      });

      // Relative-time rendering.
      var formatter = (typeof Intl !== "undefined" && Intl.RelativeTimeFormat)
        ? new Intl.RelativeTimeFormat(navigator.language || "en", { numeric: "auto" })
        : null;
      function relative(date) {
        var diff = (date.getTime() - Date.now()) / 1000;
        var units = [
          ["year",   60 * 60 * 24 * 365],
          ["month",  60 * 60 * 24 * 30],
          ["week",   60 * 60 * 24 * 7],
          ["day",    60 * 60 * 24],
          ["hour",   60 * 60],
          ["minute", 60],
          ["second", 1]
        ];
        for (var i = 0; i < units.length; i++) {
          var unit = units[i][0], secs = units[i][1];
          if (Math.abs(diff) >= secs || unit === "second") {
            return formatter
              ? formatter.format(Math.round(diff / secs), unit)
              : date.toISOString().slice(0, 10);
          }
        }
        return date.toISOString();
      }
      document.querySelectorAll("time[data-relative][datetime]").forEach(function (el) {
        var iso = el.getAttribute("datetime");
        if (!iso) return;
        var d = new Date(iso);
        if (isNaN(d.getTime())) return;
        el.textContent = relative(d);
        el.title = d.toLocaleString();
      });
    })();
  </script>
</body>
</html>
HTML

echo "Generated $PUBLISH_DIR/index.html ($BRANCH_COUNT branches, $RELEASE_COUNT releases)"
