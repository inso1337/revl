"""The playground's Pyodide pin must be a release whose micropip can install a
wheel with ``deps: false``, and the integrity hash must match the file it pins.

``site/js/playground.js`` installs ``site/vendor/cordis-4.0.0-py3-none-any.whl``
with ``deps: false`` because that wheel declares ``pyyaml`` and ``watchdog``
(issue #946) and ``watchdog`` has no wasm32 wheel, so dependency resolution
fails the whole install. That code path was broken by the runtime, not by the
page: in micropip 0.8.0, the version Pyodide v0.27.2 bundles,
``Transaction.add_wheel`` starts the wheel download as a task and awaits it
only inside ``if self.deps:``. With ``deps`` false the task is never awaited,
so ``WheelInfo.install()`` runs while ``_data`` is None and raises

    RuntimeError: Micropip internal error: attempted to install wheel before
    downloading it?

``boot()`` catches it and writes the message into the loading overlay, so the
published page sits on the overlay forever -- the revl wheel installs fine, the
cordis install is the one that dies. micropip 0.9.0 added the missing
``else: await wheel_download_task``, and Pyodide v0.27.6 is the first 0.27.x
release that bundles it. It is still Python 3.12.7 and nothing marked Breaking
separates it from v0.27.2, so it is the smallest runtime move that fixes this.

The pin stops at v0.27.6 rather than moving further: v0.27.7 turns
``enableRunUntilComplete`` on by default, a Breaking change to stack-switching
behaviour, and this page runs the compiler under JSPI. Adopting that default is
a separate decision, not a side effect of a boot fix.

There is no browser in CI, so nothing else here would notice a pin rewind: this
file is the notice. It reads the two published pages and holds three things:

  * both pages are on one Pyodide version and one integrity hash, because the
    two playgrounds share one runtime and a half-done bump is a half-broken
    page;
  * the version is at least the first release that bundles micropip 0.9.0;
  * the integrity hash is the one verified for that version. A version bump
    with the old hash blocks ``pyodide.js`` at the browser and presents as the
    same stuck overlay, so a deliberate bump updates ``VERIFIED_INTEGRITY``
    after loading the page rather than copying a URL alone.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PAGES = (ROOT / "site" / "playground.html", ROOT / "playground" / "index.html")

# The first 0.27.x Pyodide release that bundles micropip 0.9.0, where a
# `deps: false` install awaits its own download. v0.27.2 bundles micropip 0.8.0
# and raises. A later release is fine for this floor, but see the docstring on
# why v0.27.6 is where the pin deliberately stops.
MINIMUM_PYODIDE = (0, 27, 6)

# sha384 of https://cdn.jsdelivr.net/pyodide/v0.27.6/full/pyodide.js, and the
# runtime the playground was last loaded and compiled on.
VERIFIED_INTEGRITY = {
    "0.27.6": "sha384-D7p9XIGSPyIyKBz6PTuzHm2CnAKahz9n9ezzdJctZouMULIQgZI7UPR46u9fqSbO",
}

_SCRIPT = re.compile(
    r"<script\b[^>]*\bsrc=\"https://cdn\.jsdelivr\.net/pyodide/"
    r"v(?P<version>\d+\.\d+\.\d+)/full/pyodide\.js\"[^>]*>"
)
_INTEGRITY = re.compile(r"\bintegrity=\"(?P<integrity>sha384-[A-Za-z0-9+/=]+)\"")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _pins() -> dict[str, tuple[str, str]]:
    """Map each published page to the (version, integrity) it pins."""
    pins: dict[str, tuple[str, str]] = {}
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        tag = _SCRIPT.search(text)
        assert tag is not None, (
            f"{page.relative_to(ROOT)} does not load Pyodide from the jsDelivr pin"
        )
        integrity = _INTEGRITY.search(tag.group(0))
        assert integrity is not None, (
            f"{page.relative_to(ROOT)} pins Pyodide without an SRI integrity hash"
        )
        pins[str(page.relative_to(ROOT))] = (
            tag.group("version"),
            integrity.group("integrity"),
        )
    return pins


def test_both_published_pages_pin_one_pyodide_runtime() -> None:
    pins = _pins()
    assert len(set(pins.values())) == 1, (
        "site/playground.html and playground/index.html must pin the same "
        f"Pyodide version and integrity hash, got {pins}"
    )


def test_pyodide_pin_bundles_a_micropip_that_installs_a_deps_false_wheel() -> None:
    for page, (version, _integrity) in _pins().items():
        assert _version_tuple(version) >= MINIMUM_PYODIDE, (
            f"{page} pins Pyodide v{version}, which bundles a micropip that "
            "cannot install a wheel with deps: false (see this file's module "
            f"docstring); the floor is v{'.'.join(map(str, MINIMUM_PYODIDE))}"
        )


def test_integrity_hash_is_the_verified_one_for_the_pinned_pyodide() -> None:
    for page, (version, integrity) in _pins().items():
        assert version in VERIFIED_INTEGRITY, (
            f"{page} pins Pyodide v{version}, which has no verified integrity "
            "hash recorded in VERIFIED_INTEGRITY; load the page on the new "
            "version and record the sha384 of its pyodide.js"
        )
        assert integrity == VERIFIED_INTEGRITY[version], (
            f"{page} pins Pyodide v{version} with integrity {integrity}, but "
            f"that version was verified as {VERIFIED_INTEGRITY[version]}; a "
            "stale hash blocks the script and the page hangs on its overlay"
        )
