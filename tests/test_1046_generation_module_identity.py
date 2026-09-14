"""#1046: an emitted generation's `sys.modules` key carries its driver's identity.

`_Driver._emit_module` registers every generation it emits in `sys.modules` (the
emitted record dataclasses need the entry at class-creation time) and reclaims
the entry again once every component that generation defined is disposed
(item 541). The key used to be `revl_run_gen{N}`, numbered from `_Driver.generation`
-- a PER-DRIVER counter written into a PROCESS-GLOBAL table. Two live
compositions in one process therefore both emit `revl_run_gen1`: the second
overwrites the first's entry, and either driver's `_evict_dead_modules` then pops
a key belonging to the other. The name was not the identity it was being used as.

Nothing re-resolves a generation through `sys.modules` after exec, so no
single-driver behaviour depended on the collision -- it was latent. Its one
visible symptom was in tests: a leftover generation module did not *offset* a
later file's count, it collided by name with one of that file's own generations,
so the total landed one short (#1021, contained in PR #1033 by an autouse
fixture).

The key is now `revl_run_gen_d{driver}_{generation}`, from a process-global
driver ordinal, and the sweep pops a key only while it still holds that driver's
own module object. These drive both halves against real `_Driver` objects.

No runtime is needed: the emitter is stubbed, so the emitted module is trivial
and `exec` never reaches cordis -- the same approach `tests/test_trace_121.py`
uses to drive `_emit_module` on a bare IR.
"""

from __future__ import annotations

import sys
import types

from revl import run


class _StubEmitter:
    """The one `emit` surface `_emit_module` uses, returning trivial source."""

    def emit(self, ir: dict) -> str:
        return "REVL_EMITTED = True\n"


def _bare_driver():
    """A `_Driver` holding only what `_emit_module` reads on a bare IR.

    Built with `__new__` so no cordis `Context` is needed. `_gen_prefix` is
    deliberately NOT set here: allocating this driver's slice of the generation
    namespace is the behaviour under test.
    """
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = types.SimpleNamespace()  # carries no optional seam
    driver.generation = 0
    driver.emit = _StubEmitter()
    driver.root_dirs = []
    driver.config = {}
    driver.secrets = None
    driver.recorder = None
    driver.wal_path = None
    driver.fibers = {}
    driver._route_disposers = {}
    driver._gen_modules = {}
    return driver


def _ir(*components: str) -> dict:
    return {"components": [{"name": c} for c in components], "manifest": {}}


def _drop(*names: str) -> None:
    for name in names:
        sys.modules.pop(name, None)


# --------------------------------------------------------------------------- #
# 1. two live drivers in one process do not share a key.
# --------------------------------------------------------------------------- #

def test_two_live_drivers_get_distinct_generation_keys():
    """The collision itself. Both drivers are on their FIRST generation, which
    is exactly the case `revl_run_gen{N}` could not tell apart: two `Session`
    objects, a `revl run` hosting a second driver, an MCP session alongside a
    test's own. Before #1046 both names are `revl_run_gen1`, so the second
    registration silently replaces the first's module."""
    a, b = _bare_driver(), _bare_driver()
    mod_a = mod_b = None
    try:
        mod_a = a._emit_module(_ir("A"))
        mod_b = b._emit_module(_ir("B"))

        # distinct keys, and each key still resolves to ITS OWN driver's module.
        assert mod_a.__name__ != mod_b.__name__
        assert sys.modules[mod_a.__name__] is mod_a
        assert sys.modules[mod_b.__name__] is mod_b
        # each driver tracks only its own generation for the sweep to act on.
        assert set(a._gen_modules) == {mod_a.__name__}
        assert set(b._gen_modules) == {mod_b.__name__}
        # and the namespace a sweep matches on is unchanged.
        assert mod_a.__name__.startswith("revl_run_gen")
        assert mod_b.__name__.startswith("revl_run_gen")
    finally:
        _drop(*(m.__name__ for m in (mod_a, mod_b) if m is not None))


