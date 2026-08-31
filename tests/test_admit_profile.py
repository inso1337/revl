"""The untrusted-author admission profile — roadmap item 329.

Pure-compile proofs (no runtime, no cordis): the gate refuses new host code and
a reach outside the granted allowlist, at ADMIT time, as a compile refusal.

The three proofs the item names:
  * extern-smuggling is REFUSED at admit under the no-extern profile;
  * a source that only composes granted services ADMITS;
  * a source reaching a non-granted service is REFUSED.
"""

import pytest

from revl import AdmissionProfile, compile_source
from revl.errors import RevlError

# A running composition that provides two granted tools, `kv` and `fs`. Compiled
# once, its IR document is the ambient manifest the per-turn sources admit
# against — no Session (and so no cordis) is needed to prove the gate.
_BASE = """
service Kv { fn get(k: Str) -> Str }
service FsSvc { fn read(p: Str) -> Str }

component KvProvider provides kv: Kv {
  provide kv { fn get(k) = "v" }
}
component FsProvider provides fs: FsSvc {
  provide fs { fn read(p) = "data" }
}
"""

# The turn composes only the granted `kv` tool: it requires `kv` (ambient) and
# reaches nothing else.
_TURN_COMPOSES_KV = """
service Turn { fn run() -> Str }
component TurnComp requires kv: Kv provides turn: Turn {
  provide turn { fn run() = kv.get("x") }
}
"""

# The turn reaches `fs` — a service that exists in the running composition but is
# NOT in the granted set the profile allows.
_TURN_REACHES_FS = """
service Turn { fn run() -> Str }
component TurnComp requires fs: FsSvc provides turn: Turn {
  provide turn { fn run() = fs.read("x") }
}
"""

# The turn declares its own host code — the G8 escape hatch the profile forbids.
_TURN_SMUGGLES_EXTERN = """
service Turn { fn run() -> Str }
extern pure fn exfil(t: Str) -> Str
  = @py { import os; return os.environ.get("HOME", "") }
component TurnComp provides turn: Turn {
  provide turn { fn run() = exfil("x") }
}
"""


def _base_ir():
    return compile_source(_BASE, "base.rvl")


def _admit(turn_source: str, granted, base=None):
    """Admit a per-turn source against the running composition under the
    untrusted-author profile."""
    return compile_source(
        turn_source, "<turn>.rvl",
        manifest=base if base is not None else _base_ir(),
        profile=AdmissionProfile.untrusted_author(granted))


# --------------------------------------------------------------------------- #
# (a) no-extern — smuggled host code is REFUSED at admit.
# --------------------------------------------------------------------------- #

def test_extern_smuggling_refused_at_admit():
    with pytest.raises(RevlError) as exc:
        _admit(_TURN_SMUGGLES_EXTERN, granted={"Kv", "FsSvc"})
    msg = str(exc.value)
    assert "forbids new" in msg and "extern" in msg
    assert "exfil" in msg
    # G8 is the boundary this closes; the code makes it grep-able in a dossier.
    assert getattr(exc.value, "code", None) == "G8"


def test_extern_smuggling_admits_without_the_profile():
    # The SAME source is a perfectly good composition for a TRUSTED author — the
    # refusal is a property of the profile, not the code. (Standalone compile.)
    doc = compile_source(_TURN_SMUGGLES_EXTERN, "<turn>.rvl")
    assert any(e["name"] == "exfil" for e in doc.get("externs") or [])


def test_no_extern_refused_standalone_too():
    # The no-extern half holds even with no running composition to admit against
    # (the standalone compile path), so a candidate is refused the same way an
    # agent checks it before there is anything to admit into.
    with pytest.raises(RevlError) as exc:
        compile_source(_TURN_SMUGGLES_EXTERN, "<turn>.rvl",
                       profile=AdmissionProfile(no_extern=True))
    assert "forbids new" in str(exc.value)
    assert getattr(exc.value, "code", None) == "G8"


# --------------------------------------------------------------------------- #
# (b) granted allowlist — compose-only ADMITS, non-granted reach REFUSES.
# --------------------------------------------------------------------------- #

def test_composing_a_granted_service_admits():
    doc = _admit(_TURN_COMPOSES_KV, granted={"Kv"})
    # the turn's own component is admitted; it reaches only the granted `Kv`.
    names = {c["name"] for c in doc["components"]}
    assert "TurnComp" in names


def test_reaching_a_non_granted_service_refused():
    with pytest.raises(RevlError) as exc:
        _admit(_TURN_REACHES_FS, granted={"Kv"})
    msg = str(exc.value)
    assert "not in the granted set" in msg
    assert "FsSvc" in msg
    assert getattr(exc.value, "code", None) == "R2"


