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
* Hand-maintained ratchets are never resolved. A conflict in one is printed
  with the rule for resolving it by hand. The coverage ledger is the case
  that matters: its counts are per function and are moved one at a time from
  `tools/selfhost_line_coverage.py --check`, never regenerated wholesale.
* `bench/results/` is left alone unless `--bench` is passed.
* A generator whose tool is missing (`lake`, `cargo`) is skipped LOUDLY, and
  the files it owns are left as they are.

Usage, from inside the worktree, after the merge stops on its conflicts:

    git merge origin/main             # stops with conflicts
    python3 tools/regen_generated.py  # resolve + regenerate + check
    git commit                        # what the tool did is staged

    python3 tools/regen_generated.py --all          # regenerate everything
    python3 tools/regen_generated.py --only census  # one generator
    python3 tools/regen_generated.py --list         # what it knows

Exit status: 0 when every conflict it owns is resolved and every check
passes; 1 when a check fails or a conflict is left for a human; 2 on usage.
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

# Each generator: the files it owns (globs, repo-relative), whether they are
# WHOLLY generated, the commands that regenerate them in order, and the
# commands that check them. `{python}` is this interpreter, `{tmp}` a scratch
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
        "whole": True,
        "write": [{"run": ["{python}", "tools/build_gate_crate.py"]},
                  {"run": ["{python}", "tools/build_gate_wasm.py"]}],
        "check": [{"run": ["{python}", "tools/build_gate_crate.py", "--check"]},
                  {"run": ["{python}", "tools/build_gate_wasm.py", "--check"]}],
    },
    {
        "name": "grammar",
        "paths": ["grammar/revl.lark", "grammar/revl.gbnf", "grammar/revl.ebnf"],
        "whole": True,
        "write": [{"run": ["{python}", "-m", "revl", "grammar", "--write"]}],
        "check": [{"run": ["{python}", "-m", "revl", "grammar", "--check"]}],
    },
    {
        "name": "provenance",
        "paths": ["tests/fixtures/corpus_provenance.json"],
        # main's declarations plus this branch's new documents, re-declared at
        # the generation the author names: there is deliberately no default.
        "whole": False,
        "option": "provenance_generation",
        "write": [{"run": ["{python}", "tools/corpus_provenance.py", "--write",
                           "--generation", "{generation}"]}],
        "check": [{"run": ["{python}", "tools/corpus_provenance.py", "--check"]}],
    },
    {
        "name": "census",
        "paths": ["docs/census-artifact.json", "docs/census-artifact.md",
                  "tests/fixtures/census_crate_reproduction.json"],
        "whole": True,
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
        "whole": False,
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
        "whole": False,
        "write": [{"run": ["{python}", "tools/conformance.py", "--write-readme"]}],
        "check": [{"run": ["{python}", "tools/conformance.py", "--check-readme"]}],
    },
    {
        "name": "docgen",
        "paths": ["docs/DOC-STATUS.md", "docs/mcp-reference.md",
                  "docs/guide-ai-agents.md", "docs/authoring-for-agents.md",
                  "docs/commands-reference.md", "docs/rejections.md",
                  "DESIGN.md", "docs/guide-humans.md", "docs/vision.md",
                  "docs/selfhost-compile.md", "docs/selfhost-findings.md"],
        "whole": False,
        "write": [{"run": ["{python}", "tools/docgen.py", "--write"]}],
        "check": [{"run": ["{python}", "tools/docgen.py", "--check"]}],
    },
]

# Hand-maintained files that look generated and are not. Never resolved here.
HAND = [
    {"paths": ["tests/fixtures/selfhost_uncovered_lines.json"],
     "rule": "the coverage ledger is hand-maintained PER FUNCTION. Take main's "
             "side (`git checkout --theirs -- <file>`), run "
             "`python3 tools/selfhost_line_coverage.py --check`, and move only "
             "the counts it names, lowering `_budget` by exactly what you close. "
             "Never `--write` it wholesale, and keep any reason key this branch "
             "edited."},
    {"paths": ["tests/fixtures/selfhost_blind_spots.json",
               "tests/fixtures/oracle_construct_reach_ledger.json"],
     "rule": "a hand-recorded construct ratchet. Take main's side, then re-add "
             "only the entries this branch recorded on purpose, and confirm "
             "with tests/test_selfhost_coverage.py and "
             "tests/test_oracle_construct_reach.py."},
]

BENCH = ["bench/results/*"]


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


# ------------------------------------------------------------- matching

def _matches(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, glob) for glob in globs)


def _owner(path: str, registry) -> dict | None:
    return next((g for g in registry if _matches(path, g["paths"])), None)


def _hand_rule(path: str, hand) -> str | None:
    return next((h["rule"] for h in hand if _matches(path, h["paths"])), None)


# -------------------------------------------------------------- running

