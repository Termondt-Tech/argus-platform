#!/usr/bin/env bash
#
# Render every diagram in `docs/architecture/` to SVG and PDF, for reading at
# full size and for printing.
#
# WHY THIS EXISTS. The architecture docs are Markdown with embedded Mermaid, which
# is right for the repo — they diff, they review, and they live next to the code
# they describe. It is the wrong format for two jobs: following a 14-participant
# sequence diagram line by line, and putting one on paper. Both want a vector file
# at its natural size, which is what this produces.
#
#   ./scripts/render-architecture.sh              # everything, SVG + PDF
#   ./scripts/render-architecture.sh 04           # just the docs matching "04"
#   ARGUS_ARCH_FORMATS=svg ./scripts/render-architecture.sh
#
# Output lands in `build/architecture/` (gitignored), one SVG and one PDF per
# diagram, plus an `index.html` that links them all — open that and click through
# rather than hunting for filenames. The `.mmd` source of each diagram is kept
# beside them, which is what you paste into https://mermaid.live to experiment.
#
# PRINTING: use the PDFs. They are rendered with `--pdfFit`, so each page is sized
# to its own diagram rather than shrunk onto Letter — print "actual size" onto
# 11x17 / A3 for the big sequence diagrams, and landscape Letter is fine for the
# rest.
#
# THIS IS A DEVELOPER TOOL AND TOUCHES NOTHING THE TRADING PROCESS READS. No
# database, no config, no container, no network beyond `npx` fetching the renderer
# on first run. `scripts/` is inert to the deploy classifier (v0.3.20), so adding
# or changing this file forces no restart and no release.
#
# Overridable via env:
#   ARGUS_ARCH_DOCS     (default docs/architecture — the directory to scan)
#   ARGUS_ARCH_OUT      (default build/architecture — where renders land)
#   ARGUS_ARCH_FORMATS  (default "svg pdf" — either alone is fine)
#   ARGUS_ARCH_MMDC     (default @mermaid-js/mermaid-cli@11 — the pinned renderer;
#                        the MAJOR is pinned deliberately, since Mermaid's parser
#                        and config keys move between majors)
#   ARGUS_ARCH_BG       (default white — SVG/PDF background)
#   PUPPETEER_CONFIG    (optional path to a puppeteer config JSON; on Linux hosts
#                        that refuse Chrome's sandbox this is where
#                        {"args":["--no-sandbox"]} goes)
set -euo pipefail

DOCS_DIR="${ARGUS_ARCH_DOCS:-docs/architecture}"
OUT_DIR="${ARGUS_ARCH_OUT:-build/architecture}"
FORMATS="${ARGUS_ARCH_FORMATS:-svg pdf}"
MMDC_PKG="${ARGUS_ARCH_MMDC:-@mermaid-js/mermaid-cli@11}"
BG="${ARGUS_ARCH_BG:-white}"
FILTER="${1:-}"

# Run from the repo root whatever the caller's cwd is, so the relative defaults
# above mean what they say.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v npx >/dev/null 2>&1; then
    echo "render-architecture: npx not found. Install Node (brew install node)." >&2
    echo "  The renderer itself needs no install — npx fetches ${MMDC_PKG}." >&2
    exit 1
fi
if [[ ! -d "$DOCS_DIR" ]]; then
    echo "render-architecture: $DOCS_DIR does not exist." >&2
    exit 1
fi

mmdc() { npx -y -p "$MMDC_PKG" mmdc "$@"; }

# Puppeteer config is passed only when one is configured, so the common (macOS)
# path keeps the renderer's own defaults. Expanded below with the
# `${arr[@]+"${arr[@]}"}` idiom, because bash 3.2 — which is what macOS ships —
# treats an empty array as unbound under `set -u`.
PUPPET_ARGS=()
[[ -n "${PUPPETEER_CONFIG:-}" ]] && PUPPET_ARGS=(-p "$PUPPETEER_CONFIG")

mkdir -p "$OUT_DIR"

# THE CLEAN IS PER DOC, NOT PER RUN, and that is the whole reason this loop is
# shaped the way it is. Rendered output is derived and a stale file from a deleted
# diagram is worse than a slow rebuild — but wiping $OUT_DIR up front means a
# FILTERED run deletes every diagram it is not about to re-render, which is
# exactly what a filter is for avoiding. So each doc clears only its own files,
# just before it rewrites them, and the index is rebuilt at the end from whatever
# is actually on disk.
clean_doc() {
    rm -f "$OUT_DIR/$1"-[0-9]*.mmd "$OUT_DIR/$1"-[0-9]*.svg "$OUT_DIR/$1"-[0-9]*.pdf
}

