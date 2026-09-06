"""Federated contracts between sovereign compositions (roadmap item 58).

Consumer A pins the *consumer surface* of what it requires from a provider B;
B's CI runs that pinned surface against B's current manifest through the SAME
§5/drift predicate `revl version` uses. A provider change that stays
§5-compatible passes A's contract; a change that removes or narrows a required
op — or gains an emission A's G8 did not account for, or drops the service
entirely — FAILS the contract, naming the exact requirement that drifts.

The cross-check test proves the contract verdict is the real drift predicate's
(`admission._service_compatible`), not a second copy: the same probe item 49/64
used to bind their machinery to the predicate, pointed across the boundary.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.federation import (  # noqa: E402
    CONTRACT_KIND,
    check,
    consumer_surface,
    render,
)
from revl.lower import _service_compatible, _service_from_ir  # noqa: E402
from revl.parser import ServiceDecl  # noqa: E402
from revl.version import diff_services  # noqa: E402


# --- consumer A: requires `Store` from a provider, provides nothing external.
CONSUMER_A = """
service Store {
  fn get(key: Str) -> Str
  emission fn write(key: Str, value: Str)
}
component App requires s: Store {
  emit s.write("k", "v")
}
"""


# --- provider B: a family of `Store` implementations. Every variant keeps a
# valid provider so the composition compiles — only the service *declaration*
# is what the contract check reads across the boundary.
def _provider(service_body: str, extra_impl: str = "") -> str:
    return f"""
service Store {{
{service_body}
}}
component Impl provides s: Store {{
  let store = effect Map.new() undo store.drop()
  provide s {{
    fn get(key) = store.get(key)
    fn write(key, value) {{
      effect store.insert(key, value)
      undo   store.remove(key)
    }}
{extra_impl}
  }}
}}
"""


PROVIDER_BASE = _provider(
    "  fn get(key: Str) -> Str\n"
    "  emission fn write(key: Str, value: Str)")


def _surface(consumer_src: str = CONSUMER_A, consumer: str = "team-A") -> dict:
    return consumer_surface(compile_source(consumer_src), consumer=consumer)


def _check(provider_src: str, consumer_src: str = CONSUMER_A) -> dict:
    return check(_surface(consumer_src), compile_source(provider_src))


# ---------------------------------------------------- the consumer surface

def test_surface_pins_only_external_requirements():
    """The surface carries the full interface of every service A requires from
    outside itself, and nothing A provides internally."""
    src = CONSUMER_A + """
