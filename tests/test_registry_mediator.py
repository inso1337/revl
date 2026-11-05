"""A registry component can be marked a trusted mediator for a capability, and
a candidate that would replace it must clear a stronger evidence bar (issue
#2697, slice 2).

The marking is a `meta.json` facet, and it is CHECKED against the compiled
audit: the capability must be a named token (never `*`), the named component
must hold it through a declared `requires` service whose method declares it,
none of its provided services may re-declare it, and it must be the only
component in the entry's composition whose G8 reach carries it. `resolve` then
ranks a candidate that would displace a single marked mediator below it unless
the candidate carries a cryptographically valid attestation binding a passing
fault sweep. Several compatible markings are an ambiguity the answer reports;
no marked candidate is crowned by an authority heuristic.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import RevlError  # noqa: E402
from revl import attest, registry  # noqa: E402

KEY = b"revl-registry-2697-mediator-fixture-key"
NOW = "2026-01-01T00:00:00+00:00"

#: The mediator: the only component wired to the raw `fs` boundary, exposing a
#: bare-emission wrapper. Its G8 surface names `fs`.
MEDIATOR = """
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

#: A cheaper replacement: it provides the same wrapper and reaches no capability
#: at all, so least-authority ranking would put it first without the mediator bar.
FAST = """
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }

component FastWriter provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int { return 0 }
  }
}
"""

#: The mediator and a second component that also reaches `fs`: the marking must
#: be refused, because a mediator has to be the ONLY holder.
TWO_HOLDERS = """
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

component Shadow requires fs: FileStore {
  emit fs.write("shadow.log", "second holder")
}
"""

MEDIATOR_META = {
    "description": "the only holder of the raw fs boundary",
    "tags": ["fs", "mediator"],
    "mediator": {"component": "FsMediator", "capability": "fs"},
}

#: A mediator that holds TWO raw capabilities. It is one of two valid marked
#: candidates in the ambiguity test.
WIDE_MEDIATOR = """
service FileStore { emission[fs] fn write(path: Str, data: Str) -> Int }
service ArchiveStore { emission[archive] fn store(path: Str, data: Str) -> Int }
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }

component FsMediator requires fs: FileStore, archive: ArchiveStore provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int {
      emit fs.write(path, data)
      emit archive.store(path, data)
      return 0
    }
  }
}
"""

#: A second valid marked mediator, for a different capability.
NARROW_MEDIATOR = """
service NetStore { emission[net] fn send(data: Str) -> Int }
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }

component NetMediator requires net: NetStore provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int {
      emit net.send(data)
      return 0
    }
  }
}
"""

NARROW_META = {
    "description": "the only holder of the raw net boundary",
    "tags": ["net", "mediator"],
    "mediator": {"component": "NetMediator", "capability": "net"},
}

#: A valid marking with WIDER authority than MEDIATOR. It must not become the
#: protected incumbent by out-authority-ing the genuine mediator.
DECOY_MEDIATOR = """
service NetStore { emission[net] fn send(data: Str) -> Int }
service DbStore { emission[db] fn query(sql: Str) -> Int }
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }

component Decoy requires net: NetStore, db: DbStore provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int {
      emit net.send(data)
      emit db.query("select 1")
      return 0
    }
  }
}
"""

DECOY_META = {
    "description": "a decoy marked mediator with a wider surface",
    "tags": ["net", "mediator"],
    "mediator": {"component": "Decoy", "capability": "net"},
}

#: A component whose only capability is the unnameable `*` a bare emission
#: carries. `*` cannot be marked as a capability.
BARE_BOUNDARY = """
service Ops { emission fn go() -> Int }
extern emission fn ping() -> Int = @py { return 0 }

component Bare provides ops: Ops {
  provide ops {
    fn go() -> Int {
      emit ping()
      return 0
    }
  }
}
"""

#: A component that reaches `fs` through a host extern, but holds no declared
#: `requires` service for it: it is not a mediator.
NOT_REQUIRED = """
service Ops { emission fn go() -> Int }
extern emission[fs] fn write_raw(path: Str, data: Str) -> Int = @py { return 0 }

component Direct provides ops: Ops {
  provide ops {
    fn go() -> Int {
      emit write_raw("x", "y")
      return 0
    }
  }
}
"""

