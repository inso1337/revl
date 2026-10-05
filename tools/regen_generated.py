#!/usr/bin/env python3
"""Resolve generated-file conflicts after `git merge origin/main` (issue #1784).

Merging main into a PR branch conflicts, over and over, in files nobody
writes: the gate crates, the derived grammar, the census artifact and its
crate reproduction, the generated block of `formal/STATUS.md`, the
conformance matrix and the docgen blocks. The resolution is always the same:
take main's side, rerun the file's generator on the merged tree, in the order
the generators read each other's output, then run every generator's check.

This tool does exactly that, and nothing a generator does not do:

* It only CALLS generators, by their own command lines, so it never parses a
  generated file. A layout change in any of them needs no change here; only a
  renamed command or a moved file does, and both live in `REGISTRY` below.
* A file that is WHOLLY generated is taken from main and regenerated.
* A file that only CONTAINS a generated region (`formal/STATUS.md`, a doc with
  a docgen block, the README matrix) is merged, not taken. Each of its three
  versions (the merge base, this branch, main) is regenerated against the
  merged inputs, so their generated regions become identical, and the three
  results are merged with `git merge-file`. The hand-written text merges like
  any text; a real conflict in it stays a conflict, with the generated region
  already fresh. Neither side's prose is ever dropped.
* A file whose generator reads through conflict markers itself (the coverage
  ledger since issue #1768) is handed to that generator as it stands. Only the
  numbers a person owns are left: the ledger's per-function budgets, which its
  `--check` names.
* Hand-maintained ratchets are never resolved. A conflict in one is printed
  with the rule for resolving it by hand.
* When main has moved a generated file to a new layout (one file split into
  records, say), every branch cut before that hits a one-time modify/delete
  conflict on the old file. `TRANSITIONS` names those moves: main's side is
  taken (usually the deletion) and the new layout is regenerated.
* `bench/results/` is left alone unless `--bench` is passed.
* A tool is looked for on PATH and then in its own install directory
  (`TOOL_HOMES`: elan puts `lake` in `~/.elan/bin`, rustup puts `cargo` in
  `~/.cargo/bin`), because a shell that never sourced their profile scripts
  has neither on PATH. A directory found that way goes on the generators' PATH.
* A generator whose tool is still missing is skipped LOUDLY and the files it
  owns are left as they are. The run then exits 1, because an unchecked file
  is not a passing one (issue #1864); `--allow-skip` accepts the skip on a
  machine that cannot install the tool. A slow step `--fast` skips is the
  caller's choice and does not fail the run.

Usage, from inside the worktree, after the merge stops on its conflicts:

    git merge origin/main             # stops with conflicts
    python3 tools/regen_generated.py  # resolve + regenerate + check
    git commit                        # what the tool did is staged

    python3 tools/regen_generated.py --all          # regenerate everything
    python3 tools/regen_generated.py --only census  # one generator
    python3 tools/regen_generated.py --list         # what it knows

Exit status: 0 when every conflict it owns is resolved and every check
passes; 1 when a check fails, a conflict is left for a human, or a generator
was skipped for a missing tool without --allow-skip; 2 on usage.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile

# Each generator: the files it owns (globs, repo-relative), how a conflict in
# one is merged, the commands that regenerate them in order, and the commands
# that check them. `merge` is "theirs" (wholly generated: take main's side,
# regenerate), "three-way" (partly generated: regenerate each side, merge the
# results) or "in-place" (the generator resolves the conflict markers itself).
# `when` names a path that must exist in the merged tree for the generator to
# run (a layout it only knows from a given commit on); `hint` is printed when
# its check fails; `feeds` names the later generators that read this one's
# output, so regenerating it reruns them too. `{python}` is this interpreter, `{tmp}` a scratch
# directory, `{generation}` the --provenance-generation value. A step may name
# `requires` (executables that must be on PATH), `cwd` (relative to the repo
# root), `needs` (a path that must exist, usually an earlier step's output)
# and `slow` (skipped by --fast). Listed in DEPENDENCY ORDER: the census reads
# the gate crate and the provenance manifest, the formal block reads the
# corpus, docgen reads the conformance matrix.
REGISTRY = [
    {
        "name": "gate-crates",
        "paths": ["crates/revl-gate/*", "crates/revl-gate-wasm/*"],
        "merge": "theirs",
        "write": [{"run": ["{python}", "tools/build_gate_crate.py"]},
                  {"run": ["{python}", "tools/build_gate_wasm.py"]}],
        "check": [{"run": ["{python}", "tools/build_gate_crate.py", "--check"]},
                  {"run": ["{python}", "tools/build_gate_wasm.py", "--check"]}],
    },
    {
        "name": "grammar",
        "paths": ["grammar/revl.lark", "grammar/revl.gbnf", "grammar/revl.ebnf"],
        "merge": "theirs",
        "write": [{"run": ["{python}", "-m", "revl", "grammar", "--write"]}],
        "check": [{"run": ["{python}", "-m", "revl", "grammar", "--check"]}],
    },
    {
        "name": "provenance",
        "paths": ["tests/fixtures/corpus_provenance.json"],
        # main's declarations plus this branch's new documents, re-declared at
        # the generation the author names: there is deliberately no default.
        "merge": "three-way",
        "option": "provenance_generation",
        # the census pins the manifest and reports its table
        "feeds": ["census"],
        "write": [{"run": ["{python}", "tools/corpus_provenance.py", "--write",
                           "--generation", "{generation}"]}],
        "check": [{"run": ["{python}", "tools/corpus_provenance.py", "--check"]}],
        "hint": "an UNDECLARED document is one this branch added without a "
                "generation. Declare it with `python3 tools/regen_generated.py "
                "--only provenance --provenance-generation N` (N is the "
                "generation that authored it; the manifest's other documents "
                "show which is current).",
    },
    {
        "name": "census",
        # the records layout of issue #1768; the single json + md before it is
        # in TRANSITIONS
        "paths": ["docs/census-artifact/*",
                  "tests/fixtures/census_crate_reproduction/*"],
        "merge": "theirs",
        "write": [
            {"run": ["{python}", "tools/gate_reference_census.py", "--engine",
                     "crate", "--check", "--no-provenance", "--json",
                     "{tmp}/crate-census.json"],
             "requires": ["cargo"], "slow": True},
            {"run": ["{python}", "tools/census_artifact.py", "--crate-json",
                     "{tmp}/crate-census.json", "--record-reproduction"],
             "needs": "{tmp}/crate-census.json"},
            {"run": ["{python}", "tools/census_artifact.py", "--write"]},
        ],
        "check": [{"run": ["{python}", "tools/census_artifact.py", "--verify",
                           "--strict"]}],
    },
    {
        "name": "formal",
        "paths": ["formal/STATUS.md"],
        "merge": "three-way",
        "requires": ["lake"],
        "write": [{"run": ["lake", "build"], "cwd": "formal"},
                  {"run": ["{python}", "formal/harness/diff_corpus.py",
                           "--write-status"]}],
        # the differential gate reads the build output, so it is checked
        # against a fresh build of THIS tree, never a stale one
        "check": [{"run": ["lake", "build"], "cwd": "formal"},
                  {"run": ["{python}", "formal/harness/diff_corpus.py"]}],
    },
    {
        "name": "conformance",
        "paths": ["README.md", "docs/conformance.md"],
        "merge": "three-way",
        "write": [{"run": ["{python}", "tools/conformance.py", "--write-readme"]}],
        "check": [{"run": ["{python}", "tools/conformance.py", "--check-readme"]}],
    },
    {
        "name": "ledger",
        # per half and tier since issue #1768; the single json before it was
        # hand-maintained, see TRANSITIONS
        "paths": ["tests/fixtures/selfhost_uncovered_lines/*/*.jsonl"],
        "merge": "in-place",
        "when": "tests/fixtures/selfhost_uncovered_lines/README.md",
        "write": [{"run": ["{python}", "tools/selfhost_line_coverage.py",
                           "--write"], "slow": True}],
        "check": [{"run": ["{python}", "tools/selfhost_line_coverage.py",
                           "--check"], "slow": True}],
        "hint": "`--write` re-measured every count but never writes a budget. "
                "Each function the check names needs its budget (the last "
                "field of its record) set BY HAND to the merged count, in this "
                "merge commit. Lower is the goal; a raised budget needs a reason.",
    },
    {
        "name": "docgen",
        "paths": ["docs/DOC-STATUS.md", "docs/mcp-reference.md",
                  "docs/guide-ai-agents.md", "docs/authoring-for-agents.md",
                  "docs/commands-reference.md", "docs/rejections.md",
                  "DESIGN.md", "docs/guide-humans.md", "docs/vision.md",
                  "docs/selfhost-compile.md", "docs/selfhost-findings.md"],
        "merge": "three-way",
        "write": [{"run": ["{python}", "tools/docgen.py", "--write"]}],
        "check": [{"run": ["{python}", "tools/docgen.py", "--check"]}],
    },
]

# Hand-maintained files that look generated and are not. Never resolved here.
HAND = [
    {"paths": ["tests/fixtures/selfhost_blind_spots.json",
               "tests/fixtures/oracle_construct_reach_ledger.json"],
     "rule": "a hand-recorded construct ratchet. Take main's side, then re-add "
             "only the entries this branch recorded on purpose, and confirm "
             "with tests/test_selfhost_coverage.py and "
             "tests/test_oracle_construct_reach.py."},
]

BENCH = ["bench/results/*"]

LEDGER_RULE = ("the coverage ledger is hand-maintained PER FUNCTION. Take main's "
               "side (`git checkout --theirs -- <file>`), run "
               "`python3 tools/selfhost_line_coverage.py --check`, and move only "
               "the counts it names, lowering `_budget` by exactly what you "
               "close. Never `--write` it wholesale, and keep any reason key this "
               "branch edited.")

# A generated file main has moved to a new layout. `marker` is a path only the
# new layout has. Main without it: the old paths are still current and belong
# to `before` (a generator name, or a hand rule). Main with it and the merge
# base without it: this merge crosses the move once, so main's side of the old
# paths is taken (the deletion, or the page that replaced the file) and
# `group` regenerates the new layout. Both with it: the move is history and the
# old paths are ordinary files (`docs/census-artifact.md` is hand-written now).
TRANSITIONS = [
    {"paths": ["docs/census-artifact.json", "docs/census-artifact.md"],
     "marker": "docs/census-artifact/cases.jsonl",
     "group": "census", "before": {"group": "census"}},
    {"paths": ["tests/fixtures/selfhost_uncovered_lines.json"],
     "marker": "tests/fixtures/selfhost_uncovered_lines/README.md",
     "group": "ledger", "before": {"rule": LEDGER_RULE}},
    # the recorded crate reproduction (issue #1768): a single json until its
    # stored program count churned, then reproduction.json + programs.jsonl
    # until its checker_version line churned, now one `<checker version>.json`
    # per record. A branch from either earlier layout crosses to this one once.
    {"paths": ["tests/fixtures/census_crate_reproduction.json",
               "tests/fixtures/census_crate_reproduction/reproduction.json",
               "tests/fixtures/census_crate_reproduction/programs.jsonl"],
     "marker": "tests/fixtures/census_crate_reproduction/README.md",
     "group": "census", "before": {"group": "census"}},
]


# ------------------------------------------------------------------ git

def _git(root: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", root, *args], capture_output=True,
                            text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"regen_generated: git {' '.join(args)} failed: "
                         f"{result.stderr.strip()}")
    return result.stdout


def _toplevel() -> str:
    return _git(os.getcwd(), "rev-parse", "--show-toplevel").strip()


def _merging(root: str) -> bool:
    git_dir = _git(root, "rev-parse", "--git-dir").strip()
    path = git_dir if os.path.isabs(git_dir) else os.path.join(root, git_dir)
    return os.path.exists(os.path.join(path, "MERGE_HEAD"))


def _unmerged(root: str) -> list:
    out = _git(root, "diff", "--name-only", "--diff-filter=U")
    return sorted(p for p in out.splitlines() if p)


def _stage_text(root: str, path: str, stage: int) -> str | None:
    """The text of `path` at index stage 1 (merge base), 2 (this branch) or 3
    (main), or None when that side has no such file."""
    result = subprocess.run(["git", "-C", root, "show", f":{stage}:{path}"],
                            capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _has_path(root: str, rev: str | None, path: str) -> bool:
    if rev is None:
        return False
    return bool(_git(root, "ls-tree", "--name-only", rev, "--", path,
                     check=False).strip())


def _merge_base(root: str) -> str | None:
    result = subprocess.run(["git", "-C", root, "merge-base", "HEAD", "MERGE_HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() or None if result.returncode == 0 else None


# -------------------------------------------------------------- layouts

def _apply_transitions(registry, hand, transitions, root: str, merging: bool):
    """The registry and hand list for the layout main is at, plus the paths
    this merge moves across a layout change: {path: group name}."""
    registry = [dict(g, paths=list(g["paths"])) for g in registry]
    hand = list(hand)
    moving: dict = {}
    main_rev = "MERGE_HEAD" if merging else "HEAD"
    base_rev = _merge_base(root) if merging else None
    for move in transitions:
        on_main = _has_path(root, main_rev, move["marker"])
        if not on_main:
            before = move["before"]
            if "rule" in before:
                hand.append({"paths": move["paths"], "rule": before["rule"]})
            else:
                owner = next(g for g in registry if g["name"] == before["group"])
                owner["paths"] += move["paths"]
        elif merging and not _has_path(root, base_rev, move["marker"]):
            for path in move["paths"]:
                moving[path] = move["group"]
    return registry, hand, moving


def _cross_layout(root: str, path: str, group: str) -> None:
    """Take main's side of a file main moved to a new layout."""
    gone = _stage_text(root, path, 3) is None
    _take_theirs(root, path)
    _say(f"{path}: main moved it to a new layout ({'deleted' if gone else 'replaced'}"
         f" on main); took main's side, {group} regenerates the new files")


