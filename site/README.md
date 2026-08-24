# revl.dev — the site

The full front-end for revl: a landing page that shows the paradigm in motion
(hot-swap, effect/undo, the admission gate, the six-runtime fan-out, the MCP
agent loop), plus **playground v2** — the same in-browser compiler as
[`playground/`](../playground/), now with a syntax-highlighted editor, the 2.0
example set, and `?example=<id>` deep links.

Everything is static, dependency-free, and served from this directory:

```
site/
  index.html        the landing page
  playground.html   playground v2
  css/site.css      the design system (dark, teal #2dd4bf / violet #a78bfa)
  js/revl-lang.js   the revl tokenizer/highlighter (shared)
  js/landing.js     landing-page animations (no libraries)
  js/playground.js  the Pyodide driver + editor
  js/examples.js    generated — preloaded programs
  vendor/           generated — the compiler wheel
```

## Run it locally

The playground fetches a wheel and Pyodide's runtime, so serve over HTTP:

```
python3 -m http.server 8000 --directory site
# then open http://localhost:8000/
```

External fetches: Pyodide from its CDN (cached after first load) and Google
Fonts. Nothing typed into the playground leaves the page — the compiler runs
client-side.

## Regenerating the bundled assets

```
python3 site/build.py     # wheel (via playground/build_wheel.py) + examples.js
```

Only run this when `src/revl/` or the example set changes. The site touches
nothing under `src/revl/`.

## Honesty notes for edits

- The gate terminal on the landing page types **verbatim captured compiler
  output** (G3 cycle, G4 missing undo). If diagnostics change, re-capture —
  don't paraphrase.
- The agent exchange shows the real G4 JSON diagnostic shape; the `revl_admit`
  reply is a condensed illustration of a plan verdict.
- Guarantee cards mirror the G1–G8 table in `DESIGN.md`.
