"""The publish path judges authorship by a real, absolute source path, and it
validates the stored component before it writes anything (issue #2563).

The first slice (#2615, squash 59d3250c7) taught `publish_release` to compile a
source under the file it came from. It left two holes this file pins closed:

  * `source_path` stayed OPTIONAL and the only production caller, `truc ship`
    (`_host.release_facts` and `_host.publish`), never passed it, so shipping an
    entry file from `.revl/export/` still published it unprofiled. A relative
    name, or a name with no file behind it, also bypassed the rule.
  * the compile used the PUBLISHER's path, so `use`, asset and body-file
    references resolved from the publisher's working copy. `build_index`
    compiles the STORED `component.rvl` from the entry's own directory and
    cannot follow those, so it used to raise AFTER `component.rvl` was written,
    leaving a half-written registry.

`truc ship` is driven here through its Python host bodies (`revl.truc._host`),
which is where the source path was dropped; the cordis-backed CLI suites cover
the wire above it. No cordis runtime is needed.
"""

import json
from pathlib import Path

import pytest

from revl import registry
from revl.errors import RevlError
from revl.truc import _host

PROFILED = "untrusted-author profile forbids"

# The same source as the first slice's file: parses and provides a service, and
# declares a host extern the OPERATOR may declare and an exported author may
# not. Only the origin decides which profile compiles it.
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

# A module that exists beside the publisher's working copy and nowhere else:
# `use` resolves it from the importing file's directory.
LIB = 'pub fn helper() -> Str { return "h" }\n'
USES_LIB = """\
service Tool { fn describe() -> Str }
use "lib.rvl" { helper }
component Toolbox provides tool: Tool {
  provide tool { fn describe() = helper() }
}
"""


def _empty_registry(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "components").mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"indexVersion": registry.INDEX_VERSION,
                    "components": {}}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return root


@pytest.fixture()
def reg(tmp_path):
    return _empty_registry(tmp_path / "registry")


def _project(tmp_path: Path, *, entry: str, registry_path: Path,
             source: str) -> tuple[Path, Path]:
    """A truc project whose `[assembly] entry` is `entry`."""
    project = tmp_path / "project"
    entry_path = project / entry
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(source, encoding="utf-8")
    (project / "truc.toml").write_text(
        "[assembly]\n"
        'name = "app"\n'
        f'entry = ["{entry}"]\n\n'
        "[registries]\n"
        f'local = {{ path = "{registry_path}" }}\n\n'
        "[ship]\n"
        'registry = "local"\n'
        'description = "greets"\n'
        'tags = ["greet"]\n',
        encoding="utf-8")
    return project, entry_path


def _plan(registry_path: Path, source: str, *,
          source_path: str | None) -> dict:
    """The publish plan the pure Shipper hands to `host_publish`."""
    plan = {
        "publish": True,
        "registryPath": str(registry_path),
        "name": "greeter",
        "source": source,
        "description": "greets",
        "tags": ["greet"],
        "stampDossier": False,
        "dossierText": "",
        "version": "1.0.0",
        "scheme": "semver",
        "publisher": "",
    }
    if source_path is not None:
        plan["sourcePath"] = source_path
    return plan


# ------------------------------------------------- the ship caller passes a path

def test_a_ship_plan_that_omits_the_source_path_is_refused(reg):
    """The production caller used to omit `source_path` and the text compiled
    under the virtual label with no profile. A plan with no entry path must now
    fail closed: the registry is not written and nothing compiles without it.

    BEFORE the fix `publish` published the text and returned "published"; this
    fails with DID NOT RAISE.
    """
    with pytest.raises(RevlError, match="source path"):
        _host.publish(json.dumps(_plan(reg, EXPORTED, source_path=None)))

    assert not (reg / "components" / "greeter").exists()
    assert "greeter" not in (reg / "index.json").read_text(encoding="utf-8")


def test_ship_refuses_an_exported_entry_and_leaves_the_registry_untouched(
        reg, tmp_path):
    """The real `truc ship` shape: the project's entry IS the exported file.

    `ship_context` must name the entry by its resolved absolute path and
    `release_facts` must refuse, exactly as the registry's own write path does.
    BEFORE the fix both host bodies dropped the path, `release_facts` answered
    ok, and `publish` installed the exported text with the operator's trust.
    """
    project, entry = _project(tmp_path, entry=".revl/export/component.rvl",
                              registry_path=reg, source=EXPORTED)

    ctx = json.loads(_host.ship_context(str(project)))
    assert ctx.get("sourcePath") == str(entry.resolve())

    facts = json.loads(_host.release_facts(str(project), "greeter"))
    assert facts["ok"] is False
    assert PROFILED in facts["diagnostic"]

    with pytest.raises(RevlError):
        _host.publish(json.dumps(_plan(reg, EXPORTED,
                                       source_path=ctx["sourcePath"])))

    assert not (reg / "components" / "greeter").exists()
    assert "greeter" not in (reg / "index.json").read_text(encoding="utf-8")


