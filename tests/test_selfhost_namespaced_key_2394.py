"""Issue #2394: the self-host py, rust and java emitters mangle a namespaced
provision key as the reference does.

A qualified key (`acme::greeter`) is the wiring identity but never a host
identifier. The reference mangles it at the one place each emitter turns a key
into an identifier (issue #1914):

    _host_key(k) = k                                    if "::" not in k
                 = "__".join(p.replace("_", "_u") for p in k.split("::"))

so `acme::greeter` becomes `acme__greeter`, `acme::class` (a host keyword as a
part) becomes `acme__class`, and an unqualified key is unchanged. The NATIVE
lower must also carry the qualified key into the component tables: the reader
that builds the component header used to read only the trailing segment, so the
emitters never saw the qualification at all.

The issue's exit test is "each namespaced source matches the reference bytes on
py, rust and java, with no marker". py covers all four of the issue's sources.
rust and java do not: neither ports a `lifecycle test` section, which keeps
ISSUE_REPRODUCER, TWO_NAMESPACES and KEYWORD_PARTS off both tiers; rust also
drops a routed requirement (issue #2796), which keeps ROUTED_NAMESPACED off
rust; and java's legacy component path carries no `requires`, which keeps
TWO_NAMESPACES and ROUTED_NAMESPACED off java. On those pairs the document
carries `<<UNSUPPORTED-NAMESPACED-KEY:...>>` for every key the tier could not
verify, beside the marker naming the actual unported construct, so the
difference is loud rather than silent. Every other source is byte-compared on
each tier whose covered slice takes it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import compile_files  # noqa: E402

import test_namespaced_key_emit_1914 as namespaced  # noqa: E402

MARKERS = re.compile(r"<<UNSUPPORTED[^>]*>>")

TIER_BACKEND = {"py": "python", "rust": "rust", "java": "java"}

#: A provide-only namespaced pair whose key PARTS are host keywords on py/java
#: (`class`) and rust (`box`): the `__` join keeps the identifier legal.
PROVIDE_ONLY = """service Greeter { fn hello(name: Str) -> Str }

component ClassGreeter provides acme::class: Greeter {
  provide acme::class {
    fn hello(name) = "class " + name
  }
}

component BoxGreeter provides acme::box: Greeter {
  provide acme::box {
    fn hello(name) = "box " + name
  }
}
"""

#: Two flat keys whose host spellings collide with a namespaced pair. The
#: reference refuses the document; the port names the pair.
COLLISION = """service Greeter { fn hello(name: Str) -> Str }

component A provides acme::greeter: Greeter {
  provide acme::greeter { fn hello(name) = name }
}

component B provides acme__greeter: Greeter {
  provide acme__greeter { fn hello(name) = name }
}
"""

#: A `validated` emission reached through a namespaced requirement. Its whole
#: module import list is scanned over the UNREWRITTEN body, so the requirement
#: has to resolve by binding there too (review of #2394: the scan missed the
#: `validate_response` import and emitted a call to an unimported name). py only:
#: the rust and java references refuse a validated crossing by name.
VALIDATED_NS = """service Model { emission validated fn complete(h: Str) -> Str }

component Agent requires acme::model: Model {
  emit model.complete("p")
}
"""

#: A service method named `box` reaches the rust bridge proxy and dispatch as
#: well as the trait and impl; every one of those spellings is `box_`.
BOX_METHOD = """service S { fn box(n: Int) -> Int }
service Front { fn go(n: Int) -> Int }

