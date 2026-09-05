"""Issue #336: the documented happy path is the `revl` console script, not
`python -m revl` (which has a CWD-shadowing window issue #317 names that
`drop_cwd_entry` closes for everything but the package's own import-time
imports, and that the `revl` script entry point is window-free by design).

This test pins the three exit conditions the issue names, so a regression
to the unsafe shape fails loudly in CI rather than silently re-routing
every user, every agent, and every doc reader through the documented
window.

1. `revl --help` works after the documented setup. (The setup script is
   responsible for the install; the test asserts the shape on this
   checkout, not that the setup ran.)
2. No doc, script or diagnostic in this tree recommends a bare
   `python -m revl` without `-P`. A bare `-m` is the unsafe shape; the
   `-P` is the PYTHONSAFEPATH safety bit. Every `python -m revl` in
   the tree must either (a) carry `-P` on the same command, or (b) be
   inside a comment that names `-P` and explains why.
3. The diagnostic the loader prints when cordis-py is missing now leads
   with `revl` (the documented happy path) before the absolute-interpreter
   fallback. (The detailed check lives in
   `tests/test_run.py::test_run_without_the_runtime_skips_like_the_other_backends`
   and `tests/test_run.py::test_py_tier_preflights_the_missing_cordis_runtime`;
   this file asserts the rule holds across the whole tree, not just the
   two diagnostic surfaces.)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# The patterns a regression would land on. Bare `python -m revl` (no `-P`,
# no comment that explains `-P`) is the unsafe shape; a future grep for
# `python -m revl` across `docs/`, `README.md` and `*.sh` is the issue's
# third exit condition, and the assertion below is the CI half of it.
# The `(?! ... )` negative lookaheads handle the two safe shapes:
#   `python -P -m revl` (the safe `-m` form), and a line that names `-P`
#   somewhere in the same line (the comment that explains why the
#   unsafe form is being shown). The lookbehind for `-P` accepts both
#   `python -P` (the safe flag) and `\`-P\`` (the inline-code mention
#   in an explanatory comment).
_BARE_DASH_M = re.compile(
    r"\bpython\s+(-m)\s+revl"     # the unsafe shape: `python -m revl` (no -P)
    r"(?!\s*-P)"                 # the safe shape is `python -P -m revl`
    # or an explanatory line that names `-P` somewhere on the same line
    # (with or without backtick delimiters, e.g. `(no `-P`)`).
    r"(?![^\n]*-P)"
)
# Files the issue's third exit condition names, plus the diagnostic and
# site surfaces a regression would most plausibly land on. The list is
# deliberately not "every file in the tree" — `docs/design/*.md` is the
# design rationale, not user-facing documentation, and a source test in
# `src/revl/` that imports `revl` for a unit test, a test docstring
# documenting issue #317, or an in-tree design note describing the
# differential are not "recommendations". The issue's third exit
# condition is "A grep for `python -m revl` across `docs/`, `README.md`
# and `*.sh` returns only intentional occurrences" — this list IS that
# set, plus the diagnostic/setup/site surfaces a regression would most
# plausibly land on.
_GREP_ROOTS = [
    "README.md",
    "docs/apply.md",
    "docs/commands-reference.md",
    "docs/guide-ai-agents.md",
    "docs/guide-humans.md",
    "docs/opentelemetry.md",
    "docs/plan.md",
    "docs/process.md",
    "docs/revl-metrics.md",
    "docs/schedule-testing.md",
    "backends/python/setup.sh",
    "backends/python/README.md",
    "site/index.html",
    "site/js/landing.js",
    "src/revl/__main__.py",
    "src/revl/_safepath.py",
    "src/revl/run.py",
    "src/revl/test.py",
    "src/revl/placement.py",
    "src/revl/mcp/session.py",
    "src/revl/doctor.py",
    "src/revl/otel.py",
    "src/revl/lsp/__init__.py",
    "src/revl/lsp/__main__.py",
    "src/revl/truc/__init__.py",
    "src/revl/truc/__main__.py",
    "src/revl/truc/_launcher.py",
    "src/revl/truc/_relock.py",
    "src/revl/truc/truc.lock",
    "SECURITY.md",
    "demo/live_systems/run_demo.py",
    "demo/bridge_pyadt.py",
]


def _all_text_files() -> list[Path]:
    out: list[Path] = []
    for entry in _GREP_ROOTS:
        path = ROOT / entry
        if path.is_file():
            out.append(path)
        # an explicit per-file entry (not a directory) and present: keep
        # going. An explicit per-file entry that is also a directory is
        # rare in this list, so we don't rglob it.
    return sorted(set(out))


def test_no_documented_bare_python_m_revl_anywhere_in_the_tree():
    """A grep for `python -m revl` across `docs/`, `README.md` and the
    diagnostic/setup/site surfaces returns only intentional occurrences,
    each with `-P` or an adjacent explanation.

    A bare `python -m revl` (no `-P`, no comment that names `-P`) is the
    unsafe shape issue #317 names. A regression that re-introduces it on
    a documented surface re-routes the user/agent through the
    CWD-shadowing window the issue is closing.
    """
    bad: list[str] = []
    for path in _all_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _BARE_DASH_M.search(line):
                bad.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not bad, (
        "bare `python -m revl` (no `-P`) on a documented surface —\n"
        + "\n".join(bad)
        + "\n\n"
        "Either use `revl <cmd>` (the documented happy path) or\n"
        "`python -P -m revl <cmd>` (the absolute-interpreter fallback,\n"
        "with the `-P` as the PYTHONSAFEPATH safety bit). See issue #336."
    )


def test_safe_python_dash_P_dash_m_revl_appears_in_every_unsafe_surface():
    """The surfaces that mention the absolute-interpreter fallback at all
    must show it in the safe form (`-P`). A test on the absence of `-P`
    is necessary but not sufficient: a regression could remove the `-m`
    form entirely, which would also pass the bare-form test while still
    failing the issue's spirit. This test pins the positive form on the
    surfaces that named the unsafe shape historically.
    """
    # The surfaces that used to spell `python -m revl …` for the cordis
    # preflight / doc-led examples. The migration added `-P` everywhere;
    # an edit that re-introduced the bare form would land here.
    required = {
        "docs/opentelemetry.md": r"python -P -m revl\.otel run\.jsonl",
        "src/revl/otel.py":       r"python -P -m revl\.otel run\.jsonl",
        "docs/commands-reference.md": r"python -P -m revl\.otel run\.jsonl",
        "src/revl/truc/_relock.py":    r"python -P -m revl\.truc\._relock",
        "src/revl/truc/truc.lock":     r"python -P -m revl\.truc\._relock",
    }
    missing: list[str] = []
    for rel, pattern in required.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        if not re.search(pattern, text):
            missing.append(f"{rel}: no match for {pattern!r}")
    assert not missing, (
        "the documented absolute-interpreter form must appear with `-P`\n"
        "on the surfaces that name it. Missing:\n  "
        + "\n  ".join(missing)
    )


def test_rev_documented_happy_path_appears_in_the_quickstart():
    """The README quickstart spells `revl <cmd>` (the documented happy
    path), not `python -m revl <cmd>`. The issue's first exit condition
    is `revl --version works immediately after running the documented
    setup`; the README's quickstart IS the documented setup, and the
    quickstart must lead at `revl <cmd>` (a console script) so
    `revl --version` and every subsequent command work without a bare
    `-m`.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    # The Quickstart is everything between the `## Quickstart` heading and
    # the next `##` heading. Within that, the documented setup is the
    # concatenation of every fenced code block — usually one install line
    # and one use block. The user-facing-command assertion runs against
    # that whole concatenation, not just the first block.
    section = text.split("## Quickstart", 1)[1].split("\n## ", 1)[0]
    # strip the fenced code-block markers, keep the contents
    lines: list[str] = []
    in_fence = False
    for line in section.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(line)
    body = "\n".join(lines)
    assert "revl compile" in body, (
        "the Quickstart must show `revl compile …` (the documented\n"
        "happy path) somewhere in its code blocks — the README's\n"
        "quickstart IS the documented setup the issue's first exit\n"
        "condition measures.\n"
        f"Quickstart body:\n{body}"
    )
    assert "python -m revl compile" not in body, (
        "the Quickstart must not lead with `python -m revl compile` —\n"
        "that is the unsafe shape issue #336 closes; lead with `revl`."
    )


