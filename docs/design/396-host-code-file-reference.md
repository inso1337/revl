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
  MODULE and the emitter generates a thunk that imports a symbol from it at
  the extern's first call, binding the extern name to that symbol. This
  wraps existing code as-is; the price is a deploy-time dependency the
  artifact did not previously declare.

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

A third invariant is affected by BOTH options and must be named here rather
than discovered by a red pipeline: the bundle round-trip. `revl bundle`
copies the compiled sources into the bundle FLAT, keyed by basename
(`src/revl/bundle.py:316-320`), and `revl verify` recompiles the bundled
source and byte-compares IR and emitted artifacts (`bundle.py:23-31`).
Neither an A body file nor a B ref file survives that flat copy, so a
bundled program using either form fails `verify` on a missing file. Teaching
bundle to carry body and ref files under their root-relative paths (and
verify to re-hash them against the recorded content hashes) is a stage of
this item, not an afterthought; until that stage lands, bundling a program
that uses either new form must be a clean refusal naming the gap, never a
bundle that cannot verify.

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
containment check on the resolved file closes the symlink escape the textual
rule alone would leave open. Containment is specified precisely, because a
string-prefix check is not a jail: both operands are canonicalized first
(`os.path.realpath`, then case normalization where the filesystem is
case-insensitive) and the check is `os.path.commonpath` /
`Path.is_relative_to` over the canonical paths, never `str.startswith`,
which conflates `/foo` with `/foobar`. One residual is accepted and stated
rather than claimed away: a HARDLINK inside the module tree to a file
outside it realpaths to the inside name and passes. Creating that hardlink
requires write access inside the tree being compiled, at which point the
same author could paste the same bytes into an inline body; the jail's
threat model is reference REACH, not an author who already writes the tree.
Deliberately NOT the item-319 `use` machinery
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
the same jail. Two rules make that seam sound, and both are part of this
design, not implementation detail:

- Ordering, for untrusted authors. The 329 profile's no-extern cut refuses
  the entire extern declaration structurally (`admit_profile.py:79`), but it
  runs today AFTER the whole module graph loads (`_enforce_source` at
  `compiler.py:402`, over the loaded root modules), while body-file
  resolution would run DURING load at the seam above. In that order an
  untrusted-authored module gets its body files resolved, stat'd, and read
  before the no-extern refusal fires, and the missing-file refusal below
  leaks a file-existence oracle to an author who is forbidden externs
  outright. So the per-module no-extern check moves to immediately after
  parse, inside the load seam and BEFORE any body-file resolution: under a
  no-extern profile, no extern body file is ever resolved, read, or even
  stat'd, and the refusal an untrusted author sees is identical whether or
  not the named file exists. The whole-graph enforcement at
  `compiler.py:402` stays as the backstop.
- The loaderless and in-memory paths. `compile_source` with no manifest and
  no modules never builds a `_ModuleLoader` at all (`compiler.py:213-225`),
  and a virtual root module has no real directory, so "the declaring file's
  directory" would silently mean the process cwd. Specified instead: a body
  file referenced from a virtual (in-memory) module resolves ONLY through
  the `sources` map, keyed by the joined virtual path, with NO disk
  fallback, preserving the documented `compile_source` contract that
  nothing is read from the disk (`compiler.py:199`). The loaderless fast
  path either grows the same map-only resolution or routes a
  body-file-using source through `compile_files`; it never opens a file.

A missing file is a compile-time refusal naming the resolved path (subject
to the ordering rule above). The disk read itself is
open-then-fstat-then-validate: open once, validate containment against the
opened handle's real identity, read from that same handle, so the checked
file and the spliced file cannot be swapped between check and use. After the
read, the node is replaced by an ordinary `HostBody(backend, text, line)`
plus provenance, and NOTHING downstream changes: the lower dedup, the IR
bodies dict, every emitter, and audit all see exactly the inline shape. The
IR entry gains one additive key recording both the provenance path and a
content hash of the exact bytes spliced,
`"body_files": {"py": {"path": "engine_run_body.py", "sha256": "..."}}`,
present only when the file form is used, so every existing program's IR is
byte-identical, audit can print where a body came from, and two compiles of
the same tree are byte-comparable; without the hash, "this artifact came
from the reviewed file" is unfalsifiable.

