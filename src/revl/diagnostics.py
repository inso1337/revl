"""Structured diagnostics — the agent-facing projection of a RevlError.

Human-facing rendering stays `file:line: message` + hint (DESIGN §9). This
module adds the machine-facing view: a stable code, the guarantee the
rejection enforces, and the expected/actual pair where one exists, so an
agent can react to a rejection without parsing prose.

Codes are derived, not hand-maintained at ~100 raise sites: a rejection
that names its guarantee in the message (the `(G4)` convention) carries it
through, and the table below classifies the rest by shape.
"""

from __future__ import annotations

import re

from .errors import RevlError
from ._paths import relpath_or_abs

# guarantee/amendment tag embedded in the message, e.g. "... (G4)"
_TAG = re.compile(r"\((G[1-9]|A[1-9]|R[1-5]|T[1-9])\)")

# what each guarantee is *about* — the one-line description an agent can
# surface without reading DESIGN.md
GUARANTEES = {
    "G1": "declared access: a component reads only what it requires",
    "G2": "provision disjointness: one provider per key (per realm)",
    "G3": "acyclic dependencies: a cycle can never activate",
    "G4": "every mutation carries an inverse, or admits irreversibility with `emit`",
    "G5": "teardown cannot register effects",
    "G6": "purity outside effect forms",
    "G7": "derived LIFO teardown",
    "G8": "the boundary surface is enumerable",
    "G9": "untrusted data cannot create authority without a declared declassification",
    "G-SECRET": "a capability-bound secret never leaves its capability's own "
                "extern bodies through any revl construct or declared crossing",
    "G-SECRET-FLOW": "a Secret[T] value never reaches a disclosure sink (a log, a "
                     "serialization, an LLM prompt, an MCP return, an unapproved "
                     "realm or an undeclared receiver); it crosses only at a "
                     "declared Secret[T] receiver and downgrades only at a "
                     "declared endorse[confidential]",
    "G-RETAIN": "a Retained[T, P] value past P's retention deadline never "
                "reaches a persistence sink (a db/fs/store/kv/blob/archive/"
                "index/cache/queue/wal crossing), unless P declares a legal "
                "hold, which overrides the deadline",
    "G-MODEL-PLACE": "a model role declared `off_device` never receives a "
                     "confidentiality origin, an action reaches only the "
                     "roles its `route model` block names, and a role reaches "
                     "no capability the component routing through it holds",
    "G-COUNCIL-SPLIT": "a model council never resolves disagreement toward "
                       "allow: its aggregation is written down, is total over "
                       "the DECLARED members, and names no value rather than "
                       "admitting when the members disagree or one of them is "
                       "silent",
    "A1": "iteration boundaries exist only during activation",
    "A2": "no acquisition after a provision",
    "A3": "host-safe identifiers",
    "A5": "compensation accompanies an emission",
    "A6": "provide-methods match the service signature",
    "A8": "mid-body failure reverts and contains (L-Raise)",
    "A9": "a provide key is declared in the component's `provides` clause",
    "T1": "declared types are checked",
    "T2": "absence is Opt[T]; `null` has no type",
    "T3": "a hole is an obligation: it checks, but it never runs (docs/holes.md)",
    "T-UNRESOLVED": "a type this compilation does not declare is refused as "
                    "unresolved, never reported as a mismatch it cannot check",
}

