# 396: extern host bodies that reference an external host-language file

Design note for roadmap item 396 (`docs/v2.0-roadmap.md:4122`). The ask, from
the harness lane: let an extern's host body refer to a host-code file path,
like a `use`/import, so EXISTING host code can be wrapped into revl without
copy-pasting its contents into the `@py { ... }` block. This is design-first.
It changes no lexer, parser, lower, or emit code; it records the shape of the
problem, the mismatch that forces two distinct mechanisms, both mechanisms
worked in full, a per-use-case recommendation, a staged plan, and exit tests
an implementation agent can pick up. It converges with items 373 (host-body
fragment) and 388 (caller-decided extern colour); the reconciliation section
maps the three onto orthogonal axes.

## The problem (measured)

Wrapping existing host code into revl today means pasting it. The harness's
`engine_run` extern carries a 184-line `@py` body (`docs/v2.0-roadmap.md:4108`,
item 388), authored by copying working host code into the block; its ~60-line
seatbelt mechanism is copy-pasted FOUR times across `engine_run` and
`engine_run_sync`, `@py` and `@ts` (`docs/v2.0-roadmap.md:4078`, item 373).
In this repo the same pressure shows at smaller scale: 253 host bodies across
the `.rvl` sources, the largest a 92-line `@ts` body in `stdlib/json.rvl` and
80- and 58-line `@ts` bodies in `stdlib/crypto.rvl`.

Every one of those lines is invisible to host tooling. A host body lexes as one
opaque token (`src/revl/lexer.py:297-318`): no formatter formats it, no linter
lints it, no host type checker sees it, no host test imports it. So the wrap
workflow is: take a working, tested host module; copy its logic into a `@py`
block, reshaping it on the way (see the shape mismatch below); lose every host
tool on the copy; and from then on maintain two divergent sources, because the
original file keeps living wherever it came from. For a small shim that cost is
fine. For a 184-line spawn mechanism or a security seatbelt it is the dominant
authoring cost and a real drift hazard, which is what the harness lane hit.

There is a second, quieter measurement that reframes the whole item. The
stdlib ALREADY wraps an external host file, by hand, through what this note
calls the import-thunk idiom: `stdlib/fs.rvl` bodies open with `import
revl_fs_workspace as _ws` (`stdlib/fs.rvl:105`, again at `:144`), reaching a
real host module that lives at `backends/python/revl_fs_workspace.py` and is
deployed alongside the runtime. Nothing in the language surfaces that
dependency: it is buried in opaque body text, `revl audit` cannot print it,
no compile-time check confirms the file exists, and resolution is whatever the
embedder's `sys.path` happens to hold. The ask is therefore not for a new
power. G8 already grants host bodies the power to import anything. The ask is
for a DECLARED, checked, reviewable spelling of a dependency that today can
only exist as an invisible one.

## The crux: a shape mismatch, and why it forces two mechanisms

An extern body is a FUNCTION BODY: the emitter writes `def NAME(params):` and
splices the body text verbatim underneath, after `textwrap.dedent(...).strip`
(`backends/python/emit.py:2182-2183` and `:2204-2207`; the ts twin at
`backends/typescript/emit.py:2931`). Body text is dedented statements that are
legal INSIDE a function: bare `return`, references to the parameters, local
`import`s.

Existing host code is a MODULE: its own top-level imports, its own `def`s, its
own module docstring and constants. The two shapes do not convert into each
other by pointing:

- A module's text spliced into a `def` turns its functions into nested
  closures and its module constants into locals; nothing calls the entry
  point; the extern returns `None`. Wrong program, silently.
- A body's text written to a standalone file is usually NOT a valid host
  module: a top-level `return` is a `SyntaxError` in a Python module, so most
  host tools refuse the file outright.

So "point the body at `engine.py`" cannot be one mechanism. The mismatch
splits the ask into two, which this note designs together, the way the 388
note carried its options (a) and (b):

- Option A, a compile-time file SPLICE: the referenced file holds BODY-SHAPED
  text and is read at compile time, entering the pipeline exactly where an
  inline `@py { ... }` body does. It DRYs a body out of the `.rvl`; it does
  not wrap a module as-is.
