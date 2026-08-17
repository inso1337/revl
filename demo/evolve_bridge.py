"""Host side of the self-evolution demo: the model, and the compiler.

The emitted components import this module from their `@py` extern bodies —
it is the only privileged code in the experiment, and it is exactly what
`revl audit` reports as host code.

Two design points worth stating, because they are what make the loop safe
rather than merely clever:

* **`propose` never swaps synchronously.** A component asking to replace the
  composition it belongs to would be asking to be disposed in the middle of
  its own method. Instead the candidate is *admitted* (the same gate a
  human's `revl compile` runs, against the running manifest) and queued; the
  driver applies it once the call has returned. Rejected candidates never
  reach the queue.
* **The assistant is a swappable provider.** `complete()` is a deterministic
  stub here so the demo is reproducible in CI. Pointing it at a real model is
  a provider swap, not a code change — which is the paradigm's own claim
  about model routing.
"""

from __future__ import annotations

# set by the driver before the composition is loaded
SESSION = None
PENDING: list[str] = []
LOG: list[str] = []

# what a real assistant would have to produce; the stub returns it verbatim
GREETER_V2 = '''component GreeterV1 provides greeter: Greeter {
  provide greeter {
    fn greet(name) = "hello, " + name + "! (v2)"
  }
}'''

# a plausible-looking answer that does not compile — the case that matters
GREETER_BROKEN = '''component GreeterV1 provides greeter: Greeter {
  provide greeter {
    fn greet(name) = 42
  }
}'''


def _sources(candidate: str) -> str:
    """The candidate replaces GreeterV1 inside the full composition."""
    from pathlib import Path

    base = (Path(__file__).parent / "components" / "evolve.rvl").read_text(encoding="utf-8")
    start = base.index("component GreeterV1")
    end = base.index("// --- the evolver")
    return base[:start] + candidate + "\n\n" + base[end:]


def complete(prompt: str) -> str:
    """The 'model'. Deterministic: the prompt selects a canned answer."""
    LOG.append(f"assistant.complete({prompt!r})")
    return GREETER_BROKEN if "broken" in prompt else GREETER_V2


def check(source: str) -> str:
    """Compile a candidate. Empty string means it compiles."""
    from revl import compile_files  # noqa: PLC0415 — host code, not revl code
    from revl.errors import RevlError

    try:
        compile_files(["<candidate>.rvl"], sources={_abs("<candidate>.rvl"): _sources(source)})
    except RevlError as error:
        LOG.append(f"runtime.check -> REJECTED: {error.message}")
        return str(error)
    LOG.append("runtime.check -> compiles")
    return ""


def propose(source: str) -> str:
    """Admit a candidate against the RUNNING composition, then queue it.

    Returns the verdict as text — the evolver (and anything reading the
    trace) learns whether its proposal was accepted without being able to
    bypass the decision.
    """
    from revl import compile_source  # noqa: PLC0415
    from revl.errors import RevlError

    full = _sources(source)
    try:
        compile_source(full, "<candidate>.rvl", manifest=SESSION.ir)
    except RevlError as error:
        LOG.append(f"runtime.propose -> REFUSED: {error.message}")
        return f"refused: {error.message}"
    PENDING.append(full)
    LOG.append("runtime.propose -> admitted, queued for the next generation")
    return "admitted"


def _abs(name: str) -> str:
    import os

    return os.path.abspath(name)


def reset() -> None:
    PENDING.clear()
    LOG.clear()