# how to satisfy each guarantee — the one-line rewrite `revl explain <code>`
# prints, kept beside GUARANTEES so the pair stays in step. A code with no
# entry still explains itself through GUARANTEES.
FIXES = {
    "G1": "add the key to the component's `requires` clause, or drop the access",
    "G2": "one provider per key per realm — withdraw one component, or `isolate` "
          "them into different realms",
    "G3": "break the cycle: split the interface, or move the shared state into a "
          "third component both depend on",
    "G4": "give the mutation an `undo`, or admit it is irreversible — `emit` at the "
          "call site and `emission fn` on the service operation",
    "G5": "teardown may not register new effects; acquire during activation instead",
    "G6": "outside an effect form every statement is pure — bind the value with "
          "`let`, or wrap the call in `effect ... undo ...`",
    "G7": "teardown is derived and LIFO; a `verified fn` must be total, so make "
          "every recursive call structurally smaller",
    "G8": "keep the boundary enumerable — declare host code as an `extern` with a "
          "`pure`/`acquire`/`emission` classification",
    "G9": "an untrusted value cannot directly create authority — declassify it "
          "first: parse it with a `verified fn` that returns `Trusted[T]`, endorse "
          "it at a declared point (`endorse[<origin>](v, reason = \"...\")`), or "
          "gate it on a human approval",
    "G-SECRET": "a bound provider key has no declassifier and no allowed sink "
                "except a re-emission through its own bound capability - stop "
                "reflecting it into a revl value; a `secret NAME for CAP` value is "
                "a host-scope local handed straight to CAP's provider call",
    "G-SECRET-FLOW": "a Secret[T] value crosses a boundary only where the "
                     "receiving side declares a `Secret[T]` parameter (the dual of "
                     "a `Trusted[T]` sink), and downgrades only at a declared, "
                     "audited `endorse[confidential](v, reason = \"...\")` - route "
                     "it through a declared receiver, or endorse it there",
    "G-RETAIN": "the data is past the deadline its `retention` policy declares, "
                "so it may not be written to durable storage - erase it (`revl "
                "erase-report`, and a signed receipt over what was reached), "
                "extend `until` if the retention basis really has changed, or "
                "declare the `hold` that keeps it",
    "G-MODEL-PLACE": "a model placement is a declared permission, not a hint - "
                     "route the origin to a role declared `on_device`, declare "
                     "the role the arm names, or drop the arm (`*` never covers "
                     "a confidentiality origin, so an unrouted confidential "
                     "input is not placed at all). Where the refusal is about "
                     "REACH, narrow the role's `reaches [...]` to what the "
                     "component holds, or hold what the model can reach: an "
                     "omitted clause leaves the reach undeclared, which is the "
                     "unnameable `*` and not an empty set",
    "G-COUNCIL-SPLIT": "write the aggregation down and let it name no value: a "
                       "rule from `unanimous`, `majority` or `veto`, a floor "
                       "counted over the declared members (`quorum declared`, "
                       "the default), and `on_tie split` or `on_tie deny` - "
                       "there is no admitting tie outcome, a rule that picks "
                       "one member's answer is not an aggregation, and a "
                       "member that cannot answer abstains rather than "
                       "shrinking the council into a quorum",
    "A1": "`await` is an iteration boundary and exists only during activation — "
          "move it into the component body",
    "A2": "acquire everything before the first `provide`",
    "A3": "identifiers are rewritten to host-safe names automatically; rename the "
          "source binding if the collision was deliberate",
    "A5": "an emission takes `compensate <expr>` — best-effort cleanup for what "
          "cannot be undone",
    "A6": "a provide-method must match the service declaration: name, arity, "
          "parameter types, `async`",
    "A8": "a mid-body failure reverts and contains; `fail` belongs in a component "
          "activation body",
    "A9": "the provide block's key must appear in the `provides` clause — rename "
          "the block to a declared key, or add the key (with its service) to the "
          "clause",
    "T1": "make the types agree at the call site, or change the declaration",
    "T2": "revl has no `null` — model absence as `Opt[T]` and unwrap with `??`, "
          "`?.` or `match`",
    # `hole` arrived with the typed-holes feature; this table is kept total by
    # test_explain_every_guarantee_has_a_fix, which is what caught its absence.
    "T3": "fill the hole in — a draft compiles, but it cannot be admitted into "
          "a running composition; `revl compile` lists every open obligation",
    "T-UNRESOLVED": "declare the type in this compilation, or pass the file that "
                    "declares it in the same `revl compile` invocation",
}

# The one `guarantee` value that is not an obligation: the fallback refusal
# matched no code and no message pattern, and saying so is the honest answer.
UNCLASSIFIED = "unclassified"

