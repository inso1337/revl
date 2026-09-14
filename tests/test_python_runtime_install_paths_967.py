"""The two ways to obtain the cordis-py runtime must both be real (#967).

`cordis` is the Python tier's runtime foundation and nothing tracked under
`backends/python` supplies it. There are exactly two ways to get it, and a
consumer who vendors revl by git ref can only use the second:

  * `sh backends/python/setup.sh`, which clones the pinned fork into a
    git-ignored `.cordis-py/`. Every diagnostic in the tree names this one
    (`src/revl/doctor.py`, `fault.py`, `placement.py`, `run.py`). It needs
    `git`, `uv` and the network, and its clone is by construction absent from
    any `git archive <ref>` export.
  * `pip install site/vendor/cordis-4.0.0-py3-none-any.whl`, the same pinned
    runtime packaged by `site/build.py`. It was committed for the browser
    playground and was documented nowhere as a Python install path, which is
    what #967 measured: a downstream consumer exported revl by ref, followed
    the docs, and reached `ModuleNotFoundError: No module named 'cordis'` with
    the remedy sitting tracked in the tree.

The docs now name both. This file is what keeps them honest, because the
failure mode here is silent in both directions: a doc that names a wheel, a
pin or an environment variable that does not exist reads exactly like one that
does, and #967 found two of those already in
`backends/python/README.md` (it claimed the pin was `1316174` when
`setup.sh` pins `1c5e6f17`, and it offered `CORDIS_PY_REV` as the override
knob, a name no script in the repo reads).

Static and stdlib-only apart from one subprocess over an extracted copy of the
committed wheel, so it rides the `frontend` job's plain `pytest tests/ -q` and
needs no runtime, no network and no toolchain. It is a sibling of
`tests/test_cordis_wheel_declares_its_deps_946.py`, which pins what the wheel
DECLARES; this one pins that the documented way to get it still works.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "backends" / "python" / "setup.sh"
BACKEND_README = ROOT / "backends" / "python" / "README.md"
HUMAN_GUIDE = ROOT / "docs" / "guide-humans.md"
BUILD = ROOT / "site" / "build.py"

# Every document that tells a reader how to obtain the runtime.
INSTALL_DOCS = (BACKEND_README, HUMAN_GUIDE)

WHEEL_RE = re.compile(r"site/vendor/(cordis-[0-9][^/`\s]*\.whl)")
# `NAME="${NAME:-default}"` — the only shape setup.sh uses to read a knob.
SETUP_READS_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):-")


def _fenced_lines(text: str) -> list[str]:
    """The lines inside ``` fences: what a document hands over as commands."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return out


def _setup_text() -> str:
    return SETUP.read_text(encoding="utf-8")


def _setup_default(name: str) -> str:
    """The literal default `setup.sh` assigns to an environment knob."""
    m = re.search(rf'(?m)^{name}="\$\{{{name}:-([^}}]*)\}}"', _setup_text())
    assert m is not None, f"setup.sh no longer assigns {name} with a default"
    return m.group(1)


def test_the_readme_pin_is_the_pin_setup_sh_actually_checks_out():
    # The README states a commit as "the tested commit". setup.sh hard-checks
    # out CORDIS_PY_PIN. A reader who pins a build to the README's sha gets a
    # different runtime than the suite was validated against, and nothing in
    # the tree notices. The README named `1316174` while setup.sh pinned
    # `1c5e6f17`, which is `1316174` plus the dict-plugin Config fix.
    pin = _setup_default("CORDIS_PY_PIN")
    assert re.fullmatch(r"[0-9a-f]{40}", pin), pin

    text = BACKEND_README.read_text(encoding="utf-8")
    m = re.search(r"pinned to the tested commit `([0-9a-f]{7,40})`", text)
    assert m is not None, "the README no longer states the pin it documents"
    stated = m.group(1)
    assert pin.startswith(stated), (
        f"backends/python/README.md documents cordis-py pin {stated!r}, "
        f"but setup.sh checks out {pin!r}"
    )


def test_every_cordis_knob_the_readme_offers_is_one_setup_sh_reads():
    # A documented environment variable no script reads is indistinguishable
    # from one that works: export it, watch the build succeed, and get the
    # default anyway. The README offered `CORDIS_PY_REV=<sha>`; the name
    # setup.sh reads is `CORDIS_PY_PIN`.
    read = set(SETUP_READS_RE.findall(_setup_text()))
    assert {"CORDIS_PY", "CORDIS_PY_PIN"} <= read, read

    offered = set(re.findall(r"`(CORDIS_[A-Z0-9_]*)=", BACKEND_README.read_text("utf-8")))
    assert offered, "the README no longer documents any cordis knob"
    assert offered <= read, (
        f"backends/python/README.md offers {sorted(offered - read)}, which "
        f"backends/python/setup.sh does not read (it reads {sorted(read)})"
    )


