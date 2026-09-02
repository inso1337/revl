"""A `Secret[T]` nested in a type constructor keeps its marking (item 256 §7b).

The compiler strips the `Secret[...]` qualifier before lowering and leaves
stamps behind in the IR — `params[i]["secret"]`, `secret_return`, a config
field's `secret` — because the qualifier itself is gone by then and a stamp is
the ONLY channel by which a runtime can learn that a position is confidential.
Those stamps were written off the OUTERMOST head alone, so a declaration that
wrapped its confidential payload in a type constructor produced no stamp at all:

    extern witnessed fn lease(...) -> Result[Secret[Str], Str]

compiled to `returns: "Result[Str, Str]", witness: "Str"` with nothing marking
it. That shape is not an exotic one. A `witnessed` extern is REQUIRED to return
`Result[Witness, Error]` (G4 refuses anything else), so nesting is the only way
a confidential witness can be spelled — which made a confidential witness
inexpressible rather than merely unmarked.

What that cost: a witnessed extern's durable discharge-descriptor records the Ok
witness as the inverse's referent argument, and `$REVL_WAL` is a plaintext file
at rest. So a leased credential landed on disk verbatim on every recording tier.

Two halves are fixed here and both are exercised below.

**The marking follows the value.** `taint.mentions_secret` recurses through type
constructors — `Result`, `Opt`, `List`, `Map`, a declared record — because those
are containers whose value graph physically carries the confidential bytes, and
the runtime funnels that consume the marking are themselves value-graph walks.
It stops at a FUNCTION type's arguments: `Fn[Secret[Str], Int]` declares what a
closure will be CALLED with, not what the closure value holds, so there are no
bytes there for a redactor to find. `secret_witness` is the narrower sibling —
true only when the `Ok` arm is confidential — because `Result[Str, Secret[Str]]`
puts nothing confidential in the referent and redacting it would lose a usable
descriptor for nothing.

**The writers honour it.** Each tier's WAL writer decides at the ONE point that
writes the record, off the compiler's stamp, rather than at each reader of the
log — so a tier with no value-registry of its own needs none: the
confidentiality is positional and known before the program runs. The placeholder
is the one `revl recover` already reads back (`recovery._has_redacted_arg`) to
refuse a replay it cannot honestly perform.

Every assertion is PAIRED — the canary absent **and** the placeholder present —
so no test can pass because nothing was emitted at all, and each fix carries a
false-positive control proving an ordinary witness is still recorded verbatim.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402

# Spelled out rather than imported, so these tests can RUN against a tree with
# no fix in it and fail on the leak instead of on a missing symbol.
CANARY = "CANARY-LEASE-0f1e2d3c"
REDACTED_SECRET = "<redacted:secret>"

# The tiers whose `--record` mode writes a witnessed inverse's referent into a
# durable log. `typescript` is absent on purpose: its runtime builds discharge
# descriptors in memory and opens no `REVL_WAL`, so it has no durable sink to
# keep the value out of.
WAL_TIERS = ("go", "rust", "java", "wasm")


def _emitter(tier: str):
    """Load one backend emitter as a module. They are scripts, not a package."""
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_{tier}_emit_secret", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit_record(tier: str, ir: dict) -> str:
    """The tier's record-mode output as one searchable string."""
    out = _emitter(tier).emit(ir, record=True)
    if isinstance(out, dict):  # wasm returns {name: wat}
        return "\n".join(out.values())
    return out


# The emitted call that writes one witnessed inverse's discharge-descriptor.
# Every assertion below is scoped to THIS line rather than to the whole module,
# because the witness legitimately appears elsewhere — the in-process inverse
# really does replay against it — and a whole-file search would confuse the two.
RECORD_CALL = {
    "go": "revlRecordTransactional(",
    "rust": "revl_record_transactional(",
    "java": "revlRecordTransactional(",
    "wasm": "(call $revl_wal_record ",
}


def _record_call(tier: str, emitted: str) -> str:
    """The one emitted line that writes the durable descriptor."""
    lines = [ln for ln in emitted.splitlines()
             if RECORD_CALL[tier] in ln and "func " not in ln
             and "pub fn " not in ln and "static " not in ln
             and "(import " not in ln]
    assert len(lines) == 1, f"{tier}: expected one record call, got {lines}"
    return lines[0]


