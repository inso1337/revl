"""A revl composition that rewrites itself, start to finish, in memory.

    backends/python/.venv/bin/python demo/self_evolve.py

The loop: a running system asks a model for a new version of one of its own
components, the compiler admits or refuses the answer against what is
currently running, and an admitted answer becomes the next generation —
without any of it touching the filesystem.

The interesting half is the refusal. A model that returns plausible nonsense
is the expected case, not the exceptional one, so the demo runs that path too
and shows the composition still serving afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent
for path in (str(ROOT / "src"), str(DEMO)):
    if path not in sys.path:
        sys.path.insert(0, path)

import evolve_bridge  # noqa: E402
from revl import compile_files  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

SOURCE = DEMO / "components" / "evolve.rvl"


def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _greet(session: Session) -> str:
    return session.call("greeter", "greet", ["ada"])["result"]


def _apply_pending(session: Session) -> bool:
    """Apply a queued generation, if the runtime admitted one."""
    if not evolve_bridge.PENDING:
        return False
    candidate = evolve_bridge.PENDING.pop()
    ir = compile_files(["<candidate>.rvl"],
                       sources={evolve_bridge._abs("<candidate>.rvl"): candidate})
    session.swap(ir)
    return True


def main() -> int:
    session = Session()
    evolve_bridge.SESSION = session
    evolve_bridge.reset()

    _rule("1. boot the composition (nothing on disk but the source)")
    ir = compile_files([str(SOURCE)])
    state = session.load(ir)
    evolve_bridge.SESSION = session
    print("   load order :", " -> ".join(state["loadOrder"]))
    print("   provides   :", ", ".join(state["providedKeys"]))
    print("   greeter    :", _greet(session))

    _rule("2. the system asks a model to rewrite one of its own components")
    verdict = session.call("evolve", "once", ["improve the greeter"])["result"]
    print("   verdict    :", verdict)
    for line in evolve_bridge.LOG:
        print("   trace      :", line)
    evolve_bridge.LOG.clear()

    _rule("3. apply the admitted generation")
    if _apply_pending(session):
        print("   swapped    : yes")
    print("   greeter    :", _greet(session), " <- the running system changed itself")

    _rule("4. the model returns something that does not compile")
    verdict = session.call("evolve", "once", ["broken idea"])["result"]
    print("   verdict    :", verdict.split(chr(10))[0])
    for line in evolve_bridge.LOG:
        print("   trace      :", line.split(chr(10))[0])
    print("   queued     :", len(evolve_bridge.PENDING), "(a refused candidate never reaches the queue)")
    print("   greeter    :", _greet(session), " <- still serving the last good generation")

    _rule("5. tear down and prove nothing was left behind")
    report = session.unload()
    print("   no residue :", report["noResidue"])
    print("   checks     :", report["checks"])

    print("\nthe compiler decided every step; the model only proposed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