# Every OTHER code the compiler, the gate, the session and the MCP faces mint —
# the codes outside the guarantee catalogue above, which is what `revl explain`
# used to answer for alone (issue #2028). `GUARANTEES`/`FIXES` are deliberately
# untouched by this table: a guarantee is a named obligation of the DESIGN
# catalogue, and a code that enforces none must not claim one.
#
# This table plus `GUARANTEES` is the roster `revl explain` answers for, and it
# is closed over the emitters *except* for the reserved codes (see `ALL_CODES`
# below), which the evolution curriculum keeps a task per code for. Neither the
# closure nor the reservation is a hand-list: `revl.emitted_codes` derives both
# from the reference's own raise sites, and
# `tests/test_explain_coverage_2028.py` asserts the two sets close over each
# other, so a new `code=` site in the compiler fails that file until it has an
# entry here.
#
# `guarantee` is the obligation the refusal enforces, in one line, or `None`
# where the code records no guarantee at all — a parse error, or a verdict
# about the *run* rather than about the source. `fix` is the rewrite that
# satisfies it, or `None` where no single rewrite exists.
OTHER_CODES = {
    # ---- the runtime's verdicts
    "R1": {
        "category": "runtime",
        "guarantee": "LIFO recovery: a teardown runs the accumulated undos in "
                     "reverse order of their effects (docs/backend-ir.md)",
        "meaning": "the fault sweep found residue — a host resource acquired "
                   "during the activation and never released by its inverses, "
                   "or an inverse that raised. The runtime's own counters "
                   "returned to baseline, which is exactly why the trace is read",
        "fix": "give the acquisition an `undo` that releases it (G4), so the "
               "derived LIFO teardown reaches the host resource",
    },
    "R5": {
        "category": "runtime",
        "guarantee": "the conductor verifies the far host's signed receipts "
                     "against a key it holds (docs/design/118-revl-deploy.md)",
        "meaning": "a cross-machine deploy names no receipt-signing key, so the "
                   "conductor cannot verify the far host's signed admission and "
                   "COMMIT receipts",
        "fix": "set `[processes.<p>.deploy].host_key` to a copy of the far "
               "host's receipt-signing key",
    },

    # ---- the gate's verdicts about a run
    "COMPILER_FAULT": {
        "category": "gate",
        "guarantee": "the compiler decides every source it is given, or the run "
                     "is refused",
        "meaning": "the compiler itself raised — a revl bug, not a fault in the "
                   "author's source — so the gate refuses rather than reporting "
                   "a verdict it never reached",
        "fix": "not the author's to fix: re-run, and if it recurs the compiler "
               "fault is the bug (the message names the exception)",
    },
    "UNKNOWN_TIER": {
        "category": "gate",
        "guarantee": "a tier is one of the reference backends",
        "meaning": "`--tier` names a backend this build does not carry",
        "fix": "name one of the tiers the message lists (`py`, `ts`, `rust`, "
               "`java`, `wasm`, `go`)",
    },
    "TIER_REFUSED": {
        "category": "gate",
        "guarantee": "a tier lowers the document or says by name that it cannot",
        "meaning": "the backend said, by name, that it cannot lower this "
                   "document (issue #1406) — distinct from a compile refusal",
        "fix": "read the message: the tier names the construct it cannot lower; "
               "target a tier that carries it, or rewrite the construct",
    },
    "HALTED": {
        "category": "gate",
        "guarantee": "a halted instance dispatches nothing",
        "meaning": "the session was halted by an operator's E-Stop (item 443) "
                   "and no crossing was dispatched",
        "fix": "the instance is dead and there is no resume: reconcile with "
               "`revl recover --wal <file>`, or `unload` and start a fresh "
               "session",
    },
    "FORBIDDEN_GRANT": {
        "category": "gate",
        "guarantee": "a candidate may not grant itself a gate or session service",
        "meaning": "the granted set names a gate/session/decider service, "
                   "refused before any compile and independent of the operator",
        "fix": "drop the decider service from the granted set — authority over "
               "the gate is the operator's, never the candidate's",
    },
    "STATE_UNDISCLOSED": {
        "category": "gate",
        "guarantee": "operator state crosses to a successor only over a "
                     "declared §5 hand-off",
        "meaning": "the successor re-declares a template with live instance "
                   "state, or its `handoff` accept type is not §5-compatible "
                   "with the running provider's export — the item-53 gate the "
                   "successor's standalone compile never ran",
        "fix": "quiesce the live instances (or use a trusted operator swap), or "
               "make the successor's `handoff` accept the exported type",
    },
    "SWAP_REVERTED": {
        "category": "gate",
        "guarantee": "a reverted swap leaves generation N serving",
        "meaning": "the post-activation health gate (or a migration reject) "
                   "rolled the swap back, so nothing of generation N+1 is live",
        "fix": "read the message for the gate's own diagnostic, fix the "
               "candidate and propose again — generation N is intact",
    },

    # ---- the session's verdicts about a caller
    "APPROVAL_REFUSED": {
        "category": "session",
        "guarantee": "an operator's refusal is final for that ticket",
        "meaning": "an operator revoked the pending approval ticket for this "
                   "call, and nothing fired",
        "fix": "asking again opens a NEW question — change the request, or get "
               "the operator to approve it",
    },
    "STALE_HANDLE": {
        "category": "session",
        "guarantee": "a handle names the turn it was minted for",
        "meaning": "the handle names a turn a swap has since disposed; "
                   "dispatching it would hand the successor a call addressed to "
                   "the old turn (item 334)",
        "fix": "admit the turn again against the live generation to get a "
               "handle onto it",
    },
    "MCP-IDENTITY": {
        "category": "identity",
        "guarantee": "every answer comes from the revision the client pinned",
        "meaning": "the server's own tree is not the revision the client "
                   "pinned, so its answers are answers about a different "
                   "compiler",
        "fix": "point the client at the pinned checkout, or re-pin to the "
               "revision the message names",
    },
    "REVL": {
        "category": "unclassified",
        "guarantee": UNCLASSIFIED,
        "meaning": "the total fallback: a refusal no code and no message "
                   "pattern matched, so the message and hint are the whole "
                   "answer (docs/evolve-loop.md)",
        "fix": None,
    },
    "REVL-INTERNAL": {
        "category": "lsp",
        "guarantee": "a compiler crash is reported as a diagnostic, not as a "
                     "dead editor",
        "meaning": "the language server's own internal error, standing in for a "
                   "crash so the document still gets a diagnostic (anchored at "
                   "the start, because a crash has no line)",
        "fix": "a language-server bug, not the source's — the message names the "
               "exception",
    },
    "EFFECT_CLASS_ROSE": {
        "category": "effect-class",
        "guarantee": "an admitted change does not silently widen what an "
                     "operation can reach",
        "meaning": "the successor raised `key.method`'s effect class: more "
                   "crossings, and a wider posture than generation N",
        "fix": "read `crossings`: either the widening is intended (accept it as "
               "the recorded change) or the candidate reaches further than it "
               "should",
    },
    # ---- the HTTP face's routed error bodies
    "method": {
        "category": "http",
        "guarantee": "a route is called with the method it declares",
        "meaning": "the request used a method this face does not allow for the "
                   "path (`GET /` is the manifest; an operation is `POST`)",
        "fix": "send the method the message names",
    },
    "route": {
        "category": "http",
        "guarantee": "a face serves only the operations on its public surface",
        "meaning": "no operation is published at that path",
        "fix": "`GET /` lists the operations this face serves",
    },
    "request": {
        "category": "http",
        "guarantee": "every argument a route binds is decoded and schema-checked "
                     "before the call",
        "meaning": "a path/query/body argument failed to decode or failed its "
                   "declared schema, so nothing ran",
        "fix": "read the message — it names the parameter and the schema it "
               "missed",
    },
    "session": {
        "category": "http",
        "guarantee": "a session refusal is reported as data, not as a transport "
                     "error",
        "meaning": "the call reached the session and the session refused; the "
                   "message is the session's own diagnostic",
        "fix": "read the message — it is the refusal `revl_check`/`revl_call` "
               "would give, with its code beside it",
    },
    "authority": {
        "category": "http",
        "guarantee": "a path withheld by policy answers 403, not 404",
        "meaning": "the path exists but the face's exposure rules withhold it — "
                   "a policy decision, not a missing route",
        "fix": "read the message for the rule that withholds it; `GET /` lists "
               "what this face does publish",
    },
    "forbidden-origin": {
        "category": "http",
        "guarantee": "a listener answers only requests whose `Host`/`Origin` it "
                     "recognises (issue #1463)",
        "meaning": "the shared `http_guard` refused the request before its body "
                   "was read — a DNS-rebinding page and a browser page on "
                   "another origin both look like this",
        "fix": "call the listener by the name and origin its exposure declares",
    },
    "internal_error": {
        "category": "http",
        "guarantee": "a callee's exception is a result, not a crash",
        "meaning": "the operation raised, so the face reports a server error "
                   "and the listener stays up",
        "fix": "the exception type and message are the operation's own — fix "
               "the operation, not the transport",
    },
    "approval_refused": {
        "category": "http",
        "guarantee": "an operator's refusal runs nothing",
        "meaning": "the call was refused at the approval gate, and nothing ran",
        "fix": "sending the identical request again asks again",
    },
    "pending_approval": {
        "category": "http",
        "guarantee": "a class-(c) crossing does not fire without a human yes",
        "meaning": "the request waits on an operator's approval, and nothing "
                   "ran",
        "fix": "send the identical request again once the operator has answered "
               "— the ticket hash identifies the question",
    },
    "halted": {
        "category": "http",
        "guarantee": "a halted instance dispatches nothing",
        "meaning": "the service was halted by its operator (E-Stop) and the "
                   "request was not run",
        "fix": "reconcile with `revl recover --wal <file>`; the instance does "
               "not resume",
    },
    "ungated_emission": {
        "category": "http",
        "guarantee": "an irreversible crossing fires only where an approval "
                     "policy can hold it",
        "meaning": "the operation reaches an irreversible emission with no "
                   "checked inverse, and this server refuses such a crossing "
                   "when no approval policy can hold it",
        "fix": "bind an approval policy to the session (`revl approve`), or "
               "give the emission a `compensate`",
    },
}