def test_one_drivers_generations_all_share_that_drivers_prefix():
    """A driver's slice of the namespace is stable across its own generations
    (a swap/reload, or a run of admit turns), so the prefix identifies the
    driver and the suffix identifies the generation within it."""
    driver = _bare_driver()
    names = []
    try:
        for _ in range(3):
            driver.fibers = {}          # the previous generation is disposed
            names.append(driver._emit_module(_ir("A")).__name__)
        prefix = driver._gen_prefix
        assert all(n.startswith(prefix) for n in names)
        assert [n[len(prefix):] for n in names] == ["1", "2", "3"]
    finally:
        _drop(*names)


# --------------------------------------------------------------------------- #
# 2. one driver's eviction never reaches another driver's module.
# --------------------------------------------------------------------------- #

def test_one_drivers_eviction_leaves_another_drivers_module_alone():
    """The consequence of the collision. Driver A's component is still LIVE, so
    A's generation must survive; driver B's is disposed, so B's sweep reclaims
    B's own entry. Before #1046 both entries are the one key `revl_run_gen1`, so
    B's sweep deletes the module A is still running on."""
    a, b = _bare_driver(), _bare_driver()
    mod_a = mod_b = None
    try:
        mod_a = a._emit_module(_ir("A"))
        a.fibers = {"A": object()}      # A's component is live
        mod_b = b._emit_module(_ir("B"))
        b.fibers = {}                   # B's component is disposed

        b._evict_dead_modules()

        # B reclaimed its own generation...
        assert mod_b.__name__ not in sys.modules
        assert not b._gen_modules
        # ...and left A's live one exactly as it was.
        assert sys.modules.get(mod_a.__name__) is mod_a
        assert set(a._gen_modules) == {mod_a.__name__}
    finally:
        _drop(*(m.__name__ for m in (mod_a, mod_b) if m is not None))


def test_eviction_does_not_pop_a_key_something_else_now_owns():
    """Object identity, not the name, is what the sweep reclaims on.

    `_gen_prefix` already makes a foreign writer under this key a thing that
    should not happen; the sweep does not depend on that being true. A key whose
    entry is no longer this driver's module is dropped from the driver's own
    bookkeeping and left in `sys.modules` for whoever owns it now."""
    driver = _bare_driver()
    mod = None
    try:
        mod = driver._emit_module(_ir("A"))
        name = mod.__name__
        usurper = types.ModuleType(name)
        sys.modules[name] = usurper

        driver.fibers = {}
        driver._evict_dead_modules()

        assert sys.modules[name] is usurper     # not reclaimed
        assert name not in driver._gen_modules  # but no longer tracked
    finally:
        _drop(*([mod.__name__] if mod is not None else []))


# --------------------------------------------------------------------------- #
# 3. non-vacuity: item 541's reclaim still works for a single driver.
# --------------------------------------------------------------------------- #

def test_a_single_driver_still_reclaims_its_own_dead_generation():
    """Passes before and after #1046: the fix must not cost the reclaim.

    One driver, one generation, its component disposed -- the `sys.modules`
    entry is freed rather than pinned for the process lifetime (#541)."""
    driver = _bare_driver()
    mod = None
    try:
        mod = driver._emit_module(_ir("A"))
        name = mod.__name__
        assert sys.modules[name] is mod

        driver.fibers = {"A": object()}
        driver._evict_dead_modules()
        assert name in sys.modules, "a live component's generation was reclaimed"

        driver.fibers = {}
        driver._evict_dead_modules()
        assert name not in sys.modules
        assert name not in driver._gen_modules
    finally:
        _drop(*([mod.__name__] if mod is not None else []))


def test_a_superseded_generation_is_reclaimed_at_the_next_emit():
    """Also passes before and after: `_emit_module` sweeps before it claims its
    own slot, so a swap/reload does not grow the table by one dead entry per
    generation (#541)."""
    driver = _bare_driver()
    names = []
    try:
        first = driver._emit_module(_ir("A"))
        names.append(first.__name__)
        driver.fibers = {}  # the swap disposed it
        second = driver._emit_module(_ir("A"))
        names.append(second.__name__)

        assert first.__name__ not in sys.modules
        assert sys.modules[second.__name__] is second
        assert set(driver._gen_modules) == {second.__name__}
    finally:
        _drop(*names)
