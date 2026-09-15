"""Per-test async-context isolation for the ts lifecycle drivers (issue #1112).

The py reference tier runs every `lifecycle test` under its own `asyncio.run`,
so a contextvar a driver binds dies with that driver. The ts tier ran every
driver in one shared async context, so an `@ts` extern that binds ambient
context with `AsyncLocalStorage.enterWith` polluted every LATER test in the
same file.

Why it is expensive rather than merely wrong: on node >= 24 `AsyncContextFrame`
is on by default and the escape does not happen, so the same emitted file is
green on a modern dev machine and red on the node CI pins. Measured downstream
as "fails on ubuntu, passes on macOS" when the real variable was node 22 versus
node 26.

WHAT THIS FILE ASSERTS, and in which direction:

  * `test_without_the_isolation_the_first_test_leaks_into_the_next` — the leak
    itself, on the emitted bytes with the wrapper removed. This is the
    non-vacuity gate: it FAILS on a tree without the isolation and passes with
    it, *under the node <= 23 behaviour*. On node >= 24 with AsyncContextFrame
    on it does not reproduce at all, which is the point, so it is run under
    `--no-async-context-frame` wherever that flag exists.
  * `test_with_the_isolation_the_next_test_is_clean` — the same document,
    isolated, green under BOTH node behaviours.
  * `test_a_context_free_driver_is_green_on_both_trees_and_both_modes` — the
    control. A lifecycle test that binds nothing passes with and without the
    isolation, in both modes, so a red above is the leak and not the flag.
  * `test_every_lifecycle_driver_is_wrapped` — toolchain-free shape check.

WHY THIS FILE LIVES HERE (same reason as test_runtime_contract.py): the
`backend-typescript` CI job runs `pytest backends/typescript/` on a checkout
that has just done `npm ci`, so node and cordis are unconditionally present —
and that job pins **node 22**, the implementation the leak is real on.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source                             # noqa: E402
from revl.test import _node_can_run_emitted, _node_version   # noqa: E402

_RUNNER = _HERE / "scripts" / "node-tier-runner.mjs"
_GENERATED = _HERE / "tests" / "generated"

# The wrapper `_emit_ts_lifecycle_tests` puts around every driver, and its
# close. Removing them reconstructs the pre-#1112 emission from the current
# emitter's own bytes, so "the tree without the change" cannot drift away from
# what this gate tests.
_WRAP_OPEN = "  await _revl_test_context(async () => {\n"
_WRAP_CLOSE = "  })\n})\n"

# A `@ts` extern that binds ambient context the way a real host integration
# does: an AsyncLocalStorage entered with `enterWith`, which is how a turn,
# trace or cancellation scope is attached to the current async execution
# without wrapping a callback. The py bodies exist only so the document is
# lowerable on the reference tier too; nothing here executes them.
_PROBE_EXTERNS = """
pub extern pure fn bind_ambient() -> Int
  = @py { return 1 }
  = @ts {
    const hooks = process.getBuiltinModule("node:async_hooks")
    const g = globalThis as Record<string, any>
    if (g.__revl_1112_als === undefined) g.__revl_1112_als = new hooks.AsyncLocalStorage()
    g.__revl_1112_als.enterWith(1112)
    return 1n
  }

pub extern pure fn read_ambient() -> Int
  = @py { return 0 }
  = @ts {
    const g = globalThis as Record<string, any>
    const als = g.__revl_1112_als
    const seen = als === undefined ? undefined : als.getStore()
    return seen === undefined ? 0n : BigInt(seen)
  }
"""

_COMPOSITION = """
service Probe {
  fn ping() -> Int
}

component Probes provides probe: Probe {
  let m = effect Map.new() undo m.drop()
  provide probe {
    fn ping() = 1
  }
}
"""

# Two drivers in one file. The first binds ambient context AFTER its `load`
# step has awaited — which is what the shape of a real lifecycle driver
# guarantees, and why wrapping the body in `AsyncResource.runInAsyncScope`
# alone does not contain it: by then the `enterWith` lands on a promise
# continuation that has already left the resource's synchronous scope.
_LEAKY = _PROBE_EXTERNS + _COMPOSITION + """
lifecycle test "one binds ambient context" {
  load Probes
  assert bind_ambient() == 1
  assert read_ambient() == 1112
  unload Probes
  assert no_residue
}

