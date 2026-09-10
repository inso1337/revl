"""The composition SLO contract and its rollout gate (roadmap item 473, issue
#825).

The item asks for a composition-level `slo { ... }` contract the runtime
monitors, refusing a risky rollout when predicted budgets or SLOs would fail.
Verified against the tree rather than assumed, the RUNTIME half of that item has
no sink: there is no `slo` vocabulary anywhere, no generation receipt to tie a
receipt to, and `why_runtime.SCHEMA_VERSION = 2` carries LOAD/WITHDRAW/EMIT
only. Building a monitor would mean inventing the machinery, not reading it, so
what lands here is the half the tree can actually hold: the CONTRACT (a closed
datum registry with units fixed at the surface, carried into the IR) and the
COMPILE-TIME GATE (a rollout the composition has already predicted it will
breach is refused).

The prediction is not invented either. It is the ceiling item 260 already
declares: a provider declaring `emission[net(time="2s")]` says a crossing on
that route may take up to two seconds, so a document promising
`p95_latency: 250ms` has contradicted itself. That is the exit test the item
names ("a rollout predicted to breach a declared SLO is refused"), and it is
the only reading of "predicted" the language has a number for.

`docs/design/473-slo-rollout-gate.md` records the design and the left-outs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402
from revl.composition import compile_composition, resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402

# A provider that crosses `net` under a declared ceiling. This is the ONLY
# number the gate reads, and it is item 260's number, not a new one.
BILLING = """
service Billing {
  emission[net(time="2s", requests=100)] fn charge(account: Str, amount: Int) -> Bool
}

component BillingSvc provides billing: Billing {
  provide billing {
    fn charge(account, amount) = true
  }
}
"""

CONSUMER = """
service Checkout {
  emission fn pay(account: Str) -> Bool
}

component CheckoutSvc requires billing: Billing provides checkout: Checkout {
  provide checkout {
    fn pay(account) = emit billing.charge(account, 100)
  }
}
"""

# A second provider whose ceiling is wider than the base document promises. The
# fold gate is what catches it.
SLOW = """
service Slow {
  emission[net(time="10s")] fn go(a: Str) -> Bool
}

component SlowSvc provides slow: Slow {
  provide slow {
    fn go(a) = true
  }
}
"""


def write(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        target = tmp_path / f"{name}.rvl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return tmp_path


def project(tmp_path: Path, slo: str, *, stack: str = "",
            **files: str) -> Path:
    """A two-row composition whose `slo` block is `slo` (verbatim, braces
    included by the caller), over `billing.rvl` and `consumer.rvl`."""
    write(tmp_path, billing=BILLING, consumer=CONSUMER, **files)
    doc = tmp_path / "base.rvl"
    doc.write_text(f"""
composition base {{
  {slo}
{stack}  row @checkout from "consumer.rvl" provides checkout
  row @billing from "billing.rvl" provides billing
}}
""")
    return doc


def resolve(tmp_path: Path, doc: str = "base"):
    return resolve_file(str(tmp_path / f"{doc}.rvl"), str(tmp_path))


def parse_composition(slo_body: str):
    return Parser(f"composition c {{ {slo_body} }}", "t.rvl").parse() \
        .compositions[0]


# ----------------------------------------------------------- the surface (parsing)

def test_the_slo_block_parses_onto_canonical_units():
    """One block, five datums, each canonicalized by its value kind: a duration
    to milliseconds, a rate to percent, a task count to a task count. The unit
    is fixed HERE so it can never be re-read differently downstream."""
    decl = parse_composition(
        "slo { p95_latency: 250ms, success_rate: 99.5, recovery_time: 30s, "
        "approval_wait: 1m, max_pending_tasks: 8 }")
    assert decl.slo == [
        ("p95_latency", 250, 1),
        ("success_rate", 99.5, 1),
        ("recovery_time", 30000, 1),
        ("approval_wait", 60000, 1),
        ("max_pending_tasks", 8, 1),
    ]


def test_a_bare_duration_reads_as_seconds_like_every_other_duration_slot():
    """`p95_latency: 2` is two SECONDS, because every other duration slot in the
    language (`cache ... ttl`, `liveness`) reads a bare number as seconds. A
    surface that read it as milliseconds would make the same literal mean two
    things two clauses apart."""
    decl = parse_composition("slo { p95_latency: 2 }")
    assert decl.slo == [("p95_latency", 2000, 1)]


def test_slo_is_a_contextual_keyword_and_ordinary_as_a_name():
    """`slo` heads a clause only in the composition clause-head slot, so the
    lexer's KEYWORDS set is untouched and a program using `slo` as an ordinary
    name still parses. This is the discipline `remote`/`place`/`stack` keep, and
    it is what keeps the self-host lexer in sync for free."""
    program = Parser("""
service S {
  emission fn go(slo: Str) -> Str
}

