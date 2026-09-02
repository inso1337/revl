"""Multi-realm routing (item 173) on the cordis4j tier — emitter assertions.

cordis4j is UPSTREAM (github.com/1na-ko/cordis4j), so the routing PRIMITIVE
(`ctx.serviceInRealm` with a liveness check) lands via fork + PR
(forks/cordis4j/REVL-FORK.md) and running a routed composition on the real
runtime is that PR's job. What IS this suite's job is the emitter: a routed
require lowers to a router class that consumes the strict single-realm read,
excludes the routed key from `ctx.get`, and leaves a routes-less program
byte-identical.

Those used to be substring matches and nothing else, and that is precisely how
issue #154's first defect shipped: the router class throws `CordisException`
while `_core_imports` added that import only for a `fail` step, so this very
scenario emitted 6119 bytes javac rejects while every assertion here stayed
green. So the emitted unit is COMPILED (against the in-repo cordis4j API stubs,
which carry `serviceInRealm`), and each assertion below is a claim about a
program javac has already accepted. See `javac_gate` for how the JDK is found
and why the gate cannot quietly stop running in CI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import javac_gate  # noqa: E402
from revl import compile_source  # noqa: E402

SCENARIO = (BACKEND / "scenarios" / "router.rvl").read_text()


def _emit(source: str) -> str:
    """Emit, and prove the emitted unit compiles before asserting on its text."""
    spec = importlib.util.spec_from_file_location("revl_java_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return javac_gate.compile_check(module.emit(compile_source(source)),
                                    "routed require")


def test_the_routed_unit_carries_every_import_it_throws():
    """The regression gate for issue #154's first defect, stated directly.

    `compile_check` above already refuses an uncompilable unit, so this test is
    the named record of WHICH omission broke it: the router's failover exhaust
    throws `CordisException`, and that import used to be added only for a
    `fail` step. Asserting the import and the throw together means a future
    edit that drops one has to explain the other.
    """
    java = _emit(SCENARIO)
    assert "throw new CordisException(" in java
    assert "import io.cordis4j.core.CordisException;" in java


def test_emit_router_class_and_strict_read():
    java = _emit(SCENARIO)
    assert "public static final class RevlRouterRouterWorker implements Worker {" in java
    # the strict single-realm liveness-checked read (the fork's primitive)
    assert "ctx.serviceInRealm(Worker.class, realms[at])" in java
    # item 433 F3: ONE resolution per call. The selection loop keeps the handle
    # it just probed instead of discarding it, rebuilding a live-label list and
    # resolving the winner a second time, which is the shape the go emitter
    # already had. So there is exactly one `serviceInRealm` call site, no
    # `revlLive()` and no `live.contains(..)` scan.
    assert java.count("ctx.serviceInRealm(") == 1
    assert "revlLive()" not in java
    assert "live.contains(" not in java
    # ...and the served counter is a `long[]` indexed by realm position, not a
    # Map<String, Long> that boxes a Long past the valueOf cache on every call.
    assert "private final long[] served = new long[3];" in java
    assert "served.merge(" not in java
    # the emitter knows the declared strategy, so only that branch is emitted
    assert 'private final String strategy' not in java
    assert 'strategy.equals("least_loaded")' not in java
    # forwards each op through a fresh selection (failover from the body)
    assert "public String call(String request) {" in java
    assert "revlSelect().call(request)" in java


def test_emit_routed_key_not_resolved_via_get():
    java = _emit(SCENARIO)
    # the routed key is wired as a router, never a committed-view ctx.get.
    assert "Worker worker = new RevlRouterRouterWorker(ctx);" in java
    assert "Worker worker = ctx.get(Worker.class);" not in java


def test_emit_routeless_program_is_byte_identical():
    plain = """
    service Greeter { fn hi(name: Str) -> Str }
    component G provides greeter: Greeter {
      provide greeter { fn hi(name) = "hi " + name }
    }
    """
    java = _emit(plain)
    assert "RevlRouter" not in java
    assert "serviceInRealm" not in java