def test_the_diagnostic_lead_with_the_happy_path():
    """The preflight diagnostic in `src/revl/test.py` must lead with
    `revl` (the documented happy path) before the absolute-interpreter
    fallback. The detailed per-diagnostic checks live in
    `tests/test_run.py`; this one keeps the rule on a single surface, in
    a single place, so a future refactor cannot quietly reorder the two
    lines.
    """
    from revl.test import _PY_RUNTIME_REMEDY  # noqa: PLC0415
    msg = _PY_RUNTIME_REMEDY
    rerun_idx = msg.index("then rerun")
    happy_idx = msg.index("revl test", rerun_idx)
    fallback_idx = msg.index("backends/python/.venv/bin/python -P -m revl test",
                             rerun_idx)
    assert happy_idx < fallback_idx, (
        f"`revl test` (the documented happy path) must appear before the "
        f"absolute-interpreter fallback in the diagnostic; got {_PY_RUNTIME_REMEDY!r}"
    )


def test_rev_console_script_is_on_path_in_a_setup_installed_venv(tmp_path):
    """The issue's first exit condition: `revl --version` (and the
    analogous `revl <anything>` that prints a usage banner) works
    immediately after running the documented setup.

    This test does NOT run the full `backends/python/setup.sh` (it clones
    a pinned cordis-py and would not finish in a CI job). It pins the
    SHAPE on the working checkout: the `revl` package is installed
    editable, and `python -m pip install --no-deps -e .` — the half of
    the setup script that materializes the entry points — produces a
    working `revl` console script in the venv's `bin/`.
    """
    venv = tmp_path / "venv"
    subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    subprocess.check_call(
        [str(venv / "bin" / "python"), "-m", "ensurepip", "--upgrade"],
        stdout=subprocess.DEVNULL,
    )
    # install revl editable from this checkout, the way the setup script
    # does. `--no-deps` keeps the test fast and toolchain-agnostic.
    subprocess.check_call(
        [str(venv / "bin" / "python"), "-m", "pip", "install",
         "--no-deps", "-e", str(ROOT)],
        stdout=subprocess.DEVNULL,
    )
    # the entry point must be a file on the venv's bin/, runnable, and
    # answer argparse's default help message (the issue's "`revl
    # --version` works" is the operator's reading of "the revl command
    # works").
    revl_bin = venv / "bin" / "revl"
    assert revl_bin.is_file(), (
        f"`pip install -e .` did not produce `{revl_bin}` — the entry\n"
        "point materialization that the `setup.sh` step provides broke.\n"
        "Run `python -m pip install --no-deps -e .` (the half of the\n"
        "setup script that writes `[project.scripts]`) and re-run."
    )
    result = subprocess.run(
        [str(revl_bin)], capture_output=True, text=True, timeout=30,
    )
    # `revl` with no args prints argparse's usage and exits 2 — the same
    # shape `revl --help` and `revl --version` return. The exit code is
    # the load-bearing fact: an ImportError or a missing entry point
    # surfaces as a nonzero exit (and a traceback on stderr).
    assert result.returncode == 2, (
        f"`{revl_bin}` did not run; got returncode={result.returncode}, "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "usage:" in result.stdout or "usage:" in result.stderr, (
        f"`{revl_bin}` did not print argparse's usage banner; got\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