The jail protects the trusted case too: a TRUSTED compile over a partially
reviewed tree must not be able to embed `/etc/passwd`, or anything outside
the declaring module's own directory, into an artifact. (`use` today is
unjailed, but what `use` pulls is revl source that gets parsed and checked;
a spliced file is uncompiled bytes copied into the output, which warrants
the tighter rule.)

### Emit, all six tiers

No emitter change anywhere. The splice is pure compile-time text
substitution, so `@wasm file "run.wat-frag"` works exactly as an inline
`@wasm` body does: py/ts/go/rust/java/wasm all keep their current extern
paths, and both self-containment properties hold. This is the option that
keeps all-tier parity trivially. (Rust even has this natively as
`include!`, which is a decent sanity check that the mechanism is a known
shape, not an invention.)

One normalization is part of the splice's definition, because "byte-identical
to inline" is otherwise only true on a third of the emitters. Inline body
text reaches the emitters carrying the indentation it had inside the `.rvl`
braces; the py and ts emitters dedent it (`textwrap.dedent`,
`backends/python/emit.py:2204`, `backends/typescript/emit.py:2931`), but go,
rust and java only strip the ends (`backends/go/emit.py:4276`,
`backends/rust/emit.py:4628`, `backends/java/emit.py:1891`), so on those
tiers interior indentation lands verbatim. A file-sourced body is therefore
normalized ONCE, at the compile-time splice seam, with the same
`textwrap.dedent(text.strip("\n"))` shape the py/ts emitters apply, before
the `HostBody` is built. Consequences, stated exactly: on py and ts, where
the emitters' own dedent is idempotent over the pre-normalized text, the
file-vs-inline equivalence is byte-exact on the emitted artifact. On the
strip-only tiers the file form is deterministic (the normalized bytes land
verbatim) but equals an inline twin only when that twin was authored
flush-left inside its braces. The splice-equivalence exit test is scoped to
py and ts and says so; claiming a six-tier byte identity the strip-only
emitters do not implement would be the kind of quiet overclaim this note
exists to avoid.

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

## Option B: a checked host import, emitted as a lazy thunk

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
file path. The compiler checks that the file EXISTS (jail below) and records
a content hash of its bytes into the IR; it does not read or parse host code
AS code, so whether the symbol exists, whether its arity matches the
declared parameters, and whether its colour matches a declared `async` are
claims the author vouches for, on the same trust surface as the
classification (G8; `329`). The failure modes are host-native and land at
the extern's FIRST CALL, inside the extern frame where every call-time
mechanism already sits: a wrong module or symbol is an `ImportError` raised
by the thunk (below), not a failure at artifact load and not a crash deep
inside unrelated code. What the hash buys, and what it cannot, is the
subject of the deploy contract section.

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
- The IR ref entry is hermetic and PINNED: symbol, root-relative path, and
  the content hash of the file the compiler checked, e.g.
  `"refs": {"py": {"symbol": "engine_run", "path": "src/host/engine.py",
  "sha256": "..."}}`, additive next to `"bodies"`, and a backend key may
  appear in `bodies` or `refs` but never both (extending the duplicate-body
  refusal at `lower.py:2271-2275`). Containment uses the same canonical
  `commonpath`-over-realpath rule as A's jail, same hardlink residual, same
  statement of it.

And the limit of this jail, stated plainly rather than implied away: it
constrains AUTHORSHIP, not deployment. The compiler checks a FILE at compile
time; the artifact imports a dotted NAME at run time; that name is resolved
by the host import machinery of whatever process loads the artifact, against
whatever module tree is deployed there. Between compile and deploy the
checked file can be edited, swapped, shadowed by an earlier `sys.path`
entry, or absent entirely, and no compile-time check reaches across that
boundary (a TOCTOU with the deploy boundary in the middle). The recorded
content hash is what makes the boundary CHECKABLE: a driver or embedder can
resolve the module it is actually about to import, hash the file behind it,
and refuse a mismatch before any host code runs. The py run driver does
exactly that (deploy contract below). An embedder that skips the check gets
authorship discipline only, and this note says so instead of letting the
word "jail" imply a deploy-time guarantee the mechanism cannot give.

