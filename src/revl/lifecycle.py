"""The lifecycle stages of `revl run`, and how a failure names the one it hit
(roadmap item 461, issue #724).

Reproducing a failure has repeatedly required knowing *where in the run* it
happened — did the composition fail to compile, fail admission, fail to resolve
its config, fail to boot, fail while serving, or fail to tear down cleanly?
Until now `revl run` printed every failure as a bare ``error: <exc>`` with no
indication of the stage, so a reader could not tell a compile refusal from a
boot fault without reading the message closely, and an automation could not
route on it at all.

This module is the single source of truth for the stage vocabulary. It is
deliberately tiny and pure — it imports nothing from the runtime — so the CLI
error paths, the runtime fault path, and the tests all name the same stages the
same way. The ordering mirrors the actual run: a composition is COMPILEd, then
ADMITTED (no open obligations), its CONFIG is resolved, it BOOTs, it RUNs, and
it TEARs DOWN with a no-residue proof.

The renderer keeps the underlying diagnostic byte-identical (a ``RevlError``
already carries its ``file.rvl:line``) and only *appends* the stage on its own
labelled line, so every existing ``error: <exc>`` first line is unchanged and a
reader — or a ``grep`` — gains the stage without losing the source location.
"""

from __future__ import annotations

# ------------------------------------------------------------------ stages
#
# Each stage has a stable id (the greppable token an automation routes on) and
# a one-line human gloss (what the run was doing when it failed). The ids are
# kept lowercase and hyphen-free so they read cleanly in `lifecycle stage: boot`
# and survive a round-trip through a JSON field unescaped.

COMPILE = "compile"
ADMISSION = "admission"
CONFIG = "config"
BOOT = "boot"
RUN = "run"
TEARDOWN = "teardown"

# Ordered, so a consumer can reason about "which stages came before this one"
# (a boot failure implies compile + admission + config already passed).
STAGES: tuple[str, ...] = (COMPILE, ADMISSION, CONFIG, BOOT, RUN, TEARDOWN)

_GLOSS: dict[str, str] = {
    COMPILE: "compiling the .rvl sources",
    ADMISSION: "admission — a draft with open obligations may not run",
    CONFIG: "resolving config and the environment contract",
    BOOT: "booting the composition (loading + activating providers)",
    RUN: "serving a call against a provided service",
    TEARDOWN: "teardown and the no-residue proof",
}


def gloss(stage: str) -> str:
    """The human one-liner for a stage id, or the id itself if unknown (an
    unknown stage is never a crash — the id still carries the routing signal)."""
    return _GLOSS.get(stage, stage)


def annotate(stage: str) -> str:
    """The trailing annotation line naming the stage a failure hit, e.g.::

        (lifecycle stage: boot — booting the composition ...)

    It is a *second* line by design: the caller prints ``error: <exc>`` first
    (unchanged, source location intact), then this, so the stage is added
    without disturbing anything downstream that reads the first line."""
    return f"  (lifecycle stage: {stage} — {gloss(stage)})"


def render(exc_text: str, stage: str) -> str:
    """The full ``revl run`` failure text: the diagnostic, then the stage line.

    ``exc_text`` is whatever the site already rendered (a ``RevlError`` with its
    ``file.rvl:line``, or a plain problem string). This never rewrites it — a
    reader's first line, and any tool keyed to it, sees exactly what it saw
    before item 461; the stage rides underneath."""
    return f"{exc_text}\n{annotate(stage)}"