def test_ship_refuses_an_exported_entry_to_a_registry_that_does_not_exist_yet(
        tmp_path):
    """The first publish to a registry the `truc ship` run itself creates: the
    absence of the directory is not a reason to skip the origin judgement.

    BEFORE the fix `release_facts` returned ok early, without compiling the
    source, whenever the registry path was not on disk yet.
    """
    reg = tmp_path / "new-registry"
    project, entry = _project(tmp_path, entry=".revl/export/component.rvl",
                              registry_path=reg, source=EXPORTED)

    facts = json.loads(_host.release_facts(str(project), "greeter"))
    assert facts["ok"] is False
    assert PROFILED in facts["diagnostic"]

    with pytest.raises(RevlError):
        _host.publish(json.dumps(_plan(reg, EXPORTED,
                                       source_path=str(entry.resolve()))))

    assert not reg.exists()


def test_an_absolute_symlink_outside_the_export_area_is_judged_by_its_target(
        reg, tmp_path):
    """A link is not an origin of its own: the rule follows it to the target.

    This already held at the export rule, and stays held through the new
    resolve step: the refusal must be the profile's, not a weaker one.
    """
    exported = tmp_path / ".revl" / "export" / "component.rvl"
    exported.parent.mkdir(parents=True)
    exported.write_text(EXPORTED, encoding="utf-8")
    alias = tmp_path / "alias" / "component.rvl"
    alias.parent.mkdir()
    alias.symlink_to(exported)

    with pytest.raises(RevlError) as caught:
        registry.publish_release(reg, "greeter", EXPORTED, version="1.0.0",
                                 source_path=alias)

    assert PROFILED in str(caught.value)
    assert not (reg / "components" / "greeter").exists()


def test_a_relative_source_path_that_names_no_file_is_refused(reg):
    """A relative name is resolved from the cwd before it is judged, so one
    that is relative to something else names no file and must fail closed.

    BEFORE the fix the raw string was handed to the compiler, `exported` found
    no file behind it, the text compiled unprofiled and the publish succeeded.
    """
    with pytest.raises(RevlError) as caught:
        registry.publish_release(reg, "greeter", EXPORTED, version="1.0.0",
                                 source_path=Path("src") / "component.rvl")

    assert "does not name a file" in str(caught.value)
    assert not (reg / "components" / "greeter").exists()


# ------------------------------------ the stored component must build the index

def test_a_source_the_stored_component_cannot_reproduce_is_refused(reg, tmp_path):
    """`use` resolves from the importing file's directory. Compiled from the
    publisher's working copy this source is fine; compiled from the registry's
    own `components/<name>/component.rvl` it is not, so the index could never be
    built from what was stored.

    BEFORE the fix `release_facts` answered with no refusals (the working-copy
    compile passed) and only `build_index`, after the write, raised.
    """
    working = tmp_path / "working"
    working.mkdir()
    (working / "component.rvl").write_text(USES_LIB, encoding="utf-8")
    (working / "lib.rvl").write_text(LIB, encoding="utf-8")

    facts = registry.release_facts(reg, "toolbox", USES_LIB, "1.0.0",
                                   source_path=working / "component.rvl")

    assert facts["refusals"]
    assert any("stored component.rvl" in refusal for refusal in facts["refusals"])


def test_the_component_is_not_written_before_the_index_can_be_built(reg, tmp_path):
    """The half-write ordering: `publish_release` writes `component.rvl` and
    then regenerates the index, so an index failure after the write used to
    leave an entry nothing can verify.

    BEFORE the fix the publish raised from `build_index` too, but only after
    `components/toolbox/component.rvl` existed and `index.json` claimed it. The
    write must not precede the validation that the index can be built.
    """
    before = (reg / "index.json").read_text(encoding="utf-8")
    working = tmp_path / "working"
    working.mkdir()
    (working / "component.rvl").write_text(USES_LIB, encoding="utf-8")
    (working / "lib.rvl").write_text(LIB, encoding="utf-8")

    with pytest.raises(RevlError):
        registry.publish_release(reg, "toolbox", USES_LIB, version="1.0.0",
                                 source_path=working / "component.rvl")

    assert not (reg / "components" / "toolbox").exists()
    assert (reg / "index.json").read_text(encoding="utf-8") == before
    assert registry.verify(reg) == []