# ------------------------------------------------------------- matching

def _matches(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, glob) for glob in globs)


def _owner(path: str, registry) -> dict | None:
    return next((g for g in registry if _matches(path, g["paths"])), None)


def _hand_rule(path: str, hand) -> str | None:
    return next((h["rule"] for h in hand if _matches(path, h["paths"])), None)


# -------------------------------------------------------------- running

# Where a tool's installer puts it when the shell's PATH does not say
# (issue #1864). Looked in only after PATH, so a PATH entry always wins.
TOOL_HOMES = {
    "lake": ["~/.elan/bin"],
    "cargo": ["~/.cargo/bin"],
}


def _homes(tool: str) -> list:
    return [os.path.expanduser(d) for d in TOOL_HOMES.get(tool, ())]


def _find(tool: str) -> str | None:
    """The tool's executable: on PATH, else in its install directory."""
    found = shutil.which(tool)
    if found is None:
        homes = os.pathsep.join(_homes(tool))
        found = shutil.which(tool, path=homes) if homes else None
    return found


def _home_dirs() -> list:
    """The install directories that hold a tool PATH does not."""
    return [os.path.dirname(found) for tool in TOOL_HOMES
            if shutil.which(tool) is None and (found := _find(tool))]


class Context:
    def __init__(self, root: str, python: str, tmp: str, args) -> None:
        self.root, self.python, self.tmp, self.args = root, python, tmp, args
        self.skipped: list = []      # labels skipped for a missing tool

    def env(self) -> dict:
        """The generators' environment: THIS tree's `src/` first on the path,
        so `python -m revl` and every tool import the revl being regenerated,
        never another checkout's editable install."""
        env = dict(os.environ)
        extra = _home_dirs()
        if extra:
            env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
        src = os.path.join(self.root, "src")
        if os.path.isdir(src):
            env["PYTHONPATH"] = os.pathsep.join(
                p for p in (src, env.get("PYTHONPATH", "")) if p)
        return env

    def expand(self, value: str) -> str:
        generation = getattr(self.args, "provenance_generation", None)
        return (value.replace("{python}", self.python).replace("{tmp}", self.tmp)
                .replace("{generation}", "" if generation is None else str(generation)))