def test_empty_granted_set_refuses_any_reach():
    with pytest.raises(RevlError) as exc:
        _admit(_TURN_COMPOSES_KV, granted=set())
    assert "not in the granted set" in str(exc.value)


def test_allowlist_off_permits_any_reach():
    # granted=None turns the allowlist OFF: no_extern alone does not bound reach.
    doc = compile_source(
        _TURN_REACHES_FS, "<turn>.rvl", manifest=_base_ir(),
        profile=AdmissionProfile(no_extern=True, granted=None))
    assert {c["name"] for c in doc["components"]} == {"TurnComp"}


def test_service_provided_internally_is_not_a_reach():
    # A turn that provides AND consumes its own service reaches nothing external,
    # so it admits under an empty granted set.
    src = """
    service Inner { fn v() -> Str }
    service Turn { fn run() -> Str }
    component InnerProv provides inner: Inner {
      provide inner { fn v() = "x" }
    }
    component TurnComp requires inner: Inner provides turn: Turn {
      provide turn { fn run() = inner.v() }
    }
    """
    doc = compile_source(src, "<turn>.rvl", manifest=_base_ir(),
                         profile=AdmissionProfile.untrusted_author(set()))
    assert {c["name"] for c in doc["components"]} == {"InnerProv", "TurnComp"}


# --------------------------------------------------------------------------- #
# The profile is inert unless asked for — a trusted-author compile is unchanged.
# --------------------------------------------------------------------------- #

def test_inert_profile_is_a_noop():
    a = compile_source(_TURN_COMPOSES_KV, "<turn>.rvl", manifest=_base_ir())
    b = compile_source(_TURN_COMPOSES_KV, "<turn>.rvl", manifest=_base_ir(),
                       profile=AdmissionProfile())
    assert a == b


# --------------------------------------------------------------------------- #
# (a2) no-extern, import/reach bypass — item 330. `check_no_extern` refuses a
# root that DECLARES an extern, but a root that IMPORTS a pre-granted module's
# host extern and CALLS it reaches the SAME verbatim host code by another door.
# An untrusted author composes granted SERVICES; it may not reach a host extern.
# --------------------------------------------------------------------------- #

# A pre-granted tool module that ships a host-body emission extern, exactly the
# stdlib shape (`stdlib/shell.rvl`'s `pub extern emission fn sh`). Supplied
# in-memory so the proof does not couple to the real stdlib's contents.
_SHELL_TOOL = """
pub extern emission fn sh(cmd: Str) -> Str
  = @py { import os; os.system(cmd); return "" }
"""

# The untrusted turn imports that host extern and reaches it directly — the
# reviewer-reproduced bypass. It DECLARES no extern, so `check_no_extern` is
# silent; the reach of the IMPORTED host extern is what must be refused.
_TURN_IMPORTS_AND_REACHES_SH = """
use "shelltool.rvl" { sh }
service Pwn { emission fn go() -> Str }
component Pwned provides pwn: Pwn {
  provide pwn { fn go() = emit sh("id > /tmp/PWNED_BY_UNTRUSTED_TURN") }
}
"""


def test_importing_a_host_extern_and_reaching_it_is_refused():
    with pytest.raises(RevlError) as exc:
        compile_source(_TURN_IMPORTS_AND_REACHES_SH, "turn.rvl",
                       manifest=_base_ir(), modules={"shelltool.rvl": _SHELL_TOOL},
                       profile=AdmissionProfile.untrusted_author(set()))
    msg = str(exc.value)
    assert "reaching host code" in msg
    assert "sh" in msg
    assert getattr(exc.value, "code", None) == "G8"


def test_reaching_an_imported_host_extern_via_a_root_helper_is_refused():
    # The reach may hop through a root-local helper fn; the call site still lives
    # in a root body, so the bare-name sweep of the root fns catches it.
    src = """
    use "shelltool.rvl" { sh }
    fn reach_it() -> Str { return sh("id") }
    service Pwn { emission fn go() -> Str }
    component Pwned provides pwn: Pwn { provide pwn { fn go() = emit reach_it() } }
    """
    with pytest.raises(RevlError) as exc:
        compile_source(src, "turn.rvl", manifest=_base_ir(),
                       modules={"shelltool.rvl": _SHELL_TOOL},
                       profile=AdmissionProfile.untrusted_author(set()))
    assert getattr(exc.value, "code", None) == "G8"
    assert "sh" in str(exc.value)


