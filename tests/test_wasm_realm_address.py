"""The realm label is a capability ADDRESS on the wasm tier, not free text.

A realm placement lowers to the import/export namespace itself:

    isolate kv in realm("tenant_a")
      -> (import "coeffect:tenant_a/kv" "put" ...)
      -> (func (export "provide:tenant_a/kv.get") ...)

so the label is a component of the name a host binds a provider to and
resolves a provision through. That makes its grammar load-bearing in a way a
general `Str` is not, and it puts three separate obligations on this tier,
each pinned below:

1. the FRONTEND refuses a label that is not an address (`revl.parser`), so an
   author sees a rule rather than a silently repaired name;
2. the EMITTER re-asserts the same rule, because an IR document reaches
   `emit()` without necessarily having passed the frontend;
3. the emitted WAT escapes the label anyway (`_wat_string`), so even a label
   that got past both doors lands as *data* inside the string literal and
   cannot contribute s-expression structure. The `wat2wasm` case below is what
   makes that claim mechanical rather than visual.

`_assert_imports_within_requires` is checked against the RENDERED address for
the same reason: its key half only ever sees `kv`, and the realm half of the
authority address appears in no key set the emitter keeps.
"""

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

BACKEND = ROOT / "backends" / "wasm"

# The label from the reproducer: it closes the WAT string literal and opens an
# `(export ...)` of its own, i.e. it writes a second, freely-named export onto a
# function whose one legitimate export name is the capability address.
HOSTILE = 'a") (export "PWNED_EXPORT'

STORE = """
service KV { emission[kv] fn put(key: Str, value: Str) }
component Store requires kv: KV {
  isolate kv in realm("%s")
  emit kv.put("a", "b")
}
"""

PROVIDER = """
service KV { fn get(k: Str) -> Str }
component Store provides kv: KV {
  isolate kv in realm("%s")
  provide kv { fn get(k) = k }
}
"""


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(ir: dict):
    module = _emit_module()
    return module.emit(ir), module


def _retarget_realms(node, label: str) -> None:
    """Rewrite every `isolate` realm in an IR document, in place.

    The point of the bypass: `compile_source` will not produce this document,
    because the frontend refuses the label. An IR handed straight to the
    emitter can, which is exactly the reach the emitter's own re-assertion and
    the WAT escaper exist to cover.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "isolate" and isinstance(value, dict):
                node[key] = {k: label for k in value}
            else:
                _retarget_realms(value, label)
    elif isinstance(node, list):
        for item in node:
            _retarget_realms(item, label)


# ------------------------------------------------------------ 1. the frontend

@pytest.mark.parametrize("label", [
    HOSTILE,
    'x" "y',
    "a/b",          # `/` is the address's own realm/key separator
    "a b",
    "-leading",
    "a\tb",
])
def test_a_label_that_is_not_an_address_is_refused_at_parse(label):
    """The rule is a refusal the author can see, named in the diagnostic."""
    source = STORE % label.replace("\\", "\\\\").replace('"', '\\"')
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "realm.rvl")
    assert "invalid realm label" in str(excinfo.value)


@pytest.mark.parametrize("label", ["tenant_a", "tenant-a", "tenant.a", "w1", "0"])
def test_an_address_shaped_label_still_parses(label):
    """The restriction is conservative, not a break: `.`, `_`, `-` and digits
    all stay legal, which covers every realm label in this repo's corpus."""
    ir = compile_source(STORE % label, "realm.rvl")
    store = next(c for c in ir["components"] if c["name"] == "Store")
    assert store["isolate"] == {"kv": label}


# ------------------------------------------------------------- 2. the emitter

def test_the_emitter_refuses_a_hostile_label_from_a_hand_written_ir():
    """`emit()` is a door of its own — an IR document need never have passed
    the frontend — so the address rule is re-asserted here, named."""
    ir = compile_source(STORE % "tenant_a", "realm.rvl")
    _retarget_realms(ir, HOSTILE)
    module = _emit_module()
    with pytest.raises(module.EmitError) as excinfo:
        module.emit(ir)
    assert "capability address" in str(excinfo.value)
    assert "ASCII letters, digits" in str(excinfo.value)


def test_the_emitter_refuses_a_hostile_routed_realm():
    """A routed realm resolves to `realms[index] + "/" + key` on the substrate:
    the same address space, so the same rule."""
    ir = compile_source(STORE % "tenant_a", "realm.rvl")
    store = next(c for c in ir["components"] if c["name"] == "Store")
    store.pop("isolate", None)
    store["routes"] = {"kv": {"realms": ["w1", HOSTILE], "strategy": "round_robin"}}
    module = _emit_module()
    with pytest.raises(module.EmitError) as excinfo:
        module.emit(ir)
    assert "capability address" in str(excinfo.value)


# -------------------------------------- 3. the rendered address, and wat2wasm

def _render_with_label(source_template: str, label: str) -> str:
    """The emitted WAT for `source_template` with the two ADDRESS RULES
    suspended — the ingestion refusal and the finish-line address assertion.

    The suspension is deliberate and narrow. Both rules are pinned by their own
    tests above; suspending them here puts the emitter in the state it would be
    in if a hostile label had reached the render sites anyway, so the WAT
    escaper — the third obligation, independent of both refusals — is exercised
    rather than shadowed by the checks standing in front of it. Layer three is
    the one that has to hold if layers one and two are ever bypassed, which is
    the whole reason it is there.
    """
    module = _emit_module()
    ir = compile_source(source_template % "tenant_a", "realm.rvl")
    module._assert_realm_label = lambda *_args, **_kw: None
    module._assert_imports_within_requires = lambda *_args, **_kw: None
    _retarget_realms(ir, label)
    return module.emit(ir)["Store"]


