"""The stdlib MEMORY module (issue #2696, docs/stdlib-memory.md,
stdlib/memory.rvl).

A typed memory service for a long-lived, model-driven component: `put`, `get`
and `forget` over one kv-scoped resource, generic over the item's base type.
The module adds no syntax and no checker rule; it is the existing taint,
retention and confidentiality checks applied to a stored item.

Checked here:

  * an `Untrusted[Str]` item put and read back is still untrusted: a `Trusted[T]`
    sink refuses the read value with G9, exactly as it refused it before storage;
  * a `Retained[Str, P]` item past P's deadline is refused at `put` with
    G-RETAIN, under a pinned `REVL_RETENTION_AS_OF`;
  * a not-for-prompt item typed `Secret[Str]` is refused at a model crossing
    with G-SECRET-FLOW after the round trip;
  * the negative controls that keep the three refusals non-vacuous: a clean
    round trip is admitted, a future policy is admitted, and a plain value whose
    informational `policy` field names an expired policy is admitted (the field
    is not the qualifier);
  * the py-tier reference store executes put / get / forget.

The stdlib file is copied beside each consumer, because `use` resolves the
imported path relative to the importing file (the same layout
tests/test_list_stdlib.py uses). The module's repo content is pinned by
`test_module_file_is_the_documented_surface`.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

MEMORY = ROOT / "stdlib" / "memory.rvl"

#: The crossings every consumer is judged by: a web source (untrusted), a
#: declared Secret source, a shell sink (authority), a model crossing, an
#: ordinary log, a retained source, and one retention policy.
PRELUDE = """\
use "stdlib/memory.rvl" { put, get, forget, MemoryMeta }

extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }
extern emission[web] fn fetch_s(url: Str) -> Secret[Str] = @py { return "" }
extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }
extern emission[model.complete] fn ask(p: Str) -> Str = @py { return "" }
extern emission[log] fn note(msg: Str) = @py { return }
extern pure fn load(k: Str) -> Retained[Str, pii] = @py { return k }

retention pii { until: "2020-01-01T00:00:00Z" residence: "eu" deleters: dpo }

service Ops { emission fn go(url: Str) -> Str }
"""

#: A pinned instant after `pii`'s 2020 deadline, so the verdict is reproducible
#: rather than a reading of the wall clock.
AS_OF = "2026-01-01T00:00:00Z"

#: The same consumer with no retention policy at all. It proves the untrusted
#: round trip rides the resource pairing, not the retention half of the taint
#: model.
NO_RETENTION_PRELUDE = """\
use "stdlib/memory.rvl" { put, get, forget, MemoryMeta }

extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }
extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }

service Ops { emission fn go(url: Str) -> Str }
"""


def _consumer(tmp_path, body: str, retention: str | None = None,
              prelude: str | None = None) -> Path:
    """Write a consumer whose `use "stdlib/memory.rvl"` resolves to the repo's
    module, and return the main file's path."""
    (tmp_path / "stdlib").mkdir()
    (tmp_path / "stdlib" / "memory.rvl").write_text(
        MEMORY.read_text(encoding="utf-8"), encoding="utf-8")
    text = prelude if prelude is not None else PRELUDE
    if retention is not None:
        text = text.replace(
            'retention pii { until: "2020-01-01T00:00:00Z" '
            'residence: "eu" deleters: dpo }',
            retention)
    main = tmp_path / "main.rvl"
    main.write_text(text + body, encoding="utf-8")
    return main


def _refuse(tmp_path, body: str, monkeypatch=None, retention: str | None = None,
            prelude: str | None = None):
    if monkeypatch is not None:
        monkeypatch.setenv("REVL_RETENTION_AS_OF", AS_OF)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(_consumer(tmp_path, body, retention, prelude))])
    return excinfo.value


def _admit(tmp_path, body: str, monkeypatch=None, retention: str | None = None,
           prelude: str | None = None):
    if monkeypatch is not None:
        monkeypatch.setenv("REVL_RETENTION_AS_OF", AS_OF)
    return compile_files([str(_consumer(tmp_path, body, retention, prelude))])


