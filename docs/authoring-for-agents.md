# Authoring with revl for agents

If you are building an agent that writes revl, this is the loop. It exists
because a harness team spent weeks reinventing generate-whole, refuse,
regenerate before discovering that `revl scaffold`, the fill-spec enrichment
inside `revl_check`, `revl fmt`, and `revl explain` already do this job. Each
is documented on its own compiler-surface page
([scaffold.md](scaffold.md), [holes.md](holes.md) §8, item 31/32 in
[v2.0-roadmap.md](v2.0-roadmap.md)), but nothing tied the four together from
the authoring side. This does.

If you only read one other doc, read [guide-ai-agents.md](guide-ai-agents.md):
it is the full workflow guide, including the MCP verb table and the
`revl_check` -> `revl_edit` -> `revl_resolve` -> `revl_admit` loop for a
**running** composition. This page is narrower, naming the five verbs an
authoring agent reaches for before anything is live, with the CLI form that
exists today and the MCP form that is coming.

## The loop

```
scaffold  ->  fillSpec  ->  fmt  ->  explain  ->  admit
```

1. **scaffold.** Generate a typed, holed skeleton from a small spec, instead
   of hand-writing a plausible whole component. Every not-yet-known
   expression is a `hole[T]`, so it compiles as a draft, and admission
   refuses it until every hole is filled ([scaffold.md](scaffold.md)).

   ```
   revl scaffold --service Analysis --requires filesystem \
     --provides analysis --capabilities filesystem.read --out csv_analyzer.rvl
   ```

   `--json` returns the skeleton and its obligations, each hole already
   carrying its fill spec (next step), in one call, so an agent driving the
   CLI as a subprocess does not need a second round trip to `revl_check`.

2. **fillSpec.** For every open hole, `revl_check` (and `revl scaffold
   --json`) enrich the obligation with everything the checker already knew
   standing at that position: the expected type, the emission upper bound
   (may this fill cross a boundary, and within which named bound), the
   bindings in scope, and the reachable services with full signatures
   ([holes.md](holes.md) §8, `fillspec.enrich` in
   `src/revl/mcp/fillspec.py`). Fill one hole against its spec, re-check,
   repeat. This is the step that turns generate-whole/refuse/regenerate into
   scaffold/fill/fill: most wrong answers become unrepresentable before they
   are written.

3. **fmt.** Canonicalize the source once the holes are filled.

   ```
   revl fmt csv_analyzer.rvl
   ```

   IR-equivalence gated: `fmt` never changes what the file means, only its
   layout. `--check` proves a file is already canonical without writing (a
   CI gate); `--migrate` rewrites 1.x `$name` interpolation to backtick
   templates instead of formatting.

4. **explain.** When a check comes back with a diagnostic code instead of a
   clean compile, don't guess.

   ```
   revl explain G4
   ```

   turns any code back into the guarantee it enforces and the rewrite that
   satisfies it, the same text the structured diagnostic's `hint` field
   already carries, addressable on its own when an agent only kept the code.

5. **admit.** The loop closes against a real gate, not a lint pass. Working
   against a standalone file, `revl compile` is the check and `revl run`
   boots it. Working against a **live** composition, the case this loop is
   built for, admission is a session concept: `revl_admit` answers "may this
   enter this running composition?" and `revl_gauntlet` upgrades that answer
   to a graded dossier (admission plus a no-residue lifecycle test, in an
   isolated scratch session). Only what passes gets written out or swapped
   in. See the MCP loop in
   [guide-ai-agents.md](guide-ai-agents.md#driving-a-running-system-mcp) for
   the full sequence past this point.

## Verb reference

| step | CLI (today) | MCP (today) | MCP (planned, item 345) |
|---|---|---|---|
| scaffold | `revl scaffold` | none | `revl_scaffold` |
| fillSpec | folded into scaffold `--json` | `revl_check` (enriches every open hole) | already covered by `revl_check` |
| fmt | `revl fmt` | none | `revl_fmt` |
| explain | `revl explain <code>` | the `hint` field on every diagnostic | `revl_explain` |
| admit | `revl compile`, `revl run` | `revl_admit`, `revl_gauntlet` | already covered |

**Today, `scaffold`/`fmt`/`explain` exist only as CLI subcommands**; the MCP
surface stops at check/admit/plan/ship/swap/edit. An agent driving revl purely
over MCP has to shell out to the CLI for those three steps, or reimplement
scaffold-then-fill by hand-writing holed drafts, which is exactly the gap
that caused the weeks of rediscovery this doc exists to close. Exposing them
as `revl_scaffold` / `revl_fmt` / `revl_explain` (scaffold returning the
skeleton and its fill specs in one call, mirroring what `--json` already does
over the CLI) is tracked as item 345 in [v2.0-roadmap.md](v2.0-roadmap.md).
Once that lands, the MCP column above becomes the primary surface for an
MCP-native harness; the CLI forms keep working identically for anything
driving revl as a subprocess.

## See also

- [scaffold.md](scaffold.md), `revl scaffold` flags, the `--json` shape, and
  the conservative-by-construction guarantees (never grants authority the
  spec did not ask for)
- [holes.md](holes.md), the `hole[T]` construct, obligations, and §8's fill
  spec fields in full
- [guide-ai-agents.md](guide-ai-agents.md), the complete agent workflow,
  including the MCP verb table and the live-composition loop this page hands
  off to at "admit"
- [commands-reference.md](commands-reference.md), every `revl` subcommand
  with its flags; [mcp-reference.md](mcp-reference.md), every MCP verb with
  its inputs and outputs
