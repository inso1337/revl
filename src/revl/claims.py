"""Behaviour claims tied to a witness (issue #2846).

A comment or a doc sentence that states what the code DOES -- "this path reads
no bytes after the first digest", "state crosses only through the handoff" --
is a claim, and nothing ties it to the artifact that makes it true. The sentence
then outlives the code silently: the parked-sidecar comments claimed the path
read no bytes after the code started digesting, and a P24 sentence claimed state
crossed one way after the code allowed two, and only a reviewer caught either.
A stale comment costs a review round; a stale doc sentence costs a reader.

This module gives such a claim a name and a witness. An annotation in the
comment (or doc) that states the claim:

    // revl:claim <id>              a revl line comment
    <!-- revl:claim <id> -->        a Markdown HTML comment

names an entry in the repository's `claims.toml`:

    [claims]
    parked-sidecar-reads-no-bytes = "test:tests/test_sidecar.py::test_no_read"

Three witness kinds:

    test:<path>              every test in that file
    test:<path>::<test>      one pytest function, or a parametrized node
                             (`...::test_x[param]`, matched by function)
    docgen:<block>           a generated block in tools/docgen.py's BLOCKS
                             (`guarantees`, `vision-tiers`, ...)
    claim:<id>               another registered claim

The id exists, the witness exists, and -- for an annotation in a `.rvl` file --
the witness is selected by `tools/affected_tests.py` when the annotated file
changes, so the sentence cannot be edited into a state the test does not run.
The last check needs the repository's selector, which this module does not own;
`tools/check_claims.py` supplies it, and the compiler-side parsing and named
findings stay here.

WHAT THIS CANNOT KNOW. A witness that runs and passes does not mean the
sentence states what the code does: a test can assert a weaker property than
the comment claims. This narrows the window in which a dead claim goes
unnoticed; it does not replace review of the sentence itself. A witness node's
parametrization is resolved to its function, not to the parameter, so renaming
a parameter while keeping the function is not a finding.

Claims are OPTIONAL. A comment without `revl:claim` is unaffected, so adoption
is incremental; the generated-block contract in `tools/docgen.py` is the model
(one source of truth, a scanner, a named failure, no silent drift).
"""
from __future__ import annotations

import ast
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: The witness prefixes a registry entry may use.
WITNESS_KINDS = ("test", "docgen", "claim")

#: A claim id is a slug: it appears in a comment and as a TOML key.
_ID = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_CLAIM = re.compile(rf"revl:claim[ \t]+({_ID})")
_MENTION = re.compile(r"revl:claim\b")
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_FENCE = re.compile(r"(`{3,}|~{3,})")

#: Directories that hold no tracked prose or revl source, or hold a vendored
#: copy of it. A claim inside one is not the repository's claim.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".cordis-py", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".venv", "venv", "node_modules", "target", "dist", "build",
    "__pycache__", "site-packages",
})

#: Claim annotations are recognised in these suffixes only.
_SUFFIXES = (".rvl", ".md")


class RegistryError(ValueError):
    """`claims.toml` is missing its `[claims]` table or holds a bad witness."""


@dataclass(frozen=True)
class ClaimSite:
    """One `revl:claim` annotation: where it is and the id it names."""

    path: str                  # repo-relative, posix
    line: int                  # one-based
    id: str | None             # None when the annotation carries no valid id


@dataclass(frozen=True)
class Witness:
    """What makes a claim's sentence true, decoded from the registry value."""

    kind: str
    target: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.target}"


@dataclass(frozen=True)
class Finding:
    """One named failure. `code` is stable; `where` is `path:line` or a file."""

    code: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.where}: {self.detail}"


# --------------------------------------------------------------------------- #
# Parsing annotations.                                                        #
# --------------------------------------------------------------------------- #
def _rvl_comment(line: str, state: str) -> tuple[str | None, str]:
    """The line comment on `line`, if one starts outside a string, and the
    string state the next line inherits.

    The lexer drops comments, so this walks the raw line. revl has a
    double-quoted escaped string, a single-quoted one (item 382, only `\\'`
    and `\\\\` escape), a backtick raw template and a triple-quoted verbatim
    string; a `//` inside any of them is not a comment. A `"` or `'` string
    cannot span a line, so `parse_rvl_claims` resets that state at the line
    break; only a template and a triple-quoted string carry over. `@host`
    bodies are host language, so a `//` in a host body is still read as a
    comment: the annotation is comment text wherever it sits, and a host body
    cannot produce a false unknown id without someone writing `revl:claim`
    there.
    """
    i, n = 0, len(line)
    while i < n:
        if state == "triple":
            close = line.find('"""', i)
            if close < 0:
                return None, state
            i, state = close + 3, "code"
            continue
        if state == "raw":
            close = line.find("`", i)
            if close < 0:
                return None, state
            i, state = close + 1, "code"
            continue
        if state in ("quote", "squote"):
            quote = '"' if state == "quote" else "'"
            if line[i] == "\\":
                i += 2
                continue
            if line[i] == quote:
                state = "code"
            i += 1
            continue
        if line.startswith('"""', i):
            i, state = i + 3, "triple"
            continue
        char = line[i]
        if char == '"':
            i, state = i + 1, "quote"
            continue
        if char == "'":
            i, state = i + 1, "squote"
            continue
        if char == "`":
            i, state = i + 1, "raw"
            continue
        if line.startswith("//", i):
            return line[i + 2:], state
        i += 1
    return None, state