# The whole roster, for a miss: the catalogue plus the emitter codes above.
#
# Deliberately NOT every code the compiler can stamp. The evolution curriculum
# (roadmap item 533, docs/design/533-evolution-curriculum.md) keeps one task per
# *reserved* code — a refusal an agent receives and cannot look up — and the
# easy rung of that curriculum IS the proof that the code is unanswerable. A row
# here would not close that gap, it would delete the task, so the reserved set
# is absent from both tables above by construction.
#
# The reserved set is derived, not listed here: `revl.emitted_codes` scans the
# reference's own raise sites (`reserved_codes()` = the codes it *refuses* with,
# minus `GUARANTEES`), which is the same rule the curriculum generator runs, and
# `tests/test_explain_coverage_2028.py` asserts the two derivations agree and
# that this roster is exactly the emitters' complement. So a `code="X"` raise
# site added tomorrow fails that file until `X` has an entry or the curriculum
# has a task — the roster and the curriculum cannot drift into disagreeing about
# what `revl explain` answers for.
#
# That scan is not run here: parsing this package's 200-odd modules costs ~2s,
# and `import revl.diagnostics` + `explain()` is 0.10s without it (measured), so
# paying it at import would tax every `revl` command 20-fold to compute a set
# the test already proves.
ALL_CODES = tuple(sorted(set(GUARANTEES) | set(OTHER_CODES)))


