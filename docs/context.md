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

- Last, a fixed illustration of how a provider body is written
  (`revl.context.IDIOMS`, 509 proxy tokens): a compiled component that keeps
  a `Map` as an effect with its undo, writes provider methods as plain `fn`,
  answers with `match` as an expression, calls an emission with `emit`, and
  uses an emission's value by binding the `emit` expression.
  Its names (`Log`, `Kv`, `Example`) belong to no composition, and the pack
  says so. `--no-idioms` (MCP `idioms: false`) leaves it out. The
  rounds-to-green run below is why it is there: the declarations alone lost
  rounds because a model reading them had no provider body to copy idioms
  from.

Without `--component` the pack is every service the given files load, each
with the clause its provider's key implies, and no illustration.

`--json` (and the MCP verb) answers with the same content as rows: `header`,
`idioms` (whether the illustration is in the pack),
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
2. then the illustration comes off (`idioms off`);
3. then services collapse to `service X { /* n operation(s), collapsed */ }`,
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
no `effect`, `undo` or `provide` keyword token. The bar is about what is cut
from the sources, so it is measured with `--no-idioms`; the illustration,
which is a body by design and the same for every component, is counted
separately below.

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

The illustration is a fixed 509 tokens on every pack for a component, and on
small files it costs more than the file. With it, the same 98 components come
to 63,576 pack tokens against 92,558 raw (68.7%, median per component 1.03):
74 of the 98 are at or above 70% of their file, and the files of 500+ raw
tokens come to 48.6% (worst 1.24). Those are the reading alone.

Tokens alone do not decide it. A pack can win on tokens and still lose
rounds-to-green: a model given less context can spend rounds on what the
files would have shown it. The rounds-to-green run below is the half that
decides, and it is what the illustration came out of.

## Rounds-to-green

`bench/context_arm.py` is that run. Each of the eight tasks in
`bench/context_tasks.json` removes one component from a real composition and
asks for it back. The raw arm reads the composition without the component,
plus every other file the pack draws a declaration from; the pack arm reads
`revl context --component C --no-idioms`. Three more arms add to it:
pack-card reads `revl context --component C` as it ships, illustration
included; pack-sibling reads the declarations and one providing component
of the same composition, verbatim; pack-both reads the shipped pack and the
sibling. Every arm gets the same system prompt, task text,
header and retry loop. The reply must be the component block alone (an empty
or renamed reply would otherwise pass wherever nothing depends on the
component); it is spliced back and the whole file compiled. An unsolved cell
counts `max_iters + 1` rounds. `summary.md` gives, per task, the rounds of
each arm and its reading in tokens, then for each pack arm against raw whether
it loses rounds, its share of input tokens, and a verdict: PROMOTE only when it loses no rounds and
reads fewer tokens, HOLD otherwise, and none while a cell was cut short by an
endpoint error or usage went unreported. `--runner mock` checks the
pipeline without a model; `--runner local` with `--base-url`, `--model` and
`--api-key-env` (the NAME of the variable holding the key) runs it against a
hosted endpoint.

Measured with deepseek-chat on the 8 tasks, `--max-iters 3` (an unsolved
cell counts 4), 3 reps per cell except the 1-rep pilot, in four runs
recorded under `bench/results/`:

| run | arms | what it showed |
|---|---|---|
| `context-arm-deepseek-pilot` | raw, pack | the declarations alone lose rounds |
| `context-arm-deepseek-arms` | five | an illustration fixes it; a sibling body alone does not |
| `context-arm-deepseek-shipped-1` | pack-card, pack-both | the illustration inside the pack, first text: one task fails |
| `context-arm-deepseek-shipped-2` | pack-card, pack-both | the text that ships: promoted |

The two shipped runs reran only the card arms; their raw, pack and
pack-sibling rows are the five-arm run's. Mean rounds per task against raw,
and input tokens (system prompt and retries included) as a share of raw's:

| arm | rounds vs raw | input tokens / raw | verdict |
|---|---|---|---|
| pack (`--no-idioms`) | +1.38 | 0.88 | HOLD |
| pack-sibling | +0.88 | 0.84 | HOLD |
| pack-card, separate card (five-arm run) | -0.04 | 0.60 | PROMOTE |
| pack-card, shipped, first text | +0.29 | 0.67 | HOLD |
| pack-card, shipped (`revl context`) | -0.21 | 0.53 | PROMOTE |
| pack-both, shipped (shipped-2; five-arm run -0.08 / 0.61) | -0.08 | 0.64 | PROMOTE |

The declarations alone lost rounds on 5 of 8 tasks, and the failures were
about bodies rather than contracts: a bare `match` where a statement was
expected, a call to an emission without `emit`, an `emission fn` written in
a provider, a value-returning operation with no `return`. The raw arm
learned those from the other components in the file. One providing
component of the same composition (pack-sibling) helped less than a fixed
illustration: its failures were the same kinds, on the tasks whose sibling
did not happen to show the idiom needed.

The first shipped text failed payment-gateway in 3 of 3 reps where the
separate card had passed it in 3 of 3, though the two said the same thing.
In every rep the model wrote `emit ledger.append(order)` and then
`return ledger.append(order)`, and kept the second call through every
retry. The illustration never showed an emission's value being used, and
its one value-returning emission ended with an unrelated `return`. Binding
the `emit` expression (`let line = emit log.record(k)`) is the text that
ships. With it pack-card solved all 24 cells, took more rounds than raw on
one task (notes-http, 1.67 against 1.00) and fewer on two.

What this does not show. Three reps is a small sample, and the same text
moved one task from 3 of 3 to 0 of 3, so a difference of a few tenths of a
round is within the noise. The last fix was made after seeing one task fail,
on the same eight tasks it was then measured on. It is a general rule (G4),
not a task's answer, but the eight tasks are no longer held out from it.
One model. The bench loop hands back the error text and applies no
suggested edit.

## Where it is listed

`revl_context` is in the core tier `tools/list` advertises
(`src/revl/mcp/disclosure.py`), and it is step 2 of the authoring loop
`initialize` describes, "ground", between reuse and scaffold
(`src/revl/mcp/authoring_loop.py`). It was promoted on the run above: with
the illustration it lost no rounds and read about half the raw arm's input
tokens. It is still under the `author` topic of `revl_verbs`. A rerun that
loses rounds against raw (a new model, a changed illustration, more tasks)
is the reason to take it out of the loop again.