def test_the_wheel_the_docs_name_is_the_wheel_site_build_writes():
    # site/build.py hardcodes the output filename. A version bump there moves
    # the artifact and leaves every documented `pip install` line pointing at a
    # path that no longer exists.
    built = re.search(r'"(cordis-[0-9][^"]*\.whl)"', BUILD.read_text("utf-8"))
    assert built is not None, "site/build.py no longer names its output wheel"
    name = built.group(1)
    assert (ROOT / "site" / "vendor" / name).is_file()

    for doc in INSTALL_DOCS:
        named = set(WHEEL_RE.findall(doc.read_text("utf-8")))
        assert named, f"{doc.relative_to(ROOT)} no longer names the offline install path"
        assert named == {name}, (
            f"{doc.relative_to(ROOT)} names {sorted(named)}; the committed wheel is {name}"
        )


def test_no_document_tells_a_reader_to_pip_install_cordis_from_pypi():
    # PyPI's `cordis` is an unrelated project. `pip install cordis` reports
    # success and leaves `import cordis` broken, which is the remedy a reader
    # reaches for first. No document may suggest it, and the two install docs
    # must say so out loud.
    # Only inside fenced code blocks: prose that NAMES the trap (as the two
    # install docs must) is the opposite of a doc that hands it over as a
    # command to run.
    for path in sorted(ROOT.rglob("*.md")):
        if ".git" in path.parts or "node_modules" in path.parts:
            continue
        for line in _fenced_lines(path.read_text("utf-8", errors="replace")):
            assert not re.search(r"pip install\s+(?:-\S+\s+)*cordis(?![-\w/.\[])", line), (
                f"{path.relative_to(ROOT)} hands a reader PyPI's unrelated "
                f"`cordis` as a command: {line.strip()!r}"
            )

    for doc in INSTALL_DOCS:
        text = doc.read_text("utf-8")
        assert "PyPI's `cordis`" in text, (
            f"{doc.relative_to(ROOT)} no longer warns that PyPI's cordis is a "
            "different project"
        )


def test_the_committed_wheel_carries_the_whole_runtime_package():
    # The install line is only a remedy if the artifact is the runtime and not
    # a stub. `loader`, `fiber` and `context` are the modules the emitted
    # Python tier actually drives.
    with zipfile.ZipFile(ROOT / "site" / "vendor" / "cordis-4.0.0-py3-none-any.whl") as whl:
        modules = {n for n in whl.namelist() if n.startswith("cordis/")}
    assert "cordis/__init__.py" in modules
    for name in ("loader", "fiber", "context", "registry", "service"):
        assert f"cordis/{name}.py" in modules, sorted(modules)


def test_the_wheel_imports_or_fails_only_on_a_dependency_it_declares(tmp_path):
    # The end of the documented path: unpack what `pip install` would place on
    # sys.path and import it. Where pyyaml and watchdog are present (the
    # `frontend-cordis` job, and any consumer who let pip resolve the declared
    # requirements) this must import outright. Where they are not (the
    # `frontend` job installs `.[test]`, which has no watchdog) the ONLY
    # admissible failure is one of those two declared requirements: an
    # undeclared third import creeping into the runtime is exactly the #946
    # defect returning, and it would make the documented one-line install a
    # lie without failing anything else in the repo.
    with zipfile.ZipFile(ROOT / "site" / "vendor" / "cordis-4.0.0-py3-none-any.whl") as whl:
        whl.extractall(tmp_path)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", "import cordis; print(cordis.__file__)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode == 0:
        assert str(tmp_path) in proc.stdout, proc.stdout
        return

    err = proc.stderr
    declared = (
        "No module named 'yaml'" in err           # pyyaml, declared hard
        or "No module named 'watchdog'" in err    # watchdog, declared hard
        or "NoneType takes no arguments" in err   # hmr.py's watchdog fallback
    )
    assert declared, (
        "importing the committed cordis wheel failed on something the wheel "
        f"does not declare:\n{err}"
    )