- Option B, an emitted host IMPORT: the referenced file is a normal host
  MODULE and the emitter generates a real import binding the extern name to a
  symbol in it. This wraps existing code as-is; the price is a deploy-time
  dependency the artifact did not previously declare.

Neither subsumes the other. A cannot consume a module; B cannot keep the
artifact self-contained.

## Background: how a host body flows today

- Lexer. `@backend { ... }` is consumed as one brace-balanced, opaque scan,
  string- and comment-aware per backend (`_HOST_TRIVIA`,
  `src/revl/lexer.py:311`), producing a single `Token("hostbody", (backend,
  body), line)` (`lexer.py:316`). Load-bearing for the new surface: when `@py`
  is NOT followed by `{`, the lexer emits a plain `@` token and re-lexes the
  backend word as an ordinary identifier (`lexer.py:319-321`), so both new
  spellings below parse with ZERO lexer change.
- Parser. `extern_decl` (`src/revl/parser.py:1124`) captures each `= @backend
  { ... }` as one `HostBody(backend, text, line)` (`parser.py:341-345`,
  capture loop `:1239-1244`) onto `ExternDecl.bodies` (`parser.py:356`); an
  extern with no body is refused (`parser.py:1245-1246`).
- Lower. `_lower_externs` (`src/revl/lower.py:1936`) collapses the list into a
  per-backend dict, refusing a duplicate `@backend` (`lower.py:2177-2182`),
  and stores the text verbatim on the IR entry (`"bodies": bodies`,
  `lower.py:2188`).
- Emit. Each tier requires its own body only when reached, and writes it
  verbatim as the function body: py refuses a missing `@py` body
  (`emit.py:2172-2177`) and splices at `:2204-2207`; ts at
  `backends/typescript/emit.py:2920` and `:2931`; wasm lowers a `@wasm` body
  to an internal `(func $name ...)` (`backends/wasm/emit.py:36`) and refuses a
  missing one at the call site (`wasm/emit.py:1043-1051`).

### The two self-containment properties (what the options trade)

1. The IR document carries the full body text (`lower.py:2188`). Everything
   downstream of parse reads the document, never the source tree: `revl
   audit` prints per-extern backends straight from the bodies keys
   (`src/revl/__main__.py:420-421`), and the item-329 admission surface
   reviews documents and parsed ASTs, not files on disk
   (`docs/design/329-untrusted-author-profile.md`, `src/revl/admit_profile.py:49`).
2. Each emitted artifact is ONE self-contained module string
   (`emit(ir) -> str`, `backends/python/emit.py:2896-2897`). The run driver
   leans on this hard: it `exec`s the string into a synthetic in-memory
   module with a made-up filename and NO package context
   (`src/revl/run.py:562-573`). There is no disk artifact at all.

Option A preserves both properties untouched. Option B breaks the second and
thins the first; the honest accounting is below. But note the deflation the
stdlib measurement forces: property 2 is already only body-deep. An artifact
using `stdlib/fs` externs already fails at runtime unless
`revl_fs_workspace` is importable. B does not introduce artifact
dependencies; it makes them declared.

## Option A: compile-time file splice

### Surface

```revl sketch
extern emission async fn engine_run(
    argv_json: Str, env_json: Str, cwd: Str, timeout_s: Int, sandbox_mode: Str
) -> Str
    = @py file "engine_run_body.py"
    = @ts file "engine_run_body.ts"
```

`file` is a CONTEXTUAL keyword recognised only in this slot, the discipline
`witnessed`/`deferred`/`confined` already use (`parser.py:1126-1129`,
`:1177-1184`, `:1252-1271`), so the lexer's keyword set is untouched. In the
parser's body loop (`parser.py:1240`), after `=`: a `hostbody` token is
today's inline path; an `@` token (the lexer's no-brace fallback,
`lexer.py:319-321`) followed by the backend identifier and `file "path"` is
the new one.

### Resolution, and the jail

