"""Roadmap item 477 (follow-up 1) — the DECLARED liveness ceiling and the
runtime producer that WIRES it into the landed `LIVENESS_EXPIRED` vocabulary.

PR #496 landed the vocabulary: the `LIVENESS_EXPIRED` root cause, the
`cause_liveness_expired` builder, and the pure `liveness_expired` gate
(tests/test_477_liveness_expiry.py pins those with no runtime). This slice adds
the surface a producer needs and the producer itself:

  * a source-level `liveness <dur>` ceiling per activation, operator-visible and
    lowered to the additive IR key `liveness_ceiling_ms`;
  * an admission G-rule that refuses a ceiling on an activation that CANNOT hang
    (one reaching no emission and no host-call — nothing to be silent about);
  * `_Driver._perform_liveness_expiry`, the runtime producer: a provider silent
    past its declared ceiling is withdrawn with a `LIVENESS_EXPIRED` ROOT, its
    dependents cascading through the ordinary `provider-withdrawn` edge — the
    QUIET case, DISTINCT from a fault (which carries a diagnostic `code`).

`reconcileLivenessFromWorld` on restart stays the stated follow-up in
docs/design/477-liveness-expiry.md. The producer's firing path is exercised with
the fiber-settle boundary stubbed (the boundary the cordis runtime owns), the
same "provable with no runtime" fake-driver posture test_router_runtime takes to
prove `_Router` selection; a `@needs_cordis` end-to-end boots it for real in CI.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.run import _Driver, _components  # noqa: E402


# a provider whose activation acquires a host resource (`effect Pool.open`) can
# hang mid-acquisition, so a ceiling is admissible; its consumer injects `db`.
HANGABLE = """
service Database { fn query(sql: Str) -> List[Row] }
component PgDatabase provides db: Database liveness 1s {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db { fn query(sql) = pool.query(sql) }
}
component UserCache requires db: Database provides cache: Cache {
  provide cache { fn get(k) = db.query(k) }
}
service Cache { fn get(k: Str) -> List[Row] }
"""


# ---- the declared ceiling: grammar + lowering ---------------------------

def test_a_declared_ceiling_lowers_to_additive_ir_metadata():
    ir = compile_source(HANGABLE, "hangable.rvl")
    pg = next(c for c in ir["components"] if c["name"] == "PgDatabase")
    # milliseconds, funnelled through the same duration surface as `cache ttl`
    assert pg["liveness_ceiling_ms"] == 1000
    # additive: a component that declares no ceiling carries no key
    uc = next(c for c in ir["components"] if c["name"] == "UserCache")
    assert "liveness_ceiling_ms" not in uc


def test_duration_units_match_the_cache_ttl_surface():
    def ceiling(clause):
        src = HANGABLE.replace("liveness 1s", clause)
        ir = compile_source(src, "u.rvl")
        pg = next(c for c in ir["components"] if c["name"] == "PgDatabase")
        return pg["liveness_ceiling_ms"]
    assert ceiling("liveness 500ms") == 500
    assert ceiling("liveness 30") == 30_000     # a bare integer is seconds
    assert ceiling("liveness 2m") == 120_000


# ---- the admission G-rule: refuse a ceiling that can never fire ----------

def test_a_ceiling_on_an_activation_that_cannot_hang_is_refused():
    # FixedClock's activation only registers a `provide` block and binds no host
    # crossing — it completes without anything that can stall, so a ceiling on it
    # can never fire and is a category error worth refusing.
    src = """
    service Clock { fn now() -> Int }
    component FixedClock provides c: Clock liveness 1s {
      provide c { fn now() = 0 }
    }
    """
    with pytest.raises(RevlError) as exc:
        compile_source(src, "clock.rvl")
    msg = str(exc.value)
    assert "cannot hang" in msg
    assert "liveness" in msg


def test_a_non_positive_ceiling_is_refused_at_parse():
    src = HANGABLE.replace("liveness 1s", "liveness 0")
    with pytest.raises(RevlError) as exc:
        compile_source(src, "z.rvl")
    assert "positive duration" in str(exc.value)


def test_liveness_is_only_contextual_never_a_reserved_keyword():
    # `liveness` outside the header slot is an ordinary name — a provided key,
    # here — so no existing program that uses the word is broken.
    src = """
    service S { fn ping() -> Int }
    component P provides liveness: S {
      provide liveness { fn ping() = 1 }
    }
    """
    ir = compile_source(src, "name.rvl")
    p = next(c for c in ir["components"] if c["name"] == "P")
    assert "liveness" in p["provides"]
    assert "liveness_ceiling_ms" not in p


# ---- the runtime producer: withdrawal-with-cause, distinct from fault ----

def _fake_driver(ir):
    """A `_Driver` with only the state the producer touches — the fiber-settle
    boundary (the part cordis owns) is the sole stub, so `_perform_liveness_expiry`
    itself runs for real. Mirrors test_router_runtime's fake-runtime posture."""
    drv = _Driver.__new__(_Driver)
    drv.ir = ir
    drv.generation = 1
    drv._seq = 0
    drv._events = []
    drv._observing = None
    drv._settled = []
    drv.FiberState = lambda s: types.SimpleNamespace(name=s)
    return drv


