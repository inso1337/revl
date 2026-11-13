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
    test:<path>::<test>      one pytest function, or one parameter of it
                             (`...::test_x[param]`, the real pytest id)
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
unnoticed; it does not replace review of the sentence itself.

The witness is judged by reading the test file, not by collecting it (the lint
job that runs the gate has no pytest): the file must be one pytest collects
(`test_*.py` or `*_test.py`, the defaults this repository keeps), every
decorator on the test and its class and every module or class `pytestmark`
must be a mark spelled literally `pytest.mark.<name>` (any other decorator or
spelling, an alias such as `import pytest as pt` included, is refused as
unreadable: the check fails closed rather than follow it), the test must not
be unconditionally skipped (`pytest.mark.skip`, `pytest.mark.xfail(run=False)`,
a module-level `pytest.skip(...)`), and a `[param]` must be one of the ids the
function's own `@pytest.mark.parametrize` produces. Those ids are read from the
call when it is a literal (or a module-level literal, `sorted`/`list` of one,
its `.items()`/`.keys()`), with `ids=` or pytest's plain-text default ids; any
other shape (stacked or class-level parametrizations, parametrized fixtures,
`pytest_generate_tests`, an id pytest would escape) is refused, and the fix is
to name the function without the parameter. A conditional `skipif` is NOT
decided: its condition is evaluated when pytest runs, so a witness skipped by
one on every runner still passes this check. Not detected either: a
`pytest.skip()` call inside the test body, a skip raised by a fixture
(`usefixtures` or autouse), and a conftest `pytest_collection_modifyitems` that
adds a skip. A witness class with a base other than `object` or
`unittest.TestCase` is refused, since a base can carry marks; any `pytestmark`
reference that is not a plain module or class assignment, and a mark passed
`**` keywords, are refused as unreadable.

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


_BACKTICKS = re.compile(r"`+")
#: Where an inline code span must have closed: a blank line, or a line that
#: opens a block of its own (an HTML comment, an ATX heading, a list item).
_SPAN_END = re.compile(
    r"\n[ \t]*(?:\n|<!--|#{1,6}(?:[ \t]|$)|[-*+](?:[ \t]|$)|\d{1,9}[.)](?:[ \t]|$))",
    re.MULTILINE)


def _span_limit(text: str, start: int, end: int) -> int:
    """The offset an inline code span opened at `start` must close before."""
    line_start = text.rfind("\n", 0, start) + 1
    if re.match(r"[ \t]*#{1,6}(?:[ \t]|$)", text[line_start:]):
        line_end = text.find("\n", end)
        return len(text) if line_end < 0 else line_end
    found = _SPAN_END.search(text, end)
    return found.start() if found is not None else len(text)


def _html_comments(text: str):
    """The HTML comments of `text` outside inline code spans.

    Read left to right as Markdown does: whichever of a `<!--` and a backtick
    run starts first owns the text, so a comment inside a code span is code,
    and a backtick inside a comment is comment. A code span closes at the next
    backtick run of the same length before a blank line or a line that opens
    a block (`<!--`, a `#` heading, a list marker); a run with no closer there
    is a literal backtick. A heading is one line, so a run on a heading line
    closes on that line or not at all.
    """
    index = 0
    while True:
        comment = _HTML_COMMENT.search(text, index)
        ticks = _BACKTICKS.search(text, index)
        if comment is None:
            return
        if ticks is not None and ticks.start() < comment.start():
            width = len(ticks.group())
            limit = _span_limit(text, ticks.start(), ticks.end())
            closer = next((run for run in _BACKTICKS.finditer(text, ticks.end(), limit)
                           if len(run.group()) == width), None)
            index = closer.end() if closer is not None else ticks.end()
            continue
        yield comment
        index = comment.end()


def parse_markdown_claims(path: str, text: str) -> list[ClaimSite]:
    """Every claim annotation in the HTML comments of one Markdown document.

    Fenced code blocks and inline code spans are skipped, so documenting the
    syntax does not create a claim. Only an HTML comment counts; prose that
    mentions `revl:claim` is ordinary prose.
    """
    masked = _mask_fenced_code(text)
    sites: list[ClaimSite] = []
    for comment in _html_comments(masked):
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