service Local { emission fn tick() }
component Helper requires l: Local provides l2: Local {
  // an honest acquisition bracket; the method-time effect works against it.
  // Spelled `effect Map.new() undo Map.new()` before — an inverse that
  // ACQUIRES a second host Map rather than releasing the first, which a
  // teardown slot now refuses. Nothing this test asserts (the pinned external
  // requirement surface) depends on the shape of Helper's effects.
  let store = effect Map.new() undo store.drop()
  emit l.tick()
  provide l2 {
    fn tick() {
      effect store.insert("k", "v")
      undo   store.remove("k")
    }
  }
}
"""
    surface = consumer_surface(compile_source(src), consumer="team-A")
    assert surface["kind"] == CONTRACT_KIND
    assert surface["consumer"] == "team-A"
    # Store is required-not-provided -> pinned; Local is provided internally
    # (Helper provides l2: Local) -> not an external dependency -> omitted.
    assert set(surface["requires"]) == {"Store"}
    store = surface["requires"]["Store"]
    assert set(store["methods"]) == {"get", "write"}
    assert store["methods"]["write"]["emission"] is True


# ------------------------------------------------- a compatible provider passes

def test_compatible_provider_passes_identical():
    result = _check(PROVIDER_BASE)
    assert result["satisfied"] is True
    assert result["breaks"] == []


def test_compatible_provider_passes_added_method():
    # B adds `size`: additive surface, A never calls it -> contract holds.
    result = _check(_provider(
        "  fn get(key: Str) -> Str\n"
        "  emission fn write(key: Str, value: Str)\n"
        "  fn size() -> Int",
        extra_impl="    fn size() = 0"))
    assert result["satisfied"] is True
    added = [c for c in result["compatible"] if c["method"] == "size"]
    assert added and added[0]["kind"] == "added"


def test_compatible_provider_passes_dropped_emission():
    # B makes `write` pure: strictly purer, safe for the consumer -> holds.
    result = _check(_provider(
        "  fn get(key: Str) -> Str\n"
        "  fn write(key: Str, value: Str)"))
    assert result["satisfied"] is True
    assert result["breaks"] == []


def test_compatible_provider_passes_widened_param():
    # A pins `get(key: Int)`; B widens to `get(key: Float)`. A value A passes
    # (an Int) still fits (contravariant) -> compatible -> contract holds.
    consumer = CONSUMER_A.replace("fn get(key: Str) -> Str",
                                  "fn get(key: Int) -> Str")
    provider = _provider(
        "  fn get(key: Float) -> Str\n"
        "  emission fn write(key: Str, value: Str)")
    result = _check(provider, consumer_src=consumer)
    assert result["satisfied"] is True


# --------------------------------------------- a breaking provider fails, named

def test_breaking_provider_fails_removed_op():
    # B removes `get` — a required op A pinned. Contract BREAKS, naming it.
    provider = _provider("  emission fn write(key: Str, value: Str)")
    provider = provider.replace("    fn get(key) = store.get(key)\n", "")
    result = _check(provider)
    assert result["satisfied"] is False
    broken = [c for c in result["breaks"] if c["method"] == "get"]
    assert broken and broken[0]["kind"] == "removed"


def test_breaking_provider_fails_narrowed_param():
    # B narrows `get`'s parameter Str -> Nat: a value A passes no longer fits.
    provider = _provider(
        "  fn get(key: Nat) -> Str\n"
        "  emission fn write(key: Str, value: Str)")
    result = _check(provider)
    assert result["satisfied"] is False
    broken = next(c for c in result["breaks"] if c["method"] == "get")
    assert broken["kind"] == "signature"


def test_breaking_provider_fails_gained_emission():
    # B makes `get` an `emission` A's G8 never accounted for: same shape, but
    # A's unmarked call site would silently cross the boundary -> BREAKS.
    provider = _provider(
        "  emission fn get(key: Str) -> Str\n"
        "  emission fn write(key: Str, value: Str)")
    provider = provider.replace(
        "    fn get(key) = store.get(key)\n",
        "    fn get(key) {\n"
        "      effect store.insert(key, key)\n"
        "      undo   store.remove(key)\n"
        "      return store.get(key)\n"
        "    }\n")
    result = _check(provider)
    assert result["satisfied"] is False
    broken = next(c for c in result["breaks"] if c["method"] == "get")
    assert broken["kind"] == "emission"
    assert "silently cross the boundary" in broken["reason"]


def test_breaking_provider_fails_missing_service():
    # B no longer provides `Store` at all — a service A requires vanishes.
    provider = """
