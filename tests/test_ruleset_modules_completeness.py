"""`attest.RULESET_MODULES` completeness (issue #989, second pass).

`ruleset_digest()` is the identity of the implementation that did the checking,
and `RULESET_MODULES` is the set of module bytes it folds. The membership rule
is stated in `attest.py` and settled by issue #989: a module whose BYTES move
the set of programs the frontend refuses is a rule and belongs in the digest.

The list was hand-kept, and a hand-kept list of a property nobody measures
drifts. It had drifted: eleven modules met the rule and were absent, including
the two host-body jails, the capability-parameter registry, the kernel
authority enumeration and the type relation itself. A corrected list resets the
clock. This file is the part that stops it running again, and it does it by
DERIVING membership twice rather than by restating the list:

  1. `test_every_module_that_refuses_is_in_the_digest` runs the reference
     frontend over the committed rejection corpus and records, for each
     refusal, the module the refusal was CONSTRUCTED in. Every such module must
     be a member. A new checker module fails this the first time it refuses a
     corpus program, with no list to remember to edit.

  2. `test_the_import_fringe_is_classified` computes the sibling modules the
     rule modules import and asserts SET EQUALITY with `attest.NOT_A_RULE`. A
     new import into the frontend cannot be left unclassified, and an entry
     that stops being reachable cannot be left behind. This is the half that
     catches a TABLE module, which is the shape check 1 cannot see: `retention`,
     `ui_family` and `resources` raise nothing at all and are read by a module
     that does.

WHAT NEITHER CHECK COVERS, stated so it is not mistaken for coverage: a module
reached only through a code path no corpus program exercises AND imported by
nothing in `RULESET_MODULES` is invisible to both. Closing that would need the
refusal set itself as the oracle, which is a mutation run over every module and
not a unit test. `test_a_neutralised_member_moves_the_refusal_set` does that
run for one member, so the technique the audit used is committed rather than
described.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

from revl import attest
from revl.compiler import compile_files
from revl.errors import RevlError

ROOT = pathlib.Path(__file__).resolve().parent.parent
REJECTIONS = ROOT / "examples" / "rejections"
SRC = pathlib.Path(attest.__file__).resolve().parent


def _sibling_imports(path: pathlib.Path) -> set[str]:
    """The `revl` sibling modules `path` imports, lazy ones included.

    Lazy matters: `lower.py` imports `cap_order`, `cardinality` and
    `kernel_boundary` inside the functions that refuse on them, to break import
    cycles. A top-level-only scan would report the frontend as reaching none of
    the three, and all three decide refusals.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                found.add(node.module.split(".")[0])
            elif node.level == 1 and node.module is None:
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif node.module and node.module.startswith("revl."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("revl."):
                    found.add(alias.name.split(".")[1])
    return {name for name in found if (SRC / f"{name}.py").exists()}


def _refusal_origins(paths: list[pathlib.Path]) -> dict[str, list[str]]:
    """`{module: [program, ...]}` for the module each refusal is BUILT in.

    Built in, not re-raised from: the walk takes the innermost `src/revl` frame
    on the construction stack that is not `errors.py`, so a refusal `lower.py`
    constructs while `compiler.py` is on the stack is attributed to `lower.py`.
    """
    origins: dict[str, list[str]] = {}
    original = RevlError.__init__

    def recording(self, *args, **kwargs):
        original(self, *args, **kwargs)
        frame = sys._getframe(1)
        while frame is not None:
            name = os.path.abspath(frame.f_code.co_filename)
            if (name.startswith(str(SRC) + os.sep)
                    and os.path.basename(name) != "errors.py"):
                self._origin_module = os.path.basename(name)[:-3]
                return
            frame = frame.f_back
        self._origin_module = None

    RevlError.__init__ = recording
    try:
        for path in paths:
            try:
                compile_files([str(path)])
            except RevlError as error:
                module = getattr(error, "_origin_module", None)
                if module is not None:
                    origins.setdefault(module, []).append(path.name)
    finally:
        RevlError.__init__ = original
    return origins


def test_the_corpus_this_derives_from_is_still_there():
    """The derivation is only as good as the corpus it runs. A rejection corpus
    that has been emptied or moved would make the next test pass by measuring
    nothing, which is the shape a gate fails silently in."""
    programs = sorted(REJECTIONS.glob("*.rvl"))
    assert len(programs) >= 150, (
        f"examples/rejections/ holds {len(programs)} programs; the completeness "
        "derivation below reads its refusals and a shrunken corpus would weaken "
        "it without failing")


def test_every_module_that_refuses_is_in_the_digest():
    """DERIVED, not declared: every module that constructs a refusal over the
    rejection corpus must be a member of `RULESET_MODULES`.

    This is the check that would have caught `typecheck` and `lexer` on the day
    they started refusing, which is years before anyone read the list."""
    programs = sorted(REJECTIONS.glob("*.rvl"))
    origins = _refusal_origins(programs)
    assert origins, "no refusal was recorded; the corpus or the probe is broken"
    missing = sorted(set(origins) - set(attest.RULESET_MODULES))
    assert not missing, (
        "these modules refuse a program in examples/rejections/ and are not in "
        "attest.RULESET_MODULES, so two builds that refuse different programs "
        "can carry the same ruleset digest: "
        + "; ".join(f"{m} (e.g. {origins[m][0]})" for m in missing))


def test_the_import_fringe_is_classified():
    """Set equality between the rule modules' import fringe and
    `attest.NOT_A_RULE`, which is the shape that makes a second copy impossible
    rather than merely wrong.

    Both directions are load-bearing. An unclassified import is a module the
    frontend reaches that nobody has judged. A stale entry is a reason nobody
    has re-read, and the exclusion list is the only hand-kept half left."""
    fringe: set[str] = set()
    for member in attest.RULESET_MODULES:
        fringe |= _sibling_imports(SRC / f"{member}.py")
    fringe -= set(attest.RULESET_MODULES)
    unclassified = sorted(fringe - set(attest.NOT_A_RULE))
    stale = sorted(set(attest.NOT_A_RULE) - fringe)
    assert not unclassified, (
        "a rule module imports these and nothing says whether they are rules. "
        "Either add them to RULESET_MODULES or give each a reason in "
        f"attest.NOT_A_RULE: {unclassified}")
    assert not stale, (
        "attest.NOT_A_RULE excuses modules no rule module imports any more; "
        f"drop them so the table stays a statement about live code: {stale}")


def test_every_exclusion_carries_a_reason():
    """An exclusion is a claim that the module's bytes leave the refusal set
    unchanged. A blank one is an unexamined member."""
    for module, reason in sorted(attest.NOT_A_RULE.items()):
        assert isinstance(reason, str) and len(reason) > 20, (
            f"attest.NOT_A_RULE[{module!r}] states no reason")


def test_the_members_all_exist_and_the_digest_reads_them():
    """A member naming a file that is not there raises on the first digest, so
    the failure would be at attestation time. Fail here instead."""
    for member in attest.RULESET_MODULES:
        assert (SRC / f"{member}.py").is_file(), (
            f"RULESET_MODULES names {member!r} and src/revl/{member}.py is not "
            "there")
    assert len(set(attest.RULESET_MODULES)) == len(attest.RULESET_MODULES), \
        "RULESET_MODULES has a duplicate, which would fold one module twice"
    assert len(attest.ruleset_digest()) == 64


@pytest.mark.parametrize("member", ["typecheck", "lexer", "resources",
                                    "kernel_boundary", "ui_family"])
def test_a_measured_member_is_read_by_the_digest(member):
    """The eleven added members were each established by neutralising the module
    and watching the refusal set move while the digest did not. The second half
    of that measurement is what this pins: the module's bytes are now an input,
    so a byte changed in it changes `ruleset_digest()`.

    Read off the digest computation rather than performed by editing the tree:
    the memo is cleared, one member's bytes are perturbed in memory, and the
    digest is recomputed."""
    import hashlib

    def digest_over(sources: dict[str, bytes]) -> str:
        acc = hashlib.sha256(b"revl-attest-ruleset\x00")
        for name in attest.RULESET_MODULES:
            data = sources[name]
            label = name.encode("utf-8")
            acc.update(len(label).to_bytes(8, "big"))
            acc.update(label)
            acc.update(len(data).to_bytes(8, "big"))
            acc.update(data)
        return acc.hexdigest()

    sources = {name: (SRC / f"{name}.py").read_bytes()
               for name in attest.RULESET_MODULES}
    assert digest_over(sources) == attest.ruleset_digest(), (
        "this test reimplements the fold; it has drifted from _read_ruleset")
    perturbed = dict(sources)
    perturbed[member] = sources[member] + b"\n# byte\n"
    assert digest_over(perturbed) != attest.ruleset_digest(), (
        f"{member} is listed but a change to its bytes does not move the digest")


def test_a_neutralised_member_moves_the_refusal_set():
    """The audit technique itself, committed for one member so it is a run
    rather than a description.

    `resources.acquire_return_is_nominal_handle` is item 308's R0 predicate.
    `lower.py` refuses an `extern acquire` whose return is not a nominal opaque
    handle by asking it. Make it constant and the refusal stops firing, which is
    what "its bytes move the refusal set" means in one line. Patched on the name
    `lower` bound at import, not on disk, so nothing is left behind if this
    fails."""
    from revl import lower

    source = (
        "extern pure fn drop(h: Int) = @py { return None }\n"
        "extern acquire fn grab() -> Int undo drop(result) = @py { return 1 }\n")
    from revl.compiler import compile_source

    with pytest.raises(RevlError) as refusal:
        compile_source(source, "r0.rvl")
    assert "nominal opaque handle type" in str(refusal.value)

    original = lower.acquire_return_is_nominal_handle
    lower.acquire_return_is_nominal_handle = lambda returns: True
    try:
        try:
            compile_source(source, "r0.rvl")
        except RevlError as error:
            pytest.fail(
                "neutralising the R0 predicate should stop the R0 refusal; it "
                f"refused anyway ({error}), so this test no longer measures "
                "what it says it does")
    finally:
        lower.acquire_return_is_nominal_handle = original
