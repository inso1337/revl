# 3355: A component receives a fault of a service it requires as a value

Status: Proposal. Nothing in this note is implemented. It asks for a direction
(section 8) before any code changes, as issue #3355 requests.

Source read: `src/revl/parser.py` (`_header_clause`, `component`,
`remote_row_decl`), `src/revl/lower.py` (`_component_req_call`),
`src/revl/typecheck.py` (`infer_ir`), the Python tier
`backends/python/emit.py` (the `req` call rendering) and `runtime.py`,
`docs/fault-tests.md`, `docs/design/439-a2a-transport-binding.md` (D-424c.3),
`docs/design/443-estop.md`, at main `63964644a`.

## 1. The gap

A component that calls a service has no way to receive a fault of the provider's
method as a value and go on. The fault leaves the caller and the session call.

Reproduction (the program and driver of the issue, Python tier,
`revl.mcp.session.Session`), run again at the base above:

```
count before: 0
tell raised: IndexError list index out of range
count after: 1
```

`Room.tell` writes through `count.set` and then calls `game.hear`, whose
provider faults. `tell` cannot be written so that it returns after that. The
write it made before the fault stays (`count after: 1`), which is the contract
`docs/fault-tests.md` states: a provide-method error propagates to the caller
and the provider stays ACTIVE.

What exists does not cover it:

- `try e` propagates a `Result`. It does not turn a fault into one.
- `remote ... at host(...) on_failure(result)` returns a transport failure as an
  `Err` (D-424c.3). It is a clause of a composition row and covers a peer, not a
  provider in the same session.

## 2. Proposal in one paragraph

A component declares, on the requirement itself, that a fault of that service
comes back as a value:

```revl sketch
component Room requires count: Count,
                        game: Game on_failure(result)
              provides host: Host {
  provide host {
    fn tell(text) {
      emit count.set(count.get() + 1)
      let heard = emit game.hear(text)      // Result[Unit, Str]
      return match heard {
        Ok(_)  => count.get(),
        Err(_) => 0 - 1             // skip this message, the room goes on
      }
    }
  }
}
```

Every method call on `game` then has type `Result[R, Str]`, where `R` is the
method's declared return type. A fault of the provider's method body is `Err`;
a normal return is `Ok`. The component that wrote `on_failure(result)` is the
only one affected. The provider, the service declaration and every other
caller are unchanged.

## 3. Decisions

### 3.1 The clause is on the requirement, not on a composition row

The remote row can carry `on_failure(result)` because its service already
declares `Result` returns, so the clause changes no type. Here it changes the
type of the call. A type is a fact the caller's source states, and wiring must
never change what a program means (the rule `RemoteRowDecl` already cites:
remoteness is an admission fact, never a wiring fact). So the clause is written
where `carrying(...)` is written, on the requirement of the component header:

```
requires <key>: <Service> [carrying(...)] [on_failure(result)]
```

`on_failure` is a contextual keyword read only in that position, the same
discipline the remote row uses, so the lexer's keyword set does not change. The
grammar files (`grammar/revl.lark`, `.ebnf`, `.gbnf`), the self-host parser and
lowerer (which already carry `carrying`) and the formatter do change, and they
are part of slice 1 (section 4).

Only `result` is offered. The remote row's `withdraw` has no local meaning: a
local call has no peer to withdraw. The default, no clause, is today's
behaviour byte for byte.

### 3.2 The type is `Result[R, Str]`, without flattening

If the method already returns `Result[T, E]` the call type is
`Result[Result[T, E], Str]`. Flattening would make a method's own `Err(E)` and a
fault indistinguishable, which is the confusion this feature removes. A required
`Stream[T]` is not a service (a method call on it is already refused by name)
and the clause is refused on it too.

### 3.3 What a fault is

Caught: an error raised while the provider's method body runs, including one
raised by a call that body makes (index out of range, a failed arithmetic
check, an uncaught fault of a service the provider itself requires).

Not caught, still propagating exactly as today:

- the operator's verdicts: E-Stop (item 443), a session abort, cancellation of
  the calling fiber;
