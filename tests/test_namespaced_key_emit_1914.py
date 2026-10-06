"""Issue #1914: a namespaced provision key (`ns::key`) runs on every tier.

`docs/namespacing.md` makes the QUALIFIED string the wiring identity: two
independently written trees that each provide the short key `greeter` are told
apart by the namespace prefix. The checker admitted such programs from the
start, but each emitter spelled the key straight into a host identifier, so no
program that used one could run: py/ts/rust/java/wasm refused it outright and go
emitted `var _keyAcme::greeter`, which no Go compiler parses (a silently wrong
emission, measured here before the fix).

The fix mangles the key into a tier-legal identifier at the one place each
emitter turns a provision key into an identifier, and leaves the qualified
string everywhere the COMPILER compares keys (G2 conflict checks, `revl audit`,
dependency queries, the IR, the runtime registry lookups that already took the
wiring key). The rule, identically in the five hosted emitters:

    _host_key(k) = k                                          if "::" not in k
                 = "__".join(p.replace("_", "_u") for p in k.split("::"))

    greeter          -> greeter          (unqualified: byte-identical)
    acme::greeter    -> acme__greeter
    a::b__c          -> a__b_u_uc
    a__b::c          -> a_u_ub__c

Injectivity: `p -> p.replace("_", "_u")` is injective (every `_` in its image is
immediately followed by the `u` that emitted it, and no other `_` survives), and
its image never ends in `_` and never contains `__`, so splitting the mangled
form on `__` recovers the namespace parts exactly. An unqualified key is
returned byte-identically, and every mangled qualified key DOES contain `__`, so
the only residual collision is a flat key that already spells one (`a__b` beside
`a::b`); each hosted emitter refuses that loudly, with a rename hint, instead of
emitting two providers under one name.

wasm is the one tier that mangles NOTHING: a WAT identifier may contain `:`
(proved with `wat2wasm`), so `provide:acme::greeter.hello` carries the qualified
key verbatim.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # gate imports resolve in this process
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from _backend_import import backend_emitter  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

HOSTED_TIERS = ("python", "typescript", "rust", "java", "go")

#: The issue's own reproducer, verbatim.
ISSUE_REPRODUCER = """service Greeter { fn hello(name: Str) -> Str }

component AcmeGreeter provides acme::greeter: Greeter {
  provide acme::greeter {
    fn hello(name) = "hello " + name
  }
}

lifecycle test "a namespaced key answers" {
  load AcmeGreeter
  let r = call acme::greeter.hello("x")
  assert r == "hello x"
  unload AcmeGreeter
  assert no_residue
}
"""

#: Two namespaces providing the SAME short key, each with its own consumer:
#: the composition namespacing exists to make possible, and the one that must
#: not trip the G2 one-provider-per-key conflict.
TWO_NAMESPACES = """service Greeter { fn hello(name: Str) -> Str }
service Client { fn greet(name: Str) -> Str }

component AcmeGreeter provides acme::greeter: Greeter {
  provide acme::greeter {
    fn hello(name) = "acme " + name
  }
}

component GlobexGreeter provides globex::greeter: Greeter {
  provide globex::greeter {
    fn hello(name) = "globex " + name
  }
}

component AcmeClient requires acme::greeter: Greeter provides acme_client: Client {
  provide acme_client {
    fn greet(name) = greeter.hello(name)
  }
}

component GlobexClient requires globex::greeter: Greeter provides globex_client: Client {
  provide globex_client {
    fn greet(name) = greeter.hello(name)
  }
}

lifecycle test "each namespace answers for itself" {
  load AcmeGreeter
  load GlobexGreeter
  load AcmeClient
  load GlobexClient
  let a = call acme_client.greet("x")
  let g = call globex_client.greet("x")
  assert a == "acme x"
  assert g == "globex x"
  unload GlobexClient
  unload AcmeClient
  unload GlobexGreeter
  unload AcmeGreeter
  assert no_residue
}
"""

#: A routed (item 167) require on a namespaced key: the router class/struct/
#: proxy is named from the key at the declaration site AND at the use site, so
#: the two must spell it identically. Scalar-only: wasm refuses a `Str` across a
#: routed require by design (a pointer would name the caller's memory), which is
#: issue #1601's rule, not a key failure.
ROUTED_NAMESPACED = """service Worker { fn call(request: Int) -> Int }
service Front { fn ask(q: Int) -> Int }

component W1 provides acme::worker: Worker {
  isolate acme::worker in realm("w1")
  provide acme::worker { fn call(request) = request + 1 }
}

component W2 provides acme::worker: Worker {
  isolate acme::worker in realm("w2")
  provide acme::worker { fn call(request) = request + 2 }
}