The path resolves STRICTLY relative to the declaring `.rvl` file's directory.
Absolute paths are refused. `..` segments are refused as written, and a
realpath containment check (resolved file inside the declaring module's
directory tree) closes the symlink escape the textual rule alone would leave
open. Deliberately NOT the item-319 `use` machinery
(`src/revl/compiler.py:82-104`): `use` falls back to a search path
(`REVL_IMPORT_PATH`, then the stdlib root, `compiler.py:20-39` and `:94-97`),
which is right for a LIBRARY someone asks for by name and wrong here twice
over. A body file is a sibling artifact of its module, not a library to
search for; and for text that gets embedded verbatim into every emitted
artifact, letting an environment variable decide WHICH bytes get spliced is a
supply-chain seam, so the search path must never participate.

The read happens in the compiler layer, not the parser: the parser stays
IO-free and records a body-file node; `_ModuleLoader.load` resolves and reads
it right after parsing each module (`compiler.py:124-126` is the seam),
consulting the in-memory `sources` map first (`compiler.py:59-80`) so
agent-in-memory compiles can supply body files without touching disk, under
the same jail. A missing file is a compile-time refusal naming the resolved
path. After the read, the node is replaced by an ordinary
`HostBody(backend, text, line)` plus a provenance path, and NOTHING
downstream changes: the lower dedup, the IR bodies dict, every emitter, and
audit all see exactly the inline shape. The IR entry gains one additive key,
`"body_files": {"py": "engine_run_body.py"}`, present only when the file form
is used, so audit can print where a body came from and every existing
program's IR is byte-identical.

Untrusted authors: the 329 profile's no-extern cut
(`admit_profile.py:68-71`; profile doc `:27-33`) refuses the entire extern
declaration structurally, so both new forms are covered before any file IO
would run. The jail protects the other case: a TRUSTED compile over a
partially reviewed tree must not be able to embed `/etc/passwd`, or anything
outside the declaring module's own directory, into an artifact. (`use` today
is unjailed, but what `use` pulls is revl source that gets parsed and
checked; a spliced file is uncompiled bytes copied into the output, which
warrants the tighter rule.)

### Emit, all six tiers

No emitter change anywhere. The splice is pure compile-time text
substitution, so `@wasm file "run.wat-frag"` works exactly as an inline
`@wasm` body does: py/ts/go/rust/java/wasm all keep their current extern
paths, and both self-containment properties hold. This is the option that
keeps all-tier parity trivially. (Rust even has this natively as
`include!`, which is a decent sanity check that the mechanism is a known
shape, not an invention.)

### Colour (388) interaction

Composes freely. By the time colour machinery runs, the body is ordinary text
in the bodies dict; 388's eager-expand-and-prune (per its review note,
`docs/v2.0-roadmap.md:4108`) deep-copies bodies into the concrete clones, and
a file-sourced body copies like any other. A poly extern whose body is a file
reference needs nothing new.

### Effect on the copy-paste problem, honestly

A moves an inline body into a neighbouring file. That buys: the `.rvl` stays
readable next to a large body; the body is diffable and reviewable as its own
file; two externs (or two `.rvl` modules) can splice the same body file, which
is the file-sourced sibling of 373's fragment DRY. It does NOT wrap existing
code as-is, and the con is sharper than "the file must be body-shaped": a
body-shaped file is generally NOT a valid host module (top-level `return` is
a Python `SyntaxError`), so host formatters, type checkers, and tests still
cannot process it standalone. The file is a new species, host-coloured but
only compilable in situ. Wrapping a genuinely existing module via A still
requires extracting a body-shaped core or writing a thunk, at which point
option B is the honest tool.

### The honest hard parts (A)

- The body-file species. Easy to mistake for a module, unverifiable by host
  tooling, meaningful only spliced. Name the extension convention in docs
  (e.g. `engine_run_body.py`), but the compiler cannot enforce shape: the
  text stays opaque (G8, item 24).
- Cross-tier drift. An `@py` file and its `@ts` twin can silently diverge,
  exactly as inline twins can today. The mechanism reduces copies; it cannot
  force lockstep. Same trust surface as the classification itself:
  honest-by-review, stated, not solved.
