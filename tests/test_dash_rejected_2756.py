"""`revl dash` rejected-candidates section (issue #2756, src/revl/dash.py).

The section joins a refusal with its `navigate` record: per rejected candidate,
each refused edge as `component -> capability: guarantee`, with the repair the
policy allows beside it. It is opt-in and read-only, and under the
untrusted-author profile it shows only the collapsed verdict.
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import dash, navigate as nav  # noqa: E402
from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TENANTS = os.path.join(ROOT, "tests", "fixtures", "policy_tenants.rvl")

ALLOW_KV = "component TenantAJob may reach kv\n"        # `bus` is unlisted
DENY_BUS = "component TenantAJob may not reach bus\n"
ALLOW_KV_B = "component TenantBJob may reach kv\n"


@pytest.fixture(scope="module")
def tenants():
    return compile_files([TENANTS])


def _untrusted():
    return AdmissionProfile.untrusted_author(["kv"])


def test_unlisted_capability_names_edge_guarantee_and_repair(tenants):
    """Refuses a candidate that reaches an unlisted capability; the section
    names the edge, the failing guarantee and an allowed repair."""
    model = dash.build_model(tenants, policy=parse_policy(ALLOW_KV),
                             rejected=True)
    section = model["rejected"]
    assert section["view"] == "operator"
    (cand,) = section["candidates"]
    assert cand["candidate"] == "TenantAJob"
    (edge,) = cand["edges"]
    assert (edge["from"], edge["capability"]) == ("TenantAJob", "bus")
    assert edge["guarantee"] == "may reach only [kv]"
    enacts = {r["enacts"] for r in edge["repairs"]}
    assert enacts == {nav.ENACTS_AUTHOR, nav.ENACTS_OPERATOR}
    assert any("drop the reach" in r["action"] for r in edge["repairs"])

    text = dash.render_model(model)
    assert "REJECTED CANDIDATES" in text
    assert "TenantAJob -> bus: may reach only [kv]" in text
    assert "repair (author, clears-this-gate): drop the reach" in text


def test_deny_rule_guarantee_is_the_deny_pattern(tenants):
    model = dash.build_model(tenants, policy=parse_policy(DENY_BUS),
                             rejected=True)
    (edge,) = model["rejected"]["candidates"][0]["edges"]
    assert edge["guarantee"] == "may not reach [bus]"
    assert edge["family"] == "policy-deny"


def test_untrusted_author_sees_only_the_collapsed_verdict(tenants):
    """Different refused families, components and capabilities render the same
    bytes, and none of the policy topology appears."""
    sections = []
    for src in (ALLOW_KV, DENY_BUS, ALLOW_KV_B):
        model = dash.build_model(tenants, policy=parse_policy(src),
                                 rejected=True, profile=_untrusted())
        sections.append((json.dumps(model["rejected"], sort_keys=True),
                         "\n".join(dash.render_model(model)
                                   .split("REJECTED CANDIDATES")[1:])))
    first = sections[0]
    for other in sections[1:]:
        assert other == first
    blob = first[0] + first[1]
    assert nav.UNTRUSTED_REASON in blob
    for leak in ("bus", "TenantA", "TenantB", "kv", "policy-", "drop the reach"):
        assert leak not in blob


def test_untrusted_author_policy_rows_are_collapsed_too(tenants):
    """The decisions queue above the section must not leak what the section
    hides: from the decisions pane to the end of the output (the dependency
    graph above it is the author's own source) the policy topology is absent,
    and the queue is the same bytes for different refusals."""
    renders = []
    for src in (ALLOW_KV, DENY_BUS, ALLOW_KV_B):
        model = dash.build_model(tenants, policy=parse_policy(src),
                                 rejected=True, profile=_untrusted())
        text = dash.render_model(model)
        tail = text[text.index("PENDING DECISIONS"):]
        for leak in ("bus", "TenantA", "TenantB", "kv", "policy violation",
                     "why `", "may reach", "may not reach", "tenant"):
            assert leak not in tail, leak
        assert nav.UNTRUSTED_REASON in tail
        assert model["decisions"]["pending"] == 1
        renders.append((tail, json.dumps(model["decisions"], sort_keys=True)))
    assert renders[1:] == [renders[0]] * 2


def test_default_policy_queue_is_unchanged_without_a_profile(tenants):
    policy = parse_policy(ALLOW_KV)
    plain = dash.build_model(tenants, policy=policy)
    with_flag = dash.build_model(tenants, policy=policy, rejected=True)
    assert with_flag["decisions"] == plain["decisions"]
    row = plain["decisions"]["policy"][0]
    assert row["component"] == "TenantAJob" and "verdict" not in row


def test_policy_is_evaluated_once(tenants, monkeypatch):
    from revl import policy as policy_mod
    calls = []
    real = policy_mod.evaluate
    monkeypatch.setattr(policy_mod, "evaluate",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    dash.build_model(tenants, policy=parse_policy(ALLOW_KV), rejected=True)
    assert len(calls) == 1


def test_section_is_opt_in(tenants):
    policy = parse_policy(ALLOW_KV)
    plain = dash.build_model(tenants, policy=policy)
    assert "rejected" not in plain
    assert "REJECTED CANDIDATES" not in dash.render_model(plain)
    assert dash.build_model(tenants, policy=policy, rejected=True)["rejected"]


def test_no_policy_or_clean_policy_rejects_nothing(tenants):
    assert dash.build_model(tenants, rejected=True)["rejected"] == {
        "view": "operator", "candidates": []}
    clean = parse_policy("component TenantAJob may reach bus\n")
    model = dash.build_model(tenants, policy=clean, rejected=True)
    assert model["rejected"]["candidates"] == []
    assert "no candidate rejected." in dash.render_model(model)


def test_section_mutates_no_input_and_dashboard_passes_it_through(tenants):
    before = copy.deepcopy(tenants)
    policy = parse_policy(ALLOW_KV)
    board = dash.Dashboard(tenants, policy=policy, rejected=True)
    assert "REJECTED CANDIDATES" in board.render()
    assert board.snapshot()["rejected"]["view"] == "operator"
    assert tenants == before


# ------------------------------------------------------------------- CLI
#
# `revl dash --rejected` enables the section and, because the profile follows
# the source (issue #2547), an author-written file under `.revl/export/` gets
# the collapsed author verdict rather than the operator's full record.

AUTHORED = """service Bus { emission[bus] fn publish(message: Str) }

component TenantAJob requires bus: Bus {
  emit bus.publish("a-event")
}
"""


def _policy_file(tmp_path, text):
    path = tmp_path / "boundary.policy"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _dash_cli(capsys, *argv):
    from revl.__main__ import main
    rc = main(["dash", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_cli_rejected_flag_shows_the_section(tmp_path, capsys):
    policy = _policy_file(tmp_path, ALLOW_KV)
    rc, out, _ = _dash_cli(capsys, TENANTS, "--policy", policy, "--rejected")
    assert rc == 0
    assert "REJECTED CANDIDATES" in out
    assert "TenantAJob -> bus: may reach only [kv]" in out
    assert "repair (author, clears-this-gate): drop the reach" in out


def test_cli_rejected_section_is_off_by_default(tmp_path, capsys):
    policy = _policy_file(tmp_path, ALLOW_KV)
    rc, out, _ = _dash_cli(capsys, TENANTS, "--policy", policy)
    assert rc == 0
    assert "REJECTED CANDIDATES" not in out


def test_cli_json_rejected_key_follows_the_flag(tmp_path, capsys):
    policy = _policy_file(tmp_path, ALLOW_KV)
    rc, out, _ = _dash_cli(capsys, TENANTS, "--policy", policy,
                           "--rejected", "--json")
    assert rc == 0
    model = json.loads(out)
    assert model["rejected"]["view"] == "operator"
    assert model["rejected"]["candidates"]

    rc, out, _ = _dash_cli(capsys, TENANTS, "--policy", policy, "--json")
    assert rc == 0
    assert "rejected" not in json.loads(out)


def _exported(tmp_path, capsys, *flags):
    exported = tmp_path / ".revl" / "export" / "app.rvl"
    exported.parent.mkdir(parents=True)
    exported.write_text(AUTHORED, encoding="utf-8")
    policy = _policy_file(tmp_path, ALLOW_KV)
    return _dash_cli(capsys, str(exported), "--policy", policy,
                     "--rejected", *flags)


def test_cli_exported_source_is_the_collapsed_author_view(tmp_path, capsys):
    """A file under `.revl/export/` is author-written, so its compile uses the
    authoring profile and the section it enables must be the collapsed verdict,
    not the operator's full `navigate` record."""
    rc, out, _ = _exported(tmp_path, capsys)
    assert rc == 0
    section = out.split("REJECTED CANDIDATES", 1)[1]
    assert nav.UNTRUSTED_REASON in section
    assert "TenantAJob -> bus" not in section
    assert "may reach only" not in section


def test_cli_trust_exported_shows_the_operator_record(tmp_path, capsys):
    """`--trust-exported` says the file is the operator's own, so the same
    source gets the full operator view rather than the collapsed one."""
    rc, out, _ = _exported(tmp_path, capsys, "--trust-exported")
    assert rc == 0
    section = out.split("REJECTED CANDIDATES", 1)[1]
    assert "TenantAJob -> bus: may reach only [kv]" in section
    assert nav.UNTRUSTED_REASON not in section


USES_EXPORTED_LIB = """use ".revl/export/lib.rvl" { shout }
service Bus { emission[bus] fn publish(message: Str) }

component Local requires bus: Bus {
  emit bus.publish("a-event")
}
"""


def test_cli_used_exported_module_is_the_collapsed_author_view(tmp_path, capsys):
    """A root the operator owns but which `use`s an author-written module is
    compiled under the authoring profile as a whole (issue #2547), so the
    section collapses for it too."""
    lib = tmp_path / ".revl" / "export" / "lib.rvl"
    lib.parent.mkdir(parents=True)
    lib.write_text('pub fn shout() -> Str { return "x" }\n', encoding="utf-8")
    root = tmp_path / "app.rvl"
    root.write_text(USES_EXPORTED_LIB, encoding="utf-8")
    policy = _policy_file(tmp_path, "component Local may reach kv\n")
    rc, out, _ = _dash_cli(capsys, str(root), "--policy", policy, "--rejected")
    assert rc == 0
    section = out.split("REJECTED CANDIDATES", 1)[1]
    assert nav.UNTRUSTED_REASON in section
    assert "Local -> bus" not in section
