"""A `Secret[...]` on the INVERSE's parameter marks the witness position too.

The compiler strips every `Secret[...]` qualifier before lowering and leaves
stamps behind in the IR, because the qualifier itself is gone by then and a
stamp is the only channel a runtime has for learning that a position is
confidential. One of those stamps is `secret_witness`: a witnessed extern's
durable discharge-descriptor records the `Ok` witness as its inverse's referent
argument, so the stamp is what tells each tier's WAL writer to frame the
placeholder instead of the value.

An author can say "this witness is confidential" from either end of the call.
On the producing side, by qualifying what the extern hands back:

    extern witnessed fn lease(...) -> Result[Secret[Str], Str] undo release(result)

or on the receiving side, by qualifying what the inverse takes:

    extern pure fn release(lease: Secret[Str]) -> Unit = @py { ... }
    extern witnessed fn lease(...) -> Result[Str, Str] undo release(result)

Both describe the same bytes at the same position. Only the first was read: the
stamp was minted from the extern's OWN declared return type, so the second
compiled clean, minted nothing, and every recording tier wrote the credential
into `$REVL_WAL` — a plaintext file at rest — verbatim. The declaration was
accepted and load-bearing (the checker does validate the inverse's parameter
type), which is what made the silence easy to mistake for a decision.

What this file holds to, both halves of the fix:

**The marking follows the declaration, whichever end spells it.**
`taint.witness_receiver_position` resolves the `undo` slot's callee against the
declared signatures and mints the stamp when the witness binds to a parameter
the author qualified `Secret[...]`. It does NOT mint `secret_return` in that
case, and that is deliberate: the extern's return is an ordinary value, so
marking it would redact the whole result for nothing. The narrow rule that keeps
`Result[Str, Secret[Str]]`'s referent usable is preserved on both spellings.

**The writers honour it unchanged.** Every tier already decides at the one point
that writes the record, off the compiler's stamp, so no emitter moved — which is
the point: the fix is upstream of all four WAL writers at once, and a tier with
no value-registry of its own needs none.

Every assertion is PAIRED — the stringifying expression absent **and** the
placeholder present — so no test can pass because the record was dropped, and
each carries a false-positive control proving an ordinary witness is still
recorded verbatim.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

CANARY = "CANARY-RECEIVER-7a4b9c1d"
REDACTED_SECRET = "<redacted:secret>"

# The tiers whose `--record` mode writes a witnessed inverse's referent into a
# durable log. `typescript` is absent on purpose: its runtime builds discharge
# descriptors in memory and opens no `REVL_WAL`.
WAL_TIERS = ("go", "rust", "java", "wasm")


def _emitter(tier: str):
    """Load one backend emitter as a module. They are scripts, not a package."""
    directory = "python" if tier == "py" else tier
    path = ROOT / "backends" / directory / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_{tier}_emit_receiver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit_record(tier: str, ir: dict) -> str:
    out = _emitter(tier).emit(ir, record=True)
    if isinstance(out, dict):  # wasm returns {name: wat}
        return "\n".join(out.values())
    return out


RECORD_CALL = {
    "go": "revlRecordTransactional(",
    "rust": "revl_record_transactional(",
    "java": "revlRecordTransactional(",
    "wasm": "(call $revl_wal_record ",
}


def _record_call(tier: str, emitted: str) -> str:
    """The one emitted line that writes the durable descriptor, so assertions
    are scoped to the record rather than to the whole module — the witness
    legitimately appears elsewhere, because the in-process inverse replays
    against it."""
    lines = [ln for ln in emitted.splitlines()
             if RECORD_CALL[tier] in ln and "func " not in ln
             and "pub fn " not in ln and "static " not in ln
             and "(import " not in ln]
    assert len(lines) == 1, f"{tier}: expected one record call, got {lines}"
    return lines[0]


def _placeholder_reaches(tier: str, emitted: str) -> bool:
    """Whether the record call actually carries the placeholder.

    go/rust/java put the literal in the call; the wasm module cannot, because a
    Str crosses the host import as a POINTER, so the call frames the offset of a
    pooled `<redacted:secret>` data segment — resolved here rather than trusted.
    """
    call = _record_call(tier, emitted)
    if tier != "wasm":
        return REDACTED_SECRET in call
    for line in emitted.splitlines():
        if '(data (i32.const ' in line and REDACTED_SECRET in line:
            offset = line.split("(i32.const ", 1)[1].split(")", 1)[0].strip()
            return f"(i32.const {offset})" in call
    return False


# What each tier renders as the inverse's referent when the witness is NOT
# confidential. These are the exact fragments the leak was made of.
WITNESS_EXPR = {
    "go": 'fmt.Sprintf("%v", result)',
    "rust": 'format!("{}", result)',
    "java": "String.valueOf(result)",
    "wasm": "(global.get $g_wit_val_",
}

LEASE_BODY = {
    "go": '\n\treturn RevlOk[string, string]{Value: "%s"}\n' % CANARY,
    "rust": '\n Ok("%s".to_string())\n' % CANARY,
    "java": '\n return new RevlResult.Ok<>("%s");\n' % CANARY,
    "wasm": ("\n (i32.store (i32.const 4200) (i32.const 5))"
             "\n (i32.store (i32.const 4204) (i32.const 0x23776f72))"
             "\n (i32.store8 (i32.const 4208) (i32.const 0x31))"
             "\n (i32.store (i32.const 4096) (i32.const 0))"
             "\n (i64.store (i32.const 4104)"
             " (i64.extend_i32_u (i32.const 4200)))"
             "\n (i32.const 4096)\n"),
    "py": '\n return Ok("%s")\n' % CANARY,
}

REVOKE_BODY = {
    "go": "\n\t_ = id\n",
    "rust": "\n let _ = id;\n",
    "java": "\n // the in-memory inverse\n",
    "wasm": " ",
    "py": "\n return\n",
}

LIFECYCLE_TEST = """
lifecycle test "leaser boots and unloads clean" {
  load Leaser
  unload Leaser
  assert no_residue
}
"""


def _source(tier: str, inverse_param: str, returns: str = "Result[Str, Str]",
            undo: str = "revoke(result)") -> str:
    """A witnessed composition whose INVERSE takes its referent through a
    parameter declared `inverse_param`.

    The `lifecycle test` is what routes a document carrying top-level externs to
    the LIVE component path on the go tier rather than its pure typed-core one;
    the java and wasm emitters refuse a lifecycle test outright and reach the
    same call site without one."""
    tag = {"rust": "rs"}.get(tier, tier)
    witnessed = "witnessed[t]" if tier == "wasm" else "witnessed"
    drive = "" if tier in ("java", "wasm") else LIFECYCLE_TEST
    return f"""
