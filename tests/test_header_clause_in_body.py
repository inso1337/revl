"""`requires`/`provides` written inside a component body.

Both are clauses of the component header. Written as a body statement, the
parser used to fall through to the generic statement refusal, "expected a
statement (...), found 'requires'", whose hint carried `(G6)`. The structured
record then classified it G6, so its `fix` read "bind the value with `let`,
or wrap the call in `effect ... undo ...`", which is the rewrite for a
different mistake. It is now a dedicated refusal whose `fix` carries the
corrected header line.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402

SERVICES = """service Store { fn get(k: Str) -> Str }
service Show { fn show(k: Str) -> Str }
service Log { fn put(k: Str) -> Int }
"""


def _record(src: str) -> dict:
    with pytest.raises(RevlError) as info:
        compile_source(SERVICES + src, "app.rvl")
    return classify(info.value)


@pytest.mark.parametrize("src, line, header", [
    ("""component C provides front: Show {
  requires store: Store
  provide front { fn show(k) = store.get(k) }
}
""", 5, "component C requires store: Store provides front: Show {"),
    ("""component C {
  requires store: Store
  provides front: Show
  provide front { fn show(k) = store.get(k) }
}
""", 5, "component C requires store: Store provides front: Show {"),
    ("""component C {
  requires store: Store, log: Log
}
""", 5, "component C requires store: Store, log: Log {"),
    ("""component C requires store: Store {
  provides front: Show
  provide front { fn show(k) = store.get(k) }
}
""", 5, "component C requires store: Store provides front: Show {"),
])
def test_a_header_clause_in_the_body_names_the_header(src, line, header):
    record = _record(src)
    kw = "provides" if "{\n  provides" in src else "requires"
    assert record["message"] == (f"`{kw}` is part of the component header, "
                                 f"not a statement in the body of C")
    assert record["line"] == line
    assert record["code"] == "SYNTAX"
    assert record["category"] == "header"
    assert "guarantee" not in record
    assert f"`{header}`" in record["fix"]
    assert header in record["hint"]
    # the G6 rewrite is the advice this replaced, for a different mistake
    assert "`let`" not in record["fix"]
    assert "effect ... undo" not in record["fix"]


def test_the_corrected_header_compiles():
    """The header the fix spells is the program the author meant."""
    record = _record("""component C provides front: Show {
  requires store: Store
  provide front { fn show(k) = store.get(k) }
}
""")
    header = record["fix"].split("`")[1]
    compile_source(SERVICES + header + """
  provide front { fn show(k) = store.get(k) }
}
""", "app.rvl")


def test_any_other_stray_statement_keeps_the_generic_refusal():
    """Only the two header keywords are redirected; the purity refusal and its
    G6 fix still answer every other non-statement."""
    with pytest.raises(RevlError) as info:
        compile_source(SERVICES + """component C requires store: Store {
  store.get("k")
}
""", "app.rvl")
    record = classify(info.value)
    assert record["message"].startswith("expected a statement")
    assert record["code"] == "G6"
