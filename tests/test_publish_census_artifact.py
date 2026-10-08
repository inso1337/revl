"""The census edition reaches a reader, and the deploy is what puts it there.

Issue #1268 asks for the gate/reference census as an artifact checkable from
outside this repository. `tools/census_artifact.py` renders the report and its
`--verify` judges a copy of one, but neither puts a copy anywhere a reader who
has not cloned this repository can reach; `tools/publish_census_artifact.py`
does that last step, and `.github/workflows/pages.yml` runs it.

The edition is built at deploy time and NOT committed, for the reason
`tools/check_site_wheel.py`'s header weighs at length for the playground wheel:
a rendered artifact can only be correct for the sha it was rendered on, and the
inputs here are the corpus, the gate and the reference. What is left to get
wrong is the same single failure mode, so it is checked the same way — statically,
against the workflow text:

1. The deploy still BUILDS the edition. A `pages.yml` that uploads `site/`
   without the build step publishes a 404 where the artifact should be, and the
   `MANIFEST.json` URL would still be advertised by every document that points
   at it.
2. It is built BEFORE the upload. A build after the upload publishes the
   checkout, which has no edition in it.
3. The gate on the way in is `--verify --strict`, not `--check`. `--check` only
   answers whether the committed records drifted; it cannot see a missing
   per-checker-version crate reproduction, so a deploy gated on `--check` would
   publish `reproduced` on the strength of a check that never ran.
4. The edition stays OUT of the commit. Committing it restores the recurrence
   silently: everything keeps working and the copy just starts rotting.

Then the artifact's own promises, rendered rather than asserted about: the Exit
section's four items are properties of the text a reader gets, so they are read
back out of it here. Rendering is `--from-records` and takes about a second; it
runs no census and needs no cargo.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
TOOL = "tools/publish_census_artifact.py"
PAGES = ROOT / ".github" / "workflows" / "pages.yml"
GITIGNORE = ROOT / ".gitignore"
EDITION = ROOT / "site" / "census-artifact"
URL = "https://inso1337.github.io/revl/census-artifact/"


def _publisher():
    """The tool, imported once. It is a script, so there is no package to use."""
    return load_by_path("test_publish_census_artifact_under_test", ROOT / TOOL)


@pytest.fixture(scope="module")
def publisher():
    return _publisher()


@pytest.fixture(scope="module")
def rendered(publisher):
    """The edition this tree renders, as `(report, {filename: text})`."""
    artifact, report = publisher.render()
    return report, publisher.edition(artifact, report)


def _uncommented(path: Path) -> str:
    """The workflow with comment-only lines dropped.

    Every claim below is about what the workflow DOES, and `pages.yml`'s header
    explains at length the arrangements it replaced. A substring search over the
    raw text would read those explanations as the things they warn against —
    this file's own name appears in that header, for one.
    """
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))


def _run_steps(text: str) -> list[str]:
    """Each `run:` body, in file order."""
    return [m for m in re.findall(r"run:\s*\|((?:\n +.*)+)", text)]


# --- anti-vacuity ---------------------------------------------------------- #
def test_the_tool_still_renders_and_still_checks():
    """Everything below is a claim about a tool the deploy runs. If the tool is
    gone, re-derive this file rather than passing vacuously."""
    tool = ROOT / TOOL
    assert tool.is_file(), f"{TOOL} is gone"
    src = tool.read_text(encoding="utf-8")
    for flag in ("--write", "--check", "--json"):
        assert flag in src, f"{TOOL} no longer offers {flag}"
    assert "check_eval_report" in src, (
        f"{TOOL} no longer runs the report through tools/check_eval_report.py, "
        "so an over-claiming report could be published rather than withheld."
    )


# --- 1 & 2: the deploy builds it, before the upload ------------------------ #
def test_the_deploy_builds_the_census_edition_before_it_uploads():
    text = _uncommented(PAGES)
    builder = f"{TOOL} --write"
    assert builder in text, (
        f"{PAGES.name} no longer runs `{builder}`, so the deploy publishes a "
        f"site whose {URL} is a 404 — while every document that points at that "
        "URL still points at it."
    )
    upload = "actions/upload-pages-artifact@"
    assert upload in text, f"{PAGES.name} no longer uploads the artifact"
    assert text.index(builder) < text.index(upload), (
        f"`{builder}` runs AFTER the upload in {PAGES.name}: the upload would "
        "publish the checkout, which has no edition in it."
    )


def test_the_deploy_step_installs_what_the_render_imports():
    """The render imports the reference classifier from
    `tests/test_selfhost_lower.py` instead of copying it (so it cannot drift
    into disagreeing with the oracle), and that module imports pytest. The wheel
    step is stdlib-only; this one is not, and the install is why."""
    text = _uncommented(PAGES)
    step = next((s for s in _run_steps(text) if f"{TOOL} --write" in s), "")
    assert "pip install" in step and "pytest" in step, (
        "the census step runs the render without installing pytest, which the "
        f"render imports transitively; the step is:\n{step}"
    )


# --- 3: the gate on the way in is the strict verdict ----------------------- #
def test_the_deploy_gates_on_verify_strict_and_not_on_check():
    """`--check` answers "have the committed records drifted" and cannot see a
    missing per-checker-version crate reproduction. `--verify --strict` is the
    verdict, and it is what makes the published `reproduced` a claim about the
    sha being deployed rather than about the records alone."""
    text = _uncommented(PAGES)
    step = next((s for s in _run_steps(text) if f"{TOOL} --write" in s), "")
    assert "census_artifact.py --verify --strict" in step, (
        "the deploy no longer holds the edition to `--verify --strict`, so it "
        "can publish numbers that reproduce as records without the crate "
        f"reproducing them; the step is:\n{step}"
    )
    assert "census_artifact.py --check" not in step, (
        "the deploy gates on `--check`, the cheap drift check, which cannot see "
        "a missing per-checker-version crate reproduction."
    )


# --- 4: the edition is not committed --------------------------------------- #
def test_the_rendered_edition_is_not_committed():
    ignored = GITIGNORE.read_text(encoding="utf-8")
    assert re.search(r"^site/census-artifact/?$", ignored, re.M), (
        ".gitignore no longer ignores site/census-artifact/, so the edition can "
        "be committed — which is how the playground wheel rotted (#1095, #1103, "
        "#1115, #1133)."
    )
    tracked = subprocess.run(["git", "ls-files", "--", "site/census-artifact"],
                             cwd=ROOT, capture_output=True, text=True)
    if tracked.returncode == 0:
        assert not tracked.stdout.strip(), (
            "the census edition is committed again at "
            f"{tracked.stdout.split()}; it is a function of the sha being "
            "deployed and belongs in the deploy artifact only."
        )


# --- the Exit section, read back out of the text a reader gets ------------- #
def test_the_published_table_states_n_the_checker_version_and_the_allowance(
        rendered):
    """Exit item 2: `n`, the checker version, and the standing allowance with
    each residual named. The residual names are the part that cannot be a bare
    count, so the row is checked against the report rather than for a shape."""
    report, files = rendered
    census = report["census"]
    alw = census["false_admit_allowance"]
    readme = files["README.md"]
    row = next((ln for ln in readme.splitlines()
                if "standing `false-admit` allowance" in ln), "")
    assert row, "the published table no longer carries the standing allowance"
    assert str(census["n"]) in row or str(census["n"]) in readme
    assert census["checker_version"] in readme, (
        "the published table no longer states the checker version, so a reader "
        "cannot tell which checker's numbers these are"
    )
    assert str(alw["total"]) in row, (
        f"the allowance row does not state the standing allowance "
        f"({alw['total']}): {row}"
    )
    for family, count in alw["families"].items():
        assert family in row and str(count) in row, (
            f"the allowance row does not name the residual `{family}` ({count}), "
            "and an unnamed residual is the one thing the published allowance "
            f"is for: {row}"
        )
    if not alw["families"]:
        assert "empty" in row, (
            "the allowance is empty, so the row has to say that it is empty "
            f"rather than leaving a bare 0 to be read as unmeasured: {row}"
        )


def test_the_provenance_fraction_is_published_beside_its_tool(rendered):
    """Exit item 3: the fraction and the tool that measures it, in the same
    place, so the number cannot be quoted without its method."""
    report, files = rendered
    census = report["census"]
    readme = files["README.md"]
    assert census["provenance"]["tool"] in readme, (
        "the provenance fraction is published without the tool that measured it"
    )
    for row in census["provenance"]["corpora"]:
        if row["corpus"] == "census":
            assert f"{row['independent_percent']}% of {row['total']}" in readme, (
                "the published provenance fraction is not the one the report "
                f"carries ({row['independent_percent']}% of {row['total']})"
            )


def test_the_report_the_edition_publishes_passes_check_eval_report(publisher,
                                                                   rendered):
    """Exit item 4. The tool enforces this before it writes anything; this is
    the same verdict, held here so a report that starts over-claiming is a red
    in the suite and not only a failed deploy."""
    report, _ = rendered
    checker = publisher._load("tools/check_eval_report.py", "exit4_checker")
    assert checker.check_report(report) == [], (
        "the report the edition carries does not pass tools/check_eval_report.py"
    )


def test_the_published_commands_run_the_census_from_outside_the_checkout(
        rendered):
    """Exit item 1: runnable outside this repository — which the documented
    route has to be, not merely look. The census measures its inputs through an
    audit hook on every file the run opens, so a venv inside the clone puts
    pytest's own modules in the pin set and `--check` fails on `pins.jsonl` with
    UNPINNED INPUT lines that are the reader's environment. The commands
    therefore install outside the checkout, and this is what keeps them that
    way."""
    _, files = rendered
    for name in ("README.md", "index.html"):
        text = files[name]
        assert "census_artifact.py --check" in text, (
            f"{name} no longer shows a reader how to reproduce the census"
        )
        assert not re.search(r"venv\s+\.venv\b", text), (
            f"{name} tells the reader to create the venv INSIDE the checkout, "
            "which makes `tools/census_artifact.py --check` fail on pins.jsonl "
            "for a reason that is the reader's environment and not the census"
        )


# --- the edition is a copy, and check is exact ----------------------------- #
def test_two_renders_of_one_tree_are_byte_identical(publisher, rendered):
    """No timestamp and no absolute path anywhere, which is what lets `--check`
    be a byte comparison and lets the manifest's hashes mean something."""
    report, files = rendered
    artifact, again = publisher.render()
    assert publisher.edition(artifact, again) == files
    assert set(files) == set(publisher.FILES), (
        "the edition's file set drifted from FILES"
    )
    for name, text in files.items():
        assert str(ROOT) not in text, f"{name} embeds this checkout's path"
        assert not re.search(r"\b20\d\d-\d\d-\d\dT", text), (
            f"{name} embeds a timestamp, so two renders of one tree differ"
        )


