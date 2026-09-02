"""The gauntlet verb — candidate in, graded dossier out.

`revl_swap` is binary (admit or refuse); `revl_gauntlet` grades. It runs a
battery in an isolated scratch session and returns a dossier that separates
what was proved (admission, derived teardown) from what was tested with counts
(a real boot/unload no-residue lifecycle) from what remains claimed (the
enumerated G8 extern boundary), with fault-sweep (item 30) and
inverse-round-trip (item 26) slots present but `pending`.

The frontend properties — admission proved/refused, the boundary enumerated,
the pending slots, and that a rejected candidate is *graded not crashed* — run
everywhere. Only the lifecycle no-residue battery needs the cordis-py runtime,
so those assertions carry the `@needs_runtime` marker, like test_mcp_session.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.server import handle  # noqa: E402

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

# a candidate that reaches host code — its extern lands on the G8 boundary,
# the surface the dossier reports as *claimed* rather than proved
WITH_EXTERN = """extern pure fn sha256_hex(data: Str) -> Str
  = @py { import hashlib; return hashlib.sha256(data.encode()).hexdigest() }
service Hash { fn digest(d: Str) -> Str }
component Hasher provides hash: Hash {
  provide hash { fn digest(d) = sha256_hex(d) }
}
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _gauntlet(**arguments) -> dict:
    return _call("revl_gauntlet", arguments)


@pytest.fixture
def trusted_authoring():
    """A candidate that DECLARES a `@py` host body is agent-authored host code,
    which the MCP server refuses under its default (closed) authoring trust
    before any handler runs. A test whose subject is the dossier, not the trust
    level, states the trusted-author premise it always relied on
    (`server.AuthoringTrust`, `revl mcp serve --author-trust trusted`)."""
    from revl.mcp import server as server_mod
    before = server_mod.AUTHORING
    server_mod.set_authoring_trust(host_code=True)
    yield
    server_mod.AUTHORING = before


needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the lifecycle battery needs the cordis-py runtime — install it "
           "with `sh backends/python/setup.sh`, then run under "
           "`backends/python/.venv/bin/pytest`",
)


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


# ------------------------------------------------------ the dossier shape

def test_the_verb_is_advertised():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "revl_gauntlet" in names


def test_a_clean_candidate_is_graded_admissible_with_every_section():
    d = _gauntlet(source=CACHE)
    assert d["ok"] is True
    assert d["verdict"] == "admissible"

    # proved: admission + derived teardown
    assert d["proved"]["admission"]["status"] == "proved"
    assert d["proved"]["admission"]["kind"] == "proved"
    assert d["proved"]["teardown"]["status"] == "derived"
    assert d["proved"]["teardown"]["teardownOrder"] == ["MemCache"]

    # tested: the lifecycle section is present with a counts block regardless
    # of whether the runtime is installed (unavailable still carries counts)
    lifecycle = d["tested"]["lifecycle"]
    assert lifecycle["kind"] == "tested"
    assert set(lifecycle["counts"]) == {"checks", "passed", "failed"}

    # claimed: the G8 boundary is enumerated (this candidate reaches nothing)
    boundary = d["claimed"]["boundary"]
    assert boundary["kind"] == "claimed"
    assert boundary["status"] == "enumerated"
    assert boundary["externs"] == []
    assert boundary["count"] == 0


def test_the_claimed_boundary_enumerates_reached_host_code(trusted_authoring):
    """`WITH_EXTERN` DECLARES a `@py` host body, so the default authoring trust
    refuses it before the gauntlet runs; the dossier's claimed-boundary
    reporting is the subject here, not the trust level."""
    d = _gauntlet(source=WITH_EXTERN)
    assert d["verdict"] == "admissible"
    boundary = d["claimed"]["boundary"]
    assert boundary["status"] == "enumerated"
    assert "sha256_hex" in boundary["externs"]
    assert boundary["count"] == 1


def test_the_fault_sweep_and_inverse_slots_are_present_but_pending():
    d = _gauntlet(source=CACHE)
    fault = d["pending"]["faultSweep"]
    inverse = d["pending"]["inverseRoundTrip"]
    assert fault["status"] == "pending"
    assert fault["roadmapItem"] == 30
    assert inverse["status"] == "pending"
    assert inverse["roadmapItem"] == 26
    # a pending slot advertises where its counts will go
    assert "counts" in fault and fault["counts"] is None


# ------------------------------------------------------ graded, not crashed

def test_a_candidate_that_fails_admission_is_graded_not_thrown():
    bad = CACHE.replace("fn size() = 0", 'fn size() = "nope"')
    d = _gauntlet(source=bad)
    # the gauntlet ran successfully and produced a dossier — ok is True
    assert d["ok"] is True
    assert d["verdict"] == "rejected"
    admission = d["proved"]["admission"]
    assert admission["status"] == "refused"
    diagnostic = admission["diagnostics"][0]
    assert diagnostic["code"] == "T1"
    # the structured verdict carries the exact fix end-to-end through the
    # gauntlet, not just the guarantee — an agent needs no second verb (item 286)
    from revl.diagnostics import FIXES

    assert diagnostic["fix"] == FIXES["T1"]
    # downstream sections did not run, and no scratch session was booted
    assert d["tested"]["lifecycle"]["status"] == "not-run"
    assert d["claimed"]["boundary"]["status"] == "not-run"
    assert d["scratch"]["booted"] is False
    # even a refused candidate carries the (still pending) future slots
    assert d["pending"]["faultSweep"]["status"] == "pending"


def test_missing_source_and_files_is_a_clean_usage_error():
    d = _gauntlet()
    assert d["ok"] is False
    assert d["diagnostics"][0]["category"] == "session"


# ------------------------------------------------------ tested + isolation

@needs_runtime
def test_the_lifecycle_battery_boots_unloads_and_counts_no_residue():
    d = _gauntlet(source=CACHE)
    lifecycle = d["tested"]["lifecycle"]
    assert lifecycle["status"] == "passed"
    assert lifecycle["ran"] is True
    assert lifecycle["counts"] == {"checks": 4, "passed": 4, "failed": 0}
    assert all(lifecycle["checks"].values())


@needs_runtime
def test_the_scratch_session_leaves_the_live_composition_untouched():
    # a live composition is running and answering
    _call("revl_load", {"source": CACHE})
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0

    # grade a *different* candidate against it — admission is against the
    # session, and the battery boots the candidate in a scratch session
    d = _gauntlet(source=CACHE.replace("fn size() = 0", "fn size() = 99"))
    assert d["verdict"] == "admissible"
    assert d["proved"]["admission"]["against"] == "session"
    assert d["tested"]["lifecycle"]["status"] == "passed"

    # the live composition is completely unchanged: still the old provider,
    # still answering 0, never swapped to the graded candidate
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0
    state = _call("revl_state", {})
    assert state["providedKeys"] == ["cache"]
    assert state["components"] == [{"name": "MemCache", "state": "ACTIVE"}]


@needs_runtime
def test_admission_against_a_live_composition_is_reported():
    _call("revl_load", {"source": CACHE})
    # a candidate that adds an operation the running Cache service lacks would
    # drift the interface; a compatible one links cleanly
    d = _gauntlet(source=CACHE.replace("fn size() = 0", "fn size() = 7"))
    assert d["proved"]["admission"]["status"] == "proved"
    assert d["proved"]["admission"]["against"] == "session"
