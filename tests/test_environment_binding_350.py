"""Item 350 — the environment contract: `boot component` + `revl run --env`.

The gap this closes (found by the revl-harness dogfood goal): the port, the auth
token, the data dir and the model provider are read BEFORE the composition
exists, so they arrive from the host by construction. What was missing was a
place to write that down. The arrival point was an undeclared host-authored
`--config` map that could rewrite any field of any component, and nothing in the
source, the IR or the audit said which values were environment.

A `boot component`'s `config {}` block IS the environment contract. Four rules
make it closed rather than decorative, all enforced before a runtime is imported:

  1. one door       — a `--config` table naming the boot component is refused;
                      its values arrive only through `--env`.
  2. no silent      — an `--env` key the contract does not declare is refused,
     arrival          not carried through and not dropped.
  3. no missing     — a required (non-defaulted) field with no injected value
     value            refuses the boot.
  4. bounds         — a value outside a field's declared `under "<prefix>"` /
                      `in [...]` bound is refused.

Rule 4 is the authority close, and the reason this item is not just paperwork.
An environment value is DATA, so it adds no capability — but where it lands in a
resource-scoped position it is the thing that SPELLS the scope: a data dir
becomes the fs path a component actually reaches, a provider identity becomes
the network destination an emission actually posts to. Both widen the real reach
while the declared capability set is unchanged, so nothing in the audit moves.
"""

from __future__ import annotations

import json

import pytest

from revl.audit_diff import audit_report, crossings, diff_crossings
from revl.compiler import compile_source
from revl.errors import RevlError
from revl.run import (
    _env_contract_problem,
    _load_env,
    _merge_env,
    _required_config_problem,
)

BOOT = """
service Env {
  fn data_root() -> Str
  fn port() -> Int
}

boot component HarnessBoot provides env: Env {
  config {
    data_dir: Str under "./.harness-data",
    port: Int = 8099,
    model: Str in ["mock", "real", "engine"] = "mock",
  }

  provide env {
    fn data_root() = config.data_dir
    fn port()      = config.port
  }
}
"""

PLAIN = """
service Env { fn data_root() -> Str }

component Ordinary provides env: Env {
  config { data_dir: Str = "./data" }
  provide env { fn data_root() = config.data_dir }
}
"""


def _ir(src: str) -> dict:
    return compile_source(src, "boot.rvl")


# ---------------------------------------------------------------- declaration


def test_a_boot_component_is_marked_in_the_ir_and_the_manifest():
    ir = _ir(BOOT)
    comp = ir["components"][0]
    assert comp["boot"] is True
    entry = ir["manifest"]["components"][0]
    assert entry["boot"] is True


def test_an_ordinary_composition_carries_no_boot_key_anywhere():
    """Conditional presence: a composition that declares no boot component is
    byte-identical through the IR, the manifest and the audit."""
    ir = _ir(PLAIN)
    assert "boot" not in ir["components"][0]
    assert "boot" not in ir["manifest"]["components"][0]
    assert "env" not in audit_report(ir)
    assert not any(token.startswith("env:") for token in crossings(audit_report(ir)))


def test_boot_is_still_usable_as_an_ordinary_identifier():
    """`boot` is a CONTEXTUAL keyword — it heads a declaration only immediately
    before `component`, so no program that already used the name breaks."""
    ir = _ir("""
service Env { fn a() -> Str }
component C provides e: Env {
  config { boot: Str = "yes" }
  provide e {
    fn a() {
      let boot = config.boot
      return boot
    }
  }
}
""")
    assert "boot" not in ir["components"][0]


def test_a_composition_declares_at_most_one_boot_component():
    with pytest.raises(RevlError) as caught:
        _ir("""
service Env { fn a() -> Str }
service Env2 { fn b() -> Str }
boot component B1 provides e1: Env {
  config { x: Str }
  provide e1 { fn a() = config.x }
}
boot component B2 provides e2: Env2 {
  config { y: Str }
  provide e2 { fn b() = config.y }
}
""")
    assert "at most one `boot` component" in str(caught.value)
    assert "B1, B2" in str(caught.value)


def test_a_boot_component_cannot_be_spawned():
    """A spawn's config is an author-written `with { … }` no admission check
    bounds, so spawned instances would bypass the contract's declared bounds."""
    with pytest.raises(RevlError) as caught:
        _ir("""
service Env { fn a() -> Str }
boot component Boot provides e: Env {
  config { x: Str }
  provide e { fn a() = config.x }
}
component Super {
  let inst = effect spawn Boot with { x: "hi" } undo inst.dispose()
}
""")
    assert "cannot be spawned" in str(caught.value)


