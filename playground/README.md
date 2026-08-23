# revl playground (compile-only v1)

A static, single-page web app that runs the revl compiler **entirely in your
browser**. The compiler and its checker/audit/plan producers are pure Python,
so [Pyodide](https://pyodide.org) runs them client-side: there is **no server
and no operating cost** — nothing you type ever leaves the page.

Write revl, press Run, and see the four things that make revl revl:

- **Diagnostics** — the guarantee-naming rejections. This is the star: a
  refused program shows *which* guarantee it broke (G1–G8) and, where the
  checker searched to find the violation, the **why-trace** — the dependency
  cycle, the emission-propagation chain, or the two conflicting providers.
- **G8 audit** — the enumerable boundary surface per component: what each
  requires and provides, its emissions (and how many are compensated), and any
  host code it reaches. The same table `revl audit` prints.
- **Plan** — a dry run of admission: what admitting this composition into a
  cold start would do (load order, provisions gained). `revl plan`.
- **IR** — the lowered IR document, the same JSON `revl compile` emits and
  every backend consumes.

Six programs are preloaded, four of them rejections — including
`G3 — dependency cycle`, whose why-trace is the headline: break a component and
watch the gate refuse it, with the reason.

## Run it locally

The page fetches a wheel and Pyodide's runtime, so it must be served over HTTP
(opening `index.html` from `file://` will not work). Any static server does:

```
cd playground
python3 -m http.server 8000
# then open http://localhost:8000/
```

or with Node:

```
npx serve playground
```

First load pulls Pyodide (~10 MB) from its CDN and caches it; subsequent loads
are fast. Everything else is served from this directory.

## How the compiler is loaded

revl is pure Python (`src/revl/`, a hatchling project). The playground loads it
**in-process** under Pyodide:

1. `vendor/revl-<version>-py3-none-any.whl` is a wheel built straight from the
   in-tree source by `build_wheel.py` (revl has no dependencies and no native
   code, so a wheel is just a zip with a `.dist-info` — the script builds it
   without needing pip or a build backend).
2. On load, the page installs that wheel into Pyodide with `micropip`.
3. `app.js` then calls the *same functions the CLI calls* — no shelling out, no
   re-implementation:
   - `revl.compiler.compile_source(source)` → the IR document (or raises
     `RevlError`),
   - `revl.diagnostics.report(err)` → the agent-facing diagnostic, why-trace
     and all,
   - `revl.audit_diff.audit_report(ir)` → the G8 audit,
   - `revl.plan.plan(source=…)` + `render()` → the plan.

### Regenerating the bundled assets

Both bundled files are generated from the tree and can be rebuilt any time the
source or examples change:

```
python3 playground/build_wheel.py     # -> playground/vendor/revl-<version>-...whl
python3 playground/gen_examples.py     # -> playground/examples.js
```

The playground touches nothing under `src/revl/`; it only reads the source to
build the wheel and to embed the examples.

## Scope: compile-only

This is the **compile-only** version. It checks, lowers, audits and plans — the
full static story — but it does **not execute** the composition. In-browser
**execution on the TS tier is the explicit stretch goal**, not part of v1. When
it lands, every preloaded example gains a "run it" button alongside the "see why
it was refused" one it already has.
