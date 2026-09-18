"""The COMPOSITION document argument, shared by every command that takes one.

A composition document is not a module. It declares no module-level component:
its rows, and the providers a `remote` row SYNTHESIZES, exist only once the row
table is RESOLVED. A command that compiles it with `compile_files` therefore
reads an EMPTY compilation and answers from that, which is how roadmap item 439
(issue #118) found a boundary surface, an IR document, a release-gate diff, a
policy dry run and a supervisor's decision queue each answering for a
composition that crosses to a peer over the network.

This module holds the one door all of them go through, so the refusals and the
admission choices are stated once. `revl.__main__` routes the shared compile
step through it; `revl.cli.observe` routes `revl dash`. It lives here rather
than in `revl.__main__` because that module runs `drop_cwd_entry()` at import,
so importing it from a per-command handler would re-run a side effect under
`python -m revl`, where `__main__` is not `revl.__main__`.
"""

from __future__ import annotations

import json
import sys

from ..diagnostics import report
from ..errors import RevlError


def _wiring_documents(paths: list[str]) -> tuple[list[str], list[str]]:
    """Which of `paths` declare a COMPOSITION, and which declare a LAYER, read
    by parsing alone.

    Detection never speaks for the compiler: a file that will not parse or will
    not open is skipped here and reaches the shared compile step, which reports
    it in its own wording with its own pointer.
    """
    from ..parser import parse_file  # noqa: PLC0415 — lazy, cli startup cost
    # `..parser` is the LANGUAGE parser (`revl.parser`), not this
    # package's `revl.cli.parser` argument-parser assembly.

    compositions: list[str] = []
    layers: list[str] = []
    for path in paths:
        try:
            program = parse_file(path)
        except (RevlError, OSError):
            continue
        if program.compositions:
            compositions.append(path)
        elif program.layers:
            layers.append(path)
    return compositions, layers


# The commands on the shared module compile step that RESOLVE a composition
# document argument instead (item 439, issue #118). Every command below reads
# `ir`; a composition compiled as a module makes that `ir` empty, and each one
# then answers from the empty document rather than from the composition.
#
# `goal` shares the same step and is deliberately NOT here. `goal audit` over a
# composition with no termination contract exits 0 by item 441/458's decision
# (`docs/design/458-termination-language-surface.md` §2.1); changing the
# document that decision is evaluated over is that item's call, not this one's.
_RESOLVES_A_COMPOSITION = frozenset({
    "audit", "compile", "version", "test", "query", "erase-report"})