component Router requires acme::worker: Worker provides front: Front {
  isolate acme::worker in realms("w1", "w2") strategy(least_loaded)
  provide front { fn ask(q) = worker.call(q) }
}
"""

#: Namespaced keys whose PARTS are host keywords on some tier: `class` on
#: py/java, `box` on rust. The `__` join means the emitted identifier is never
#: the bare keyword, and this pins that end to end through a real boot.
KEYWORD_PARTS = """service Greeter { fn hello(name: Str) -> Str }
service Client { fn greet(name: Str) -> Str }

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

lifecycle test "keyword-named namespace parts answer" {
  load ClassGreeter
  load BoxGreeter
  let a = call acme::class.hello("x")
  let b = call acme::box.hello("x")
  assert a == "class x"
  assert b == "box x"
  unload BoxGreeter
  unload ClassGreeter
  assert no_residue
}
"""

#: Scalar-boundary pair for the wasm tier (no `Str` crosses any boundary on it).
WASM_PAIR = """service Inc { fn add(x: Int) -> Int }

component AcmeInc provides acme::inc: Inc {
  provide acme::inc {
    fn add(x) = x + 1
  }
}

component IncUser requires acme::inc: Inc provides user_api: Inc {
  provide user_api {
    fn add(x) = inc.add(x)
  }
}
"""


# ------------------------------------------------------------------ toolchains
#: Why a tier cannot run here, or None when it can. Probed once at import so
#: the reason travels into the skip message (never a fake pass).
def _cordis_reason() -> str | None:
    if importlib.util.find_spec("cordis") is None:
        return ("the calls run against a live cordis-py composition; install the "
                "pinned fork with `sh backends/python/setup.sh`")
    return None


def _vitest_reason() -> str | None:
    if not (ROOT / "backends" / "typescript" / "node_modules" / ".bin" / "vitest").exists():
        return "vitest not installed in backends/typescript"
    return None


def _go_reason() -> str | None:
    return None if shutil.which("go") else "go not installed"


def _rust_reason() -> str | None:
    from revl.run_rust import rust_runtime_reason  # noqa: PLC0415

    reason = rust_runtime_reason()
    return f"no cordis-rs runtime: {reason}" if reason else None


def _java_reason() -> str | None:
    from revl.run_java import java_runtime_reason  # noqa: PLC0415

    reason = java_runtime_reason()
    return f"no working JDK for the java tier: {reason}" if reason else None


#: Every tier with a live composition runtime, and the gate that says whether it
#: can run here.
TIER_GATES = {
    "py": _cordis_reason,
    "ts": _vitest_reason,
    "rust": _rust_reason,
    "java": _java_reason,
    "go": _go_reason,
}


def _run_on_tier(tier: str, source: Path) -> None:
    """Run a lifecycle test on `tier`, skipping with the tier's own reason when
    its toolchain is absent and failing loudly when it is present."""
    reason = TIER_GATES[tier]()
    if reason:
        pytest.skip(reason)
    proc = _revl_test(tier, source)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert f"[{tier}] pass:" in out, out
    # Each tier reports the single lifecycle test it ran in its own words; the
    # witness is pinned so a tier that silently ran NOTHING cannot pass here.
    assert re.search(TIER_RAN_ONE[tier], out, re.M), out


#: How each tier reports that it ran the one lifecycle test in the fixture.
TIER_RAN_ONE = {
    "py": r"\b1 test\(s\) passed\b",
    "ts": r"Tests\s+1 passed \(1\)",
    "rust": r"\b1 passed\b",
    "java": r"\b1 lifecycle test\(s\)",
    "go": r"^ok\s+\S*revltest\b",
}


def _revl_test(backend: str, source: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-P", "-m", "revl", "test", "--backend", backend, str(source)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


#: A host-language string literal (single or double quoted, escapes honoured).
_QUOTED = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')


def _unquoted(text: str) -> str:
    """`text` with every string literal removed: what is left is the host code,
    the part that has to be made of identifiers."""
    return _QUOTED.sub("", text)


@pytest.mark.parametrize("tier", ["py", "ts", "rust", "java", "go"])
def test_the_issue_reproducer_runs_on_the_tier(tier, tmp_path):
    """load -> call through the qualified key -> assert -> unload -> no residue.

    The reproducer is the issue's, unmodified: it must pass on every tier that
    has a runtime, and this is the failing direction before the fix (py/ts
    refused at emit; the emitted go did not compile)."""
    source = _write(tmp_path, "ns_1914.rvl", ISSUE_REPRODUCER)
    _run_on_tier(tier, source)


@pytest.mark.parametrize("tier", ["py", "ts", "rust", "java", "go"])
def test_a_namespaced_key_whose_parts_are_host_keywords_still_runs(tier, tmp_path):
    """`class` is a keyword on py/java, `box` on rust, `mod` on rust: a part the
    tier reserves must not survive as a bare identifier (the `__` join already
    guarantees that, and this pins it end to end)."""
    source = _write(tmp_path, "ns_kw_1914.rvl", KEYWORD_PARTS)
    _run_on_tier(tier, source)


@pytest.mark.parametrize("tier", ["py", "ts", "rust", "java", "go"])
def test_two_namespaces_provide_the_same_short_key_and_each_resolves_to_its_own(
        tier, tmp_path):
    """The composition namespacing exists for: `greeter` provided by two
    namespaces in one composition, no G2 collision, each consumer bound to its
    own namespace's implementation."""
    source = _write(tmp_path, "ns_two_1914.rvl", TWO_NAMESPACES)
    _run_on_tier(tier, source)


