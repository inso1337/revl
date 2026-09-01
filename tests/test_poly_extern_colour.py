"""caller-decided extern colour — roadmap item 388 (option a).

One `extern emission fn|async NAME(...)` — a colour-polymorphic extern, one
authored host body, colour decided at the CALL SITE. A sync `emission fn` caller
resolves it to a sync `def`/`function` clone (unawaited); an async `emission
async fn` caller resolves it to an `async def`/`async function` clone (awaited).
This is the EXTERN analog of item 342's arrow monomorphization: keep the
colouring fixpoint over CONCRETE externs, and split the poly extern into concrete
clones on demand, pruning the colour no call site requested.

The exit tests below are docs/design/388-caller-decided-extern-colour.md
§"Exit tests": additive byte-identity; a two-colour poly extern whose py+ts
goldens equal the two-hand-written-extern baseline byte-for-byte; A1 untouched;
the `await`-in-poly-body lint; and go/rust emitting ONE host function.
"""

import importlib.util
from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError


ROOT = Path(__file__).resolve().parents[1]


def _emit(backend: str, ir):
    spec = importlib.util.spec_from_file_location(
        f"revl_{backend}_emit_388", ROOT / "backends" / backend / "emit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.emit(ir)


# The @py/@ts body, authored once for the poly extern and twice (byte-identical)
# for the hand-written baseline it must reproduce.
_PY_BODY = '    return x + " ran"'
_TS_BODY = '    return x + " ran";'


def _poly_program() -> str:
    return (
        "extern emission fn|async engine_run(x: Str) -> Str\n"
        f"  = @py {{\n{_PY_BODY}\n  }}\n"
        f"  = @ts {{\n{_TS_BODY}\n  }}\n"
        "service ARun { emission async fn go(x: Str) -> Str }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
        "component AsyncAgent provides arun: ARun {\n"
        "  provide arun { async fn go(x) = engine_run(x) }\n"
        "}\n"
        "component SyncAgent provides srun: SRun {\n"
        "  provide srun { fn go(x) = engine_run(x) }\n"
        "}\n"
    )


def _baseline_program() -> str:
    # The two hand-written externs the poly extern collapses: an async
    # `engine_run` and a sync `engine_run_revl_sync` (the monomorph name the sync
    # call site resolves to), each carrying the SAME body, each called from the
    # matching-colour method. This is what item 388 deletes.
    return (
        "extern emission async fn engine_run(x: Str) -> Str\n"
        f"  = @py {{\n{_PY_BODY}\n  }}\n"
        f"  = @ts {{\n{_TS_BODY}\n  }}\n"
        "extern emission fn engine_run_revl_sync(x: Str) -> Str\n"
        f"  = @py {{\n{_PY_BODY}\n  }}\n"
        f"  = @ts {{\n{_TS_BODY}\n  }}\n"
        "service ARun { emission async fn go(x: Str) -> Str }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
        "component AsyncAgent provides arun: ARun {\n"
        "  provide arun { async fn go(x) = engine_run(x) }\n"
        "}\n"
        "component SyncAgent provides srun: SRun {\n"
        "  provide srun { fn go(x) = engine_run_revl_sync(x) }\n"
        "}\n"
    )


def _externs(ir):
    return {e["name"]: e for e in ir.get("externs", [])}


def _fn_names_in(component):
    names: set = set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("kind") == "fn":
                names.add(n.get("name"))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(component)
    return names


def _component(ir, name):
    return [c for c in ir["components"] if c["name"] == name][0]


# -- the split: one poly extern -> two concrete clones -----------------------

def test_two_colour_poly_extern_splits_into_two_concrete_externs():
    ir = compile_source(_poly_program(), "poly.rvl")
    externs = _externs(ir)
    assert set(externs) == {"engine_run", "engine_run_revl_sync"}
    # the async clone keeps the original name; the sync clone takes the suffix
    assert externs["engine_run"].get("async") is True
    assert not externs["engine_run_revl_sync"].get("async")
    # both carry the ONE authored body, byte-identical
    assert externs["engine_run"]["bodies"] == externs["engine_run_revl_sync"]["bodies"]
    # the internal poly marker is stripped from the final IR
    assert "colour_poly" not in externs["engine_run"]
    assert "colour_poly" not in externs["engine_run_revl_sync"]


def test_sync_caller_reaches_sync_clone_async_caller_reaches_async_clone():
    ir = compile_source(_poly_program(), "poly.rvl")
    async_calls = _fn_names_in(_component(ir, "AsyncAgent"))
    sync_calls = _fn_names_in(_component(ir, "SyncAgent"))
    assert "engine_run" in async_calls and "engine_run_revl_sync" not in async_calls
    assert "engine_run_revl_sync" in sync_calls and "engine_run" not in sync_calls


# -- the headline: py+ts goldens equal the hand-written baseline -------------

@pytest.mark.parametrize("backend", ["python", "typescript"])
def test_poly_extern_emits_byte_identical_to_two_hand_written_externs(backend):
    poly = _emit(backend, compile_source(_poly_program(), "poly.rvl"))
    baseline = _emit(backend, compile_source(_baseline_program(), "baseline.rvl"))
    assert poly == baseline


@pytest.mark.parametrize("backend", ["python", "typescript"])
def test_async_call_site_awaited_sync_call_site_not(backend):
    out = _emit(backend, compile_source(_poly_program(), "poly.rvl"))
    # the async clone is awaited; the sync clone is a plain call
    assert "await engine_run(x)" in out
    assert "await engine_run_revl_sync" not in out


# -- A1 is untouched ---------------------------------------------------------

def test_sync_caller_of_poly_extern_is_not_refused_and_emits_no_await():
    # the sync `emission fn` caller compiles (reaches the sync clone, not the
    # async one), and the emitted sync function contains no `await`.
    ir = compile_source(_poly_program(), "poly.rvl")
    py = _emit("python", ir)
    # isolate the sync clone's def body and assert it has no await
    lines = py.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip().startswith("def engine_run_revl_sync("))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and not lines[i][0].isspace()), len(lines))
    assert "await" not in "\n".join(lines[start:end])