### The tier gate

"Import a source symbol from a checked file" is native on py and ts only:
both have a first-class notion of a module AT a path, so an import specifier
can be derived from the file the compiler checked. On go, rust and java the
primitive this design leans on DOES NOT EXIST, and an earlier draft's
"representable, each with its own deploy story" understated that. Go imports
PACKAGES by import path, resolved by the module system; a file is not an
importable unit. A rust `use` names an item inside the crate's module tree,
and no `use` can name a file outside that tree; files enter the tree through
`mod` declarations and crate layout, not paths. Java imports classes
resolved via the CLASSPATH, where a source file is not addressable at all.
"Derive the import from the checked file path" is therefore not a template
those tiers can instantiate; each needs its own design, spelled in its own
ecosystem's terms (a go module requirement, a rust `mod`/crate arrangement,
a java build-and-classpath story), with its own deploy contract and its own
design note. They are UNSOLVED here, not pending; a future implementer must
not treat the py/ts mechanism as a porting guide. Wasm is further still: a
`@wasm` extern lowers to an internal `(func $name ...)` (`wasm/emit.py:36`);
wasm's native analog of B is a module IMPORT satisfied by the EMBEDDER at
instantiation, a runtime-linking mechanism with no file path in it, and
designing that embedder wiring is a different feature, out of scope here.
Follow the item-395 precedent exactly: an `_EXTERN_REF_TIERS` set next to
`_CONFIG_INJECTION_TIERS` (`lower.py:548`), fixed at `{"py", "ts"}` by this
design, and a compile-time refusal in `_lower_externs` naming the offending
tier with a redirect hint, mirroring the config tier gate
(`lower.py:2254-2262`). A `= @wasm ref ...` or `= @go ref ...` is refused at
compile, never a broken artifact; a tier joins the set only behind its own
design note, not by analogy.

### Emit, py: a lazy import thunk, never a top-level import

The obvious emission is a module-top `from src.host.engine import
engine_run`. An earlier draft of this note specified exactly that, and it is
UNSOUND, for a reason worth spelling out because it shapes the whole
emission. The py run driver `exec`s the emitted artifact as one module
(`run.py:562-573`); a top-level import statement RUNS THE REFERENCED
MODULE'S TOP LEVEL during that exec, at artifact LOAD: before any component
is plugged, before plug-time config exists, and even if the extern is never
called. Every call-time property the extern system has built is scoped to
CALLS: classification is reviewed against what a call does, `requires
approval` is consumed per crossing (item 246), witnessed frames wrap calls.
A load-time import is host code executing OUTSIDE all of it, triggered by
merely loading an artifact that mentions the extern; that is a new execution
point the language has never had, and no spelling of B may introduce it.

So B does not emit an import. It emits a THUNK, and host execution stays
where an inline body's execution is: at the first call, inside the extern
frame.

```python
def engine_run(argv_json, env_json, cwd, timeout_s, sandbox_mode):
    _f = _REVL_REFS.get("engine_run")
    if _f is None:
        from src.host.engine import engine_run as _f
        if _inspect.iscoroutinefunction(_f):
            raise TypeError(
                "revl extern `engine_run` is declared sync but "
                "src.host.engine.engine_run is a coroutine function; "
                "declare the extern `async` or ref a sync symbol")
        _REVL_REFS["engine_run"] = _f
    return _f(argv_json, env_json, cwd, timeout_s, sandbox_mode)
```

`_REVL_REFS` is one emitted module-level dict, defined only when the IR
carries a ref; `_inspect` is an emitted stdlib `import inspect` at module
top (stdlib only; no user code runs at load). The import statement executes
inside the function on first call and the resolved symbol is cached; python
itself caches the module in `sys.modules`, so every call after the first
costs one dict get. Aliasing falls out of the `as` clause when the symbol
name differs from the extern name. The specifier is derived from the
root-relative path (`src/host/engine.py` becomes `src.host.engine`); a path
component that is not a valid identifier on the tier is refused at compile.
Call sites are untouched: they call `engine_run(...)` by name exactly as
against today's emitted `def`.