- refusals and breaches the runtime raises about the session rather than the
  plugin: read-only, approval, lease, and the ceilings (a ceiling breach can be
  raised in the middle of a body, but it is the operator's budget, not the
  plugin's fault);
- a transport fault of a remote provider under `on_failure(withdraw)`. It is
  provider withdrawal (D-424c.3, `run.py`), and catching it here would defeat
  that rule. A remote row that says `on_failure(result)` produces an `Err`
  value already; the caller's clause wraps it once more, so the type is
  `Result[Result[T, Str], Str]` and no `Err` is lost or merged;
- a validation fault of a `validated` emission, which the caller's own
  boundary check raises on the caller's side of the crossing, not in the
  provider.

Every one of those, and every other class in the runtime that is a
`RuntimeError`, is a different thing from "the provider's code faulted", and the
two cannot be told apart by type alone. A deny-list of names fails open the day
a gate is added. The mechanism is therefore: the runtime's verdict and gate
errors carry one marker (the way a transport fault already carries
`_revl_transport_fault`), the helper re-raises anything marked and anything that
is not an `Exception`, and a test enumerates every exception class the runtime
defines and requires it to be classified, so a new class fails that test until
someone decides. The initial classification is part of slice 1 and is listed in
its PR.

Also unchanged: a call to a provider that is not ACTIVE. That is R2, the caller
stays PENDING, and no `Err` is produced.

**The `Err` text is a fixed form, not the raw message.** It names the method
(`fault in Game.hear: <first line>`) and the first line goes through the same
secret redaction the WAL applies to a failed emission's message. A fault message
can carry a secret (a failed call that echoes its argument); handing it to
program data unredacted would let a value the runtime redacts everywhere else
flow into a `Str`. Programs branch on `Ok`/`Err`, never on the text, as with the
remote row's text `Err`.

### 3.4 State left behind: one answer, no revert

**The `Err` is handed over with the provider's state as the fault left it.**
Whatever the failed call did before it faulted stands: its effects, its
crossings, the entries it registered. Nothing is reverted before the `Err`
returns. Only the channel changes: the same fault, the same state, delivered as
a value instead of an exception. The entries stay in the provider's teardown
contract, as `docs/fault-tests.md` already says for a provide-method error.
"Stands" means stands in the session: the session's own verdict settles it
later as it does today (an emission the failed call deferred is flushed on
commit and dropped on abort, `245-session-commit.md`).

Why not revert:

- Some of what a call does cannot be undone by the call: an emission that
  already crossed the boundary (G4). A revert that is "all or nothing" cannot
  be promised, so it would have to be stated as "reverted except what cannot
  be".
- Reverting makes every such call a transaction with its own commit point, which
  is a new semantic and a new interaction with the LIFO teardown contract and
  the session commit, for a clause that is only meant to move the catch into the
  language.
- The caller already owns the repair: it sees the `Err` and compensates through
  its own effects if it needs a clean state.

**One rule for the call site.** A call that returns `Err` behaves, for
everything except control flow, as the call that faulted does today. What the
caller registered before the call is untouched. What is registered only after
the call succeeds (a `compensate` or an `effect req.m() undo` attached to the
call) is not registered, because the call did not succeed. The caller goes on
at the next statement with the `Err` bound.

Forms that do not fit one value per call are refused by name on a key that
carries the clause until slice 1 shows each is sound: a call inside a parallel
fan-out, and `emit ... with approval`. The list is settled in slice 1 and stated
in its PR.

This is what the issue asks for: one answer, stated.

### 3.5 Recorded sessions

Today the WAL records an emission that faulted as `emission-failed` with its type
and message, and reads are not recorded. Under the clause the failed emission is
recorded the same way, with the same redacted message, whether the fault
propagated or became an `Err`. The `Err` value itself is not a new record kind
and replay is not asked to reproduce it from a recording in this note.
`simulate swap` (#3260) re-runs a pure component against a stub context with no
providers, so it never reaches a provider fault and is not affected. If a later
slice needs a replayed `Err`, it specifies the recording then.

## 4. What changes

| Layer | Change |
|---|---|
| Parser | `ComponentDecl` gets `require_on_failure: dict[key, "result"]`, filled by `_header_clause` beside `require_carry`. Duplicate, wrong argument and `Stream[T]` are refusals with a hint. |
| Grammar, formatter, self-host | `grammar/revl.lark`, `.ebnf`, `.gbnf`, the formatter, and the self-host parser and lowerer learn the clause. The lexer does not change. |
| Validation (`lower.py`) | `_component_req_call` checks the clause is legal for the key (not a `Stream[T]`, not a refused form of 3.4) and puts the marker on the call node. Sites that read the method's declared return or async flag (for example the `emit`, `let` and fan-out lowerings) see the wrapped type. |
| Typing (`typecheck.py`) | `infer_ir` returns `Result[R, Str]` instead of `decl.returns` for a call node carrying the marker, so `match`, `try` and `let` see it. |
| IR | the requirement entry gains an optional `onFailure: "result"`. Absent means today, so every existing IR and golden is byte-identical. |
| Python tier | the call, including an async service op (awaited or in expression position), is rendered through one runtime helper that catches per 3.3 and returns `Ok`/`Err`. |
| Other tiers | refuse a document that uses the clause, by name, until their slice lands. |

## 5. Slices

1. Parser, grammar, formatter, self-host, validation, typing, IR field, Python
   tier, and tests: the reproduction above with `on_failure(result)` (the `Err`
   arrives and `tell` returns), the state-stands rule, the call-site rule of 3.4,
   the not-caught list including the exception-class classification test, the
   fixed redacted `Err` text, a `Stream[T]` refusal, an async provider, and
   byte-identical IR without the clause.
2. The other five tiers, each behind its own conformance case.

Slice 1 is the frontend, the self-host mirror and the Python tier, and adds one
optional IR field. Slice 2 is where the work grows, because the five other tiers
each have their own call rendering.

## 6. Alternatives considered

- A catch expression in the body (`try { ... } catch`). It adds a control-flow
  form and a second way to handle failure, while the language's way is a value.
  The clause on the requirement keeps the type honest at one declaration.
- The clause on the composition row. Rejected in 3.1: it would let wiring change
  a type.
- Reverting the provider's effects before the `Err`. Rejected in 3.4.
- Catching everything. Rejected in 3.3: it would let a plugin's caller outlive an
  operator's stop.

## 7. Not in this note

Provider-side declaration of faults, retry or backoff on `Err`, and a typed `Err`
instead of text. Each can be added later without changing a program that uses
this clause.

## 8. Questions for the owner

1. Is the spelling (the clause on the requirement, `Result[R, Str]`, no
   flattening) the direction?
2. Is the not-caught list in 3.3 right: operator verdicts, runtime gate
   refusals, ceilings, a remote withdrawal and a caller-side validation fault
   propagate, everything the provider body raises becomes `Err`?
3. Is "no revert, the state stands" (3.4) the answer to state?
