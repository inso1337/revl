# Declarations-only context (`revl context`, `revl_context`)

**Status:** implemented. `src/revl/context.py`, the CLI verb in
`src/revl/cli/observe.py`, the MCP verb in `src/revl/mcp/context_tool.py`,
pinned by `tests/test_context.py`.

An agent about to write a component needs two things from the composition: the
services it may call and the services it must implement, spelled the way the
checker will hold it to them. It does not need any provider's body. Reading the
composition to find those declarations is where its grounding tokens go, and
[token-economy.md](token-economy.md) measures only what it writes, never what
it reads. `revl context` is the input side: the declarations, cut verbatim from
the source, and nothing else.

The idea is "declarations, not bodies": hand the agent the interface it
compiles against and nothing else. revl already has the declaration the pack
needs: a `service` is the whole contract a provider is held to (G4 makes it an
upper bound), and it carries no body. So the pack is a cut, not a new
artifact.

The compiler keeps the same boundary. Compiling a consumer reads the
declarations its `use` names, and a provider body in the imported module is
not checked as part of the consumer, even one that does not check. A module
given as a root is compiled whole, bodies included. The pack does not lean on
either: it is cut from the parse, and it never reads a body.

## What the pack carries

For `revl context FILES --component C`:

- C's header as written, `component C requires ... provides ...`, without its
  body.
- Every service C provides, then every service C requires, each cut verbatim
  from its declaring file: the doc block above it, every operation with its
  parameter and return types, `emission` and its capability bound, and every
  other modifier the source spells, a `pub` or `commutative` prefix included.
  Nothing is re-rendered. Tokens carry a line and no column, so each cut is
  found by its tokens: a candidate span is kept only when it lexes back to
  exactly the declaration's own tokens. Two declarations on one line, a brace
  in a comment, or a body opened on the header's line each give a span that
  lexes to something else, and it is passed over.
- Every `type` a shown service names in a signature, and every `type` those
  name in turn, cut verbatim beside that service (its `pub` and doc block
  included, and a `use` line when it lives in a module that is not a root). A
  type two services name is cut once, beside the first one that still shows its
  operations. Types resolve the way the service's own module sees them (its
  own declarations, then the names its `use` lists); the closure follows
  private types and types of other modules too, which is a superset of what
  the compiler pulls in with an imported service. A `use` line is printed only
  for a `pub` type: one reached through another type needs no import. A name no `type` declares (`Str`, `Opt`, `List`) is a builtin and
  carries nothing.
- Per service: the clause C writes (`requires writer: AppendWriter`), the
  components of the given files that provide that same declaration and under
  which key, the `use` line a root writes when the service lives in a module
  that is not a root, and a `hash`.
- A bare service name resolves the way the component's module sees it: its own
  declaration, then the module a `use "..." { Name }` names, then the other
  given files.
- A required type no `service` declares (a `Stream[T]` capability, a typo) is
  named as such rather than dropped.

Without `--component` the pack is every service the given files load, each
with the clause its provider's key implies.

`--json` (and the MCP verb) answers with the same content as rows: `header`,
`services` (`role`, `key`, `service`, `providers`, `module`, `line`, `hash`,
`clause`, `use`, `operations`, `text`, `types`: each with `name`, `module`,
`line`, `use`, `text`), `pack` (the text above), and
`tokens: {pack, raw, rawFiles}`: the pack's size beside the files it was cut
from, both counted with the proxy tokens-to-green uses.

## It grounds a component that does not check yet

The pack is built from PARSED modules (the same `_ModuleLoader` the compiler
uses, so `use` resolves the same way), never from lowered ones. A component
whose body calls a method its service does not have, or fails any other check,
still gets its pack. What stops it is whatever the module loader itself
refuses, and that comes back as the usual structured diagnostic (on the CLI
with `--json` too): a parse error, a `use` that cannot be resolved, is cyclic or
names something that is not `pub`, and the file checks the loader makes at load
time, so an `asset` (or a host-body file) that is not on disk stops the pack even
though nothing in a signature depends on it. A file copied away from the
directory its `asset` paths are relative to will not build a pack until they
resolve.

