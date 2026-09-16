"""The playground's compiler wheel is built by the deploy, and that is checked.

HISTORY, because this file's claims inverted (issues #252, #547).

The wheel that boots the compiler under Pyodide was COMMITTED at
`playground/vendor/` and `site/vendor/`, and `.github/workflows/site-wheel.yml`
gated the committed copy against a fresh build on every push to main. The gate
was honest and it worked; the artifact could not keep up with it. A wheel
vendoring the whole of `src/revl` is stale the moment any module changes, so its
refresh was written against one sha and landed several merges later: `site wheel
drift` was refreshed four times in one day (#1095, #1103, #1115, #1133) and red
again within hours of each.

The committed copy is therefore gone. `.github/workflows/pages.yml` builds the
wheel into the artifact it publishes, so the deployed playground is built from
the sha being deployed and drift is impossible rather than merely detected.
`tools/check_site_wheel.py`'s header records why the alternatives lose (a per-PR
regeneration of a 2.5 MB non-deterministic zip conflicts by construction under a
merge queue; a bot commit needs a standing credential and keeps growing a
history that already spends 49.3 MiB of 66.0 MiB of packed blob data on 191
revisions of this one file).

What this file stops from quietly regressing, in both directions, none of which
can be claimed from inside the workflows themselves:

1. The wheel stays OUT of the commit, and stays ignored. Re-committing it
   restores the whole recurrence, and it would do so silently: everything keeps
   working, the file just starts rotting again.
2. The deploy still builds it. A `pages.yml` that uploads `site/` without the
   build step publishes a playground that 404s on its own compiler.
3. `site-wheel.yml` still runs the CHECK form of the tool and still fails. A job
   rewritten to `--write` would build a wheel on a throwaway runner and report
   success forever.
4. It stays OFF the per-PR path. The check is cheap now, but a `pull_request`
   trigger adds a check name to every PR and branch protection matches on names.

Static, like `tests/test_ts_typecheck_gate_runs_in_ci.py` and
`tests/test_java_javac_gate_runs_in_ci.py`: it reads the workflows as text, so
it needs no toolchain and no PyYAML (not a declared dependency) and rides the
`frontend` job's plain `pytest tests/ -q`. It does NOT build a wheel, so it
costs a PR nothing and cannot itself become a per-PR wheel gate.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DRIFT = WORKFLOWS / "site-wheel.yml"
PAGES = WORKFLOWS / "pages.yml"
CI = WORKFLOWS / "ci.yml"
TOOL = "tools/check_site_wheel.py"
VENDORS = ("playground", "site")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_steps(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"run:\s*(.+)", text)]


def _uncommented(path: Path) -> list[str]:
    """The workflow's lines with comment-only lines dropped.

    Comments are stripped because every claim here is about what a workflow
    DOES, and both workflows' headers explain at length the arrangements they
    replaced — a substring search over the raw text would read those
    explanations as the things they warn against.
    """
    return [ln for ln in _text(path).splitlines() if not ln.lstrip().startswith("#")]


def _triggers(path: Path) -> str:
    head = "\n".join(_uncommented(path)).split("jobs:", 1)[0]
    return head


# --- anti-vacuity ---------------------------------------------------------- #
def test_the_checker_still_exists_and_still_builds():
    """Everything below is a claim about workflows that run this tool. If the
    tool is gone, re-derive this file against whatever replaced it rather than
    passing vacuously."""
    tool = ROOT / TOOL
    assert tool.is_file(), f"{TOOL} is gone"
    src = _text(tool)
    assert "--write" in src, (
        f"{TOOL} no longer offers `--write`, which is how the deploy (and a "
        "developer serving site/ locally) materializes the wheel."
    )
    assert "pages.yml" in src, (
        f"{TOOL} no longer reads pages.yml, so nothing checks that the deploy "
        "still builds the wheel — the one failure mode building-at-deploy "
        "introduces."
    )


# --- 1: the wheel is not committed ----------------------------------------- #
def test_the_revl_wheel_is_not_committed():
    """The recurrence in one assertion.

    A committed wheel vendoring all of `src/revl` can only be correct for the
    sha it was built on, and every mechanism for refreshing it either races the
    merge queue (a follow-up commit), conflicts with it (a per-PR rebuild of a
    non-deterministic zip), or needs a standing credential (a bot push). It is
    built by the deploy instead.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", "playground/vendor", "site/vendor"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if tracked.returncode != 0:
        return  # not a checkout; the ignore assertion below is the backstop
    committed = [p for p in tracked.stdout.split() if re.search(r"/revl-.*\.whl$", p)]
    assert not committed, (
        f"the revl playground wheel is committed again at {', '.join(committed)}. "
        "It vendors the whole of src/revl, so a committed copy goes stale on "
        "every source change and its refresh always lands after the merges that "
        "staled it — `site wheel drift` was refreshed four times in one day on "
        "that arrangement. .github/workflows/pages.yml builds it into the "
        "published artifact instead; see tools/check_site_wheel.py's header."
    )


def test_the_wheel_is_gitignored_in_both_vendor_dirs():
    """`--write` puts a wheel in both vendor dirs on any developer machine that
    serves the site. Without an ignore rule the next `git add -A` re-commits it
    and the recurrence is back."""
    patterns = _text(ROOT / ".gitignore")
    for vendor in VENDORS:
        assert f"{vendor}/vendor/revl-*.whl" in patterns, (
            f".gitignore no longer ignores {vendor}/vendor/revl-*.whl, so the "
            "wheel `tools/check_site_wheel.py --write` writes there can be "
            "swept back into the commit."
        )