lifecycle test "two must not inherit it" {
  load Probes
  assert read_ambient() == 0
  unload Probes
  assert no_residue
}
"""

# The control: same composition, same two-driver file, no ambient context
# anywhere. Green on both trees in both modes.
_CONTROL = _COMPOSITION + """
lifecycle test "control one" {
  load Probes
  let seen = call probe.ping()
  assert seen == 1
  unload Probes
  assert no_residue
}

lifecycle test "control two" {
  load Probes
  let seen = call probe.ping()
  assert seen == 1
  unload Probes
  assert no_residue
}
"""


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_1112",
                                                  _HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(source: str, *, isolated: bool) -> str:
    """The emitted module, optionally with the #1112 wrapper taken back out.

    The un-isolated form is built from the current emitter's own output rather
    than from a checked-in copy of the old bytes, so it tracks every other
    change to the driver. The counts are asserted: if the wrapper's text moves,
    this raises instead of silently handing back an already-isolated module and
    reporting a pass.
    """
    module = _emitter().emit(compile_source(source, "probe_1112.rvl"),
                             runtime_import="../../runtime.ts")
    drivers = module.count(_WRAP_OPEN)
    assert drivers == 2, (
        f"expected 2 wrapped lifecycle drivers, found {drivers} — the #1112 "
        f"wrapper's shape changed and this file's reconstruction is stale")
    if isolated:
        return module
    stripped = module.replace(_WRAP_OPEN, "")
    closes = stripped.count(_WRAP_CLOSE)
    assert closes == drivers, (
        f"expected {drivers} wrapper closes, found {closes} — the #1112 "
        f"wrapper's shape changed and this file's reconstruction is stale")
    return stripped.replace(_WRAP_CLOSE, "})\n")


def _run(source: str, *, isolated: bool, legacy: bool) -> tuple[int, str]:
    """Execute the emitted module under the plain-node tier runner.

    `legacy=True` asks for the node <= 23 async-context behaviour: the flag on
    a node that has AsyncContextFrame, and nothing at all on a node that never
    had it (where the behaviour is already the default).
    """
    _GENERATED.mkdir(parents=True, exist_ok=True)
    # Not a `.test.ts`: these are driven by the tier runner below, and a name
    # vitest's include glob matches would also collect them into the backend's
    # own suite.
    path = _GENERATED / f"lifecycle_isolation_1112_{'iso' if isolated else 'bare'}.ts"
    path.write_text(_emit(source, isolated=isolated), encoding="utf-8")
    argv = ["node"]
    if legacy and _LEGACY_FLAG:
        argv += [_LEGACY_FLAG]
    try:
        result = subprocess.run(argv + [str(_RUNNER), str(path)], cwd=_HERE,
                                capture_output=True, text=True, timeout=300)
    finally:
        path.unlink(missing_ok=True)
    return result.returncode, result.stdout + result.stderr


def _legacy_flag() -> str | None:
    """`--no-async-context-frame` when this node accepts it, else None."""
    try:
        probe = subprocess.run(["node", "--no-async-context-frame", "-e", ""],
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
        return None
    return "--no-async-context-frame" if probe.returncode == 0 else None


_LEGACY_FLAG = _legacy_flag()
_VERSION = _node_version()
# Either the flag turns AsyncContextFrame off, or this node predates it and is
# already in the leaky mode.
_LEGACY_REACHABLE = bool(_LEGACY_FLAG) or (_VERSION is not None and _VERSION[0] <= 23)

needs_toolchain = pytest.mark.skipif(
    not (_HERE / "node_modules" / "cordis").exists()
    or not _node_can_run_emitted(_VERSION),
    reason="needs backends/typescript/node_modules and node >= 22.18 "
           "(`cd backends/typescript && npm ci`)",
)
needs_legacy = pytest.mark.skipif(
    not _LEGACY_REACHABLE,
    reason="this node has AsyncContextFrame on and no --no-async-context-frame "
           "flag, so the node <= 23 behaviour the leak needs is unreachable "
           "here; the backend-typescript CI job pins node 22, where it is the "
           "default",
)


# --------------------------------------------------------------------------- #
# the leak, and the fix                                                        #
# --------------------------------------------------------------------------- #
@needs_toolchain
@needs_legacy
def test_without_the_isolation_the_first_test_leaks_into_the_next():
    """Non-vacuity: this is red on the tree before the isolation landed.

    It is red only under the node <= 23 behaviour. On plain node >= 24 the
    same un-isolated bytes pass (see the test below), which is exactly the
    silence this gate exists to end — so this assertion must never be read as
    "the emitted ts tier leaks everywhere".
    """
    code, output = _run(_LEAKY, isolated=False, legacy=True)
    assert code != 0, (
        "the un-isolated emission was expected to leak the first driver's "
        "ambient context into the second under the node <= 23 behaviour, and "
        "did not — the reconstruction above, or the leak, is stale:\n" + output)
    assert "FAIL two must not inherit it" in output, output
    assert "left  = 1112" in output, output


@needs_toolchain
def test_without_the_isolation_node_24_confines_the_binding():
    """The half that makes it expensive, asserted rather than described.

    With AsyncContextFrame on (node >= 24, the default) the `enterWith`
    binding does not escape the callback it ran in, so the un-isolated bytes
    are green: there is no concealed escape on that node, there is no escape.
    Nothing about the emitted program changed; only the node did. If a future
    node stopped confining it, the gate above would be measuring the default
    mode rather than the legacy one, and this says so.
    """
    if not (_VERSION and _VERSION[0] >= 24):
        pytest.skip(f"needs node >= 24 (AsyncContextFrame on by default); "
                    f"have {_VERSION}")
    code, output = _run(_LEAKY, isolated=False, legacy=False)
    assert code == 0, (
        "node >= 24 was expected to confine the `enterWith` binding to the "
        "callback it ran in, so that no cross-test leak exists there at "
        "all:\n" + output)


@needs_toolchain
@pytest.mark.parametrize("legacy", [False, True])
def test_with_the_isolation_the_next_test_is_clean(legacy):
    """The delivered behaviour: green under BOTH node implementations."""
    code, output = _run(_LEAKY, isolated=True, legacy=legacy)
    assert code == 0, output
    assert "2 emitted test(s) passed" in output, output


# --------------------------------------------------------------------------- #
# the control                                                                  #
# --------------------------------------------------------------------------- #
@needs_toolchain
@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_a_context_free_driver_is_green_on_both_trees_and_both_modes(isolated,
                                                                     legacy):
    """Two lifecycle drivers that bind no ambient context pass everywhere.

    Without this, a red above could be `--no-async-context-frame` breaking
    cordis, node's timers or the tier runner rather than the leak.
    """
    code, output = _run(_CONTROL, isolated=isolated, legacy=legacy)
    assert code == 0, output
    assert "2 emitted test(s) passed" in output, output


# --------------------------------------------------------------------------- #
# shape — no toolchain, so it can never go quiet                               #
# --------------------------------------------------------------------------- #
def test_every_lifecycle_driver_is_wrapped():
    module = _emitter().emit(compile_source(_LEAKY, "probe_1112.rvl"),
                             runtime_import="../../runtime.ts")
    assert "import { AsyncLocalStorage } from 'node:async_hooks'" in module
    # captured once, at module evaluation — the file's entry context, which is
    # what each py driver's `asyncio.run` copies
    assert module.count("const _revl_test_context = AsyncLocalStorage.snapshot()") == 1
    assert module.count(_WRAP_OPEN) == module.count("it(")


def test_a_document_without_lifecycle_tests_is_untouched():
    """Pure `test` blocks pay nothing: no import, no snapshot, no wrapper."""
    module = _emitter().emit(
        compile_source('test "pure" { assert 1 + 1 == 2 }', "pure.rvl"),
        runtime_import="../../runtime.ts")
    assert "async_hooks" not in module
    assert "_revl_test_context" not in module
