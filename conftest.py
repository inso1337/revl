"""Repository-wide pytest hook: a required tier's missing toolchain is a failure.

Issue #2264. ``REVL_REQUIRE_TIERS`` (src/revl/test.py) turns an absent-toolchain
skip into a failure, but only for a test that goes through a ``revl.test``
runner. Most go tests do not: they probe ``shutil.which("go")`` themselves and
call ``pytest.skip``, in a ``skipif`` marker, a fixture or the test body. So a
CI job that lost its go toolchain skipped every one of them and still went
green, because a skip is green.

This hook looks at what actually happened rather than at how the test raised
its skip, so a marker, a fixture and a test body are all covered. When the
session is REQUIRED to have a tier (``REVL_REQUIRE_TIERS`` names it, or
``all``), that tier's toolchain is not on PATH, and a test in a file that
probes for the toolchain reports SKIPPED, the skip becomes a failure that says
so. "Probes" means the file itself spells the probe (``which("go")`` or
``go_runtime_reason(``); a probe hidden in a helper module another file
imports is not seen.

It lives at the repository root so it covers ``tests/`` and ``backends/go/``
alike. It never fires where the toolchain is installed, and never fires where
the tier is not required: a laptop without go still skips with a reason. Rust
is handled the same way (issue #2210), and also when cargo is present but a
test skips because cordis-rs does not resolve. Java (issue #2734, the per-tier
support table) probes for a working JDK the same way: without that, the java
crash-recovery cell could read `proved` on a job whose JDK setup had broken and
whose test had quietly skipped.
``tests/test_go_tier_required_2264.py`` runs the go shapes end to end, and
``tests/test_java_tier_required_2734.py`` does the same for java.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

#: Same variable `revl.test.REQUIRE_TIERS_ENV` reads. Parsed here directly so
#: this file imports nothing from revl: it is loaded by every pytest run in the
#: repository, including jobs whose interpreter is not the one under test.
_REQUIRE_ENV = "REVL_REQUIRE_TIERS"

#: tier -> (the binary that proves its toolchain is present, the probe a test
#: file uses to look for it). go is the tier whose tests probe the toolchain
#: directly and that CI has required longest; rust (issue #2210) probes with
#: `which("cargo")` or `rust_runtime_reason(` the same way, in a `skipif`
#: marker or a test body, and bypasses the `revl.test` runners. java probes
#: with `java_runtime_reason(` (src/revl/run_java.py), which reports a missing
#: JDK as a skip reason for the same reason.
_PROBES: dict[str, tuple[str, re.Pattern[str]]] = {
    "go": ("go", re.compile(r"""which\(\s*["']go["']\s*\)|go_runtime_reason\(""")),
    "rust": ("cargo", re.compile(
        r"""which\(\s*["']cargo["']\s*\)|rust_runtime_reason\(""")),
    "java": ("javac", re.compile(r"java_runtime_reason\(")),
}

#: tier -> what a skip reason says when the toolchain is on PATH but unusable.
#: cargo can be present while cordis-rs does not resolve (crates.io down, a
#: cold registry): `rust_runtime_reason` (src/revl/run_rust.py) then answers
#: with the phrases below, and the tests quote it in their skip. That is the same
#: broken provisioning as a missing cargo, so it fails too. Only those phrases:
#: a by-design skip that merely names cordis-rs (an offline build of a crate that
#: needs a warm ~/.cargo, say) stays a skip. "not in the local cargo registry"
#: is the one phrase two spellings share ("cordis-rs is not in ..." from
#: `rust_runtime_reason`, "cordis-rs: not in ..." in test_realm_conformance.py).
#: Java's `java_runtime_reason` (src/revl/run_java.py) reports a binary that is
#: present but not a working JDK (`/usr/bin/javac` is a shim that errors until
#: one is installed) and a JDK older than the 21 the emitter targets; both are
#: unusable provisioning, not an environment fact.
_UNUSABLE: dict[str, re.Pattern[str]] = {
    "rust": re.compile(r"cargo could not resolve cordis-rs"
                       r"|not in the local cargo registry"
                       r"|cargo not on PATH"),
    "java": re.compile(r"no working JDK found|older than 21"),
}

#: tier -> was its toolchain absent when the session started. Measured once,
#: so a test that monkeypatches `shutil.which` cannot change the answer.
_ABSENT: dict[str, bool] = {}

_PROBING_FILES: dict[str, bool] = {}


def _required(raw: str) -> set[str]:
    names = {n for n in raw.replace(",", " ").split() if n}
    return set(_PROBES) if "all" in names else names & set(_PROBES)


def pytest_configure(config: pytest.Config) -> None:
    _ABSENT.clear()
    for tier in _required(os.environ.get(_REQUIRE_ENV, "")):
        binary, _probe = _PROBES[tier]
        _ABSENT[tier] = shutil.which(binary) is None


def _probes(path: str, tier: str) -> bool:
    key = f"{tier}:{path}"
    if key not in _PROBING_FILES:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            text = ""
        _PROBING_FILES[key] = bool(_PROBES[tier][1].search(text))
    return _PROBING_FILES[key]


def _missing_required_tier(item: pytest.Item, reason: object = "") -> str | None:
    """The required tier whose absent (or, for rust, unresolvable) toolchain
    this item's file probes for."""
    path = str(getattr(item, "path", "") or item.fspath)
    for tier in sorted(_ABSENT):
        unusable = tier in _UNUSABLE and _UNUSABLE[tier].search(str(reason))
        if (_ABSENT.get(tier) or unusable) and _probes(path, tier):
            return tier
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.skipped or hasattr(report, "wasxfail"):
        return
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
    tier = _missing_required_tier(item, reason)
    if tier is None:
        return
    report.outcome = "failed"
    why = (f"`{_PROBES[tier][0]}` is not on PATH" if _ABSENT[tier]
           else f"its runtime does not resolve ({_PROBES[tier][0]} is on PATH)")
    report.longrepr = (
        f"the {tier} tier is REQUIRED here ({_REQUIRE_ENV}) but {why}, "
        f"so this test would have measured nothing and reported "
        f"green. It skipped with: {reason}"
    )