service Noop {{ fn ping() -> Str }}

extern {witnessed} fn lease() -> {returns}
    undo {undo}
    = @{tag} {{{LEASE_BODY[tier]}}}

extern pure fn revoke(id: {inverse_param}) -> Unit = @{tag} {{{REVOKE_BODY[tier]}}}

component Leaser provides noop: Noop {{
  effect lease()

  provide noop {{
    fn ping() = "pong"
  }}
}}

{drive}"""


def _lease_ir(inverse_param: str, tier: str = "py", **kw) -> dict:
    return compile_source(_source(tier, inverse_param, **kw))


# ---------------------------------------------------------------------------
# the rule, read off the IR
# ---------------------------------------------------------------------------


def test_a_secret_on_the_inverse_parameter_stamps_the_witness():
    """The finding's exact declaration. The witness position is marked, and the
    return is NOT — the extern hands back an ordinary value, so `secret_return`
    would redact the whole result for nothing."""
    ext = {e["name"]: e for e in
           _lease_ir("Secret[Str]")["externs"]}["lease"]
    assert ext["secret_witness"] is True
    assert "secret_return" not in ext
    assert ext["returns"] == "Result[Str, Str]"
    assert ext["witness"] == "Str"


def test_the_qualifier_is_still_stripped_off_the_inverse():
    """Orthogonality: the inverse's declared type reaches the base checker and
    the emitter bare, exactly as before the stamp was minted."""
    ir = _lease_ir("Secret[Str]")
    revoke = {e["name"]: e for e in ir["externs"]}["revoke"]
    assert revoke["params"][0]["type"] == "Str"
    assert revoke["params"][0]["secret"] is True


def test_a_nested_qualifier_on_the_inverse_parameter_stamps_too():
    """The receiving side follows the value, not the spelling, exactly as the
    producing side does: the referent reaches the inverse inside a container."""
    ext = {e["name"]: e for e in
           _lease_ir("List[Secret[Str]]",
                     returns="Result[List[Str], Str]")["externs"]}["lease"]
    assert ext["secret_witness"] is True
    assert "secret_return" not in ext


def test_a_witness_bound_to_an_unqualified_parameter_is_not_stamped():
    """The rule's boundary, and its false-positive control. A `Secret[...]` on
    an inverse parameter the witness does NOT flow into says nothing about the
    referent, so marking it would redact a usable descriptor for nothing."""
    ext = {e["name"]: e for e in
           _lease_ir("Str", undo="revoke(result)")["externs"]}["lease"]
    assert "secret_witness" not in ext

    # ...and the same, with a confidential parameter present on the inverse but
    # reached by a constant rather than by the witness.
    src = _source("py", "Str", undo='revoke(result, "tag")')
    src = src.replace("extern pure fn revoke(id: Str) -> Unit",
                      "extern pure fn revoke(id: Str, tag: Secret[Str]) -> Unit")
    ir = compile_source(src)
    ext = {e["name"]: e for e in ir["externs"]}["lease"]
    assert "secret_witness" not in ext


def test_a_secret_parameter_on_an_unrelated_declaration_is_not_the_witness():
    """Only the DECLARED INVERSE's parameters are consulted. A module-level fn
    that happens to take a `Secret[...]` — or an extern the undo slot never
    names — cannot mint the witnessed extern's stamp."""
    src = """
extern pure fn stash(box: Secret[Str]) -> Unit = @py { return }
extern witnessed fn lease() -> Result[Str, Str] undo revoke(result) = @py {
    return Ok("x")
}
extern pure fn revoke(id: Str) -> Unit = @py { return }
component C { effect lease() }
"""
    ext = {e["name"]: e for e in compile_source(src)["externs"]}["lease"]
    assert "secret_witness" not in ext


