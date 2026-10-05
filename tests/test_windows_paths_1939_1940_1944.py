"""Three spellings that only work on POSIX, found by a windows-latest job.

* #1944: `parse_file` recorded provenance with `os.path.relpath(path)`, which
  raises ValueError on Windows when the file is on another drive than the
  working directory. Every user-path `relpath` now goes through
  `revl._paths.relpath_or_abs`, which keeps the absolute path then.
* #1940: `revl test --backend ts` executed `node_modules/.bin/vitest`, npm's
  POSIX shell shim, which Windows refuses (WinError 193). It now runs vitest's
  entry script through node, the same argv on every platform.
* #1939: `backends/python/setup.sh` and the runtime's venv lookups hardcoded
  `.venv/bin/python`; a Windows venv keeps it at `.venv/Scripts/python.exe`.

The Windows behaviour is reproduced here on any platform: ntpath's `relpath`
raises across drives on every OS, and a venv layout is just files.
"""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from revl import _paths  # noqa: E402
from revl import test as revl_test  # noqa: E402
from revl.parser import parse_file  # noqa: E402

SOURCE = "fn f() -> Int { return 1 }\n"


def _cross_drive_relpath(path, start=None):
    """os.path.relpath as Windows computes it with the path on C: and the
    working directory on D:."""
    return ntpath.relpath("C:\\Users\\me\\a.rvl", start or "D:\\work")


# ------------------------------------------------------------ #1944 relpath

def test_ntpath_relpath_really_raises_across_drives():
    """Anti-vacuity: the failure being guarded is real on every platform."""
    with pytest.raises(ValueError):
        _cross_drive_relpath("x")


def test_relpath_or_abs_keeps_a_relative_path_when_there_is_one(tmp_path):
    target = tmp_path / "sub" / "a.rvl"
    assert _paths.relpath_or_abs(str(target), str(tmp_path)) == os.path.join("sub", "a.rvl")


def test_relpath_or_abs_falls_back_to_the_absolute_path(tmp_path, monkeypatch):
    target = tmp_path / "a.rvl"
    monkeypatch.setattr(os.path, "relpath", _cross_drive_relpath)
    assert _paths.relpath_or_abs(str(target)) == os.path.abspath(str(target))
    assert _paths.relpath_or_abs(str(target), "D:\\work") == os.path.abspath(str(target))


def test_parse_file_on_another_drive_records_the_absolute_path(tmp_path, monkeypatch):
    target = tmp_path / "a.rvl"
    target.write_text(SOURCE, encoding="utf-8")
    monkeypatch.setattr(os.path, "relpath", _cross_drive_relpath)
    program = parse_file(str(target))       # raised ValueError before #1944
    assert program is not None


def test_no_user_path_relpath_is_left_unguarded():
    """Every `os.path.relpath` in src/revl either goes through relpath_or_abs
    or is one of the listed calls whose path is already inside its root (so
    on the same drive). A new bare call fails here by name."""
    inside_root = {
        # hostref resolves `real`/`key` and refuses it unless it is inside
        # `root` before this line runs
        ("hostref.py", "rel = os.path.relpath(real, root)"),
        ("hostref.py", "rel = os.path.relpath(key, root)"),
        ("hostref.py", "_record_asset(node, os.path.relpath(real, root),"),
        ("hostref.py", "_record_asset(node, os.path.relpath(key, root),"),
        # os.walk under root
        ("promotion_barrier.py",
         'rel = os.path.relpath(full, str(root)).replace(os.sep, "/")'),
        # the helper itself
        ("_paths.py",
         "return os.path.relpath(path, start) if start is not None else os.path.relpath(path)"),
    }
    found = set()
    for path in (ROOT / "src" / "revl").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if "os.path.relpath(" in code and "`os.path.relpath(" not in code:
                found.add((path.name, line.strip()))
    assert found - inside_root == set(), sorted(found - inside_root)


# ------------------------------------------------------------ #1940 vitest

def test_vitest_runs_through_node_not_the_bin_shim(tmp_path, monkeypatch):
    entry = tmp_path / "vitest.mjs"
    entry.write_text("", encoding="utf-8")
    monkeypatch.setattr(revl_test, "VITEST_ENTRY", entry)
    monkeypatch.setattr(revl_test.shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    assert revl_test.vitest_command() == ["/usr/bin/node", str(entry)]


def test_vitest_entry_is_the_packages_bin():
    """The entry run is the one vitest's package.json names as its `bin`, read
    from the lock file so a vitest upgrade that moves it fails here."""
    import json  # noqa: PLC0415

    lock = json.loads((ROOT / "backends" / "typescript" / "package-lock.json")
                      .read_text(encoding="utf-8"))
    rel = lock["packages"]["node_modules/vitest"]["bin"]["vitest"]
    assert revl_test.VITEST_ENTRY == (ROOT / "backends" / "typescript"
                                      / "node_modules" / "vitest" / rel)
    assert ".bin" not in revl_test.VITEST_ENTRY.parts


def test_no_vitest_or_no_node_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(revl_test, "VITEST_ENTRY", tmp_path / "absent.mjs")
    assert revl_test.vitest_command() is None
    entry = tmp_path / "vitest.mjs"
    entry.write_text("", encoding="utf-8")
    monkeypatch.setattr(revl_test, "VITEST_ENTRY", entry)
    monkeypatch.setattr(revl_test.shutil, "which", lambda name: None)
    assert revl_test.vitest_command() is None


# ------------------------------------------------------------ #1939 venv

@pytest.mark.parametrize("layout, expected", [
    ("posix", Path("bin") / "python"),
    ("windows", Path("Scripts") / "python.exe"),
    ("absent", Path("bin") / "python"),
])
def test_venv_python_reads_the_layout(tmp_path, layout, expected):
    venv = tmp_path / ".venv"
    if layout == "posix":
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
    elif layout == "windows":
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    assert _paths.venv_python(venv) == venv / expected


def test_no_runtime_lookup_hardcodes_the_posix_venv():
    """The runtime finds a venv's interpreter through `venv_python`; a literal
    `".venv" / "bin" / "python"` path built in src/revl is the #1939 shape."""
    hits = []
    for path in (ROOT / "src" / "revl").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'"\.venv"\s*/\s*"bin"\s*/\s*"python"', text):
            hits.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not hits, hits


def _setup_choice() -> str:
    """The interpreter-choosing block of setup.sh, verbatim."""
    text = (ROOT / "backends" / "python" / "setup.sh").read_text(encoding="utf-8")
    m = re.search(r"^if \[ -f \.venv/Scripts/python\.exe \]; then\n.*?^fi\n", text,
                  re.S | re.M)
    assert m, "setup.sh no longer chooses the venv interpreter in one block"
    return m.group(0)


@pytest.mark.parametrize("windows", [False, True])
def test_setup_sh_picks_the_venv_layout_it_finds(tmp_path, windows):
    if windows:
        (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
        (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    script = _setup_choice() + 'printf "%s|%s" "$VBIN" "$VPY"\n'
    out = subprocess.run(["sh", "-c", script], cwd=tmp_path, capture_output=True,
                         text=True, check=True).stdout
    assert out == (".venv/Scripts|.venv/Scripts/python.exe" if windows
                   else ".venv/bin|.venv/bin/python")


def test_setup_sh_uses_the_choice_everywhere():
    text = (ROOT / "backends" / "python" / "setup.sh").read_text(encoding="utf-8")
    rest = text.replace(_setup_choice(), "")
    assert ".venv/bin" not in rest, [line for line in rest.splitlines()
                                     if ".venv/bin" in line]
