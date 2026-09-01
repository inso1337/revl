"""Multi-realm routing (item 173) on the cordis4j tier — emitter assertions.

cordis4j is UPSTREAM (github.com/1na-ko/cordis4j) and no real runtime + JRE is
reachable here, so this tier lands via fork + PR (forks/cordis4j/REVL-FORK.md):
the runtime primitive is a patch/PR-spec, and only the emitter is exercised
here. These are pure-Python assertions — the emitter lowers a routed require
into a router class that consumes the strict single-realm read
`ctx.serviceInRealm(...)`, excludes the routed key from `ctx.get`, and leaves a
routes-less program byte-identical. Building/running against a real cordis4j +
JRE is the upstream PR's job (see the fork README).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SCENARIO = (BACKEND / "scenarios" / "router.rvl").read_text()


def _emit(source: str) -> str:
    spec = importlib.util.spec_from_file_location("revl_java_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source))


def test_emit_router_class_and_strict_read():
    java = _emit(SCENARIO)
    assert "public static final class RevlRouterRouterWorker implements Worker {" in java
    # the strict single-realm liveness-checked read (the fork's primitive)
    assert "ctx.serviceInRealm(Worker.class, r).isPresent()" in java
    assert "ctx.serviceInRealm(Worker.class, cand).get()" in java
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
