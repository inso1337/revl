"""Roadmap item 44 — delivery semantics: idempotency as a checked IR property.

`emission idempotent fn put(…)` promotes idempotency from a header comment to
a checked flag. The IR carries it, the parser refuses it on anything that is
not an emission, `revl import openapi` writes it for PUT/DELETE (RFC 9110
§9.2.2) exactly the way it writes `safe` for GET/HEAD/OPTIONS/TRACE, and the
python runtime consults it before deciding whether a transient failure of an
emission may be auto-retried.

`commutative` (Def. 39) is the precedent: an algebraic property the author
declares, carried into the IR, consumed by tiers. Idempotency is its sibling,
and the one distributed systems actually bleed over — retrying a non-
idempotent emission can double its effect, so without the checked flag the
runtime has no right to retry anything. `docs/delivery-semantics.md` is the
design note; this file pins the four promises the roadmap names: the IR
marks it, OpenAPI imports it, a non-emission cannot claim it, and the py
tier emits it.
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# backends/python stays on sys.path for the EXEC'D modules (`from runtime
# import ...`) — emit itself is loaded by path below, NOT via this entry
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402
from revl.import_openapi import import_openapi  # noqa: E402

emit = backend_emitter("python")


# ------------------------------------------------------------ (a) the IR mark

def test_idempotent_emission_is_marked_in_the_ir():
    ir = compile_source(
        "service S { emission idempotent fn put(k: Str, v: Str) -> Int }")
    put = ir["services"]["S"]["methods"]["put"]
    assert put["emission"] is True
    assert put["idempotent"] is True
    # the property is tier-3: a document that carries it must say so
    assert ir["ir_version"] == 3


def test_bare_emission_carries_no_idempotent_flag():
    ir = compile_source("service S { emission fn fire() -> Int }")
    assert "idempotent" not in ir["services"]["S"]["methods"]["fire"]


def test_modifier_order_is_free_but_the_emission_requirement_is_not():
    ir = compile_source("service S { idempotent emission fn put() -> Int }")
    assert ir["services"]["S"]["methods"]["put"]["idempotent"] is True


# --------------------------------------------------- (c) the parser's refusal

def test_idempotent_on_a_plain_fn_is_refused():
    with pytest.raises(RevlError, match="only meaningful on an `emission` operation"):
        compile_source("service S { idempotent fn f() }")


def test_idempotent_alongside_async_still_requires_emission():
    with pytest.raises(RevlError, match="only meaningful on an `emission` operation"):
        compile_source("service S { async idempotent fn f() }")


def test_the_refusal_names_the_fix():
    with pytest.raises(RevlError) as excinfo:
        compile_source("service S { idempotent fn f() }")
    assert "nothing to re-deliver" in str(excinfo.value)


# ------------------------------------------------ (b) OpenAPI import evidence

def _openapi(methods: list[str]) -> dict:
    paths = {"/thing": {m: {"responses": {"200": {"description": "ok"}}}
                        for m in methods}}
    return {"openapi": "3.0.3",
            "info": {"title": "Probe", "version": "1.0.0"},
            "paths": paths}


@pytest.mark.parametrize("method", ["put", "delete"])
def test_put_and_delete_import_as_idempotent_emissions(method):
    source = import_openapi(_openapi([method]), filename="probe.json")
    name = f"{method}_thing"
    # the claim is written next to the operation, like the `safe` claim is
    assert "`idempotent` by RFC 9110 §9.2.2" in source
    assert f"emission idempotent fn {name}()" in source
    # and it is a checked IR property once the source compiles
    op = compile_source(source)["services"]["Probe"]["methods"][name]
    assert op["emission"] is True
    assert op["idempotent"] is True


@pytest.mark.parametrize("method", ["post", "patch"])
def test_post_and_patch_import_without_idempotency(method):
    source = import_openapi(_openapi([method]), filename="probe.json")
    name = f"{method}_thing"
    assert f"emission fn {name}()" in source
    # the header documents the RFC rule for PUT/DELETE, so key on the
    # *declaration* and the per-operation claim, not on the bare phrase
    assert f"emission idempotent fn {name}()" not in source
    assert "`idempotent` by RFC 9110 §9.2.2" not in source
    op = compile_source(source)["services"]["Probe"]["methods"][name]
    assert op["emission"] is True
    assert "idempotent" not in op


def test_a_put_weakened_to_plain_imports_without_the_delivery_claim():
    """The delivery claim only rides on an operation that actually imports as
    an emission — a verb the importing engineer weakened to plain `fn` has no
    delivery, so it earns no retry right."""
    source = import_openapi(_openapi(["put"]), filename="probe.json",
                            pure=["put_thing"])
    assert "fn put_thing()" in source
    assert "emission idempotent fn put_thing()" not in source
    assert "`idempotent` by RFC 9110" not in source
    op = compile_source(source)["services"]["Probe"]["methods"]["put_thing"]
    assert op["emission"] is False
    assert "idempotent" not in op


# ----------------------------------------------------- (d) the py tier emits

_LIFECYCLE_SOURCE = """
service Store {
  fn get(key: Str) -> Str
  emission idempotent fn put(key: Str, value: Str) -> Int
}