def test_the_consumer_resolves_to_the_producers_mangled_identifier():
    """The acceptance test for the mapping: the producer registers under the
    mangled identifier and the consumer's call reaches exactly that name."""
    out = backend_emitter("python").emit(compile_source(TWO_NAMESPACES))
    assert "_revl_ctx.provide('acme__greeter')" in out
    assert "_revl_ctx.provide('globex__greeter')" in out
    assert "_revl_ctx.acme__greeter.hello(name)" in out
    assert "_revl_ctx.globex__greeter.hello(name)" in out
    # `inject` names the same identifier the provider registered, or the
    # composition would resolve nothing at runtime.
    assert "'inject': ['acme__greeter']" in out
    assert "'inject': ['globex__greeter']" in out


def test_an_unqualified_key_keeps_its_original_spelling_on_every_tier():
    """The mangling must be a no-op on the keys that already worked: every
    emitter's own spelling still comes out for a flat key, so no golden moves."""
    ir = compile_source("""service Greeter { fn hello(name: Str) -> Str }

component Flat provides greeter: Greeter {
  provide greeter { fn hello(name) = "hello " + name }
}
""")
    py = backend_emitter("python").emit(ir)
    assert "_revl_ctx.provide('greeter')" in py
    ts = backend_emitter("typescript").emit(ir)
    assert 'yield ctx.provide("greeter"' in ts
    rs = backend_emitter("rust").emit(ir)
    assert '"greeter"' in rs
    go = backend_emitter("go").emit(ir)
    assert 'stc.NewKey[Greeter]("greeter")' in go
    java = backend_emitter("java").emit(ir)
    assert '"greeter"' in java


# ------------------------------------------------------------ the rule itself
#: Keys the rule must agree on across every hosted emitter. The collisions the
#: rule can produce (`a__b` is both a flat key and the image of `a::b`) are
#: covered by the refusal test below; here the point is agreement + injectivity.
RULE_KEYS = ("greeter", "a", "a_b", "acme::greeter", "a::b", "x::y::z",
             "a::b__c", "a__b::c", "_x::y", "x::y_")


@pytest.mark.parametrize("tier", HOSTED_TIERS)
def test_the_host_identifier_rule_is_the_same_on_every_hosted_tier(tier):
    """One rule, five emitters, byte-identical answers — plus the injectivity
    claim the rule rests on: two distinct keys never share a host identifier
    here, and an unqualified key is returned verbatim."""
    emit = backend_emitter(tier)
    host, binding = emit._host_key, emit._key_binding
    assert emit.KEY_NAMESPACE_SEP == "::"
    for key in RULE_KEYS:
        want = key if "::" not in key else "__".join(p.replace("_", "_u")
                                                     for p in key.split("::"))
        assert host(key) == want, (tier, key)
        assert binding(key) == key.rsplit("::", 1)[-1], (tier, key)
    mangled = [host(k) for k in RULE_KEYS]
    assert len(set(mangled)) == len(mangled), (tier, mangled)
    assert all(host(k) == k for k in RULE_KEYS if "::" not in k), tier


def test_wasm_carries_the_qualified_key_verbatim_and_needs_no_mangling():
    """`:` is a legal WAT identifier character, so this tier mangles nothing:
    the qualified key IS the identifier, in the export and in the import."""
    out = backend_emitter("wasm").emit(compile_source(WASM_PAIR))
    text = "\n".join(out.values())
    assert "provide:acme::inc.add" in text
    assert "coeffect:acme::inc" in text
    assert "$req.acme::inc.add" in text
    assert backend_emitter("wasm")._provision_key("acme::inc", "provide key") == "acme::inc"
    assert backend_emitter("wasm")._key_binding("acme::inc") == "inc"


