"""The registry's versioned publish path — roadmap item 49 phase 2.

Phase 0 was a read path and the one write path on top of it could publish a
name exactly ONCE (first-come, by "the directory did not exist"). There was
therefore no second release, nothing for item 64's computed bump to be checked
against, and nothing for item 261's derived changelog to be derived *across*.
These pin the update flow that closes that:

  * a release is DECLARED, and a published release is immutable;
  * the declared version must satisfy the bump `version.derive` COMPUTES from
    the interface diff against the release being replaced — an under-bump is
    refused by name, an over-bump is not (it misleads nobody);
  * where the bump cannot be computed the publish is REFUSED, not waved
    through, unless the publisher declares an opaque scheme — and then the
    release records `cannot verify` forever;
  * an unversioned entry can never be replaced (nothing to bump from);
  * a name does not change publisher silently;
  * every release freezes its bytes, its manifest, its record and its derived
    changelog under `releases/<version>/`, so the chain survives the next
    publish and the registry stays regenerate-or-red.

These need no cordis runtime — `publish_release` is the frontend compiler plus
`registry.build_index` — so the CI frontend job actually enforces them.
"""

import json

import pytest

from revl import registry
from revl.errors import RevlError

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

# an added method: additive surface, so item 64 computes MINOR.
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

# an arity change on a live operation: a breaking reshape, so item 64 computes
# MAJOR — every running consumer's call site is invalidated.
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


@pytest.fixture()
def reg(tmp_path):
    """An empty registry, the shape `truc ship` creates."""
    root = tmp_path / "registry"
    (root / "components").mkdir(parents=True)
    (root / "index.json").write_text(
        json.dumps({"indexVersion": "0", "components": {}}), encoding="utf-8")
    return root


def _entry(reg, name="greeter"):
    return reg / "components" / name


def _row(reg, name="greeter"):
    return json.loads((reg / "index.json").read_text())["components"][name]


# ------------------------------------------------------------ the first release

def test_a_first_release_is_recorded_frozen_and_indexed(reg):
    record = registry.publish_release(reg, "greeter", V1, version="1.0.0")

    assert record["version"] == "1.0.0"
    assert record["previousVersion"] == ""
    assert record["bumpCheck"] == registry.BUMP_FIRST_RELEASE
    # the entry declares the release, and the release is frozen beside it.
    assert (_entry(reg) / "version").read_text().strip() == "1.0.0"
    assert registry.released_versions(_entry(reg)) == ["1.0.0"]
    frozen = _entry(reg) / "releases" / "1.0.0"
    assert (frozen / "component.rvl").read_text() == V1
    assert json.loads((frozen / "manifest.json").read_text())
    # a first release has nothing to diff against and gets no changelog rather
    # than an invented one.
    assert registry.release_changelog(reg, "greeter", "1.0.0") is None
    # and the index the publish leaves behind is the one CI would regenerate.
    assert registry.verify(reg) == []
    assert _row(reg)["version"] == "1.0.0"
    assert _row(reg)["releases"] == ["1.0.0"]


def test_the_discoverability_fields_ride_into_the_published_row(reg):
    """description/tags are the two fields the compiler cannot derive, so the
    publish carries them into the row (they are not part of the regenerated
    surface `verify` reproduces)."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0",
                             description="greets", tags=["greet"])

    assert _row(reg)["description"] == "greets"
    assert _row(reg)["tags"] == ["greet"]


def test_an_unversioned_first_release_stays_legal_and_carries_no_release_row(reg):
    """§1.1's honest-degradation rule survives: nothing is invented for an entry
    that declares no version. It simply cannot be UPDATED (see below)."""
    record = registry.publish_release(reg, "greeter", V1)

    assert record["version"] == ""
    assert not (_entry(reg) / "version").exists()
    assert registry.released_versions(_entry(reg)) == []
    assert "releases" not in _row(reg)
    assert "version" not in _row(reg)
    assert registry.verify(reg) == []


# ----------------------------------------------------------------- the update

def test_an_update_that_satisfies_the_computed_bump_publishes(reg):
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    record = registry.publish_release(reg, "greeter", V1_PLUS, version="1.1.0")

    assert record["previousVersion"] == "1.0.0"
    assert record["computedBump"] == "minor"
    assert record["bumpCheck"] == registry.BUMP_VERIFIED
    assert registry.released_versions(_entry(reg)) == ["1.0.0", "1.1.0"]
    # the release it replaced keeps its own bytes: the chain is diffable and a
    # substitution is visible rather than silent.
    assert (_entry(reg) / "releases" / "1.0.0" / "component.rvl").read_text() == V1
    assert (_entry(reg) / "component.rvl").read_text() == V1_PLUS
    assert _row(reg)["releases"] == ["1.0.0", "1.1.0"]
    assert registry.verify(reg) == []


def test_an_over_bump_is_not_a_contradiction(reg):
    """Declaring 2.0.0 where a minor suffices is conservative and misleads
    nobody; only an UNDER-bump is a contradiction."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    record = registry.publish_release(reg, "greeter", V1_PLUS, version="2.0.0")

    assert record["computedBump"] == "minor"
    assert record["bumpCheck"] == registry.BUMP_VERIFIED


