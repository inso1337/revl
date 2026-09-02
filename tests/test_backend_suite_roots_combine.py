"""The backend test roots must stay combinable in ONE pytest process (419a).

Roadmap item 419a was reported as `pytest backends/wasm/ backends/python/tests/`
in one process yielding 91 spurious failures while each root alone was green:
both roots ship a same-named ``emit.py``, and a bare ``import emit`` bound
whichever directory won the race onto ``sys.path``. That was fixed by pinning
(``backends/python/tests/conftest.py``'s ``_load_and_pin``, guarded by
``backends/python/tests/test_bare_import_pin.py``) and a CI step that actually
runs the two roots together.

The DECISION behind that fix is "the roots are co-importable", not "those two
roots are co-importable". The distinction matters, because the same trap was
still live one root over: ``backends/go/`` and ``backends/wasm/`` each shipped a
``test_router_exec.py``, and pytest's rootdir-relative module naming (no
``__init__.py`` in either directory) makes two collectable files with the same
basename an outright COLLECTION ERROR — ``pytest backends/``, the obvious
command, refused to run at all. Both were renamed to tier-qualified basenames.

These tests keep the decision true for files nobody has written yet. They are
pure filesystem checks, so they cost nothing and need no toolchain.
"""

import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ROOT / "backends"


def _collectable_test_files() -> list[Path]:
    """Every file pytest would collect under `backends/`, ignoring caches and
    the vendored/committed trees that carry no python tests."""
    skip = {"__pycache__", ".venv", ".cordis-py", "node_modules", "target",
            "golden", "stubs"}
    found = []
    for path in BACKENDS.rglob("test_*.py"):
        if any(part in skip for part in path.relative_to(BACKENDS).parts):
            continue
        found.append(path)
    return found


def test_no_two_backend_test_files_share_a_basename():
    """None of the backend roots carries a python package marker, so pytest
    derives a test module's import name from its BASENAME alone. Two files
    named alike anywhere under `backends/` therefore collide in `sys.modules`
    and pytest aborts the whole run with `import file mismatch` — a false red
    for a reason that has nothing to do with the code under test, which is
    exactly the 419a trap. Qualify the basename by tier instead."""
    by_name: dict[str, list[str]] = collections.defaultdict(list)
    for path in _collectable_test_files():
        by_name[path.name].append(str(path.relative_to(ROOT)))
    clashes = {name: sorted(paths) for name, paths in by_name.items()
               if len(paths) > 1}
    assert not clashes, (
        "backend test files sharing a basename break `pytest backends/` at "
        f"collection (roadmap 419a): {clashes}")


def test_the_backend_roots_carry_no_package_markers():
    """The basename rule above holds only while the roots stay rootdir-relative.
    If a backend ever grows an `__init__.py`, module names become dotted and the
    check above stops being the right one — fail here so the guard is re-derived
    rather than silently weakened."""
    markers = sorted(
        str(p.relative_to(ROOT))
        for p in BACKENDS.rglob("__init__.py")
        if not any(part in {"__pycache__", ".venv", ".cordis-py",
                            "node_modules", "target"}
                   for part in p.relative_to(BACKENDS).parts))
    assert markers == [], (
        "a backend root became a package; re-derive the basename guard in this "
        f"file before assuming it still holds: {markers}")


def test_the_same_named_backend_modules_are_still_the_known_set():
    """`emit.py` in six tiers plus one `runtime.py` is the collision surface the
    419a pin covers. A NEW same-named module appearing across roots is the same
    hazard arriving somewhere the pin does not reach, so name it here (and pin
    it) rather than discovering it from a combined run months later."""
    by_name: dict[str, list[str]] = collections.defaultdict(list)
    for path in BACKENDS.glob("*/*.py"):
        if path.name.startswith("test_"):
            continue
        by_name[path.name].append(path.parent.name)
    shared = {name: sorted(dirs) for name, dirs in by_name.items()
              if len(dirs) > 1}
    assert shared == {
        "emit.py": ["go", "java", "python", "rust", "typescript", "wasm"],
        "demo.py": ["python", "wasm"],
    }, (
        "the set of same-named backend modules changed; a bare `import <name>` "
        "in either suite now binds whichever root won the sys.path race — "
        "extend `backends/python/tests/conftest.py`'s `_load_and_pin` (and its "
        f"guard test) before updating this expectation: {shared}")