def _catalogue_code(name: str) -> str | None:
    """The catalogue key `name` names, matched exactly and then folded.

    The fold is what makes `revl explain g4` work. The exact match is what
    keeps `HALTED` (the gate's verdict) and `halted` (the HTTP face's body)
    apart: they are different codes that differ only in case."""
    if name in GUARANTEES or name in OTHER_CODES:
        return name
    folded = name.upper()
    for known in ALL_CODES:
        if known.upper() == folded:
            return known
    return None


# One code, several failure modes (issue #2029). A code is a *guarantee*, and a
# guarantee is often refused for more than one reason, each needing a different
# rewrite: `G1` (declared access) is raised when a `Delegate[X]` names no
# service, when a requirement key shadows a builtin, when a body reads a key the
# component does not require, and when a name is read before its declaration.
# The per-code rows above are written for one mode each — `FIXES["G1"]` is the
# *requires* rewrite — so applying them by code alone hands three of the four
# modes a remedy that contradicts their own hint (the residual of #1652).
#
# The raise site already knows its mode, so the mode is the lookup key. A code
# with no entry here keeps its per-code row; a rejection that carries its own
# `fix` still outranks both. Category is the mode name the raise sites already
# use for this purpose; `G1`'s four are `delegation` (lower.py/typecheck.py),
# `wiring` (`_refuse_builtin_requirement_key`), `requirement` (a read of a key
# the component does not require) and `binding` (a local read before its
# declaration). A mode's remedy is only reachable if its raise site sets that
# category explicitly, because `_PATTERNS` below is a message-shape fallback and
# a `(G1)` tag in a hint would otherwise land the record in `guarantee`.
FIXES_BY_CATEGORY: dict[tuple[str, str], str] = {
    # the per-code row is written for exactly this mode, so it is reused here
    # rather than spelled twice.
    ("G1", "requirement"): FIXES["G1"],
    ("G1", "binding"): "declare it with `let` (single-assignment) or `var` "
                       "(mutable), or add it as a parameter",
    ("G1", "delegation"): "name a `service` this composition declares: write "
                          "`Delegate[S]` where `S` is a `service` declaration",
    ("G1", "wiring"): "rename the key: a requirement key may not spell a builtin "
                      "type or a host root",
}