def test_the_manifest_hashes_every_file_in_the_edition(publisher, rendered):
    _, files = rendered
    manifest = json.loads(files["MANIFEST.json"])
    assert manifest["schema"] == publisher.EDITION_SCHEMA
    assert manifest["url"] == URL
    hashed = [n for n in publisher.FILES if n != "MANIFEST.json"]
    for name in hashed:
        assert name in manifest["files"], f"MANIFEST.json does not hash {name}"
        assert manifest["files"][name]["sha256"] == publisher._sha256(files[name]), (
            f"MANIFEST.json's hash for {name} is not the file's"
        )
        assert manifest["files"][name]["bytes"] == len(files[name].encode("utf-8"))
    # A file cannot carry its own hash, so MANIFEST.json is the one entry it
    # cannot have — and the reader's route to checking it is the edition's
    # sha256 from wherever they fetched it.
    assert "MANIFEST.json" not in manifest["files"]


def test_check_is_exact_in_both_directions(publisher, rendered, tmp_path):
    """A mutated byte and a missing file are both differences. Without this the
    deploy could publish anything and `--check` would agree."""
    _, files = rendered
    dest = tmp_path / "edition"
    assert publisher.write(files, dest) == 0
    assert publisher.check(files, dest) == 0

    readme = dest / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "extra\n",
                      encoding="utf-8")
    assert publisher.check(files, dest) == 1, "a mutated edition passed --check"

    readme.write_text(files["README.md"], encoding="utf-8")
    (dest / "MANIFEST.json").unlink()
    assert publisher.check(files, dest) == 1, "a missing file passed --check"


def test_an_over_claiming_report_is_not_published(publisher, monkeypatch,
                                                  tmp_path):
    """The point of running the checker BEFORE writing: a report that
    over-claims is withheld rather than published and noticed."""
    real_load = publisher._load

    def load(rel, name):
        if rel.endswith("check_eval_report.py"):
            return type("Stub", (), {"check_report": staticmethod(
                lambda report: ["a claim the numbers do not carry"])})
        return real_load(rel, name)

    monkeypatch.setattr(publisher, "_load", load)
    dest = tmp_path / "withheld"
    with pytest.raises(SystemExit) as raised:
        publisher.main(["--write", "--dest", str(dest)])
    assert "check_eval_report" in str(raised.value)
    assert not dest.exists(), (
        "the edition was written despite the report failing "
        "tools/check_eval_report.py"
    )