def test_a_bound_outside_a_boot_component_is_refused():
    """Unenforced grammar is the fail-open shape this item removes: a bound is a
    check on the value the HOST injects, and host injection is contract-declared
    only for the boot component."""
    with pytest.raises(RevlError) as caught:
        _ir("""
service Env { fn a() -> Str }
component Ordinary provides e: Env {
  config { path: Str under "./data" }
  provide e { fn a() = config.path }
}
""")
    assert "declares a bound outside a `boot` component" in str(caught.value)


def test_an_empty_admissible_set_is_refused():
    with pytest.raises(RevlError) as caught:
        _ir("""
service Env { fn a() -> Str }
boot component Boot provides e: Env {
  config { model: Str in [] }
  provide e { fn a() = config.model }
}
""")
    assert "empty `in [...]` bound" in str(caught.value)


# ------------------------------------------------------------------ admission


def test_rule_1_the_contract_has_one_door():
    ir = _ir(BOOT)
    problem = _env_contract_problem(ir, {}, {"HarnessBoot": {"data_dir": "/etc"}})
    assert problem is not None
    assert "arrive through --env, not --config" in problem


def test_rule_1_leaves_other_components_config_alone():
    ir = _ir(BOOT + """
component Other requires env: Env {
  config { note: Str = "x" }
}
""")
    assert _env_contract_problem(ir, {}, {"Other": {"note": "y"}}) is None


def test_rule_2_an_undeclared_env_key_is_refused_not_ignored():
    ir = _ir(BOOT)
    problem = _env_contract_problem(
        ir, {"endpoint": "http://elsewhere.example"}, {})
    assert problem is not None
    assert 'does not declare "endpoint"' in problem


def test_rule_3_a_missing_required_field_refuses_and_names_the_env_door():
    ir = _ir(BOOT)
    problem = _required_config_problem(ir, {})
    assert problem is not None
    assert 'HarnessBoot is missing required --env "data_dir"' in problem
    assert "--env <file>" in problem


def test_rule_4_a_path_that_walks_out_of_its_prefix_is_refused():
    ir = _ir(BOOT)
    problem = _env_contract_problem(
        ir, {"data_dir": "./.harness-data/../../etc"}, {})
    assert problem is not None
    assert "walks up out of the prefix" in problem


def test_rule_4_an_absolute_path_under_a_relative_prefix_is_refused():
    ir = _ir(BOOT)
    problem = _env_contract_problem(ir, {"data_dir": "/etc/shadow"}, {})
    assert problem is not None
    assert "absolute" in problem


def test_rule_4_a_sibling_directory_sharing_a_prefix_STRING_is_refused():
    """`./.harness-data-evil` starts with the prefix as a STRING but is not
    inside it as a PATH."""
    ir = _ir(BOOT)
    problem = _env_contract_problem(ir, {"data_dir": "./.harness-data-evil"}, {})
    assert problem is not None
    assert "outside the prefix" in problem


def test_rule_4_a_path_inside_the_prefix_is_admitted():
    ir = _ir(BOOT)
    assert _env_contract_problem(
        ir, {"data_dir": "./.harness-data/live/sessions"}, {}) is None
    assert _env_contract_problem(ir, {"data_dir": "./.harness-data"}, {}) is None


def test_rule_4_an_unlisted_provider_is_refused():
    """The authority case the item names: 'which model provider' chooses a
    network destination, so an unbounded env string re-points an emission."""
    ir = _ir(BOOT)
    problem = _env_contract_problem(
        ir, {"data_dir": "./.harness-data", "model": "attacker-endpoint"}, {})
    assert problem is not None
    assert "is not one of them" in problem


def test_rule_4_a_listed_provider_is_admitted():
    ir = _ir(BOOT)
    assert _env_contract_problem(
        ir, {"data_dir": "./.harness-data", "model": "real"}, {}) is None


def test_an_injected_value_of_the_wrong_type_is_refused():
    ir = _ir(BOOT)
    problem = _env_contract_problem(
        ir, {"data_dir": "./.harness-data", "port": "8099"}, {})
    assert problem is not None
    assert "declared Int and the injected value is not" in problem


def test_a_bool_does_not_pass_as_an_int():
    ir = _ir(BOOT)
    problem = _env_contract_problem(
        ir, {"data_dir": "./.harness-data", "port": True}, {})
    assert problem is not None
    assert "declared Int" in problem


