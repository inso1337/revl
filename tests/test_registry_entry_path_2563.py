"""A published entry's read-back names the entry's REAL `component.rvl`.

Issue #2563, read-back slice. The export-area trust rule
(`export_area.CliRule`) decides authorship from the name a compile is given:
a real file under `.revl/export/` is author-written, and a label that answers
no file is not looked at at all. Three read-back sites still handed the
compiler a label while the file was right there:

  * `revl policy evaluate --registry` compiled the entry text under
    `"component.rvl"` (twice: the audit IR and the attestation IR), and handed
    the cold gauntlet `{"source": ...}`;
  * `registry._compiled_entry_ir` (the adapter probe) compiled it under
    `"component.rvl"`;
  * `truc`'s `_knowledge_signed` compiled the vendored registry entry under
    `"component.rvl"` after reading it from `src_dir / "component.rvl"`.

So an entry whose source sits in an export directory evaluated, probed and
signed as the operator's own: `policy evaluate --registry` printed
`clean` and exited 0 for text `from_dir` had just refused to verify under the
authoring profile. Each site now names the real file it read.

The in-memory case stays as slice 1 left it: `_compile_source` with no path
still compiles under the virtual label
(`/__revl_registry__/<name>/component.rvl`), because the inversion for text
from no file is the central rule's slice, not this one. Every production
caller here holds a file.

Every "exported" case below moves the registry INTO a `.revl/export/`
directory. The same bytes outside it are the control, so what the tests
measure is the PATH the rule reads, not the content: an outside entry still
evaluates clean, still probes, and still verifies its signature.

No cordis runtime: these are the frontend compiler and the pure `truc` host
bodies.
"""

import inspect
import json
import os

from revl import registry, registry_evidence
from revl.__main__ import main
from revl.truc import _host

# compiles under the operator's trust and is REFUSED under the authoring
# profile (an untrusted author may not declare host code)
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

POLICY = "component * may reach db\n"

KEY = "publisher-secret"

TRAP = {"op": "note", "id": "k_0123456789ab", "kind": "trap",
        "anchor": {"path": "component.rvl", "symbol": "Greeter",
                   "fingerprint": "sha256:the-publishers"},
        "body": "hello does not trim the name", "evidence": [],
        "author": {"kind": "agent", "trust": "untrusted"},
        "created": "2026-10-03T00:00:00+00:00", "seq": 1}


def _empty_registry(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "components").mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"indexVersion": registry.INDEX_VERSION,
                    "components": {}}, indent=2) + "\n", encoding="utf-8")
    return root


def _published(tmp_path, *, in_export: bool, knowledge=None):
    """A registry holding one published entry, moved into the export area when
    `in_export`. The bytes are the same either way; only the path differs."""
    source_file = tmp_path / "source.rvl"
    source_file.write_text(EXPORTED, encoding="utf-8")
    reg = _empty_registry(tmp_path / "registry")
    registry.publish_release(reg, "greeter", EXPORTED,
                             source_path=source_file, version="1.0.0",
                             description="greets", knowledge=knowledge)
    if not in_export:
        return reg
    return _move_to_export(tmp_path, reg)


def _move_to_export(tmp_path, reg):
    exported = tmp_path / ".revl" / "export" / "registry"
    exported.parent.mkdir(parents=True, exist_ok=True)
    reg.rename(exported)
    return exported


def _entry(reg):
    return registry.Registry.from_dir(reg).entries[0]


def _component_at(tmp_path, *parts, text=EXPORTED):
    """`text` as a component file under `tmp_path`, optionally in the export
    area."""
    path = tmp_path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _evaluate(tmp_path, reg) -> int:
    policy = tmp_path / "boundary.policy"
    policy.write_text(POLICY, encoding="utf-8")
    return main(["policy", "evaluate", str(policy), "--registry", str(reg),
                 "--candidate", "greeter"])


# ------------------------------------------- policy evaluate --registry

def test_policy_evaluate_profiles_an_exported_entry(tmp_path, capsys):
    """BEFORE the fix the audit IR came from `compile_source(entry.source,
    "component.rvl")`: the label answered no file, the rule saw nothing and
    the entry evaluated clean with exit 0 even though `from_dir` had already
    refused to verify it under the authoring profile. AFTER it compiles under
    the entry's real path, the profile refuses the extern and the dry run
    exits 2 rather than reporting a clean gate."""
    reg = _published(tmp_path, in_export=True)
    assert _evaluate(tmp_path, reg) == 2
    assert PROFILED in capsys.readouterr().err


def test_policy_evaluate_compiles_the_same_entry_outside_the_export_area(
        tmp_path, capsys):
    """The control: the same bytes at a path outside any export directory are
    the operator's own and evaluate clean, so the path decides."""
    reg = _published(tmp_path, in_export=False)
    assert _evaluate(tmp_path, reg) == 0
    assert "clean" in capsys.readouterr().out