# The same split for the guarantee one-liner. `GUARANTEES[code]` is the code's
# headline — it is what `revl explain`, the LSP hover and three generated docs
# render — and it is written for the code's commonest mode. A mode whose
# headline misdescribes it says so here instead; a code/mode with no entry keeps
# the headline.
GUARANTEES_BY_CATEGORY: dict[tuple[str, str], str] = {
    ("G1", "binding"): "declared names: a body reads only the names it declares "
                       "or receives as parameters",
    ("G1", "delegation"): "declared delegation: a `Delegate[X]` names a service "
                          "this composition declares",
    ("G1", "wiring"): "unshadowed requirement keys: a requirement key may not "
                      "spell a builtin type or a host root",
}


def explain(code: str) -> dict:
    """What a diagnostic code means and how to fix it — the `revl explain`
    payload. Every code the compiler can emit is covered (issue #2028): the
    guarantee catalogue plus `OTHER_CODES`. Unknown codes answer with the
    roster rather than nothing, so a typo is one command from the right code."""
    normalized = (code or "").strip()
    known = _catalogue_code(normalized)
    if known is None:
        return {"ok": False, "code": normalized.upper(),
                "message": f"no diagnostic code `{normalized.upper()}`",
                "known": list(ALL_CODES)}
    if known in GUARANTEES:
        record = {"ok": True, "code": known, "guarantee": GUARANTEES[known]}
        if known in FIXES:
            record["fix"] = FIXES[known]
        return record
    entry = OTHER_CODES[known]
    record = {"ok": True, "code": known, "category": entry["category"],
              "meaning": entry["meaning"]}
    if entry["guarantee"]:
        record["guarantee"] = entry["guarantee"]
    if entry["fix"]:
        record["fix"] = entry["fix"]
    return record


# message-shape -> (code, category) for rejections that carry no tag
_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^`null` has no type"), "T2", "null-safety"),
    (re.compile(r"expects `.*`, got `"), "T1", "type-mismatch"),
    (re.compile(r"non-exhaustive match"), "T1", "exhaustiveness"),
    (re.compile(r"has no field |record literal for "), "T1", "type-mismatch"),
    (re.compile(r"takes \d+ (type )?argument|arity does not match"), "T1", "arity"),
    (re.compile(r"is not a method of service|is not declared by service"), "A6", "interface"),
    (re.compile(r"differs from the running manifest"), "G2", "admission"),
    (re.compile(r"provision conflict"), "G2", "linking"),
    (re.compile(r"dependency cycle|import cycle"), "G3", "linking"),
    (re.compile(r"cannot reassign|already (declared|bound)"), "G6", "binding"),
    (re.compile(r"is not declared in this (function|component)"), "G1", "binding"),
    (re.compile(r"is not a declared requirement"), "G1", "requirement"),
    (re.compile(r"acquisition after `provide`"), "A2", "ordering"),
    (re.compile(r"no builtin method"), "T1", "stdlib"),
    (re.compile(r"verified fn .* is not total"), "G7", "totality"),
    # witnessed-inverse externs (item 243, docs/design/243-witnessed-externs.md).
    # These raises carry explicit codes, but the shapes are classified here too
    # so a message-only path still resolves to the `witnessed` category.
    (re.compile(r"witnessed extern .* cannot be called in "), "G4", "witnessed"),
    (re.compile(r"inverse of witnessed extern|witnessed extern .* must (return|declare)"
                r"|witness .* is a host object"), "G4", "witnessed"),
    # items 399/400: the acquire-with-`undo` and `deferred`-emission fn-body
    # refusals carry explicit codes too, classified here for the message-only path.
    (re.compile(r"`acquire` extern .* cannot be called in "), "G4", "acquire"),
    (re.compile(r"`deferred` emission extern .* cannot be called in "), "G4", "deferred"),
    (re.compile(r"unclassified extern"), "G8", "boundary"),
    (re.compile(r"expected .*, found "), "SYNTAX", "parse"),
    (re.compile(r"unexpected character|unterminated string"), "SYNTAX", "lex"),
]