class Context:
    def __init__(self, root: str, python: str, tmp: str, args) -> None:
        self.root, self.python, self.tmp, self.args = root, python, tmp, args

    def env(self) -> dict:
        """The generators' environment: THIS tree's `src/` first on the path,
        so `python -m revl` and every tool import the revl being regenerated,
        never another checkout's editable install."""
        env = dict(os.environ)
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
    return [tool for tool in step.get("requires") or ()
            if shutil.which(tool) is None]


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
        _loud(f"{label}: {' '.join(missing)} is not installed, so `{shown}` "
              f"did not run")
        return None
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


def _group_runnable(group: dict, ctx: Context) -> bool:
    missing = _missing(group)
    if missing:
        _loud(f"{group['name']}: {' '.join(missing)} is not installed, so "
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
    if _missing(group):
        _loud(f"{group['name']} check: {' '.join(_missing(group))} is not "
              f"installed")
        return None
    results = [_run_step(step, ctx, f"{group['name']} check")
               for step in group["check"]]
    if any(r is False for r in results):
        return False
    return None if all(r is None for r in results) else True


# --------------------------------------------------------- resolution

def _take_theirs(root: str, path: str) -> None:
    _git(root, "checkout", "--theirs", "--", path)


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


def _restore(root: str, paths: list) -> list:
    for path in paths:
        _git(root, "checkout", "-m", "--", path)
    return list(paths)


def _resolve_group(group: dict, paths: list, ctx: Context) -> list:
    """Resolve `paths` (all owned by `group`) and stage them; return the paths
    left in conflict."""
    if not group["whole"]:
        return _resolve_partial(group, paths, ctx)
    for path in paths:
        _take_theirs(ctx.root, path)
    if not _regenerate(group, ctx):
        _say(f"{group['name']}: a generator failed; {', '.join(paths)} restored "
             f"to their conflicted state")
        return _restore(ctx.root, paths)
    for path in paths:
        _git(ctx.root, "add", "--", path)
    return []


def _changed(root: str) -> list:
    """Every modified, added or deleted path in the worktree, conflicts aside."""
    out = _git(root, "status", "--porcelain", "--untracked-files=all")
    paths = []
    for line in out.splitlines():
        status, path = line[:2], line[3:]
        if "U" in status or status in ("AA", "DD"):
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
    ap.add_argument("--fast", action="store_true",
                    help="skip the slow steps (the cargo-built crate census)")
    ap.add_argument("--no-check", action="store_true",
                    help="regenerate but do not run the checks")
    ap.add_argument("--list", action="store_true",
                    help="print the generators and hand-maintained files, then exit")
    ap.add_argument("--registry", metavar="FILE",
                    help="a JSON {registry, hand, bench} replacing the built-in "
                         "tables (for tests and experiments)")
    return ap


def _tables(args):
    if not args.registry:
        return REGISTRY, HAND, BENCH
    with open(args.registry, encoding="utf-8") as handle:
        data = json.load(handle)
    return data.get("registry", []), data.get("hand", []), data.get("bench", [])


def _list(registry, hand, bench) -> int:
    for group in registry:
        kind = "whole" if group["whole"] else "partly generated"
        print(f"{group['name']} ({kind}): {', '.join(group['paths'])}")
        for step in group["write"]:
            print(f"    write: {' '.join(step['run'])}")
        for step in group["check"]:
            print(f"    check: {' '.join(step['run'])}")
    for entry in hand:
        print(f"hand-maintained: {', '.join(entry['paths'])}")
    print(f"left alone unless --bench: {', '.join(bench)}")
    return 0


def _report_hand(path: str, rule: str) -> None:
    _say(f"{path} is HAND-MAINTAINED and is left in conflict: {rule}")


def main(argv: list | None = None) -> int:
    args = _parser().parse_args(argv)
    registry, hand, bench = _tables(args)
    if args.list:
        return _list(registry, hand, bench)
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

    left: list = []
    by_group: dict = {}
    for path in conflicted:
        rule = _hand_rule(path, hand)
        if rule is not None:
            _report_hand(path, rule)
            left.append(path)
            continue
        if _matches(path, bench):
            if args.bench:
                _take_theirs(root, path)
                _git(root, "add", "--", path)
                _say(f"{path}: took main's side (--bench)")
            else:
                _say(f"{path}: under bench/results/, left in conflict "
                     f"(pass --bench to take main's side)")
                left.append(path)
            continue
        group = _owner(path, registry)
        if group is None:
            _say(f"{path}: not a generated file; resolve it by hand")
            left.append(path)
            continue
        by_group.setdefault(group["name"], []).append(path)

    selected = [g for g in registry
                if (args.all or g["name"] in args.only or g["name"] in by_group)
                and (not args.only or g["name"] in args.only)]
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

    _summary(left, failed, merging)
    return 1 if (left or failed) else 0


def _summary(left: list, failed: list, merging: bool) -> None:
    if left:
        _say("still in conflict, for a human: " + ", ".join(sorted(set(left))))
    if failed:
        _say("FAILED: " + ", ".join(failed) + ". Rerun the named generator with "
             "--only NAME, or everything with --all, against this merged tree")
    if not left and not failed:
        _say("every generated conflict is resolved and every check passes"
             + ("; review and `git commit`" if merging else ""))


if __name__ == "__main__":
    sys.exit(main())