- Provenance. A runtime traceback names the emitted artifact's line, exactly
  as for inline bodies (the driver keeps the emitted source for quoting,
  `run.py:566-569`); compile time never looks inside either way. No worse
  than today, and the `body_files` key lets a human map back. Do not inject a
  splice-origin comment line into the emitted body: it would break the
  file-vs-inline byte-identity equivalence that makes A trivially safe to
  verify.

## Option B: emit a real host import

### Surface

```revl sketch
extern emission async fn engine_run(
    argv_json: Str, env_json: Str, cwd: Str, timeout_s: Int, sandbox_mode: Str
) -> Str
    = @py ref engine_run from "host/engine.py"
    = @ts ref engineRun from "host/engine.ts"
```

`ref` and the following `from` are contextual in the same slot. The symbol is
a host identifier in the referenced module; the path is a source-relative
file path. The compiler checks the file EXISTS (jail below) and nothing more:
it does not read or parse host code, so whether the symbol exists, whether
its arity matches the declared parameters, and whether its colour matches a
declared `async` are all claims the author vouches for, on the same trust
surface as the classification (G8; `329`). The failure modes are at least
host-native and early: a wrong module or symbol is an `ImportError` when the
artifact loads, not deep inside a call.

### Resolution and the jail (different root than A, deliberately)

The written path resolves relative to the declaring `.rvl` file, for
authoring locality. But B's file must ALSO be reachable at deploy time
through one import root, so the jail is the ROOT tree: the resolved realpath
must sit inside the root compile file's directory tree, and the emitted
import specifier is derived from the resolved path RELATIVE TO THAT ROOT.
`..` segments in the written path are allowed (a `src/plugins/x.rvl` may ref
`../host/engine.py`) precisely because the root-tree containment check is
what holds. Consequences, stated plainly:

- A module resolved from OUTSIDE the root tree (the stdlib via the item-319
  search path, or a `REVL_IMPORT_PATH` entry) may not declare a `ref`:
  refused at compile with a redirect to an inline body or `file` splice.
  The alternative (IR entries carrying per-ref absolute roots) would put
  machine-specific paths into the IR document and is rejected; a multi-root
  import story can be its own later item if the stdlib ever needs one.
- The IR ref entry is hermetic: symbol plus root-relative path, e.g.
  `"refs": {"py": {"symbol": "engine_run", "path": "src/host/engine.py"}}`,
  additive next to `"bodies"`, and a backend key may appear in `bodies` or
  `refs` but never both (extending the duplicate-body refusal at
  `lower.py:2179-2181`).

### The tier gate

"Import a source symbol" is native on py and ts and representable on go, rust
and java, each with its own deploy story (go imports packages, not files;
java resolves via classpath; rust via `mod`). It is NOT representable on
wasm: a `@wasm` extern lowers to an internal `(func $name ...)`
(`wasm/emit.py:36`); wasm's native analog of B is a module IMPORT satisfied
by the EMBEDDER at instantiation, a runtime-linking mechanism with no file
path in it, and designing that embedder wiring is a different feature, out of
scope here. Follow the item-395 precedent exactly: an `_EXTERN_REF_TIERS`
set next to `_CONFIG_INJECTION_TIERS` (`lower.py:547`), starting `{"py",
"ts"}`, and a compile-time refusal in `_lower_externs` naming the offending
tier with a redirect hint, mirroring the config tier gate at
`lower.py:2151-2176`. A `= @wasm ref ...` is refused at compile, never a
broken artifact; go/rust/java join the set when someone builds their seam.

### Emit, py

The externs section emits an import instead of a `def`:

```python
from src.host.engine import engine_run
```

with aliasing when the symbol name differs (`from src.host.engine import run
as engine_run`), so every call site is untouched: calls resolve by name
exactly as they do against today's emitted `def`. The specifier is derived
from the root-relative path (`src/host/engine.py` becomes `src.host.engine`);
a path component that is not a valid identifier on the tier is refused at
compile. An `async` extern needs no wrapper: call sites await by NAME
membership (`_PY_ASYNC_EXTERNS`, `emit.py:2926`), so an imported coroutine
function is awaited exactly as an emitted `async def` is; that the symbol
really is a coroutine function is part of the declared-colour claim.

