"""Item 424 gap (b), slice B1 — interposition, measured (no language change).

docs/design/424-dsh-language-gaps.md §2.2 and §2.6 make two claims about what
revl admits TODAY, and B1's job is to pin both as executed facts rather than
recalled ones, so the day either changes something says so:

* the SANCTIONED pattern — a distinct-key wrapper — actually observes a call on
  the real runtime (examples/interpose_observe.rvl), at the cost of re-keying
  the inner provider in its own source; and
* the tempting SAME-key shape (an item-162 one-element route) compiles, admits,
  passes G4 — and its provide body, which is the whole interception, is
  silently discarded by the reference driver, because a `routes`-carrying
  component is realized as a `_Router` proxy and never plugged as a fiber
  (`src/revl/run.py`, the `if comp.get("routes"): self._install_router(...)`
  guard in `_load`). That is the `routes` hole B1 records so slice B2 does not
  inherit it.

Both proofs need execution, so they run on the backend's own venv — the one
with cordis-py installed — and skip with a reason otherwise, never a feint at
passing.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _revl_test(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl", "test", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


# ==========================================================================
# the sanctioned pattern: a distinct-key wrapper observes the call
# ==========================================================================


def test_distinct_key_wrapper_observes_the_call():
    """examples/interpose_observe.rvl: the `Seam` takes key `db`, forwards to
    the re-keyed `inner_db`, and records each call in `AuditSink`. The
    lifecycle test asserts the observation was recorded (`audit.seen() == 1`)
    and that the composition reverts with no residue."""
    result = _revl_test(str(EXAMPLES / "interpose_observe.rvl"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS a distinct-key wrapper observes the call" in result.stdout
    assert "[py] pass: 1 test(s) passed" in result.stdout


def test_the_observation_is_really_checked(tmp_path):
    """Guard against an inert test: if the seam did NOT observe the call, the
    assertion must fail. Break the expected count and the runtime catches it —
    an assertion that can only pass is not an assertion."""
    source = (EXAMPLES / "interpose_observe.rvl").read_text(encoding="utf-8")
    broken = source.replace("assert n == 1", "assert n == 0")
    assert broken != source
    path = tmp_path / "inert.rvl"
    path.write_text(broken, encoding="utf-8")

    result = _revl_test(str(path))
    assert result.returncode == 1
    assert "FAIL a distinct-key wrapper observes the call" in result.stdout
    assert "assertion failed" in result.stdout


# ==========================================================================
# the `routes` hole: the same-key shape's provide body is never run
# ==========================================================================

# The same-key interposition of §2.2: the inner provider isolates `db` in
# realm("inner"); the seam requires `db` in that realm and provides `db` in the
# parent realm via a one-element route (`isolate db in realms("inner")`). Its
# provide body would emit `audit.record(q)` on every call — that IS the
# interception. The driver never runs it.
SAME_KEY = """
service Db {
  emission[wire, audit, db] fn execute(q: Str) -> Str
}
service Audit {
  emission[log_line] fn record(line: Str)
  fn seen() -> Int
}
extern emission fn wire(q: Str) -> Str = @py { return "row" }
extern emission fn log_line(s: Str)    = @py { pass }

component Inner provides db: Db {
  isolate db in realm("inner")
  provide db { fn execute(q) = emit wire(q) }
}
component AuditSink provides audit: Audit {
  let log = effect Map.new() undo log.drop()
  provide audit {
    fn record(line) { effect log.insert(line, "seen") undo log.remove(line) emit log_line(line) }
    fn seen() = log.size()
  }
}
component Seam requires db: Db, audit: Audit provides db: Db {
  isolate db in realms("inner")
  provide db {
    fn execute(q) {
      let r = emit db.execute(q)
      emit audit.record(q)
      return r
    }
  }
}
"""


def _build_driver(ir):
    """A `_Driver` on the real cordis-py backend, wired exactly as
    `run_command` wires it — the same helper test_router_runtime.py uses."""
    from revl._paths import backends_root  # noqa: PLC0415
    from revl.run import _Driver  # noqa: PLC0415

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    return _Driver(ir, {}, emit, runtime_mod, Context, FiberState)


def test_same_key_route_compiles_and_admits():
    """The same-key shape is not a compile error: it compiles and the `routes`
    entry lands on the seam's IR (item 162's one-element multi-realm bind).
    This runs on any interpreter — no runtime needed to see the IR."""
    from revl import compile_source  # noqa: PLC0415

    ir = compile_source(SAME_KEY, "same_key.rvl")
    by_name = {c["name"]: c for c in ir["components"]}
    assert by_name["Seam"].get("routes") == {"db": {"realms": ["inner"],
                                                    "strategy": None}}


@pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")
def test_a_routes_carrying_provide_body_is_never_executed():
    """The `routes` hole, pinned. A component carrying `routes` is realized as
    a `_Router` proxy by `src/revl/run.py` (the `if comp.get("routes"):
    self._install_router(...)` guard in `_load`) and never plugged as a fiber,
    so its provide body is discarded. The consumer's `db.execute` reaches the
    inner provider directly (returns "row"), and the seam's `audit.record` —
    the whole interception — never fires (`audit.seen() == 0`).

    When this behaviour changes (a seam that runs its body), this test flips
    and forces the change to be acknowledged — §2.5's "the implementation must
    not ride `routes`"."""
    from revl import compile_source  # noqa: PLC0415
    from revl.run import _Router  # noqa: PLC0415

    ir = compile_source(SAME_KEY, "same_key.rvl")

    async def scenario():
        driver = _build_driver(ir)
        module = driver._emit_module(ir)
        await driver._load(ir, module)

        # G2: the consumer resolves `db` to exactly one provider — but it is
        # the router proxy, not the seam's fiber.
        db = driver.root.get("db")
        assert isinstance(db, _Router)

        # the call reaches the inner provider directly; the return is the
        # inner's, unmediated.
        assert db.execute("select 1") == "row"

        # the interception never happened: the seam's provide body (the
        # audit.record emit) was silently discarded.
        audit = driver.root.get("audit")
        assert audit.seen() == 0

        await driver._teardown()
        assert driver.root.reflect.store == {}

    asyncio.run(scenario())