def parse_rvl_claims(path: str, text: str) -> list[ClaimSite]:
    """Every claim annotation in the line comments of one `.rvl` source."""
    sites: list[ClaimSite] = []
    state = "code"
    for number, line in enumerate(text.splitlines(), 1):
        comment, state = _rvl_comment(line, state)
        # A `"`/`'` string is single-line, so an unmatched one (a host-body
        # apostrophe, say) must not swallow every later line's comment.
        if state in ("quote", "squote"):
            state = "code"
        if comment is None:
            continue
        match = _CLAIM.search(comment)
        if match is not None:
            sites.append(ClaimSite(path, number, match.group(1)))
        elif _MENTION.search(comment):
            sites.append(ClaimSite(path, number, None))
    return sites


def _mask_fenced_code(text: str) -> str:
    """`text` with the inside of every fenced code block blanked to spaces.

    A fenced block is documentation OF the syntax, not an annotation; keeping
    the character offsets means line numbers still point at the real document.
    """
    chars = list(text)
    fence: str | None = None
    start = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if fence is None:
            match = _FENCE.match(stripped)
            if match is not None:
                fence = match.group(1)
        elif stripped.startswith(fence):
            fence = None
        if fence is not None:
            for index in range(start, start + len(line)):
                if chars[index] not in "\r\n":
                    chars[index] = " "
        start += len(line)
    return "".join(chars)


def parse_markdown_claims(path: str, text: str) -> list[ClaimSite]:
    """Every claim annotation in the HTML comments of one Markdown document.

    Fenced code blocks are skipped, so documenting the syntax does not create a
    claim. Only an HTML comment counts; prose that mentions `revl:claim` is
    ordinary prose.
    """
    masked = _mask_fenced_code(text)
    sites: list[ClaimSite] = []
    for comment in _HTML_COMMENT.finditer(masked):
        body = comment.group(1)
        line = masked.count("\n", 0, comment.start()) + 1
        match = _CLAIM.search(body)
        if match is not None:
            sites.append(ClaimSite(path, line, match.group(1)))
        elif _MENTION.search(body):
            sites.append(ClaimSite(path, line, None))
    return sites


def parse_claims(path: str, text: str) -> list[ClaimSite]:
    """The claim annotations in one file, by suffix."""
    if path.endswith(".rvl"):
        return parse_rvl_claims(path, text)
    if path.endswith(".md"):
        return parse_markdown_claims(path, text)
    return []


def discover(root: str | Path) -> list[ClaimSite]:
    """Every claim annotation in the `.rvl` and `.md` files under `root`.

    The walk is deterministic and skips VCS, cache, build and vendored trees;
    a duplicate annotation is kept once per site because each one must resolve.
    """
    root = Path(root)
    sites: list[ClaimSite] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if not name.endswith(_SUFFIXES):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sites.extend(parse_claims(rel, text))
    return sites


# --------------------------------------------------------------------------- #
# The registry.                                                               #
# --------------------------------------------------------------------------- #
def parse_witness(value: str) -> Witness:
    """Decode one `claims.toml` value, refusing a shape the check cannot judge."""
    kind, separator, target = value.partition(":")
    kind, target = kind.strip(), target.strip()
    if not separator or kind not in WITNESS_KINDS or not target:
        kinds = ", ".join(f"`{k}:`" for k in WITNESS_KINDS)
        raise RegistryError(
            f"{value!r} is not a witness; write one of {kinds} followed by a target")
    return Witness(kind, target)


