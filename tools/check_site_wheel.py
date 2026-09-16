#!/usr/bin/env python3
"""The published playground's compiler wheel: built at deploy time, checked here.

The playground boots the compiler in-process under Pyodide from a wheel built
straight out of `src/revl` (plus the py tier's top-level modules) by
`playground/build_wheel.py`. That wheel USED TO BE COMMITTED, at
`playground/vendor/` and `site/vendor/`, and gated against a fresh build by
this tool. That arrangement is what this file no longer implements, and the
reasoning is the point of this header.

WHY THE COMMITTED COPY IS GONE.

A wheel that vendors 200-odd modules is stale the moment any one of them
changes, so the committed copy could only ever be correct for the sha it was
built on. The gate around it (`.github/workflows/site-wheel.yml`, main-only,
issue #252/#547) found that honestly and often: `site wheel drift` was refreshed
four times in one day (#1095, #1103, #1115, #1133) and was red again within
hours of each, because a refresh is written against one sha and lands several
merges later. There is no scheduling of refreshes that closes that window: the
window IS the queue.

The three ways to close it were weighed and only one is free:

  1. Regenerate it in the PR that changes an input. `playground/build_wheel.py`
     now declares its inputs (#1104) so the selector can tell a `parser.py` PR
     from a test-only one, which answers the original precision objection. It
     still does not work HERE. The wheel is a 2.5 MB DEFLATE zip, and
     `zipfile.writestr` stamps wall-clock time, so two builds of identical
     source do not even agree byte-wise. Two PRs touching `src/revl` therefore
     write two different blobs to the same binary path and conflict by
     construction — and this repo lands through a merge queue (`merge_group` in
     ci.yml), which builds each candidate on the queue's tip, so the loser is
     not a conflict a human resolves at leisure, it is an ejected PR. That is a
     worse outage class than the one it replaces.
  2. Have the drift job commit the refresh itself. `GITHUB_TOKEN` cannot open a
     PR that triggers `ci.yml`, so that half needs a PAT or an App — a standing
     credential — and a direct push needs branch protection relaxed on main.
     Both are owner-only, and both make the growth below permanent.
  3. Stop committing a file that is a pure function of the tree, and build it
     into the deploy artifact instead. No credential, no merge latency, no work
     moved onto PR authors.

The cost of (1) and (2) is measurable, and it has already been paid 191 times:
the two committed wheels account for 49.3 MiB of this repository's 66.0 MiB of
packed blob data. Three quarters of the repo's weight is revisions of a
generated zip. Every future regeneration, by whatever mechanism, adds ~260 KiB
of permanent history for an artifact whose fix is one stdlib-only command.

WHAT REPLACES THE DRIFT CHECK. Nothing detects drift any more because drift is
no longer possible: `.github/workflows/pages.yml` builds the wheel from the
checkout it is about to publish, so the published playground is built from the
sha being deployed, always, with no step anyone has to remember. Detection is
replaced by construction, which is strictly stronger than the gate it retires.

That substitution introduces exactly one new way to be wrong — the deploy could
stop building the wheel, or could build one the page does not ask for — so this
tool now checks THAT, and `site-wheel.yml` still runs it post-merge on main and
weekly:

  1. `pages.yml` runs a builder BEFORE it uploads `site/`. A deploy that
     dropped that step would publish a `site/` with no wheel in it and 404 the
     playground, and nothing else in the tree would notice.
  2. A fresh build is named exactly what the pages fetch. `site/js/playground.js`
     and `playground/app.js` hardcode `vendor/revl-2.0.0-py3-none-any.whl`,
     while the builder names its output from `pyproject.toml`'s version — so a
     version bump renames the wheel and 404s both playgrounds. That was true
     before this change too and nothing checked it.
  3. The fresh build's member set equals `git ls-files` of the trees it
     vendors. Computed from git rather than from the builder's own `_members()`
     so it is an outside check, the same way `tools/check_wheel_manifest.py`
     asserts the PyPI wheel from outside its build hook: a builder that emitted
     an empty or half-populated zip in the deploy environment would otherwise
     upload cleanly.

    python3 tools/check_site_wheel.py           # check the three claims above
    python3 tools/check_site_wheel.py --write    # materialize the wheel locally

`--write` is what the deploy runs, and what to run before serving `site/` or
`playground/` from a checkout — the wheel is gitignored, so a fresh clone has
none until you build one. It is stdlib-only and takes about a second.

SCOPE: the `revl-*.whl` wheel only. `site/vendor/cordis-*.whl` IS still
committed and must stay that way: its source is the pinned
`backends/python/setup.sh` clone, which is absent from a plain checkout and
from this tool's workflow, so it is not a function of this tree and cannot be
built at deploy time. It is gated instead by
`tests/test_cordis_wheel_records_its_source_revision_1029.py`.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Where `--write` materializes the wheel. Both are gitignored: `site/vendor` is
# what the deploy uploads, `playground/vendor` is what `python3 -m http.server`
# serves out of a checkout.
TARGETS = (
    ROOT / "playground" / "vendor",
    ROOT / "site" / "vendor",
)
# The pages that fetch the wheel by name, and the JS constant each one uses.
CONSUMERS = (
    Path("site") / "js" / "playground.js",
    Path("playground") / "app.js",
)
WHEEL_REF_RE = re.compile(r'vendor/(revl-[^"\']+\.whl)')

PAGES_WORKFLOW = Path(".github") / "workflows" / "pages.yml"
# Any of these, run as a workflow step, materializes the wheel into site/vendor.
BUILDERS = (
    "tools/check_site_wheel.py --write",
    "site/build.py",
)
UPLOAD_ACTION = "upload-pages-artifact"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _build_wheel_module():
    return _load("build_wheel", ROOT / "playground" / "build_wheel.py")


def _build_fresh(out_dir: Path) -> Path:
    """Build the revl wheel from the current tree into out_dir; return its path."""
    bw = _build_wheel_module()
    # build_wheel writes to its own OUT_DIR and prints a path relative to ROOT;
    # redirect both so the build lands in a scratch dir and the print stays sane.
    bw.OUT_DIR = out_dir
    bw.ROOT = ROOT
    bw.main()
    return next(out_dir.glob("revl-*.whl"))


# --- claim 1: the deploy builds it ----------------------------------------- #
def _check_deploy_builds_the_wheel() -> list[str]:
    """`pages.yml` must run a builder before it uploads `site/`.

    Read as text, in order: a builder step that came AFTER the upload would
    publish the checkout's (absent) wheel and rebuild it into a directory
    nobody reads.
    """
    path = ROOT / PAGES_WORKFLOW
    if not path.is_file():
        return [f"{PAGES_WORKFLOW} is gone; nothing publishes site/ any more."]
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    upload_at = next((i for i, ln in enumerate(lines) if UPLOAD_ACTION in ln), None)
    if upload_at is None:
        return [f"{PAGES_WORKFLOW} no longer uses {UPLOAD_ACTION}; "
                "re-derive this check against whatever publishes site/."]
    build_at = next(
        (i for i, ln in enumerate(lines) if any(b in ln for b in BUILDERS)), None
    )
    if build_at is None:
        return [
            f"{PAGES_WORKFLOW} uploads site/ without building the compiler wheel "
            f"(no step runs any of {', '.join(BUILDERS)}).",
            "    The wheel is not committed, so the published playground would "
            "404 on it.",
        ]
    if build_at > upload_at:
        return [
            f"{PAGES_WORKFLOW} builds the wheel AFTER {UPLOAD_ACTION}, so the "
            "upload carries the state of the checkout, not of the build.",
        ]
    return []


# --- claim 2: the pages fetch the name the builder writes ------------------ #
def _check_consumers_name_the_built_wheel(built_name: str) -> list[str]:
    """Every page that loads the wheel must name the file a fresh build writes.

    `pyproject.toml` supplies the version that names the wheel; the pages
    hardcode it. A bump moves the artifact and leaves the fetch behind.
    """
    problems: list[str] = []
    for rel in CONSUMERS:
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"{rel} is gone; re-derive this check against the "
                            "page that now loads the wheel.")
            continue
        refs = set(WHEEL_REF_RE.findall(path.read_text(encoding="utf-8")))
        if not refs:
            problems.append(f"{rel} no longer fetches a revl wheel at all.")
            continue
        wrong = sorted(r for r in refs if r != built_name)
        if wrong:
            problems.append(
                f"{rel} fetches vendor/{', vendor/'.join(wrong)} but a fresh "
                f"build writes {built_name} (pyproject.toml's version names it)."
            )
    return problems


# --- claim 3: the build vendors what the commit tracks --------------------- #
def _tracked_members() -> dict[str, None] | None:
    """Expected wheel arcnames, from `git ls-files` rather than from the builder.

    The tree -> arcname map comes from `build_wheel.SOURCE_TREES` (one source of
    truth for WHAT is vendored, also read by `tools/affected_tests.py`), but the
    file list is asked of git here, so a builder that walked the wrong directory
    or produced an empty zip is a red rather than a clean upload. Returns None
    when git cannot answer, which is the builder's own fallback condition.
    """
    bw = _build_wheel_module()
    out: dict[str, None] = {}
    for rel_tree, pattern, prefix in bw.SOURCE_TREES:
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-z", "--", rel_tree],
                cwd=ROOT, capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        paths = [p for p in proc.stdout.split("\0") if p]
        if not paths:
            return None
        for rel in paths:
            # Mirror the builder's glob: `**/*.py` is recursive, `*.py` is the
            # tree's top level only (revl/backends/python is a FLAT layout).
            sub = Path(rel).relative_to(rel_tree)
            if sub.suffix != ".py":
                continue
            if pattern == "*.py" and len(sub.parts) != 1:
                continue
            out[prefix + sub.as_posix()] = None
    return out or None


def _check_members(wheel: Path) -> list[str]:
    expected = _tracked_members()
    if expected is None:
        return ["    (skipped: git could not list the vendored trees)"]
    with zipfile.ZipFile(wheel) as zf:
        got = {n for n in zf.namelist() if not n.startswith("revl-")}
        dist_info = {n for n in zf.namelist() if n.startswith("revl-")}
    problems: list[str] = []
    missing = sorted(set(expected) - got)
    extra = sorted(got - set(expected))
    if missing:
        problems.append(f"    the build omits {len(missing)} tracked module(s): "
                        + ", ".join(missing[:12])
                        + (" ..." if len(missing) > 12 else ""))
    if extra:
        problems.append(f"    the build vendors {len(extra)} file(s) the commit "
                        "does not track: " + ", ".join(extra[:12])
                        + (" ..." if len(extra) > 12 else ""))
    if not dist_info:
        problems.append("    the build has no .dist-info; micropip cannot "
                        "install it.")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="materialize the wheel into playground/vendor and "
                         "site/vendor instead of checking (what the deploy runs)")
    args = ap.parse_args()

    if args.write:
        bw = _build_wheel_module()
        bw.main()  # writes playground/vendor/revl-<version>.whl
        built = next((ROOT / "playground" / "vendor").glob("revl-*.whl"))
        for vendor in TARGETS:
            vendor.mkdir(parents=True, exist_ok=True)
            dest = vendor / built.name
            if dest.resolve() != built.resolve():
                shutil.copy2(built, dest)
                print(f"wrote {dest.relative_to(ROOT)}")
        return 0

    problems: list[str] = []

    problems += _check_deploy_builds_the_wheel()

    # Build under ROOT: build_wheel prints its output path relative to ROOT, so
    # a scratch dir outside the tree would make that print raise.
    tmp = Path(tempfile.mkdtemp(prefix=".wheelcheck-", dir=ROOT))
    try:
        fresh = _build_fresh(tmp)
        problems += _check_consumers_name_the_built_wheel(fresh.name)
        problems += _check_members(fresh)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The final line must not contradict what precedes it: a stale wheel once
    # printed "matches a fresh build" for a day, and a log tail read the
    # opposite of the truth.
    if problems:
        for line in problems:
            print(f"::warning::{line}" if not line.startswith("  ") else line)
        print("the site wheel's deploy contract is BROKEN (see above)")
        return 1
    print("site wheel deploy contract OK: pages.yml builds it before upload, "
          "the pages fetch the name it writes, and it vendors what git tracks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