One extern feature does not carry over: a `config` extern binds
`_revl_config` as the first line of the emitted body (`emit.py:2202-2203`),
and an imported symbol has no emitted body to bind into. `config` plus `ref`
is refused at compile with a redirect to a thunk body; lifting that
restriction (e.g. passing config as a call-site argument) is out of scope.

### Emit, ts

```ts
import { engineRun as engine_run } from "./src/host/engine";
```

at module top (ESM imports hoist). Same aliasing, same call-site
transparency, and the ts async story is already name-keyed like py's.

### The deploy contract (the invariant break, named)

Today the artifact is the whole program on its tier; with a `ref` it is not,
and the obligation lands on every embedder, not just on disk layout. The py
run driver is the sharpest case: it `exec`s the emitted string into a
synthetic module with no package context (`run.py:562-573`), so `from
src.host.engine import ...` resolves against the DRIVER's `sys.path`, which
does not contain the project. The contract is therefore explicit:

- The import root is the root compile file's directory.
- The py run driver puts that root on `sys.path` for the exec (it already
  knows the source path; a small, contained change at `_emit_module`).
- The ts bridge (`revl.run_ts`) must materialise or resolve the artifact so
  `./src/host/...` specifiers reach the project tree.
- An out-of-tree embedder must ship the referenced files alongside the
  artifact and root its import path the same way. `revl audit` printing
  every ref (below) makes the closure ENUMERABLE, which is the hook a build
  step needs; actually carrying the files is a build-tooling story and out
  of scope for the language item.

The honest framing cuts both ways. Yes, B breaks "one artifact, no project
dependencies", and this note says so rather than hiding it in a build
appendix. And also: that invariant is already body-deep, breached invisibly
by the import-thunk idiom the stdlib itself uses (`stdlib/fs.rvl:105`,
`backends/python/revl_fs_workspace.py`). B converts a buried, unjailed,
unauditable runtime dependency into a declared, jailed, existence-checked,
audit-printed one. A team that wants the strict invariant back can grep the
IR for `refs` and refuse it in CI; today they cannot even find the thunks.

### Colour (388) interaction

A fixed-colour extern composes as described (import, await by name). A poly
extern (388) whose body is a `ref` is subtler: one host symbol cannot be both
awaitable and plainly callable, so the two concrete clones cannot both be
bare imports. The composition is per-colour thunks around a colour-agnostic
blocking symbol (sync clone `def name(...): return _sym(...)`, async clone
`async def name(...): return _sym(...)`), a small emitter addition. Defer it
until 388's implementation lands; nothing in B's stage 1 blocks it.

### Effect on the copy-paste problem

This is the option that closes the ask. `engine.py` stays a normal host
module: importable, formattable, type-checked, unit-tested by host tooling,
one source of truth, zero restructuring. The `.rvl` holds what revl actually
owns: the signature, the classification, the capability scope, the reach
clause. The 184-line paste never happens.

### The honest hard parts (B)

1. The deploy contract above. Real, permanent, and every embedder's problem.
2. G8 widens by one notch: today the author vouches for body TEXT the
   document at least contains; with a ref the author vouches for a symbol
   the compiler never sees at all. Signature, colour, and existence of the
   symbol are unverifiable at compile; only file existence is checked.