component C provides svc: S {
  provide svc { fn go(slo) = slo }
}
""", "t.rvl").parse()
    assert list(program.services[0].methods["go"].params)[0][0] == "slo"
    assert program.components[0].name == "C"


@pytest.mark.parametrize("body, needle", [
    ("slo { }", "declares no target"),
    ("slo { throughput: 100 }", "unknown SLO datum `throughput`"),
    ("slo { p95_latency: 0 }", "positive duration"),
    ("slo { p95_latency: -1s }", "expected a `p95_latency` duration"),
    ("slo { success_rate: 0 }", "within (0, 100]"),
    ("slo { success_rate: 100.1 }", "within (0, 100]"),
    ("slo { max_pending_tasks: 0 }", "positive task count"),
    ("slo { max_pending_tasks: 1.5 }", "task count"),
    ("slo { p95_latency: 1s, p95_latency: 2s }", "duplicate SLO datum"),
    ("slo { p95_latency: 1s } slo { success_rate: 99 }", "second `slo` block"),
])
def test_an_empty_or_contradictory_block_is_refused(body, needle):
    """Every refusal here is a document that does not make the promise it reads
    as making: an empty block, an unknown datum the registry would silently
    drop, a target outside its own domain, or a datum declared twice."""
    with pytest.raises(RevlError) as exc:
        parse_composition(body)
    assert needle in str(exc.value)


def test_the_unknown_datum_refusal_names_the_whole_registry():
    """A closed registry is only honest if the refusal says where the closed set
    is, so the author does not have to guess a spelling."""
    with pytest.raises(RevlError) as exc:
        parse_composition("slo { latency: 250ms }")
    for datum in ("p95_latency", "success_rate", "recovery_time",
                  "approval_wait", "max_pending_tasks"):
        assert f"`{datum}`" in exc.value.hint


def test_the_clause_refusal_mentions_slo():
    """The composition clause list is the discoverability surface: an author who
    does not know the block exists reads it off the refusal."""
    with pytest.raises(RevlError) as exc:
        Parser("composition c { nope }", "t.rvl").parse()
    assert "`slo`" in str(exc.value)
    assert "slo {" in (exc.value.hint or "")


# ---------------------------------------------------------- the gate (the exit test)

def test_a_rollout_predicted_to_breach_a_declared_slo_is_refused(tmp_path):
    """THE EXIT TEST. `billing.rvl` declares `emission[net(time="2s",
    requests=100)]`, so the composition itself predicts up to two seconds and
    one hundred crossings on that route. A document promising a 250ms latency
    and 50 pending tasks has promised something its own declaration contradicts,
    and it is refused as a G4 rather than admitted and discovered later."""
    doc = project(tmp_path, "slo { p95_latency: 250ms, max_pending_tasks: 50 }")
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    assert exc.value.code == "G4"
    assert exc.value.category == "slo"
    assert "p95_latency: 250" in exc.value.message
    assert "time=2000" in exc.value.message
    assert "billing.rvl" in exc.value.hint

    # the count datum refuses off the same declaration, on the OTHER parameter
    doc2 = project(tmp_path, "slo { max_pending_tasks: 50 }")
    with pytest.raises(RevlError) as exc2:
        resolve_file(str(doc2), str(tmp_path))
    assert "max_pending_tasks: 50" in exc2.value.message
    assert "calls=100" in exc2.value.message


def test_the_boundary_is_inclusive_so_a_target_at_the_ceiling_resolves(tmp_path):
    """The comparison is `target >= ceiling`. This is the mutation-sensitive
    pin: flipping the operator, or dropping the equality, makes exactly this
    document refuse, and the test above still passes."""
    doc = project(tmp_path, "slo { p95_latency: 2s, max_pending_tasks: 100 }")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.slo == {"p95_latency_ms": (2000, 3),
                         "max_pending_tasks": (100, 3)}

    # one millisecond under, and one task under: both refuse
    for body, needle in (("p95_latency: 1999ms", "time=2000"),
                         ("max_pending_tasks: 99", "calls=100")):
        doc = project(tmp_path, f"slo {{ {body} }}")
        with pytest.raises(RevlError) as exc:
            resolve_file(str(doc), str(tmp_path))
        assert needle in exc.value.message


@pytest.mark.parametrize("body, key, value", [
    ("success_rate: 99.5", "success_rate_pct", 99.5),
    ("recovery_time: 30s", "recovery_time_ms", 30000),
    ("approval_wait: 1m", "approval_wait_ms", 60000),
])
def test_a_datum_the_language_cannot_bound_is_carried_ungated(tmp_path, body,
                                                             key, value):
    """`success_rate`, `recovery_time` and `approval_wait` have no
    declaration-owned bound in this language version: nothing in the tree
    declares any of the three. They are admitted as the CONTRACT and carried
    into the IR, and they are NOT statically gated, because gating them would
    mean inventing the quantity rather than reading one. This test pins that
    deliberate silence, so the day a bound appears something says so."""
    doc = project(tmp_path, f"slo {{ {body} }}")
    assert resolve_file(str(doc), str(tmp_path)).slo == {key: (value, 3)}


def test_a_composition_declaring_no_ceiling_is_not_gated(tmp_path):
    """The gate reads declarations, so a composition whose sources declare no
    `emission[...]` ceiling has nothing to refuse on. The contract is still
    carried: it is the runtime's, not the compiler's, and pretending otherwise
    would be a second invention."""
    write(tmp_path, plain="""