def test_the_cordis_wheel_is_still_committed():
    """The scope boundary, stated as a test so it is not lost to a sweep.

    `site/vendor/cordis-*.whl` is built from the pinned
    `backends/python/setup.sh` clone, which is not in this tree and is not
    available to the Pages runner, so it CANNOT be built at deploy time. It
    stays committed, and `tests/test_cordis_wheel_records_its_source_revision_1029.py`
    is what holds it to its pin.
    """
    found = sorted((ROOT / "site" / "vendor").glob("cordis-*.whl"))
    assert found, (
        "site/vendor/ has no committed cordis wheel. The playground's live "
        "mode boots on it, and it is not derivable from this tree."
    )


# --- 2: the deploy builds it ----------------------------------------------- #
def test_the_pages_deploy_builds_the_wheel_before_it_uploads():
    """The tool asserts this too, from the same file; this is the half that
    runs on every PR, for free, in the frontend job."""
    assert PAGES.is_file(), ".github/workflows/pages.yml is gone."
    lines = _uncommented(PAGES)
    upload_at = next((i for i, ln in enumerate(lines) if "upload-pages-artifact" in ln), None)
    assert upload_at is not None, (
        "pages.yml no longer uploads a Pages artifact; re-derive this file "
        "against whatever publishes site/."
    )
    build_at = next(
        (i for i, ln in enumerate(lines)
         if f"{TOOL} --write" in ln or "site/build.py" in ln),
        None,
    )
    assert build_at is not None, (
        "pages.yml uploads site/ without building the compiler wheel. The "
        "wheel is not committed, so the published playground would 404 on it."
    )
    assert build_at < upload_at, (
        "pages.yml builds the wheel after the upload step, so the upload "
        "carries the checkout's state rather than the build's."
    )


def test_the_pages_deploy_is_not_filtered_to_site_only():
    """The deployed artifact is now a function of src/revl and the builder as
    well as of site/. A `paths: [site/**]` filter would mean a compiler change
    never reaches the published playground at all — which is exactly what made
    the committed wheel the only thing that ever refreshed it."""
    head = _triggers(PAGES)
    assert "paths:" not in head, (
        "pages.yml filters its push trigger by path again. The wheel is built "
        "at deploy time from src/revl, so a filter that omits a wheel input "
        "silently stops publishing compiler changes."
    )


# --- 3: the check still exists and still fails ----------------------------- #
def test_a_workflow_owns_the_check():
    assert DRIFT.is_file(), (
        ".github/workflows/site-wheel.yml is gone. Nothing then checks that "
        "the deploy still builds the wheel the playground fetches, which is "
        "the one failure mode building-at-deploy introduces."
    )


def test_the_job_runs_the_checker_in_check_mode():
    steps = _run_steps(_text(DRIFT))
    checks = [s for s in steps if TOOL in s]
    assert checks, f"site-wheel.yml no longer runs {TOOL}; it checks nothing."
    for step in checks:
        assert "--write" not in step, (
            "site-wheel.yml runs the checker with `--write`. That builds a "
            "wheel on a throwaway runner and throws it away, so the job passes "
            "forever. The job must run the CHECK form and fail."
        )


def test_the_job_runs_after_a_merge_to_main():
    """Post-merge on main is where the tree that ships to Pages lives, and it
    is an event no agent has to remember."""
    head = _triggers(DRIFT)
    assert re.search(r"^\s*push:\s*$", head, re.M), (
        "site-wheel.yml no longer triggers on push. A schedule alone means a "
        "broken deploy contract can sit on main for up to a week, and a "
        "`workflow_dispatch` alone is the 'someone remembers to run it' "
        "ownership issue #252 is about."
    )
    assert re.search(r"branches:\s*\[\s*main\s*\]", head), (
        "site-wheel.yml's push trigger no longer names main."
    )


def test_the_job_does_not_swallow_its_own_failure():
    text = _text(DRIFT)
    assert "continue-on-error" not in text, (
        "site-wheel.yml sets continue-on-error; a break would then be a green "
        "run with a warning."
    )
    for step in _run_steps(text):
        if TOOL in step:
            assert "|| true" not in step and "|| :" not in step, (
                f"the {TOOL} step swallows its exit status."
            )


# --- 4: it stays off the per-PR critical path ------------------------------ #
def test_the_workflow_never_runs_on_a_pull_request():
    """The check is seconds long now, but a `pull_request` trigger adds a check
    name to every PR and branch protection matches on names. Post-merge on main
    is where this belongs."""
    head = _triggers(DRIFT)
    assert "pull_request" not in head, (
        "site-wheel.yml now triggers on pull_request, adding a check name to "
        "every PR. Post-merge detection on main is the deliberate placement."
    )


def test_ci_yml_does_not_gate_pull_requests_on_the_wheel():
    """Same rule for the matrix workflow, which does run on pull_request."""
    for step in _run_steps(_text(CI)):
        assert TOOL not in step, (
            f"ci.yml runs {TOOL}. ci.yml runs on pull_request, so this puts the "
            "wheel back on the per-PR critical path (issue #252 / roadmap 110c)."
        )


def test_the_affected_selector_still_maps_src_revl_to_the_wheel_gate():
    """The local half: `make pre-merge-affected` still runs the wheel gate on a
    src/revl change, so an agent sees a broken build or a renamed artifact
    before it lands. Cheap, and it is the only local run of the tool."""
    src = _text(ROOT / "tools" / "affected_tests.py")
    assert '"site-wheel"' in src, (
        "tools/affected_tests.py no longer selects a `site-wheel` gate, so "
        "`make pre-merge-affected` no longer builds the wheel on a src/revl "
        "change and the post-merge job is the only check left."
    )
