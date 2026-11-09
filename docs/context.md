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
`clause`, `use`, `operations`, `text`), `pack` (the text above), and
`tokens: {pack, raw, rawFiles}`: the pack's size beside the files it was cut
from, both counted with the proxy tokens-to-green uses.

## It grounds a component that does not check yet

The pack is built from PARSED modules (the same `_ModuleLoader` the compiler
uses, so `use` resolves the same way), never from lowered ones. A component
whose body calls a method its service does not have, or fails any other check,
still gets its pack: only a parse error stops it, and that comes back as the
usual structured diagnostic.

## The hash

`hash` is a sha256 prefix over the lexed tokens of the service's declaration.
The lexer drops comments, so a doc edit, a trailing comment or a reflow leaves
it where it was, while a renamed operation, a changed type or an added
`emission` moves it. A `pub` prefix is left out (services are pub by default,
so it changes nothing a consumer compiles against); `commutative` is kept. A
consumer that grounded on hash `h` can tell from the hash alone whether what it
compiled against moved.

## `--budget N`

The pack shrinks in a fixed order and is never truncated:

1. doc comments come off, from the last service backwards;
2. then services collapse to `service X { /* n operation(s), collapsed */ }`,
   again from the last one backwards.

The services keep their order (provides first, then requires), so a provided
service, the one C must implement, is the last to lose its operations. Every
service stays named with its clause and its hash at every step. A step that
would not make its service shorter (collapsing a one-operation service, say)
is skipped. Each step is measured on the pack exactly as it will be printed.
When the fully collapsed pack still exceeds the budget the answer says
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
| every example | 98 (59 files) | 13,694 | 92,558 | 14.8% (median per component 0.18) |
| files of 500+ raw tokens | 61 | | | 11.4% (worst 0.34) |

The per-component tail is honest about small files: 16 of the 98 components
have a pack at or above 70% of their file, and every one of those files is 303
raw tokens or fewer, where the pack's header and notes cost about what the file
does. `tokens.raw` is on every answer so a caller can read the file instead
when it is the cheaper read.

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
