"""An untrusted edit may not shrink a component's realm set (issue #2586).

Issues #2487, #2523 and #2585 compare realm placements per (component, key)
between the admitted generation and the candidate, and a pair the candidate no
longer has is exempt. That exemption also covers an edit that renames the key
a component is placed under: the old pair is gone, so nothing is compared, and
the component ends in the shared realm. The policy selects a component for
realm-scoped allow and deny rules by every realm it is isolated into, so the
component has left the rules the operator wrote for that realm.

The untrusted author is now also refused, by component name, when a component
that is admitted ends with fewer realms than it had, whatever else the
candidate places in the realm it left. The comparison is against the admitted
generation only: an edit that also withdraws the realm's other holders is
refused too. The check stops at the first component it finds, in name order.
Controls: a trusted author may shrink the set, an edit that keeps the set is
admitted, and so is an enlarged set and a withdrawn component. The per-key
refusals keep their text and fire first.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.admit_profile import check_no_realm_removal  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from test_untrusted_requirer_realm_2585 import (  # noqa: E402,F401
    REFUSAL, _add_shared_provider, _call, _drop_requirer_clause, _edit,
    _line_of, _messages, _realms, clean_session, loaded)

SHRINK = "cannot shrink the realm set"

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime: "
           "install it with `sh backends/python/setup.sh`")

APP = ('component TenantAApp requires kv: Kv {\n'
       '  isolate kv in realm("tenant_a")\n'
       '  intercept kv with { quota: 5, tags: ["tenant_a"] }\n'
       '  effect kv.set("who", "alice") undo kv.unset("who")\n'
       '}\n')
APP_KV2 = ('component TenantAApp requires kv2: Kv {\n'
           '  effect kv2.set("who", "alice") undo kv2.unset("who")\n'
           '}\n')
STORE_HEAD = ('component TenantAStore provides kv: Kv {\n'
              '  isolate kv in realm("tenant_a")\n'
              '  let store = effect Map.new() undo store.drop()\n'
              '  provide kv {')
STORE_HEAD_KV2 = ('component TenantAStore provides kv2: Kv {\n'
                  '  let store = effect Map.new() undo store.drop()\n'
                  '  provide kv2 {')
STORE_A = ('component TenantAStore provides kv: Kv {\n'
           '  isolate kv in realm("tenant_a")\n'
           '  let store = effect Map.new() undo store.drop()\n'
           '  provide kv {\n'
           '    fn get(k) = store.get(k)\n'
           '    fn set(k, v) {\n'
           '      effect store.insert(k, v)\n'
           '      undo   store.remove(k)\n'
           '    }\n'
           '    fn unset(k) = store.remove(k)\n'
           '  }\n'
           '}\n')
SHARED_KV2 = """component SharedKv2 provides kv2: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv2 {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
    fn unset(k) = store.remove(k)
  }
}

