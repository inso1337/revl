"""Issue #1382: the python tier refuses a `validated` EXTERN, and still lowers a
`validated` service operation.

Background. Items 257 and 513 make a `validated` emission a checked boundary:
validate the completion against the schema derived from the return type, build
the declared value from the validated payload, raise a typed fault when it does
not conform, honour the decoding grammar and the `retry` budget. Issue #1373
found the other five tiers DROPPING all of that (byte-identical output with and
without the modifier) and PR #1381 made them refuse by name. Python was left
alone because it owns the seam.

It owns the seam at ONE of the two carriers. `_validated_call` fires at a
`key.method(...)` crossing, where revl owns the receiving side. An extern's host
body IS the provider, so the seam never fires there, `_grammar_registry`
deliberately withholds the grammar, and the emitted module was byte-identical
with and without `validated` on python too: measured 829 chars either way on
c6e8847a5, the same shape #1373 measured on ts (421), rust (551), wasm (697),
go (351) and java (4174).

`_grammar_registry`'s stated reason is that an extern's claim is about a provider
revl cannot judge, and an unjudgeable claim is worse than no claim. That is a
sound argument for withholding the GRAMMAR. It is not an argument for withholding
the DIAGNOSTIC: its own premise is that an unjudgeable claim is worse than no
claim, and accepting the modifier silently is exactly what lets an author make
one and keep believing it holds.

What this file pins, in both directions, because before it nothing in the tree
would have noticed this behaviour changing either way:

  * the extern carrier is refused, by name;
  * the operation carrier is NOT, and its seam still fires;
  * the refusal is declaration-keyed, not call-site keyed;
  * a document with no `validated` extern is untouched;
  * `_grammar_registry` still withholds the grammar (the half that was right);
  * and `--target temporal`, which READS this modifier on an extern, is not
    touched by the gate. That target pins a `validated` crossing to at-most-once
    so a completion is never re-billed as an idempotent write. A blanket refusal
    would have deleted it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402

import emit  # noqa: E402


_TYPES = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])
"""


def _extern_program(modifier: str) -> str:
    return _TYPES + f"""
extern emission[model] {modifier}fn complete(h: Str) -> AgentTurn = @py {{
  return {{"kind": "Final", "value": "x"}}
}}
"""


# --------------------------------------------------------------- the refusal

def test_a_validated_extern_is_refused_by_name():
    """The whole point: the author hears about it. The message names the extern,
    says which seam is missing and gives both ways forward."""
    ir = compile_source(_extern_program("validated "), "e.rvl")
    with pytest.raises(emit.EmitError) as exc:
        emit.emit(ir)
    message = str(exc.value)
    assert "`validated` extern `complete`" in message
    # it explains WHY this carrier and not the other one
    assert "SERVICE-METHOD crossing" in message
    # and it does not leave the author without a move
    assert "Drop `validated`" in message
    assert "`service` emission" in message


def test_the_unvalidated_twin_still_compiles():
    """The gate is keyed on the modifier and nothing else: the same extern
    without `validated` is emitted exactly as before."""
    code = emit.emit(compile_source(_extern_program(""), "e.rvl"))
    assert "def complete(" in code


def test_the_refusal_is_declaration_keyed_not_call_site_keyed():
    """`session_commit.refuse_deferred_on_ownerless_tier` keys off the call site,
    because a `deferred` extern's whole lowering is the enqueue there. `validated`
    is the other shape: the extern wrapper reaches the emitted module whether or
    not this document also calls it, so an uncalled declaration is still an
    unchecked boundary in the output and is still refused."""
    ir = compile_source(_extern_program("validated "), "e.rvl")
    assert not any(comp.get("steps") for comp in ir.get("components") or [])
    with pytest.raises(emit.EmitError, match="`validated` extern `complete`"):
        emit.emit(ir)


def test_retry_on_a_validated_extern_is_refused_with_it():
    """Slice 2's budget rides the same declaration, so it is refused by the same
    gate rather than needing a second one."""
    ir = compile_source(_extern_program("validated retry 3 "), "e.rvl")
    assert ir["externs"][0]["retry"] == 3
    with pytest.raises(emit.EmitError, match="`validated` extern `complete`"):
        emit.emit(ir)


# ------------------------------------------- the carrier that is NOT refused

_OPERATION_PROGRAM = _TYPES + """
service Model { emission[model] validated fn complete(h: List[Str]) -> AgentTurn }
service Loop { emission fn run(p: Str) -> Int }
component Agent requires model: Model provides agent: Loop {
  provide agent {
    fn run(session_id) {
      let t = emit model.complete(["p"])
      return 1
    }
  }
}
"""


def test_a_validated_service_operation_still_lowers_the_whole_seam():
    """The narrowness of the gate is the claim that needs pinning hardest. This
    tier is the only one that HAS the validate seam, and it keeps firing at the
    crossing where revl owns the receiving side."""
    code = emit.emit(compile_source(_OPERATION_PROGRAM, "m.rvl"))
    assert "_revl_validate" in code
    assert "_revl_register_grammars(" in code


def test_the_grammar_registry_still_withholds_an_externs_grammar():
    """The half of `_grammar_registry`'s reasoning that was right, and stays.
    The IR carries the derived grammar; the registry still does not offer it,
    so `revl_constrain("extern:...")` still finds nothing. This gate adds a
    diagnostic, it does not start making an unjudgeable claim judgeable."""
    ir = compile_source(_extern_program("validated "), "e.rvl")
    assert ir["externs"][0]["response_grammar"]
    assert emit._grammar_registry(ir.get("services") or {}) == {}


# ------------------------------------ the guarantee this must not have deleted

def test_temporal_still_reads_validated_on_an_extern():
    """The reason this gate is python-emitter-local rather than a frontend check.

    `--target temporal` READS `validated` on an extern: `_retry_class` pins the
    crossing to at-most-once so Temporal never re-bills a model completion as an
    idempotent write, even when the same declaration carries a key that would
    otherwise earn a RetryPolicy. Its two renderings of one keyed extern differ,
    which is what a blanket refusal would have destroyed.

    Run in a SUBPROCESS: `emit_temporal` does `from emit import _Ctx, ...`,
    meaning the typescript `emit`, and this module has already bound the python
    one as `sys.modules["emit"]`. Two backend roots, one module name; the clean
    interpreter is what keeps this checking the temporal renderer rather than an
    import accident."""
    probe = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
sys.path.insert(0, {str(ROOT / "backends" / "typescript")!r})
from revl import compile_source
import emit_temporal

def src(modifier):
    return (f'extern emission[model] {{modifier}}idempotent(key: card)'
            ' fn ask(card: Str) -> Str = @ts {{ return "ok" }}\\n'
            'component Pay {{\\n  emit ask("visa")\\n}}\\n')

validated = emit_temporal.emit_temporal(compile_source(src('validated retry 2 '), 'a.revl'))
plain = emit_temporal.emit_temporal(compile_source(src(''), 'a.revl'))
assert validated != plain, 'temporal no longer distinguishes a validated extern'
assert 'maximumAttempts: 2' not in validated, 'the 257 budget must not become a Temporal policy'
assert 'DEDUP_SAFE_RETRY' in plain, 'the keyed twin should still earn a retry policy'
assert 'DEDUP_SAFE_RETRY' not in validated, 'a validated crossing must stay at-most-once'
assert '{{ ask, recordResidue }} = proxyActivities' in validated
assert 'const {{ ask }} = proxyActivities' in plain
print('ok')
"""
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