def test_an_extern_with_no_inverse_is_untouched():
    """Additive: nothing is stamped unless a witnessed extern declares an
    inverse that receives the witness through a qualified parameter."""
    ir = compile_source(
        'extern pure fn stash(box: Secret[Str]) -> Unit = @py { return }\n'
        "component C { }\n")
    assert "secret_witness" not in ir["externs"][0]


def test_both_spellings_at_once_stamp_once():
    """An author who qualifies both ends is describing the same fact; the stamp
    is a boolean and stays one, and the return keeps its own stamp."""
    ext = {e["name"]: e for e in
           _lease_ir("Secret[Str]", returns="Result[Secret[Str], Str]")["externs"]}["lease"]
    assert ext["secret_witness"] is True
    assert ext["secret_return"] is True


# ---------------------------------------------------------------------------
# the self-host lowering gate, which must mint the same IR
# ---------------------------------------------------------------------------


def _selfhost_lower_to_ir():
    """The `lower_to_ir` the SELF-HOSTED lowering gate compiles to.

    `selfhost/lower.rvl` streams tokens straight to IR JSON and never builds the
    reference's `Prog`, so it resolves the inverse's parameter types from its own
    token-level scan (`taint_decl_params`) rather than from `prog.fns`. That scan
    is a second implementation of the same rule, which is exactly why it needs
    the same tests: a fix in `taint.py` alone would leave the gate minting IR
    without the stamp, and the gate's IR is what `crates/revl-gate` consumes."""
    from revl import compile_files
    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_receiver", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_receiver.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["lower_to_ir"]


