"""Item 424 slice B1: interposition without a language change, and the hole.

`docs/design/424-dsh-language-gaps.md` §2.2 and §2.6 (slice B1). Gap (b) asks
where a third party stands to observe a call across a provision edge. The full
answer is the `seam` composition row (slices B2 onward), which is not built.
This slice records what the CURRENT language already admits, and pins the one
trap in it, so nothing here waits on B2 and the trap cannot rot into a comment.

Two shapes, both of which compile against the tree today:

* the DISTINCT-KEY wrapper (`docs/composition-rows.md`, the interposition
  section) is the sanctioned pattern. A seam component requires the inner
  provider under a re-keyed name (`inner_db`) and provides the consumer's key
  (`db`), so every call to `db` runs the seam body first. Its cost is exact and
  is the whole of 424(b)'s "a third party has no place to stand": the inner
  provider had to be re-keyed IN ITS OWN SOURCE, which is a source edit to a
  component the seam author does not own.

* the SAME-KEY wrapper via a one-element route (`isolate db in realms("inner")`)
  needs no re-key and COMPILES, admits and passes G4 -- and its provide body,
  which is the whole interception, is silently discarded by the reference
  driver. A component carrying a `routes` entry is realized by `_install_router`
  (`src/revl/run.py`, the `if comp.get("routes")` branch in `_load`) and is
  never plugged as a fiber, so its body never runs. `docs/router.md` states the
  reason: a routed require has no single-realm provider, so an emitted body
  would sit PENDING forever.

The two `@needs_cordis` tests run the production path; a runtime-less
interpreter skips them with a reason (never a feint at passing). The two
compile-level tests run on every interpreter, so the shape and the hole are
pinned even where no runtime is installed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.run import _Router  # noqa: E402

# The same availability gate test_run.py / test_router_runtime.py use.
try:  # noqa: SIM105
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover -- depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")


# --------------------------------------------------------------------------
# the two programs
# --------------------------------------------------------------------------

# The sanctioned interposition pattern. `Inner` is re-keyed to `inner_db` in its
# own source -- that re-key is the cost. The seam observes by recording into its
# OWN local state (a `Map` effect, disposed on teardown), so no cross-service
# emission is involved and the shape stays focused on the lifecycle property.
DISTINCT_WRAPPER = """
service Db { fn execute(q: Str) -> Str  fn seen() -> Int }

component Inner provides inner_db: Db {
  provide inner_db { fn execute(q) = "row"  fn seen() = 0 }
}

component Seam requires inner_db: Db provides db: Db {
  let log = effect Map.new() undo log.drop()
  provide db {
    fn execute(q) {
      let r = inner_db.execute(q)
      effect log.insert(q, "1")
      undo   log.remove(q)
      return r
    }
    fn seen() = log.size()
  }
}
"""

# The same-key shape: no re-key, interposed through a one-element route. It
# compiles and admits, and the seam's provide body -- the `log.insert`
# observation -- is discarded at load, because the seam carries a `routes` entry.
SAME_KEY_ROUTED = """
service Db { fn execute(q: Str) -> Str  fn seen() -> Int }

component Inner provides db: Db {
  isolate db in realm("inner")
  provide db { fn execute(q) = "row"  fn seen() = 0 }
}

component Seam requires db: Db provides db: Db {
  isolate db in realms("inner")
  let log = effect Map.new() undo log.drop()
  provide db {
    fn execute(q) {
      let r = db.execute(q)
      effect log.insert(q, "1")
      undo   log.remove(q)
      return r
    }
    fn seen() = log.size()
  }
}
"""


def _components(ir: dict) -> dict:
    return {c["name"]: c for c in ir["components"]}


def _build_driver(ir):
    """A `_Driver` on the real cordis-py backend, wired exactly as
    `run_command` wires it -- the same helper `test_router_runtime.py` uses."""
    from revl.run import _Driver  # noqa: PLC0415
    from revl._paths import backends_root  # noqa: PLC0415

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    return _Driver(ir, {}, emit, runtime_mod, Context, FiberState)


# --------------------------------------------------------- compile-level pins
# These run on every interpreter, so the sanctioned shape and the routes hole
# are recorded even with no runtime installed.


def test_distinct_key_wrapper_compiles_and_pays_the_rekey_cost():
    """The sanctioned pattern admits, and the re-key is visible in the IR: the
    inner provider claims `inner_db`, not `db`, which is the source edit 424(b)
    calls the seam author's cost."""
    comps = _components(compile_source(DISTINCT_WRAPPER, "distinct.rvl"))
    assert "inner_db" in comps["Inner"]["provides"]
    assert "db" not in comps["Inner"]["provides"]
    assert "db" in comps["Seam"]["provides"]
    assert "inner_db" in comps["Seam"]["requires"]
    # a distinct-key wrapper is an ordinary fiber, never a router
    assert not comps["Seam"].get("routes")
    assert not comps["Inner"].get("routes")