def _missing(step: dict) -> list:
    return [tool for tool in step.get("requires") or () if _find(tool) is None]


def _resolve(cmd: list) -> list:
    """A bare command name found only in its install directory, by path."""
    if cmd and os.sep not in cmd[0] and shutil.which(cmd[0]) is None:
        found = _find(cmd[0])
        if found:
            return [found, *cmd[1:]]
    return cmd


def _skip_missing(ctx, label: str, missing: list, what: str) -> None:
    ctx.skipped.append(f"{label} ({' '.join(missing)})")
    _loud(f"{label}: {' '.join(missing)} is not installed, so {what}"
          + ("" if ctx.args.allow_skip else
             ". This fails the run; install it, or pass --allow-skip"))


def _say(message: str) -> None:
    print(f"regen_generated: {message}", flush=True)


def _loud(message: str) -> None:
    bar = "!" * 72
    print(f"{bar}\nregen_generated: SKIPPED. {message}\n{bar}", flush=True)


def _run_step(step: dict, ctx: Context, label: str) -> bool | None:
    """True on success, False on failure, None when skipped."""
    cmd = [ctx.expand(part) for part in step["run"]]
    shown = " ".join(cmd)
    missing = _missing(step)
    if missing:
        _skip_missing(ctx, label, missing, f"`{shown}` did not run")
        return None
    cmd = _resolve(cmd)
    if step.get("slow") and ctx.args.fast:
        _loud(f"{label}: --fast, so the slow step `{shown}` did not run")
        return None
    needs = step.get("needs")
    if needs and not os.path.exists(ctx.expand(needs)):
        _loud(f"{label}: `{shown}` needs {ctx.expand(needs)}, which an earlier "
              f"step did not produce")
        return None
    cwd = os.path.join(ctx.root, step.get("cwd") or "")
    _say(f"{label}: {' '.join(cmd)}" + (f"  (in {step['cwd']})" if step.get("cwd") else ""))
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                            env=ctx.env())
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-12:]
        _say(f"{label}: exited {result.returncode}\n    " + "\n    ".join(tail))
        return False
    return True


