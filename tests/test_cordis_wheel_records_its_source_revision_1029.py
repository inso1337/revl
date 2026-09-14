"""The committed cordis wheel must say which revision built it (#1029).

`backends/python/setup.sh` hard-checks-out `CORDIS_PY_PIN`, and
`site/build.py` packages that checkout as
`site/vendor/cordis-4.0.0-py3-none-any.whl`. Nothing joined the two. The wheel
carried no record of its source at all, so bumping the pin without re-running
`site/build.py` left the committed artifact serving an older runtime than the
suite was validated against, and nothing went red. The offline install path the
docs hand a consumer (`pip install site/vendor/cordis-4.0.0-py3-none-any.whl`,
added by #967) reads that wheel, so the stale runtime reached them with no
signal either.

This is the third instance of one shape in this area: a documented mechanism
with nothing enforcing it. #967 found the README stating pin `1316174` while
`setup.sh` checked out `1c5e6f17`, and a `CORDIS_PY_REV` override knob no
script in the repo reads. Both read exactly like working ones.

WHAT CLOSES IT. `site/build.py` now writes the clone's `git rev-parse HEAD`
into `cordis-4.0.0.dist-info/REVISION`, and this file compares that recorded
revision to `CORDIS_PY_PIN`. A repin with no rebuild reds here, and so does a
rebuild from a clone that had drifted off the pin.

WHY THAT RECORD AND NOT ANOTHER. A file beside the wheel does not survive the
artifact: copy or install the wheel and the provenance is gone, and it can
drift from the wheel it describes, which is this same class one level up. A
dist-info member survives both, is installed by pip so an installed cordis can
answer for itself, and is listed in RECORD like every other member, so it
cannot be edited in place to claim a different revision while the wheel still
installs. It is not a new METADATA field because `CORDIS_METADATA` is a fixed
declaration of what the package requires, pinned byte-for-byte by
`tests/test_cordis_wheel_declares_its_deps_946.py`; a value that changes on
every repin does not belong in a constant whose point is that it does not.

WHAT MAKES THE RECORD HONEST. Recording setup.sh's literal would make this
gate compare a string to itself. `test_the_recorded_revision_follows_the_clone`
below builds a wheel from a throwaway git repo and checks that the record
follows that repo's HEAD, and that a dirty clone is refused rather than
recorded, because a revision that does not describe the packaged bytes is
worse than none.

Static and stdlib-only apart from `git` on a temp repo, so it rides the
`frontend` job's plain `pytest tests/ -q`. The one leg that needs the real
cordis-py clone (a fresh build compared member by member, the mechanism
`tools/check_site_wheel.py` uses for the revl wheels) skips where the clone is
absent and runs for real in `frontend-cordis`, which provisions it via
`sh backends/python/setup.sh`. Every other leg here runs everywhere.

Sibling of `tests/test_cordis_wheel_declares_its_deps_946.py` (what the wheel
DECLARES) and `tests/test_python_runtime_install_paths_967.py` (that the
documented way to get it still works).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WHEEL = ROOT / "site" / "vendor" / "cordis-4.0.0-py3-none-any.whl"
DIST_INFO = "cordis-4.0.0.dist-info"
BUILD = ROOT / "site" / "build.py"
SETUP = ROOT / "backends" / "python" / "setup.sh"

SHA_RE = re.compile(r"[0-9a-f]{40}")


def _build_module():
    spec = importlib.util.spec_from_file_location("_site_build_1029", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pin() -> str:
    """The revision `backends/python/setup.sh` hard-checks-out by default."""
    text = SETUP.read_text(encoding="utf-8")
    m = re.search(r'(?m)^CORDIS_PY_PIN="\$\{CORDIS_PY_PIN:-([^}]*)\}"', text)
    assert m is not None, "setup.sh no longer assigns CORDIS_PY_PIN with a default"
    return m.group(1)


def _member(name: str) -> bytes:
    with zipfile.ZipFile(WHEEL) as whl:
        return whl.read(f"{DIST_INFO}/{name}")


def _recorded_revision() -> str:
    return _member("REVISION").decode("utf-8").strip()


def _member_hashes(wheel: Path) -> dict[str, str]:
    """arcname -> sha256 of its bytes, ignoring the zip's own timestamps.

    Byte-for-byte is the wrong comparison for a wheel: `zipfile.writestr`
    stamps each member with the wall-clock build time, so two builds of
    identical source never match byte-wise. Same reasoning, same shape as
    `tools/check_site_wheel.py`.
    """
    with zipfile.ZipFile(wheel) as zf:
        return {n: hashlib.sha256(zf.read(n)).hexdigest() for n in zf.namelist()}


# --------------------------------------------------------------------------
# The pin is a full sha. Control: true before and after #1029, so a run where
# every assertion below passes vacuously would still have to explain this one.
# --------------------------------------------------------------------------
def test_setup_sh_pins_a_full_commit_sha():
    pin = _pin()
    assert SHA_RE.fullmatch(pin), pin


def test_the_committed_wheel_records_the_revision_it_was_built_from():
    # One name, asserted against the builder rather than restated: renaming the
    # member in site/build.py without rebuilding, or rebuilding without
    # updating this file, both have to show up as a failure here.
    assert _build_module().CORDIS_REVISION_MEMBER == "REVISION"
    with zipfile.ZipFile(WHEEL) as whl:
        names = set(whl.namelist())
    assert f"{DIST_INFO}/REVISION" in names, (
        "the committed cordis wheel carries no record of its source revision; "
        "rebuild it with `python3 site/build.py`"
    )
    raw = _member("REVISION").decode("utf-8")
    assert SHA_RE.fullmatch(raw.strip()), repr(raw)
    # One line, nothing else: the value is read by a gate, not by a human
    # skimming a changelog, and a second field is a second thing to drift.
    assert raw == raw.strip() + "\n", repr(raw)


def test_the_recorded_revision_is_the_pin_setup_sh_checks_out():
    # The whole of #1029. Bump CORDIS_PY_PIN without re-running site/build.py
    # and this is what reds; until now nothing did, and the committed wheel
    # silently served an older runtime than the suite validated against.
    recorded = _recorded_revision()
    pin = _pin()
    assert recorded == pin, (
        f"site/vendor/{WHEEL.name} was built from cordis-py {recorded}, but "
        f"backends/python/setup.sh pins {pin}. Rebuild the wheel against the "
        "pin (`sh backends/python/setup.sh && python3 site/build.py`) and "
        "commit it, or the offline install path serves a runtime the suite "
        "never validated."
    )


def test_the_recorded_revision_is_covered_by_the_record():
    # An unlisted dist-info member makes the wheel fail to install ("hash
    # mismatch"), and a member listed with a stale hash does too. Both matter
    # here beyond hygiene: RECORD is what stops the revision being edited in
    # place to claim a pin the bytes were never built from.
    with zipfile.ZipFile(WHEEL) as whl:
        record = whl.read(f"{DIST_INFO}/RECORD").decode("utf-8").splitlines()
    line = next(
        (ln for ln in record if ln.split(",")[0] == f"{DIST_INFO}/REVISION"), None
    )
    assert line is not None, "REVISION is not listed in the wheel's RECORD"
    _, digest, size = line.split(",")
    raw = _member("REVISION")
    assert int(size) == len(raw)
    want = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
    assert digest == f"sha256={want}"


def test_the_recorded_revision_follows_the_clone(tmp_path):
    # What keeps the gate above from comparing a string to itself: the record
    # is the CLONE's HEAD, read at build time, not a copy of setup.sh's
    # literal. Built here from a throwaway git repo, so it needs no cordis-py
    # and no network.
    clone = tmp_path / "fake-cordis"
    src = clone / "src" / "cordis"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    git = ("git", "-C", str(clone))
    subprocess.run((*git[:1], "init", "-q", str(clone)), check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "t"),
                       ("commit.gpgsign", "false")):
        subprocess.run((*git, "config", key, value), check=True)
    subprocess.run((*git, "add", "-A"), check=True)
    subprocess.run((*git, "commit", "-qm", "seed"), check=True)
    head = subprocess.run(
        (*git, "rev-parse", "HEAD"), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head != _pin(), "the throwaway repo must not coincide with the pin"

    build = _build_module()
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    env_key = "CORDIS_PY"
    previous = os.environ.get(env_key)
    os.environ[env_key] = str(clone)
    try:
        build.build_cordis_wheel(vendor)
        with zipfile.ZipFile(vendor / "cordis-4.0.0-py3-none-any.whl") as whl:
            recorded = whl.read(f"{DIST_INFO}/REVISION").decode("utf-8").strip()
        assert recorded == head, (recorded, head)

        # A dirty clone's HEAD does not describe the bytes being packaged. A
        # record that does not describe the bytes would let the gate pass over
        # an artifact that disagrees with the pin, so the build refuses.
        (src / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
        with pytest.raises(SystemExit) as caught:
            build.build_cordis_wheel(vendor)
        assert "uncommitted changes" in str(caught.value)
    finally:
        if previous is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = previous


def test_the_committed_wheel_matches_a_fresh_build_of_the_pinned_clone(tmp_path):
    # The content half, and the one leg that needs the runtime clone. The
    # recorded revision says what the wheel CLAIMS; this says the bytes agree
    # with it, using the member-set + per-member sha256 comparison
    # `tools/check_site_wheel.py` uses for the revl wheels.
    #
    # Skipped where there is no clone (the `frontend` job, a fresh checkout);
    # real in `frontend-cordis`, which runs `sh backends/python/setup.sh` and
    # therefore has the clone AT the pin. The legs above are what hold on a
    # checkout with no runtime, so nothing here is gated on this one running.
    clone = Path(os.environ.get("CORDIS_PY", ROOT / "backends" / "python" / ".cordis-py"))
    if not (clone / "src" / "cordis").is_dir():
        pytest.skip(f"no cordis-py clone at {clone}")

    build = _build_module()
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    build.build_cordis_wheel(vendor)
    fresh = _member_hashes(vendor / "cordis-4.0.0-py3-none-any.whl")
    committed = _member_hashes(WHEEL)

    added = sorted(set(fresh) - set(committed))
    removed = sorted(set(committed) - set(fresh))
    changed = sorted(n for n in fresh if n in committed and fresh[n] != committed[n])
    assert not (added or removed or changed), (
        f"site/vendor/{WHEEL.name} disagrees with a fresh build of {clone}: "
        f"missing={added} stale={removed} drifted={changed}. Rebuild it with "
        "`python3 site/build.py` and commit the result."
    )