3. The review surface thins. The audited IR document no longer contains the
   implementation of that crossing; a 329-style review of the document alone
   cannot read what the emission does, only where it lives. Audit printing
   `ref src/host/engine.py#engine_run` on the extern line (extending
   `__main__.py:420-421`) keeps the surface honest, and `audit --diff`
   should flag an inline-to-ref transition the way it flags a reach
   weakening (item 373's precedent), because "the implementation moved out
   of the document" is exactly the kind of change a reviewer must see. The
   counterweight is also true: a 184-line pasted body was never genuinely
   reviewed inside a `.rvl`, and the ref'd module gets real host-tooling
   review the paste never had.
4. Cross-tier drift, same as A and same as today: `engine.py` and
   `engine.ts` lockstep by review, not by mechanism.

## Recommendation: B for wrapping, A for body DRY, per use-case

Adopt BOTH, as 388 adopted its (a) and (b); they answer different halves of
the shape mismatch. Reach for them by case:

- Wrapping an existing host module (the ask as stated, and the harness's
  measured pain): option B, on py and ts first, behind `_EXTERN_REF_TIERS`.
  The big externs live on py/ts; the mechanism is the declared spelling of
  the import-thunk idiom already in production in the stdlib.
- A program that must stay one self-contained artifact per tier, or that
  reaches wasm: inline bodies or option A. A is the only file mechanism that
  works on all six tiers, because it is pure compile-time text.
- A large revl-AUTHORED body crowding its `.rvl`: option A.
- Text shared BETWEEN externs (the seatbelt): item 373's fragment, with A as
  its file-sourced sibling; both are splices into body text and should share
  one splice seam (see reconciliation).

Build B first. It is the item's actual ask, its py stage is small (parse,
jail, gate, one import line, one driver `sys.path` line), and A alone would
leave the ask unclosed: a team with an existing `engine.py` gains nothing
from a mechanism that requires reshaping it into body-shaped text. A follows,
ideally co-scheduled with 373 so the repo grows ONE splice mechanism, not
two.

## Reconciliation with 373 and 388: three orthogonal axes

- 388 is the COLOUR axis: one body, sync and async callers, colour decided at
  the call site (`docs/design/388-caller-decided-extern-colour.md`;
  implementation direction per its review note: eager-expand-and-prune,
  `docs/v2.0-roadmap.md:4108`).
- 373 is the DRY axis for REVL-AUTHORED host text: a named fragment declared
  in revl, spliced into bodies, plus the already-landed reach clause
  (`parser.py:1252-1271`, `lower.py:2009-2031`).
- 396 is the PROVENANCE axis: where a host implementation LIVES. Inline text;
  a spliced neighbouring file (A); or an imported host module (B).

They compose rather than overlap. A body is either TEXT (inline, file-spliced
via A, fragment-including via 373; one splice engine serves the latter two)
or a REF (B), never both; fragments splice into text bodies only, since a ref
has no text to splice into. Colour composes with both: a poly extern's text
body clones by copy (trivial), a poly extern's ref clones as per-colour
thunks (deferred until 388 lands). The full stack is expressible: a poly
extern whose `@py` is a ref to a tested host module and whose `@wasm` is a
file-spliced wat fragment, each axis pulling its own weight.

## Staged implementation plan

Each stage lands independently and keeps every existing golden byte-identical
(the additivity discipline of 342/388: no new surface used means no output
changes anywhere).

- Stage 1 (parser, both spellings). In the `extern_decl` body loop
  (`parser.py:1239-1244`), accept `= @backend file "path"` and `= @backend
  ref sym from "path"` alongside the `hostbody` token, via the lexer's
  existing `@`-fallback (`lexer.py:319-321`); contextual `file`/`ref`/`from`;
  new parse nodes next to `HostBody` (`parser.py:341`). Exit: both forms
  parse; a plain extern is byte-identical; mixed inline+file+ref lists parse
  with per-backend positions preserved.
- Stage 2 (compiler resolution and the jail). Resolve in
  `_ModuleLoader.load` right after parse (`compiler.py:124-126`), through
  the in-memory `sources` seam (`compiler.py:59-80`). A: declaring-dir jail
  (no absolute, no `..`, realpath containment), read, replace with
  `HostBody` plus provenance. B: root-tree jail, existence check, carry the
  root-relative path. Neither consults `REVL_IMPORT_PATH`. Exit: the jail
  and missing-file refusals below; an in-memory compile with an in-memory
  body file succeeds.