def _absent_layout(group: dict, ctx: Context) -> str | None:
    """The `when` path this tree lacks, so the generator does not apply."""
    when = group.get("when")
    if when and not os.path.exists(os.path.join(ctx.root, when)):
        return when
    return None


def _group_runnable(group: dict, ctx: Context) -> bool:
    absent = _absent_layout(group, ctx)
    if absent:
        _say(f"{group['name']}: this tree has no {absent}, so it predates the "
             f"layout this generator writes; not run")
        return False
    missing = _missing(group)
    if missing:
        _skip_missing(ctx, group["name"], missing,
                      f"{', '.join(group['paths'])} are left exactly as they are")
        return False
    option = group.get("option")
    if option and getattr(ctx.args, option, None) is None:
        flag = "--" + option.replace("_", "-")
        _loud(f"{group['name']}: needs {flag} N (the generation this branch's "
              f"new documents are declared at; there is no default), so "
              f"{', '.join(group['paths'])} are left exactly as they are")
        return False
    return True


def _regenerate(group: dict, ctx: Context) -> bool:
    ok = True
    for step in group["write"]:
        outcome = _run_step(step, ctx, f"{group['name']} write")
        if outcome is False:
            ok = False
            break
    return ok


def _check(group: dict, ctx: Context) -> bool | None:
    if _absent_layout(group, ctx):
        return None
    if _missing(group):
        _skip_missing(ctx, f"{group['name']} check", _missing(group),
                      "its check did not run")
        return None
    results = [_run_step(step, ctx, f"{group['name']} check")
               for step in group["check"]]
    if any(r is False for r in results):
        if group.get("hint"):
            _say(f"{group['name']} check: {group['hint']}")
        return False
    return None if all(r is None for r in results) else True


