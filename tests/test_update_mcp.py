"""The operator update surface: check, plan, apply, roll back (docs/updates.md).

The live tests boot a real cordis session; they are marked per test so the
runtime-free assertions (the verbs exist, a disabled server refuses by name,
the widening gate, the tag fallback refusal) still run everywhere.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _update_feed_fixture import pin_test_key, serve_feed, tar_gz  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp import update_tools  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.update.feed import resolve_release  # noqa: E402
from revl.update.journal import Journal  # noqa: E402

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

CACHE_V2 = CACHE.replace("fn size() = 0", "fn size() = 43")

#: a composition no release was applied to has no known release version, so
#: its first plan consents to that explicitly
FIRST = {"acceptUnknownVersion": True}

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the update apply verbs need the cordis-py runtime")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _release_feed(version: str = "9.9.9", source: str = CACHE_V2):
    artifact = tar_gz({"cache.rvl": source.encode()},
                      top=f"app-{version}")
    return serve_feed(version=version, artifact_bytes=artifact)


@pytest.fixture(autouse=True)
def _isolated():
    update_tools.reset()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    update_tools.reset()


@pytest.fixture(autouse=True)
def _pin_fixture_release_key(monkeypatch):
    pin_test_key(monkeypatch)


def _configure(feed, tmp_path: Path, *,
               consents: tuple[str, ...] | None = None) -> None:
    if consents is None:
        consents = update_tools.OPERATOR_CONSENTS
    update_tools.configure(enabled=True, repo="owner/app", project="app",
                           api_base=feed.base_url, home=str(tmp_path),
                           consents=consents)


# ------------------------------------------------------------ runtime free

def test_the_four_verbs_are_registered_and_documented_as_a_topic():
    names = {tool["name"] for tool in server_mod.TOOLS}
    assert {"revl_update_check", "revl_update_apply", "revl_update_status",
            "revl_update_rollback"} <= names
    topic = server_mod._disclosure.TOPICS["update"][1]
    assert set(topic) == {"revl_update_check", "revl_update_apply",
                          "revl_update_status", "revl_update_rollback"}


def test_a_disabled_server_refuses_the_mutating_update_verbs_by_name():
    for verb in ("revl_update_check", "revl_update_apply",
                 "revl_update_rollback"):
        payload = _call(verb, {})
        assert payload["ok"] is False
        assert payload["refusal"]["code"] == "UPDATE_DISABLED"


def test_status_answers_even_when_disabled_and_names_the_config(tmp_path):
    update_tools.configure(home=str(tmp_path))
    payload = _call("revl_update_status", {})
    assert payload["ok"] is True
    assert payload["enabled"] is False
    assert payload["config"]["repo"] == "inso1337/revl"
    assert payload["staged"] == []
    assert payload["identity"]["version"]


def test_rollback_without_an_applied_release_is_refused(tmp_path):
    update_tools.configure(enabled=True, home=str(tmp_path))
    payload = _call("revl_update_rollback", {})
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_NOTHING_APPLIED"


def test_check_compares_the_feed_against_the_running_identity(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        payload = _call("revl_update_check", {})
    finally:
        feed.close()
    assert payload["ok"] is True
    assert payload["comparison"]["updateAvailable"] is None
    # nothing loaded: no running release, so the release is compared with
    # the composition (unknown), never with the toolchain's own version
    assert payload["comparison"]["basis"] == "composition"
    assert payload["comparison"]["relation"] == "unknown"
    assert payload["comparison"]["bump"] == "unknown"
    assert payload["release"]["version"] == "9.9.9"
    assert payload["applyAvailable"] is False  # nothing loaded yet
    assert payload["applyReason"] == "nothing is loaded"


def test_apply_without_a_plan_hash_needs_a_running_composition(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        payload = _call("revl_update_apply", {})
    finally:
        feed.close()
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_NOTHING_LOADED"


@needs_runtime
def test_a_tag_fallback_is_refused_for_apply(tmp_path):
    feed = serve_feed(tag_only=True, version="9.9.9")
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        payload = _call("revl_update_apply", FIRST)
    finally:
        feed.close()
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_TAG_FALLBACK"


# -------------------------------------------------------------- live apply

@needs_runtime
def test_plan_apply_and_rollback_through_the_gate(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        assert _call("revl_load", {"source": CACHE})["ok"] is True
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
        check = _call("revl_update_check", {})
        assert check["applyAvailable"] is False
        assert "acceptUnknownVersion" in check["applyReason"]
        planned = _call("revl_update_apply", FIRST)
        assert planned["ok"] is True and planned["planned"] is True
        assert planned["applied"] is False
        assert planned["generation"] == 1
        assert planned["widening"]["widened"] is False
        plan_hash = planned["planHash"]
        applied = _call("revl_update_apply", {"planHash": plan_hash})
        assert applied["ok"] is True and applied["applied"] is True
        assert applied["generationBefore"] == 1
        assert applied["generation"] == 2
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 43
        rolled = _call("revl_update_rollback", {})
        assert rolled["ok"] is True and rolled["rolledBack"] is True
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
        # the applied sources went with the rollback that consumed the apply
        assert list((tmp_path / "applied").iterdir()) == []
    finally:
        feed.close()


@needs_runtime
def test_a_stale_plan_hash_is_refused(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", FIRST)
        plan_hash = planned["planHash"]
        # the running composition moves after the plan: the plan is stale.
        assert _call("revl_swap", {"source": CACHE_V2})["swapped"] is True
        stale = _call("revl_update_apply", {"planHash": plan_hash})
        assert stale["ok"] is False
        assert stale["refusal"]["code"] == "UPDATE_PLAN_STALE"
    finally:
        feed.close()


@needs_runtime
def test_widening_requires_acknowledgement(monkeypatch, tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        monkeypatch.setattr(update_tools, "_widening",
                            lambda _ir, _files: {
                                "widened": True,
                                "added": ["emit:MemCache:send"],
                                "reachWeakened": []})
        planned = _call("revl_update_apply", FIRST)
        plan_hash = planned["planHash"]
        refused = _call("revl_update_apply", {"planHash": plan_hash})
        assert refused["ok"] is False
        assert refused["approvalRequired"] is True
        assert refused["refusal"]["code"] == "UPDATE_WIDENS"
        accepted = _call("revl_update_apply",
                         {"planHash": plan_hash, "acknowledgeWidening": True})
        assert accepted["ok"] is True and accepted["applied"] is True
        assert accepted["widening"]["widened"] is True
    finally:
        feed.close()


@needs_runtime
def test_a_refused_admission_leaves_the_running_composition_serving(tmp_path):
    broken = CACHE.replace("fn size() = 0", 'fn size() = "nope"')
    feed = _release_feed(source=broken)
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", FIRST)
        assert planned["ok"] is False
        assert planned["stoppedAt"] == "admit"
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
        assert _call("revl_state", {})["generation"] == 1
    finally:
        feed.close()


@needs_runtime
def test_a_release_that_is_not_live_is_refused_for_apply(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
    finally:
        feed.close()
    # a feed whose manifest says live: false
    not_live = serve_feed(version="9.9.9",
                          artifact_bytes=tar_gz({"cache.rvl": CACHE_V2.encode()},
                                                top="app-9.9.9"),
                          live=False)
    try:
        _configure(not_live, tmp_path)
        planned = _call("revl_update_apply", FIRST)
        assert planned["ok"] is False
        assert planned["refusal"]["code"] == "UPDATE_NOT_LIVE"
        # force is not consent to a release the feed has not certified live
        forced = _call("revl_update_apply", {**FIRST, "force": True})
        assert forced["ok"] is False
        assert forced["refusal"]["code"] == "UPDATE_NOT_LIVE"
        accepted = _call("revl_update_apply", {**FIRST, "acceptNotLive": True})
        assert accepted["ok"] is True
        assert accepted["live"] is False
    finally:
        not_live.close()


@needs_runtime
def test_a_staged_file_changed_after_the_plan_is_refused(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", FIRST)
        staged = Path(planned["staged"]["sources"]["root"]) / "cache.rvl"
        staged.write_text(CACHE_V2.replace("43", "99"), encoding="utf-8")
        tampered = _call("revl_update_apply", {"planHash": planned["planHash"]})
        assert tampered["ok"] is False
        assert tampered["refusal"]["code"] == "UPDATE_PLAN_TAMPERED"
        assert _call("revl_call", {"key": "cache",
                                   "method": "size"})["result"] == 0
    finally:
        feed.close()


@needs_runtime
def test_a_plan_does_not_survive_a_different_running_composition(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", FIRST)
        _call("revl_unload", {})
        _call("revl_load", {"source": CACHE.replace("fn size() = 0",
                                                    "fn size() = 7")})
        stale = _call("revl_update_apply", {"planHash": planned["planHash"]})
        assert stale["ok"] is False
        assert stale["refusal"]["code"] == "UPDATE_PLAN_STALE"
    finally:
        feed.close()


# ------------------------------------------------- widening axes (unit)

def _audit(**overrides):
    audit = {"boundary": {}, "externs": [], "capability_registers": {},
             "recovery_surface": [], "cardinality": {}}
    audit.update(overrides)
    return audit


def test_each_authority_axis_can_widen_on_its_own():
    base = _audit()
    crossing = _audit(boundary={"C": {"emissions": ["send.mail"]}})
    backend = _audit(externs=[{"name": "send", "backends": ["py", "ts"]}])
    backend_base = _audit(externs=[{"name": "send", "backends": ["py"]}])
    recovery = _audit(recovery_surface=[{"name": "s", "kind": "effect",
                                         "register": "keyed"}])
    registers = _audit(capability_registers={"net": None})
    registers_base = _audit(capability_registers={"net": "keyed"})
    cardinality = _audit(cardinality={"C": {"per_capability": {
        "send.mail": {"kind": "bounded", "bound": 5}}}})
    cardinality_base = _audit(cardinality={"C": {"per_capability": {
        "send.mail": {"kind": "bounded", "bound": 2}}}})

    def widened(before, after):
        return update_tools._widening_from_audits(before, after)["widened"]

    assert widened(base, base) is False
    assert widened(base, crossing) is True
    assert widened(backend_base, backend) is True
    assert widened(recovery, base) is True
    assert widened(registers_base, registers) is True
    assert widened(cardinality_base, cardinality) is True


def test_an_empty_key_set_is_a_healthy_successor():
    assert update_tools._keys_served([], []) is True
    assert update_tools._keys_served(["cache"], ["cache", "extra"]) is True
    assert update_tools._keys_served(["cache"], []) is False


def test_the_token_is_never_sent_over_cleartext_off_loopback(monkeypatch):
    from revl.update import feed as feed_module

    monkeypatch.setattr(feed_module, "_ACTIVE_API_HOST", "127.0.0.1")
    assert feed_module.token_host_allowed("https://github.com/a") is True
    assert feed_module.token_host_allowed("http://127.0.0.1:5555/a") is True
    assert feed_module.token_host_allowed("http://github.com/a") is False
    assert feed_module.token_host_allowed("http://localhost:5555/a") is True


def test_rollback_ignores_an_apply_recorded_by_another_session(tmp_path):
    update_tools.configure(enabled=True, home=str(tmp_path))
    journal = Journal(str(tmp_path))
    journal.append("apply", sessionId="another-server:123", project="app",
                   version="9.9.9", generationBefore=1, generationAfter=2)
    payload = _call("revl_update_rollback", {})
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_NOTHING_APPLIED"


def test_a_tag_fallback_staging_is_not_reused_for_a_manifest(tmp_path):
    tag_feed = serve_feed(tag_only=True, version="9.9.9")
    try:
        tag_release = resolve_release("owner/app",
                                      api_base_override=tag_feed.base_url)
        cfg = {"home": str(tmp_path), "trust_tags": True,
               "api_base": tag_feed.base_url, "token_env": None}
        tag_staged, refusal = update_tools._stage_for_apply(
            tag_release, cfg, Journal(str(tmp_path)))
        assert refusal is None and tag_staged.tag_fallback is True
        manifest_feed = serve_feed(
            version="9.9.9",
            artifact_bytes=tar_gz({"cache.rvl": CACHE_V2.encode()},
                                  top="app-9.9.9"))
        try:
            manifest_release = resolve_release(
                "owner/app", api_base_override=manifest_feed.base_url)
            cfg["api_base"] = manifest_feed.base_url
            staged, refusal = update_tools._stage_for_apply(
                manifest_release, cfg, Journal(str(tmp_path)))
        finally:
            manifest_feed.close()
    finally:
        tag_feed.close()
    assert refusal is None
    assert staged.tag_fallback is False
    assert staged.directory != tag_staged.directory
    assert staged.digest == manifest_release.manifest.artifacts[0].sha256


# ------------------------------------------- review fixes (PR #3002)

def _apply(planned: dict | None = None) -> dict:
    planned = planned or _call("revl_update_apply", FIRST)
    assert planned["ok"] is True and planned["planned"] is True, planned
    applied = _call("revl_update_apply", {"planHash": planned["planHash"]})
    assert applied["ok"] is True and applied["applied"] is True, applied
    return applied


def _size() -> int:
    return _call("revl_call", {"key": "cache", "method": "size"})["result"]


@needs_runtime
def test_an_application_release_below_the_toolchain_version_is_planned(
        tmp_path):
    # the feed versions the application, not the toolchain: 1.2.0 is below
    # the compiler's own version and is still the release to apply
    feed = _release_feed(version="1.2.0")
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        check = _call("revl_update_check", {})
        assert check["comparison"]["relation"] == "unknown"
        _apply()
        assert _size() == 43
    finally:
        feed.close()


@needs_runtime
def test_an_applied_release_is_not_offered_again(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        check = _call("revl_update_check", {})
        assert check["runningRelease"]["version"] == "9.9.9"
        assert check["comparison"]["relation"] == "same"
        assert check["applyAvailable"] is False
        again = _call("revl_update_apply", {})
        assert again["ok"] is True and again["planned"] is False
        # force re-plans the running release, and only that
        assert _call("revl_update_apply", {"force": True})["planned"] is True
    finally:
        feed.close()


@needs_runtime
def test_force_does_not_consent_to_a_downgrade(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
    finally:
        feed.close()
    older = _release_feed(version="1.2.0", source=CACHE)
    try:
        _configure(older, tmp_path)
        assert _call("revl_update_apply", {})["planned"] is False
        forced = _call("revl_update_apply", {"force": True})
        assert forced["ok"] is False
        assert forced["refusal"]["code"] == "UPDATE_DOWNGRADE"
        consented = _call("revl_update_apply", {"allowDowngrade": True})
        assert consented["ok"] is True and consented["planned"] is True
        assert consented["consents"]["allowDowngrade"] is True
    finally:
        older.close()


@needs_runtime
def test_an_incompatible_release_is_refused_unless_accepted(tmp_path):
    compat = {"language": "2.0.0", "gateApi": "99.0.0", "irVersion": 1,
              "minPython": "3.11"}
    feed = serve_feed(version="9.9.9",
                      artifact_bytes=tar_gz({"cache.rvl": CACHE_V2.encode()},
                                            top="app-9.9.9"),
                      manifest_extra={"compat": compat})
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        check = _call("revl_update_check", {})
        assert check["comparison"]["compatible"] is False
        assert check["applyAvailable"] is False
        refused = _call("revl_update_apply", FIRST)
        assert refused["ok"] is False
        assert refused["refusal"]["code"] == "UPDATE_INCOMPATIBLE"
        assert not (tmp_path / "staged").exists()
        accepted = _call("revl_update_apply",
                         {**FIRST, "acceptIncompatible": True})
        assert accepted["ok"] is True and accepted["planned"] is True
    finally:
        feed.close()


@needs_runtime
def test_rollback_runs_revl_undo_profile_and_answers_its_approval(
        monkeypatch, tmp_path):
    from revl.mcp.approval import ApprovalRequired

    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        seen: list[dict] = []

        def needs_approval(to=None, **kwargs):
            seen.append({"to": to, **kwargs})
            raise ApprovalRequired({"hash": "f" * 64, "crossings": []})

        with monkeypatch.context() as patch:
            patch.setattr(server_mod.SESSION, "undo", needs_approval)
            pending = _call("revl_update_rollback", {})
        assert seen and seen[0]["to"] == 1
        assert seen[0]["profile"] == server_mod.AUTHORING.profile()
        assert pending["ok"] is False
        assert pending["approvalRequired"] is True
        assert pending["rolledBack"] is False
        # nothing was consumed: once approved the same rollback still runs
        rolled = _call("revl_update_rollback", {})
        assert rolled["ok"] is True and rolled["rolledBack"] is True
        assert _size() == 0
    finally:
        feed.close()


@needs_runtime
def test_a_rollback_consumes_its_apply(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        assert _call("revl_update_rollback", {})["ok"] is True
        generation = _call("revl_state", {})["generation"]
        twice = _call("revl_update_rollback", {})
        assert twice["ok"] is False
        assert twice["refusal"]["code"] == "UPDATE_NOTHING_APPLIED"
        assert _call("revl_state", {})["generation"] == generation
        # the rollback consumed the only apply: the version is unknown again
        check = _call("revl_update_check", {})
        assert check["comparison"]["relation"] == "unknown"
        assert check["lastApplied"] is None
    finally:
        feed.close()


@needs_runtime
def test_rollback_refuses_when_a_later_generation_runs(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        later = CACHE.replace("fn size() = 0", "fn size() = 7")
        assert _call("revl_swap", {"source": later})["swapped"] is True
        refused = _call("revl_update_rollback", {})
        assert refused["ok"] is False
        assert refused["refusal"]["code"] == "UPDATE_ROLLBACK_STALE"
        assert _size() == 7
    finally:
        feed.close()


def test_a_reused_staging_is_checked_file_by_file(tmp_path):
    feed = _release_feed()
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
        cfg = {"home": str(tmp_path), "trust_tags": False,
               "api_base": feed.base_url, "token_env": None}
        first, refusal = update_tools._stage_for_apply(
            release, cfg, Journal(str(tmp_path)))
        assert refusal is None
        source = first.source_root / "cache.rvl"
        source.write_text(CACHE_V2.replace("43", "99"), encoding="utf-8")
        second, refusal = update_tools._stage_for_apply(
            release, cfg, Journal(str(tmp_path)))
    finally:
        feed.close()
    assert refusal is None
    assert (second.source_root / "cache.rvl").read_text(
        encoding="utf-8") == CACHE_V2


def test_a_reused_staging_is_checked_against_its_archive_not_provenance(
        tmp_path):
    feed = _release_feed()
    try:
        release = resolve_release("owner/app", api_base_override=feed.base_url)
        cfg = {"home": str(tmp_path), "trust_tags": False,
               "api_base": feed.base_url, "token_env": None}
        first, refusal = update_tools._stage_for_apply(
            release, cfg, Journal(str(tmp_path)))
        assert refusal is None
        # a local writer edits a source and its digest in provenance.json and
        # leaves the kept archive, which still matches the signed sha256
        edited = CACHE_V2.replace("43", "99")
        (first.source_root / "cache.rvl").write_text(edited, encoding="utf-8")
        record = first.directory / "provenance.json"
        provenance = json.loads(record.read_text(encoding="utf-8"))
        provenance["sources"]["files"]["cache.rvl"] = hashlib.sha256(
            edited.encode("utf-8")).hexdigest()
        record.write_text(json.dumps(provenance), encoding="utf-8")
        second, refusal = update_tools._stage_for_apply(
            release, cfg, Journal(str(tmp_path)))
    finally:
        feed.close()
    assert refusal is None
    assert (second.source_root / "cache.rvl").read_text(
        encoding="utf-8") == CACHE_V2


@needs_runtime
def test_the_swap_compiles_the_bytes_that_were_hashed(monkeypatch, tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", FIRST)
        staged = Path(planned["staged"]["sources"]["root"]) / "cache.rvl"
        real_swap = server_mod._tool_swap

        def edit_then_swap(arguments):
            # a write that lands after apply re-hashed the staging
            staged.write_text(CACHE_V2.replace("43", "99"), encoding="utf-8")
            return real_swap(arguments)

        monkeypatch.setattr(server_mod, "_tool_swap", edit_then_swap)
        _apply(planned)
        assert "99" in staged.read_text(encoding="utf-8")
        assert _size() == 43
    finally:
        feed.close()


# ------------------------------------- re-review fixes (PR #3002, round 2)

@needs_runtime
def test_a_first_apply_needs_consent_to_an_unknown_version(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        refused = _call("revl_update_apply", {})
        assert refused["ok"] is False
        assert refused["refusal"]["code"] == "UPDATE_VERSION_UNKNOWN"
        assert not (tmp_path / "staged").exists()
        _apply(_call("revl_update_apply", FIRST))
        assert _size() == 43
    finally:
        feed.close()


@needs_runtime
def test_a_hand_swap_after_an_apply_leaves_the_version_unknown_since_it(
        tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        later = CACHE_V2.replace("43", "7")
        assert _call("revl_swap", {"source": later})["swapped"] is True
    finally:
        feed.close()
    older = _release_feed(version="1.0.0", source=CACHE)
    try:
        _configure(older, tmp_path)
        check = _call("revl_update_check", {})
        assert check["comparison"]["relation"] == "unknown"
        assert check["comparison"]["since"] == "9.9.9"
        assert check["applyAvailable"] is False
        refused = _call("revl_update_apply", {})
        assert refused["refusal"]["code"] == "UPDATE_VERSION_UNKNOWN"
        # consent to the unknown version is not consent to a downgrade
        still = _call("revl_update_apply", FIRST)
        assert still["refusal"]["code"] == "UPDATE_DOWNGRADE"
        consented = _call("revl_update_apply",
                          {**FIRST, "allowDowngrade": True})
        assert consented["ok"] is True and consented["planned"] is True
        assert _size() == 7
    finally:
        older.close()


@needs_runtime
def test_a_journal_log_does_not_move_the_version_gate(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        # The journal is a log. A local writer can append an apply record whose
        # irDigest matches the running composition and whose version is lower;
        # the version gate must not read it.
        journal = Journal(str(tmp_path))
        journal.append("apply", planHash="f" * 64, sessionId="forged:1",
                       project="app", version="0.0.1",
                       irDigest=update_tools._ir_digest(server_mod.SESSION.ir),
                       generationBefore=0, generationAfter=1)
        check = _call("revl_update_check", {})
        assert check["runningRelease"]["version"] == "9.9.9"
        assert check["comparison"]["relation"] == "same"
        assert check["comparison"]["updateAvailable"] is False
        # A real restart unloads the session, and its generation history with
        # it: the log names no release, so the version is unknown.
        _call("revl_unload", {})
        update_tools.reset()
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE_V2})
        after = _call("revl_update_check", {})
        assert after["runningRelease"] is None
        assert after["lastApplied"] is None
        assert after["comparison"]["relation"] == "unknown"
    finally:
        feed.close()


@needs_runtime
def test_an_operator_restore_carries_the_applied_release(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        _apply()
        snap = _call("revl_snapshot", {})["snapshot"]
        assert snap["meta"]["appliedRelease"]["version"] == "9.9.9"
        assert server_mod.SESSION._history[-1]["appliedRelease"]["version"] == \
            "9.9.9"
        _call("revl_unload", {})
        update_tools.reset()
        # the operator restore path (no agent profile), like --restore
        restored = server_mod.SESSION.restore(snap)
    finally:
        feed.close()
    assert restored["restored"] is True
    assert restored["appliedRelease"]["version"] == "9.9.9"
    check_feed = _release_feed()
    try:
        _configure(check_feed, tmp_path)
        check = _call("revl_update_check", {})
    finally:
        check_feed.close()
    assert check["runningRelease"]["version"] == "9.9.9"
    assert check["comparison"]["relation"] == "same"


# ------------------------------------------- issue #3021: follow-ups

@needs_runtime
def test_a_consent_flag_the_operator_did_not_grant_is_refused(tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path, consents=())
        _call("revl_load", {"source": CACHE})
        payload = _call("revl_update_apply", {"acceptUnknownVersion": True})
    finally:
        feed.close()
    assert payload["ok"] is False
    assert payload["refusal"]["code"] == "UPDATE_CONSENT_NOT_GRANTED"
    assert "acceptUnknownVersion" in payload["refusal"]["message"]
    assert "--update-allow-consent acceptUnknownVersion" in \
        payload["refusal"]["fix"]
    # refused before staging: the ungranted consent unlocks nothing
    assert not (tmp_path / "staged").exists()


@needs_runtime
def test_acknowledging_a_widening_needs_the_operator_grant(monkeypatch,
                                                           tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path, consents=("acceptUnknownVersion",))
        _call("revl_load", {"source": CACHE})
        monkeypatch.setattr(
            update_tools, "_widening",
            lambda _ir, _files: {"widened": True,
                                 "added": ["emit:MemCache:send"]})
        planned = _call("revl_update_apply", FIRST)
        plan_hash = planned["planHash"]
        ungranted = _call("revl_update_apply",
                          {"planHash": plan_hash,
                           "acknowledgeWidening": True})
        assert ungranted["ok"] is False
        assert ungranted["refusal"]["code"] == "UPDATE_CONSENT_NOT_GRANTED"
        assert ungranted["approvalRequired"] is True
        assert _call("revl_state", {})["generation"] == 1
        # the operator can still grant it for the same plan and session
        _configure(feed, tmp_path,
                   consents=("acceptUnknownVersion", "acknowledgeWidening"))
        accepted = _call("revl_update_apply",
                         {"planHash": plan_hash,
                          "acknowledgeWidening": True})
        assert accepted["ok"] is True and accepted["applied"] is True
    finally:
        feed.close()


def test_applied_directories_are_pruned_to_the_rollback_set(tmp_path):
    journal = Journal(str(tmp_path))
    applied = tmp_path / "applied"
    applied.mkdir()
    update_tools.configure(enabled=True, home=str(tmp_path))
    rollback_target = applied / ("a" * 16 + "-rollback-target")
    consumed = applied / ("b" * 16 + "-consumed")
    legacy = applied / ("c" * 16 + "-legacy")
    orphan = applied / ("d" * 16 + "-orphan")
    foreign = applied / ("f" * 16 + "-other-session")
    for directory in (rollback_target, consumed, legacy, orphan, foreign):
        directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = applied / ("e" * 16 + "-linked")
    linked.symlink_to(outside, target_is_directory=True)
    journal.append("apply", planHash="a" * 64,
                   sessionId=update_tools._SESSION_ID,
                   appliedDirectory=str(rollback_target))
    journal.append("apply", planHash="b" * 64,
                   sessionId=update_tools._SESSION_ID,
                   appliedDirectory=str(consumed))
    # an apply recorded before the directory was named: matched by plan prefix
    journal.append("apply", planHash="c" * 64,
                   sessionId=update_tools._SESSION_ID)
    # another server session's apply is not this session's rollback target
    journal.append("apply", planHash="f" * 64,
                   sessionId="another-session:1",
                   appliedDirectory=str(foreign))
    journal.append("rollback", consumes="b" * 64)
    removed = update_tools._prune_applied(journal)
    assert sorted(removed) == sorted(
        [consumed.name, orphan.name, foreign.name])
    assert rollback_target.is_dir() and legacy.is_dir()
    assert not consumed.exists() and not orphan.exists()
    assert not foreign.exists()
    # a link in the update home is never followed, and nothing behind it goes
    assert linked.is_symlink() and outside.is_dir()


def test_an_unknown_consent_name_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown update consent"):
        update_tools.configure(enabled=True, home=str(tmp_path),
                               consents=("acceptEverything",))


def test_every_plan_consent_needs_both_the_grant_and_the_flag(tmp_path):
    from types import SimpleNamespace

    resolved = SimpleNamespace(version="9.9.9",
                               manifest=SimpleNamespace(live=False))
    comparison = {"running": "9.9.9", "requirements": [], "since": None,
                  "sinceRelation": None}
    cases = {
        "allowDowngrade": ({**comparison, "relation": "older",
                            "compatible": True}, {"force": True}),
        "acceptUnknownVersion": ({**comparison, "relation": "unknown",
                                  "compatible": True}, {}),
        "acceptIncompatible": ({**comparison, "relation": "same",
                                "compatible": False,
                                "requirements": ["gateApi: x"]},
                               {"force": True}),
        "acceptNotLive": ({**comparison, "relation": "same",
                           "compatible": True}, {"force": True}),
    }
    # without the operator's grant, the caller's flag unlocks nothing
    update_tools.configure(enabled=True, home=str(tmp_path), consents=())
    base = {name: False for name in update_tools._CONSENTS}
    for name, (case, extra) in cases.items():
        payload = update_tools._plan_gate(
            resolved, case, {**base, name: True, **extra})
        assert payload["refusal"]["code"] == "UPDATE_CONSENT_NOT_GRANTED", name
        assert name in payload["refusal"]["message"]
    # with the grant, the caller still has to pass its own flag
    update_tools.configure(enabled=True, home=str(tmp_path),
                           consents=update_tools.OPERATOR_CONSENTS)
    expected = {"allowDowngrade": "UPDATE_DOWNGRADE",
                "acceptUnknownVersion": "UPDATE_VERSION_UNKNOWN",
                "acceptIncompatible": "UPDATE_INCOMPATIBLE",
                "acceptNotLive": "UPDATE_NOT_LIVE"}
    for name, (case, extra) in cases.items():
        payload = update_tools._plan_gate(resolved, case, {**base, **extra})
        assert payload["refusal"]["code"] == expected[name], name


def test_the_retained_applied_set_is_bounded_by_the_undo_history(tmp_path):
    journal = Journal(str(tmp_path))
    update_tools.configure(enabled=True, home=str(tmp_path))
    limit = update_tools._undo_history()
    total = limit + 6
    for index in range(total):
        journal.append("apply", planHash=f"{index:064x}",
                       sessionId=update_tools._SESSION_ID,
                       appliedDirectory=str(tmp_path / "applied"
                                            / f"{index:016x}-dir"))
    names, _prefixes = update_tools._retained_applied_directories(journal)
    assert len(names) == limit
    # the newest applies are the ones a rollback can still reach
    assert f"{total - 1:016x}-dir" in names
    assert "0000000000000000-dir" not in names


@needs_runtime
def test_a_refused_swap_leaves_no_applied_directory(monkeypatch, tmp_path):
    feed = _release_feed()
    try:
        _configure(feed, tmp_path)
        _call("revl_load", {"source": CACHE})
        planned = _call("revl_update_apply", FIRST)
        monkeypatch.setattr(server_mod, "_tool_swap",
                            lambda arguments: {"ok": False, "refusal": {}})
        refused = _call("revl_update_apply", {"planHash": planned["planHash"]})
        assert refused["stoppedAt"] == "swap"
        assert _call("revl_state", {})["generation"] == 1
        assert list((tmp_path / "applied").iterdir()) == []
    finally:
        feed.close()
