"""`registry.resolve(release=...)`: selecting among releases (issue #2685).

A resolve used to read the current release of each entry only; the frozen
history under `releases/<version>/` (docs/registry.md §1.2) was a record, not a
selectable set. These pin the selection:

  * the default is unchanged: current releases only, and the answer carries
    exactly the keys it always had;
  * a version selects the entries that are that release, current or frozen, and
    `*` considers every release of every entry;
  * a candidate names its `version` and whether it is `frozen`, and at equal fit
    the current release ranks first;
  * a release the current registry no longer satisfies is still selectable: the
    point of selecting among releases is a consumer pinned to an older one;
  * a frozen release is checked, not believed: bytes that no longer hash to what
    its own record froze refuse it, and it is never credited with the evidence
    of the current release;
  * a registry loaded without its history refuses the request rather than
    answering about the wrong set.

These need no cordis runtime: `publish_release` is the frontend compiler plus
`registry.build_index`.
"""

import json

import pytest

from revl import registry, registry_evidence
from revl.errors import RevlError

KEY = b"revl-registry-2685-release-fixture-key"
NOW = "2026-01-01T00:00:00+00:00"

V1 = """\
service Greet {
  fn hello(name: Str) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name)
  }
}
"""

# the same interface and a longer body: a patch release
V1_PATCH = """\
service Greet {
  fn hello(name: Str) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name).concat(", and welcome to the registry")
  }
}
"""

# an added method: additive, so a MINOR bump
V1_PLUS = """\
service Greet {
  fn hello(name: Str) -> Str
  fn bye(name: Str) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name)
    fn bye(name) = "bye, ".concat(name)
  }
}
"""

# an arity change: breaking, so MAJOR; a consumer of v1 cannot use it
V2_BREAKING = """\
service Greet {
  fn hello(name: Str, loud: Bool) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name, loud) = "hello, ".concat(name)
  }
}
"""

# what a consumer of v1 needs
NEED = """
service Wants {
  fn hello(name: Str) -> Str
}
"""


@pytest.fixture()
def reg(tmp_path):
    root = tmp_path / "registry"
    (root / "components").mkdir(parents=True)
    (root / "index.json").write_text(
        json.dumps({"indexVersion": "0", "components": {}}), encoding="utf-8")
    return root


def _publish(reg, source, version):
    registry.publish_release(reg, "greeter", source, version=version)


@pytest.fixture()
def three_releases(reg):
    """1.0.0 and 1.1.0 frozen; 2.0.0 (breaking) is the current release."""
    _publish(reg, V1, "1.0.0")
    _publish(reg, V1_PLUS, "1.1.0")
    _publish(reg, V2_BREAKING, "2.0.0")
    return reg


def _versions(result):
    return [c["version"] for c in result["candidates"]]


# ------------------------------------------------------------------ default

def test_the_default_reads_the_current_release_only(three_releases):
    # the current release is the breaking 2.0.0, which a v1 consumer cannot use,
    # so the default finds nothing even though older releases are on disk
    result = registry.resolve(three_releases, NEED, adapt=False)
    assert result["candidates"] == []
    assert "releaseSelection" not in result


def test_the_default_answer_has_exactly_the_keys_it_always_had(reg):
    _publish(reg, V1, "1.0.0")
    result = registry.resolve(reg, NEED, adapt=False)
    [candidate] = result["candidates"]
    assert "version" not in candidate and "frozen" not in candidate
    assert "releaseSelection" not in result
    # and a registry loaded without its history holds no frozen entry
    loaded = registry.Registry.from_dir(reg)
    assert loaded.releases_loaded is False
    assert not any(e.frozen for e in loaded.entries)


# ---------------------------------------------------------------- selection

def test_every_release_is_selectable(three_releases):
    result = registry.resolve(three_releases, NEED, release="*", adapt=False)
    assert result["releaseSelection"] == {"release": "*", "considered": 3}
    # v1 (identical interface) outranks v1.1 (a compatible superset) on fit;
    # the breaking current release is filtered by the §5 gate, not by age
    assert _versions(result) == ["1.0.0", "1.1.0"]
    assert [c["frozen"] for c in result["candidates"]] == [True, True]