# --------------------------------------------------------- resolution

def _take_theirs(root: str, path: str) -> None:
    """Main's side of `path`, staged; main's deletion when main deleted it."""
    if _stage_text(root, path, 3) is None:
        _git(root, "rm", "-q", "--force", "--", path)
        return
    _git(root, "checkout", "--theirs", "--", path)
    _git(root, "add", "--", path)


def _write(root: str, path: str, text: str | None) -> None:
    full = os.path.join(root, path)
    if text is None:
        if os.path.exists(full):
            os.remove(full)
        return
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


def _read(root: str, path: str) -> str:
    full = os.path.join(root, path)
    if not os.path.exists(full):
        return ""
    with open(full, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _merge_file(ours: str, base: str, theirs: str, path: str, tmp: str) -> tuple:
    """`git merge-file` of three texts: (merged text, conflict count)."""
    names = []
    for side, text in (("ours", ours), ("base", base), ("theirs", theirs)):
        name = os.path.join(tmp, f"merge-{side}")
        with open(name, "w", encoding="utf-8") as handle:
            handle.write(text)
        names.append(name)
    result = subprocess.run(
        ["git", "merge-file", "-p", "-L", f"{path} (this branch)",
         "-L", f"{path} (merge base)", "-L", f"{path} (main)", *names],
        capture_output=True, text=True)
    if result.returncode < 0:
        raise SystemExit(f"regen_generated: git merge-file failed on {path}")
    return result.stdout, result.returncode


def _resolve_partial(group: dict, paths: list, ctx: Context) -> list:
    """Regenerate each version of the partly generated `paths`, merge them;
    return the paths still in conflict."""
    versions: dict = {path: {} for path in paths}
    for stage, side in ((2, "ours"), (1, "base"), (3, "theirs")):
        for path in paths:
            _write(ctx.root, path, _stage_text(ctx.root, path, stage) or "")
        if not _regenerate(group, ctx):
            _say(f"{group['name']}: a generator failed on the {side} side")
            return _restore(ctx.root, paths)
        for path in paths:
            versions[path][side] = _read(ctx.root, path)
    left = []
    for path in paths:
        v = versions[path]
        text, conflicts = _merge_file(v["ours"], v["base"], v["theirs"], path,
                                      ctx.tmp)
        _write(ctx.root, path, text)
        if conflicts:
            _say(f"{path}: only PART of it is generated. Its generated region is "
                 f"regenerated; its hand-written text has {conflicts} real "
                 f"conflict(s) between this branch and main, left marked for you. "
                 f"Resolve them, `git add {path}`, then rerun with --only "
                 f"{group['name']}")
            left.append(path)
            continue
        _git(ctx.root, "add", "--", path)
    # one more pass over the merged texts: a generator that also writes a
    # file derived from these (DOC-STATUS from every doc) last saw main's side
    if not left and _regenerate(group, ctx):
        for path in paths:
            _git(ctx.root, "add", "--", path)
    return left


def _has_markers(text: str) -> bool:
    return any(line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "======="
               for line in text.splitlines())


def _resolve_in_place(group: dict, paths: list, ctx: Context) -> list:
    """Hand the conflicted `paths` to a generator that reads through conflict
    markers; return the paths it left marked (or did not get to run on)."""
    if not _regenerate(group, ctx):
        _say(f"{group['name']}: a generator failed; {', '.join(paths)} restored "
             f"to their conflicted state")
        return _restore(ctx.root, paths)
    left = []
    for path in paths:
        if _has_markers(_read(ctx.root, path)):
            _say(f"{path}: still has conflict markers (its generator did not "
                 f"run, see above); left for you")
            left.append(path)
            continue
        _git(ctx.root, "add", "-A", "--", path)
    return left


def _restore(root: str, paths: list) -> list:
    for path in paths:
        # a path main deleted has no conflict to recreate; it stays deleted
        _git(root, "checkout", "-m", "--", path, check=False)
    return list(paths)


def _resolve_group(group: dict, paths: list, ctx: Context) -> list:
    """Resolve `paths` (all owned by `group`) and stage them; return the paths
    left in conflict."""
    if group["merge"] == "three-way":
        return _resolve_partial(group, paths, ctx)
    if group["merge"] == "in-place":
        return _resolve_in_place(group, paths, ctx)
    for path in paths:
        _take_theirs(ctx.root, path)
    if not _regenerate(group, ctx):
        _say(f"{group['name']}: a generator failed; {', '.join(paths)} restored "
             f"to their conflicted state")
        return _restore(ctx.root, paths)
    for path in paths:
        if os.path.exists(os.path.join(ctx.root, path)):
            _git(ctx.root, "add", "--", path)
    return []


def _changed(root: str) -> list:
    """Every path with an UNSTAGED change in the worktree (modified, added,
    untracked or deleted), conflicts aside.

    A change already staged and untouched since (`D ` after a transition's
    `git rm`, `M ` after `_take_theirs`) is left out: it has nothing more to
    stage, and `git add -A -- <path>` on a path deleted from both the index
    and the worktree fails with "pathspec did not match" (#1917's two-file
    layout, which main deleted inside the census generator's own directory)."""
    out = _git(root, "status", "--porcelain", "--untracked-files=all")
    paths = []
    for line in out.splitlines():
        status, path = line[:2], line[3:]
        if "U" in status or status in ("AA", "DD"):
            continue
        if status[1] == " ":
            continue
        paths.append(path.split(" -> ")[-1])
    return paths


def _stage_outputs(group: dict, ctx: Context, keep: list) -> None:
    """Stage whatever else the generator rewrote among the files it owns,
    except the paths `keep` names (restored conflicts)."""
    for path in _changed(ctx.root):
        if _matches(path, group["paths"]) and path not in keep:
            _git(ctx.root, "add", "-A", "--", path)


# ---------------------------------------------------------------- main

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true",
                    help="regenerate every generator, conflicted or not")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="regenerate only this generator (repeatable)")
    ap.add_argument("--bench", action="store_true",
                    help="also take main's side for conflicts under bench/results/")
    ap.add_argument("--provenance-generation", type=int, default=None,
                    metavar="N", help="the generation to declare this branch's "
                    "new census documents at (passed to corpus_provenance.py)")
    ap.add_argument("--allow-skip", action="store_true",
                    help="a generator skipped because its tool is missing does "
                         "not fail the run (the banner still prints)")
    ap.add_argument("--fast", action="store_true",
                    help="skip the slow steps (the cargo-built crate census)")
    ap.add_argument("--no-check", action="store_true",
                    help="regenerate but do not run the checks")
    ap.add_argument("--list", action="store_true",
                    help="print the generators and hand-maintained files, then exit")
    ap.add_argument("--registry", metavar="FILE",
                    help="a JSON {registry, hand, transitions, bench} replacing the built-in "
                         "tables (for tests and experiments)")
    return ap