#: A wrapper that re-declares the raw token on its provided service, handing it
#: to every consumer: it is not a mediator.
RE_EXPOSING = """
service FileStore { emission[fs] fn write(path: Str, data: Str) -> Int }
service AppendWriter { emission[fs] fn append(path: Str, data: Str) -> Int }

component ReExposing requires fs: FileStore provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int {
      emit fs.write(path, data)
      return 0
    }
  }
}
"""

#: A declared `db` requirement the component never reaches: the token is a
#: valid requirement, but the composition does not carry it.
UNREACHED = """
service FileStore { emission[fs] fn write(path: Str, data: Str) -> Int }
service DbStore { emission[db] fn query(sql: Str) -> Int }
service AppendWriter { emission fn append(path: Str, data: Str) -> Int }

component FsMediator requires fs: FileStore, db: DbStore provides writer: AppendWriter {
  provide writer {
    fn append(path, data) -> Int {
      emit fs.write(path, data)
      return 0
    }
  }
}
"""

#: A need whose service is declared under a different name, so a match is
#: structural (`resolve` never matches text).
NEED = """
service Append { emission fn append(path: Str, data: Str) -> Int }
"""


def _fault_sweep(passed: int, steps: int) -> dict:
    return {
        "kind": "tested", "status": "passed" if passed == steps else "failed",
        "roadmapItem": 30, "title": "fault sweep at every step", "tier": "py",
        "counts": {"components": 1, "steps": steps, "passed": passed,
                   "failed": steps - passed, "unreachable": 0},
        "components": [], "unreachable": [],
    }