component Kv provides kv: Store {
  provide kv {
    fn get(key) = "v"
    fn put(key, value) { return 1 }
  }
}

lifecycle test "round trip" {
  load Kv
  call kv.put("k", "v")
  unload Kv
  assert no_residue
}
"""


def test_the_marker_survives_emission_on_the_py_tier():
    ir = compile_source(_LIFECYCLE_SOURCE)
    source = emit.emit(ir)
    # the service interface carries the checked flag into the runtime metadata
    assert "'idempotent': True" in source
    # the driver receives the retry-eligibility map for the provision it calls
    assert "_REVL_IDEMPOTENT = {'kv': {'put'}}" in source
    # the reference runtime's retry helper is what the driver invokes
    assert "retry_idempotent" in source
    # and the emitted module really carries the flag
    ns = {}
    exec(compile(source, "emitted.py", "exec"), ns)
    put = ns["SERVICES"]["Store"]["put"]
    assert put["emission"] is True
    assert put["idempotent"] is True


def test_a_non_idempotent_emission_is_not_in_the_retry_map():
    source = emit.emit(compile_source("""
service Store {
  emission fn fire() -> Int
}

component Kv provides kv: Store {
  provide kv { fn fire() { return 1 } }
}

lifecycle test "t" {
  load Kv
  call kv.fire()
  unload Kv
  assert no_residue
}
"""))
    # the map is still emitted (so `_revl_call` can consult it) — and empty:
    # without the checked property, no emission earns a retry right
    assert "_REVL_IDEMPOTENT = {}" in source


# ---------------------------------------- the retry right (reference runtime)

def _runtime():
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    import runtime
    return runtime


def test_an_idempotent_emission_is_retried_on_transient_failure():
    rt = _runtime()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise rt.TransientError("dropped")
        return "ok"

    result = asyncio.run(rt.retry_idempotent(flaky, idempotent=True, attempts=3))
    assert result == "ok"
    assert calls["n"] == 3


def test_a_non_idempotent_emission_gets_exactly_one_attempt():
    rt = _runtime()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise rt.TransientError("dropped")

    with pytest.raises(rt.TransientError):
        asyncio.run(rt.retry_idempotent(flaky, idempotent=False))
    assert calls["n"] == 1


def test_a_real_error_is_never_retried_even_when_idempotent():
    rt = _runtime()
    calls = {"n": 0}

    def hard():
        calls["n"] += 1
        raise ValueError("real error")

    with pytest.raises(ValueError):
        asyncio.run(rt.retry_idempotent(hard, idempotent=True, attempts=3))
    assert calls["n"] == 1


def test_budget_exhaustion_re_raises_the_transient_failure():
    rt = _runtime()
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise rt.TransientError("always")

    with pytest.raises(rt.TransientError):
        asyncio.run(rt.retry_idempotent(always, idempotent=True, attempts=3))
    assert calls["n"] == 3


def test_an_awaitable_call_is_retried_and_awaited():
    rt = _runtime()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise rt.TransientError("dropped")
        return "async-ok"

    result = asyncio.run(rt.retry_idempotent(flaky, idempotent=True, attempts=3))
    assert result == "async-ok"
    assert calls["n"] == 2