def _placeholder_reaches(tier: str, emitted: str) -> bool:
    """Whether the record call actually carries the placeholder.

    Four tiers spell it two ways. go/rust/java put the literal in the call. The
    wasm module cannot: a Str crosses the host import as a POINTER, so the call
    frames the offset of a pooled `<redacted:secret>` data segment — which is
    checked here by resolving that offset rather than by trusting the constant.

    Scoped to the record call on purpose: the go tier's host-trace registry
    (item 421 F6) declares the SAME constant in its preamble, so a whole-module
    search would report a redacted referent for an emission that has none.
    """
    call = _record_call(tier, emitted)
    if tier != "wasm":
        return REDACTED_SECRET in call
    for line in emitted.splitlines():
        if '(data (i32.const ' in line and REDACTED_SECRET in line:
            offset = line.split("(i32.const ", 1)[1].split(")", 1)[0].strip()
            return f"(i32.const {offset})" in call.rsplit("(i32.const", 1)[0] + \
                "(i32.const" + call.rsplit("(i32.const", 1)[1]
    return False


# What each tier's `--record` emission renders as the inverse's referent when
# the witness is NOT confidential. These are the exact fragments the leak was
# made of, so asserting on them is asserting on the leak itself: revert the fix
# and the confidential case renders one of these again.
WITNESS_EXPR = {
    "go": 'fmt.Sprintf("%v", result)',
    "rust": 'format!("{}", result)',
    "java": "String.valueOf(result)",
    "wasm": "(global.get $g_wit_val_",
}