def _write_entries(reg: str, entries: dict) -> str:
    comps = os.path.join(reg, "components")
    for name, (source, meta) in entries.items():
        entry_dir = os.path.join(comps, name)
        os.makedirs(entry_dir, exist_ok=True)
        with open(os.path.join(entry_dir, "component.rvl"), "w",
                  encoding="utf-8") as fh:
            fh.write(source)
        if meta is not None:
            with open(os.path.join(entry_dir, "meta.json"), "w",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return reg


def _build(tmp_path, entries: dict) -> str:
    reg = _write_entries(os.path.join(str(tmp_path), "registry"), entries)
    registry.build_index(reg)
    return reg


def _put_evidence(reg: str, name: str, files: dict) -> None:
    ev = os.path.join(reg, "components", name, registry.EVIDENCE_DIRNAME)
    os.makedirs(ev, exist_ok=True)
    for filename, doc in files.items():
        with open(os.path.join(ev, filename), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _attestation(reg: str, name: str, bindings: dict | None = None) -> dict:
    path = os.path.join(reg, "components", name, "component.rvl")
    verdict = attest.run_gate(paths=[path],
                              normalize=registry._normalize_ir_for_attest)
    return attest.make_attestation(verdict.ir, KEY, verdict=verdict, now=NOW,
                                   signer="revl-ci",
                                   evidence_bindings=bindings or None)


def _names(result: dict) -> list:
    return [c["name"] for c in result["candidates"]]


# --------------------------------------------------------------- the marking

def test_a_verified_marking_reaches_the_index_and_verify_stays_empty(tmp_path):
    reg = _build(tmp_path, {"fs_mediator": (MEDIATOR, MEDIATOR_META)})
    row = json.loads(open(os.path.join(reg, "index.json"),
                          encoding="utf-8").read())["components"]["fs_mediator"]
    assert row["mediator"] == MEDIATOR_META["mediator"]
    assert registry.verify(reg) == []
    entry, = registry.Registry.from_dir(reg).entries
    assert entry.mediator == MEDIATOR_META["mediator"]
    assert entry.index_problems == ()


def test_a_marking_whose_capability_has_a_second_holder_is_refused(tmp_path):
    reg = _write_entries(os.path.join(str(tmp_path), "registry"),
                         {"fs_mediator": (TWO_HOLDERS, MEDIATOR_META)})
    with pytest.raises(RevlError) as refused:
        registry.build_index(reg)
    assert "sole holder" in refused.value.message
    assert "Shadow" in refused.value.message


def test_a_marking_whose_capability_nobody_reaches_is_refused(tmp_path):
    meta = dict(MEDIATOR_META,
                mediator={"component": "FsMediator", "capability": "db"})
    reg = _write_entries(os.path.join(str(tmp_path), "registry"),
                         {"fs_mediator": (UNREACHED, meta)})
    with pytest.raises(RevlError) as refused:
        registry.build_index(reg)
    assert "no component" in refused.value.message
    assert "db" in refused.value.message


def test_a_marking_for_the_unnameable_boundary_is_refused(tmp_path):
    meta = {"description": "bare wrapper", "tags": [],
            "mediator": {"component": "Bare", "capability": "*"}}
    reg = _write_entries(os.path.join(str(tmp_path), "registry"),
                         {"bare": (BARE_BOUNDARY, meta)})
    with pytest.raises(RevlError) as refused:
        registry.build_index(reg)
    assert "cannot be marked" in refused.value.message


def test_a_marking_for_a_capability_not_declared_as_a_requirement_is_refused(tmp_path):
    meta = {"description": "raw host call", "tags": [],
            "mediator": {"component": "Direct", "capability": "fs"}}
    reg = _write_entries(os.path.join(str(tmp_path), "registry"),
                         {"direct": (NOT_REQUIRED, meta)})
    with pytest.raises(RevlError) as refused:
        registry.build_index(reg)
    assert "declared requirement" in refused.value.message


def test_a_wrapper_that_redeclares_the_token_is_refused(tmp_path):
    meta = {"description": "re-exposing wrapper", "tags": [],
            "mediator": {"component": "ReExposing", "capability": "fs"}}
    reg = _write_entries(os.path.join(str(tmp_path), "registry"),
                         {"re_exposing": (RE_EXPOSING, meta)})
    with pytest.raises(RevlError) as refused:
        registry.build_index(reg)
    assert "must not hand the raw capability" in refused.value.message


def test_a_marking_naming_the_wrong_component_is_refused(tmp_path):
    meta = dict(MEDIATOR_META,
                mediator={"component": "NotTheMediator", "capability": "fs"})
    reg = _write_entries(os.path.join(str(tmp_path), "registry"),
                         {"fs_mediator": (MEDIATOR, meta)})
    with pytest.raises(RevlError) as refused:
        registry.build_index(reg)
    assert "NotTheMediator" in refused.value.message


def test_an_index_that_drops_the_marking_is_not_believed(tmp_path):
    reg = _build(tmp_path, {"fs_mediator": (MEDIATOR, MEDIATOR_META)})
    index_path = os.path.join(reg, "index.json")
    index = json.loads(open(index_path, encoding="utf-8").read())
    del index["components"]["fs_mediator"]["mediator"]
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(index, indent=2, sort_keys=True) + "\n")
    entry, = registry.Registry.from_dir(reg).entries
    assert any("mediator" in problem for problem in entry.index_problems)


# --------------------------------------------------------------- the ranking

def test_an_unsubstantiated_replacement_ranks_below_the_mediator(tmp_path):
    reg = _build(tmp_path, {
        "fs_mediator": (MEDIATOR, MEDIATOR_META),
        "fast_writer": (FAST, None),
    })
    # the replacement publishes a fault sweep but no attestation to bind it.
    _put_evidence(reg, "fast_writer",
                  {registry.EVIDENCE_FAULT_SWEEP: _fault_sweep(12, 12)})
    result = registry.resolve(reg, NEED, key=KEY)
    assert _names(result) == ["fs_mediator", "fast_writer"]
    winner, loser = result["candidates"]
    assert winner["mediator"] == MEDIATOR_META["mediator"]
    assert loser["replacementBar"]["met"] is False
    assert "trusted mediator" in loser["why"]
    assert "fs_mediator" in loser["why"]
    # the bar is stated in the answer, not left to the caller to infer.
    assert any("trusted mediator" in line and "ranks BELOW" in line
               for line in result["assumptions"])


def test_without_a_key_no_candidate_can_prove_the_bar(tmp_path):
    reg = _build(tmp_path, {
        "fs_mediator": (MEDIATOR, MEDIATOR_META),
        "fast_writer": (FAST, None),
    })
    result = registry.resolve(reg, NEED)   # no key: attestations only `present`
    assert _names(result) == ["fs_mediator", "fast_writer"]
    assert any("no signing key was supplied" in line
               for line in result["assumptions"])


def test_a_bound_attestation_and_passing_sweep_may_displace_the_mediator(tmp_path):
    reg = _build(tmp_path, {
        "fs_mediator": (MEDIATOR, MEDIATOR_META),
        "fast_writer": (FAST, None),
    })
    sweep = _fault_sweep(12, 12)
    _put_evidence(reg, "fast_writer", {registry.EVIDENCE_FAULT_SWEEP: sweep})
    _put_evidence(reg, "fast_writer", {registry.EVIDENCE_ATTESTATION: _attestation(
        reg, "fast_writer",
        bindings={"fault-sweep": registry._facet_hash(sweep)})})
    result = registry.resolve(reg, NEED, key=KEY)
    assert _names(result)[0] == "fast_writer"
    assert "replacementBar" not in result["candidates"][0]
    assert result["candidates"][0]["evidence"]["facets"]["attestation"] == "valid"
    # the mediator is still returned, still marked.
    assert result["candidates"][1]["mediator"] == MEDIATOR_META["mediator"]


def test_an_attestation_that_does_not_bind_the_sweep_does_not_clear_the_bar(tmp_path):
    reg = _build(tmp_path, {
        "fs_mediator": (MEDIATOR, MEDIATOR_META),
        "fast_writer": (FAST, None),
    })
    # a valid attestation over the component, but it binds no dossier: the
    # fault sweep beside it is still the publisher's own claim.
    _put_evidence(reg, "fast_writer",
                  {registry.EVIDENCE_FAULT_SWEEP: _fault_sweep(12, 12)})
    _put_evidence(reg, "fast_writer",
                  {registry.EVIDENCE_ATTESTATION: _attestation(reg, "fast_writer")})
    result = registry.resolve(reg, NEED, key=KEY)
    loser = result["candidates"][1]
    assert _names(result) == ["fs_mediator", "fast_writer"]
    assert "no passing fault sweep bound" in loser["replacementBar"]["reason"]


def test_two_marked_mediators_are_reported_as_ambiguous(tmp_path):
    """Several compatible marked mediators cannot be resolved to one incumbent
    without guessing, so no candidate is exempt: every one that lacks the bar
    carries replacementBar and the answer reports the ambiguity."""
    reg = _build(tmp_path, {
        "fs_mediator": (WIDE_MEDIATOR, MEDIATOR_META),
        "net_mediator": (NARROW_MEDIATOR, NARROW_META),
    })
    result = registry.resolve(reg, NEED, key=KEY)
    assert result["mediatorAmbiguity"] == ["fs_mediator", "net_mediator"]
    # least authority decides among candidates that all lack the bar.
    assert _names(result) == ["net_mediator", "fs_mediator"]
    for candidate in result["candidates"]:
        assert candidate["replacementBar"]["ambiguous"] is True
    assert any("several marked mediators" in line
               for line in result["assumptions"])


def test_a_wider_decoy_marking_does_not_become_the_incumbent(tmp_path):
    """A decoy marked mediator with more authority must not be crowned the
    protected incumbent: several markings are an ambiguity, not a ranking
    choice, so the genuine mediator is not the one barred."""
    reg = _build(tmp_path, {
        "fs_mediator": (MEDIATOR, MEDIATOR_META),
        "decoy": (DECOY_MEDIATOR, DECOY_META),
    })
    result = registry.resolve(reg, NEED, key=KEY)
    assert result["mediatorAmbiguity"] == ["decoy", "fs_mediator"]
    # the genuine, narrower mediator ranks first; both carry the bar.
    assert _names(result)[0] == "fs_mediator"
    decoy = next(c for c in result["candidates"] if c["name"] == "decoy")
    assert decoy["replacementBar"]["ambiguous"] is True
    assert decoy["replacementBar"]["mediators"] == ["decoy", "fs_mediator"]


def test_an_attested_candidate_ranks_above_the_unbarred_in_the_ambiguous_case(tmp_path):
    """The ambiguity removes the EXEMPTION, not the bar: a candidate with a
    valid attestation binding a passing sweep still ranks above the marked
    candidates and the unattested narrower candidate that lack it."""
    reg = _build(tmp_path, {
        "fs_mediator": (WIDE_MEDIATOR, MEDIATOR_META),
        "net_mediator": (NARROW_MEDIATOR, NARROW_META),
        # the narrowest, unattested candidate would win on authority alone.
        "aaa_unattested": (FAST, None),
        "zzz_evidenced": (FAST, None),
    })
    sweep = _fault_sweep(12, 12)
    _put_evidence(reg, "zzz_evidenced",
                  {registry.EVIDENCE_FAULT_SWEEP: sweep})
    _put_evidence(reg, "zzz_evidenced", {registry.EVIDENCE_ATTESTATION: _attestation(
        reg, "zzz_evidenced",
        bindings={"fault-sweep": registry._facet_hash(sweep)})})
    result = registry.resolve(reg, NEED, key=KEY)
    assert result["mediatorAmbiguity"] == ["fs_mediator", "net_mediator"]
    assert _names(result)[0] == "zzz_evidenced"
    assert "replacementBar" not in result["candidates"][0]
    unattested = next(c for c in result["candidates"]
                      if c["name"] == "aaa_unattested")
    assert unattested["replacementBar"]["ambiguous"] is True
    # every candidate without the bar is demoted, including the marked ones.
    marked = [c for c in result["candidates"] if "mediator" in c]
    assert len(marked) == 2
    assert all("replacementBar" in c for c in marked)


def test_migrate_meta_refuses_an_unverified_mediator_before_writing(tmp_path):
    """A mediator lifted out of a committed index is an authority field. The
    migration verifies it before writing anything, so a refusing rebuild cannot
    leave an unverified meta.json behind."""
    reg = _build(tmp_path, {"fast_writer": (FAST, None)})
    index_path = os.path.join(reg, "index.json")
    index = json.loads(open(index_path, encoding="utf-8").read())
    index["components"]["fast_writer"]["mediator"] = {
        "component": "FastWriter", "capability": "fs"}
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(index, indent=2, sort_keys=True) + "\n")
    with pytest.raises(RevlError) as refused:
        registry.migrate_meta(reg)
    assert "unverified mediator" in refused.value.message
    assert not os.path.exists(os.path.join(
        reg, "components", "fast_writer", "meta.json"))


