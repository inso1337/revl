#!/usr/bin/env python3
"""Resolves docs/vision.md's claims about the tree against the tree.

`docs/vision.md` is the document the roadmap's own header cites as its
rationale (`**Why:** docs/vision.md`), and until this tool existed no tool read
it at all:

    $ git grep -niE 'vision\\.md' origin/main -- tools/
    (no output)

The other two halves of the same promise are gated. `docs/v2.0-roadmap.md` has
its markers resolved against git (`tools/check_roadmap_markers.py`) and its
citations resolved against the tree (`tools/check_roadmap_claims.py`), and the
source-derived blocks in six documents are byte-compared against a fresh
generation (`tools/docgen.py --check`). The vision half carried two
hand-written tables and nothing watched either one (issue #1204).

WHAT IS CHECKED. Four rules, in the shape `check_roadmap_claims.py` uses: a
claim is judged only where the sentence names the artifact that makes it
falsifiable.

  command  Every backticked COMMAND in the document still refers to something
           that exists. The document's own rule is "a claim in this project
           gets a command or it gets softened", so a command that names a
           moved directory, a renamed test file or a deleted make target is
           the exact decay this file is supposed to be immune to.

           `&&` chains are split and `cd` is tracked, so
           `cd backends/python && .venv/bin/pytest -q` is judged in
           `backends/python`. Per verb:

             cd D        D is a directory in the tree.
             pytest A B  each non-flag argument resolves (the file half of a
                         `::` node id); with no arguments, the working
                         directory has at least one `test_*.py` to collect.
             make T      the Makefile defines the target `T`.
             sh S        S resolves, relative to the working directory.
             python3 S   the first `.py` argument resolves.
             npm/npx     the working directory has a `package.json`, and the
                         tool `npx` runs (or the script `npm run` names) is in
                         it.

  link     Every relative markdown link target resolves: a file, or a
           directory. An `#anchor` is stripped and an absolute URL is skipped.

  path     Every backticked repo path resolves, exactly or by unique-or-not
           suffix, the way `check_roadmap_claims.py` resolves an abbreviated
           citation (`setup.sh` for `backends/python/setup.sh`). A path
           containing `*` is a glob and passes when it matches at least one
           tracked file, which is what makes `demo/bridge_*` falsifiable
           rather than decorative.

  tier     The six-tier table equals a fresh derivation from the conformance
           register. See the next section.

THE TIER TABLE IS GENERATED, NOT CHECKED. The table's per-tier column is a
`docgen` block (`vision-tiers`), rendered by `tools/docgen.py` from the per-tier
totals `tools/conformance.py --write-readme` generates into
`docs/conformance.md`. Generating beats checking: a hand-written table that
merely gets compared can still be edited into agreement with a stale register,
while a generated one is a pure function of its source. The `tier` rule here
re-renders the block and compares bytes, so the same drift is red in the
required `lint` job as well as in `docgen --check`, which runs in `frontend`
and is skipped on a docs-only pull request.

The chain is register -> `docs/conformance.md` -> `docs/vision.md`, and each
link is byte-gated: `conformance.py --check-readme` holds the middle document
to a fresh walk of the emitters, and this holds the vision block to the middle
document. Reading the middle document rather than re-running the emitters is
deliberate: the `lint` job installs no revl, and re-deriving a number that is
already gated would buy nothing but minutes.

WHAT THIS DOES NOT DO: IT DOES NOT RUN THE COMMANDS. Resolving
`pytest backends/go/test_emit_go.py -q` is a claim that the file is still
there, not a claim that it passes. Running them here was measured and refused.
The suites those seven commands name are the per-backend CI jobs
(`backend-python`, `backend-typescript`, `backend-rust`, `backend-java`,
`backend-wasm`, `backend-go`) plus the `formal` job; every one of them is a
required check, so running them a second time inside `lint` would re-buy an
answer the same pull request already has, at minutes per job, and would need
`lint` to provision cargo, a JDK, Go, node, wasmtime and elan to do it. A gate
that heavy gets narrowed, and a narrowed gate is the failure this one exists to
prevent. The honest split is: CI runs the commands, this resolves them.

Do not widen the reporting. A resolved command is not a passing command, a
resolved link is not a correct sentence, and a generated table that regenerates
to itself says the numbers agree with the register, not that the prose beside
them is true.

Usage:

    python3 tools/check_vision_claims.py             # report, exit 0
    python3 tools/check_vision_claims.py --check     # exit 1 on any finding
    python3 tools/check_vision_claims.py --self-test # prove the rules fire
    python3 tools/check_vision_claims.py --rule command --rule tier
    python3 tools/check_vision_claims.py --doc docs/vision.md --root .
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_roadmap_claims as claims  # noqa: E402
import docgen  # noqa: E402

DEFAULT_DOC = ROOT / "docs" / "vision.md"
RULES = ("command", "link", "path", "tier")

# The block `tools/docgen.py` owns in this document. Named here as well so the
# `tier` rule and the generator cannot drift apart on which block they mean.
TIER_BLOCK = "vision-tiers"

# Verbs a backticked span has to start with before it is read as a command.
# Everything else in backticks is prose, an identifier or a flag.
VERBS = ("cd", "pytest", "make", "sh", "bash", "python3", "python", "npm", "npx")

# A backticked repo path: at least one directory segment then a file with a
# known source extension, or the same with a trailing `*` glob.
PATH_RE = re.compile(
    r"`((?:[.A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]+\.(?:" + claims.SOURCE_EXT + r"))`"
)
GLOB_RE = re.compile(r"`((?:[.A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]*\*[A-Za-z0-9_.*-]*)`")

# A backticked BARE filename, resolved by suffix over the tracked files. The
# document abbreviates the way the roadmap does (`setup.sh` for
# `backends/python/setup.sh`), and the abbreviation is the citation. The stem
# is required, so `.rvl` naming an extension is not read as a file.
BARE_PATH_RE = re.compile(
    r"`([A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:" + claims.SOURCE_EXT + r"))`"
)

# A markdown link target. Only relative ones are judged.
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# A backticked span. Nested backticks do not occur in this document.
SPAN_RE = re.compile(r"`([^`\n]+)`")


# --------------------------------------------------------------------------
# The tree the claims are resolved against.
# --------------------------------------------------------------------------
def _is_dir(tree: "claims.Tree", rel: str) -> bool:
    rel = rel.rstrip("/")
    if not rel or rel == ".":
        return True
    prefix = rel + "/"
    return any(f.startswith(prefix) for f in tree.files)


def _exists(tree: "claims.Tree", rel: str) -> bool:
    return rel in set(tree.files) or _is_dir(tree, rel)


def _join(cwd: str, arg: str) -> str:
    """The repo-relative path an argument names from `cwd`, without touching
    the filesystem: the tree is the authority, not the checkout."""
    if arg.startswith("/"):
        return arg.lstrip("/")
    parts = [p for p in (cwd.split("/") if cwd else []) if p]
    for part in arg.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _glob_matches(tree: "claims.Tree", pattern: str) -> bool:
    return any(fnmatch(f, pattern) for f in tree.files)


def _make_targets(tree: "claims.Tree") -> set:
    text = tree.text("Makefile")
    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", text, re.M))


def _package_json(tree: "claims.Tree", cwd: str) -> Optional[str]:
    rel = _join(cwd, "package.json")
    return tree.text(rel) if rel in set(tree.files) else None


# --------------------------------------------------------------------------
# Collecting.
# --------------------------------------------------------------------------
def _claim(rule: str, key: str, source: str, index: int, text: str) -> "claims.Claim":
    return claims.Claim(rule, key, claims._line_of(source, index), text)


def collect_command_claims(source: str) -> List["claims.Claim"]:
    out: List["claims.Claim"] = []
    seen = set()
    for m in SPAN_RE.finditer(source):
        body = m.group(1).strip()
        words = body.split()
        # A one-word span is the NAME of a tool (`npx`, `make`), not a command
        # it could resolve. Judging it would red on prose that merely says
        # which tool runs something.
        if len(words) < 2 or Path(words[0]).name not in VERBS:
            continue
        if claims._is_historical(source, m.start()):
            continue
        if body in seen:
            continue
        seen.add(body)
        out.append(_claim("command", body, source, m.start(), m.group(0)))
    return out


def collect_link_claims(source: str) -> List["claims.Claim"]:
    out: List["claims.Claim"] = []
    seen = set()
    for m in LINK_RE.finditer(source):
        target = m.group(1)
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        if claims._is_historical(source, m.start()):
            continue
        target = target.split("#", 1)[0]
        if not target or target in seen:
            continue
        seen.add(target)
        out.append(_claim("link", target, source, m.start(), m.group(0)))
    return out


def collect_path_claims(source: str) -> List["claims.Claim"]:
    out: List["claims.Claim"] = []
    seen = set()
    for pattern in (PATH_RE, GLOB_RE, BARE_PATH_RE):
        for m in pattern.finditer(source):
            key = m.group(1)
            if "..." in key or key in seen:
                continue
            if claims._is_historical(source, m.start()):
                continue
            seen.add(key)
            out.append(_claim("path", key, source, m.start(), m.group(0)))
    return out


def collect_tier_claims(source: str) -> List["claims.Claim"]:
    """One claim: the generated block is the block a fresh render produces.

    A document with no such block is not judged, so the tool stays usable
    against another document.
    """
    marker = "<!-- docgen:%s begin -->" % TIER_BLOCK
    if marker not in source:
        return []
    return [_claim("tier", TIER_BLOCK, source, source.index(marker), marker)]


COLLECTORS = {
    "command": collect_command_claims,
    "link": collect_link_claims,
    "path": collect_path_claims,
    "tier": collect_tier_claims,
}


# --------------------------------------------------------------------------
# Judging.
# --------------------------------------------------------------------------
def _judge_simple(words: List[str], cwd: str, tree: "claims.Tree") -> Optional[str]:
    """One simple command out of an `&&` chain. Returns a verdict or None."""
    verb = Path(words[0]).name
    args = words[1:]
    positional = [a for a in args if not a.startswith("-")]

    if verb == "cd":
        if not positional:
            return None
        target = _join(cwd, positional[0])
        if not _is_dir(tree, target):
            return "changes into `%s`, which is not a directory in the tree" % target
        return None

    if verb == "pytest":
        if not positional:
            if not any(
                f.startswith(cwd + "/" if cwd else "")
                and Path(f).name.startswith("test_")
                and f.endswith(".py")
                for f in tree.files
            ):
                return ("runs pytest in `%s`, which holds no `test_*.py` to collect"
                        % (cwd or "."))
            return None
        missing = []
        for arg in positional:
            rel = _join(cwd, arg.split("::", 1)[0])
            if not _exists(tree, rel):
                missing.append(rel)
        if missing:
            return "names %s, which the tree does not have" % ", ".join(
                "`%s`" % m for m in missing
            )
        return None

    if verb == "make":
        targets = _make_targets(tree)
        missing = [t for t in positional if t not in targets]
        if missing:
            return "names the make target(s) %s, which the Makefile does not define" % (
                ", ".join("`%s`" % t for t in missing)
            )
        return None

    if verb in ("sh", "bash", "python3", "python"):
        for arg in positional:
            if verb in ("python3", "python") and not arg.endswith(".py"):
                continue
            rel = _join(cwd, arg)
            if not _exists(tree, rel):
                return "runs `%s`, which is not a file in the tree" % rel
            return None
        return None

    if verb in ("npm", "npx"):
        package = _package_json(tree, cwd)
        if package is None:
            return "runs %s in `%s`, which has no `package.json`" % (verb, cwd or ".")
        if verb == "npx" and positional:
            tool = positional[0]
            if ('"%s"' % tool) not in package:
                return "runs `npx %s` in `%s`, whose `package.json` does not name it" % (
                    tool, cwd or ".",
                )
        if verb == "npm" and len(positional) >= 2 and positional[0] == "run":
            script = positional[1]
            if ('"%s"' % script) not in package:
                return "runs `npm run %s` in `%s`, whose `package.json` has no such script" % (
                    script, cwd or ".",
                )
        return None

    return None


def judge_command(claim: "claims.Claim", tree: "claims.Tree") -> Optional[str]:
    cwd = ""
    for part in re.split(r"&&|\|\||;", claim.key):
        part = part.strip()
        if not part:
            continue
        try:
            words = shlex.split(part)
        except ValueError:
            return None
        if not words:
            continue
        verdict = _judge_simple(words, cwd, tree)
        if verdict is not None:
            return verdict
        if Path(words[0]).name == "cd":
            rest = [w for w in words[1:] if not w.startswith("-")]
            if rest:
                cwd = _join(cwd, rest[0])
    return None


def judge_link(claim: "claims.Claim", tree: "claims.Tree") -> Optional[str]:
    rel = _join(claim.doc_dir, claim.key)  # type: ignore[attr-defined]
    if _exists(tree, rel):
        return None
    return "links to `%s`, which is not a file or directory in the tree" % rel


def judge_path(claim: "claims.Claim", tree: "claims.Tree") -> Optional[str]:
    if "*" in claim.key:
        if _glob_matches(tree, claim.key):
            return None
        return "cites `%s`, which matches no tracked file" % claim.key
    candidates = tree.resolve(claim.key)
    if candidates is None or candidates:
        return None
    if _is_dir(tree, claim.key):
        return None
    return "cites `%s`, which is not a file in the tree" % claim.key


def judge_tier(claim: "claims.Claim", tree: "claims.Tree") -> Optional[str]:
    text = (tree.root / claim.doc_rel).read_text(  # type: ignore[attr-defined]
        encoding="utf-8"
    )
    committed = docgen.extract(text, TIER_BLOCK)
    try:
        fresh = docgen.block_vision_tiers(committed, root=tree.root)
    except docgen.VisionTierError as exc:
        return str(exc)
    if committed.strip("\n") == fresh.strip("\n"):
        return None
    return (
        "the `%s` block disagrees with the per-tier totals in "
        "`docs/conformance.md`; regenerate it with `make docs-gen`" % TIER_BLOCK
    )


JUDGES = {
    "command": judge_command,
    "link": judge_link,
    "path": judge_path,
    "tier": judge_tier,
}


# --------------------------------------------------------------------------
# The run.
# --------------------------------------------------------------------------
def run(
    source: str,
    tree: "claims.Tree",
    rules: Iterable[str] = RULES,
    doc_rel: str = "docs/vision.md",
) -> Tuple[Dict[str, int], List[Tuple["claims.Claim", str]]]:
    """Return (claims collected per rule, findings)."""
    doc_dir = doc_rel.rsplit("/", 1)[0] if "/" in doc_rel else ""
    denominator: Dict[str, int] = {}
    findings: List[Tuple["claims.Claim", str]] = []
    for rule in rules:
        collected = COLLECTORS[rule](source)
        denominator[rule] = len(collected)
        for claim in collected:
            claim.doc_rel = doc_rel  # type: ignore[attr-defined]
            claim.doc_dir = doc_dir  # type: ignore[attr-defined]
            verdict = JUDGES[rule](claim, tree)
            if verdict is not None:
                findings.append((claim, verdict))
    return denominator, findings


# --------------------------------------------------------------------------
# The self-test: the gate, seen to fire.
# --------------------------------------------------------------------------
_SELF_TEST_FILES = {
    "Makefile": "formal:\n\tsh formal/scripts/run_gate.sh\n",
    "formal/scripts/run_gate.sh": "echo gate\n",
    "backends/go/test_emit_go.py": "def test_go():\n    pass\n",
    "backends/python/tests/test_semantics.py": "def test_py():\n    pass\n",
    "backends/typescript/package.json": '{"devDependencies": {"vitest": "4.1.11"}}\n',
    "demo/bridge_pypy.py": "print(1)\n",
    "docs/conformance.md": "# c\n",
}

_SELF_TEST_DOC = """# fixture