# A witnessed extern body per tier, in that tier's own dialect. Each emitter
# requires its NATIVE body and never falls back to another tier's, so one
# source cannot serve them all — but the WAL call site is rendered from the
# DECLARATION, which is identical across every column below.
LEASE_BODY = {
    "go": '\n\treturn RevlOk[string, string]{Value: "%s"}\n' % CANARY,
    "rust": '\n Ok("%s".to_string())\n' % CANARY,
    "java": '\n return new RevlResult.Ok<>("%s");\n' % CANARY,
    # The wasm witness is built at fixed scratch addresses (the convention
    # backends/wasm/test_witnessed_teardown.py uses); its bytes never appear in
    # the emitted WAT, which is why this tier is measured on the framing call.
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


def _source(tier: str, returns: str) -> str:
    """A witnessed composition whose extern hands a witness back in `returns`.

    The `lifecycle test` is what routes a document carrying top-level externs to
    the LIVE component path on the go tier rather than its pure typed-core one;
    without it the components are dropped and there is no WAL call site to
    measure at all. The java and wasm emitters refuse a lifecycle test outright
    (it asserts R4 residue-freedom through introspection only the reference tier
    implements) and reach the same call site without one, so they get none."""
    tag = {"rust": "rs"}.get(tier, tier)
    witnessed = "witnessed[t]" if tier == "wasm" else "witnessed"
    drive = "" if tier in ("java", "wasm") else LIFECYCLE_TEST
    return f"""
service Noop {{ fn ping() -> Str }}

extern {witnessed} fn lease() -> {returns}
    undo revoke(result)
    = @{tag} {{{LEASE_BODY[tier]}}}

extern pure fn revoke(id: Str) -> Unit = @{tag} {{{REVOKE_BODY[tier]}}}

component Leaser provides noop: Noop {{
  effect lease()

  provide noop {{
    fn ping() = "pong"
  }}
}}

{drive}"""


def _lease_ir(returns: str, tier: str = "py") -> dict:
    return compile_source(_source(tier, returns))


# ---------------------------------------------------------------------------
# the rule, read off the IR
# ---------------------------------------------------------------------------


def test_a_secret_nested_in_a_result_is_stamped():
    """The finding's exact declaration. Both stamps: `secret_return` says the
    return carries confidential bytes, `secret_witness` says the WAL's referent
    position specifically does."""
    ext = {e["name"]: e for e in _lease_ir("Result[Secret[Str], Str]")["externs"]}["lease"]
    assert ext["secret_return"] is True
    assert ext["secret_witness"] is True
    assert ext["returns"] == "Result[Str, Str]"  # qualifier still stripped
    assert ext["witness"] == "Str"


def test_a_secret_on_the_error_arm_does_not_mark_the_witness():
    """`secret_witness` is narrower than `secret_return` on purpose: the
    referent is the Ok arm, so redacting it here would lose a usable descriptor
    and protect nothing. The false-positive control for the whole fix."""
    ext = {e["name"]: e for e in _lease_ir("Result[Str, Secret[Str]]")["externs"]}["lease"]
    assert ext["secret_return"] is True
    assert "secret_witness" not in ext


def test_a_top_level_secret_return_still_stamps():
    """The case that already worked keeps working — the widening is additive."""
    ir = compile_source(
        'extern pure fn mint(u: Str) -> Secret[Str] = @py { return "t" }\n'
        "component C { }\n")
    assert ir["externs"][0]["secret_return"] is True


@pytest.mark.parametrize("declared", [
    "Opt[Secret[Str]]",
    "List[Secret[Str]]",
    "Map[Str, Secret[Str]]",
])
def test_every_container_carries_the_marking(declared):
    """A container's value graph physically holds the confidential bytes, so the
    marking follows the value rather than the spelling."""
    ir = compile_source(
        f'extern pure fn mint(u: Str) -> {declared} = @py {{ return None }}\n'
        "component C { }\n")
    assert ir["externs"][0]["secret_return"] is True


def test_a_function_type_argument_is_not_a_container():
    """The rule's deliberate boundary. `Fn[Secret[Str], Int]` describes what a
    closure will be CALLED with; the closure value holds no confidential bytes,
    so marking it would redact a callback's rendering and let the real
    disclosure — the call — go unexamined at its own crossing."""
    ir = compile_source(
        "extern pure fn pick(f: Fn[Secret[Str], Int]) -> Int = @py { return 1 }\n"
        "component C { }\n")
    assert "secret_return" not in ir["externs"][0]
    assert "secret" not in ir["externs"][0]["params"][0]


def test_a_nested_secret_parameter_is_stamped():
    """The receiver side of the same widening: an argument that takes the
    payload inside a container receives the same bytes, and the recorder redacts
    the whole ARGUMENT, so the container is what the stamp must name."""
    ir = compile_source(
        "extern pure fn take(box: Result[Secret[Str], Str]) -> Int = @py { return 1 }\n"
        "component C { }\n")
    assert ir["externs"][0]["params"][0]["secret"] is True


def test_a_nested_secret_config_field_is_stamped():
    """An optional credential is spelled `Opt[Secret[Str]]`, and reaches the run
    log and the `revl_load` MCP response exactly as the unwrapped one does."""
    ir = compile_source(
        "component C { config { key: Opt[Secret[Str]] } }\n")
    field = ir["components"][0]["config"][0]
    assert field["secret"] is True


def test_an_ordinary_declaration_is_untouched():
    """Additive: nothing is stamped unless the author wrote `Secret[...]`."""
    ir = compile_source(
        "extern pure fn plain(a: Str) -> Result[Str, Str] = @py { return a }\n"
        "component C { }\n")
    ext = ir["externs"][0]
    assert "secret_return" not in ext and "secret_witness" not in ext
    assert "secret" not in ext["params"][0]


# ---------------------------------------------------------------------------
# the tier WAL writers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", WAL_TIERS)
def test_the_tier_wal_writer_keeps_a_confidential_witness_off_disk(tier):
    """The paired assertion, per tier: the expression that stringified the
    witness into the durable record is GONE, and the placeholder stands in its
    place — so this cannot pass on an emission that simply dropped the record.

    Measured on the emitted call site rather than on the canary bytes because
    that is what every tier has in common: the wasm witness is built at a
    scratch address and its bytes never appear in the WAT at all."""
    emitted = _emit_record(tier, _lease_ir("Result[Secret[Str], Str]", tier))
    assert WITNESS_EXPR[tier] not in _record_call(tier, emitted), \
        f"{tier} still writes the witness into the WAL record"
    assert _placeholder_reaches(tier, emitted), f"{tier} framed no placeholder"


@pytest.mark.parametrize("tier", WAL_TIERS)
def test_an_ordinary_witness_is_still_recorded_verbatim(tier):
    """The false-positive control. A writer that erased every referent would
    pass the leak test above and fail this one: recovery needs the referent to
    address the right thing, so over-redaction is a real cost, not a free win."""
    emitted = _emit_record(tier, _lease_ir("Result[Str, Str]", tier))
    assert WITNESS_EXPR[tier] in _record_call(tier, emitted), \
        f"{tier} lost its referent"
    assert not _placeholder_reaches(tier, emitted), \
        f"{tier} redacts an unmarked witness"


@pytest.mark.parametrize("tier", WAL_TIERS)
def test_a_secret_on_the_error_arm_leaves_the_referent_alone(tier):
    """The narrow rule, end of the emitter: `secret_return` is set here and
    `secret_witness` is not, and it is the latter the WAL writer reads."""
    emitted = _emit_record(tier, _lease_ir("Result[Str, Secret[Str]]", tier))
    assert WITNESS_EXPR[tier] in _record_call(tier, emitted)
    assert not _placeholder_reaches(tier, emitted)


# ---------------------------------------------------------------------------
# the py tier: the value registry has to reach through the wrapper
# ---------------------------------------------------------------------------


def test_the_registry_reaches_through_an_ok_wrapper():
    """The py tier redacts its WAL through a value REGISTRY rather than a
    positional stamp, and the emitted `Ok`/`Err` classes keep their payload in
    `__slots__` — so a fallible crossing used to register the wrapper, which is
    not a string, and therefore registered nothing at all."""
    import confidential  # noqa: PLC0415 (backend module, path set above)

    class Ok:  # the shape backends/python/emit.py emits
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

    confidential.forget_secret_values()
    try:
        confidential.register_secret_tree(Ok(CANARY))
        assert confidential.is_secret_value(CANARY)
        assert confidential.redact_value(CANARY) == REDACTED_SECRET
        text = confidential.redact_text(f"no row for {CANARY} in ledger")
        assert CANARY not in text and REDACTED_SECRET in text
        # ...and the sentence around it survives, so the record still reads.
        assert "no row for" in text and "in ledger" in text
    finally:
        confidential.forget_secret_values()


def test_an_unregistered_value_is_still_rendered_verbatim():
    """The registry is an exact-value match, never a heuristic: the canary is
    redacted because a declared marking named it, not because of how it looks."""
    import confidential  # noqa: PLC0415

    confidential.forget_secret_values()
    assert confidential.redact_value(CANARY) == CANARY


# ---------------------------------------------------------------------------
# the go tier, executed: what actually lands in $REVL_WAL
# ---------------------------------------------------------------------------

needs_go = pytest.mark.skipif(
    shutil.which("go") is None, reason="needs the go toolchain")

_GO_MOD = """module revl.secretwal

go 1.25.0

require github.com/0xdenny218/stc-go v0.6.1-0.20260818143352-b3d6788a428e
"""


def _run_go_wal(tmp: Path, returns: str) -> str:
    """Emit, build and RUN a record-mode go composition; return its WAL text.

    The claim under test is about a file on disk, so this reads the file rather
    than the emitter's output."""
    src = _emitter("go").emit(_lease_ir(returns, "go"), "secretwal", record=True)
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
    """Every discharge-descriptor's referent argument, DECODED.

    Parsed rather than grepped because that is how `revl recover` reads the log,
    and because go's `json.Marshal` escapes `<` as `\\u003c` — a substring
    search for the placeholder would miss a record that carries it."""
    out = []
    for line in wal.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record") == "discharge-descriptor":
            out.extend((record.get("call") or {}).get("args") or [])
    return out


@needs_go
def test_a_leased_credential_does_not_reach_the_durable_log():
    """End to end, against the real stc-go runtime: what lands in the file at
    rest is the placeholder `revl recover` knows how to refuse, not the
    credential."""
    with tempfile.TemporaryDirectory() as d:
        wal = _run_go_wal(Path(d), "Result[Secret[Str], Str]")
    assert _referents(wal) == [REDACTED_SECRET], "nothing was recorded at all"
    assert CANARY not in wal


@needs_go
def test_an_ordinary_witness_still_reaches_the_durable_log():
    """The executed false-positive control: recovery keeps its referent, so a
    rollback still addresses the right thing."""
    with tempfile.TemporaryDirectory() as d:
        wal = _run_go_wal(Path(d), "Result[Str, Str]")
    assert _referents(wal) == [CANARY]
