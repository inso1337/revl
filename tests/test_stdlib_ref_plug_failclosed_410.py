"""Item 410 adversarial follow-up: the py plug-time stdlib-ref backstop must
fail CLOSED, not OPEN.

The landed 410 seam advertised "a LOUD plug-time mismatch, never a silent
substitution". The adversarial review found it FAILED OPEN: the hash block only
ran when `resolve_ref_spec_origin` returned a FILE. When an earlier `sys.path`
entry holds a REGULAR `backends`-shaped package (an `__init__.py`) WITHOUT the
`python.<helper>` portion, the static disk walk descends into that foreign
package, finds no leaf, and returns `None`. `origin is None` skipped the entire
hash block, so no refusal fired; the emitted thunk's real
`from <pkg>.<sub>.<helper> import symbol` then imported the foreign package,
running its top-level `__init__.py` (arbitrary side effects, e.g. writing a
PWNED file) BEFORE raising ModuleNotFoundError. Foreign code ran under the guise
of a hash-checked stdlib ref.

The fix: for a `root == "stdlib"` ref, an origin that is not a file INSIDE the
appended install root (including `origin is None`) is a HARD REFUSAL at plug,
naming both the pinned install path and the intercepting foreign one — the
import (and thus the foreign `__init__.py`) never runs.

These tests exercise `plug_refs` directly against a stand-in install so the real
revl install tree is never touched.
"""

from __future__ import annotations

import hashlib
import sys

import pytest

from revl import hostref
from revl.errors import RevlError


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _install(tmp_path, monkeypatch):
    """A stand-in install tree whose `stdlib_root()` is `<install>/stdlib`, so
    the plug's appended install root is `<install>`; the shipped helper lives at
    `<install>/runtimes/pyhost/helper.py` (dotted `runtimes.pyhost.helper`)."""
    install = tmp_path / "install"
    stdlib_dir = install / "stdlib"
    stdlib_dir.mkdir(parents=True)
    _write(install / "runtimes" / "pyhost" / "helper.py",
           "def shout(x):\n    return x.upper()\n")
    monkeypatch.setattr(hostref, "stdlib_root", lambda: stdlib_dir)
    return install


def _stdlib_ir(install):
    helper = install / "runtimes" / "pyhost" / "helper.py"
    return {"externs": [{
        "name": "shout",
        "refs": {"py": {
            "symbol": "shout",
            "path": "runtimes/pyhost/helper.py",
            "sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            "root": "stdlib",
        }},
    }]}


# ---------------------------------------------------------------------------
# HEADLINE security regression: a partial `runtimes` shadow now REFUSES LOUDLY
# and the foreign `__init__.py` is NEVER executed.
# ---------------------------------------------------------------------------

def test_partial_shadow_refuses_loudly_and_foreign_init_never_runs(
        tmp_path, monkeypatch):
    install = _install(tmp_path, monkeypatch)
    ir = _stdlib_ir(install)

    # A FOREIGN, regular `runtimes` package (has `__init__.py`) on an EARLIER
    # sys.path entry, WITHOUT the `pyhost.helper` portion. Its top-level code
    # writes a PWNED sentinel if it is ever imported.
    foreign_root = tmp_path / "foreign"
    sentinel = tmp_path / "PWNED"
    _write(foreign_root / "runtimes" / "__init__.py",
           f"open({str(sentinel)!r}, 'w').write('pwned')\n")

    before = list(sys.path)
    for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
        sys.modules.pop(name, None)
    # foreign root FIRST (earlier entries win on path order); the plug appends
    # the install root AFTER it.
    sys.path.insert(0, str(foreign_root))
    try:
        with pytest.raises(RevlError) as exc:
            hostref.plug_refs(ir, [str(tmp_path / "userroot")])
        msg = str(exc.value)
        # names BOTH paths: the pinned install file and the intercepting foreign
        # package.
        assert "runtimes/pyhost/helper.py" in msg
        assert str(install) in msg
        assert str(foreign_root / "runtimes") in msg
        # and the refusal fired BEFORE any import: the foreign top-level code
        # never ran, so the sentinel was never written.
        assert not sentinel.exists(), \
            "foreign __init__.py executed — the backstop failed OPEN"
        # the foreign package was not even imported
        assert "runtimes" not in sys.modules or \
            sys.modules.get("runtimes") is None
    finally:
        sys.path[:] = before
        for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
            sys.modules.pop(name, None)