def test_trusted_composition_importing_a_host_extern_still_admits():
    # The SAME source is normal usage for a TRUSTED author — the refusal is a
    # property of the profile, not the composition. No profile => admits.
    doc = compile_source(_TURN_IMPORTS_AND_REACHES_SH, "turn.rvl",
                         manifest=_base_ir(),
                         modules={"shelltool.rvl": _SHELL_TOOL})
    assert {c["name"] for c in doc["components"]} == {"Pwned"}
    assert any(e["name"] == "sh" for e in doc.get("externs") or [])


def test_untrusted_importing_a_pure_fn_still_admits():
    # Additivity: an untrusted turn may still import and call a plain `pub fn`
    # from a module — only an imported HOST EXTERN is a reach. The tool module
    # here exposes a pure helper, no host body, so the turn admits.
    tool = "pub fn greet(n: Str) -> Str { return \"hi\" }\n"
    src = """
    use "helper.rvl" { greet }
    service Turn { fn run() -> Str }
    component TurnComp provides turn: Turn { provide turn { fn run() = greet("x") } }
    """
    doc = compile_source(src, "turn.rvl", manifest=_base_ir(),
                         modules={"helper.rvl": tool},
                         profile=AdmissionProfile.untrusted_author(set()))
    assert {c["name"] for c in doc["components"]} == {"TurnComp"}


# --------------------------------------------------------------------------- #
# (a3) no-extern, TRANSITIVE import/reach bypass: items 330 + 329/transitive.
# The 330 cut walked only the ROOT's directly-imported extern surface and swept
# only the ROOT bodies. But `compile_files` MERGES and LOWERS the entire
# transitive module closure into one program, so an untrusted turn reaches
# arbitrary host code through a `pub fn` wrapper in a NON-root module: the wrapper
# is bare-callable from a root body and its own body reaches the host extern.
# The reach is real (the merged doc lowers and carries the host body) but the
# host extern is DECLARED in a non-root module the root never `use`s directly.
# The reach sweep must follow the transitive call graph across the merged closure.
# --------------------------------------------------------------------------- #


def test_transitive_reach_through_a_nonroot_pub_fn_wrapper_is_refused():
    # One hop: root -> `wrap` (a `pub fn` in a NON-root module) -> `sh` (host
    # extern in a third module). The root declares no extern and imports no extern
    # (only the pure-looking `wrap`), so the root-only 330 sweep saw nothing, yet
    # the merged program lowers `sh` and `wrap` calls it. Must be REFUSED.
    root = """
    use "evil.rvl" { wrap }
    service Pwn { emission fn go() -> Str }
    component P provides pwn: Pwn {
      provide pwn { fn go() = emit wrap("id > /tmp/PWNED") }
    }
    """
    evil = 'use "shelltool.rvl" { sh }\npub fn wrap(c: Str) -> Str { return sh(c) }\n'
    with pytest.raises(RevlError) as exc:
        compile_source(root, "turn.rvl", manifest=_base_ir(),
                       modules={"evil.rvl": evil, "shelltool.rvl": _SHELL_TOOL},
                       profile=AdmissionProfile.untrusted_author(set()))
    assert getattr(exc.value, "code", None) == "G8"
    msg = str(exc.value)
    assert "reaching host code" in msg
    assert "sh" in msg


def test_two_hop_nonroot_chain_to_a_host_extern_is_refused():
    # Two hops through NON-root modules: root -> `relay` -> `sh`. Neither the
    # relay module nor the host module is imported by the root directly; the reach
    # sweep must follow the whole call graph across the merged closure.
    root = """
    use "a.rvl" { relay }
    service Pwn { emission fn go() -> Str }
    component P provides pwn: Pwn { provide pwn { fn go() = emit relay("id") } }
    """
    a = 'use "b.rvl" { sh }\npub fn relay(c: Str) -> Str { return sh(c) }\n'
    with pytest.raises(RevlError) as exc:
        compile_source(root, "turn.rvl", manifest=_base_ir(),
                       modules={"a.rvl": a, "b.rvl": _SHELL_TOOL},
                       profile=AdmissionProfile.untrusted_author(set()))
    assert getattr(exc.value, "code", None) == "G8"
    assert "sh" in str(exc.value)


def test_pure_py_host_body_reached_transitively_is_refused():
    # A `pure` @py body (os.popen) is host code just the same: the profile
    # refuses ANY host-body reach, across all classifications, exactly
    # `check_no_extern`'s stance. Reached one hop through a non-root wrapper.
    root = """
    use "evil.rvl" { wrap }
    service Turn { fn go() -> Str }
    component P provides turn: Turn { provide turn { fn go() = wrap("id") } }
    """
    evil = 'use "tool.rvl" { sh }\npub fn wrap(c: Str) -> Str { return sh(c) }\n'
    tool = ('pub extern pure fn sh(cmd: Str) -> Str\n'
            '  = @py { import os; return os.popen(cmd).read() }\n')
    with pytest.raises(RevlError) as exc:
        compile_source(root, "turn.rvl", manifest=_base_ir(),
                       modules={"evil.rvl": evil, "tool.rvl": tool},
                       profile=AdmissionProfile.untrusted_author(set()))
    assert getattr(exc.value, "code", None) == "G8"
    assert "sh" in str(exc.value)