class _HungFiber:
    """A provider fiber that, on `dispose`, settles itself AND its dependent
    exactly as the cordis reactive graph would when a provider is withdrawn —
    the target to DISPOSED, the consumer of its key to PENDING."""

    def __init__(self, drv, target, dependent):
        self._drv, self._target, self._dependent = drv, target, dependent
        self.state = "ACTIVE"   # the pre-withdrawal snapshot reads every fiber

    async def dispose(self):
        self._drv._settled.append((self._target, "ACTIVE", "DISPOSED", None))
        self._drv._settled.append((self._dependent, "ACTIVE", "PENDING", None))


def test_producer_withdraws_a_hung_provider_with_a_liveness_expiry_root():
    ir = compile_source(HANGABLE, "hangable.rvl")
    drv = _fake_driver(ir)
    drv.fibers = {
        "PgDatabase": _HungFiber(drv, "PgDatabase", "UserCache"),
        "UserCache": types.SimpleNamespace(state="ACTIVE"),
    }

    # PgDatabase went silent 1500ms, past its declared 1000ms ceiling.
    report = asyncio.run(drv._perform_liveness_expiry("PgDatabase", silent_ms=1500))
    assert report is not None and report["conforms"]

    trace = wr.Trace(drv._events)

    # the hung provider's OWN withdrawal roots at the expiry — a root cause, with
    # the operator-visible accounting, and NOT a fault (no diagnostic code).
    root = trace.cause_chain("PgDatabase")
    assert root[-1].cause["kind"] == wr.LIVENESS_EXPIRED
    assert root[-1].cause["ceilingMs"] == 1000
    assert root[-1].cause["silentMs"] == 1500
    assert "code" not in root[-1].cause

    # the dependent still tears down through the ordinary provider-withdrawn edge,
    # and its chain walks up to the hung provider's expiry root.
    dep = trace.cause_chain("UserCache")
    assert [f.component for f in dep] == ["UserCache", "PgDatabase"]
    assert dep[0].cause["kind"] == wr.PROVIDER_WITHDRAWN
    assert dep[-1].cause["kind"] == wr.LIVENESS_EXPIRED


def test_the_expiry_root_is_distinct_from_a_fault_withdrawal():
    compile_source(HANGABLE, "hangable.rvl")
    expiry = wr.cause_liveness_expired(ceiling_ms=1000, silent_ms=1500)

    # the producer's own cause-selection: the target roots at the expiry override,
    # a FAULTING target (a FAILED settle whose error classifies) roots at a
    # trigger carrying a diagnostic code. Same method, two honestly-distinct roots.
    hung = _Driver._withdraw_cause("PgDatabase", "PgDatabase", "DISPOSED",
                                   None, {}, root_cause=expiry)
    faulted = _Driver._withdraw_cause(
        "PgDatabase", "PgDatabase", "FAILED",
        RevlError("x.rvl", 1, "host raised", code="R2"), {})
    assert hung["kind"] == wr.LIVENESS_EXPIRED and "code" not in hung
    assert faulted["kind"] != wr.LIVENESS_EXPIRED
    assert faulted.get("code") == "R2"


def test_producer_never_fabricates_an_expiry_on_a_partial_world():
    ir = compile_source(HANGABLE, "hangable.rvl")
    drv = _fake_driver(ir)
    drv.fibers = {"PgDatabase": _HungFiber(drv, "PgDatabase", "UserCache")}

    # silence still inside the ceiling -> not expired, no withdrawal recorded.
    assert asyncio.run(drv._perform_liveness_expiry("PgDatabase", silent_ms=500)) is None
    assert drv._events == []

    # a provider that declared NO ceiling can never expire (defensive gate),
    # even on a long observed silence.
    assert asyncio.run(drv._perform_liveness_expiry("UserCache", silent_ms=10_000)) is None
    assert drv._events == []


def test_ceiling_lookup_reads_the_ir_key():
    ir = compile_source(HANGABLE, "hangable.rvl")
    drv = _Driver.__new__(_Driver)
    drv.ir = ir
    assert drv._liveness_ceiling("PgDatabase") == 1000
    assert drv._liveness_ceiling("UserCache") is None
    assert drv._liveness_ceiling("Nonexistent") is None
    # sanity: the module helper the lookup rides on sees both components
    assert {c["name"] for c in _components(ir)} == {"PgDatabase", "UserCache"}


# ---- end-to-end on the real cordis runtime (CI) -------------------------

try:
    from revl._paths import backends_root  # noqa: E402
    sys.path.insert(0, str(backends_root() / "python"))
    import cordis  # noqa: F401,E402
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under backends/python/.venv/bin/python)")


@needs_cordis
def test_end_to_end_hung_provider_expiry_cascade_on_the_driver():
    """Boot the composition on the real driver, drive a silence past the declared
    ceiling, and assert the recorded cascade roots at the expiry — the producer
    wired to the runtime, not just its cause-selection."""
    from revl._paths import backends_root  # noqa: PLC0415
    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    ir = compile_source(HANGABLE, "hangable.rvl")

    async def scenario():
        drv = _Driver(ir, {}, emit, runtime_mod, Context, FiberState)
        drv.tracing = True
        module = drv._emit_module(ir)
        await drv._load(ir, module)
        report = await drv._perform_liveness_expiry("PgDatabase", silent_ms=1500)
        assert report is not None and report["conforms"]
        trace = wr.Trace(drv._events)
        assert trace.cause_chain("PgDatabase")[-1].cause["kind"] == wr.LIVENESS_EXPIRED
        await drv._teardown()

    asyncio.run(scenario())