def classify(error: RevlError) -> dict:
    """One rejection as a structured record."""
    code = getattr(error, "code", None)
    category = getattr(error, "category", None)
    if code is None:
        # the guarantee tag may sit in the message or in the fix hint
        tag = _TAG.search(error.message) or _TAG.search(error.hint or "")
        if tag:
            code = tag.group(1)
            category = category or "guarantee"
        else:
            for pattern, mapped_code, mapped_category in _PATTERNS:
                if pattern.search(error.message):
                    code, category = mapped_code, mapped_category
                    break
    # the record always names a code: an unrecognised refusal is the `REVL`
    # fallback, and `REVL` is an emitter code with its own entry below
    effective = code or "REVL"
    record = {
        "severity": "error",
        "code": effective,
        "category": category or "check",
        # relativize at the JSON boundary so no raise site can leak the
        # loader-resolved absolute path (issue #2222)
        "file": relpath_or_abs(error.filename),
        "line": error.line,
        "message": error.message,
    }
    if error.hint:
        record["hint"] = error.hint
    expected = getattr(error, "expected", None)
    actual = getattr(error, "actual", None)
    if expected is not None or actual is not None:
        record["expected"] = expected
        record["actual"] = actual
    if code in GUARANTEES:
        # the code's headline guarantee, unless this failure mode has its own
        # (issue #2029)
        record["guarantee"] = GUARANTEES_BY_CATEGORY.get(
            (code, record["category"]), GUARANTEES[code])
    else:
        # a code outside the guarantee catalogue declares its own obligation,
        # or `None` where it enforces none (a parse error, or a verdict about
        # the run rather than about the source). A code with no row at all — a
        # reserved code, which `explain` refuses on purpose — adds no field
        # rather than inventing one. The fallback `REVL` carries
        # `unclassified` in its own row, so the honest degrade is unchanged
        # (issue #2028).
        declared = OTHER_CODES.get(effective, {}).get("guarantee")
        if declared is not None:
            record["guarantee"] = declared
    if getattr(error, "fix", None):
        # a rewrite specific to this rejection (a corrected line) outranks the
        # per-code one: the code's fix is written for its commonest shape
        record["fix"] = error.fix
    elif (code, record["category"]) in FIXES_BY_CATEGORY:
        # the code covers several failure modes and this is not the one the
        # per-code row was written for (issue #2029)
        record["fix"] = FIXES_BY_CATEGORY[(code, record["category"])]
    elif code in FIXES:
        # the exact rewrite, beside the guarantee, so an agent gets the fix
        # without a second `explain` call or parsing the prose hint
        record["fix"] = FIXES[code]
    elif OTHER_CODES.get(effective, {}).get("fix"):
        # the same remedy `revl explain <code>` gives, for a code the
        # guarantee/fix table never carried (issue #2028)
        record["fix"] = OTHER_CODES[effective]["fix"]
    # the derivation behind a search-based rejection (why.py): the G4
    # emission chain, the G3 cycle path, the two G2 providers
    why = getattr(error, "why", None)
    if why is not None:
        record["why"] = why.to_json()
    # item 274: the navigable-refusal map, copied verbatim beside the static
    # `fix`. Additive — a rejection with no `navigate` serializes exactly as
    # before, so `--json` consumers without navigate knowledge see a strict
    # superset. The record is already redacted for the untrusted-author view at
    # construction (navigate.py), so nothing here re-filters it.
    navigate = getattr(error, "navigate", None)
    if navigate is not None:
        record["navigate"] = navigate
    return record


def obligations(holes: list[dict]) -> dict:
    """Open typed holes as an agent-consumable document (docs/holes.md).

    Severity is `obligation`, not `error`: the draft compiled. It is the
    admission gate that says no while any of these is open, and an agent
    should treat the list as its remaining work, not as a rejection.
    """
    return {
        "ok": True,
        "holes": [
            {
                "severity": "obligation",
                "code": "T3",
                "category": "hole",
                "file": hole.get("file"),
                "line": hole.get("line"),
                "expected": hole.get("type"),
                "message": hole.get("message"),
                "guarantee": GUARANTEES["T3"],
            }
            for hole in holes
        ],
    }


def report(error: RevlError) -> dict:
    """A failed compile as an agent-consumable document.

    A multi-refusal compile raises a `RevlErrors` carrier (item 386) whose
    `.errors` holds every collected refusal; map `classify` over the list. A
    single `RevlError` (no `.errors`) still yields a one-element list, so every
    existing single-error consumer is unchanged.
    """
    errors = getattr(error, "errors", None) or [error]
    return {"ok": False, "diagnostics": [classify(e) for e in errors]}


def ok(**payload) -> dict:
    return {"ok": True, **payload}