def test_the_render_sites_escape_a_hostile_label_into_data():
    """Both address sites go through `_wat_string`. Restore either to a raw
    f-string interpolation and this fails: the label's `") (export "` closes
    the literal and this assertion finds `PWNED_EXPORT` outside it."""
    consumer = _render_with_label(STORE, HOSTILE)
    provider = _render_with_label(PROVIDER, HOSTILE)
    for wat in (consumer, provider):
        # every occurrence of the injected text is inside a string literal,
        # preceded by the escaped quote the label tried to close it with.
        assert 'PWNED_EXPORT' in wat
        assert '") (export "PWNED_EXPORT' not in wat
        assert '\\") (export \\"PWNED_EXPORT' in wat
    assert '(import "coeffect:a\\") (export \\"PWNED_EXPORT/kv"' in consumer
    assert '(func (export "provide:a\\") (export \\"PWNED_EXPORT/kv.get")' in provider


def _assemble(wat: str, tmp_path: Path, stem: str) -> Path:
    wat_path = tmp_path / f"{stem}.wat"
    wasm_path = tmp_path / f"{stem}.wasm"
    wat_path.write_text(wat)
    proc = subprocess.run(
        ["wat2wasm", "--enable-all", str(wat_path), "-o", str(wasm_path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, f"wat2wasm refused the module:\n{proc.stderr}"
    return wasm_path


def _boundary_sizes(wasm_path: Path) -> tuple[str, ...]:
    """The BINARY's own count of import and export entries.

    Read from `wasm-objdump`'s section header (`Import[1]:` / `Export[3]:`)
    rather than by counting `(export ` in text: the hostile label contains the
    literal text `) (export "`, so any textual count is confounded by the very
    string under test. The section header is the assembler's own tally.
    """
    proc = subprocess.run(["wasm-objdump", "-x", str(wasm_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return tuple(sorted(re.findall(r"^(Import|Export)\[(\d+)\]:", proc.stdout, re.M)))


@pytest.mark.skipif(any(shutil.which(t) is None for t in ("wat2wasm", "wasm-objdump")),
                    reason="wabt (wat2wasm/wasm-objdump) is not installed")
@pytest.mark.parametrize("template,stem", [(STORE, "consumer"), (PROVIDER, "provider")])
def test_wat2wasm_accepts_the_escaped_module(template, stem, tmp_path):
    """The mechanical half of the claim, stated by an assembler rather than by
    reading: wabt takes the module, and the binary it produces has exactly the
    boundary a clean realm produces — the label is one NAME, not structure.

    The clean/hostile comparison is what makes this non-vacuous. An injected
    `(export "PWNED_EXPORT")` is a real, extra entry in the binary's export
    section, so the two tallies would differ.
    """
    hostile = _assemble(_render_with_label(template, HOSTILE),
                        tmp_path, f"{stem}-hostile")
    clean = _assemble(_render_with_label(template, "tenant_a"),
                      tmp_path, f"{stem}-clean")
    assert _boundary_sizes(hostile) == _boundary_sizes(clean)
    # and the label really did travel through the boundary, as data.
    assert b"PWNED_EXPORT" in hostile.read_bytes()


# ------------------------- `_assert_imports_within_requires` on the ADDRESS

def test_the_import_assertion_reads_the_rendered_address():
    """Its key half sees `kv` whatever the realm is; the realm half of the
    address is only visible in the rendered string, so that is what it checks."""
    module = _emit_module()
    # a well-formed realmed address for a declared key passes.
    module._assert_imports_within_requires(
        "W", {"kv"}, {"kv"}, {"coeffect:tenant_a/kv"})
    # structure smuggled into the realm half is refused, even though the KEY
    # half is impeccable and the key check alone sees nothing wrong.
    with pytest.raises(module.EmitError) as excinfo:
        module._assert_imports_within_requires(
            "W", {"kv"}, {"kv"}, {'coeffect:a") (export "PWNED/kv'})
    assert "least-authority (289)" in str(excinfo.value)
    assert "capability address" in str(excinfo.value)


def test_the_import_assertion_refuses_an_undeclared_key_behind_a_realm():
    """A realm prefix does not launder an undeclared key past the subset leg."""
    module = _emit_module()
    with pytest.raises(module.EmitError) as excinfo:
        module._assert_imports_within_requires(
            "W", {"kv"}, {"kv"}, {"coeffect:tenant_a/net"})
    assert "least-authority (289)" in str(excinfo.value)
    assert "'net'" in str(excinfo.value)


def test_the_import_assertion_admits_the_reserved_wal_channel():
    """`coeffect:revl:wal` is a first-party host binding, keyed by no realm and
    named in no `requires` — allowed by name, not by widening the grammar."""
    module = _emit_module()
    module._assert_imports_within_requires(
        "W", {"kv"}, {"kv"}, {"coeffect:kv", module._WAL_IMPORT_MODULE})


def test_a_realmed_module_still_emits_and_assembles():
    """The ordinary path, unmoved: a legal realm renders its address and the
    assertion passes it."""
    modules, _ = _emit(compile_source(STORE % "tenant_a", "realm.rvl"))
    assert '(import "coeffect:tenant_a/kv" "put"' in modules["Store"]