# ---------------------------------------------------------------- 1. untrusted

UNTRUSTED_ROUND_TRIP = """\
component A provides ops: Ops {
  provide ops {
    fn go(url) {
      let u = emit fetch(url)
      let ok = emit put("note-1", u,
                        { origin: "web", purpose: "draft", policy: "" })
      let got: Opt[Str] = emit get("note-1")
      let v = match got { Some(x) => x, None => "" }
      emit run(v)
      return ""
    }
  }
}
"""


def test_an_untrusted_item_is_refused_at_a_trusted_sink_after_the_round_trip(
        tmp_path, monkeypatch):
    """The read value is still untrusted: the shell sink refuses it with G9,
    exactly as it refuses the value before it was stored."""
    err = _refuse(tmp_path, UNTRUSTED_ROUND_TRIP, monkeypatch)
    assert err.code == "G9"
    assert "untrusted" in err.message.lower()
    assert "`run`" in err.message


def test_the_untrusted_round_trip_does_not_need_a_retention_policy(tmp_path):
    """The same refusal with no `retention` declaration anywhere: the origin is
    carried by the kv resource pairing, not by the retention half of the taint
    model."""
    err = _refuse(tmp_path, UNTRUSTED_ROUND_TRIP, prelude=NO_RETENTION_PRELUDE)
    assert err.code == "G9"
    assert "untrusted" in err.message.lower()


def test_a_clean_item_round_trips_into_the_trusted_sink(tmp_path):
    """The negative control: the refusal above is the origin, not the store.
    A value put with no origin comes back clean and is admitted."""
    _admit(tmp_path, """\
component A provides ops: Ops {
  provide ops {
    fn go(url) {
      let ok = emit put("note-1", "public",
                        { origin: "local", purpose: "draft", policy: "" })
      let got: Opt[Str] = emit get("note-1")
      let v = match got { Some(x) => x, None => "" }
      emit run(v)
      return ""
    }
  }
}
""")


# ----------------------------------------------------------------- 2. retained

RETAINED_ROUND_TRIP = """\
component A provides ops: Ops {
  provide ops {
    fn go(url) {
      let r = load(url)
      let ok = emit put("note-1", r,
                        { origin: "fs", purpose: "audit", policy: "pii" })
      return ""
    }
  }
}
"""


def test_a_retained_item_past_its_deadline_is_refused_at_put(tmp_path,
                                                              monkeypatch):
    """The retained value reaches the kv persistence sink and G-RETAIN names the
    crossing and the scope, under a pinned evaluation instant."""
    err = _refuse(tmp_path, RETAINED_ROUND_TRIP, monkeypatch)
    assert err.code == "G-RETAIN"
    assert "`put`" in err.message
    assert "`kv` crossing" in err.message
    assert "pii" in err.message


def test_a_retained_item_before_its_deadline_is_admitted(tmp_path, monkeypatch):
    """The negative control: the same value under a policy that has not expired
    is admitted, so the refusal is the deadline rather than retained-ness."""
    _admit(tmp_path, RETAINED_ROUND_TRIP, monkeypatch,
           retention='retention pii { until: "2099-01-01T00:00:00Z" '
                     'residence: "eu" deleters: dpo }')


def test_the_informational_policy_field_retains_nothing(tmp_path, monkeypatch):
    """`meta.policy` is a string, not a `Retained[T, P]` qualifier: a plain value
    naming the expired policy in the field is still admitted. The docs say the
    metadata is informational; this is that claim as a test."""
    _admit(tmp_path, """\
component A provides ops: Ops {
  provide ops {
    fn go(url) {
      let ok = emit put("note-1", "plain",
                        { origin: "web", purpose: "draft", policy: "pii" })
      let got: Opt[Str] = emit get("note-1")
      let v = match got { Some(x) => x, None => "" }
      emit run(v)
      return ""
    }
  }
}
""", monkeypatch)