service Clock { fn now() -> Int }
component C provides c: Clock { provide c { fn now() = 0 } }
"""
    result = _check(provider)
    assert result["satisfied"] is False
    broken = next(c for c in result["breaks"] if c["service"] == "Store")
    assert broken["kind"] == "service-removed" and broken["method"] is None


# --------------------------- item 296: satisfied-via-adapter pin (slice 3)
#
# A MAJOR drift breaks the direct §5 pin, but the same difference may be closed
# by a SAFE adapter the consumer deploys at its boundary. `check` probes the
# LANDED `adapt.bridge_plan` and records the verdict; it never flips
# `satisfied` (proposed-not-silent, §3).

def _bridgeable_provider() -> str:
    """B adds an absence-shaped `options: Opt[Str]` parameter to `get`. The
    consumer's shipped `get(key)` call no longer type-checks (a direct break),
    but a B1 default (`Opt` -> `None`) bridges it with NO opt-in."""
    provider = _provider(
        "  fn get(key: Str, options: Opt[Str]) -> Str\n"
        "  emission fn write(key: Str, value: Str)")
    return provider.replace("fn get(key) = store.get(key)",
                            "fn get(key, options) = store.get(key)")


def test_a_breaking_drift_is_recorded_satisfied_via_adapter():
    result = _check(_bridgeable_provider())
    # the DIRECT pin is still broken — an added required param invalidates the
    # consumer's shipped call site, and `satisfied` stays False.
    assert result["satisfied"] is False
    assert any(c["method"] == "get" for c in result["breaks"])
    # ... but a safe adapter closes it, and the pin records exactly that.
    assert result["verdict"] == "satisfied-via-adapter"
    assert result["unbridgeable"] == []
    bridged = {e["service"]: e for e in result["bridged"]}
    assert set(bridged) == {"Store"}
    store = bridged["Store"]
    # the plan is auditable: the extra parameter is defaulted (B1), and there
    # is no outcome merge to weaken (a clean, opt-in-free bridge).
    assert store["merges"] == []
    get = next(m for m in store["methods"] if m["method"] == "get")
    options = next(s for s in get["steps"] if s["position"] == "options")
    assert options["transformation"] == "B1"
    # the proposed adapter source is rendered for the author to commit.
    assert "component StoreAdapter" in store["source"]


def test_an_unbridgeable_break_stays_broken():
    # B removes `get` entirely: no candidate method to adapt -> method-missing.
    provider = _provider("  emission fn write(key: Str, value: Str)")
    provider = provider.replace("    fn get(key) = store.get(key)\n", "")
    result = _check(provider)
    assert result["satisfied"] is False
    assert result["verdict"] == "broken"
    assert result["bridged"] == []
    refused = next(e for e in result["unbridgeable"] if e["service"] == "Store")
    assert any(r["clause"] == "method-missing" for r in refused["refusals"])


def test_a_removed_service_has_no_adapter():
    # B drops `Store` altogether: an adapter bridges a shape difference, it
    # cannot supply a provider that is gone.
    provider = """