def _tables(args):
    if not args.registry:
        return REGISTRY, HAND, TRANSITIONS, BENCH
    with open(args.registry, encoding="utf-8") as handle:
        data = json.load(handle)
    return (data.get("registry", []), data.get("hand", []),
            data.get("transitions", []), data.get("bench", []))


def _list(registry, hand, transitions, bench) -> int:
    for group in registry:
        print(f"{group['name']} ({group['merge']}): {', '.join(group['paths'])}")
        for step in group["write"]:
            print(f"    write: {' '.join(step['run'])}")
        for step in group["check"]:
            print(f"    check: {' '.join(step['run'])}")
    for entry in hand:
        print(f"hand-maintained: {', '.join(entry['paths'])}")
    for move in transitions:
        print(f"layout move: {', '.join(move['paths'])} -> {move['group']} "
              f"once main has {move['marker']}")
    print(f"left alone unless --bench: {', '.join(bench)}")
    return 0


def _report_hand(path: str, rule: str) -> None:
    _say(f"{path} is HAND-MAINTAINED and is left in conflict: {rule}")


def main(argv: list | None = None) -> int:
    args = _parser().parse_args(argv)
    registry, hand, transitions, bench = _tables(args)
    if args.list:
        return _list(registry, hand, transitions, bench)
    names = {g["name"] for g in registry}
    unknown = sorted(set(args.only) - names)
    if unknown:
        _say(f"unknown generator(s): {', '.join(unknown)} (known: {', '.join(sorted(names))})")
        return 2

    root = _toplevel()
    merging = _merging(root)
    conflicted = _unmerged(root) if merging else []
    if not merging and not (args.all or args.only):
        _say("no merge in progress; pass --all or --only NAME to regenerate "
             "anyway")
        return 2

    registry, hand, moving = _apply_transitions(registry, hand, transitions,
                                                root, merging)
    left: list = []
    by_group: dict = {}
    for path in conflicted:
        if path in moving:
            _cross_layout(root, path, moving[path])
            by_group.setdefault(moving[path], [])
            continue
        rule = _hand_rule(path, hand)
        if rule is not None:
            _report_hand(path, rule)
            left.append(path)
            continue
        if _matches(path, bench):
            if args.bench:
                _take_theirs(root, path)
                _say(f"{path}: took main's side (--bench)")
            else:
                _say(f"{path}: under bench/results/, left in conflict "
                     f"(pass --bench to take main's side)")
                left.append(path)
            continue
        group = _owner(path, registry)
        if group is None:
            gone = _stage_text(root, path, 3) is None
            _say(f"{path}: not a generated file; resolve it by hand"
                 + (" (main deleted it and this branch changed it)" if gone else ""))
            left.append(path)
            continue
        by_group.setdefault(group["name"], []).append(path)

    selected = _select(registry, args, by_group)
    failed: list = []
    with tempfile.TemporaryDirectory(prefix="regen-generated-") as tmp:
        ctx = Context(root, sys.executable, tmp, args)
        for group in selected:
            paths = by_group.get(group["name"], [])
            if not _group_runnable(group, ctx):
                left.extend(paths)
                continue
            kept: list = []
            if paths:
                kept = _resolve_group(group, paths, ctx)
                left.extend(kept)
            elif not _regenerate(group, ctx):
                failed.append(f"{group['name']} write")
            _stage_outputs(group, ctx, kept)
        if not args.no_check:
            for group in registry:
                if _check(group, ctx) is False:
                    failed.append(f"{group['name']} check")

    unchecked = [] if args.allow_skip else ctx.skipped
    _summary(left, failed, unchecked, merging)
    return 1 if (left or failed or unchecked) else 0


