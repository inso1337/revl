"""The docs' revl snippets, compiled.

Nothing used to compile the code in README.md or docs/**.md, and it rotted:
when the G4 emission rule landed, the flagship `UserCache` example went stale
in three separate documents at once — including one in docs/guide-ai-agents.md
that contradicted a rule stated seventy lines below it — and the suite stayed
green. This file closes that.

The hard part is not compiling the blocks; it is that a doc snippet is not
always a program. Docs legitimately show a component body without its
services, a bare `match` arm list, a `provide` block on its own, elided
pseudocode with `...` in it, and syntax that does not exist yet. A gate that
demanded every snippet be a whole program would make the docs worse, and a
gate that skipped anything it could not compile would check nothing. So each
block declares what it *is*, in its fence, and every kind gets a real check:

    ```revl                 a complete program.   MUST COMPILE.
    ```revl fragment        not a whole program — a component body, a method
                            body, a statement, an expression. Compiled inside
                            each scaffold in SCAFFOLDS; MUST GET PAST THE
                            PARSER in at least one of them, so the fragment's
                            syntax is really revl syntax. A fragment that
                            compiles standalone is rejected: it is a program,
                            so drop the marker.
    ```revl sketch          deliberately not compilable: elided bodies (`...`),
                            or syntax that is proposed rather than implemented.
                            MUST NOT COMPILE — when the feature lands, this
                            fails and tells the author to promote the block.
    ```revl reject          code the compiler must refuse (a worked rejection).
    ```revl reject T1       MUST NOT COMPILE; with a code, the diagnostic's
                            `revl.diagnostics.classify()` code must match.

The convention is written up in docs/conformance.md ("Doc examples are
compiled"), and every failure below reprints it, so an author who trips the
gate never has to go looking.

What this does NOT check: a `fragment` is checked for syntax, not for types —
its free names (`config`, `Pool`, a service it never declares) have no
meaning outside the doc's prose. Marking a rotted program `fragment` would
therefore hide it. That is a review question, not a mechanical one; the
markers are deliberately few and deliberately loud so it stays visible.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402

# Files whose snippets are swept. README plus every doc; a new doc is covered
# the moment it is added, which is the point — an opt-in list would rot the
# same way the snippets did.
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]

# Files held out of the sweep, with the reason. This must stay ~empty: it is
# the one place the gate can be silenced, so a name here is a debt, not a
# setting. Held-out blocks are still collected and reported as skips with this
# reason, never dropped.
HELD_OUT: dict[str, str] = {
    # Owned by another work stream right now; its one block is `lifecycle test`
    # — proposed syntax that needs the `sketch` marker. Add the marker and
    # delete this entry.
    "docs/v2.0-roadmap.md": "roadmap is owned elsewhere; its block needs a "
                            "`sketch` marker (proposed `lifecycle test` syntax)",
}

# The scaffolds a `fragment` is tried in, in order. Each is self-contained and
# knows nothing about any particular block: they exist to get a non-top-level
# form to the place in the grammar where it is legal, not to supply a snippet's
# missing declarations.
SCAFFOLDS: list[tuple[str, str]] = [
    ("as written, at top level", "{body}"),
    ("inside a component activation body", "component _Doc {{\n{body}\n}}"),
    ("inside a provide method", "component _Doc {{\n  provide doc {{\n{body}\n  }}\n}}"),
    ("inside a function body", "fn _doc() {{\n{body}\n}}"),
    ("in expression position", "fn _doc() = (\n{body}\n)"),
]

HOWTO = """
revl blocks in README.md and docs/**.md are compiled by tests/test_doc_examples.py.
Tell the gate what this block is, in the fence:

  ```revl            a complete program            -> must compile
  ```revl fragment   a body/statement/expression   -> must be valid revl syntax
                                                      in some scaffold
  ```revl sketch     elided (`...`) or proposed    -> must NOT compile
  ```revl reject     code the compiler refuses     -> must NOT compile
  ```revl reject T1  ... for this diagnostic code  -> must fail with that code

Pick the weakest marker that is true. If the block is meant to be a working
example, the fix is the snippet, not the marker.
"""

FENCE = re.compile(r"^```revl([^\n]*)\n(.*?)^```", re.M | re.S)
# A parse/lex rejection, as opposed to a name/type/link one. `classify` cannot
# be used for this: the parser tags some of its own errors with the guarantee
# they protect (a stray statement in a component body comes back as G6), so the
# code says "G6" for what is really a syntax error. The message shape is what
# separates them, and DESIGN.md §9 makes these openings a deliverable.
SYNTAX_MESSAGE = re.compile(r"^(expected |unexpected character|unterminated )")

MARKERS = ("", "fragment", "sketch", "reject")


@dataclass(frozen=True)
class Block:
    path: Path
    line: int
    marker: str
    argument: str
    body: str

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}"

    def __str__(self) -> str:  # the pytest parameter id
        return self.where + (f" [{self.marker}]" if self.marker else "")


def _collect() -> list[Block]:
    blocks: list[Block] = []
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        for match in FENCE.finditer(text):
            info = match.group(1).strip().split()
            blocks.append(Block(
                path=path,
                line=text[:match.start()].count("\n") + 1,
                marker=info[0] if info else "",
                argument=info[1] if len(info) > 1 else "",
                body=match.group(2),
            ))
    return blocks


BLOCKS = _collect()


def _compile(source: str) -> dict | None:
    """None when it compiles, else the classified diagnostic."""
    try:
        compile_source(source)
    except RevlError as error:
        return classify(error)
    except Exception as error:  # noqa: BLE001 — a crash is a failure, loudly
        return {"code": "CRASH", "category": "crash",
                "message": f"{type(error).__name__}: {error}"}
    return None


def _is_syntax(diagnostic: dict) -> bool:
    return (diagnostic.get("category") in ("parse", "lex")
            or bool(SYNTAX_MESSAGE.match(diagnostic["message"])))


def _render(diagnostic: dict) -> str:
    return f"{diagnostic['code']}: {diagnostic['message']}"


def _numbered(body: str) -> str:
    return "\n".join(f"  {n:>3} | {line}"
                     for n, line in enumerate(body.rstrip().splitlines(), 1))


def test_the_sweep_actually_found_blocks():
    """A regex that stops matching would turn this whole file into a no-op."""
    assert len(BLOCKS) >= 30, f"only {len(BLOCKS)} revl blocks found — extractor broken?"
    assert any(b.path.name == "README.md" for b in BLOCKS)


@pytest.mark.parametrize("block", BLOCKS, ids=str)
def test_doc_example(block: Block):
    held = HELD_OUT.get(str(block.path.relative_to(ROOT)))
    if held:
        pytest.skip(f"{block.where}: {held}")

    assert block.marker in MARKERS, (
        f"{block.where}: unknown fence marker ```revl {block.marker}`\n{HOWTO}")

    if block.marker == "":
        diagnostic = _compile(block.body)
        assert diagnostic is None, (
            f"{block.where}: this block is fenced as a complete revl program, "
            f"and it does not compile.\n\n"
            f"  {_render(diagnostic)}\n\n{_numbered(block.body)}\n\n"
            f"Fix the snippet if it is meant to work — a doc example that no "
            f"longer compiles is the bug this gate exists for. If it was never "
            f"a whole program, mark it.\n{HOWTO}")
        return

    if block.marker == "fragment":
        assert _compile(block.body) is not None, (
            f"{block.where}: marked `fragment`, but it compiles as a complete "
            f"program on its own. Drop the marker — a plain ```revl fence "
            f"checks it properly.\n{HOWTO}")
        attempts = []
        for description, scaffold in SCAFFOLDS:
            diagnostic = _compile(scaffold.format(body=block.body))
            if diagnostic is None or not _is_syntax(diagnostic):
                return  # got past the parser: the fragment's syntax is real
            attempts.append(f"  {description}: {_render(diagnostic)}")
        pytest.fail(
            f"{block.where}: marked `fragment`, but it is not valid revl "
            f"syntax in any position — every scaffold failed to parse it:\n"
            + "\n".join(attempts) + f"\n\n{_numbered(block.body)}\n\n"
            f"If the block is elided pseudocode (`...`) or proposed syntax, "
            f"mark it `sketch`. Otherwise the snippet has a syntax error.\n{HOWTO}")

    if block.marker == "sketch":
        assert _compile(block.body) is not None, (
            f"{block.where}: marked `sketch` — pseudocode or not-yet syntax — "
            f"but it compiles now. Promote it: drop the marker so the gate "
            f"holds it to a working example.\n{HOWTO}")
        return

    if block.marker == "reject":
        diagnostic = _compile(block.body)
        assert diagnostic is not None, (
            f"{block.where}: marked `reject`, but the compiler accepts it. "
            f"Either the refusal regressed (a real bug — check "
            f"examples/rejections/) or the snippet drifted.\n"
            f"{_numbered(block.body)}\n{HOWTO}")
        if block.argument:
            assert diagnostic["code"] == block.argument, (
                f"{block.where}: marked `reject {block.argument}`, but the "
                f"rejection came back as {_render(diagnostic)}.\n"
                f"Update the fence if the new code is the right one.\n{HOWTO}")


# ---------------------------------------------------------------------------
# the gate's own negative controls
#
# A gate nobody has watched fail is a gate nobody knows works — and the docs
# will be green for long stretches, so there is no other moment to find out.
# Each case below drives the same `test_doc_example` the sweep does, over a
# synthetic block, and pins that it fails for the right reason.
# ---------------------------------------------------------------------------

def _gate(marker: str, body: str, argument: str = "") -> str | None:
    """Run the gate over one synthetic block. None on pass, else the message."""
    block = Block(path=ROOT / "README.md", line=0, marker=marker,
                  argument=argument, body=body)
    try:
        test_doc_example(block)
    except BaseException as failure:  # noqa: BLE001 — pytest.fail raises BaseException
        return str(failure)
    return None


# The README's flagship example exactly as it was before this branch: it
# `requires db: Database` and no document ever declared that service. This is
# the shape of rot the gate exists for, kept as a fixture so the check cannot
# be weakened without a red test.
ROTTED_FLAGSHIP = """
service Cache {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
"""

# The same component with the G4 rule broken the way it broke for real: the
# service declares `put` plain, the body reaches an emission.
G4_ROTTED_FLAGSHIP = ROTTED_FLAGSHIP.replace(
    "emission fn put(key: Str, value: Str)", "fn put(key: Str, value: Str)")

WORKING_FLAGSHIP = ("service Database { emission fn execute(sql: Str) -> Int }\n"
                    + ROTTED_FLAGSHIP)


def test_a_working_program_passes():
    assert _gate("", WORKING_FLAGSHIP) is None


def test_a_rotted_program_fails_and_names_the_missing_service():
    message = _gate("", ROTTED_FLAGSHIP)
    assert message is not None, "the gate accepted a program that does not compile"
    assert "unknown service `Database`" in message
    assert "does not compile" in message


def test_the_g4_regression_that_started_this_is_caught():
    """The exact rot in the brief: a plain-declared `put` whose body emits.

    This is the one the gate had to catch. With `Database` declared, the only
    thing wrong with the block is the emission propagation rule — so if this
    passes, the gate is not reading the compiler's answer.
    """
    source = ("service Database { emission fn execute(sql: Str) -> Int }\n"
              + G4_ROTTED_FLAGSHIP)
    message = _gate("", source)
    assert message is not None, "a G4 violation compiled — the rule regressed"
    assert "emission" in message, message


def test_a_fragment_that_is_really_a_program_is_rejected():
    message = _gate("fragment", WORKING_FLAGSHIP)
    assert message is not None
    assert "Drop the marker" in message


def test_a_fragment_with_a_syntax_error_fails():
    message = _gate("fragment", "provide cache {\n  fn get(key) = = store.get(key)\n}")
    assert message is not None
    assert "not valid revl syntax in any position" in message


def test_a_real_fragment_passes():
    assert _gate("fragment", "provide cache {\n  fn get(key) = store.get(key)\n}") is None


def test_a_sketch_that_started_compiling_must_be_promoted():
    message = _gate("sketch", WORKING_FLAGSHIP)
    assert message is not None
    assert "Promote it" in message


def test_a_real_sketch_passes():
    assert _gate("sketch", "pub fn lex(source: Str) -> List[Token] { ... }") is None


REJECTED = ('service S { fn f(x: Int) -> Int }\n'
            'component C provides s: S { provide s { fn f(x) = "nope" } }')


def test_a_rejection_block_must_actually_be_rejected():
    assert _gate("reject", REJECTED) is None
    message = _gate("reject", WORKING_FLAGSHIP)
    assert message is not None
    assert "the compiler accepts it" in message


def test_a_rejection_block_pins_its_diagnostic_code():
    assert _gate("reject", REJECTED, argument="T1") is None
    message = _gate("reject", REJECTED, argument="G4")
    assert message is not None
    assert "came back as T1" in message


def test_an_unknown_marker_is_refused():
    message = _gate("probably-fine", WORKING_FLAGSHIP)
    assert message is not None
    assert "unknown fence marker" in message