# The heading a diagram sits under: the last "## ..." before its fence. Cosmetic,
# and worth it — "05-uml-state-4" says nothing on its own.
heading_of() {  # heading_of <doc.md> <block index>
    awk -v want="$2" '
        /^#+ / { h = $0 }
        /^```mermaid[[:space:]]*$/ { n++; if (n == want) { sub(/^#+ /, "", h); print h; exit } }
    ' "$1"
}

rendered=0
for doc in "$DOCS_DIR"/*.md; do
    name="$(basename "$doc" .md)"
    [[ -n "$FILTER" && "$name" != *"$FILTER"* ]] && continue

    clean_doc "$name"

    # Split the Markdown into one .mmd per fenced mermaid block. awk rather than a
    # Markdown parser on purpose: the fence is the whole grammar here, and the
    # init directive on the first line of a block must survive verbatim.
    awk -v out="$OUT_DIR" -v base="$name" '
        /^```mermaid[[:space:]]*$/ { n++; f = sprintf("%s/%s-%d.mmd", out, base, n); inblock = 1; next }
        inblock && /^```[[:space:]]*$/ { close(f); inblock = 0; next }
        inblock { print > f }
    ' "$doc"

    shopt -s nullglob
    blocks=("$OUT_DIR/$name"-*.mmd)
    shopt -u nullglob
    [[ ${#blocks[@]} -eq 0 ]] && continue

    for mmd in "${blocks[@]}"; do
        stem="${mmd%.mmd}"
        label="$(basename "$stem")"
        heading="$(heading_of "$doc" "${label##*-}")"

        echo "  ${label}${heading:+ — $heading}"
        for fmt in $FORMATS; do
            case "$fmt" in
                svg) mmdc ${PUPPET_ARGS[@]+"${PUPPET_ARGS[@]}"} -i "$mmd" -o "${stem}.svg" -b "$BG" >/dev/null ;;
                pdf) mmdc ${PUPPET_ARGS[@]+"${PUPPET_ARGS[@]}"} -i "$mmd" -o "${stem}.pdf" -b "$BG" --pdfFit >/dev/null ;;
                *)   echo "render-architecture: unknown format '$fmt'" >&2; exit 1 ;;
            esac
        done
        rendered=$((rendered + 1))
    done
done

# THE INDEX IS BUILT LAST, FROM DISK. Iterating every doc — not just the filtered
# ones — and listing the diagrams whose files are actually present means a
# filtered run leaves a complete index covering what an earlier run rendered,
# instead of an index that silently forgets it.
INDEX="$OUT_DIR/index.html"
cat > "$INDEX" <<'HTML'
<!doctype html>
<meta charset="utf-8">
<title>Argus architecture diagrams</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; }
  h2 { margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: .3rem; }
  li { margin: .35rem 0; }
  a.pdf, a.src { font-size: .85em; color: #666; margin-left: .5rem; }
</style>
<h1>Argus architecture diagrams</h1>
<p>Rendered from <code>docs/architecture/</code>. SVG opens in the browser and zooms
without blurring (Cmd-+ / Ctrl-+). PDF is sized per diagram for printing.</p>
HTML

listed=0
for doc in "$DOCS_DIR"/*.md; do
    name="$(basename "$doc" .md)"
    count="$(grep -c '^```mermaid[[:space:]]*$' "$doc" || true)"
    [[ "$count" -eq 0 ]] && continue

    entries=""
    for ((i = 1; i <= count; i++)); do
        label="$name-$i"
        # Present on disk, in either format — a doc rendered SVG-only still lists.
        [[ -f "$OUT_DIR/$label.svg" || -f "$OUT_DIR/$label.pdf" ]] || continue
        heading="$(heading_of "$doc" "$i")"
        link="$label.svg"
        [[ -f "$OUT_DIR/$label.svg" ]] || link="$label.pdf"
        entries+="  <li><a href=\"$link\">${heading:-$label}</a>"
        [[ -f "$OUT_DIR/$label.pdf" ]] && entries+="<a class=\"pdf\" href=\"$label.pdf\">PDF</a>"
        entries+="<a class=\"src\" href=\"$label.mmd\">source</a></li>"$'\n'
        listed=$((listed + 1))
    done
    [[ -z "$entries" ]] && continue

    title="$(sed -n 's/^# //p' "$doc" | head -1)"
    printf '<h2>%s <small>(%s.md)</small></h2>\n<ul>\n%s</ul>\n' \
        "${title:-$name}" "$name" "$entries" >> "$INDEX"
done

echo
echo "rendered $rendered diagram(s) into $OUT_DIR ($listed on the index)"
if [[ -n "$FILTER" && "$listed" -gt "$rendered" ]]; then
    echo "  (filtered run — the index also lists $((listed - rendered)) diagram(s) from an earlier one)"
fi
echo "open $OUT_DIR/index.html"