def test_a_version_selects_only_that_release(three_releases):
    result = registry.resolve(three_releases, NEED, release="1.1.0", adapt=False)
    assert _versions(result) == ["1.1.0"]
    assert result["releaseSelection"] == {"release": "1.1.0", "considered": 1}
    assert "only release 1.1.0" in " ".join(result["assumptions"])


def test_the_current_release_is_selectable_by_its_version(three_releases):
    result = registry.resolve(three_releases, "service W { fn hello(name: Str, loud: Bool) -> Str }",
                              release="2.0.0", adapt=False)
    [candidate] = result["candidates"]
    assert (candidate["version"], candidate["frozen"]) == ("2.0.0", False)


def test_an_unknown_release_finds_nothing(three_releases):
    result = registry.resolve(three_releases, NEED, release="9.9.9", adapt=False)
    assert result["candidates"] == []
    assert result["releaseSelection"]["considered"] == 0


def test_the_selected_candidate_is_the_frozen_bytes(three_releases):
    result = registry.resolve(three_releases, NEED, release="1.0.0", adapt=False)
    [candidate] = result["candidates"]
    frozen = (three_releases / "components" / "greeter" / "releases" / "1.0.0"
              / "component.rvl").read_text(encoding="utf-8")
    assert candidate["source"] == frozen == V1
    assert "bye" not in candidate["source"]


# ------------------------------------------------------------------ ranking

def test_at_equal_fit_the_current_release_ranks_first(reg):
    # a patch release: the same interface, so both releases fit the need
    # exactly. The older source is the SHORTER one, which would win the old
    # tiebreak; the current release wins on age instead.
    _publish(reg, V1, "1.0.0")
    _publish(reg, V1_PATCH, "1.0.1")
    result = registry.resolve(reg, NEED, release="*", adapt=False)
    assert _versions(result) == ["1.0.1", "1.0.0"]
    assert [c["frozen"] for c in result["candidates"]] == [False, True]
    assert len(result["candidates"][0]["source"]) > len(
        result["candidates"][1]["source"])


# --------------------------------------------------------------- not believed

def test_a_frozen_release_is_never_credited_with_the_current_evidence(reg):
    # the current release carries a signed attestation; the frozen one must not
    # borrow it, even though both are the same component name
    _publish(reg, V1, "1.0.0")
    _publish(reg, V1_PLUS, "1.1.0")
    registry_evidence.build_evidence(reg, key=KEY, now=NOW, signer="revl-ci",
                                     publisher="revl-ci")
    result = registry.resolve(reg, NEED, release="*", key=KEY, adapt=False)
    by_version = {c["version"]: c for c in result["candidates"]}
    assert by_version["1.1.0"]["evidence"]["facets"]["attestation"] == "valid"
    frozen = by_version["1.0.0"]
    assert frozen["frozen"] is True
    assert frozen["evidence"]["facets"]["attestation"] == "unavailable"
    assert frozen["evidence"]["present"] == []
    assert "dossier" not in frozen


def test_a_tampered_frozen_release_is_refused(three_releases):
    frozen = (three_releases / "components" / "greeter" / "releases" / "1.0.0"
              / "component.rvl")
    frozen.write_text(V1.replace("hello, ", "pwned, "), encoding="utf-8")
    result = registry.resolve(three_releases, NEED, release="*", adapt=False)
    assert _versions(result) == ["1.1.0"]
    [refusal] = result["refused"]
    assert (refusal["name"], refusal["version"]) == ("greeter", "1.0.0")
    assert "was changed" in refusal["reasons"][0]


def test_a_release_the_current_compiler_cannot_read_is_refused_not_crashed(
        three_releases):
    frozen = (three_releases / "components" / "greeter" / "releases" / "1.0.0")
    (frozen / "component.rvl").write_text("this is not revl", encoding="utf-8")
    result = registry.resolve(three_releases, NEED, release="*", adapt=False)
    refused = {(r["name"], r.get("version")): r["reasons"]
               for r in result["refused"]}
    assert ("greeter", "1.0.0") in refused
    assert any("does not compile" in reason
               for reason in refused[("greeter", "1.0.0")])
    assert _versions(result) == ["1.1.0"]


