"""Item 424 gap (b), slice B1 — interposition, measured (no language change).

docs/design/424-dsh-language-gaps.md §2.2 and §2.6 make two claims about what
revl admits TODAY, and B1's job is to pin both as executed facts rather than
recalled ones, so the day either changes something says so:

* the SANCTIONED pattern — a distinct-key wrapper — actually observes a call on
  the real runtime (examples/interpose_observe.rvl), at the cost of re-keying
  the inner provider in its own source; and
* the tempting SAME-key shape (an item-162 one-element route) is admitted ONLY
  in its sanctioned form — a `realms(...)` route that carries NO redundant
  `provide <routed-key>` body. Adding that body back is refused by G2 at compile
  (item 449): a `routes`-carrying component is realized as a `_Router` proxy and
  never plugged as a fiber (`src/revl/run.py`, the `if comp.get("routes"):
  self._install_router(...)` guard in `_load`), so the body — which would be the
  whole interception — could never run. G2 refuses it outright rather than
  discard it silently at load. That is the `routes` hole B1 records so slice B2
  does not inherit it.

Both proofs need execution, so they run on the backend's own venv — the one
with cordis-py installed — and skip with a reason otherwise, never a feint at
passing.
"""

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

# The same-key interposition of §2.2, in its two shapes. Both isolate `db` in
# realm("inner") on the inner provider and route `db` across realms("inner") on
# the seam (item 162's one-element multi-realm bind). They differ in one thing,
# and that difference is the whole point of these two tests:
#
#   * SANCTIONED_SAME_KEY carries NO redundant `provide db` body on the routed
#     key — the header `provides db: Db` plus the route is enough. This is the
#     shape that compiles and admits.
#   * BODY_CARRYING_SAME_KEY adds a hand-written `provide db { … }` body back on
#     the routed key. That body would be the interception (it emits
#     `audit.record(q)` on every call). G2 refuses it at compile, so it can
#     never run.
#
# They are deliberately distinct fixtures: the compiles-and-admits test needs
# the no-body shape to succeed, and the body-refused test needs the body to be
# present so there is something for G2 to refuse. One shared fixture cannot
# serve both — migrating it to satisfy one test breaks the other.

SANCTIONED_SAME_KEY = """
service Db {
  emission[wire] fn execute(q: Str) -> Str
}
extern emission fn wire(q: Str) -> Str = @py { return "row" }

component Inner provides db: Db {
  isolate db in realm("inner")
  provide db { fn execute(q) = emit wire(q) }
}
component Seam requires db: Db provides db: Db {
  isolate db in realms("inner")
}
"""

BODY_CARRYING_SAME_KEY = """
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


def test_same_key_route_compiles_and_admits():
    """The sanctioned same-key shape — a `realms(...)` route with NO redundant
    `provide <routed-key>` body — is not a compile error: it compiles and the
    `routes` entry lands on the seam's IR (item 162's one-element multi-realm
    bind). This runs on any interpreter — no runtime needed to see the IR."""
    from revl import compile_source  # noqa: PLC0415

    ir = compile_source(SANCTIONED_SAME_KEY, "same_key.rvl")
    by_name = {c["name"]: c for c in ir["components"]}
    assert by_name["Seam"].get("routes") == {"db": {"realms": ["inner"],
                                                    "strategy": None}}


def test_a_routes_carrying_provide_body_is_never_executed():
    """The `routes` hole, closed. A seam that routes `db` across `realms(...)`
    AND carries a hand-written `provide db { … }` body on that routed key is
    refused by G2 at compile (item 449). The body — which would be the whole
    interception, emitting `audit.record(q)` on every call — can never run,
    because the program never admits: a `routes`-carrying component is realized
    as a `_Router` proxy and never plugged as a fiber (`src/revl/run.py`, the
    `if comp.get("routes"): self._install_router(...)` guard in `_load`), so the
    body would be silently discarded at load — and G2 refuses it outright rather
    than let that happen.

    This runs on any interpreter — the refusal is a compile fact, no runtime
    needed. When this behaviour changes (a seam that runs its body), the refusal
    flips and forces the change to be acknowledged — §2.5's "the implementation
    must not ride `routes`"."""
    from revl import compile_source  # noqa: PLC0415
    from revl.errors import RevlErrors  # noqa: PLC0415

    with pytest.raises(RevlErrors) as excinfo:
        compile_source(BODY_CARRYING_SAME_KEY, "same_key.rvl")
    message = str(excinfo.value)
    assert "`provide db` in Seam is silently discarded" in message
    assert "realms(" in message
