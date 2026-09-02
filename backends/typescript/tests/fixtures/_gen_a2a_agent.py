"""Regenerator for the a2a-to-ts fixture pair — NOT part of the build.

Issue #251. `revl import a2a --backend ts` emitted `await` inside a
*synchronous* ts function, and neither typecheck gate saw it: #219 widened the
gate over every module under `tests/generated/`, #238 over the hand-written
sources, and **no fixture exercised the a2a-to-ts path at all**. A gate covers
what it is pointed at, so this pair points it at the importer's own output.

The chain is the real one, end to end, with nothing hand-assembled:

    a2a_agent_card.json  --(revl import a2a --backend ts)-->  a2a_agent.rvl
                         --(revl compile)-------------------> a2a_agent.ir.json
                         --(backends/typescript/emit.py)-----> tests/generated/a2a_agent.ts
                         --(scripts/typecheck-generated.mjs)-> tsc

`tests/test_import_a2a.py::test_the_ts_backend_fixture_is_current` re-runs the
first two steps and compares, so an importer change that moves the emitted
source cannot leave this fixture — and therefore the gate — pointed at output
nobody emits any more.

Run by hand after an intentional importer change:

    python3 backends/typescript/tests/fixtures/_gen_a2a_agent.py
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.import_a2a import import_a2a, load_card  # noqa: E402

CARD = HERE / "a2a_agent_card.json"
RVL = HERE / "a2a_agent.rvl"
IR = HERE / "a2a_agent.ir.json"

#: The card's name as the generated header records it. Spelled repo-relative on
#: purpose: `import_a2a_file` would bake this checkout's absolute path into the
#: committed fixture, and the currency test would then only ever pass on the
#: machine that last regenerated it.
CARD_NAME = "backends/typescript/tests/fixtures/a2a_agent_card.json"


def generate() -> tuple[str, dict]:
    """The importer's ts-backend output for the card, and its compiled IR.

    Shared with the currency test so both sides run one recipe rather than two
    that can drift.
    """
    text = CARD.read_text(encoding="utf-8")
    source = import_a2a(load_card(text, filename=CARD_NAME), filename=CARD_NAME,
                        backend="ts", source=text)
    return source, compile_source(source, "a2a_agent.rvl")


if __name__ == "__main__":
    source, ir = generate()
    RVL.write_text(source, encoding="utf-8")
    IR.write_text(json.dumps(ir, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {RVL.name} and {IR.name}")