@pytest.fixture(scope="module")
def selfhost():
    return _selfhost_lower_to_ir()


# The externs section of a document the self-host gate lowers, next to what the
# reference lowers for the same text. The gate is held to byte parity here, not
# merely to the stamp: a gate that minted the flag but disagreed about the
# witness TYPE would still hand `crates/revl-gate` IR the emitters cannot read.
SELFHOST_PARITY = {
    "receiver-side":
        'extern pure fn revoke(id: Secret[Str]) -> Unit = @py {\n return\n}\n'
        'extern witnessed fn lease() -> Result[Str, Str]\n'
        '    undo revoke(result)\n'
        '    = @py {\n return Ok("CANARY-RECEIVER-7a4b9c1d")\n}\n',
    "ordinary":
        'extern pure fn revoke(id: Str) -> Unit = @py {\n return\n}\n'
        'extern witnessed fn lease() -> Result[Str, Str]\n'
        '    undo revoke(result)\n'
        '    = @py {\n return Ok("CANARY-RECEIVER-7a4b9c1d")\n}\n',
    "return-side":
        'extern pure fn revoke(id: Str) -> Unit = @py {\n return\n}\n'
        'extern witnessed fn lease() -> Result[Secret[Str], Str]\n'
        '    undo revoke(result)\n'
        '    = @py {\n return Ok("CANARY-RECEIVER-7a4b9c1d")\n}\n',
    "receiver-side, nested":
        'extern pure fn revoke(w: List[Secret[Str]]) -> Unit = @py {\n return\n}\n'
        'extern witnessed fn lease() -> Result[Str, Str]\n'
        '    undo revoke([result])\n'
        '    = @py {\n return Ok("CANARY-RECEIVER-7a4b9c1d")\n}\n',
    "receiver-side, wrong parameter":
        'extern pure fn revoke(id: Str, tag: Secret[Str]) -> Unit = @py {\n'
        ' return\n}\n'
        'extern witnessed fn lease() -> Result[Str, Str]\n'
        '    undo revoke(result, "tag")\n'
        '    = @py {\n return Ok("CANARY-RECEIVER-7a4b9c1d")\n}\n',
}


@pytest.mark.parametrize("case", sorted(SELFHOST_PARITY))
def test_the_selfhost_gate_lowers_the_same_externs_as_the_reference(case, selfhost):
    """Byte parity over the whole externs section, both spellings plus the
    boundary cases. The gate never built a `Prog`, so this is the only check
    that its own token-level scan agrees with `taint.py` — and the boundary
    cases matter as much as the positive one, because a scan that matched the
    wrong parameter would mark a usable witness confidential."""
    src = SELFHOST_PARITY[case]
    assert json.loads(selfhost(src))["externs"] == compile_source(src)["externs"]


@pytest.mark.parametrize("case", ["return-side", "receiver-side", "ordinary"])
def test_the_selfhost_gate_strips_the_qualifier_off_the_witness_type(case, selfhost):
    """`retDecl` is the RAW declared text, because the taint stamps read the
    qualifier off it — so the gate has to take the qualifier back off before it
    writes `witness`, which is a concrete data type (`_rust_type`,
    `_java_v3_type` and the wasm emitter's direct read all parse it). A gate
    that wrote `Secret[Str]` there would hand every backend an unknown type
    name and silently degrade the witness to an opaque value."""
    ext = {e["name"]: e for e in
           json.loads(selfhost(SELFHOST_PARITY[case]))["externs"]}["lease"]
    assert ext["witness"] == "Str"


def test_the_selfhost_gate_marks_the_receiver_spelling(selfhost):
    """The stamp itself, read off the gate's own IR."""
    marked = {e["name"]: e for e in
              json.loads(selfhost(SELFHOST_PARITY["receiver-side"]))["externs"]}
    assert marked["lease"]["secret_witness"] is True
    assert "secret_return" not in marked["lease"]

    plain = {e["name"]: e for e in
             json.loads(selfhost(SELFHOST_PARITY["ordinary"]))["externs"]}
    assert "secret_witness" not in plain["lease"]