def test_the_rule_rejects_two_keys_that_land_on_one_identifier():
    """`_check_host_keys` is the guard the injectivity proof leans on, and it
    must see the collision however the two keys arrive. Pinned on one tier per
    spelling of the pair: a namespaced key beside the flat key that spells its
    image, in both declaration orders."""
    for tier in HOSTED_TIERS:
        check = backend_emitter(tier)._check_host_keys
        with pytest.raises(Exception) as excinfo:
            check({"components": [{"provides": {"a__b": "S", "a::b": "S"}}]})
        assert type(excinfo.value).__name__ == "EmitError", (tier, excinfo.value)
        # The guard is not trigger-happy: distinct keys pass it untouched.
        check({"components": [{"provides": {"a__b": "S", "b__c": "S", "c::d": "S"}}]})


# ------------------------------------------------------------ the residual one
@pytest.mark.parametrize("tier", HOSTED_TIERS)
def test_a_flat_key_that_already_spells_a_mangled_key_is_refused(tier):
    """The only collision the injective rule leaves: `a__b` (flat) beside
    `a::b`, whose image is also `a__b`. Two providers must never land on one
    host identifier, so this is refused with a rename hint."""
    ir = compile_source("""service Greeter { fn hello(name: Str) -> Str }

component Flat provides a__b: Greeter {
  provide a__b { fn hello(name) = "flat " + name }
}

component Ns provides a::b: Greeter {
  provide a::b { fn hello(name) = "ns " + name }
}
""")
    with pytest.raises(Exception) as excinfo:
        backend_emitter(tier).emit(ir)
    assert type(excinfo.value).__name__ == "EmitError", (tier, excinfo.value)
    message = str(excinfo.value)
    assert "'a__b'" in message and "'a::b'" in message, (tier, message)
    assert "rename" in message.lower(), (tier, message)


def test_wasm_keeps_both_keys_apart_without_a_refusal():
    """wasm mangles nothing, so the pair the hosted tiers must refuse is simply
    two distinct WAT identifiers here."""
    ir = compile_source("""service Greeter { fn hello(name: Str) -> Str }

component Flat provides a__b: Greeter {
  provide a__b { fn hello(name) = "flat " + name }
}

component Ns provides a::b: Greeter {
  provide a::b { fn hello(name) = "ns " + name }
}
""")
    out = backend_emitter("wasm").emit(ir)
    text = "\n".join(out.values())
    assert "provide:a__b.hello" in text and "provide:a::b.hello" in text


# ------------------------------------------------------------------- placement
#: The router name each hosted tier builds from the key's host spelling, and how
#: many times the emitted document must carry it (declaration + use sites).
ROUTER_WITNESS = {
    "python": ("_revl_route_acme__worker", 2),
    "typescript": ("_revl_route_acme__worker", 2),
    "rust": ("RevlRouterRouterAcmeWorker", 4),
    "java": ("RevlRouterRouterAcmeWorker", 3),
    "go": ("revlRouterRouterAcmeWorker", 2),
}


@pytest.mark.parametrize("tier", HOSTED_TIERS)
def test_a_routed_namespaced_require_names_its_router_from_one_spelling(tier):
    """item 167: a routed require is realized by a router class/struct/proxy
    named from the key. Declaration and use site must spell it the same, or the
    emitted program does not compile — the failure a namespaced key would have
    introduced had only one of the two sites been mangled."""
    out = backend_emitter(tier).emit(compile_source(ROUTED_NAMESPACED))
    witness, minimum = ROUTER_WITNESS[tier]
    assert out.count(witness) >= minimum, (tier, witness, out.count(witness))
    # The router's identity is built from a `::`-free spelling, never from the
    # qualified key.
    assert "::" not in witness
    assert tier in ("java", "go") or backend_emitter(tier)._host_key("acme::worker") in out, tier


@pytest.mark.parametrize("tier", HOSTED_TIERS)
def test_a_namespaced_key_never_reaches_a_host_compiler_as_a_bare_identifier(tier):
    """The pre-fix failure, pinned per tier (go wrote `var _keyAcme::greeter`):
    the qualified key is a WIRING STRING, so in emitted host code it appears
    only inside string literals — the wiring identity the runtime looks up —
    and never where the tier needs an identifier."""
    for fixture in (ISSUE_REPRODUCER, TWO_NAMESPACES, ROUTED_NAMESPACED,
                    KEYWORD_PARTS):
        keys = set(re.findall(r"\b[A-Za-z_]\w*::[A-Za-z_]\w*\b", fixture))
        assert keys, "fixture must use a namespaced key"
        out = backend_emitter(tier).emit(compile_source(fixture))
        unp = _unquoted(out)
        for key in keys:
            for line in unp.splitlines():
                assert key not in line, (tier, key, line)