# ------------------------------------------------------------ 3. secret / prompt

SECRET_ROUND_TRIP = """\
component A provides ops: Ops {
  provide ops {
    fn go(url) {
      let s = emit fetch_s(url)
      let ok = emit put("note-1", s,
                        { origin: "web", purpose: "draft", policy: "" })
      let got: Opt[Str] = emit get("note-1")
      let v = match got { Some(x) => x, None => "" }
      let a = emit ask(v)
      return a
    }
  }
}
"""


def test_a_secret_item_is_refused_at_a_model_crossing(tmp_path, monkeypatch):
    """The not-for-prompt item survives the round trip as confidential: the
    model crossing refuses it with G-SECRET-FLOW."""
    err = _refuse(tmp_path, SECRET_ROUND_TRIP, monkeypatch)
    assert err.code == "G-SECRET-FLOW"
    assert "`ask`" in err.message
    assert "confidential" in err.message.lower()


def test_the_secret_receiver_does_not_make_a_plain_write_confidential(
        tmp_path, monkeypatch):
    """`put`'s `Secret[T]` parameter is admission-only: storing a plain value
    through the same crossing does not make it confidential."""
    _admit(tmp_path, """\
component A provides ops: Ops {
  provide ops {
    fn go(url) {
      let ok = emit put("note-1", "plain",
                        { origin: "local", purpose: "draft", policy: "" })
      let got: Opt[Str] = emit get("note-1")
      let v = match got { Some(x) => x, None => "" }
      emit ask(v)
      return v
    }
  }
}
""", monkeypatch)


# --------------------------------------------------------- 4. the py reference

RUNTIME_CONSUMER = """\
fn remember(key: Str, value: Str) -> Bool {
  return put(key, value, { origin: "local", purpose: "runtime", policy: "" })
}

fn recall(key: Str) -> Opt[Str] {
  return get(key)
}

fn drop_note(key: Str) {
  forget(key)
}
"""


def _exec_python(ir: dict) -> dict:
    spec = importlib.util.spec_from_file_location(
        "pyemit_memory", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "memory.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


def test_the_py_reference_store_writes_reads_and_forgets(tmp_path):
    """The reference crossing is not a stub: put stores, get returns, a missing
    key is `None`, and forget drops the stored value."""
    ir = _admit(tmp_path, RUNTIME_CONSUMER)
    ns = _exec_python(ir)
    assert ns["recall"]("absent") is None
    assert ns["remember"]("note-1", "kept") is True
    assert ns["recall"]("note-1") == "kept"
    ns["drop_note"]("note-1")
    assert ns["recall"]("note-1") is None


# ------------------------------------------------------------------ 5. the file

def test_module_file_is_the_documented_surface():
    text = MEMORY.read_text(encoding="utf-8")
    assert "pub type MemoryMeta = {" in text
    assert "pub extern emission[kv.memory] fn put[T](key: Str, value: Secret[T]," in text
    assert "pub extern emission[kv.memory] fn get[T](key: Str) -> Opt[T]" in text
    assert "pub extern emission[kv.memory] fn forget(key: Str) -> Unit" in text


def test_write_and_read_are_siblings_of_one_kv_scope():
    """The #1983 fixpoint only pairs crossings that declare the SAME resource
    scope; pin the two declarations so a refactor cannot split them."""
    text = MEMORY.read_text(encoding="utf-8")
    assert text.count("emission[kv.memory]") >= 3
    assert 'emission[kv.memory] fn put[T]' in text
    assert 'emission[kv.memory] fn get[T]' in text


def test_the_example_compiles():
    """`examples/memory_service.rvl` is the module's shipped consumer: the
    documented `Notebook` component must stay admitted as the module changes."""
    ir = compile_files([str(ROOT / "examples" / "memory_service.rvl")])
    names = {c.get("name") for c in ir["components"]}
    assert "Notebook" in names
