#!/usr/bin/env bash
# Generate the EvilFlowers Catalog docs landing page (publish/index.html).
#
# Inputs: publish/branch/<slug>/ and publish/release/<slug>/ directories,
#         each optionally containing meta.json (written by the docs workflow).
# Output: publish/index.html plus publish/assets/{elvira,index}.css.
#
# The look is elvira.digital's: .github/docs/elvira.css holds its tokens and top
# bar, shared with the Redoc template (.github/docs/redoc.hbs).

set -euo pipefail

PUBLISH_DIR="${PUBLISH_DIR_OVERRIDE:-publish}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$PUBLISH_DIR/assets"
cp "$SCRIPT_DIR/../docs/elvira.css" "$PUBLISH_DIR/assets/elvira.css"

REPO_URL="${REPO_URL:-https://github.com/EvilFlowersCatalog/EvilFlowersCatalog}"
PROTOCOL_URL="${PROTOCOL_URL:-https://github.com/EvilFlowersCatalog/evilflowers-protocol}"
SITE_URL="${SITE_URL:-https://elvira.digital}"
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
      '{kind:$kind, slug:$slug, ref:(if $ref == "" then $slug else $ref end), deployed_at:$deployed, sha:$sha, short:$short, message:$msg, author:$author}'
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
# Releases newest first by version, not by string (0.10.0 after 0.9.0).
RELEASES_JSON="$(jq '[.[] | select(.kind == "release")]
  | sort_by(.slug | [splits("[.-]") | (tonumber? // .)]) | reverse' <<<"$ENTRIES_JSON")"
BRANCH_COUNT=$(jq 'length' <<<"$BRANCHES_JSON")
RELEASE_COUNT=$(jq 'length' <<<"$RELEASES_JSON")
LATEST_RELEASE="$(jq -r '.[0].slug // ""' <<<"$RELEASES_JSON")"

escape_html() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

# ── Render entry list items (branches / releases) ───────────────────────────
render_items() {
  local list_json="$1" kind="$2" prefix="$3"
  if [ "$(jq 'length' <<<"$list_json")" = "0" ]; then
    printf '<li class="entry entry--empty">No %s published yet.</li>\n' "$kind"
    return
  fi

  jq -c '.[]' <<<"$list_json" | while read -r entry; do
    local slug ref deployed_at short sha message classes badge meta_html safe_message safe_ref
    slug="$(jq -r '.slug' <<<"$entry")"
    ref="$(jq -r '.ref // .slug' <<<"$entry")"
    deployed_at="$(jq -r '.deployed_at // ""' <<<"$entry")"
    short="$(jq -r '.short // ""' <<<"$entry")"
    sha="$(jq -r '.sha // ""' <<<"$entry")"
    message="$(jq -r '.message // ""' <<<"$entry")"

    classes="entry"
    badge=""
    if [ "$kind" = "branches" ] && [ "$slug" = "develop" ]; then
      classes+=" entry--primary"
      badge='<span class="badge badge--accent">development</span>'
    elif [ "$kind" = "releases" ] && [ "$slug" = "$LATEST_RELEASE" ]; then
      classes+=" entry--primary"
      badge='<span class="badge badge--accent">latest</span>'
    fi

    safe_message="$(escape_html "$message")"
    safe_ref="$(escape_html "$ref")"

    meta_html=""
    if [ -n "$short" ]; then
      meta_html+="<a class=\"meta__commit\" href=\"${REPO_URL}/commit/${sha}\" title=\"${sha}\">${short}</a>"
    fi
    if [ -n "$deployed_at" ]; then
      meta_html+="<time datetime=\"$deployed_at\" data-relative>$deployed_at</time>"
    fi
    if [ -n "$safe_message" ]; then
      meta_html+="<span class=\"meta__message\">$safe_message</span>"
    fi
    if [ -z "$meta_html" ]; then
      meta_html='<span class="meta__message">No deploy metadata</span>'
    fi

    cat <<HTML
        <li class="$classes">
          <a class="entry__link" href="$prefix/$slug/">
            <span class="entry__name">$safe_ref</span>
            $badge
            <span class="entry__arrow" aria-hidden="true">→</span>
          </a>
          <div class="entry__meta">$meta_html</div>
        </li>
HTML
  done
}

BRANCHES_HTML="$(render_items "$BRANCHES_JSON" branches /EvilFlowersCatalog/branch)"
RELEASES_HTML="$(render_items "$RELEASES_JSON" releases /EvilFlowersCatalog/release)"

LEAD_LINK=""
if [ -n "$LATEST_RELEASE" ]; then
  LEAD_LINK="<a class=\"button\" href=\"/EvilFlowersCatalog/release/${LATEST_RELEASE}/\">Latest release (${LATEST_RELEASE})</a>"
fi

# ── CSS (page layout; tokens and the top bar come from elvira.css) ──────────
cat > "$PUBLISH_DIR/assets/index.css" <<'CSS'
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--e-font-text);
  color: var(--e-ink);
  background: var(--e-paper);
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
a { color: var(--e-accent); }
a:hover { color: var(--e-ink); }

.page {
  max-width: var(--e-wide);
  margin: 0 auto;
  padding: 4rem 1.25rem 5rem;
}

.hero { max-width: var(--e-measure); margin-bottom: 3.5rem; }
.eyebrow {
  margin: 0 0 0.75rem;
  color: var(--e-muted);
  font-size: 0.8rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.hero h1 {
  margin: 0 0 1rem;
  font-family: var(--e-font-display);
  font-size: clamp(2rem, 4vw, 2.6rem);
  font-weight: 600;
  line-height: 1.15;
}
.hero p { margin: 0; color: var(--e-muted); font-size: 1.1rem; }
.hero__actions { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 1.75rem; }

.button {
  display: inline-flex;
  align-items: center;
  padding: 0.55rem 1rem;
  border: 1px solid var(--e-rule-strong);
  border-radius: var(--e-radius);
  color: var(--e-ink);
  font-size: 0.95rem;
  text-decoration: none;
}
.button:hover { border-color: var(--e-ink); color: var(--e-ink); }
.button--primary { background: var(--e-accent); border-color: var(--e-accent); color: #fff; }
.button--primary:hover { background: var(--e-accent-hover); border-color: var(--e-accent-hover); color: #fff; }
:root.dark .button--primary,
:root.dark .button--primary:hover { color: #151313; }
@media (prefers-color-scheme: dark) {
  :root:not(.light) .button--primary,
  :root:not(.light) .button--primary:hover { color: #151313; }
}

.columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
  gap: 3rem;
}
.section__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--e-ink);
}
.section__title {
  margin: 0;
  font-family: var(--e-font-display);
  font-size: 1.35rem;
  font-weight: 600;
}
.section__count { color: var(--e-muted); font-family: var(--e-font-mono); font-size: 0.85rem; }

.entries { list-style: none; margin: 0; padding: 0; }
.entry { border-bottom: 1px solid var(--e-rule); padding: 0.85rem 0 0.85rem 0.9rem; position: relative; }
.entry--primary::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0.85rem;
  bottom: 0.85rem;
  width: 3px;
  background: var(--e-accent);
}
.entry--empty { color: var(--e-muted); font-style: italic; }
.entry__link {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  color: var(--e-ink);
  text-decoration: none;
}
.entry__link:hover .entry__name { color: var(--e-accent); text-decoration: underline; text-underline-offset: 0.25em; }
.entry__name { font-family: var(--e-font-mono); font-size: 0.95rem; font-weight: 500; overflow-wrap: anywhere; }
.entry__arrow { margin-left: auto; color: var(--e-muted); transition: transform 0.15s ease; }
.entry__link:hover .entry__arrow { transform: translateX(3px); color: var(--e-accent); }
.entry__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 0.9rem;
  margin-top: 0.3rem;
  color: var(--e-muted);
  font-size: 0.85rem;
}
.meta__commit { font-family: var(--e-font-mono); color: var(--e-muted); text-decoration: none; }
.meta__commit:hover { color: var(--e-accent); }
.meta__message { min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.badge {
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 0.05rem 0.45rem;
  border-radius: var(--e-radius);
  border: 1px solid var(--e-rule-strong);
  color: var(--e-muted);
}
.badge--accent { border-color: var(--e-accent); color: var(--e-accent); background: var(--e-accent-wash); }

.footer {
  border-top: 1px solid var(--e-rule);
  padding: 2.5rem 1.25rem 3rem;
  font-size: 0.9rem;
  color: var(--e-muted);
}
.footer__inner {
  max-width: var(--e-wide);
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 1rem 2rem;
}
.footer a { color: var(--e-muted); }
.footer a:hover { color: var(--e-accent); }
.footer__links { display: flex; flex-wrap: wrap; gap: 1.25rem; }

@media (max-width: 560px) {
  .page { padding: 2.5rem 1rem 3.5rem; }
  .hero { margin-bottom: 2.5rem; }
}
CSS

# ── HTML ────────────────────────────────────────────────────────────────────
cat > "$PUBLISH_DIR/index.html" <<HTML
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>API reference · ELVIRA</title>
  <meta name="description" content="OpenAPI reference of the EvilFlowers Catalog, the server behind ELVIRA, for its development branch, feature branches and releases.">
  <link rel="icon" href="${SITE_URL}/img/favicon.ico">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=Literata:opsz,wght@7..72,500..700&display=swap">
  <link rel="stylesheet" href="/EvilFlowersCatalog/assets/elvira.css">
  <link rel="stylesheet" href="/EvilFlowersCatalog/assets/index.css">
  <script>
    // Colour mode before paint, shared with the Redoc pages and elvira.digital (Docusaurus \`theme\`).
    (function () {
      try {
        var mode = localStorage.getItem("redoc.colorMode") || localStorage.getItem("theme");
      } catch (_) {}
      if (mode !== "light" && mode !== "dark") {
        mode = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      document.documentElement.classList.add(mode);
    })();
  </script>
</head>
<body>
  <header class="e-bar">
    <a class="e-brand" href="${SITE_URL}/" aria-label="ELVIRA, home">
      <span class="e-wordmark">ELVIRA</span>
      <span class="e-org">Slovak University of Technology</span>
    </a>
    <nav class="e-nav" aria-label="ELVIRA">
      <a class="e-site" href="${SITE_URL}/docs/intro">Docs</a>
      <a class="e-site" href="${SITE_URL}/how-it-works">How it works</a>
      <a class="e-site" href="${SITE_URL}/news">News</a>
      <a class="e-site" href="${SITE_URL}/releases">Releases</a>
      <a href="/EvilFlowersCatalog/" aria-current="page">API</a>
      <a href="https://github.com/EvilFlowersCatalog">GitHub</a>
      <button type="button" class="e-mode" aria-label="Switch colour mode" data-mode-toggle>
        <svg class="e-moon" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 .278a.77.77 0 0 1 .08.858 7.2 7.2 0 0 0-.878 3.46c0 4.021 3.278 7.277 7.318 7.277q.792-.001 1.533-.16a.79.79 0 0 1 .81.316.73.73 0 0 1-.031.893A8.35 8.35 0 0 1 8.344 16C3.734 16 0 12.286 0 7.71 0 4.266 2.114 1.312 5.124.06A.75.75 0 0 1 6 .278"/></svg>
        <svg class="e-sun" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0m0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13m8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2a.5.5 0 0 1 .5.5M3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8m10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0m-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0m9.193 2.121a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707M4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708"/></svg>
      </button>
    </nav>
  </header>

  <main class="page">
    <section class="hero">
      <p class="eyebrow">EvilFlowers Catalog</p>
      <h1>API reference</h1>
      <p>The catalog is the server behind ELVIRA: OPDS feeds for reading apps, Readium LCP lending, OAI-PMH for aggregators, an MCP server for AI clients and the REST API that manages it all. Pick the version you run.</p>
      <div class="hero__actions">
        <a class="button button--primary" href="/EvilFlowersCatalog/branch/develop/">Development branch</a>
        ${LEAD_LINK}
        <a class="button" href="${PROTOCOL_URL}">OpenAPI files</a>
      </div>
    </section>

    <div class="columns">
      <section>
        <header class="section__header">
          <h2 class="section__title">Releases</h2>
          <span class="section__count">${RELEASE_COUNT}</span>
        </header>
        <ul class="entries">
${RELEASES_HTML}
        </ul>
      </section>

      <section>
        <header class="section__header">
          <h2 class="section__title">Branches</h2>
          <span class="section__count">${BRANCH_COUNT}</span>
        </header>
        <ul class="entries">
${BRANCHES_HTML}
        </ul>
      </section>
    </div>
  </main>

  <footer class="footer">
    <div class="footer__inner">
      <span>Built <time datetime="${GENERATED_AT}" data-relative>${GENERATED_AT}</time> from <a href="${REPO_URL}">EvilFlowersCatalog</a> · Slovak University of Technology in Bratislava</span>
      <span class="footer__links">
        <a href="${REPO_URL}/blob/develop/CHANGELOG.md">Changelog</a>
        <a href="${REPO_URL}/issues">Issues</a>
        <a href="${SITE_URL}/">elvira.digital</a>
      </span>
    </div>
  </footer>

  <script>
    (function () {
      var root = document.documentElement;
      var button = document.querySelector("[data-mode-toggle]");
      button && button.addEventListener("click", function () {
        var dark = root.classList.contains("dark")
          || (!root.classList.contains("light") && matchMedia("(prefers-color-scheme: dark)").matches);
        var next = dark ? "light" : "dark";
        root.classList.remove("light", "dark");
        root.classList.add(next);
        try { localStorage.setItem("redoc.colorMode", next); } catch (_) {}
      });

      var formatter = (typeof Intl !== "undefined" && Intl.RelativeTimeFormat)
        ? new Intl.RelativeTimeFormat("en", { numeric: "auto" })
        : null;
      var units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
      document.querySelectorAll("time[data-relative][datetime]").forEach(function (el) {
        var date = new Date(el.getAttribute("datetime"));
        if (isNaN(date.getTime()) || !formatter) return;
        var diff = (date.getTime() - Date.now()) / 1000;
        for (var i = 0; i < units.length; i++) {
          if (Math.abs(diff) >= units[i][1] || units[i][0] === "second") {
            el.textContent = formatter.format(Math.round(diff / units[i][1]), units[i][0]);
            break;
          }
        }
        el.title = date.toLocaleString();
      });
    })();
  </script>
</body>
</html>
HTML

echo "Generated $PUBLISH_DIR/index.html ($BRANCH_COUNT branches, $RELEASE_COUNT releases)"