def test_missing_helper_refuses_closed_not_open(tmp_path, monkeypatch):
    """No shadow at all, but the pinned helper is absent from the install
    (`origin is None` because the static walk finds nothing): a stdlib ref must
    still REFUSE at plug, never fall through to a first-call import."""
    install = _install(tmp_path, monkeypatch)
    ir = _stdlib_ir(install)
    (install / "runtimes" / "pyhost" / "helper.py").unlink()

    before = list(sys.path)
    for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
        sys.modules.pop(name, None)
    try:
        with pytest.raises(RevlError) as exc:
            hostref.plug_refs(ir, [str(tmp_path / "userroot")])
        assert "runtimes/pyhost/helper.py" in str(exc.value)
    finally:
        sys.path[:] = before
        for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# The legitimate stdlib ref (install file present, hash matches) still resolves.
# ---------------------------------------------------------------------------

def test_legit_stdlib_ref_still_plugs(tmp_path, monkeypatch):
    install = _install(tmp_path, monkeypatch)
    ir = _stdlib_ir(install)
    before = list(sys.path)
    for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
        sys.modules.pop(name, None)
    try:
        # no shadow; the appended install root owns `runtimes` -> resolves,
        # hash matches, no refusal.
        hostref.plug_refs(ir, [str(tmp_path / "userroot")])
        assert str(install) in sys.path
    finally:
        sys.path[:] = before
        for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
            sys.modules.pop(name, None)


def test_present_file_hash_mismatch_still_refuses(tmp_path, monkeypatch):
    """A helper PRESENT in the install root but EDITED since compile still
    refuses with the existing loud-mismatch message shape (unchanged)."""
    install = _install(tmp_path, monkeypatch)
    ir = _stdlib_ir(install)
    # edit the file so its sha differs from the pin
    _write(install / "runtimes" / "pyhost" / "helper.py",
           "def shout(x):\n    return x + '!'\n")
    before = list(sys.path)
    for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
        sys.modules.pop(name, None)
    try:
        with pytest.raises(RevlError) as exc:
            hostref.plug_refs(ir, [str(tmp_path / "userroot")])
        msg = str(exc.value)
        assert "does not match the file pinned at compile" in msg
        assert "runtimes/pyhost/helper.py" in msg
    finally:
        sys.path[:] = before
        for name in ("runtimes", "runtimes.pyhost", "runtimes.pyhost.helper"):
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Finding 3: leaf resolution checks the PACKAGE before the module, per FileFinder.
# ---------------------------------------------------------------------------

def test_leaf_package_wins_over_module(tmp_path):
    """A directory holding BOTH `helper.py` and `helper/__init__.py` must resolve
    to the PACKAGE `__init__.py` (what CPython's FileFinder imports), not the
    `.py` module — else the resolver hashes one file while another executes."""
    base = tmp_path / "base"
    _write(base / "helper.py", "X = 'module'\n")
    _write(base / "helper" / "__init__.py", "X = 'package'\n")
    got = hostref.resolve_ref_spec_origin("helper", [str(base)])
    assert got == str(base / "helper" / "__init__.py"), got


def test_leaf_module_only_still_resolves(tmp_path):
    """When only the `.py` module exists (no package dir), it still resolves."""
    base = tmp_path / "base"
    _write(base / "helper.py", "X = 'module'\n")
    got = hostref.resolve_ref_spec_origin("helper", [str(base)])
    assert got == str(base / "helper.py"), got