See [conformance.md](conformance.md) and `demo/bridge_*`.

| tier | gate |
|---|---|
| py | `cd backends/python && .venv/bin/pytest -q` |
| ts | `cd backends/typescript && npm ci && npx vitest run` |
| go | `pytest backends/go/test_emit_go.py -q` |
| formal | `make formal` (`sh formal/scripts/run_gate.sh`) |
"""

# One breakage per rule, each the shape a rename or a move actually takes.
_SELF_TEST_BREAKS = {
    "command": ("`pytest backends/go/test_emit_go.py -q`",
                "`pytest backends/go/test_emit_golang.py -q`"),
    "link": ("[conformance.md](conformance.md)",
             "[conformance.md](conformance-matrix.md)"),
    "path": ("`demo/bridge_*`", "`demo/span_*`"),
}


def self_test(root: Path) -> int:
    """Plant one breakage per rule in a synthetic tree and require each one to
    be reported, then require the unbroken fixture to be silent.

    A gate nobody has watched fail is not known to work, and a gate that reads
    prose with regular expressions is the easiest kind to write so that it can
    never fire. This runs in CI ahead of the real check for that reason.
    """
    import tempfile

    failures: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for rel, body in _SELF_TEST_FILES.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        tree = claims.Tree(base, sorted(_SELF_TEST_FILES))

        rules = sorted(_SELF_TEST_BREAKS)
        _, clean = run(_SELF_TEST_DOC, tree, rules, doc_rel="docs/fixture.md")
        if clean:
            failures.append(
                "the UNBROKEN fixture reported %d finding(s): %s"
                % (len(clean), "; ".join(v for _, v in clean))
            )
        for rule, (before, after) in _SELF_TEST_BREAKS.items():
            assert before in _SELF_TEST_DOC, rule
            broken = _SELF_TEST_DOC.replace(before, after)
            _, found = run(broken, tree, [rule], doc_rel="docs/fixture.md")
            if not found:
                failures.append(
                    "[%s] renaming %s to %s was NOT reported" % (rule, before, after)
                )
            print("  [%s] %s -> %s: %s"
                  % (rule, before, after,
                     found[0][1] if found else "NOT REPORTED"))

    if failures:
        print("\ncheck_vision_claims: self-test FAILED.", file=sys.stderr)
        for f in failures:
            print("  %s" % f, file=sys.stderr)
        return 1
    print("check_vision_claims: self-test ok, %d rule(s) seen to fire and to pass."
          % len(_SELF_TEST_BREAKS))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--rule", action="append", choices=RULES, dest="rules")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 on any finding (the CI mode)")
    parser.add_argument("--self-test", action="store_true",
                        help="plant a breakage per rule and require each to be "
                             "reported; run this ahead of --check in CI")
    parser.add_argument("--quiet", action="store_true", help="counts only")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test(args.root)

    rules = args.rules or list(RULES)
    tree = claims.Tree.from_git(args.root)
    doc_rel = str(args.doc.resolve().relative_to(args.root.resolve()))
    source = args.doc.read_text(encoding="utf-8")
    denominator, findings = run(source, tree, rules, doc_rel)

    print("%s: %d claims checked over %d rules"
          % (doc_rel, sum(denominator.values()), len(rules)))
    for rule in rules:
        stale = sum(1 for c, _ in findings if c.rule == rule)
        print("  %-7s %3d claims, %d stale" % (rule, denominator[rule], stale))

    if not args.quiet:
        for claim, verdict in findings:
            print("\n%s:%d  [%s]\n  %s\n  %s"
                  % (doc_rel, claim.line, claim.rule, claim.text.strip(), verdict))

    print("\nThis gate RESOLVED the commands. It did not run them: the suites "
          "they name are the per-backend CI jobs, which are required checks.")
    if findings and not args.check:
        print("Advisory run: %d finding(s), exit 0. Use --check for the CI mode."
              % len(findings))
    return 1 if (args.check and findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