# -- the guard: await in a poly body is refused ------------------------------

def test_await_in_poly_body_is_refused():
    bad = (
        "extern emission fn|async bad(x: Str) -> Str\n"
        "  = @py { return await x }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
        "component S provides srun: SRun { provide srun { fn go(x) = bad(x) } }\n"
    )
    with pytest.raises(RevlError, match="await|colour-agnostic"):
        compile_source(bad, "bad.rvl")


def test_await_as_identifier_substring_is_not_refused():
    ok = (
        "extern emission fn|async ok(x: Str) -> Str\n"
        "  = @py { awaited = x\n  return awaited }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
        "component S provides srun: SRun { provide srun { fn go(x) = ok(x) } }\n"
    )
    compile_source(ok, "ok.rvl")  # must not raise


# -- validity refusals reuse the emission-only site --------------------------

@pytest.mark.parametrize("src,match", [
    ("extern pure fn|async p(x: Str) -> Str = @py { return x }\n",
     "only valid on an `emission`"),
    ("extern emission async fn|async p(x: Str) -> Str = @py { return x }\n",
     "both `async` and `fn|async`"),
    ("extern emission deferred fn|async p(x: Str) -> Unit = @py { pass }\n",
     "cannot be `deferred`"),
])
def test_poly_validity_refusals(src, match):
    with pytest.raises(RevlError, match=match):
        compile_source(src, "x.rvl")


# -- additive: no poly extern -> byte-identical, no clone --------------------

def test_additive_a_plain_emission_extern_program_is_unchanged():
    plain = (
        "extern emission fn e(x: Str) -> Str = @py { return x } = @ts { return x }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
        "component S provides srun: SRun { provide srun { fn go(x) = e(x) } }\n"
    )
    ir = compile_source(plain, "plain.rvl")
    externs = _externs(ir)
    assert set(externs) == {"e"}
    assert "async" not in externs["e"] and "colour_poly" not in externs["e"]


def test_poly_extern_with_no_caller_emits_no_extern_entry():
    # additive: a poly extern nobody instantiates synthesizes neither clone.
    src = "extern emission fn|async e(x: Str) -> Str = @py { return x }\n"
    ir = compile_source(src, "none.rvl")
    assert not ir.get("externs")


def test_single_colour_instantiation_prunes_the_other_clone():
    # sync-only -> only the sync clone; async-only -> only the async clone.
    body = "  = @py { return x }\n"
    common = (
        "service ARun { emission async fn go(x: Str) -> Str }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
    )
    sync_only = (
        "extern emission fn|async e(x: Str) -> Str\n" + body + common
        + "component S provides srun: SRun { provide srun { fn go(x) = e(x) } }\n"
    )
    async_only = (
        "extern emission fn|async e(x: Str) -> Str\n" + body + common
        + "component A provides arun: ARun { provide arun { async fn go(x) = e(x) } }\n"
    )
    sync_externs = _externs(compile_source(sync_only, "s.rvl"))
    assert set(sync_externs) == {"e_revl_sync"}
    assert not sync_externs["e_revl_sync"].get("async")

    async_externs = _externs(compile_source(async_only, "a.rvl"))
    assert set(async_externs) == {"e"}
    assert async_externs["e"].get("async") is True


# -- colour-erasing tiers emit ONE host function -----------------------------

def _colour_erased_program() -> str:
    return (
        "extern emission fn|async engine_run(x: Str) -> Str\n"
        "  = @py { return x }\n"
        "  = @go { return x }\n"
        "  = @rs { return x.to_string() }\n"
        "  = @java { return x; }\n"
        "  = @wasm { local.get $x }\n"
        "service ARun { emission async fn go(x: Str) -> Str }\n"
        "service SRun { emission fn go(x: Str) -> Str }\n"
        "component AsyncAgent provides arun: ARun {\n"
        "  provide arun { async fn go(x) = engine_run(x) }\n"
        "}\n"
        "component SyncAgent provides srun: SRun {\n"
        "  provide srun { fn go(x) = engine_run(x) }\n"
        "}\n"
    )


@pytest.mark.parametrize("backend,marker", [
    ("go", "func engine_run"),
    ("rust", "fn engine_run"),
    ("java", "String engine_run("),  # the return-typed def signature, not a call
])
def test_colour_erasing_tier_emits_one_host_function(backend, marker):
    out = _emit(backend, compile_source(_colour_erased_program(), "ce.rvl"))
    text = out if isinstance(out, str) else "\n".join(out.values())
    # exactly one host function, and no dangling reference to the sync clone
    assert text.count(marker) == 1, text
    assert "engine_run_revl_sync" not in text


def test_wasm_emits_no_sync_clone_and_one_body_per_module():
    out = _emit("wasm", compile_source(_colour_erased_program(), "ce.rvl"))
    for name, module in out.items():
        # de-dup is per self-contained WAT module: no module carries both clones
        assert "engine_run_revl_sync" not in module
        assert module.count("(func $engine_run ") <= 1, (name, module)
