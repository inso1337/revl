"""The tier gate for a `validated` emission (roadmap items 257 and 513).

A `validated` emission is a CHECKED boundary. Item 257 derives a schema from the
declared return type, item 513 renders a decoding grammar from that same schema,
and the emitted crossing validates the completion against the schema, builds the
declared value from the validated payload, and raises a typed validation fault
when the response does not conform. Item 257 Slice 2 adds the `retry N` budget on
top, and item 121 Slice 2 mints the value-flow token a validated completion
carries forward.

All of that lives in ONE tier. `validate_response`, `validate_retry`,
`register_grammars`, `revl_constrain` and `ResponseValidationError` are declared
in `backends/python/runtime.py` and emitted by `backends/python/emit.py`, and
nowhere else: measured on 2026-09-22, the four names appear in 0 files under
`backends/{typescript,rust,wasm,go,java}`.

Until this module, the other five tiers did not refuse a `validated` emission.
They DROPPED it. Compiling one program twice, differing only in the modifier, the
typescript, rust, wasm, go and java output was byte-identical both ways (issue
#1373; the java half of that measurement is new, the issue could only infer it
from source). The author wrote a constraint, the tier emitted a program without
it, and said nothing. A wrong lowering is caught by a byte oracle and a refusal
is caught by its message; nothing catches a tier that emits less than it was
given and stays quiet, which is why this is refused rather than left as a missing
feature.

Why refuse rather than lower, on all five
-----------------------------------------
The typescript runtime is the near miss: `_jsonSchemaError` in
`backends/typescript/runtime.ts` is already a faithful mirror of the python
`_json_schema_error`, over the same derived JSON-Schema subset, written for item
130's event contracts. It could check the shape today. It is still refused,
because the shape check is one of five parts. A tier that lowered only the check
would silently drop the declared-value construction (§3.2, so the tagged wire
shape leaks into the matched-over value), item 513's grammar claim, item 257
Slice 2's `retry` budget, and item 121's provenance token: the same silent drop
one layer down, in a boundary that now reads as validated. Half a checked
boundary that reads as a whole one is the failure this gate exists to stop, so
the five tiers refuse until a tier grows the whole seam.

Keyed on the DECLARATION, not the call site
-------------------------------------------
`session_commit.refuse_deferred_on_ownerless_tier` keys off the call site,
because `deferred`'s entire lowering is the enqueue at that site and a declared
but never-called deferred extern costs the emitted module nothing. `validated` is
the other shape: the guarantee is attached to the crossing itself, and the tier
emits that crossing (the service interface a provider implements and a caller
invokes) whether or not this document also calls it. A declared-but-uncalled
`validated` method still reaches the tier's output as an unchecked boundary, so
the declaration is what is refused.
"""

from __future__ import annotations

from .errors import RevlError

#: The tiers with no response-validation seam today. The py tier is deliberately
#: absent: it owns `validate_response` / `validate_retry` / the grammar registry.
UNVALIDATING_TIERS = ("rust", "go", "java", "wasm", "typescript")


def validated_crossings(ir: dict) -> list:
    """Every `validated` crossing declared in ``ir``, as ``(where, kind)`` pairs.

    ``where`` is how the crossing is named to an author: ``"Model.complete"``
    for a service operation, ``"complete"`` for an extern. ``kind`` is
    ``"operation"`` or ``"extern"``. Ordered services-then-externs, each sorted
    by name, so the refusal names the same crossing on every run.

    Both carriers are here because `lower.py` binds the same three keys on both
    (`_method_validated_ir` and the extern descriptor), and both are dropped by
    the five tiers: measured on 2026-09-22, a `validated` extern with a native
    body emits byte-identically to its unvalidated twin on typescript, rust,
    wasm, go and java (and on python, which never validates an extern's response
    by design; see `_grammar_registry` in `backends/python/emit.py`)."""
    found: list = []
    for svc_name, svc in sorted((ir.get("services") or {}).items()):
        methods = (svc or {}).get("methods") or {}
        for m_name, spec in sorted(methods.items()):
            if (spec or {}).get("validated"):
                found.append((f"{svc_name}.{m_name}", "operation"))
    for ext in ir.get("externs") or []:
        if ext.get("validated"):
            found.append((ext.get("name") or "?", "extern"))
    return found


def _diagnostic(where: str, kind: str, tier: str) -> str:
    """The one wording for all five tiers, so five backends do not invent five
    messages (the same reason `session_commit._diagnostic` is shared)."""
    return (
        f"`validated` {kind} `{where}` needs the response-validation seam "
        f"(check the completion against the schema derived from its return type, "
        f"build the declared value from the validated payload, raise a typed "
        f"validation fault when it does not conform, honour the stated decoding "
        f"grammar and the `retry` budget), and the {tier} tier has none of it. "
        f"Lowering it would emit a module byte-identical to the same crossing "
        f"written WITHOUT `validated`: a checked boundary in the source and an "
        f"unchecked one in the output, which no byte oracle can catch. So the "
        f"tier refuses by name rather than answering with less than it was given "
        f"(items 257 and 513). Drop `validated` to accept an unchecked boundary, "
        f"or target the python tier, which lowers it.")


def refuse_validated_on_unvalidating_tier(ir: dict, tier: str,
                                          filename: str = "<emit>") -> None:
    """Refuse a `validated` crossing on one of the five tiers with no validation
    seam. A no-op on the py tier and on any IR that declares no `validated`
    crossing, which today is every checked-in program: 0 of 1066 `.rvl` sources
    declare one, 0 of 54 checked-in `.ir.json` documents and 0 of 28 backend
    goldens carry `response_schema` or `response_grammar` (measured 2026-09-22),
    so this gate moves no golden.

    Raises :class:`RevlError` on the first declared crossing; each backend
    re-raises it through its own `EmitError` channel."""
    if tier in ("python", "py"):
        return
    for where, kind in validated_crossings(ir):
        raise RevlError(filename, 0, _diagnostic(where, kind, tier),
                        code="G4", category="validated")