component C provides s: S, front: Front {
  provide s { fn box(n) = n }
  provide front { fn go(n) = n }
}
"""

#: The documents each tier's covered self-host slice can compare byte for byte.
#: rust and java leave the four sources out that they cannot emit whole; the
#: module docstring names which and why.
COVERED = {
    "py": (namespaced.ISSUE_REPRODUCER, namespaced.KEYWORD_PARTS,
           namespaced.ROUTED_NAMESPACED, namespaced.WASM_PAIR, PROVIDE_ONLY,
           VALIDATED_NS, BOX_METHOD, namespaced.TWO_NAMESPACES),
    "rust": (namespaced.WASM_PAIR, PROVIDE_ONLY, BOX_METHOD),
    "java": (PROVIDE_ONLY, BOX_METHOD),
}

#: The sources whose namespaced keys reach the NATIVE chain (lower + emitter).
NATIVE = {
    "py": (namespaced.ISSUE_REPRODUCER, namespaced.ROUTED_NAMESPACED, PROVIDE_ONLY,
           BOX_METHOD),
    "rust": (namespaced.WASM_PAIR, PROVIDE_ONLY, BOX_METHOD),
    "java": (PROVIDE_ONLY, BOX_METHOD),
}


def _load_reference(tier: str):
    spec = importlib.util.spec_from_file_location(
        "ns2394_reference_" + tier,
        ROOT / "backends" / TIER_BACKEND[tier] / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_stage(rel: str) -> dict:
    ir = compile_files([str(ROOT / rel)])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "ns2394_emit_" + rel.replace("/", "_"), ROOT / "backends" / "python" / "emit.py")
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(emitter.emit(ir), rel.replace("/", "_") + ".py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def emitted():
    return {tier: _exec_stage("selfhost/emit_%s.rvl" % tier) for tier in TIER_BACKEND}


@pytest.fixture(scope="module")
def reference():
    return {tier: _load_reference(tier) for tier in TIER_BACKEND}


@pytest.fixture(scope="module")
def native():
    return _exec_stage("selfhost/compile.rvl")


def _source_ir(source: str):
    from revl.compiler import compile_source
    return compile_source(source)


@pytest.mark.parametrize("tier", sorted(COVERED))
@pytest.mark.parametrize("source", range(8), ids=lambda i: "doc%d" % i)
def test_emitter_matches_the_reference_on_a_namespaced_source(
        emitted, reference, tier, source):
    """A namespaced component document emits the reference's bytes, with no
    `<<UNSUPPORTED` marker, on the tiers whose covered slice takes it."""
    if source >= len(COVERED[tier]):
        pytest.skip("this tier's covered slice does not take the document")
    text = COVERED[tier][source]
    ir = _source_ir(text)
    want = reference[tier].emit(ir)
    got = emitted[tier]["emit_%s_src" % tier](ir)
    assert not MARKERS.findall(got), MARKERS.findall(got)
    assert got == want


def _write(text: str) -> Path:
    import tempfile
    dirpath = Path(tempfile.mkdtemp())
    path = dirpath / "doc.rvl"
    path.write_text(text, encoding="utf-8")
    return path




@pytest.mark.parametrize("tier", sorted(NATIVE))
@pytest.mark.parametrize("source", range(4), ids=lambda i: "doc%d" % i)
def test_native_chain_lowers_the_qualified_key_and_emits(
        native, reference, tier, source):
    """The native driver's IR carries the qualified key and the whole chain
    emits the reference's bytes, so the reader fix and the emitter fix are
    proven together."""
    if source >= len(NATIVE[tier]):
        pytest.skip("this tier's native slice does not take the document")
    text = NATIVE[tier][source]
    path = _write(text)
    got = native["compile_to_at"](path.read_text(encoding="utf-8"), tier, str(path))
    ir = compile_files([str(path)])
    assert got == reference[tier].emit(ir), got[:200]


@pytest.mark.parametrize("tier", sorted(TIER_BACKEND))
def test_a_host_identifier_collision_is_named_not_emitted(emitted, tier):
    """`a__b` beside `a::b` is the one collision the mangling cannot represent.
    The reference refuses the document; the port names the pair instead of
    emitting two providers under one identifier."""
    ir = _source_ir(COLLISION)
    with pytest.raises(Exception) as refused:
        _load_reference(tier).emit(ir)
    assert "rename one" in str(refused.value)
    got = emitted[tier]["emit_%s_src" % tier](ir)
    assert "<<UNSUPPORTED-HOST-KEY-COLLISION:acme__greeter>>" in got