The async form is the SAME shape, uniform rather than special-cased: an
`async def` thunk whose last line is `return await _f(...)`, with the
coroutine assertion inverted (a declared-async ref REQUIRES
`inspect.iscoroutinefunction(_f)`).

That mirrored assertion closes a silent miscompile a bare import could never
catch. Call sites decide whether to await by NAME membership alone
(`_PY_ASYNC_EXTERNS`, `backends/python/emit.py:158`, consulted at `:196` and
`:1910`); the emitter never inspects the callee. With a bare import, a
symbol declared `async` that is really a plain `def` at least fails loudly
(awaiting a non-awaitable raises). But a symbol declared SYNC that is really
an `async def` returns an UN-AWAITED COROUTINE that flows onward as a value:
nothing raises, the host operation never runs, and the program is silently
wrong. That is the item-380 severity class, the one the roadmap ranks above
everything else (silent-wrong beats loud-wrong). The compiler cannot check
colour without reading host code, which G8 forbids; the thunk converts the
unverifiable claim into a host-native assertion at first call, the earliest
gate this design owns. The wrapper also carries the declared colour ITSELF
(`def` vs `async def`), so the name-keyed await sites stay correct no matter
what the referenced symbol turns out to be.

One extern feature does not carry over: a `config` extern binds
`_revl_config` as the first line of the emitted body
(`backends/python/emit.py:2198-2203`) for the BODY TEXT to read, and a ref
has no author-written body text; binding `_revl_config` inside the emitted
thunk would bind a name the referenced host symbol cannot see. `config` plus
`ref` is refused at compile with a redirect to an inline body that does the
forwarding explicitly; lifting that restriction (e.g. passing config as a
call-site argument) is out of scope.

### Emit, ts: the same thunk, and a resolution story run_ts can implement