service Plain { emission fn go(a: Str) -> Bool }

component PlainSvc provides plain: Plain {
  provide plain { fn go(a) = true }
}
""")
    doc = tmp_path / "base.rvl"
    doc.write_text("""
composition base {
  slo { p95_latency: 250ms, success_rate: 99.9 }
  row @plain from "plain.rvl" provides plain
}
""")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.slo == {"p95_latency_ms": (250, 3),
                         "success_rate_pct": (99.9, 3)}


# --------------------------------------------------------------- the fold path

def test_the_fold_gates_a_layer_that_widens_a_ceiling(tmp_path):
    """`fold` runs the same gate over the FOLDED rows, so a stack layer cannot
    widen its way out of the base document's contract: the provider the layer
    adds declares `time="10s"` while the document promises five seconds."""
    write(tmp_path, slow=SLOW, **{"layers/slow": """
layer Slow for base {
  add row @slow from "../slow.rvl" provides slow
}
"""})
    doc = project(tmp_path, "slo { p95_latency: 5s }",
                  stack='  stack "layers/slow.rvl"\n')
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    assert exc.value.category == "slo"
    assert "time=10000" in exc.value.message

    # the same document with a target above the widened ceiling folds and holds
    doc2 = project(tmp_path, "slo { p95_latency: 10s }",
                   stack='  stack "layers/slow.rvl"\n')
    table = resolve_file(str(doc2), str(tmp_path))
    assert table.slo == {"p95_latency_ms": (10000, 3)}
    assert ".::@slow" in [row.qualified for row in table.rows]


# ------------------------------------------------------------------ the IR and panel

def test_the_contract_is_carried_into_the_ir_under_unit_bearing_keys(tmp_path):
    """The IR keys carry their unit (`p95_latency_ms`), because a rollout
    decision reads the contract as a document and an unlabelled `250` cannot be
    told from 250 seconds once it leaves the file."""
    doc = project(tmp_path, "slo { p95_latency: 5s, success_rate: 99.5, "
                            "max_pending_tasks: 150 }")
    document = compile_composition(str(doc), str(tmp_path))
    assert document["rows"]["slo"] == {"p95_latency_ms": 5000,
                                       "success_rate_pct": 99.5,
                                       "max_pending_tasks": 150}
    # and it is the same object on the table, with the line it was declared on
    table = resolve_file(str(doc), str(tmp_path))
    assert table.slo_contract() == {
        "p95_latency_ms": {"target": 5000, "line": 3},
        "success_rate_pct": {"target": 99.5, "line": 3},
        "max_pending_tasks": {"target": 150, "line": 3},
    }


def test_a_composition_without_an_slo_block_emits_no_slo_key(tmp_path):
    """Additive and conditional, the `liveness`/`cache` discipline: a document
    that declares no contract emits the S1 document byte for byte, so the whole
    feature is inert unless a program opts in."""
    doc = project(tmp_path, "")
    table = resolve_file(str(doc), str(tmp_path))
    assert table.slo == {}
    assert "slo" not in table.to_ir()
    assert "slo" not in compile_composition(str(doc), str(tmp_path))["rows"]


def test_the_panel_shows_the_contract_and_only_when_declared(tmp_path, capsys):
    """`revl composition` prints the contract with the composition's identity,
    and prints nothing extra for a composition that declares none, so no
    existing panel output changes."""
    doc = project(tmp_path, "slo { p95_latency: 5s }")
    assert main(["composition", str(doc), "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "SLO" in out and "p95_latency_ms" in out and "5000" in out

    plain = project(tmp_path, "")
    assert main(["composition", str(plain), "--root", str(tmp_path)]) == 0
    assert "SLO" not in capsys.readouterr().out


def test_the_cli_refuses_a_predicted_breach(tmp_path, capsys):
    """The refusal reaches the operator as a nonzero exit and a message on
    stderr, and `--json` never emits a document for a refused rollout."""
    doc = project(tmp_path, "slo { p95_latency: 250ms }")
    assert main(["composition", str(doc), "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "p95_latency" in err and "time=2000" in err

    assert main(["composition", str(doc), "--root", str(tmp_path),
                 "--json"]) == 1
    assert capsys.readouterr().out == ""


def test_the_contract_survives_a_json_round_trip(tmp_path, capsys):
    """`--json` is the machine surface and ships the unit-bearing keys."""
    doc = project(tmp_path, "slo { recovery_time: 30s }")
    assert main(["composition", str(doc), "--root", str(tmp_path),
                 "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["slo"] == {
        "recovery_time_ms": 30000}