def test_a_broken_other_entry_does_not_leave_this_publish_half_written(
        reg, tmp_path):
    """The whole index must be buildable before this publish writes, not just
    this entry: a registry with one broken component would otherwise fail in
    `build_index` after the new `component.rvl` had landed.

    BEFORE the fix the new entry was written and the rebuild raised on the
    older one, so the publish failed but its own bytes stayed behind.
    """
    working = tmp_path / "working"
    working.mkdir()
    entry = working / "component.rvl"
    entry.write_text(EXPORTED, encoding="utf-8")

    registry.publish_release(reg, "good", EXPORTED, version="1.0.0",
                             source_path=entry)
    (reg / "components" / "good" / "component.rvl").write_text(
        "component Broken {\n", encoding="utf-8")
    before = (reg / "index.json").read_text(encoding="utf-8")

    with pytest.raises(RevlError, match="index cannot be regenerated"):
        registry.publish_release(reg, "second", EXPORTED, version="1.0.0",
                                 source_path=entry)

    assert not (reg / "components" / "second").exists()
    assert (reg / "index.json").read_text(encoding="utf-8") == before


# ------------------------------- publishing OVER a broken target repairs it

def test_publishing_over_a_target_with_a_broken_meta_json_repairs_it(
        reg, tmp_path):
    """The entry being replaced is not evidence about the index this publish
    will build: its `component.rvl` and `meta.json` are exactly what the write
    overwrites, and publishing over an entry is how an operator repairs one
    whose stored files have gone bad.

    BEFORE the fix the dry run compiled every entry as it lay on disk, so the
    target's malformed `meta.json` raised `_entry_meta` and the publish refused
    with "the index cannot be regenerated" before writing anything. The broken
    entry could not be repaired by publishing over it.
    """
    working = tmp_path / "working"
    working.mkdir()
    entry = working / "component.rvl"
    entry.write_text(EXPORTED, encoding="utf-8")

    registry.publish_release(reg, "greeter", EXPORTED, version="1.0.0",
                             source_path=entry, description="greets",
                             tags=["greet"])
    meta = reg / "components" / "greeter" / "meta.json"
    meta.write_text("{ this is not a meta document\n", encoding="utf-8")
    with pytest.raises(RevlError):
        registry.build_index(reg, write=False)   # the target is broken

    record = registry.publish_release(
        reg, "greeter", EXPORTED, version="2.0.0", source_path=entry,
        description="greets, again", tags=["greet"])

    assert record["version"] == "2.0.0"
    row = json.loads((reg / "index.json").read_text(
        encoding="utf-8"))["components"]["greeter"]
    assert row["version"] == "2.0.0"
    assert row["description"] == "greets, again"
    assert registry.verify(reg) == []


def test_publishing_over_a_target_that_no_longer_compiles_repairs_it(
        reg, tmp_path):
    """The same for the target's stored `component.rvl`: the bytes the dry run
    compiled are replaced by this publish, so they cannot be a reason to refuse
    it. The bump check cannot be taken against a source that does not compile,
    so this publishes under `opaque`, which records that instead of refusing.

    BEFORE the fix the dry run refused with "the index cannot be regenerated"
    on the target's own broken source, before any write.
    """
    working = tmp_path / "working"
    working.mkdir()
    entry = working / "component.rvl"
    entry.write_text(EXPORTED, encoding="utf-8")

    registry.publish_release(reg, "greeter", EXPORTED, version="1.0.0",
                             scheme="opaque", source_path=entry)
    stored = reg / "components" / "greeter" / "component.rvl"
    stored.write_text("component Broken {\n", encoding="utf-8")
    with pytest.raises(RevlError):
        registry.build_index(reg, write=False)   # the target is broken

    record = registry.publish_release(reg, "greeter", EXPORTED, version="2.0.0",
                                      scheme="opaque", source_path=entry)

    assert record["version"] == "2.0.0"
    assert stored.read_text(encoding="utf-8") == EXPORTED
    row = json.loads((reg / "index.json").read_text(
        encoding="utf-8"))["components"]["greeter"]
    assert row["version"] == "2.0.0"
    assert registry.verify(reg) == []