def test_real_stdlib_transitive_pure_host_reach_is_refused():
    # A real-stdlib case: `value_is_object` is a `pub fn` that calls the host-body
    # `pub extern pure fn value_kind`. An untrusted turn that imports and reaches
    # `value_is_object` transitively reaches `value_kind`'s @py body. It is a
    # BENIGN pure body today, but the profile refuses ALL reachable host-body
    # externs (the all-classifications stance `check_no_extern` set): the reviewer
    # showed the SAME transitive path reaches shell the instant any stdlib `pub fn`
    # ever wraps an effectful extern, so "refuse every host-body reach" is the only
    # safe rule. Refusing here, not carving a pure-stdlib exception, keeps the
    # profile sound against that future without re-auditing the whole stdlib.
    src = """
    use "stdlib/value.rvl" { value_is_object }
    service Turn { fn run(v: Value) -> Bool }
    component TurnComp provides turn: Turn {
      provide turn { fn run(v) = value_is_object(v) }
    }
    """
    with pytest.raises(RevlError) as exc:
        compile_source(src, "turn.rvl", manifest=_base_ir(),
                       profile=AdmissionProfile.untrusted_author(set()))
    assert getattr(exc.value, "code", None) == "G8"
    msg = str(exc.value)
    assert "value_kind" in msg
    assert "reaching host code" in msg


def test_transitive_reach_admits_without_the_profile():
    # The SAME transitive composition is normal usage for a TRUSTED author: the
    # refusal is a property of the profile, not the code. No profile => admits,
    # and the merged doc carries the host extern as before.
    root = """
    use "evil.rvl" { wrap }
    service Pwn { emission fn go() -> Str }
    component P provides pwn: Pwn { provide pwn { fn go() = emit wrap("id") } }
    """
    evil = 'use "shelltool.rvl" { sh }\npub fn wrap(c: Str) -> Str { return sh(c) }\n'
    doc = compile_source(root, "turn.rvl", manifest=_base_ir(),
                         modules={"evil.rvl": evil, "shelltool.rvl": _SHELL_TOOL})
    assert {c["name"] for c in doc["components"]} == {"P"}
    assert any(e["name"] == "sh" for e in doc.get("externs") or [])


def test_unreached_nonroot_host_extern_still_admits():
    # Precision, not over-approximation: an imported module MAY declare a host
    # extern the turn never actually reaches. The root imports only the pure `calm`
    # wrapper; the module's OTHER `pub fn` `danger` (which reaches `sh`) is never
    # called from any reachable path, so the precise call-graph sweep does not
    # false-refuse. A closure over-approximation would wrongly refuse this.
    root = """
    use "mixed.rvl" { calm }
    service Turn { fn run() -> Str }
    component TurnComp provides turn: Turn { provide turn { fn run() = calm("x") } }
    """
    mixed = (
        'use "shelltool.rvl" { sh }\n'
        'pub fn calm(x: Str) -> Str { return x }\n'
        'pub fn danger(c: Str) -> Str { return sh(c) }\n'
    )
    doc = compile_source(root, "turn.rvl", manifest=_base_ir(),
                         modules={"mixed.rvl": mixed, "shelltool.rvl": _SHELL_TOOL},
                         profile=AdmissionProfile.untrusted_author(set()))
    assert {c["name"] for c in doc["components"]} == {"TurnComp"}


def test_real_stdlib_shell_import_and_emit_is_refused():
    # Fidelity to the reviewer's exact probe: the REAL stdlib `sh` extern,
    # resolved off the item-319 search path, imported and emitted by an
    # empty-grant untrusted turn — refused at admission before any host body runs.
    src = (
        'use "stdlib/shell.rvl" { sh }\n'
        "service Pwn { emission fn go() -> Str }\n"
        "component Pwned provides pwn: Pwn {\n"
        '  provide pwn { fn go() = emit sh("id > /tmp/PWNED_BY_UNTRUSTED_TURN") }\n'
        "}\n"
    )
    with pytest.raises(RevlError) as exc:
        compile_source(src, "turn.rvl", manifest=_base_ir(),
                       profile=AdmissionProfile.untrusted_author(set()))
    assert getattr(exc.value, "code", None) == "G8"
    assert "sh" in str(exc.value)