A module-top `import { engineRun as engine_run } from "./src/host/engine"`
fails the same soundness bar twice over. ESM imports hoist and execute at
module evaluation, so it is the same load-time execution point the py
section refuses. And it cannot even RESOLVE: `revl.run_ts` emits the
artifact into `backends/typescript/_gen/` inside the revl install
(`src/revl/run_ts.py:160-166`) precisely so the artifact's `../runtime.ts`
import resolves, and from that directory a project-relative
`./src/host/engine` specifier points at nothing. On top of that, node's
native type stripping (>= 23.6, the bridge's floor for running `.ts`)
resolves only EXTENSION-FUL ESM specifiers, so the extensionless spelling
above would be refused by node even if the path existed. An earlier draft
sketched exactly that import and an exit test no implementation could pass.

Specified instead, in three parts:

- Specifiers are extension-ful, always: the recorded root-relative path
  keeps its real extension (`./src/host/engine.ts`), and an extensionless
  ref path is refused at compile on the ts tier.
- The emission is a lazy thunk, as on py. A declared-async ref caches a
  dynamic import: `_f = (await import(_revl_ref_url("src/host/engine.ts"))).engineRun`,
  asserted callable, then `return await _f(...)`. A declared-SYNC ref has no
  synchronous dynamic import in ESM; its thunk goes through
  `node:module`'s `createRequire`, which since node's require(esm) support
  can load a synchronous ESM graph. A referenced module whose graph
  contains top-level await fails that require host-natively at first call;
  that is a real limitation of sync refs on ts, stated here, not papered
  over. The colour assertion mirrors py's where the tier allows: a
  declared-sync ref whose resolved symbol is an async function
  (`_f.constructor.name === "AsyncFunction"`) is refused at first call; the
  async direction cannot be asserted structurally on ts (an ordinary
  function may legitimately return a promise), so it stays awaited-by-name,
  which is loud-wrong at worst, never silent.
- Resolution is the DRIVER's job, so the artifact text stays
  machine-independent. The emitted helper `_revl_ref_url` joins a root
  handed to the process at run time (the runner reads it from the spec file
  `run_ts` already writes next to the artifact, `run_ts.py:172-174`, and
  exports it to the module; an out-of-tree embedder sets the same knob)
  with the recorded relative path, into an absolute `file://` URL at call
  time. The alternative, emitting the artifact into the project tree so
  relative specifiers resolve naturally, is rejected: it breaks the
  `../runtime.ts` placement contract that `_gen` exists to satisfy, and it
  writes generated files into the user's tree as a side effect of `run`.
  The runner performs the same hash-refusal as the py driver before the
  first import: hash the file at the joined path, compare to the IR's
  recorded hash, refuse a mismatch naming both.

Same aliasing, same call-site transparency; the ts async story is already
name-keyed like py's, and the thunk carries the declared colour itself.

### The deploy contract (the invariant break, named and bounded)

Today the artifact is the whole program on its tier; with a `ref` it is not,
and the obligation lands on every embedder, not just on disk layout. The py
run driver is the sharpest case: it `exec`s the emitted string into a
synthetic module with no package context (`run.py:562-573`), so the thunk's
`from src.host.engine import ...` resolves against the DRIVER's `sys.path`,
which does not contain the project. The contract is therefore explicit:

- The import root is the root compile file's directory.
- The py run driver APPENDS that root to `sys.path`, and only when the IR
  carries refs; a ref-free program's driver behaviour is byte-identical.
  APPENDS, not prepends, deliberately. Prepending the project root would
  let any project file SHADOW every module the runtime itself trusts, for
  the whole process: a project-level `revl_fs_workspace.py` would hijack
  the `stdlib/fs` import-thunk's reach into
  `backends/python/revl_fs_workspace.py`, escalating "can add a ref" into
  "can replace trusted host code". Appending means a ref can never shadow
  anything already importable. The mirror rule is also stated: a dotted
  name already importable in the embedder wins over the project's ref'd
  file, silently, on path position alone. The hash check is the detector
  for exactly that case.
- The py run driver verifies before it executes: at plug, for each ref, it
  resolves the module spec the interpreter would actually import (a
  find-spec, no execution), hashes the file behind it, and REFUSES a
  mismatch against the IR's recorded hash, naming both the expected and
  the resolved path. The compile/deploy TOCTOU becomes a load-time refusal
  in the driver's own vocabulary instead of a silent substitution. What
  the check cannot see: a missing SYMBOL inside a matching module still
  surfaces as the thunk's `ImportError` at first call.
- The ts bridge resolves at call time through the runner-provided root and
  performs the same hash refusal (previous section).
- An out-of-tree embedder must ship the referenced files alongside the
  artifact, root its import path the same way, and SHOULD run the same
  hash check; the IR gives it everything it needs (path, hash). `revl
  audit` printing every ref (below) makes the closure ENUMERABLE, which is
  the hook a build step needs; actually carrying the files is a
  build-tooling story and out of scope for the language item, except for
  `revl bundle`, which is this item's stage 4.

The honest framing cuts both ways. Yes, B breaks "one artifact, no project
dependencies", and this note says so rather than hiding it in a build
appendix. And also: that invariant is already body-deep, breached invisibly
by the import-thunk idiom the stdlib itself uses (`stdlib/fs.rvl:105`,
`backends/python/revl_fs_workspace.py`). B converts a buried, unjailed,
unauditable runtime dependency into a declared, jailed, hash-pinned,
audit-printed one. A team that wants the strict invariant back can grep the
IR for `refs` and refuse it in CI; today they cannot even find the thunks.

### Colour (388) interaction

A fixed-colour extern composes as described (the thunk carries the declared
colour; call sites await by name). Two concrete points for whoever lands
388's eager-expand-and-prune, both easy to miss:

- 388's clone synthesis deep-copies `bodies` into each concrete clone. A
  ref lives in the SEPARATE `refs` IR key, so a synthesis that copies only
  `bodies` silently DROPS the ref, and the clone fails at emit with a
  missing-body error pointing nowhere near the cause. `refs` needs its own
  deep-copy in the synthesis, and the bodies-xor-refs collision rule must
  hold per clone, not just per declaration.
- One host symbol cannot be both awaitable and plainly callable, so the two
  clones cannot share one thunk. The composition is a per-colour thunk
  PAIR around the cached symbol (sync clone `def`, async clone `async
  def`, each with its own colour assertion), which is a NEW emit form,
  small but real, not a reuse of the fixed-colour emission.

Defer both until 388's implementation lands; nothing in B's staging blocks
it.

### Effect on the copy-paste problem

This is the option that closes the ask. `engine.py` stays a normal host
module: importable, formattable, type-checked, unit-tested by host tooling,
one source of truth, zero restructuring. The `.rvl` holds what revl actually
owns: the signature, the classification, the capability scope, the reach
clause. The 184-line paste never happens.

### The honest hard parts (B)

1. The deploy contract above. Real, permanent, and every embedder's
   problem; the content hash makes it CHECKABLE, but only the drivers this
   item ships are obligated to check it.
2. The lazy thunk trades load-time failure for call-time failure: a
   composition whose ref'd module is missing a SYMBOL boots green and fails
   at the extern's first call. The driver's plug-time find-spec-plus-hash
   catches the missing or substituted MODULE without executing it; the
   symbol claim stays first-call. That is the price of keeping host
   execution out of artifact load, and it is the right trade: a late loud
   error beats an early silent execution point.
3. G8 widens by one notch: today the author vouches for body TEXT the
   document at least contains; with a ref the author vouches for a symbol
   the compiler never sees at all. Signature, colour, and existence of the
   symbol are unverifiable at compile; only file existence is checked.
4. The review surface thins. The audited IR document no longer contains the
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
5. Cross-tier drift, same as A and same as today: `engine.py` and
   `engine.ts` lockstep by review, not by mechanism.

## Recommendation: build A first; B only as redesigned here

Adopt BOTH, as 388 adopted its (a) and (b); they answer different halves of
the shape mismatch. Reach for them by case:

- Wrapping an existing host module (the ask as stated, and the harness's
  measured pain): option B, on py and ts only, behind `_EXTERN_REF_TIERS`.
  The big externs live on py/ts; the mechanism is the declared spelling of
  the import-thunk idiom already in production in the stdlib.
- A program that must stay one self-contained artifact per tier, or that
  reaches wasm, go, rust, or java: inline bodies or option A. A is the only
  file mechanism that works on all six tiers, because it is pure
  compile-time text.
- A large revl-AUTHORED body crowding its `.rvl`: option A.
- Text shared BETWEEN externs (the seatbelt): item 373's fragment, with A as
  its file-sourced sibling; both are splices into body text and should share
  one splice seam (see reconciliation).

Build A FIRST. The first draft of this note said the opposite, on the sizing
"one import line, one driver `sys.path` line"; the adversarial review that
found the load-time execution point showed that was the sizing of the
UNSOUND version. The order flips with the finding:

- A's soundness is entirely within the compiler's own boundary: pure
  compile-time text substitution, both self-containment properties intact,
  a jail (as fixed above) that no embedder can weaken because no embedder
  participates. Its honest scope is also now explicit: byte-exact
  file-vs-inline equivalence on py and ts, deterministic normalized splice
  on the strip-only tiers, all six tiers working.
- B is buildable ONLY in the shape this revision specifies: the lazy thunk
  (no host execution at artifact load), the mirrored colour assertions (no
  item-380-class silent miscompile), the content hash pinned in the IR plus
  the driver-side refusal (a deploy story that can be VERIFIED, not just
  believed), the appended-not-prepended `sys.path` rule (no shadowing
  escalation over trusted runtime modules), and the ts call-time resolution
  contract that `run_ts` can actually implement. That is several stages of
  real surface, none skippable: each one closes a finding, and shipping B
  without any one of them ships the corresponding hole.
- A alone still leaves the headline ask unclosed (a team with an existing
  `engine.py` gains nothing from reshaping it into body-shaped text), so B
  follows, not never. But an unclosed ask is a gap; an unsound B in
  production would have been an incident.

Co-schedule A with 373 where possible, so the repo grows ONE splice
mechanism, not two.

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
- Stage 2 (compiler resolution and the jail; A becomes usable here).
  Resolve in `_ModuleLoader.load` right after parse (`compiler.py:124-126`),
  through the in-memory `sources` seam (`compiler.py:59-80`), with every
  rule the jail section fixed: the per-module no-extern check runs after
  parse and BEFORE any body-file resolution (today's whole-graph
  enforcement at `compiler.py:402` stays as backstop); virtual modules
  resolve body files through the `sources` map only, no disk fallback, and
  the loaderless `compile_source` path (`compiler.py:213-225`) never opens
  a file; containment is canonical `commonpath`-over-realpath, never
  string prefix; disk reads are open-then-fstat-then-validate; A's splice
  applies the canonical dedent normalization and records path plus content
  hash; B records existence plus content hash under the root-tree jail.
  Neither consults `REVL_IMPORT_PATH`. Exit: the jail, ordering,
  reproducibility, and missing-file refusals below; an in-memory compile
  with an in-memory body file succeeds.
- Stage 3 (lower). A flows through the existing bodies dict unchanged plus
  the additive `body_files` key; B lands the additive `refs` key (symbol,
  path, sha256), the backend-collision refusal (extending
  `lower.py:2271-2275`), the `config`-plus-ref refusal, and the
  `_EXTERN_REF_TIERS` gate ({"py","ts"}) with a config-gate-style message
  (`lower.py:2254-2262` as the template). Exit: IR shapes; the
  wasm/go/rust/java ref refusal names the tier and redirects.
- Stage 4 (bundle and verify; completes A end-to-end). `revl bundle`
  carries body and ref files under their root-relative paths instead of
  losing them to the flat basename copy (`bundle.py:316-320`); `revl
  verify` re-hashes them against the recorded content hashes as part of
  its recompile-and-compare. Until this stage lands, bundling a program
  using either form is a clean refusal naming the gap. Exit: bundle a
  program using each form; verify passes; a modified body/ref file inside
  the bundle fails verify naming the file.
- Stage 5 (emit py thunk, plus the driver). Thunk emission with caching,
  aliasing, and the mirrored colour assertions; `_emit_module` APPENDS the
  root to `sys.path` only when refs are present, and performs the
  plug-time find-spec-plus-hash refusal (`run.py:562-573` is the seam).
  Exit: the wrap-existing py test below, including its no-load-execution
  sentinel; `revl run` round-trips a composition whose extern is a ref
  into a real, separately pytest-tested host module.
- Stage 6 (emit ts thunk, plus the bridge). The lazy thunk (dynamic import
  for async, `createRequire` for sync), extension-ful specifier refusal,
  the `_revl_ref_url` root plumbing through the runner spec
  (`run_ts.py:172-174`), and the runner-side hash refusal. Exit: ts golden
  shows the thunk with the extension-ful relative specifier; the bridge
  `--once` boots and tears down clean.
- Stage 7 (audit and docs). Audit prints file provenance and
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
  IR, and every backend golden, and `revl run` of a ref-free program
  touches `sys.path` not at all.
- Splice equivalence, scoped honestly: `= @py { X }` and `= @py file
  "f.py"` where `f.py` contains exactly X produce byte-identical py
  output; same on ts. On go/rust/java the file form emits the
  dedent-normalized bytes deterministically (golden), with no
  byte-identity claim against an indented inline twin. This pins A as pure
  text substitution where the emitters dedent, and pins the normalization
  where they do not.
- Jail: an absolute path is refused; a `../escape.py` under option A is
  refused; an option-B ref resolving outside the root tree is refused; a
  symlink inside the module dir pointing outside is refused; a sibling
  directory sharing a name prefix with the jail root (`/foo` vs `/foobar`)
  does not pass containment; each names the rule in the message. Setting
  `REVL_IMPORT_PATH` changes nothing for either form.
- Jail ordering: under the untrusted-author profile, a module declaring
  `= @py file ...` gets the no-extern refusal, and the refusal is
  byte-identical whether or not the named file exists (no existence
  oracle); no stat of the body file occurs.
- In-memory: a virtual module's body file resolves through the `sources`
  map; a decoy file on disk at the equivalent path is never read.
- Reproducibility: compiling the same tree twice yields identical IR,
  including the recorded hashes; touching a body/ref file changes its
  recorded hash.
- Missing file: both forms refuse at compile naming the resolved path
  (trusted authors only, per the ordering rule).
- Wrap-existing, py (the headline): a real host module with a tested `def`,
  wrapped by `= @py ref`, composes and `revl run` round-trips; the module
  itself still passes its own pytest untouched, proving single-source. The
  module's top level sets a sentinel: the sentinel is UNSET after artifact
  load and composition plug, and set only after the extern's first call,
  proving no load-time execution.
- Deploy hash: editing the ref'd file between compile and `revl run` makes
  the driver refuse at plug, naming the expected and resolved paths; a
  same-dotted-name module already importable in the embedder is what the
  refusal catches (appended `sys.path` means the embedder's wins), and the
  runtime's own host modules cannot be shadowed by a project file.
- Wrap-existing, ts: the golden carries the thunk with the extension-ful
  root-relative specifier; an extensionless ref path is refused at compile
  on the ts tier; the bridge `--once` boots and tears down clean.
- The tiers that cannot: `= @wasm ref ...`, `= @go ref ...`, `= @rs ref
  ...`, and `= @java ref ...` are each refused at compile with the
  redirect hint; a program pairing `@py ref` with an inline `@wasm` body
  compiles, and each tier emits its own form.
- Colour: a declared-async ref extern is awaited at its call sites
  (golden); a declared-SYNC ref whose symbol is really `async def` raises
  the thunk's TypeError at first call, naming the extern and the symbol
  (the silent un-awaited-coroutine case is impossible by construction);
  the mirrored declared-async-but-plain-def case raises likewise on py; a
  `config` extern with a ref is refused with the inline-body redirect.
- Collision: `= @py { ... }` plus `= @py ref ...` on one extern is refused
  as a duplicate `@py`.
- Audit: `revl audit` shows the ref path and symbol for a ref extern and
  file provenance for a spliced one; `audit --diff` flags an inline-to-ref
  change.
- Bundle: a program using each form bundles with the body/ref files under
  their root-relative paths and `revl verify` passes; tampering with a
  bundled body/ref file fails verify naming the file; before stage 4
  lands, bundling either form is a clean refusal.
- `test_doc_examples` stays green: every proposed-syntax block in this note
  is marked `sketch` and must not compile until the feature lands.

## The honest hard part (consolidated)

The shape mismatch is the whole item: a function-body slot cannot absorb a
module, so no single mechanism exists, and any design claiming one is hiding
either a reshaping step (A pretending to wrap) or a deploy dependency (B
pretending to be self-contained). This note takes both costs in the open. A
keeps every invariant but creates a file species host tooling cannot check
and does not close the ask. B closes the ask, and this revision closes what
the first draft of B opened: a top-level import would have run host code at
artifact LOAD, outside classification, approvals, and witnesses, so the
emission is a lazy thunk and host execution stays at first call, inside the
extern frame, with the declared colour asserted there so the un-awaited
coroutine miscompile (the item-380 severity class) cannot happen silently.
What remains genuinely traded: B swaps the self-contained artifact for a
declared dependency plus an embedder contract whose jail constrains
authorship, not deployment; the pinned content hash and the drivers'
plug-time refusal make the deploy side CHECKABLE, but only the shipped
drivers are obligated to check. B widens G8 from "the document contains text
the compiler will not read" to "the document names a symbol the compiler
never sees", and thins the 329 review surface, mitigated by making the ref a
first-class, jailed, hashed, audited declaration rather than the invisible
import-thunk the stdlib already lives with. Wasm can express neither an
import nor a file dependency; go, rust, and java lack the file-addressable
module primitive B leans on and are unsolved, not pending; all four are
refused at compile by the same tier-gate pattern the config coeffect proved
out. And cross-tier twins drift by review on every mechanism here, including
the status quo: the design reduces copies and surfaces dependencies; it does
not pretend to verify opaque host code, on any tier, in any form.
