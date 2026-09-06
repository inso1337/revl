"""Item 254 (docs/design/254-witnessed-network.md): an emission extern that
DECLARES its own `compensate` slot must WIRE that compensation to fire at
teardown — the extern owns the reversal, so no site-spelled `emit ... compensate
...` is needed. This is the runtime half of the compensate-grade network effect:
before this slice the extern's declared compensate was enumerated on the extern
IR (and the audit surface) but py never registered it, so its reversal never ran.

The two-phase item-247 contract still governs it: DISCHARGED on a clean unload
(the compensation never runs, the forward emission stands), and RUN best-effort
in Phase 2 of an abort. These two exit tests pin both edges for the extern-owned
form, mirroring the a5a / a5b split the site-spelled form already has in
test_v1_semantics.py.
"""

from __future__ import annotations

import asyncio
import types

from cordis import Context
from cordis.fiber import FiberState

import emit
from revl import compile_source


# `ping` is a one-way emission extern that OWNS its reversal: every `emit ping`
# registers the extern's declared `unping()` compensation, with no site-spelled
# `compensate`. Both bodies append to a free module global `_probe` the test
# injects before plug, so the test can observe fire-vs-discharge without a
# backend import (a user @py body may not `import runtime`, but may reference an
# ambient host name). `unping` takes no `result` binding — a compensate follows a
# one-way emission, which acquires nothing (lower.py `_check_extern_undo`) — so
# it compensates from constants.
_EXTERNS = """
extern emission fn ping(msg: Str) -> Int compensate unping() = @py {
    _probe.append("ping " + msg)
    return 1
}
extern emission fn unping() -> Int = @py {
    _probe.append("unping fired")
    return 0
}
"""


def _module(src: str, name: str, probe: list) -> types.ModuleType:
    code = emit.emit(compile_source(src))
    module = types.ModuleType(name)
    module.__dict__["_probe"] = probe
    exec(compile(code, f"{name}.py", "exec"), module.__dict__)
    # re-pin after exec so the emitted preamble can never shadow the probe
    module.__dict__["_probe"] = probe
    return module


async def _flush() -> None:
    for _ in range(40):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# a5a for the extern-owned form — discharged on a clean unload
# ---------------------------------------------------------------------------

async def test_extern_compensate_discharged_on_clean_unload():
    probe: list[str] = []
    module = _module(_EXTERNS + """
component CleanEmitter {
  emit ping("hello")
}
""", "extern_comp_clean", probe)
    root = Context()
    emitter = root.plugin(module.CleanEmitter)
    await _flush()
    assert emitter.state is FiberState.ACTIVE
    assert "ping hello" in probe, "the forward emission must fire"

    emitter.dispose()
    await _flush()

    assert "unping fired" not in probe, \
        "an extern-declared compensation is DISCHARGED on a clean unload (a5a)"
    # the forward emission the compensation would have offset stands
    assert probe == ["ping hello"]


# ---------------------------------------------------------------------------
# a5b for the extern-owned form — runs in Phase 2 on abort
# ---------------------------------------------------------------------------

async def test_extern_compensate_runs_on_abort():
    probe: list[str] = []
    # `emit ping` fires and registers its declared compensation, THEN the failing
    # `Pool.open("boom://…")` acquisition aborts the fiber — so the already-
    # registered extern compensation runs in Phase 2 of the abort.
    module = _module(_EXTERNS + """
component AbortEmitter {
  emit ping("hello")
  let pool = effect Pool.open("boom://nope", 1) undo pool.close()
}
""", "extern_comp_abort", probe)
    root = Context()
    emitter = root.plugin(module.AbortEmitter)
    await _flush()

    assert "ping hello" in probe, "the forward emission fired before the abort"
    assert "unping fired" in probe, \
        "the extern-declared compensation must RUN in Phase 2 of an abort (a5b)"
    # the compensation runs AFTER its forward emission, never before it
    assert probe.index("unping fired") > probe.index("ping hello")
    assert emitter.state is not FiberState.ACTIVE