#: The pytest defaults (`python_files`); the repository configures no other
#: pattern, so a test function in any other file is never collected.
_COLLECTED_FILE = re.compile(r"(test_.*|.*_test)\.py")


def _is_test_function(node: ast.stmt) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
        and node.name.startswith("test")


def _literal_mark(mark: ast.expr) -> str | None:
    """`skip` for a mark spelled literally `pytest.mark.skip` or
    `pytest.mark.skip(...)`; None for any other spelling. An alias
    (`import pytest as pt`, `from pytest import mark`, `skip =
    pytest.mark.skip`) is not followed: the caller refuses it as unreadable."""
    if isinstance(mark, ast.Call):
        mark = mark.func
    if isinstance(mark, ast.Attribute) and isinstance(mark.value, ast.Attribute) \
            and mark.value.attr == "mark" and isinstance(mark.value.value, ast.Name) \
            and mark.value.value.id == "pytest":
        return mark.attr
    return None


def _marks(node: ast.expr) -> list[ast.expr]:
    """The marks in a `pytestmark = ...` value: one mark or a list of them."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    return [node]


def _pytestmark_values(body: list[ast.stmt]) -> list[ast.expr]:
    """The values assigned to `pytestmark` directly in a module or class body."""
    values = []
    for item in body:
        if isinstance(item, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in item.targets):
            values.append(item.value)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) \
                and item.target.id == "pytestmark" and item.value is not None:
            values.append(item.value)
    return values


def _pytestmark_references(tree: ast.Module) -> int:
    """Every mention of `pytestmark`: a name in any context, an attribute, or
    a string (`globals()["pytestmark"]`)."""
    return sum(1 for node in ast.walk(tree)
               if (isinstance(node, ast.Name) and node.id == "pytestmark")
               or (isinstance(node, ast.Attribute) and node.attr == "pytestmark")
               or (isinstance(node, ast.Constant) and node.value == "pytestmark"))


def _direct_pytestmark_assignments(tree: ast.Module) -> int:
    """The `pytestmark = ...` statements this check reads: one plain target,
    in the module body or a top-level class body."""
    def plain(body: list[ast.stmt]) -> int:
        return sum(1 for item in body
                   if (isinstance(item, ast.Assign) and len(item.targets) == 1
                       and isinstance(item.targets[0], ast.Name)
                       and item.targets[0].id == "pytestmark")
                   or (isinstance(item, ast.AnnAssign) and item.value is not None
                       and isinstance(item.target, ast.Name)
                       and item.target.id == "pytestmark"))
    return plain(tree.body) + sum(plain(item.body) for item in tree.body
                                  if isinstance(item, ast.ClassDef))


def _base_reason(cls: ast.ClassDef) -> str | None:
    """A class inherits marks from its bases (a base's `pytestmark` or class
    decorator), so only `object` and `unittest.TestCase` are read."""
    if cls.keywords:
        return f"class {cls.name} passes class keywords the check cannot read"
    for base in cls.bases:
        spelled = ast.unparse(base)
        if spelled not in ("object", "unittest.TestCase"):
            return (f"class {cls.name} inherits from `{spelled}`, whose marks the "
                    "check cannot read")
    return None


class _Marks:
    """Every mark that applies to one test, read without importing it.

    `unreadable` is set when a decorator or a `pytestmark` is not a mark
    spelled literally `pytest.mark.<name>`: the check fails closed on it rather
    than resolve aliases. `own` are the function's decorators, `inherited` the
    class decorators and the class and module `pytestmark` values.
    """

    def __init__(self, tree: ast.Module, function: ast.stmt, cls: ast.stmt | None):
        self.unreadable: str | None = None
        inherited: list[ast.expr] = []
        for value in _pytestmark_values(tree.body):
            inherited.extend(_marks(value))
        if cls is not None:
            inherited.extend(cls.decorator_list)
            for value in _pytestmark_values(cls.body):
                inherited.extend(_marks(value))
        if _pytestmark_references(tree) != _direct_pytestmark_assignments(tree):
            self.unreadable = ("`pytestmark` is referenced in a shape the check "
                               "cannot read (only a plain module or class "
                               "assignment is read)")
        if cls is not None and self.unreadable is None:
            self.unreadable = _base_reason(cls)
        self.own = list(function.decorator_list)
        self.inherited = inherited
        for mark in self.own + self.inherited:
            if self.unreadable is not None:
                break
            if _literal_mark(mark) is None:
                self.unreadable = (f"`{ast.unparse(mark)}` is not a mark spelled "
                                   "`pytest.mark.<name>`, so the check cannot read it")
            elif isinstance(mark, ast.Call) and any(
                    k.arg is None for k in mark.keywords):
                self.unreadable = (f"`{ast.unparse(mark)}` passes `**` keywords, "
                                   "so the check cannot read it")

    def named(self, name: str, *, own_only: bool = False) -> list[ast.expr]:
        marks = self.own if own_only else self.own + self.inherited
        return [mark for mark in marks if _literal_mark(mark) == name]


def _run_reason(tree: ast.Module, function: ast.stmt, cls: ast.stmt | None) -> str | None:
    """Why one collected test does not run or cannot be read, or None.

    Refused: an unreadable mark, `pytest.mark.skip`, and
    `pytest.mark.xfail(run=False)` (a `run=` that is not a literal is
    unreadable). NOT decided: `pytest.mark.skipif`, whose condition runs only
    under pytest, and a `pytest.skip()` call inside the test body.
    """
    marks = _Marks(tree, function, cls)
    if marks.unreadable is not None:
        return marks.unreadable
    if marks.named("skip"):
        return "it is unconditionally skipped"
    for mark in marks.named("xfail"):
        run = next((k.value for k in mark.keywords if k.arg == "run"), None) \
            if isinstance(mark, ast.Call) else None
        if run is None:
            continue
        if not isinstance(run, ast.Constant):
            return "`pytest.mark.xfail(run=...)` is not a literal the check can read"
        if not run.value:
            return "it is marked `xfail(run=False)`, so it never runs"
    return None


def _skips_the_module(tree: ast.Module) -> bool:
    """A top-level `pytest.skip(...)` statement: no test in the file runs."""
    for item in tree.body:
        if isinstance(item, ast.Expr) and isinstance(item.value, ast.Call):
            func = item.value.func
            if isinstance(func, ast.Attribute) and func.attr == "skip" \
                    and isinstance(func.value, ast.Name) and func.value.id == "pytest":
                return True
    return False


def _tests(tree: ast.Module):
    """Every collectable test as `(name, function, class or None)`."""
    for item in tree.body:
        if _is_test_function(item):
            yield item.name, item, None
        elif isinstance(item, ast.ClassDef) and item.name.startswith("Test"):
            for member in item.body:
                if _is_test_function(member):
                    yield f"{item.name}::{member.name}", member, item


def _file_reason(tree: ast.Module, file: str) -> str | None:
    """None when the module has a test that runs and can be read; otherwise
    why not (the first test's reason): a witness file with no test left to
    run is not a witness."""
    reasons = []
    for name, function, cls in _tests(tree):
        reason = _run_reason(tree, function, cls)
        if reason is None:
            return None
        reasons.append(f"{name}: {reason}")
    if not reasons:
        return f"test file {file!r} defines no test function"
    return f"test file {file!r} has no test that runs ({reasons[0]})"


class _Unreadable(Exception):
    """A parametrization whose ids this module cannot compute exactly."""


#: Methods that leave a module-level literal as it was assigned.
_READ_ONLY_METHODS = frozenset({"items", "keys", "values", "get", "copy", "count",
                                "index"})


def _module_literal(tree: ast.Module, name: str):
    """The value of a module-level name bound once to a literal and never
    changed afterwards, or `_Unreadable`."""
    values = [item.value for item in tree.body
              if isinstance(item, ast.Assign) and len(item.targets) == 1
              and isinstance(item.targets[0], ast.Name) and item.targets[0].id == name]
    if len(values) != 1:
        raise _Unreadable(f"`{name}` is not bound once at module level")
    stores = [node for node in ast.walk(tree)
              if isinstance(node, ast.Name) and node.id == name
              and isinstance(node.ctx, (ast.Store, ast.Del))]
    if len(stores) != 1:
        raise _Unreadable(f"`{name}` is rebound")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            raise _Unreadable(f"`{name}` is rebound")
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            raise _Unreadable(f"`{name}` is changed in place")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == name and node.attr not in _READ_ONLY_METHODS:
            raise _Unreadable(f"`{name}.{node.attr}` may change it")
    return _static(values[0], tree)


def _static(expr: ast.expr, tree: ast.Module):
    """The value of `expr` when it is a literal, a module-level literal name,
    `sorted`/`list`/`tuple` of one, or its `.items()`/`.keys()`/`.values()`."""
    try:
        return ast.literal_eval(expr)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        pass
    if isinstance(expr, ast.Name):
        return _module_literal(tree, expr.id)
    if isinstance(expr, ast.Call) and not expr.keywords and len(expr.args) == 1:
        func = expr.func
        if isinstance(func, ast.Name) and func.id in ("sorted", "list", "tuple"):
            value = _static(expr.args[0], tree)
            return {"sorted": sorted, "list": list, "tuple": tuple}[func.id](value)
    if isinstance(expr, ast.Call) and not expr.keywords and not expr.args \
            and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr in ("items", "keys", "values"):
        value = _static(expr.func.value, tree)
        if isinstance(value, dict):
            return list(getattr(value, expr.func.attr)())
    raise _Unreadable("the parametrization is not a literal this check can read")


def _plain_id(value) -> str:
    """pytest's id for one parameter value, for the values whose id is the
    value itself; anything else is refused rather than guessed."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and value and value.isascii() and value.isprintable() \
            and "\\" not in value:
        return value
    raise _Unreadable(f"the id of parameter {value!r} is not its plain text")


def _argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _parameter_ids(call: ast.Call, tree: ast.Module) -> set[str]:
    """The ids of one `@pytest.mark.parametrize(...)`, exactly, or
    `_Unreadable`."""
    names_node = _argument(call, 0, "argnames")
    values_node = _argument(call, 1, "argvalues")
    if names_node is None or values_node is None:
        raise _Unreadable("the parametrize call has no argnames/argvalues")
    try:
        return _read_ids(names_node, values_node, call, tree)
    except (TypeError, ValueError, AttributeError) as exc:
        # a malformed call (`parametrize("x", 5)`, `ids=sorted(5)`) is refused
        # as unreadable; pytest itself would fail to collect it
        raise _Unreadable(f"the parametrize call is malformed: {exc}") from None


def _read_ids(names_node: ast.expr, values_node: ast.expr, call: ast.Call,
              tree: ast.Module) -> set[str]:
    names = _static(names_node, tree)
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    if not isinstance(names, (list, tuple)) or not names \
            or not all(isinstance(n, str) for n in names):
        raise _Unreadable("the argnames are not a string or a list of strings")
    values = list(_static(values_node, tree))
    ids_node = next((k.value for k in call.keywords if k.arg == "ids"), None)
    if ids_node is not None:
        ids = list(_static(ids_node, tree))
        if len(ids) != len(values):
            raise _Unreadable("`ids` and the parameter values differ in length")
        rendered = [_plain_id(i) if isinstance(i, str) else _bad_id(i) for i in ids]
    elif len(names) == 1:
        rendered = [_plain_id(value) for value in values]
    else:
        rendered = []
        for value in values:
            if not isinstance(value, (list, tuple)) or len(value) != len(names):
                raise _Unreadable("a parameter set does not match the argnames")
            rendered.append("-".join(_plain_id(part) for part in value))
    if len(set(rendered)) != len(rendered):
        raise _Unreadable("two parameter sets share an id, so pytest renames them")
    return set(rendered)


def _bad_id(value) -> str:
    raise _Unreadable(f"`ids` entry {value!r} is not a string")


def _fixture_params(tree: ast.Module) -> bool:
    """A `@pytest.fixture(params=...)` in the module: it multiplies the ids of
    whichever tests reach it, which this check does not trace."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and any(
                        k.arg == "params" for k in decorator.keywords):
                    return True
    return False


def _conftests(root: Path, file: str) -> list[Path]:
    """The `conftest.py` files pytest loads for a test file under `root`."""
    found = []
    directory = (root / file).parent
    while True:
        if (directory / "conftest.py").is_file():
            found.append(directory / "conftest.py")
        if directory == root or root not in directory.parents:
            return found
        directory = directory.parent


def _parameter_reason(tree: ast.Module, function: ast.stmt, cls: ast.stmt | None,
                      param: str, root: Path, file: str) -> str | None:
    """Why `<function>[param]` is not one of the function's real parameter
    ids, or None when it is. The ids are read from the parametrize call; a
    shape whose ids cannot be computed exactly is refused, never guessed."""
    marks = _Marks(tree, function, cls)
    if len(marks.named("parametrize")) != len(marks.named("parametrize", own_only=True)):
        return ("a class or module mark parametrizes it; name the function "
                "instead")
    calls = marks.named("parametrize", own_only=True)
    hooks = [tree]
    for conftest in _conftests(root, file):
        try:
            hooks.append(ast.parse(conftest.read_text(encoding="utf-8")))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            return f"{conftest.relative_to(root).as_posix()} cannot be parsed"
    for module in hooks:
        if _fixture_params(module) or any(
                isinstance(item, ast.FunctionDef)
                and item.name == "pytest_generate_tests" for item in module.body):
            return ("a parametrized fixture or `pytest_generate_tests` can add "
                    "ids; name the function instead")
    if not calls:
        return "the function is not parametrized"
    if len(calls) != 1 or not isinstance(calls[0], ast.Call):
        return "stacked parametrizations are not read; name the function instead"
    try:
        ids = _parameter_ids(calls[0], tree)
    except _Unreadable as exc:
        return f"its parameter ids cannot be read statically ({exc})"
    if param not in ids:
        return f"[{param}] is not one of its {len(ids)} parameter ids"
    return None


def _node_reason(tree: ast.Module, node: str, root: Path, file: str) -> str | None:
    """Why `test_x`, `test_x[param]` or `TestK::test_y[param]` does not name a
    collectable, not unconditionally skipped test in `tree`, or None."""
    bare, bracket, param = node.partition("[")
    if bracket and not param.endswith("]"):
        return f"test {node!r} is not a pytest node id"
    param = param[:-1]
    for name, function, cls in _tests(tree):
        if name != bare:
            continue
        reason = _run_reason(tree, function, cls)
        if reason is not None:
            return f"test {bare!r} in {file}: {reason}"
        if bracket:
            reason = _parameter_reason(tree, function, cls, param, root, file)
            if reason is not None:
                return f"test {node!r} in {file}: {reason}"
        return None
    return f"test {bare!r} is not a test function in {file}"


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
    if not _COLLECTED_FILE.fullmatch(path.name):
        return (f"{file!r} is not collected by pytest "
                "(the file name is not `test_*.py` or `*_test.py`)")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return f"test file {file!r} cannot be parsed"
    if _skips_the_module(tree):
        return f"test file {file!r} is unconditionally skipped"
    if not separator:
        return _file_reason(tree, file)
    return _node_reason(tree, node, root, file)


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
    findings.extend(_unnamed_entries(sites, registry, root, docgen_blocks))
    return findings


def _unnamed_entries(sites: list[ClaimSite], registry: dict[str, Witness],
                     root: Path, docgen_blocks: frozenset[str]) -> list[Finding]:
    """A finding for every registry entry no annotation reaches whose witness
    does not resolve. An entry may be registered before any sentence names
    it, but a dead witness in it is reported all the same; an entry on the
    `claim:` chain of an annotation is already judged at that annotation."""
    reached: set[str] = set()
    pending = [site.id for site in sites if site.id in registry]
    while pending:
        claim_id = pending.pop()
        if claim_id in reached or claim_id not in registry:
            continue
        reached.add(claim_id)
        if registry[claim_id].kind == "claim":
            pending.append(registry[claim_id].target)
    findings = []
    for claim_id in sorted(set(registry) - reached):
        witness = registry[claim_id]
        reason = _resolve(witness, registry, root, docgen_blocks, frozenset({claim_id}))
        if reason is not None:
            findings.append(Finding(
                "claim-witness-missing", "claims.toml",
                f"`{claim_id}` (named by no annotation) witness `{witness}`: {reason}"))
    return findings


def render(findings: list[Finding]) -> str:
    """The findings, one per line, for a CLI or a test."""
    return "\n".join(str(finding) for finding in findings)