def _select(registry, args, by_group: dict) -> list:
    """The generators to run, in registry order: every one with a conflict
    (or named by --only, or all of them), plus every generator a selected one
    `feeds`, since its output is now stale. --only limits the first set, never
    the second."""
    asked = {g["name"] for g in registry
             if args.all or g["name"] in args.only
             or (not args.only and g["name"] in by_group)}
    wanted = set(asked)
    while True:
        fed = {name for g in registry if g["name"] in wanted
               for name in g.get("feeds", ())}
        if fed <= wanted:
            break
        wanted |= fed
    for name in sorted(wanted - asked):
        _say(f"{name}: reads what this run regenerates, so it reruns too")
    return [g for g in registry if g["name"] in wanted]


def _summary(left: list, failed: list, unchecked: list, merging: bool) -> None:
    if unchecked:
        _say("UNCHECKED, so this run fails: " + ", ".join(unchecked) + " did not "
             "run because a tool is missing (looked on PATH and in "
             + ", ".join(sorted({d for ds in TOOL_HOMES.values() for d in ds}))
             + "). Install it, or pass --allow-skip to accept the files as they "
             "are")
    if left:
        _say("still in conflict, for a human: " + ", ".join(sorted(set(left))))
    if failed:
        _say("FAILED: " + ", ".join(failed) + ". Rerun the named generator with "
             "--only NAME, or everything with --all, against this merged tree")
    if not left and not failed and not unchecked:
        _say("every generated conflict is resolved and every check passes"
             + ("; review and `git commit`" if merging else ""))


if __name__ == "__main__":
    sys.exit(main())
