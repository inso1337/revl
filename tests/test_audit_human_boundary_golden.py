"""Golden over `revl audit`'s HUMAN boundary line (issue #288).

`revl audit` (no `--json`) is a human render, and it is explicitly NOT a
stability contract (docs/interchange-format.md, "The human render is not a
stability contract"). But it was load-bearing for a downstream consumer with no
check on it at all: PR #257 (`a3dc10ad`) added the declared capability token to
the boundary line —

    before:  host code: … write (witnessed, py)
    after:   host code: … write [fs] (witnessed, py)

— which is a strict improvement (the `[fs]` token is what a capability rule
targets), but the classification never moved. A consumer matching the old prose
string stopped matching and read the failed match as "no longer witnessed". No
test would have flagged that the boundary line's shape changed for every scoped
extern in a corpus.

This golden is that check. It pins the `host code:` clause for one corpus that
exercises all four G4 classifications — `pure`, `acquire`, `emission`,
`witnessed` — with the two scopable classes (`emission`, `witnessed`) present
BOTH scoped and bare. A wording change to the render then shows up as a diff on
`EXPECTED_HOST_CODE` in this file, reviewed deliberately, rather than reaching a
consumer silently. It does not freeze the format — it makes changing it visible.

NON-VACUITY (issue #288 exit test — "prove it bites"): re-applying #257's change
means rendering the ` [fs]` token differently on the witnessed/emission line.
Removing the scoped-token render (`e.get("capabilities")` clause in
`__main__._host_extern`) drops `write [fs] …` back to `write …` and turns
`test_host_code_line_is_the_golden` RED; re-adding it turns the golden GREEN.
The neighbouring machine-readable assertion (`test_json_boundary_externs_*`)
pins the SUPPORTED surface a consumer should read instead of the prose.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402

# One corpus reaching every classification. `emission` and `witnessed` are the
# only two classes the grammar lets a capability scope onto (item 343), so they
# appear both scoped (`store [db]`, `write [fs]`) and bare (`send`, `touch`);
# `pure` and `acquire` are never scoped. `open_h` (acquire) and the witnessed
# mutations name their inverses, as G4 requires.
CORPUS = """\
type Handle = { fd: Int }
type W = { path: Str }
type E = { msg: Str }
extern pure fn digest(x: Str) -> Str = @py { return x }
extern pure fn release_h(h: Handle) -> Unit = @py { return None }
extern pure fn restore(w: W) -> Unit = @py { return None }
extern acquire fn open_h() -> Handle undo release_h(result) = @py { return {"fd": 1} }
extern emission fn send(x: Str) -> Str = @py { return x }
extern emission[db] fn store(x: Str) -> Str = @py { return x }
extern witnessed fn touch(p: Str) -> Result[W, E] undo restore(result) = @py { return {"path": p} }
extern witnessed[fs] fn write(p: Str) -> Result[W, E] undo restore(result) = @py { return {"path": p} }

component Worker {
  let h = effect open_h() undo release_h(h)
  emit send(digest("m"))
  emit store("k")
  effect touch("t")
  effect write("w")
}
"""

# THE GOLDEN. Each entry is `name [scope] (class, tier)`; a bare class has no
# `[scope]`. This is the exact clause #257 changed for `write` and `store`.
EXPECTED_HOST_CODE = (
    "host code: "
    "digest (pure, py), "
    "open_h (acquire, py), "
    "release_h (pure, py), "
    "send (emission, py), "
    "store [db] (emission, py), "
    "touch (witnessed, py), "
    "write [fs] (witnessed, py)"
)


def _audit_stdout(tmp_path, capsys, *args) -> str:
    src = tmp_path / "corpus.rvl"
    src.write_text(CORPUS)
    assert main(["audit", str(src), *args]) == 0
    return capsys.readouterr().out


def _host_code_clause(text: str) -> str:
    """Extract the `host code: …` clause from Worker's boundary line, cut at the
    next `; ` clause (the render appends `cardinality: …` after it)."""
    marker = "host code: "
    start = text.index(marker)
    rest = text[start:]
    end = rest.find("; ")
    return rest if end == -1 else rest[:end]


def test_host_code_line_is_the_golden(tmp_path, capsys):
    out = _audit_stdout(tmp_path, capsys)
    assert _host_code_clause(out) == EXPECTED_HOST_CODE


def test_json_boundary_externs_are_the_supported_surface(tmp_path, capsys):
    # The machine-readable equivalent of the golden line — the surface a consumer
    # should read INSTEAD of the prose (docs/interchange-format.md). Every field
    # the human line folds into text is a documented key here: `class` (all four
    # classifications), `capabilities` (the scoped token, present only when
    # scoped), and `backends` (the tier).
    doc = json.loads(_audit_stdout(tmp_path, capsys, "--json"))
    externs = doc["boundary"]["Worker"]["externs"]
    assert externs == [
        {"name": "digest", "class": "pure", "backends": ["py"]},
        {"name": "open_h", "class": "acquire", "backends": ["py"]},
        {"name": "release_h", "class": "pure", "backends": ["py"]},
        {"name": "send", "class": "emission", "backends": ["py"]},
        {"name": "store", "class": "emission", "backends": ["py"],
         "capabilities": ["db"]},
        {"name": "touch", "class": "witnessed", "backends": ["py"]},
        {"name": "write", "class": "witnessed", "backends": ["py"],
         "capabilities": ["fs"]},
    ]


def test_all_four_classifications_are_covered(tmp_path, capsys):
    # Guard the corpus itself: if a future edit drops a classification, this
    # fails rather than letting the golden certify less than it names.
    doc = json.loads(_audit_stdout(tmp_path, capsys, "--json"))
    classes = {e["class"] for e in doc["boundary"]["Worker"]["externs"]}
    assert {"pure", "acquire", "emission", "witnessed"} <= classes
    # both scoped and bare present for the two scopable classes
    externs = {e["name"]: e for e in doc["boundary"]["Worker"]["externs"]}
    assert externs["store"]["capabilities"] == ["db"]   # emission, scoped
    assert "capabilities" not in externs["send"]        # emission, bare
    assert externs["write"]["capabilities"] == ["fs"]   # witnessed, scoped
    assert "capabilities" not in externs["touch"]       # witnessed, bare
