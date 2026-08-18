"""Why-traces — the derivation behind a rejection, carried as data.

Several of revl's most important rejections are the verdict of a search the
compiler runs and then throws away: G4's least fixed point over the call
graph, G3's cycle detection, G2's provider table. The verdict alone makes
the author (or the agent) re-run that search by hand to find out *why*. A
`WhyTrace` keeps the evidence and hands it on: `render` for the human
`error:` output, `to_json` for `--json-diagnostics` and the MCP bridge.

Design decisions this module makes:

* **Evidence is data, never a pre-formatted string.** Producers build
  `TraceStep`s; only `render`/`to_json` turn them into text. Nothing in
  `lower.py` formats a trace.
* **Locations are best-effort.** `file`/`line` are `None` when a
  declaration's provenance is not recorded — an ambient component read
  from a running manifest has a file but no line; `compile_source` has no
  file at all. Renderers must tolerate both.
* **`detail` is a classification, not prose** ("emission", "provides
  `db`"). It says why an arrow was followed without re-deriving it.
* **`kind` is an open string, not an enum.** A later analysis (capability
  sets on emissions, realm-aware conflicts) can introduce a trace kind
  without touching any consumer.
* **`shape` distinguishes a path from a set.** G4 and G3 evidence is a
  chain (`a -> b -> c`); a G2 conflict is an unordered pair of providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHAIN = "chain"   # consecutive steps compose: step[i] leads to step[i+1]
SET = "set"       # steps are co-equal exhibits (both providers of one key)

# headline per trace kind; `{subject}` is the thing being explained. Unknown
# kinds fall back to a generic line, so a new analysis needs no edit here.
_HEADLINES = {
    "emission-propagation": "why `{subject}` is emission",
    "dependency-cycle": "why `{subject}` is in a dependency cycle",
    "provision-conflict": "why `{subject}` has more than one provider",
}


@dataclass(frozen=True)
class TraceStep:
    """One link of a derivation: a named thing, where it is declared, and
    what role it plays."""

    name: str
    kind: str = "step"           # "call" | "emission" | "component" | "provider"
    file: str | None = None
    line: int | None = None
    detail: str | None = None    # short classification, e.g. "emission"
    # What the hop reaches, when the analysis knows more than "an emission".
    # G4's emission set is growing into a set of *capabilities* rather than a
    # boolean, and a chain is far more useful when it says which capability
    # each hop pulled in. Empty until that lands — every consumer already
    # renders and serialises it, so populating it is a change at the producer
    # (`_EmissionEvidence.capabilities_of`) and nowhere else.
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # accept any sequence at the call site; store an immutable one
        object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def location(self) -> str | None:
        if self.file is None:
            return None
        return f"{self.file}:{self.line}" if self.line is not None else self.file

    def label(self) -> str | None:
        """`detail`, widened by the capabilities this hop reached."""
        if not self.capabilities:
            return self.detail
        caps = "[" + ", ".join(self.capabilities) + "]"
        return f"{self.detail} {caps}" if self.detail else caps

    def to_json(self) -> dict:
        record: dict = {"name": self.name, "kind": self.kind}
        if self.file is not None:
            record["file"] = self.file
        if self.line is not None:
            record["line"] = self.line
        if self.detail is not None:
            record["detail"] = self.detail
        if self.capabilities:
            record["capabilities"] = list(self.capabilities)
        return record


@dataclass(frozen=True)
class WhyTrace:
    """The evidence for one rejection."""

    kind: str
    subject: str
    steps: tuple[TraceStep, ...] = field(default_factory=tuple)
    shape: str = CHAIN

    def __post_init__(self) -> None:
        # accept any sequence at the call site; store an immutable one
        object.__setattr__(self, "steps", tuple(self.steps))

    def headline(self) -> str:
        template = _HEADLINES.get(self.kind, "why `{subject}` was rejected")
        return template.format(subject=self.subject)

    def path(self) -> list[str]:
        return [step.name for step in self.steps]

    def to_json(self) -> dict:
        record = {
            "kind": self.kind,
            "subject": self.subject,
            "shape": self.shape,
            "steps": [step.to_json() for step in self.steps],
        }
        if self.shape == CHAIN:
            # the chain as plain names, so an agent can match on the
            # derivation without walking `steps`
            record["path"] = self.path()
        return record


def render(trace: WhyTrace | None, indent: str = "  ") -> str:
    """The human rendering: a headline, the chain, then one located line
    per step. Returns "" for no trace, so callers can concatenate blindly."""
    if trace is None or not trace.steps:
        return ""
    lines = [indent + trace.headline() + ":"]
    body_indent = indent + "  "

    if trace.shape == CHAIN and len(trace.steps) > 1:
        chain = " -> ".join(step.name for step in trace.steps)
        tail = trace.steps[-1].label()
        lines.append(body_indent + chain + (f"   ({tail})" if tail else ""))
        body_indent += "  "
        # the chain line already carries the names; the per-step lines exist
        # to pin each hop to a source location. Skip them when there is
        # nothing to pin (compile_source, ambient manifest entries).
        if not any(step.location() for step in trace.steps):
            return "\n".join(lines)

    width = max(len(step.name) for step in trace.steps)
    for step in trace.steps:
        parts = [step.name.ljust(width)]
        location = step.location()
        if location:
            parts.append(location)
        label = step.label()
        if label:
            parts.append(label)
        lines.append((body_indent + "  ".join(parts)).rstrip())
    return "\n".join(lines)