# ------------------------------------------------- the adapter probe

def test_the_adapter_probe_profiles_an_exported_entry(tmp_path):
    """`_compiled_entry_ir` compiled `entry.source` under `"component.rvl"`
    for the adapter probe. AFTER the fix the real path is profiled and the
    refusal is cached as `{}` - nothing is proposed against text the gate
    would refuse."""
    entry = _entry(_published(tmp_path, in_export=True))
    assert registry._compiled_entry_ir(entry, {}) == {}


def test_the_adapter_probe_still_compiles_an_entry_outside_the_export_area(
        tmp_path):
    entry = _entry(_published(tmp_path, in_export=False))
    assert registry._compiled_entry_ir(entry, {})


# --------------------------------------------- the cold ship gauntlet

def _ship_gauntlet(source, path):
    """`host_gauntlet` as `truc ship` calls it, handing the entry's path when
    this build takes one (before the fix there was none to hand)."""
    if "source_path" in inspect.signature(_host.gauntlet_evidence).parameters:
        return json.loads(_host.gauntlet_evidence(source, str(path)))
    return json.loads(_host.gauntlet_evidence(source))


def test_truc_ship_gauntlet_profiles_an_exported_entry(tmp_path):
    """`truc ship` re-runs the proving ground on the entry. BEFORE the fix the
    shipper passed only the text (`{"source": ...}`), so the gauntlet compiled
    it under `<candidate>.rvl` and graded an exported entry admissible with
    the operator's trust. AFTER it compiles the entry by its real path under
    the authoring profile, which refuses the extern: the dossier is
    `rejected` and the candidate is never booted."""
    exported = _component_at(tmp_path, ".revl", "export", "component.rvl")
    dossier = _ship_gauntlet(EXPORTED, exported)
    assert dossier["verdict"] == "rejected", dossier
    assert dossier["admission"] == "refused", dossier


def test_truc_ship_gauntlet_still_grades_an_entry_outside_the_export_area(
        tmp_path):
    """The control: the same bytes from a plain project directory are the
    operator's own and grade admissible."""
    own = _component_at(tmp_path, "src", "component.rvl")
    dossier = _ship_gauntlet(EXPORTED, own)
    assert dossier["verdict"] == "admissible", dossier


def test_the_entry_path_is_absolute_so_a_chdir_cannot_hide_it(
        tmp_path, monkeypatch):
    """`from_dir` may be given a relative path (`--registry registry`). The
    entry's stored path must be absolute: a read-back can happen after the
    process changes directory, and a relative name would then answer no file
    (or a different one), so the rule would let exported text compile with the
    operator's trust."""
    reg = _move_to_export(tmp_path, _published(tmp_path, in_export=False))
    monkeypatch.chdir(tmp_path)
    loaded = registry.Registry.from_dir(reg.relative_to(tmp_path))
    entry = loaded.entries[0]
    assert os.path.isabs(entry.source_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert registry._compiled_entry_ir(entry, {}) == {}


# ------------------------------------------- the vendored knowledge signature

def _signed_registry(tmp_path, build_evidence):
    """A registry with one published entry carrying a signed knowledge record."""
    reg = _published(tmp_path, in_export=False, knowledge=[TRAP])
    build_evidence(reg, key=KEY.encode(), signer="publisher")
    return reg


def test_a_signed_entry_in_the_export_area_is_not_counted_signed(
        tmp_path, monkeypatch):
    """`truc` measured an entry's signature with `compile_source(source,
    "component.rvl")` after reading that very file. BEFORE the fix the label
    hid the export path, the entry compiled as the operator's and its
    knowledge counted as signed. AFTER the real path is profiled, the extern
    refusal makes the answer False: an exported release cannot carry operator
    signature trust (fail closed)."""
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY)
    # sign it outside the export area first: `build_evidence` compiles the
    # entry by its real path too, so it would refuse an exported one
    reg = _move_to_export(
        tmp_path,
        _signed_registry(tmp_path, registry_evidence.build_evidence))
    entry_dir = reg / "components" / "greeter"
    digest = registry._facet_hash(registry.load_knowledge(entry_dir))
    assert _host._knowledge_signed(entry_dir, digest) is False


def test_a_signed_entry_outside_the_export_area_is_still_signed(
        tmp_path, monkeypatch):
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY)
    reg = _signed_registry(tmp_path, registry_evidence.build_evidence)
    entry_dir = reg / "components" / "greeter"
    digest = registry._facet_hash(registry.load_knowledge(entry_dir))
    assert _host._knowledge_signed(entry_dir, digest) is True
