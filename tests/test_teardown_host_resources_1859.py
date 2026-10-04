"""The session teardown report checks that every host resource was released
(issue #1859, slice 2).

`noResidue` used to be `all()` of four cordis counters: the registry, the
provisions, the effect stack and the listeners. Those see a disposer RUN, not a
resource RELEASED, so a release that raised, or an undo that is not the
acquire's release, still read `noResidue: true`. The host trace pairs each
`open`/`new` with its `close`/`drop` (`fault._unreleased_host_resources`), and
the report now carries that as `checks["hostResources"]` with
`detail["unreleased"]`. With no host trace the check cannot run: the report
says `unverified: ["hostResources"]` and never `noResidue: true`.
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a teardown needs a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

SOURCE = """
service S { fn ping() -> Int }
component Db provides s: S {
  let p = effect Pool.open("db://orders", 2) undo p.close()
  provide s { fn ping() = 1 }
}
"""


def _loaded():
    from revl.mcp.session import Session

    session = Session()
    session.load(copy.deepcopy(compile_source(SOURCE, "db.rvl")))
    return session


@needs_cordis
def test_a_clean_release_is_checked_and_reads_no_residue():
    report = _loaded().unload()
    assert report["checks"]["hostResources"] is True
    assert report["detail"]["unreleased"] == []
    assert report["noResidue"] is True
    assert "unverified" not in report


@needs_cordis
def test_a_raising_close_reports_residue_and_names_the_pool(monkeypatch):
    import runtime

    session = _loaded()

    def fail(self):
        raise RuntimeError("injected: the pool would not close")

    monkeypatch.setattr(runtime.Pool, "close", fail)
    report = session.unload()
    # the disposer ran, so the four counters are clean
    assert all(report["checks"][k] for k in
               ("registry", "provisions", "effects", "listeners")), report
    assert report["checks"]["hostResources"] is False
    [unreleased] = report["detail"]["unreleased"]
    assert unreleased.startswith("pool") and "open() with no close()" in unreleased
    assert report["noResidue"] is False


@needs_cordis
def test_with_no_host_trace_the_check_is_unverified_never_clean():
    session = _loaded()
    del session._driver.host_events          # a driver that captured no host trace
    report = session.unload()
    assert report["unverified"] == ["hostResources"]
    assert "hostResources" not in report["checks"]
    assert report["noResidue"] is False