# ---------------------------------------------------------------------------
# the tier WAL writers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", WAL_TIERS)
def test_the_tier_wal_writer_keeps_it_off_disk(tier):
    """The paired assertion, per tier: the expression that stringified the
    witness into the durable record is GONE and the placeholder stands in its
    place — so this cannot pass on an emission that dropped the record.

    Measured on the emitted call site rather than on the canary bytes because
    the wasm witness is built at a scratch address and its bytes never appear in
    the WAT at all."""
    emitted = _emit_record(tier, _lease_ir("Secret[Str]", tier))
    assert WITNESS_EXPR[tier] not in _record_call(tier, emitted), \
        f"{tier} still writes the witness into the WAL record"
    assert _placeholder_reaches(tier, emitted), f"{tier} framed no placeholder"


@pytest.mark.parametrize("tier", WAL_TIERS)
def test_an_ordinary_witness_is_still_recorded_verbatim(tier):
    """The false-positive control: recovery needs the referent to address the
    right thing, so over-redaction is a real cost, not a free win."""
    emitted = _emit_record(tier, _lease_ir("Str", tier))
    assert WITNESS_EXPR[tier] in _record_call(tier, emitted), \
        f"{tier} lost its referent"
    assert not _placeholder_reaches(tier, emitted), \
        f"{tier} redacts an unmarked witness"


@pytest.mark.parametrize("tier", WAL_TIERS)
def test_a_secret_on_the_wrong_parameter_leaves_the_referent_alone(tier):
    """End of the emitter, on the receiving-side rule: the inverse declares a
    confidential parameter, the witness is not the argument that reaches it, and
    the record keeps its referent."""
    emitted = _emit_record(tier, _lease_ir("Str", tier))
    assert WITNESS_EXPR[tier] in _record_call(tier, emitted)
    assert not _placeholder_reaches(tier, emitted)


# ---------------------------------------------------------------------------
# the py tier, which redacts by value rather than by position
# ---------------------------------------------------------------------------


def _emit_py(ir: dict) -> str:
    """The py emitter takes no `record` flag — it always emits the WAL-aware
    runtime call — so it is reached without the WAL tiers' keyword."""
    return _emitter("py").emit(ir)


def test_the_py_recorder_is_told_which_witness_to_keep():
    """This tier cannot read the stamp at the record site.

    The four WAL tiers substitute a literal for the referent when
    `secret_witness` is set, so one upstream stamp fixes them together. The py
    recorder instead asks the confidential registry whether the value it is
    about to write is one a declared marking already identified — and neither
    the extern's return decorator (there is no `Secret` in its return) nor a
    `Secret` receiver head (the inverse is an ordinary pure extern) fires for
    this spelling. So the witnessed step has to register the witness itself,
    before the frame takes it."""
    emitted = _emit_py(_lease_ir("Secret[Str]"))
    lines = emitted.splitlines()
    step = next(i for i, ln in enumerate(lines) if "transactional(" in ln)
    assert "mark_secret" in lines[step - 1], \
        f"the witness reaches the recorder unregistered: {lines[step - 1]!r}"
    assert ".value" in lines[step - 1]


def test_the_py_recorder_leaves_an_unmarked_witness_alone():
    """The false-positive control, and the reason the registration is gated:
    an ordinary referent is what `revl recover` replays the inverse against."""
    emitted = _emit_py(_lease_ir("Str"))
    assert "mark_secret" not in emitted


def test_the_py_recorder_does_not_double_register_a_declared_return():
    """The return-position spelling already registered the value at its origin,
    so the step must add nothing — the same emission as before the change. A
    `Secret[...]` in the `Ok` arm implies `secret_return`, which is what keeps
    the two spellings apart at the call site."""
    ir = _lease_ir("Str", returns="Result[Secret[Str], Str]")
    ext = {e["name"]: e for e in ir["externs"]}["lease"]
    assert ext["secret_witness"] is True and ext["secret_return"] is True
    emitted = _emit_py(ir)
    assert "@_revl_secret_result" in emitted
    assert "mark_secret" not in emitted


