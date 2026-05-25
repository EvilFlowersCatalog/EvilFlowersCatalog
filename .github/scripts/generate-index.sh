#!/bin/bash
# .github/scripts/generate-index.sh
#
# Generates publish/index.html from the contents of publish/branch/* and
# publish/release/*. When a branch/release directory contains a meta.json
# sidecar (written by the docs workflow), commit info and deploy time are
# rendered next to the link.

set -euo pipefail

OUTPUT_FILE="publish/index.html"

mkdir -p publish
if [ -f "docs/images/logo.png" ]; then
  cp docs/images/logo.png publish/logo.png
fi

# ── Render one <li> for a deploy directory, with metadata when available ────
render_entry() {
  local dir="$1" href="$2" label_extra="${3:-}"
  local slug ref short sha message author deployed_at
  slug=$(basename "$dir")

  if [ -f "$dir/meta.json" ]; then
    ref=$(jq -r '.ref // ""'             "$dir/meta.json")
    short=$(jq -r '.commit.short // ""'  "$dir/meta.json")
    sha=$(jq -r '.commit.sha // ""'      "$dir/meta.json")
    message=$(jq -r '.commit.message // ""' "$dir/meta.json")
    author=$(jq -r '.commit.author // ""'   "$dir/meta.json")
    deployed_at=$(jq -r '.deployed_at // ""' "$dir/meta.json")
  else
    ref=""; short=""; sha=""; message=""; author=""; deployed_at=""
  fi

  local label="$slug"
  [ -n "$label_extra" ] && label+=" $label_extra"

  # Build the meta line. Each piece is emitted only when we have it.
  local meta=""
  if [ -n "$short" ]; then
    meta+="<span class=\"meta-chip\" title=\"${sha}\">${short}</span>"
  fi
  if [ -n "$deployed_at" ]; then
    meta+="<time class=\"meta-time\" datetime=\"${deployed_at}\" data-relative>${deployed_at}</time>"
  fi
  if [ -n "$author" ]; then
    local safe_author
    safe_author=$(printf '%s' "$author" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
    meta+="<span class=\"meta-author\">${safe_author}</span>"
  fi
  if [ -n "$message" ]; then
    local safe_message
    safe_message=$(printf '%s' "$message" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
    meta+="<span class=\"meta-message\" title=\"${safe_message}\">${safe_message}</span>"
  fi
  [ -z "$meta" ] && meta='<span class="meta-empty">No deploy metadata yet</span>'

  printf '                    <li>\n'
  printf '                        <a href="%s">%s</a>\n' "$href" "$label"
  printf '                        <div class="meta">%s</div>\n' "$meta"
  printf '                    </li>\n'
}

# ── Page header (unchanged design) ──────────────────────────────────────────
cat > $OUTPUT_FILE << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EvilFlowers Catalog - API Documentation</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            line-height: 1.6;
            color: #333;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }

        .header {
            text-align: center;
            margin-bottom: 3rem;
            background: rgba(255, 255, 255, 0.95);
            padding: 2rem;
            border-radius: 15px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
        }

        .logo {
            width: 120px;
            height: 120px;
            margin: 0 auto 1rem;
            border-radius: 50%;
            object-fit: cover;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }

        .header h1 {
            font-size: 2.5rem;
            color: #2c3e50;
            margin-bottom: 0.5rem;
            font-weight: 700;
        }

        .header p {
            font-size: 1.2rem;
            color: #7f8c8d;
            margin-bottom: 1rem;
        }

        .badge {
            display: inline-block;
            background: #3498db;
            color: white;
            padding: 0.3rem 0.8rem;
            border-radius: 20px;
            font-size: 0.9rem;
            font-weight: 600;
        }

        .docs-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 2rem;
            margin-top: 2rem;
        }

        .docs-section {
            background: rgba(255, 255, 255, 0.95);
            padding: 2rem;
            border-radius: 15px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }

        .docs-section:hover {
            transform: translateY(-5px);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
        }

        .docs-section h2 {
            color: #2c3e50;
            margin-bottom: 1rem;
            font-size: 1.5rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .docs-section h2::before {
            content: "📖";
            font-size: 1.2rem;
        }

        .docs-section.releases h2::before {
            content: "🚀";
        }

        .docs-list {
            list-style: none;
        }

        .docs-list li {
            margin-bottom: 0.8rem;
        }

        .docs-list a {
            display: block;
            color: #3498db;
            text-decoration: none;
            padding: 0.8rem 1rem;
            border-radius: 8px;
            transition: all 0.3s ease;
            border: 2px solid transparent;
            position: relative;
            overflow: hidden;
        }

        .docs-list a:hover {
            background: linear-gradient(45deg, #3498db, #2980b9);
            color: white;
            transform: translateX(5px);
            box-shadow: 0 4px 15px rgba(52, 152, 219, 0.3);
        }

        .docs-list a::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
            transition: left 0.5s;
        }

        .docs-list a:hover::before {
            left: 100%;
        }

        .meta {
            margin-top: 0.35rem;
            padding: 0 1rem;
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem 0.9rem;
            align-items: center;
            font-size: 0.82rem;
            color: #6b7280;
        }

        .meta-chip {
            font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
            font-size: 0.78rem;
            padding: 0.1rem 0.45rem;
            border-radius: 4px;
            background: rgba(52, 152, 219, 0.10);
            color: #2563eb;
            border: 1px solid rgba(52, 152, 219, 0.18);
            white-space: nowrap;
        }

        .meta-time {
            color: #475569;
            white-space: nowrap;
        }

        .meta-author {
            color: #6b7280;
        }

        .meta-author::before {
            content: "by ";
            color: #9ca3af;
        }

        .meta-message {
            flex: 1 1 100%;
            color: #4b5563;
            font-style: italic;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .meta-empty {
            color: #9ca3af;
            font-style: italic;
            font-size: 0.78rem;
        }

        .empty-state {
            text-align: center;
            color: #7f8c8d;
            font-style: italic;
            padding: 2rem;
        }

        .footer {
            margin-top: 3rem;
            text-align: center;
            color: rgba(255, 255, 255, 0.8);
            font-size: 0.9rem;
        }

        .footer a {
            color: rgba(255, 255, 255, 0.9);
            text-decoration: none;
        }

        .footer a:hover {
            color: white;
        }

        @media (max-width: 768px) {
            .container {
                padding: 1rem;
            }

            .header h1 {
                font-size: 2rem;
            }

            .docs-grid {
                grid-template-columns: 1fr;
            }

            .logo {
                width: 80px;
                height: 80px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <img src="logo.png" alt="EvilFlowers Logo" class="logo">
            <h1>EvilFlowers Catalog</h1>
            <p>OPDS 1.2 Compatible Digital Catalog Server</p>
            <span class="badge">API Documentation</span>
        </div>

        <div class="docs-grid">
EOF

# ── Development branches ────────────────────────────────────────────────────
if [ -d "publish/branch" ] && compgen -G "publish/branch/*/" >/dev/null; then
    {
        echo '            <div class="docs-section">'
        echo '                <h2>Development Branches</h2>'
        echo '                <ul class="docs-list">'
    } >> $OUTPUT_FILE

    # Sort: develop first, then by deploy time desc (entries without meta sort last).
    # shellcheck disable=SC2207
    branch_dirs=($(
        for dir in publish/branch/*/; do
            [ -d "$dir" ] || continue
            slug=$(basename "$dir")
            ts=""
            [ -f "$dir/meta.json" ] && ts=$(jq -r '.deployed_at // ""' "$dir/meta.json")
            # Sort key: 0 for develop, 1 otherwise; then inverted timestamp.
            if [ "$slug" = "develop" ]; then
                printf '0\t%s\t%s\n' "$ts" "$dir"
            else
                printf '1\t%s\t%s\n' "$ts" "$dir"
            fi
        done | sort -t $'\t' -k1,1 -k2,2r | cut -f3
    ))

    for dir in "${branch_dirs[@]}"; do
        slug=$(basename "$dir")
        extra=""
        [ "$slug" = "develop" ] && extra='<small style="color:#6b7280">(main development)</small>'
        href="/EvilFlowersCatalog/branch/${slug}/"
        render_entry "$dir" "$href" "$extra" >> $OUTPUT_FILE
    done

    {
        echo '                </ul>'
        echo '            </div>'
    } >> $OUTPUT_FILE
else
    {
        echo '            <div class="docs-section">'
        echo '                <h2>Development Branches</h2>'
        echo '                <div class="empty-state">No development branches available</div>'
        echo '            </div>'
    } >> $OUTPUT_FILE
fi

# ── Releases ────────────────────────────────────────────────────────────────
if [ -d "publish/release" ] && compgen -G "publish/release/*/" >/dev/null; then
    {
        echo '            <div class="docs-section releases">'
        echo '                <h2>Release Versions</h2>'
        echo '                <ul class="docs-list">'
    } >> $OUTPUT_FILE

    # shellcheck disable=SC2207
    release_dirs=($(
        for dir in publish/release/*/; do
            [ -d "$dir" ] || continue
            printf '%s\n' "$(basename "$dir")"
        done | sort -V -r | while read -r slug; do printf 'publish/release/%s/\n' "$slug"; done
    ))

    for dir in "${release_dirs[@]}"; do
        slug=$(basename "$dir")
        href="/EvilFlowersCatalog/release/${slug}/"
        render_entry "$dir" "$href" "" >> $OUTPUT_FILE
    done

    {
        echo '                </ul>'
        echo '            </div>'
    } >> $OUTPUT_FILE
else
    {
        echo '            <div class="docs-section releases">'
        echo '                <h2>Release Versions</h2>'
        echo '                <div class="empty-state">No releases available</div>'
        echo '            </div>'
    } >> $OUTPUT_FILE
fi

# ── Footer + relative-time script ───────────────────────────────────────────
cat >> $OUTPUT_FILE << 'EOF'
        </div>

        <div class="footer">
            <p>Generated automatically from <a href="https://github.com/EvilFlowersCatalog/EvilFlowersCatalog">EvilFlowers Catalog</a></p>
            <p>For technical support, visit our <a href="https://github.com/EvilFlowersCatalog/EvilFlowersCatalog/issues">GitHub Issues</a></p>
        </div>
    </div>

    <script>
      // Render deploy timestamps as friendly relative times ("2h ago").
      (function () {
        var formatter = (typeof Intl !== "undefined" && Intl.RelativeTimeFormat)
          ? new Intl.RelativeTimeFormat(navigator.language || "en", { numeric: "auto" })
          : null;
        var units = [
          ["year",   60 * 60 * 24 * 365],
          ["month",  60 * 60 * 24 * 30],
          ["week",   60 * 60 * 24 * 7],
          ["day",    60 * 60 * 24],
          ["hour",   60 * 60],
          ["minute", 60],
          ["second", 1]
        ];
        function relative(date) {
          var diff = (date.getTime() - Date.now()) / 1000;
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
EOF

echo "Generated $OUTPUT_FILE"
