"""Terse in, canonical out (issue #1700).

Output tokens cost several times what input tokens cost and are decode-bound,
so an agent should not spend them on layout. `revl fmt` computes the canonical
layout of any source it can scan, and its IR-equivalence gate proves the
rewrite changed nothing the compiler sees. The authoring verbs use both:

* `canonicalise(text)` formats one text under that gate. A rewrite the gate
  refuses, or a text the formatter cannot scan, is kept exactly as written:
  canonicalisation is a convenience and never changes what is admitted.
* a whole source `revl_check`, `revl_swap` or a `revl_load` draft receives is
  COMPILED AS SENT (after `complete.py` fills in terse punctuation that does
  not parse, inside its own lines), so every diagnostic names a line the agent wrote, and only
  STORED canonical (the gate proved both compile to the same IR). The verb
  answers with `canonicalSource: {changed, digest}`, and the text itself only
  when asked (`returnCanonical: true`), because sending the text back would
  cost the agent input tokens it rarely needs.

File-backed candidates are left alone: their text is the operator's file, not
the agent's output. `revl_load` keeps what it was sent too, so a draft or a
first load holds the agent's bytes (and an anchor edit copied from them still
matches); the first swap or edit stores canonical text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..errors import RevlError

REPORT_KEY = "canonicalSource"


@dataclass
class Canonical:
    text: str
    changed: bool
    reason: str = ""

    def report(self, *, with_text: bool = False) -> dict:
        """The stored text's digest, whether the server rewrote what was sent,
        and the text itself only when asked for."""
        out: dict = {"changed": self.changed, "digest": digest(self.text)}
        if self.reason:
            out["kept"] = self.reason
        if with_text:
            out["source"] = self.text
        return out


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def canonicalise(text: str, filename: str = "<candidate>.rvl", *,
                 original_ir: dict | None = None) -> Canonical:
    """`text` in canonical layout when the formatter can prove the rewrite
    changed nothing the compiler sees; `text` itself otherwise.

    `original_ir` (issue #2194) is the document a caller already compiled for
    `text`, so the gate does not compile it again."""
    from ..formatter import FormatError, format_admitted  # noqa: PLC0415

    try:
        formatted, gate = format_admitted(text, filename,
                                          original_ir=original_ir)
    except (FormatError, RevlError) as error:
        return Canonical(text, False, f"not formatted: {error.message}")
    if formatted == text:
        return Canonical(text, False)
    if not gate.admitted:
        return Canonical(text, False, f"not formatted: {gate.reason}")
    return Canonical(formatted, True)


def canonical_arguments(arguments: dict) -> tuple[dict, dict | None]:
    """A verb's arguments with an inline `source` and its inline modules
    canonicalised, and the report on the main source (None when the call
    carried no inline source)."""
    source = arguments.get("source")
    modules = arguments.get("modules")
    if not isinstance(source, str) and not modules:
        return arguments, None
    out = dict(arguments)
    report = None
    if isinstance(source, str):
        canon = canonicalise(source)
        out["source"] = canon.text
        report = canon.report(with_text=arguments.get("returnCanonical") is True)
    if isinstance(modules, dict):
        out["modules"] = {path: canonicalise(text, path).text
                          if isinstance(text, str) else text
                          for path, text in modules.items()}
    return out, report


def complete_arguments(arguments: dict) -> tuple[dict, list]:
    """`(sent, completed)`: the arguments with terse punctuation completed
    (`complete.py`), and the insertions made. Split out of `prepare` so a verb
    that compiles before it canonicalises (issue #2194) can complete first."""
    from .complete import complete  # noqa: PLC0415

    source = arguments.get("source")
    sent, completed = dict(arguments), []
    if isinstance(source, str):
        sent["source"], completed = complete(source)
    modules = arguments.get("modules")
    if isinstance(modules, dict):
        sent["modules"] = {path: complete(text, path)[0] if isinstance(text, str)
                           else text for path, text in modules.items()}
    return sent, completed


def prepare(arguments: dict) -> tuple[dict, dict, dict | None]:
    """`(sent, stored, report)` for a verb's inline candidate (issue #1700).

    `sent` is what to COMPILE: the arguments as given, with terse punctuation
    completed when the source does not parse as written (`complete.py`; the
    insertions stay inside their lines, so diagnostics keep their line
    numbers). `stored` is what to HOLD: `sent` canonicalised under the
    IR-equivalence gate. `report` is `canonicalSource`, with `completed` when
    anything was inserted; None when the call carried no inline source."""
    sent, completed = complete_arguments(arguments)
    stored, report = canonical_arguments(sent)
    if report is not None and completed:
        report["completed"] = completed
        report["changed"] = True
    return sent, stored, report


def report_after(arguments: dict, completed=(), original_ir=None) -> dict | None:
    """`canonicalSource` for arguments already completed, and optionally
    already compiled (issue #2194).

    `original_ir` is the document the caller's own compile produced for the
    inline `source`; the gate reuses it instead of compiling that text again.
    The `modules` shape keeps the plain path: the gate compiles the main
    source WITHOUT `modules` (they are not its argument), so the caller's
    compile is not the one the gate would make and cannot be substituted."""
    source = arguments.get("source")
    if (original_ir is not None and isinstance(source, str)
            and not arguments.get("modules")):
        report = canonicalise(source, original_ir=original_ir).report(
            with_text=arguments.get("returnCanonical") is True)
    else:
        _stored, report = canonical_arguments(arguments)
    if report is not None and completed:
        report["completed"] = list(completed)
        report["changed"] = True
    return report


def attach(payload: dict, report: dict | None) -> dict:
    """`payload` with the canonical-source report, when there is one."""
    if report is not None and isinstance(payload, dict):
        payload[REPORT_KEY] = report
    return payload