## The hash

`hash` is a sha256 prefix over the lexed tokens of the service's declaration,
followed by those of each `type` declaration the pack cuts beside it. The lexer
drops comments, so a doc edit, a trailing comment or a reflow leaves it where it
was, while a renamed operation, a changed parameter type, an edited record field
or variant case of a type the service names, or an added `emission` moves it.
A `pub` prefix on a service is left out (it changes nothing a consumer
compiles against), a `pub` on a type is kept (it decides what a `use` may name);
`commutative` is kept. A consumer that grounded on hash `h` can tell from the
hash alone whether what it compiled against moved, as far as the pack reaches:
the declarations of the services and types it shows.

## `--budget N`

The pack shrinks in a fixed order and is never truncated:

1. doc comments come off, from the last service backwards (the docs of the
   types beside a service go with it);
2. then services collapse to `service X { /* n operation(s), collapsed */ }`,
   again from the last one backwards. A collapsed service shows no operations,
   so the types beside it are not printed.

The services keep their order (provides first, then requires), so a provided
service, the one C must implement, is the last to lose its operations. Every
service stays named with its clause and its hash at every step. Each service
holds its own copy of the types beside it, so a docs step only sheds docs a
service prints itself. A step that would not make the pack shorter
(collapsing a one-operation service, say) is skipped. Each step is measured on
the pack exactly as it will be printed. When the fully collapsed pack still exceeds the budget the answer says
`fits: false` rather than cutting a name. `budget.steps` lists what was done,
in order.

## The bar, and what it measured

The bar was fixed before this change was measured:
**the pack costs under 70% of the raw files it was cut from, and carries no
body.** `tests/test_context.py::test_examples_bar_the_pack_is_under_seventy_percent_and_bodiless`
holds it over every component of every example under `examples/` (the
`rejections/` fixtures excluded), with "no body" checked on the lexed pack:
no `effect`, `undo` or `provide` keyword token.

Measured on this change, with the tokens-to-green proxy:

| set | components | pack tokens | raw tokens | pack / raw |
|---|---|---|---|---|
| every example | 98 (59 files) | 16,678 | 112,433 | 14.8% (median per component 0.18) |
| components of 500+ raw tokens | 63 | | | 11.6% (worst 0.34) |

The packs include the `type` declarations the services name (13 of the 98
components show at least one); without them the same set measures 13,832
against 96,452 (14.3%). The per-component tail is honest about small files: 21
of the 98 components have a pack at or above 70% of their file, and every one
of those files is 410 raw tokens or fewer, where the pack's header and notes
cost about what the file does. `tokens.raw` counts every file a pack was cut
from, a type's file included, even when a collapse hides the type. It is on
every answer so a caller can read the file instead when it is the cheaper
read.

What this does NOT measure: rounds-to-green with and without the pack. That
needs paid model runs on a fixed task, the way `bench/run.py` drives
iterations-to-green, and it is the half that decides whether the pack helps.
A pack can win on tokens and still lose rounds: a model given less context
can spend a round retyping an edit the compiler already suggested, so the
loop the run uses matters as much as the pack. revl's bench loop hands the
error text back and applies no suggested edit yet. Until that run exists, the
claim is the token number above and nothing more.

## Where it is listed, and what would move it

`revl_context` is NOT in the core tier `tools/list` advertises, and the
authoring loop `initialize` describes does not name it. It is reached through
`revl_verbs` under the `author` topic (`src/revl/mcp/disclosure.py`). Promotion
into the core tier and the loop waits on the rounds-to-green run above: same
task, same model, with and without the pack. Promote it only if the pack does
not lose rounds and still wins on tokens. If it loses rounds, it stays where it
is, a verb an agent can ask for.