def test_same_key_routed_wrapper_carries_a_routes_entry():
    """The no-re-key shape compiles and admits, and the seam carries a `routes`
    entry for `db` -- which is exactly what the driver realizes as a router
    instead of a fiber. This is the compile-time half of the hole: the shape the
    day it stops being discarded, this assertion changes."""
    comps = _components(compile_source(SAME_KEY_ROUTED, "samekey.rvl"))
    routes = comps["Seam"].get("routes")
    assert routes is not None, "the same-key wrapper must record a routes bind"
    assert routes["db"]["realms"] == ["inner"]


# ------------------------------------------------------- end-to-end lifecycle


@needs_cordis
def test_distinct_key_wrapper_observes_every_call():
    """The sanctioned pattern, run: the seam sits in front of the inner provider
    and its observation store counts each call, exactly once, in order -- and
    teardown replays the store's inverse, leaving no residue."""
    ir = compile_source(DISTINCT_WRAPPER, "distinct.rvl")

    async def scenario():
        driver = _build_driver(ir)
        module = driver._emit_module(ir)
        await driver._load(ir, module)

        db = driver.root.get("db")
        assert not isinstance(db, _Router)     # a plain fiber, not a route
        assert db.seen() == 0
        assert db.execute("select 1") == "row"
        assert db.execute("select 2") == "row"
        # the seam observed both calls that crossed the `db` edge
        assert db.seen() == 2

        await driver._teardown()
        assert driver.root.registry.size == 0
        assert driver.root.reflect.store == {}
        assert (driver.root.fiber._disposables.length
                == driver._baseline_disposables)

    asyncio.run(scenario())


@needs_cordis
def test_routed_wrapper_provide_body_is_never_executed():
    """The trap, pinned as an executed fact. The same-key seam carries a
    `routes` entry, so `_Driver._load` takes its `if comp.get("routes")` branch
    (`src/revl/run.py`): it installs a `_Router` for the key and `continue`s,
    NEVER plugging the seam as a fiber. The consequence is that the seam's
    provide body -- its `log.insert` observation, which is the whole
    interception -- never runs. The call still succeeds, because the router
    forwards it to the inner provider in `realm("inner")`.

    The day the driver stops discarding that body, `Seam` will appear in
    `driver.fibers` and `seen()` will count, and this test will go red -- which
    is the point of pinning it."""
    ir = compile_source(SAME_KEY_ROUTED, "samekey.rvl")

    async def scenario():
        driver = _build_driver(ir)
        module = driver._emit_module(ir)
        await driver._load(ir, module)

        # the seam was realized as a router, not plugged as a fiber: its body
        # was never installed, so it never ran.
        assert "Seam" not in driver.fibers
        assert ("Seam", "db") in driver.routers

        db = driver.root.get("db")
        assert isinstance(db, _Router)
        # the call still runs -- the router forwards it to the inner provider
        assert db.execute("select 1") == "row"
        assert db.execute("select 2") == "row"
        # ...but the seam's observation store never saw anything, because the
        # provide body that would have recorded into it was discarded. `seen()`
        # is forwarded to the inner provider (which counts nothing), so it is 0
        # rather than the 2 the distinct-key wrapper reported for the same calls.
        assert db.seen() == 0

        await driver._teardown()
        assert driver.root.reflect.store == {}

    asyncio.run(scenario())