def test_a_current_release_changed_after_it_was_frozen_is_refused(
        three_releases):
    # publish_release froze 2.0.0 when it was published. Editing the current
    # component.rvl afterwards leaves the frozen copy as the witness, and the
    # copy is kept and refused rather than skipped as the "same" release.
    current = three_releases / "components" / "greeter" / "component.rvl"
    current.write_text(V2_BREAKING + "\n// edited after publish\n",
                       encoding="utf-8")
    result = registry.resolve(three_releases, NEED, release="2.0.0", adapt=False)
    # the edited current entry is refused by the index check as it always was,
    # and the frozen copy is kept as its own refusal naming the cause
    assert len([r for r in result["refused"] if r["version"] == "2.0.0"]) == 2
    twin = [r for r in result["refused"]
            if "changed after it was frozen" in " ".join(r["reasons"])]
    assert len(twin) == 1


# -------------------------------------------------------------- misuse

def test_a_registry_without_its_history_refuses_the_request(reg):
    _publish(reg, V1, "1.0.0")
    loaded = registry.Registry.from_dir(reg)
    with pytest.raises(RevlError, match="release history"):
        loaded.resolve(NEED, release="*", adapt=False)


def test_a_loaded_registry_with_its_history_selects(three_releases):
    loaded = registry.Registry.from_dir(three_releases, releases=True)
    assert loaded.releases_loaded is True
    result = loaded.resolve(NEED, release="1.0.0", adapt=False)
    assert _versions(result) == ["1.0.0"]


@pytest.mark.parametrize("bad", ["", 3])
def test_a_malformed_release_is_refused(three_releases, bad):
    with pytest.raises(RevlError, match="release must be a version string"):
        registry.resolve(three_releases, NEED, release=bad, adapt=False)


# ---------------------------------------- the default path is not disturbed

def test_a_malformed_version_file_does_not_refuse_a_default_resolve(reg):
    _publish(reg, V1, "1.0.0")
    (reg / "components" / "greeter" / "version").write_text(
        "bad token!", encoding="utf-8")
    default = registry.resolve(reg, NEED, adapt=False)
    # exactly what a resolve served before releases could be selected
    assert [c["name"] for c in default["candidates"]] == ["greeter"]
    assert default["refused"] == []
    # a release-aware load does read the file, and says so on the entry
    aware = registry.resolve(reg, NEED, release="*", adapt=False)
    assert "version file is unusable" in aware["refused"][0]["reasons"][0]
    # the current entry cannot be labelled, so it is refused; its frozen copy
    # is still a release of its own and is served as one
    assert [(c["version"], c["frozen"]) for c in aware["candidates"]] == [
        ("1.0.0", True)]


def test_an_unversioned_entry_is_selectable_with_the_star_only(reg):
    entry = reg / "components" / "plain"
    entry.mkdir(parents=True)
    (entry / "component.rvl").write_text(V1, encoding="utf-8")
    registry.build_index(reg)
    assert _names(registry.resolve(reg, NEED, adapt=False)) == ["plain"]
    star = registry.resolve(reg, NEED, release="*", adapt=False)
    assert [(c["name"], c["version"], c["frozen"])
            for c in star["candidates"]] == [("plain", None, False)]
    assert registry.resolve(reg, NEED, release="1.0.0",
                            adapt=False)["candidates"] == []


def _names(result):
    return [c["name"] for c in result["candidates"]]


def test_two_entries_with_releases_are_ranked_and_labelled(reg):
    registry.publish_release(reg, "alpha", V1, version="1.0.0")
    registry.publish_release(reg, "alpha", V1_PATCH, version="1.0.1")
    registry.publish_release(reg, "beta", V1, version="3.0.0")
    result = registry.resolve(reg, NEED, release="*", adapt=False)
    labelled = [(c["name"], c["version"], c["frozen"])
                for c in result["candidates"]]
    # current releases first; the frozen alpha release after them
    assert labelled[-1] == ("alpha", "1.0.0", True)
    assert {(n, v) for n, v, f in labelled if not f} == {
        ("alpha", "1.0.1"), ("beta", "3.0.0")}
    assert result["releaseSelection"]["considered"] == 3


# -------------------------------------------- frozen releases, read carefully

def _frozen(reg, version):
    return reg / "components" / "greeter" / "releases" / version