def test_the_derived_changelog_is_attached_to_the_release(reg):
    """Item 261's registry-attach half: the release note is computed from the
    same two IRs the bump was read off, and stored with the release."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    record = registry.publish_release(reg, "greeter", V2_BREAKING, version="2.0.0")

    changelog = registry.release_changelog(reg, "greeter", "2.0.0")
    assert changelog is not None
    # item 64's computed bump is the headline.
    assert changelog["headline"]["bump"] == "major"
    assert record["headline"] == changelog["headline"]
    assert changelog["generatedFrom"] == {"fromLabel": "greeter@1.0.0",
                                          "toLabel": "greeter@2.0.0"}
    # the breaking reshape is a classified line, not an honesty line.
    assert any("hello" in line["text"] for line in changelog["breaking"])


# ------------------------------------------------------------------- refusals

def _refused(reg, **kwargs) -> str:
    with pytest.raises(RevlError) as caught:
        registry.publish_release(reg, "greeter", kwargs.pop("source"), **kwargs)
    return str(caught.value)


def test_an_under_bump_is_refused_by_name(reg):
    """Item 64's registry-refusal half. The version number is a measurement."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    message = _refused(reg, source=V2_BREAKING, version="1.0.1")

    assert "contradicts the computed bump" in message
    assert "requires a major bump (2.0.0 or later)" in message
    # and the registry is untouched: the published release still stands.
    assert (_entry(reg) / "component.rvl").read_text() == V1
    assert registry.released_versions(_entry(reg)) == ["1.0.0"]
    assert registry.verify(reg) == []


def test_a_published_release_is_immutable(reg):
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    message = _refused(reg, source=V1_PLUS, version="1.0.0")

    assert "a release is immutable" in message
    assert (_entry(reg) / "component.rvl").read_text() == V1


def test_a_version_that_goes_backwards_is_refused(reg):
    registry.publish_release(reg, "greeter", V1, version="1.1.0")
    message = _refused(reg, source=V1_PLUS, version="1.0.0")

    assert "does not follow" in message


def test_an_update_must_declare_its_release(reg):
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    message = _refused(reg, source=V1_PLUS)

    assert "an update must declare which release it is" in message


def test_an_unversioned_entry_cannot_be_replaced(reg):
    """Fail closed. Nothing recorded which release the published bytes are, so
    there is no bump to compute and no claim a new version could be checked
    against — the publish is refused, never rounded down to a pass."""
    registry.publish_release(reg, "greeter", V1)
    message = _refused(reg, source=V1_PLUS, version="1.1.0")

    assert "declares no version" in message
    assert "no computed bump to check a new one against" in message
    assert (_entry(reg) / "component.rvl").read_text() == V1


def test_a_source_that_does_not_compile_never_reaches_the_registry(reg):
    message = _refused(reg, source="component Broken { this is not revl }",
                       version="1.0.0")

    assert "does not compile" in message
    assert not _entry(reg).exists()


def test_an_unusable_declared_version_is_refused(reg):
    message = _refused(reg, source=V1, version="1.0.0 or thereabouts")

    assert "is not one token matching" in message
    assert not _entry(reg).exists()


# --------------------------------------------------- versions that cannot be checked

