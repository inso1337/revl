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

## Live mode

The playground is not compile-only: **▶ Boot** activates the composition in
the browser on the real cordis-py runtime, driven through
`revl.mcp.session.Session` — the same machinery `revl mcp serve` uses. The
system graph, per-key operation invokers and the lifecycle trace render live;
**⇄ Swap** re-admits the editor's code against the running manifest (state
crosses where a `handoff` is declared, drift is refused), and **■ Unload**
replays teardown and shows the checked no-residue verdict.

Mechanics: `Session`'s sync verbs block on a private asyncio loop, which
Pyodide's WebLoop cannot do — live mode patches `Session._run` to
`pyodide.ffi.run_sync`, which needs JSPI (recent Chrome/Edge; the Boot button
disables itself elsewhere). cordis-py ships as its own wheel
(`vendor/cordis-*.whl`, built from the `backends/python/setup.sh` clone at the
tested pin), and `cordis.hmr`'s watchdog import gets an inert browser shim.
Components whose `config` fields have no default are booted with typed
placeholders, each one reported in the trace.

### Meta mode — the page is a revl composition (and boots itself)

`site/playground_shell.rvl` (the ◐ example, first in the list) is the page
itself, and on a JSPI browser it **boots automatically on load**: the editor,
toolbar, status bar and system panel are region components, each tab is owned
by a *View component — every one mounted by an `effect ui_mount…(…) undo
ui_unmount…(…)` pair — and tab clicks route through the composition's `tabs`
service. Withdraw a node (✕ on the graph) and that part of the page leaves;
withdrawing a provider cascades its dependents down with it (withdraw
`TabRouter` and every tab goes). The page's look is a **versioned
component**: `StyleV1` mounts the teal build; change both `v1`s to `v2` and
Swap to ship `StyleV2` — the graph shows the version replace, the page reskins
violet in place, and withdrawing the style unwinds to the unthemed baseline.
The fixed ◐ dock (bottom-right) is the JS
chrome's one foothold: it appears only when part of the page is withdrawn and
loads the absent components back
— the DOM changes only when the `ui_select` emission crosses the boundary, so
every click lands in the trace and the G8 audit enumerates the UI's own host
surface. Booting a source that imports `playground_host` puts the pane under
revl ownership (the JS chrome strips its tabs first and takes them back after
unload); swap an edited shell to change the UI in place, unload it to watch
the pane unbuild itself backwards, residue-free. `playground_host` is the one
bridge module the shell's externs may import — three DOM operations, forwarded
to `window.__metaHost`.

## Regenerating the bundled assets

```
python3 site/build.py     # revl + cordis wheels, examples.js
```

The cordis wheel needs the `backends/python/setup.sh` clone (or `CORDIS_PY`
pointing at one).

Only run this when `src/revl/` or the example set changes. The site touches
nothing under `src/revl/`.

## Honesty notes for edits

- The gate terminal on the landing page types **verbatim captured compiler
  output** (G3 cycle, G4 missing undo). If diagnostics change, re-capture —
  don't paraphrase.
- The agent exchange shows the real G4 JSON diagnostic shape; the `revl_admit`
  reply is a condensed illustration of a plan verdict.
- Guarantee cards mirror the G1–G8 table in `DESIGN.md`.