def _method_seam(inverse_param: str) -> str:
    return (
        "extern pure fn revoke(id: %s) -> Unit = @py {\n return\n}\n"
        "extern witnessed fn lease() -> Result[Str, Str]\n"
        "    undo revoke(result)\n"
        '    = @py {\n return Ok("%s")\n}\n'
        "service Ops { emission fn touch(p: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn touch(p) {\n"
        "      effect lease()\n"
        "    }\n"
        "  }\n"
        "}\n"
    ) % (inverse_param, CANARY)


def test_the_py_witnessed_method_step_registers_it_too():
    """A witnessed effect in a PROVIDE-METHOD body is a second call site with
    its own emitter (`transactional_method`, item 318's per-tool-call seam) and
    the same declaration reaches it, so the registration has to be there too —
    the method form yields no disposer into a body generator, so a reader
    checking only `_witnessed_step` would miss it."""
    lines = _emit_py(compile_source(_method_seam("Secret[Str]"))).splitlines()
    step = next(i for i, ln in enumerate(lines) if "transactional_method(" in ln)
    assert "mark_secret" in lines[step - 1], \
        f"the method seam hands the recorder an unregistered witness: {lines[step - 1]!r}"
    assert "mark_secret" not in _emit_py(compile_source(_method_seam("Str")))


# ---------------------------------------------------------------------------
# the go tier, executed: what actually lands in $REVL_WAL
# ---------------------------------------------------------------------------

needs_go = pytest.mark.skipif(
    shutil.which("go") is None, reason="needs the go toolchain")

_GO_MOD = """module revl.secretwalreceiver

go 1.25.0

require github.com/0xdenny218/stc-go v0.6.1-0.20260818143352-b3d6788a428e
"""


def _run_go_wal(tmp: Path, inverse_param: str) -> str:
    """Emit, build and RUN a record-mode go composition; return its WAL text.

    The claim under test is about a file on disk, so this reads the file rather
    than the emitter's output."""
    src = _emitter("go").emit(_lease_ir(inverse_param, "go"), "secretwal", record=True)
    (tmp / "gen_test.go").write_text(src, encoding="utf-8")
    (tmp / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    sums = ROOT / "backends" / "go" / "scenarios" / "go.sum"
    if sums.exists():
        shutil.copy(sums, tmp / "go.sum")
    wal = tmp / "wal.jsonl"
    result = subprocess.run(
        ["go", "test", "./..."], cwd=str(tmp), capture_output=True, text=True,
        timeout=600, stdin=subprocess.DEVNULL,
        env={**os.environ, "REVL_WAL": str(wal), "GOFLAGS": "-mod=mod"})
    if not wal.exists():
        pytest.skip(f"go build/run unavailable here: {result.stdout}{result.stderr}")
    return wal.read_text(encoding="utf-8")


def _referents(wal: str) -> list:
    """Every discharge-descriptor's referent argument, DECODED — go's
    `json.Marshal` escapes `<` as `\\u003c`, so a substring search for the
    placeholder would miss a record that carries it."""
    out = []
    for line in wal.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record") == "discharge-descriptor":
            out.extend((record.get("call") or {}).get("args") or [])
    return out


@needs_go
def test_a_receiver_declared_credential_does_not_reach_the_durable_log():
    """End to end, against the real stc-go runtime: what lands in the file at
    rest is the placeholder `revl recover` knows how to refuse."""
    with tempfile.TemporaryDirectory() as d:
        wal = _run_go_wal(Path(d), "Secret[Str]")
    assert _referents(wal) == [REDACTED_SECRET], "nothing was recorded at all"
    assert CANARY not in wal


@needs_go
def test_an_unqualified_receiver_still_reaches_the_durable_log():
    """The executed false-positive control."""
    with tempfile.TemporaryDirectory() as d:
        wal = _run_go_wal(Path(d), "Str")
    assert _referents(wal) == [CANARY]
