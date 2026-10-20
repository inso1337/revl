"""An exported source published to the registry is profiled as author-written.

Roadmap item 49 wrote the registry's publish path so that a component's source
travels as TEXT, compiled under a STABLE VIRTUAL LABEL
(`/__revl_registry__/<name>/component.rvl`) rather than under the publisher's
real path, so a manifest does not depend on where a working copy happens to
sit. That was the right call for reproducibility and it quietly disarmed issue
#2546's export rule on this path: `export_area.CliRule` decides authorship from
the name the text is compiled under, and that label is a name no file answers
to. Text an agent exported into `.revl/export/` therefore published with the
operator's own trust, whatever it declares (issue #2563, first slice).

These pin the publish half of that:

  * publishing bytes whose ORIGIN is under `.revl/export/` compiles them under
    the untrusted-author profile, so a source declaring host code is REFUSED;
  * the same bytes from a file outside the export area still publish, so the
    rule reads the ORIGIN and not the content;
  * the same bytes with no origin at all still publish as they did before, so a
    caller holding text from no file is not thereby accused.

The last two are not decoration: without them the first would be satisfied by
a source that never compiles at all, which is exactly the unreachable
assertion that has cost earlier changes dear.

No cordis runtime: `publish_release` is the frontend compiler plus
`registry.build_index`, so the CI frontend job enforces these.
"""

import inspect

import pytest

from revl import registry
from revl.errors import RevlError

# The untrusted-author profile forbids a new `extern`/host-block declaration
# (`admit_profile.check_no_extern`, G8) and nothing else about this source is
# unusual: it parses, typechecks and provides its service, and the OPERATOR's
# own component may declare this extern freely. That one difference is what the
# three publishes below turn on.
EXPORTED = """\
service Greet {
  fn hello(name: Str) -> Str
}
extern emission[db] fn pg_write(row: Str) -> Int = @py { return 0 }
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name)
  }
}
"""

PROFILED = "untrusted-author profile forbids"

_EXPORT_PARTS = (".revl", "export")