service Clock { fn now() -> Int }
component C provides c: Clock { provide c { fn now() = 0 } }
"""
    result = _check(provider)
    assert result["verdict"] == "broken"
    refused = next(e for e in result["unbridgeable"] if e["service"] == "Store")
    assert "reason" in refused and "cannot" in refused["reason"]


# A consumer requiring `get -> Opt[Str]`; a provider drifting to
# `-> Result[Str, Error]` (opaque error record) breaks the pin and is
# bridgeable only with an explicit outcome-merge opt-in (§2.1 S4c / E2).
CONSUMER_OPT = """
service Store {
  fn get(key: Str) -> Opt[Str]
  emission fn write(key: Str, value: Str)
}
component App requires s: Store {
  emit s.write("k", "v")
}
"""


def _result_provider() -> str:
    provider = "type Error = { code: Str }\n" + _provider(
        "  fn get(key: Str) -> Result[Str, Error]\n"
        "  emission fn write(key: Str, value: Str)")
    return provider.replace("fn get(key) = store.get(key)",
                            "fn get(key) = Ok(store.get(key))")


def test_an_opt_in_only_bridge_is_not_auto_admitted():
    # Folding the candidate's `Result` into the consumer's `Opt` merges an
    # `Err` with a legitimate `None`. The pin does NOT invent that waiver (§3):
    # with no `D`, the bridge is refused and the contract stays broken.
    surface = _surface(CONSUMER_OPT)
    result = check(surface, compile_source(_result_provider()))
    assert result["satisfied"] is False
    assert result["verdict"] == "broken"
    refused = next(e for e in result["unbridgeable"] if e["service"] == "Store")
    assert any(r["clause"] == "outcome-merge" for r in refused["refusals"])


def test_an_opt_in_bridge_is_recorded_when_the_author_supplies_it():
    # The SAME pair, now with the author's total-waiver opt-in threaded through
    # to `bridge_plan`: the pin records it satisfied-via-adapter and names the
    # merge shape, so the deploy story stays honest about what was merged.
    surface = _surface(CONSUMER_OPT)
    result = check(surface, compile_source(_result_provider()),
                   adapt_opt_ins={"Store": {"get":
                                            {"return": {"merge": "total"}}}})
    assert result["satisfied"] is False
    assert result["verdict"] == "satisfied-via-adapter"
    store = next(e for e in result["bridged"] if e["service"] == "Store")
    assert store["merges"] == ["merge-total"]
    assert "Err(_) => None" in store["source"]


def test_render_names_the_adapter_proposal():
    text = render(_check(_bridgeable_provider()), "team-A", "provider-B")
    # the direct pin is broken (existing contract text preserved) ...
    assert "contract BROKEN" in text
    # ... and the safe-adapter proposal is surfaced, proposed not automatic.
    assert "safe adapter available" in text
    assert "PROPOSED" in text
    assert "[adapter] Store" in text


# ----------------------------------- the contract verdict IS the real predicate

def test_contract_agrees_with_the_real_drift_predicate():
    """For every operation A pins that B still declares, the contract's
    break/no-break verdict is exactly `_service_compatible`'s: a reported drift
    is a break, its absence is compatible. This is the item-49/64 probe pointed
    across the boundary — the classification is reused, not re-implemented."""
    provider = _provider(
        "  emission fn get(key: Str) -> Str\n"      # gains emission -> break
        "  fn write(key: Str, value: Str)\n"         # drops emission -> ok
        "  fn size() -> Int",                         # added -> ok
        extra_impl="    fn size() = 0")
    provider = provider.replace(
        "    fn get(key) = store.get(key)\n",
        "    fn get(key) {\n"
        "      effect store.insert(key, key)\n"
        "      undo   store.remove(key)\n"
        "      return store.get(key)\n"
        "    }\n")

    surface = _surface()
    provider_ir = compile_source(provider)
    result = check(surface, provider_ir)
    by_op = {(b["service"], b["method"]): b for b in result["breaks"]}

    pinned = surface["requires"]["Store"]
    old_svc = _service_from_ir("Store", pinned)
    new_svc = _service_from_ir("Store", provider_ir["services"]["Store"])
    shared = set(old_svc.methods) & set(new_svc.methods)
    assert shared  # the probe is only meaningful over shared operations
    for method in shared:
        one_old = ServiceDecl("Store", {method: old_svc.methods[method]}, 0)
        one_new = ServiceDecl("Store", {method: new_svc.methods[method]}, 0)
        drift = _service_compatible(one_new, one_old, providers_retained=False)
        if drift is not None:
            # a break the predicate reports must surface as a contract break
            assert ("Store", method) in by_op
        else:
            # no break: the op is not among the contract's breaks
            assert ("Store", method) not in by_op


def test_diff_services_reused_for_the_classification():
    """The contract's break set is exactly `version.diff_services`' MAJOR
    changes over (pinned, provider) — the same call item 64 makes, restricted
    to what A pins."""
    provider = _provider("  emission fn write(key: Str, value: Str)")
    provider = provider.replace("    fn get(key) = store.get(key)\n", "")
    surface = _surface()
    provider_ir = compile_source(provider)
    changes = diff_services(surface["requires"],
                            {"Store": provider_ir["services"]["Store"]})
    majors = {(c.service, c.method) for c in changes if c.bump == "major"}
    result = check(surface, provider_ir)
    assert {(b["service"], b["method"]) for b in result["breaks"]} == majors


# ------------------------------------------------------------------ the CLI

def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def test_cli_export_round_trips(tmp_path, capsys):
    a = _write(tmp_path, "A.rvl", CONSUMER_A)
    assert main(["contract", "export", str(a), "--consumer", "team-A"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == CONTRACT_KIND
    assert doc["consumer"] == "team-A"
    assert "Store" in doc["requires"]


def test_cli_check_compatible_provider_passes(tmp_path, capsys):
    a = _write(tmp_path, "A.rvl", CONSUMER_A)
    assert main(["contract", "export", str(a)]) == 0
    pinned = tmp_path / "A-pinned.json"
    pinned.write_text(capsys.readouterr().out)

    b = _write(tmp_path, "B.rvl", _provider(
        "  fn get(key: Str) -> Str\n"
        "  emission fn write(key: Str, value: Str)\n"
        "  fn size() -> Int",
        extra_impl="    fn size() = 0"))
    assert main(["contract", "check", "--consumer", str(pinned),
                 "--provider", str(b)]) == 0
    out = capsys.readouterr().out
    assert "contract OK" in out


def test_cli_check_breaking_provider_fails_named(tmp_path, capsys):
    a = _write(tmp_path, "A.rvl", CONSUMER_A)
    assert main(["contract", "export", str(a), "--consumer", "team-A"]) == 0
    pinned = tmp_path / "A-pinned.json"
    pinned.write_text(capsys.readouterr().out)

    breaking = _provider("  emission fn write(key: Str, value: Str)")
    breaking = breaking.replace("    fn get(key) = store.get(key)\n", "")
    b = _write(tmp_path, "B.rvl", breaking)

    # human form: nonzero exit, names the drift
    assert main(["contract", "check", "--consumer", str(pinned),
                 "--provider", str(b)]) == 1
    out = capsys.readouterr().out
    assert "contract BROKEN" in out and "get" in out and "removed" in out

    # --json form: same verdict, machine-readable
    assert main(["contract", "check", "--consumer", str(pinned),
                 "--provider", str(b), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["satisfied"] is False
    assert any(x["method"] == "get" for x in result["breaks"])


def test_cli_check_provider_as_compiled_manifest(tmp_path, capsys):
    # `--provider` accepts a pre-compiled manifest .json, not only sources.
    a = _write(tmp_path, "A.rvl", CONSUMER_A)
    assert main(["contract", "export", str(a)]) == 0
    pinned = tmp_path / "A-pinned.json"
    pinned.write_text(capsys.readouterr().out)

    b = _write(tmp_path, "B.rvl", PROVIDER_BASE)
    assert main(["version", str(b), "--emit-manifest"]) == 0
    manifest = tmp_path / "B.json"
    manifest.write_text(capsys.readouterr().out)

    assert main(["contract", "check", "--consumer", str(pinned),
                 "--provider", str(manifest)]) == 0
    assert "contract OK" in capsys.readouterr().out


def test_cli_check_rejects_wrong_kind_consumer(tmp_path, capsys):
    # a compiled composition is not a consumer surface -> pointed error.
    a = _write(tmp_path, "A.rvl", CONSUMER_A)
    assert main(["compile", str(a), "-o", str(tmp_path / "A-ir.json")]) == 0
    capsys.readouterr()
    b = _write(tmp_path, "B.rvl", PROVIDER_BASE)
    assert main(["contract", "check", "--consumer", str(tmp_path / "A-ir.json"),
                 "--provider", str(b)]) == 1
    assert "not a consumer-surface artifact" in capsys.readouterr().err


def test_cli_check_rejects_audit_doc_provider(tmp_path, capsys):
    # an audit report has no `services` table -> same clear message as version.
    a = _write(tmp_path, "A.rvl", CONSUMER_A)
    assert main(["contract", "export", str(a)]) == 0
    pinned = tmp_path / "A-pinned.json"
    pinned.write_text(capsys.readouterr().out)

    b = _write(tmp_path, "B.rvl", PROVIDER_BASE)
    assert main(["audit", str(b), "--json"]) == 0
    audit = tmp_path / "B-audit.json"
    audit.write_text(capsys.readouterr().out)

    assert main(["contract", "check", "--consumer", str(pinned),
                 "--provider", str(audit)]) == 1
    assert "no `services` table" in capsys.readouterr().err