# --------------------------------------------------------------- publish flow

def _publish_mediator(tmp_path, version: str, description: str) -> str:
    reg = os.path.join(str(tmp_path), "registry")
    os.makedirs(reg)
    registry.publish_release(reg, "fs_mediator", MEDIATOR, version=version,
                             description=description, tags=["fs"])
    return reg


def _mark_published_mediator(reg: str) -> str:
    entry_dir = os.path.join(reg, "components", "fs_mediator")
    meta_path = os.path.join(entry_dir, "meta.json")
    meta = json.loads(open(meta_path, encoding="utf-8").read())
    meta["mediator"] = MEDIATOR_META["mediator"]
    with open(meta_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    registry.build_index(reg)
    return meta_path


def test_publish_preserves_a_marking_across_a_description_restatement(tmp_path):
    reg = _publish_mediator(tmp_path, "1.0.0", "first")
    meta_path = _mark_published_mediator(reg)
    registry.publish_release(reg, "fs_mediator", MEDIATOR, version="1.0.1",
                             description="second", tags=["fs", "mediator"])
    meta = json.loads(open(meta_path, encoding="utf-8").read())
    assert meta["mediator"] == MEDIATOR_META["mediator"]
    assert meta["description"] == "second"
    assert registry.verify(reg) == []


def test_publish_refuses_a_source_that_no_longer_backs_the_marking(tmp_path):
    reg = _publish_mediator(tmp_path, "1.0.0", "first")
    meta_path = _mark_published_mediator(reg)
    entry_dir = os.path.join(reg, "components", "fs_mediator")
    source_path = os.path.join(entry_dir, "component.rvl")
    before = open(source_path, encoding="utf-8").read()
    with pytest.raises(RevlError) as refused:
        registry.publish_release(reg, "fs_mediator", FAST, version="1.0.1",
                                 description="dropped the raw boundary",
                                 tags=["fs"])
    assert "mediator" in refused.value.message
    # nothing was written: both the source and the declaration are unchanged.
    assert open(source_path, encoding="utf-8").read() == before
    assert json.loads(open(meta_path, encoding="utf-8").read())[
        "mediator"] == MEDIATOR_META["mediator"]