def load_registry(path: str | Path) -> dict[str, Witness]:
    """The claim id to witness map in one `claims.toml`."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(f"cannot read {path.name}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path.name} is not valid TOML: {exc}") from None
    table = data.get("claims")
    if not isinstance(table, dict):
        raise RegistryError(f"{path.name} has no [claims] table")
    registry: dict[str, Witness] = {}
    for claim_id, value in table.items():
        if not isinstance(value, str):
            raise RegistryError(
                f"claims.{claim_id} must be a witness string, not {type(value).__name__}")
        registry[claim_id] = parse_witness(value)
    return registry


def _is_test_function(node: ast.stmt) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
        and node.name.startswith("test")


def _has_a_test(tree: ast.Module) -> bool:
    """True when the module defines a top-level `test_*` or a `Test*` class
    with one: a witness file with no test left is not a witness."""
    for item in tree.body:
        if _is_test_function(item):
            return True
        if isinstance(item, ast.ClassDef) and item.name.startswith("Test"):
            if any(_is_test_function(member) for member in item.body):
                return True
    return False


def _node_resolves(tree: ast.Module, node: str) -> bool:
    """`test_x`, `test_x[param]` or `TestK::test_y`: the function (and class)
    the node names exists as a test."""
    parts = node.split("::")
    name = parts[-1].split("[", 1)[0]
    if not name.startswith("test"):
        return False
    if len(parts) == 1:
        return any(_is_test_function(item) and item.name == name
                   for item in tree.body)
    if len(parts) == 2:
        class_name, method = parts[0], name
        return any(
            isinstance(item, ast.ClassDef) and item.name == class_name
            and any(_is_test_function(member) and member.name == method
                    for member in item.body)
            for item in tree.body)
    return False


# --------------------------------------------------------------------------- #
# Checking.                                                                   #
# --------------------------------------------------------------------------- #
def _resolve(witness: Witness, registry: dict[str, Witness], root: Path,
             docgen_blocks: frozenset[str], seen: frozenset[str]) -> str | None:
    """Why `witness` does not resolve, or None when it does.

    A chain through `claim:<id>` is followed; a cycle is reported rather than
    walked forever.
    """
    if witness.kind == "docgen":
        if witness.target in docgen_blocks:
            return None
        return (f"generated block {witness.target!r} is not one of "
                "tools/docgen.py's BLOCKS")
    if witness.kind == "claim":
        if witness.target not in registry:
            return f"claim {witness.target!r} is not registered in claims.toml"
        if witness.target in seen:
            return f"claim chain cycles through {witness.target!r}"
        return _resolve(registry[witness.target], registry, root,
                        docgen_blocks, seen | {witness.target})
    file, separator, node = witness.target.partition("::")
    if not file:
        return "the witness names no test file"
    path = root / file
    if not path.is_file():
        return f"test file {file!r} does not exist"
    if path.suffix != ".py":
        return f"{file!r} is not a Python test file"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return f"test file {file!r} cannot be parsed"
    if not separator:
        if _has_a_test(tree):
            return None
        return f"test file {file!r} defines no test function"
    if not _node_resolves(tree, node):
        return f"test {node!r} is not a test function in {file}"
    return None


def _terminal(witness: Witness, registry: dict[str, Witness],
              seen: frozenset[str] = frozenset()) -> Witness | None:
    """The witness at the end of a `claim:` chain, or None for a cycle or an
    unregistered link (both are already reported by `_resolve`)."""
    if witness.kind != "claim":
        return witness
    if witness.target in seen or witness.target not in registry:
        return None
    return _terminal(registry[witness.target], registry, seen | {witness.target})


def _bare_node(node: str) -> str:
    return node.split("[", 1)[0]


def witness_selected(target: str, selected: set[str] | frozenset[str]) -> bool:
    """True when an affected-selection result runs the witness.

    A selected file runs every test in it, a selected node without a parameter
    runs every parameter of that function, and a selected directory prefix runs
    everything below it (the selector's FULL result is passed as None, not as a
    set, so it never reaches here).
    """
    file = target.split("::", 1)[0]
    if file in selected or target in selected:
        return True
    if _bare_node(target) in selected:
        return True
    for chosen in selected:
        if chosen.endswith("/") and (file == chosen or file.startswith(chosen)):
            return True
    return False


def check(sites: list[ClaimSite], registry: dict[str, Witness], root: str | Path,
          *, docgen_blocks: frozenset[str] = frozenset(),
          selected=None) -> list[Finding]:
    """The named findings for every annotation in `sites`.

    `docgen_blocks` is the roster `tools/docgen.py` generates. `selected` is
    called with a repo-relative `.rvl` path and returns its affected-selection
    result (a set of pytest files/nodes, or None for FULL); omit it to judge
    only ids and witnesses, which is what a caller without the repository
    selector can do.
    """
    root = Path(root)
    findings: list[Finding] = []
    for site in sorted(sites, key=lambda s: (s.path, s.line)):
        where = f"{site.path}:{site.line}"
        if site.id is None:
            findings.append(Finding(
                "claim-annotation-malformed", where,
                "`revl:claim` is not followed by a claim id"))
            continue
        witness = registry.get(site.id)
        if witness is None:
            findings.append(Finding(
                "claim-unknown-id", where,
                f"`revl:claim {site.id}` names no entry in claims.toml"))
            continue
        reason = _resolve(witness, registry, root, docgen_blocks, frozenset())
        if reason is not None:
            findings.append(Finding(
                "claim-witness-missing", where,
                f"`{site.id}` witness `{witness}`: {reason}"))
            continue
        if selected is not None and site.path.endswith(".rvl"):
            terminal = _terminal(witness, registry)
            if terminal is not None and terminal.kind == "test":
                chosen = selected(site.path)
                if chosen is not None and not witness_selected(terminal.target, chosen):
                    findings.append(Finding(
                        "claim-witness-not-selected", where,
                        f"`{site.id}` witness `{witness}` resolves to "
                        f"`{terminal.target}`, which is not selected when "
                        f"{site.path} changes; extend tools/affected_tests.py "
                        "or register a witness that is"))
    return findings


def render(findings: list[Finding]) -> str:
    """The findings, one per line, for a CLI or a test."""
    return "\n".join(str(finding) for finding in findings)