def test_a_missing_release_record_refuses_the_release(three_releases):
    (_frozen(three_releases, "1.0.0") / "release.json").unlink()
    result = registry.resolve(three_releases, NEED, release="*", adapt=False)
    assert _versions(result) == ["1.1.0"]
    [refusal] = result["refused"]
    assert "release.json is missing or unreadable" in refusal["reasons"][0]


def test_a_tampered_source_cannot_hide_behind_a_deleted_record(three_releases):
    frozen = _frozen(three_releases, "1.0.0")
    (frozen / "component.rvl").write_text(V1.replace("hello, ", "pwned, "),
                                          encoding="utf-8")
    (frozen / "release.json").unlink()
    result = registry.resolve(three_releases, NEED, release="*", adapt=False)
    assert "1.0.0" not in _versions(result)
    assert [r["version"] for r in result["refused"]] == ["1.0.0"]


def test_a_corrupt_manifest_refuses_one_release_not_the_load(three_releases):
    (_frozen(three_releases, "1.0.0") / "manifest.json").write_text(
        "{ not json", encoding="utf-8")
    result = registry.resolve(three_releases, NEED, release="*", adapt=False)
    assert _versions(result) == ["1.1.0"]
    reasons = " ".join(result["refused"][0]["reasons"])
    assert "manifest.json is not valid JSON" in reasons


def test_a_crlf_source_is_not_falsely_refused(reg):
    crlf = V1.replace("\n", "\r\n")
    registry.publish_release(reg, "greeter", crlf, version="1.0.0")
    registry.publish_release(reg, "greeter", V1_PLUS, version="1.1.0")
    result = registry.resolve(reg, NEED, release="1.0.0", adapt=False)
    assert result["refused"] == []
    assert _versions(result) == ["1.0.0"]


def test_an_unusable_current_meta_does_not_refuse_the_history(three_releases):
    (three_releases / "components" / "greeter" / "meta.json").write_text(
        "{ broken", encoding="utf-8")
    result = registry.resolve(three_releases, NEED, release="1.0.0", adapt=False)
    assert _versions(result) == ["1.0.0"]
    assert result["refused"] == []


def test_non_semver_versions_are_selectable(reg):
    registry.publish_release(reg, "greeter", V1, version="2026-01",
                             scheme=registry.SCHEME_OPAQUE)
    registry.publish_release(reg, "greeter", V1_PATCH, version="2026-02",
                             scheme=registry.SCHEME_OPAQUE)
    result = registry.resolve(reg, NEED, release="2026-01", adapt=False)
    assert [(c["version"], c["frozen"]) for c in result["candidates"]] == [
        ("2026-01", True)]


# ---------------------------------------- policy and mediator, by release

def test_a_would_be_refused_marker_names_the_frozen_release(three_releases):
    from revl.policy import parse_policy
    policy = parse_policy(
        "component * requires evidence [attestation valid]\n")
    result = registry.resolve(three_releases, NEED, release="*", adapt=False,
                              policy=policy)
    predicted = result["policyPreview"]["predicted"]
    assert "greeter@1.0.0" in predicted


FS_MEDIATOR = """
service FileStore { emission[fs] fn write(path: Str, data: Str) -> Int }
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }

component FsMediator requires fs: FileStore provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int {
      emit fs.write(path, data)
      return 0
    }
  }
}
"""
WRITER_NEED = "service W { emission fn append(path: Str, data: Str) -> Int }"


def test_a_frozen_release_of_a_mediator_does_not_inherit_its_protection(reg):
    registry.publish_release(reg, "fsw", FS_MEDIATOR, version="1.0.0")
    registry.publish_release(reg, "fsw", FS_MEDIATOR + "\n// patch\n",
                             version="1.0.1")
    (reg / "components" / "fsw" / "meta.json").write_text(json.dumps({
        "description": "the only holder of the raw fs boundary",
        "tags": ["fs"],
        "mediator": {"component": "FsMediator", "capability": "fs"},
    }), encoding="utf-8")
    registry.build_index(reg)
    result = registry.resolve(reg, WRITER_NEED, release="*", adapt=False)
    by_version = {c["version"]: c for c in result["candidates"]}
    # the current release is the protected incumbent; the frozen release has no
    # marking of its own, so it is a replacement that must clear the bar
    assert "replacementBar" not in by_version["1.0.1"]
    assert by_version["1.0.0"]["replacementBar"]["met"] is False