- Stage 3 (lower). A flows through the existing bodies dict unchanged plus
  the additive `body_files` key; B lands the additive `refs` key, the
  backend-collision refusal (extending `lower.py:2179-2181`), the
  `config`-plus-ref refusal, and the `_EXTERN_REF_TIERS` gate ({"py","ts"})
  with a config-gate-style message (`lower.py:2151-2176` as the template).
  Exit: IR shapes; the wasm/go/rust/java ref refusal names the tier and
  redirects.
- Stage 4 (emit py, plus the driver). Import emission with aliasing and the
  async (coroutine) case; `_emit_module` puts the root on `sys.path`
  (`run.py:562-573`). Exit: the wrap-existing py test below; `revl run`
  round-trips a composition whose extern is a ref into a real, separately
  pytest-tested host module.
- Stage 5 (emit ts). The import statement and the `run_ts` resolution
  contract. Exit: ts golden shows the import; the bridge `--once` boots.
- Stage 6 (audit and docs). Audit prints file provenance and
  `ref path#symbol` on the extern line (`__main__.py:420-421`); `audit
  --diff` flags inline-to-ref transitions; extern docs updated; this note's
  `sketch` blocks promoted to compiled examples. Exit: `test_doc_examples`
  green throughout (blocks are `sketch` until promotion).
- Stage A-co (with 373). When 373's fragment lands, unify its splice with
  stage 2's file read behind one seam so fragment-include and file-splice
  are one mechanism with two sources. If 373 lands first, stage 2's A half
  builds on its seam instead.

## Exit tests

- Additivity: a program using neither form is byte-identical across parse,
  IR, and every backend golden.
- Splice equivalence: `= @py { X }` and `= @py file "f.py"` where `f.py`
  contains exactly X produce byte-identical py output; same on ts. This is
  the test that pins A as pure text substitution.
- Jail: an absolute path is refused; a `../escape.py` under option A is
  refused; an option-B ref resolving outside the root tree is refused; a
  symlink inside the module dir pointing outside is refused; each names the
  rule in the message. Setting `REVL_IMPORT_PATH` changes nothing for either
  form.
- Missing file: both forms refuse at compile naming the resolved path.
- Wrap-existing, py (the headline): a real host module with a tested `def`,
  wrapped by `= @py ref`, composes and `revl run` round-trips; the module
  itself still passes its own pytest untouched, proving single-source.
- Wrap-existing, ts: the golden carries the aliased import; the bridge
  `--once` boots and tears down clean.
- The tier that cannot: `= @wasm ref ...` is refused at compile with the
  redirect hint; a program pairing `@py ref` with an inline `@wasm` body
  compiles, and each tier emits its own form.
- Colour: a declared-async ref extern is awaited at its call sites (golden);
  a `config` extern with a ref is refused with the thunk redirect.
- Collision: `= @py { ... }` plus `= @py ref ...` on one extern is refused
  as a duplicate `@py`.
- Audit: `revl audit` shows the ref path and symbol for a ref extern and
  file provenance for a spliced one; `audit --diff` flags an inline-to-ref
  change.
- `test_doc_examples` stays green: every proposed-syntax block in this note
  is marked `sketch` and must not compile until the feature lands.

## The honest hard part (consolidated)

The shape mismatch is the whole item: a function-body slot cannot absorb a
module, so no single mechanism exists, and any design claiming one is hiding
either a reshaping step (A pretending to wrap) or a deploy dependency (B
pretending to be self-contained). This note takes both costs in the open. A
keeps every invariant but creates a file species host tooling cannot check
and does not close the ask. B closes the ask by trading the self-contained
artifact for a declared dependency plus an embedder contract, widens G8 from
"the document contains text the compiler will not read" to "the document
names a symbol the compiler never sees", and thins the 329 review surface,
mitigated by making the ref a first-class, jailed, audited declaration
rather than the invisible import-thunk the stdlib already lives with. Wasm
can express neither an import nor a file dependency and is refused at
compile by the same tier-gate pattern the config coeffect proved out. And
cross-tier twins drift by review on every mechanism here, including the
status quo: the design reduces copies and surfaces dependencies; it does not
pretend to verify opaque host code, on any tier, in any form.
