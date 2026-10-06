"""Capability-bound secrets — the taint-side bound-key guarantee (roadmap item
256, Slice 1).

The property, stated honestly (docs/design/256-capability-bound-secrets.md): the
provider key is injected only into its bound capability's extern bodies, as a
host-scope local, and the RETURN of a bound emission carries a distinct `secret`
origin that is refused at EVERY boundary crossing with NO declassifier. So no
revl construct and no declared crossing can carry the key out. What is NOT
covered — a host body splicing the injected key into its own outbound request —
is the G8 host-body residual, narrowed but not removed, and this suite pins that
the honest boundary is exactly there: a body that hands the key straight to its
own host call COMPILES AND RUNS.

Slice 1 is the flow half only; there is no bound-key TYPE (the earlier draft's
mechanism was vacuous for a host-scope local the checker never sees). The
guarantee is a FLOW guarantee: the `secret` origin, minted unconditionally at a
bound emission's return, refused at each of the five crossing kinds enumerated in
§4a.2, with the reach-completeness row in `tests/test_reach_completeness.py` as
the guardrail against a fold that visits one crossing and misses another.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from revl import RevlError
from revl.compiler import compile_source
from revl.diagnostics import classify, explain
from revl.run import _resolve_secrets, _secret_rows

ROOT = Path(__file__).resolve().parents[1]


def _pyemit():
    """The cordis-py emitter, loaded from the backend (as `test_goldens` does)."""
    spec = importlib.util.spec_from_file_location(
        "_pyemit_256", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# a bound secret and the emission extern it is confined to. `complete`'s return
# is minted `secret` unconditionally (§4a.1), so `let s = emit complete(u)` puts a
# `secret`-origin value into the revl value graph — the thing that must never
# cross.
_PRELUDE = (
    "secret openai_key for model.complete\n"
    "extern emission[model.complete] fn complete(p: Str) -> Str = @py { return p }\n"
)


def _agent(body: str, extra: str = "", sig: str = "fn go(u: Str) -> Int",
           params: str = "u", reqs: str = "") -> str:
    """An `Agent` whose `ops.go` provide method runs `body`."""
    return (
        _PRELUDE + extra
        + "service Ops { emission " + sig + " }\n"
        + "component Agent " + reqs + "provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(" + params + ") {\n" + body + "\n      return 0\n    }\n"
        + "  }\n}\n"
    )


def _refuses(src: str, filename: str = "secret.rvl") -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, filename)
    err = excinfo.value
    assert getattr(err, "code", None) == "G-SECRET", (
        f"expected G-SECRET, got {getattr(err, 'code', None)}: {err.message}")
    return err


# ===========================================================================
# 1. The five crossing kinds (§4a.2) — one test each. Each is a DISTINCT code
#    path in taint.py; the reach-completeness row asserts the set is complete.
# ===========================================================================

def test_kind1_emit_arm_refuses_a_secret():
    """The `emit` arm: a `secret` crossing an emission is refused, not merely
    recorded (the crossing today only folds outbound origins into `reaches`)."""
    err = _refuses(_agent(
        "      let s = emit complete(u)\n      emit snk.out(s)",
        extra="service Sink { emission fn out(s: Str) -> Int }\n",
        reqs="requires snk: Sink "))
    assert "emission" in err.message


def test_kind2_plain_extern_call_refuses_a_secret():
    """The plain (non-declared-sink, non-source) extern call: an ordinary host
    extern must not receive the bound key."""
    err = _refuses(_agent(
        "      let s = emit complete(u)\n      let x = emit host_sink(s)",
        extra="extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"))
    assert "extern" in err.message


def test_kind3_unnameable_indirect_callable_refuses_a_secret():
    """The unnameable indirect / `*` callable: a first-class function value revl
    cannot name must refuse a `secret` argument — independently of `any_sink`
    (what cannot be named cannot be proven to re-emit through the bound cap)."""
    err = _refuses(_agent(
        "      let s = emit complete(u)\n      let y = cb(s)",
        sig="fn go(cb: (Str) -> Int, u: Str) -> Int", params="cb, u"))
    assert "first-class" in err.message


def test_kind4_provide_method_return_refuses_a_secret():
    """The provide-method return across the service / MCP bridge: a method that
    returns a `secret`-carrying value hands the key across the boundary."""
    src = (
        _PRELUDE
        + "service Ops { emission fn go(u: Str) -> Str }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      return emit complete(u)\n    }\n  }\n}\n")
    err = _refuses(src)
    assert "provide-method return" in err.message


def test_kind5_secret_nested_in_a_record_is_not_laundered():
    """A `secret` nested in a record/variant/generic rides the value-graph joins
    and is caught at whichever crossing the container reaches (no new raise — the
    container's origin union carries `secret`)."""
    # nested in a record, whole record handed to an extern
    _refuses(_agent(
        "      let s = emit complete(u)\n      let r = { key: s, tag: \"t\" }\n"
        "      let x = emit host_box(r)",
        extra="type Box = { key: Str, tag: Str }\n"
              "extern emission fn host_box(b: Box) -> Int = @py { return 0 }\n"))


def test_kind5_secret_through_a_generic_round_trip_is_not_laundered():
    """A generic `id(secret)` round-trip does not erase the origin: taint rides
    the value, not the declared type (the A2 no-launder-through-generic case)."""
    _refuses(_agent(
        "      let s = emit complete(u)\n      let g = id(s)\n      let x = emit host_sink(g)",
        extra="fn id(x: Str) -> Str { return x }\n"
              "extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"))


# ===========================================================================
# 2. Reflection out of a host body, refused at the first downstream crossing.
# ===========================================================================

def test_reflected_key_is_refused_at_the_first_downstream_crossing():
    """A1: a host body reflects the key into its return; the reflected value
    carries `secret` (minted on the bound emission's return) and is refused at the
    first crossing it reaches downstream, whichever kind that is."""
    # the body "returns the key"; revl code then tries to emit it further
    err = _refuses(_agent(
        "      let leaked = emit complete(u)\n      let x = emit host_sink(leaked)",
        extra="extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"))
    assert "bound provider key" in err.message


# ===========================================================================
# 3. The ONE allowed crossing (§4b): re-emission through the SAME capability.
# ===========================================================================

def test_same_capability_reemission_is_allowed():
    """The one crossing that does not refuse: re-entry into an extern body of the
    same bound capability (a legitimate same-capability retry). `complete` is the
    bound emission, so passing the key back into it is admitted — it returns via
    the `secret` source before any crossing raise."""
    # compiles: the secret is threaded back into the same bound capability
    compile_source(_agent("      let s = emit complete(u)\n      let s2 = emit complete(s)"),
                   "reemit.rvl")


# ===========================================================================
# 4. No declassifier (§4a.3): `endorse[secret]` AND the verified-fn launder.
# ===========================================================================

def test_endorse_secret_is_refused_unconditionally():
    """`endorse[secret]` is refused before the declared-slot check — no
    declaration can ever grant a downgrade for a bound key."""
    err = _refuses(_agent(
        "      let s = emit complete(u)\n"
        "      let c = endorse[secret](s, reason = \"trust me\")\n"
        "      let x = emit host_sink(c)",
        extra="extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"))
    assert "no declassifier" in err.message


def test_verified_fn_declassifier_does_not_launder_a_secret():
    """A `verified fn` returning `Trusted[T]` — the parser-declassifier — does NOT
    clean a `secret`-carrying argument; the crossing is refused."""
    _refuses(_agent(
        "      let s = emit complete(u)\n      let c = wash(s)\n      let x = emit host_sink(c)",
        extra="verified fn wash(x: Str) -> Trusted[Str] { return x }\n"
              "extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"))


# ===========================================================================
# 5. The honest G8 boundary: a host body that hands the key to its OWN host call
#    is NOT refused — it compiles and RUNS. This is the residual the design names
#    precisely and never oversells (A3, OPEN).
# ===========================================================================

_HONEST_G8 = (
    "secret api_key for net.send\n"
    "extern emission[net.send] fn send(m: Str) -> Int "
    "= @py { return len(m) + len(api_key) }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component A provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let n = emit send(u)\n      return 0\n    }\n  }\n}\n"
)


def test_host_body_using_the_injected_key_in_its_own_call_compiles():
    """The key is a host-scope local the body hands straight to its provider call;
    it never becomes a revl value, so nothing crosses and the program compiles."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    assert ir.get("secrets") == [{"name": "api_key", "capability": "net.send"}]


def test_host_body_using_the_injected_key_actually_runs():
    """And it RUNS: the emitter injects `api_key = _revl_secret("api_key")` as the
    first body local, the driver installs the value at plug, and the body reads
    it. This is the honest boundary — revl does not see inside the body."""
    backend = str(ROOT / "backends" / "python")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    source = _pyemit().emit(compile_source(_HONEST_G8, "honest.rvl"))
    assert "def _revl_secret" in source
    assert "api_key = _revl_secret('api_key')" in source
    mod = types.ModuleType("_revl256_run")
    sys.modules["_revl256_run"] = mod
    exec(compile(source, "_revl256_run.py", "exec"), mod.__dict__)
    mod._REVL_SECRETS.update({"api_key": "sk-secret-value"})
    # len("hi") + len("sk-secret-value") == 2 + 15
    assert mod.send("hi") == 17


def test_the_helper_is_fail_loud_and_never_names_the_value():
    """No installed value is a HARD error at the extern call, naming the secret,
    never guessing a default and never leaking the value (there is no defaults
    path for a secret)."""
    source = _pyemit().emit(compile_source(_HONEST_G8, "honest.rvl"))
    mod = types.ModuleType("_revl256_faill")
    sys.modules["_revl256_faill"] = mod
    exec(compile(source, "_revl256_faill.py", "exec"), mod.__dict__)
    with pytest.raises(RuntimeError) as excinfo:
        mod.send("hi")
    assert "api_key" in str(excinfo.value)
    assert "sk-" not in str(excinfo.value)  # value never installed, never named


# ===========================================================================
# 6. Byte-identity: a secret-free program is untouched.
# ===========================================================================

_SECRET_FREE = (
    "extern emission[net.send] fn send(m: Str) -> Int = @py { return 0 }\n"
    "service Ops { emission fn go(u: Str) -> Int }\n"
    "component A provides ops: Ops {\n  provide ops {\n"
    "    fn go(u) {\n      let n = emit send(u)\n      return 0\n    }\n  }\n}\n"
)


def test_secret_free_program_is_byte_identical():
    """A program with no secret engages nothing: no `secrets` IR key, and the
    emitter emits neither the `_REVL_SECRETS` map nor the `_revl_secret` helper."""
    ir = compile_source(_SECRET_FREE, "free.rvl")
    assert "secrets" not in ir
    source = _pyemit().emit(ir)
    assert "_REVL_SECRETS" not in source
    assert "_revl_secret" not in source


# ===========================================================================
# 7. Emit-side "nowhere else": the key is injected ONLY into the bound extern.
# ===========================================================================

def test_key_is_injected_only_into_the_bound_extern_body():
    """The injection is a property of the extern-emitter loop, gated on capability
    match: a DIFFERENT-capability extern and a component method body get no
    `_revl_secret` call, so the name resolves nowhere else (§3)."""
    src = (
        "secret api_key for net.send\n"
        "extern emission[net.send] fn send(m: Str) -> Int "
        "= @py { return len(api_key) }\n"
        "extern emission[fs.write] fn write_fs(p: Str) -> Int = @py { return 0 }\n"
        "service Ops { emission fn go(u: Str) -> Int }\n"
        "component A provides ops: Ops {\n  provide ops {\n"
        "    fn go(u) {\n      let n = emit send(u)\n      let w = emit write_fs(u)\n"
        "      return 0\n    }\n  }\n}\n")
    ir = compile_source(src, "nowhere.rvl")
    externs = {e["name"]: e for e in ir["externs"]}
    assert externs["send"].get("secrets") == ["api_key"]
    assert "secrets" not in externs["write_fs"]  # different capability, not bound
    source = _pyemit().emit(ir)
    # exactly one injection site, inside `send`
    assert source.count("_revl_secret('api_key')") == 1


# ===========================================================================
# 8. lower.py cross-index refusals.
# ===========================================================================

def test_secret_on_a_wasm_only_capability_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(
            "secret k for net.send\n"
            "extern emission[net.send] fn send(m: Str) -> Int "
            "= @wasm { (i32.const 0) }\n"
            "service Ops { fn go(u: Str) -> Int }\n"
            "component A provides ops: Ops {\n  provide ops {\n"
            "    fn go(u) = 0\n  }\n}\n", "wasm.rvl")
    assert "wasm" in excinfo.value.message


def test_secret_name_colliding_with_a_parameter_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(
            "secret m for net.send\n"
            "extern emission[net.send] fn send(m: Str) -> Int = @py { return 0 }\n"
            "service Ops { fn go(u: Str) -> Int }\n"
            "component A provides ops: Ops {\n  provide ops {\n"
            "    fn go(u) = 0\n  }\n}\n", "collide.rvl")
    assert "collides" in excinfo.value.message


def test_secret_binding_no_emission_extern_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(
            "secret k for net.send\n"
            "service Ops { fn go(u: Str) -> Int }\n"
            "component A provides ops: Ops {\n  provide ops {\n"
            "    fn go(u) = 0\n  }\n}\n", "nobind.rvl")
    assert "no emission extern" in excinfo.value.message


# ===========================================================================
# 9. The IR / manifest surface carries the NAME, never the value.
# ===========================================================================

def test_ir_carries_name_and_capability_only_never_a_value():
    ir = compile_source(_HONEST_G8, "honest.rvl")
    rows = ir["secrets"]
    assert rows == [{"name": "api_key", "capability": "net.send"}]
    # there is no value anywhere in the row (it lives only in the driver at run)
    for row in rows:
        assert set(row) == {"name", "capability"}


# ===========================================================================
# 10. The driver resolves values out of band; never logs / echoes them.
# ===========================================================================

def test_driver_resolves_from_a_supplied_store_then_the_environment():
    ir = compile_source(_HONEST_G8, "honest.rvl")
    assert _secret_rows(ir) == [{"name": "api_key", "capability": "net.send"}]
    # a supplied store wins
    assert _resolve_secrets(ir, {"api_key": "v1"}) == {"api_key": "v1"}
    # else the environment, keyed REVL_SECRET_<NAME>
    os.environ["REVL_SECRET_API_KEY"] = "envval"
    try:
        assert _resolve_secrets(ir) == {"api_key": "envval"}
    finally:
        del os.environ["REVL_SECRET_API_KEY"]


def test_driver_is_fail_loud_with_no_value_and_never_names_it():
    ir = compile_source(_HONEST_G8, "honest.rvl")
    os.environ.pop("REVL_SECRET_API_KEY", None)
    with pytest.raises(RuntimeError) as excinfo:
        _resolve_secrets(ir)
    assert "api_key" in str(excinfo.value)
    assert "REVL_SECRET_API_KEY" in str(excinfo.value)  # the operator's fix


def test_secret_free_composition_resolves_no_secrets():
    ir = compile_source(_SECRET_FREE, "free.rvl")
    assert _resolve_secrets(ir) == {}


# ===========================================================================
# 11. The G-SECRET diagnostic is registered.
# ===========================================================================

def test_g_secret_is_a_registered_diagnostic():
    record = explain("G-SECRET")
    assert record["ok"] and record["guarantee"] and record["fix"]
    err = _refuses(_agent(
        "      let s = emit complete(u)\n      let x = emit host_sink(s)",
        extra="extern emission fn host_sink(s: Str) -> Int = @py { return 0 }\n"))
    assert classify(err)["code"] == "G-SECRET"


# ===========================================================================
# 12. Issue #1936 part 2: the binding resolves AT CALL TIME.
#
# §3's plug-time resolution is the default and stays strict: a declared secret
# with no resolvable value refuses the boot, naming the secret and never a
# value. #1936's second defect is the other half of that — the operator who
# configures a mailbox AFTER start, or rotates a password, must be able to use
# the binding. So a composition can opt in to booting with the name unset
# (`secrets_may_be_absent`) and install it afterwards through
# `_Driver.set_secret`. That seam writes the SAME name-keyed map plug-time
# resolution writes, so the audit token still names the real binding — never a
# locator and never a value (§5a). What it does NOT do is invent a second
# mechanism: the emitted body already reads the map on every call, which is what
# makes an install-after-start and a later rotation visible without a re-plug.
# ===========================================================================


def test_absent_at_plug_is_an_explicit_opt_in_and_the_default_still_refuses():
    """`allow_absent` is an explicit opt-in, and the default is unchanged: with
    it an unresolvable name is left for the call to report, without it the plug
    still refuses, naming the secret and never a value."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    os.environ.pop("REVL_SECRET_API_KEY", None)
    # the opt-in leaves the name unset instead of raising
    assert _resolve_secrets(ir, None, allow_absent=True) == {}
    # a value that IS resolvable is still resolved, in both modes
    assert _resolve_secrets(ir, {"api_key": "v1"}, allow_absent=True) == {
        "api_key": "v1"}
    # the default is byte-identically strict, and still never names the value
    with pytest.raises(RuntimeError) as excinfo:
        _resolve_secrets(ir, None)
    assert "api_key" in str(excinfo.value)
    assert "REVL_SECRET_API_KEY" in str(excinfo.value)  # the operator's fix


def test_the_opt_in_cannot_excuse_a_name_the_document_does_not_declare():
    """It is a flag for the whole document, never a pattern over names, so it
    cannot install a binding no `secret ... for ...` declared: a composition
    with no bound secret resolves to nothing either way, and a name the document
    does not declare is not a name the opt-in can excuse."""
    free = compile_source(_SECRET_FREE, "free.rvl")
    assert _resolve_secrets(free, None, allow_absent=True) == {}
    assert _resolve_secrets(free, {"anything": "v"}, allow_absent=True) == {}


# The same availability gate test_424_b1_interposition.py / test_run.py use.
try:  # noqa: SIM105
    import cordis  # noqa: F401
    _HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover -- depends on the interpreter
    _HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not _HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")


def _driver(ir: dict, **kwargs):
    """A `_Driver` on the real cordis-py backend, wired exactly as
    `run_command` wires it (the same helper test_router_runtime.py uses)."""
    from revl._paths import backends_root, python_backend_emitter  # noqa: PLC0415
    from revl.run import _Driver  # noqa: PLC0415

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    return _Driver(ir, {}, python_backend_emitter(), runtime_mod, Context,
                   FiberState, **kwargs)


@needs_cordis
def test_a_composition_opted_into_post_plug_resolution_boots_and_refuses_at_the_call():
    """The defect, restated as a test: with no value configured the composition
    still BOOTS, the driver installs nothing, and the CALL is the fail-loud gate
    — naming the secret and never a value. Before #1936 part 2 this refused at
    plug, so a key the operator had not configured yet made the composition
    unstartable."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    os.environ.pop("REVL_SECRET_API_KEY", None)
    driver = _driver(ir, secrets_may_be_absent=True)
    module = driver._emit_module(ir)          # the plug that used to refuse
    assert module._REVL_SECRETS == {}
    with pytest.raises(RuntimeError) as excinfo:
        module.send("hi")
    assert "api_key" in str(excinfo.value)
    assert "sk-" not in str(excinfo.value)


@needs_cordis
def test_the_driver_seam_installs_and_rotates_the_key_the_next_call_reads():
    """`set_secret` writes the map the emitted body reads on EVERY call, so an
    install after start and a later rotation are both visible to the NEXT call —
    no re-plug, and the binding's name never changes."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    os.environ.pop("REVL_SECRET_API_KEY", None)
    driver = _driver(ir, secrets_may_be_absent=True)
    module = driver._emit_module(ir)
    first = driver.set_secret("api_key", "sk-first")
    assert first["action"] == "install"
    assert module.send("hi") == 2 + len("sk-first")
    second = driver.set_secret("api_key", "sk-second-longer")
    assert second["action"] == "rotate"
    assert module.send("hi") == 2 + len("sk-second-longer")


@needs_cordis
def test_a_plug_resolved_key_rotates_through_the_same_seam():
    """The strict plug path and the seam write the same map, so a key resolved at
    plug rotates through `set_secret` too — and that first write is a ROTATION,
    not an install, because a value was already in force."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    driver = _driver(ir, secrets={"api_key": "sk-plug"})
    module = driver._emit_module(ir)
    assert module.send("hi") == 2 + len("sk-plug")
    record = driver.set_secret("api_key", "sk-rotated")
    assert record["action"] == "rotate"
    assert module.send("hi") == 2 + len("sk-rotated")


@needs_cordis
def test_the_seam_is_name_only_and_refuses_an_undeclared_or_blank_value():
    """§5a: the audit surface names the secret and never the value. The seam is
    strict on the NAME — an undeclared name has no binding, so it is refused
    rather than silently installed — and refuses a blank value without echoing
    it. Neither refusal installs anything."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    driver = _driver(ir, secrets_may_be_absent=True)
    module = driver._emit_module(ir)
    with pytest.raises(ValueError) as undeclared:
        driver.set_secret("not_declared", "sk-leak")
    assert "not_declared" in str(undeclared.value)
    assert "sk-leak" not in str(undeclared.value)
    with pytest.raises(ValueError) as blank:
        driver.set_secret("api_key", "")
    assert "api_key" in str(blank.value)
    assert module._REVL_SECRETS == {} and driver.secret_events == []
    record = driver.set_secret("api_key", "sk-live")
    # the record names the binding; the value appears nowhere in it
    assert set(record) == {"name", "capability", "action", "generation"}
    assert record["capability"] == "net.send"
    assert "sk-live" not in repr(driver.secret_events)
    assert "sk-live" not in repr(ir)


@needs_cordis
def test_the_opt_in_is_off_by_default_so_a_missing_key_still_refuses_the_boot():
    """No opt-in: a declared secret with no value still refuses at plug, exactly
    as it did before #1936 — the seam is never reached by accident."""
    ir = compile_source(_HONEST_G8, "honest.rvl")
    os.environ.pop("REVL_SECRET_API_KEY", None)
    driver = _driver(ir)
    with pytest.raises(RuntimeError) as excinfo:
        driver._emit_module(ir)
    assert "api_key" in str(excinfo.value)