def test_a_refusal_never_echoes_the_injected_value():
    """A contract field may be a credential; a diagnostic is the one place a
    preflight could leak one. Names, types and author-written bounds are source
    and are named freely; values never are."""
    canary = "./.harness-data/../../var/secrets/tenant-9f2c-prod-key"
    ir = _ir(BOOT)
    problem = _env_contract_problem(ir, {"data_dir": canary}, {})
    assert problem is not None
    assert "tenant-9f2c" not in problem
    assert canary not in problem


def test_env_without_a_boot_component_is_refused():
    ir = _ir(PLAIN)
    problem = _env_contract_problem(ir, {"data_dir": "./x"}, {})
    assert problem is not None
    assert "declares no `boot` component" in problem


def test_a_boot_free_composition_with_no_env_is_untouched():
    ir = _ir(PLAIN)
    assert _env_contract_problem(ir, {}, {}) is None
    assert _merge_env(ir, {}, {"Ordinary": {"data_dir": "./x"}}) == {
        "Ordinary": {"data_dir": "./x"}}


def test_merge_seats_env_values_in_the_boot_components_table():
    ir = _ir(BOOT)
    merged = _merge_env(ir, {"data_dir": "./.harness-data", "model": "real"}, {})
    assert merged == {"HarnessBoot": {"data_dir": "./.harness-data",
                                      "model": "real"}}


def test_an_env_file_must_be_flat(tmp_path):
    nested = tmp_path / "env.toml"
    nested.write_text("[HarnessBoot]\ndata_dir = \"./x\"\n")
    with pytest.raises(RevlError) as caught:
        _load_env(str(nested))
    assert "flat table of `name = value`" in str(caught.value)


def test_an_env_file_loads_from_toml_and_json(tmp_path):
    toml = tmp_path / "env.toml"
    toml.write_text('data_dir = "./.harness-data"\nport = 9000\n')
    js = tmp_path / "env.json"
    js.write_text(json.dumps({"data_dir": "./.harness-data", "port": 9000}))
    assert _load_env(str(toml)) == _load_env(str(js))


# ----------------------------------------------------------------- audit/diff


def test_the_audit_publishes_the_contract_but_never_a_value():
    audit = audit_report(_ir(BOOT))
    table = audit["env"]
    assert table["component"] == "HarnessBoot"
    rows = {row["name"]: row for row in table["fields"]}
    assert rows["data_dir"]["required"] is True
    assert rows["data_dir"]["bound"] == {"kind": "under",
                                         "prefix": "./.harness-data"}
    assert rows["port"]["required"] is False
    # UNBOUNDED is stated explicitly rather than left unsaid
    assert rows["port"]["bound"] is None
    assert rows["model"]["bound"]["kind"] == "in"


def test_a_secret_contract_field_is_marked_and_still_valueless():
    audit = audit_report(_ir("""
service Env { fn token() -> Str }
boot component Boot provides env: Env {
  config { auth_token: Secret[Str] }
  provide env { fn token() = config.auth_token }
}
"""))
    row = audit["env"]["fields"][0]
    assert row["name"] == "auth_token" and row["secret"] is True
    assert "value" not in row


def test_adding_a_contract_field_is_a_widening():
    before = audit_report(_ir(BOOT))
    after = audit_report(_ir(BOOT.replace(
        'data_dir: Str under "./.harness-data",',
        'data_dir: Str under "./.harness-data",\n    extra: Str = "x",')))
    delta = diff_crossings(before, after)
    assert "env:extra:*" in delta["added"]


def test_dropping_a_bound_is_a_widening():
    """The fail-open this closes: an env field that quietly loses its bound is
    exactly as wide as the pre-350 host-written config map. The crossing token
    carries the bound, so the drift gate sees the moment it became unbounded."""
    before = audit_report(_ir(BOOT))
    after = audit_report(_ir(BOOT.replace(' under "./.harness-data"', "")))
    delta = diff_crossings(before, after)
    assert "env:data_dir:*" in delta["added"]
    assert "env:data_dir:under=./.harness-data" in delta["removed"]


def test_tightening_a_bound_is_not_an_addition_of_the_old_token():
    before = audit_report(_ir(BOOT))
    after = audit_report(_ir(BOOT.replace(
        'model: Str in ["mock", "real", "engine"]', 'model: Str in ["mock"]')))
    delta = diff_crossings(before, after)
    assert "env:model:in=mock" in delta["added"]
    assert "env:model:in=mock|real|engine" in delta["removed"]