def _composition_document(args, *, label: str | None = None,
                          files: list[str] | None = None,
                          refuse_code: int = 1):
    """A COMPOSITION document argument, for the commands that share the module
    compile step (item 439, issue #118).

    `main` compiles its file arguments as MODULES (`compile_files`). A
    composition document declares no module-level component: its rows, and the
    providers a `remote` row SYNTHESIZES, exist only once the row table is
    RESOLVED. So every command on the shared step used to read an EMPTY
    compilation for a composition, and answer from it.

    Slice G8c fixed that for `revl audit`, where the empty answer was an empty
    BOUNDARY SURFACE: `docs/design/439-a2a-task-lifecycle.md` decision 3 states
    G8 as "the boundary surface is the four (or one) synthesized externs,
    enumerable on the boundary surface `revl audit` renders, each carrying the
    folded `net.<host>` reach", and an operator who ran the CLI over a
    composition holding a `remote ... through a2a` row read "no crossings" for a
    composition that crosses to a peer over the network. An empty surface is
    read as an ABSENCE of authority, so that silence failed OPEN.

    The same wrong document reaches the rest of the group, and three of them
    fail open in the same direction (the failure direction is recorded per
    command in `docs/design/439-a2a-transport-binding.md`):

      * `compile` prints an IR document with no services, no components and an
        empty load order, and exits 0. It is a POSITIVE artifact asserting that
        the composition contains nothing.
      * `version` reads that same document — `--emit-manifest` prints it as the
        diff input a later `--against` reads, and a diff between two of them
        derives "PATCH, the interface is unchanged" for a composition that
        gained or lost a whole remote provider.
      * `test` collects nothing from the rows and prints "no tests to run" with
        exit 0, which is a green by vacuity.

    Two fail CLOSED today and are corrected here rather than repaired:
    `query` answers "unknown component" / "unknown service" with an empty known
    list, and `erase-report` answers "unknown realm" — each a nonzero exit, but
    each a false statement about a name the composition does define.

    `goal` is deliberately NOT on this door. `goal audit`'s zero exit over a
    composition with no termination contract is item 441/458's own decision
    (`docs/design/458-termination-language-surface.md` §2.1), and routing it
    through here would change the document that decision is made over without
    the review that belongs to it.

    Slice G8e brings the three doors onto the item-33 BOUNDARY POLICY here too,
    and they do not share that compile step, so the three parameters below say
    which command is being answered for rather than reading it off
    `args.command`: `label` is the command as an operator spells it (`policy
    evaluate`), `files` is the argument list to read when it is not `args.files`
    (`simulate policy-diff --composition`), and `refuse_code` is the exit status
    a refusal takes, because 1 already MEANS "a component would be refused" on
    `policy evaluate` and on `simulate policy-diff`. A layer document handed to
    either is a usage error, not a policy verdict, so it exits 2 there and stays
    1 on the shared step, where 1 is what every other refusal already returns.

    Returns `None` when no argument declares a composition (the caller falls
    through to the shared module compile), an `int` exit code when the command
    refuses, or the compiled composition document.
    """
    import os  # noqa: PLC0415 — one call site

    from ..composition import compile_composition  # noqa: PLC0415 — lazy

    command = label or args.command
    paths = list(args.files if files is None else files)
    docs, layers = _wiring_documents(paths)

    def _refuse(message: str, hint: str) -> int:
        print(f"error: {message}", file=sys.stderr)
        print(f"  hint: {hint}", file=sys.stderr)
        return refuse_code

    if layers:
        # A layer is a DELTA over a composition (426 §2.4): its rows are only
        # meaningful folded into the base that stacks it, so a layer on its own
        # is not a document any of these commands can answer over. Compiled as a
        # module it yields the same empty compilation a composition did, which
        # is the same wrong answer for a different reason.
        named = ", ".join(os.path.basename(layer) for layer in layers)
        return _refuse(
            f"`revl {command}` was given the layer document"
            f"{'s' if len(layers) > 1 else ''} {named}; a layer is a DELTA over "
            f"a composition, so it has no composition of its own to resolve",
            f"run `revl {command}` over the composition that names this layer "
            f"in its `stack` (or `site`) list: the folded document is the one "
            f"an operator reads")
    if not docs:
        return None

    names = ", ".join(os.path.basename(doc) for doc in docs)
    if len(docs) > 1:
        return _refuse(
            f"`revl {command}` was given {len(docs)} composition documents "
            f"({names}); a composition document IS the compiled unit, so there "
            f"is no one document to answer over",
            f"run `revl {command}` over one composition document per invocation")
    if len(paths) > 1:
        others = ", ".join(os.path.basename(f) for f in paths if f not in docs)
        return _refuse(
            f"`revl {command}` was given the composition document `{names}` "
            f"alongside modules ({others}); a composition names the rows it "
            f"compiles, so reading it beside hand-listed modules would answer "
            f"for a program neither of them describes",
            f"run the composition alone (`revl {command} {names}`), or run the "
            f"modules without it")

    # WHOLE-composition admission, and `confine=True` as `revl composition
    # --admit` uses it: every row is compiled (never a layer delta, so nothing
    # is skipped out of the document being answered over), and a
    # non-first-party stack-layer row compiles under its own untrusted-author
    # profile. Both choices are the over-refusing direction. There is
    # deliberately no `--trust-host-code` on this door: a command that had to be
    # told to trust the code it is enumerating would be answering a different
    # question.
    try:
        return compile_composition(docs[0], getattr(args, "root", None),
                                   confine=True)
    except RevlError as error:
        if getattr(args, "json_diagnostics", False):
            print(json.dumps(report(error), indent=2))
        else:
            print(f"error: {error}", file=sys.stderr)
        return refuse_code