def test_an_uncheckable_bump_is_refused_under_the_default_scheme(reg):
    """A date or build id cannot be compared against a computed bump. Publishing
    anyway would ship a version number nothing checked, so the default refuses
    and says which opt-out exists."""
    registry.publish_release(reg, "greeter", V1, version="2026-01-01")
    message = _refused(reg, source=V2_BREAKING, version="2026-02-01")

    assert "cannot be verified" in message
    assert 'version_scheme = "opaque"' in message
    assert (_entry(reg) / "component.rvl").read_text() == V1


def test_an_opaque_scheme_publishes_but_records_cannot_verify(reg):
    """The opt-out is explicit and it is RECORDED — a consumer reading the
    release sees `cannot verify`, never a pass."""
    registry.publish_release(reg, "greeter", V1, version="2026-01-01",
                             scheme=registry.SCHEME_OPAQUE)
    record = registry.publish_release(reg, "greeter", V2_BREAKING,
                                      version="2026-02-01",
                                      scheme=registry.SCHEME_OPAQUE)

    assert record["bumpCheck"] == registry.BUMP_UNVERIFIABLE
    assert "not both MAJOR.MINOR.PATCH" in record["bumpCheckReason"]
    # the bump is still COMPUTED and recorded; only the comparison is impossible.
    assert record["computedBump"] == "major"
    assert registry.released_versions(_entry(reg)) == ["2026-01-01", "2026-02-01"]


def test_an_unknown_scheme_is_refused(reg):
    message = _refused(reg, source=V1, version="1.0.0", scheme="calendar")

    assert "unknown version scheme" in message


# ---------------------------------------------------------- publisher continuity

def test_a_name_does_not_change_publisher_silently(reg):
    registry.publish_release(reg, "greeter", V1, version="1.0.0",
                             publisher="acme")
    message = _refused(reg, source=V1_PLUS, version="1.1.0", publisher="squatter")

    assert "does not change hands silently" in message
    assert (_entry(reg) / "component.rvl").read_text() == V1

    # dropping the claim entirely is refused the same way.
    assert "does not change hands silently" in _refused(
        reg, source=V1_PLUS, version="1.1.0")


def test_the_same_publisher_continues_and_the_release_records_it(reg):
    registry.publish_release(reg, "greeter", V1, version="1.0.0", publisher="acme")
    record = registry.publish_release(reg, "greeter", V1_PLUS, version="1.1.0",
                                      publisher="acme")

    assert record["publisherContinuity"] == registry.BUMP_VERIFIED
    assert record["publisher"] == "acme"


def test_an_entry_with_no_recorded_publisher_says_it_cannot_verify_continuity(reg):
    """Honest degradation: there is no identity to continue, so the release says
    so rather than reading as a verified handover. Authenticated publisher
    identity is the remaining half of phase 2 (docs/registry.md §7)."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    record = registry.publish_release(reg, "greeter", V1_PLUS, version="1.1.0")

    assert record["publisherContinuity"] == registry.BUMP_UNVERIFIABLE


# ------------------------------------------------------- entries from before phase 2

def test_replacing_an_entry_that_predates_release_history_archives_it_honestly(reg):
    """An entry committed by hand (the phase 0 path) has bytes and a version but
    no release directory. Its archive is written when it is replaced, and its
    record says that is when it was written — not that it was checked."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    # simulate the phase-0 shape: the entry, but no frozen release history.
    import shutil
    shutil.rmtree(_entry(reg) / "releases")
    registry.build_index(reg)
    assert "releases" not in _row(reg)

    registry.publish_release(reg, "greeter", V1_PLUS, version="1.1.0")

    archived = json.loads(
        (_entry(reg) / "releases" / "1.0.0" / "release.json").read_text())
    assert archived["version"] == "1.0.0"
    assert archived["bumpCheck"] == registry.BUMP_UNVERIFIABLE
    assert "archived when it was replaced" in archived["bumpCheckReason"]
    assert (_entry(reg) / "releases" / "1.0.0" / "component.rvl").read_text() == V1
    assert registry.verify(reg) == []


def test_a_frozen_release_is_never_rewritten(reg):
    """Freezing is idempotent and one-way: a release directory that exists is
    the release, and a later publish does not touch it."""
    registry.publish_release(reg, "greeter", V1, version="1.0.0")
    frozen = _entry(reg) / "releases" / "1.0.0" / "component.rvl"
    before = frozen.read_text()

    registry.publish_release(reg, "greeter", V1_PLUS, version="1.1.0")

    assert frozen.read_text() == before == V1