def _empty_registry(root):
    """An empty registry at `root`: no entries, the empty index."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "components").mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        '{\n  "components": {},\n  "indexVersion": "0"\n}\n', encoding="utf-8")
    return root


@pytest.fixture()
def reg(tmp_path):
    """An empty registry, the shape `truc ship` creates."""
    return _empty_registry(tmp_path / "registry")


def _takes_source_path() -> bool:
    """Whether this build's `publish_release` knows where the bytes came from.

    The two calls below are written against the SHAPE the fix has, not against
    its signature, so that what the test measures is the PUBLISH and not the
    keyword: on base (before the fix) the exported text is simply published —
    the bug — and the assertion below fails with "DID NOT RAISE" rather than
    with a `TypeError` that says nothing about behaviour. A base run therefore
    demonstrates the hole; a fixed run demonstrates it closed.
    """
    return "source_path" in inspect.signature(registry.publish_release).parameters


def _publish(reg, name, source, *, source_path=None, **kwargs) -> dict:
    """`publish_release`, naming the file `source` was read from when this
    build understands that. See `_takes_source_path`."""
    if not _takes_source_path():
        return registry.publish_release(reg, name, source, **kwargs)
    return registry.publish_release(reg, name, source,
                                    source_path=source_path, **kwargs)


def _exported_file(tmp_path, text=EXPORTED):
    """The bug's shape: `tmp_path/.revl/export/component.rvl`, which is where
    `revl_export` writes what an agent produced (issue #2546). It lies OUTSIDE
    the registry, so publishing its bytes is a plausible-looking publish of the
    operator's own project entry."""
    path = tmp_path.joinpath(*_EXPORT_PARTS, "component.rvl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_publishing_exported_text_is_refused(reg, tmp_path):
    """Slice 1 of issue #2563. Publishing bytes whose ORIGIN is the export area
    must compile them under the authoring profile, because they are not the
    operator's text whatever they say.

    BEFORE the fix `_compile_source` named this text by the virtual label
    `/__revl_registry__/greeter/component.rvl`. `CliRule.exported` asks whether
    that name is a FILE inside an export directory; it is not a file at all, so
    the rule answered no, the compile ran with NO profile, the extern was
    admitted and the publish succeeded: an exported file reached the registry
    carrying the operator's trust. AFTER the fix the publish names the text by
    the real file, the rule answers yes, the authoring profile is installed and
    `enforce_source` raises the extern refusal, which `release_facts` reports as
    `does not compile`.

    The assertion is REACHED, and the two tests below are what makes that
    checkable rather than merely claimed. `test_the_same_bytes_from_outside_the
    _export_area_publish` runs the SAME BYTES through a REAL FILE and they
    publish; `test_in_memory_text_from_no_file_still_publishes` runs them
    through NO file and they publish. Only the origin differs across the three,
    so nothing about this source apart from its authorship can be what refuses
    it — the refusal cannot be a parse defect, a missing provide, or an effect
    of naming a real path instead of a label, because each of those is held
    constant and publishing elsewhere in this file.
    """
    exported = _exported_file(tmp_path)

    with pytest.raises(RevlError) as caught:
        _publish(reg, "greeter", exported.read_text(), version="1.0.0",
                 source_path=exported)

    message = str(caught.value)
    assert "does not compile" in message
    assert PROFILED in message
    # refused before anything is written: no entry, still the empty index.
    assert not (reg / "components" / "greeter").exists()
    assert (reg / "index.json").read_text().count("greeter") == 0


def test_the_same_bytes_from_outside_the_export_area_publish(reg, tmp_path):
    """The control for the content axis: identical bytes, from a plain project
    directory instead. BEFORE the fix both calls behaved identically, so this
    one published then too; AFTER it still publishes, because the rule's
    question is WHERE the file sits and this one sits nowhere named
    `.revl/export/`.

    This also holds constant everything that naming a real path changes (the
    loader reads the file, the per-root profile map is keyed by its abspath),
    so those cannot be what refused the exported publish above.
    """
    own = tmp_path / "src" / "component.rvl"
    own.parent.mkdir(parents=True)
    own.write_text(EXPORTED, encoding="utf-8")

    record = _publish(reg, "greeter", own.read_text(), version="1.0.0",
                      source_path=own)

    assert record["version"] == "1.0.0"
    assert (reg / "components" / "greeter" / "component.rvl").read_text() == EXPORTED
    assert registry.verify(reg) == []


def test_the_same_bytes_from_an_exported_file_are_refused_through_facts(reg, tmp_path):
    """The same rule read through `release_facts`, which is what `truc ship`
    consults to phrase a refusal before it writes. Keeps the gate and the write
    path from drifting apart, the way
    `test_release_facts_flags_a_traversal_name_as_a_refusal` does for #538."""
    exported = _exported_file(tmp_path)
    facts = (registry.release_facts(reg, "greeter", exported.read_text(), "1.0.0",
                                    source_path=exported)
             if _takes_source_path()
             else registry.release_facts(reg, "greeter",
                                         exported.read_text(), "1.0.0"))

    assert facts["refusals"]
    assert any(PROFILED in refusal for refusal in facts["refusals"])


def test_in_memory_text_from_no_file_still_publishes(reg):
    """The control for the missing-origin axis, and the guard against this change
    having inverted the default by accident. Later slices will meet callers that
    hold registry text from no file at all (an MCP repair candidate, a
    complaint); none is then the honest answer, and it must not be read as an
    accusation — the compile keeps item 49's virtual label and no profile, so
    these callers publish exactly as they do today.

    BEFORE the fix this is the behaviour of EVERY caller, exported or not, and
    that is the bug. It stays the behaviour of the source-less ones, which is
    what distinguishes this slice from a rule inversion.
    """
    record = _publish(reg, "greeter", EXPORTED, version="1.0.0")

    assert record["version"] == "1.0.0"
    assert registry.verify(reg) == []


def test_a_real_origin_does_not_change_the_release_bytes(reg, tmp_path):
    """Naming where the bytes came from must not change what the release IS.

    `attest.path_normalized_ir` reduces each component's `source` and `file` to
    their basename before hashing, and both spellings involved here end in
    `component.rvl`, so a publish that names a real operator file and one that
    keeps the virtual label must record the same source and manifest hashes.
    This pins that claim instead of leaving it argued in prose.
    """
    own = tmp_path / "src" / "component.rvl"
    own.parent.mkdir()
    own.write_text(EXPORTED, encoding="utf-8")

    with_origin = _publish(reg, "greeter", EXPORTED, version="1.0.0",
                           source_path=own)
    plain = _empty_registry(tmp_path / "registry-no-origin")
    without_origin = _publish(plain, "greeter", EXPORTED, version="1.0.0")

    assert with_origin["sourceSha256"] == without_origin["sourceSha256"]
    assert with_origin["manifestSha256"] == without_origin["manifestSha256"]
    assert (reg / "components" / "greeter" / "manifest.json").read_text() == \
        (plain / "components" / "greeter" / "manifest.json").read_text()
    assert registry.verify(reg) == []
    assert registry.verify(plain) == []