"""


def _requirer_leaves_for_a_shared_key(path) -> dict:
    """TenantAApp requires `kv2` in the shared realm instead of `kv` in
    tenant_a, and a shared-realm provider of `kv2` is added ahead of the
    second tenant's app. The pair (TenantAApp, kv) is gone from the candidate;
    TenantAStore still holds tenant_a."""
    anchor = "component TenantBApp requires kv: Kv {"
    return _edit(path,
                 {"anchor": APP, "replacement": APP_KV2},
                 {"anchor": anchor, "replacement": SHARED_KV2 + anchor})


def _provider_renames_its_key(path) -> dict:
    """TenantAStore provides `kv2` with no clause instead of `kv` in tenant_a.
    TenantAApp still holds tenant_a."""
    return _edit(path, {"anchor": STORE_HEAD, "replacement": STORE_HEAD_KV2})


def _assert_shrink_refused(result: dict, path, component: str,
                           pointed: bool = True) -> None:
    assert result["ok"] is False and result.get("admitted") is False, result
    message = _messages(result)
    assert SHRINK in message, message
    assert f"`{component}`" in message, message
    assert 'realm("tenant_a")' in message, message
    assert REFUSAL not in message, message
    refusal = next(d for d in result["diagnostics"] if SHRINK in d["message"])
    assert refusal.get("code") == "G9", refusal
    assert str(refusal.get("file")).endswith(path.name), refusal
    if not pointed:  # the edit moves lines, so the original line is no guide
        return
    header = next(h for h in path.read_text().splitlines()
                  if h.startswith(f"component {component} "))
    assert refusal.get("line") == _line_of(path.read_text(), header), refusal


@needs_cordis
def test_untrusted_provider_key_rename_into_the_shared_realm_is_refused(
        loaded):  # noqa: F811
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    proposed = _call("revl_change", {"edit": _provider_renames_its_key(loaded)})
    _assert_shrink_refused(proposed, loaded, "TenantAStore")
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _realms("TenantAStore") == {"kv": "tenant_a"}


@needs_cordis
def test_untrusted_requirer_key_swap_into_the_shared_realm_is_refused(
        loaded):  # noqa: F811
    assert server_mod.AUTHORING.profile() is not None
    proposed = _call("revl_change",
                     {"edit": _requirer_leaves_for_a_shared_key(loaded)})
    _assert_shrink_refused(proposed, loaded, "TenantAApp")
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    running = {c["name"] for c in server_mod.SESSION.ir["components"]}
    assert "SharedKv2" not in running, running


@needs_cordis
def test_emptying_the_realm_in_the_same_edit_is_still_refused(
        loaded):  # noqa: F811
    """Issue case 1 with the realm's other holder, TenantAStore, withdrawn in
    the same edit: tenant_a is then placed nowhere in the candidate, yet
    TenantAApp still left it, and the comparison is against the admitted
    generation."""
    edit = _requirer_leaves_for_a_shared_key(loaded)
    edit["edits"].append({"anchor": STORE_A, "replacement": ""})
    proposed = _call("revl_change", {"edit": edit})
    _assert_shrink_refused(proposed, loaded, "TenantAApp", pointed=False)
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert _realms("TenantAStore") == {"kv": "tenant_a"}


@needs_cordis
def test_trusted_author_may_shrink_the_realm_set(loaded):  # noqa: F811
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    proposed = _call("revl_change",
                     {"edit": _requirer_leaves_for_a_shared_key(loaded)})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _realms("TenantAApp") == {}


@needs_cordis
def test_untrusted_edit_that_keeps_the_realm_set_is_admitted(
        loaded):  # noqa: F811
    proposed = _call("revl_change", {"edit": _edit(loaded, {
        "anchor": 'kv.set("who", "alice")',
        "replacement": 'kv.set("who", "carol")'})})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}


@needs_cordis
def test_the_per_key_refusal_still_fires_first_with_its_text(
        loaded):  # noqa: F811
    """Dropping the requirer's clause for the same key is the #2585 refusal,
    which names the key; the set check does not replace or reword it."""
    proposed = _call("revl_change", {"edit": _edit(
        loaded, _add_shared_provider(), _drop_requirer_clause())})
    assert proposed["ok"] is False and proposed.get("admitted") is False, proposed
    message = _messages(proposed)
    assert REFUSAL in message and "TenantAApp" in message, message
    assert "`kv` requirement" in message, message
    assert SHRINK not in message, message


# ---------------------------------------------------------------------------
# The check on the admitted and candidate generations directly: the placements
# an untrusted author cannot write (a realm set that grows) are only reachable
# here. The profile is the server's own untrusted authoring profile.

def _comp(name: str, isolate: dict, key: str = "kv") -> dict:
    return {"name": name, "source": f"{name}.rvl", "isolate": isolate,
            "provides": {}, "requires": {key: "Kv"}}


def _untrusted():
    profile = server_mod.AUTHORING.profile()
    assert profile is not None
    return profile


def test_shrink_with_the_realm_still_in_use_is_refused():
    admitted = {"components": [_comp("A", {"kv": "r"}), _comp("B", {"kv": "r"})]}
    candidate = {"components": [_comp("A", {}, "kv2"), _comp("B", {"kv": "r"})]}
    with pytest.raises(RevlError) as refused:
        check_no_realm_removal(admitted, candidate, _untrusted())
    assert SHRINK in refused.value.message, refused.value.message
    assert "`A`" in refused.value.message, refused.value.message
    assert 'realm("r")' in refused.value.message, refused.value.message
    assert refused.value.code == "G9"


def test_enlarged_realm_set_is_admitted():
    admitted = {"components": [_comp("A", {"kv": "r"})]}
    candidate = {"components": [_comp("A", {"kv": "r", "kv2": "s"})]}
    check_no_realm_removal(admitted, candidate, _untrusted())


def test_equal_realm_set_under_a_new_key_is_admitted():
    admitted = {"components": [_comp("A", {"kv": "r"}), _comp("B", {"kv": "r"})]}
    candidate = {"components": [_comp("A", {"kv2": "r"}, "kv2"),
                                _comp("B", {"kv": "r"})]}
    check_no_realm_removal(admitted, candidate, _untrusted())


def test_withdrawn_component_is_exempt():
    admitted = {"components": [_comp("A", {"kv": "r"}), _comp("B", {"kv": "r"})]}
    candidate = {"components": [_comp("B", {"kv": "r"})]}
    check_no_realm_removal(admitted, candidate, _untrusted())


def test_every_holder_leaving_the_realm_is_refused_at_the_first():
    """A and B both leave `r`, so no component of the candidate keeps it; the
    comparison is against the admitted generation, so it is refused. The check
    stops at the first component in name order, `A`."""
    admitted = {"components": [_comp("A", {"kv": "r"}), _comp("B", {"kv": "r"})]}
    candidate = {"components": [_comp("A", {}, "kv2"), _comp("B", {}, "kv2")]}
    with pytest.raises(RevlError) as refused:
        check_no_realm_removal(admitted, candidate, _untrusted())
    assert SHRINK in refused.value.message, refused.value.message
    assert "`A`" in refused.value.message, refused.value.message


def test_trusted_profile_is_never_refused():
    admitted = {"components": [_comp("A", {"kv": "r"}), _comp("B", {"kv": "r"})]}
    candidate = {"components": [_comp("A", {}, "kv2"), _comp("B", {"kv": "r"})]}
    check_no_realm_removal(admitted, candidate, None)
