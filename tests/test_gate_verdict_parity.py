"""Cross-tier verdict-shape parity — roadmap item 332, the "Verdict shape
agreement" exit test (docs/design/332-embeddable-gate-api.md).

The design fixes ONE structured verdict shape so a host reading a gate through
its wire string cannot tell a py gate from a rust gate on the arm both tiers
share (`refused`). The rust crate's half of that contract is already pinned by
`test_gate_crate_admit.py` (it builds a standalone consumer and reads the
crate's `Verdict::to_json`). This module pins the PY half and the two paths that
have to converge on it:

1. `revl.gate._json_string` mirrors the crate's `json_string`
   (`crates/revl-gate/src/lib.rs`) byte for byte, so an identical `(code,
   message)` serialises identically on both tiers.
2. `Verdict.to_json` lays the fields out in the fixed order the crate uses.
3. The two py CONSTRUCTION paths converge: a refusal built structurally from a
   `RevlError` (`revl.gate.admit`) and the same refusal re-read from the native
   `"<TAG>|message"` wire (`Verdict.from_native`, the shape a rust gate emits)
   serialise to byte-identical JSON — over the whole rejected corpus.
4. A source-level drift guard: if the crate's wire layout or escape set changes,
   this reds, so the two tiers can never silently diverge (the cargo-built
   differential in `test_gate_crate_admit.py` is the heavier, CI-gated proof;
   this is the always-on one).
"""

import sys
from pathlib import Path

import pytest

from revl.gate import Verdict, _json_string, admit

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# The rejected corpus, imported (not copied) from the self-host lowering oracle
# so parity is measured over the same programs `test_gate_crate_admit.py` feeds
# the crate.
import test_selfhost_lower as oracle  # noqa: E402

_CRATE_LIB = _ROOT / "crates" / "revl-gate" / "src" / "lib.rs"


# --------------------------------------------------------------------------
# 1. `_json_string` mirrors the crate's encoder byte for byte.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, encoded",
    [
        ("plain", '"plain"'),
        ('a"b', '"a\\"b"'),
        ("a\\b", '"a\\\\b"'),
        ("a\nb", '"a\\nb"'),
        ("a\rb", '"a\\rb"'),
        ("a\tb", '"a\\tb"'),
        # U+0008 / U+000C: the crate spells these `` / ``, NOT the
        # stdlib's `\b` / `\f`. This is the exact case `json.dumps` would get
        # wrong, so it is the load-bearing row.
        ("\x08", '"\\u0008"'),
        ("\x0c", '"\\u000c"'),
        ("\x00", '"\\u0000"'),
        ("\x1f", '"\\u001f"'),
        # a bar survives (the wire splits on the FIRST bar, but the encoder does
        # not treat it specially).
        ("a|b", '"a|b"'),
        ("unicode ✓ é", '"unicode ✓ é"'),
    ],
)
def test_json_string_mirrors_the_crate_encoder(raw, encoded):
    assert _json_string(raw) == encoded


def test_json_string_never_shortens_backspace_or_formfeed_like_stdlib():
    """The one place `json.dumps` and the crate disagree — proven by showing the
    stdlib would produce a DIFFERENT string here, so the mirror is not
    accidentally `json.dumps`."""
    import json

    assert json.dumps("\x08\x0c") == '"\\b\\f"'
    assert _json_string("\x08\x0c") == '"\\u0008\\u000c"'


# --------------------------------------------------------------------------
# 2. `kind()` and the `to_json()` field layout.
# --------------------------------------------------------------------------


def test_kind_is_admitted_or_refused():
    assert Verdict(True).kind() == "admitted"
    assert Verdict(False, code="G2", message="m").kind() == "refused"


def test_to_json_layout_for_a_refusal():
    v = Verdict(False, code="G2", message="provider table collides on a")
    assert v.to_json() == (
        '{"verdict":"refused","admitted":false,'
        '"code":"G2","message":"provider table collides on a"}')


def test_to_json_layout_for_an_admission():
    assert Verdict(True).to_json() == (
        '{"verdict":"admitted","admitted":true,"code":null,"message":null}')


def test_to_json_nulls_a_missing_code():
    v = Verdict(False, message="no code on this one")
    assert v.to_json() == (
        '{"verdict":"refused","admitted":false,'
        '"code":null,"message":"no code on this one"}')


def test_to_json_escapes_code_and_message():
    v = Verdict(False, code='a"b', message="line1\nline2\ttab")
    assert v.to_json() == (
        '{"verdict":"refused","admitted":false,'
        '"code":"a\\"b","message":"line1\\nline2\\ttab"}')


# --------------------------------------------------------------------------
# 3. The two py construction paths converge on the same wire JSON.
# --------------------------------------------------------------------------


def _rewire(v: Verdict) -> str:
    """The native `"<TAG>|message"` wire a rust gate would emit for the refusal
    `v` (empty tag when `v` carries no code). The message can hold bars; the
    parser splits on the first only, so this round-trips."""
    return f"{v.code or ''}|{v.message}"


@pytest.mark.parametrize(
    "name, source",
    [(name, src) for name, src, *_ in oracle.REJECTED_PROGRAMS],
    ids=[name for name, _src, *_ in oracle.REJECTED_PROGRAMS],
)
def test_structured_refusal_and_wire_reread_serialise_identically(name, source):
    """`admit(source)` builds a refusal structurally from the reference
    `RevlError`; re-reading that refusal's OWN native wire through
    `Verdict.from_native` (the path a rust receiver takes) must land on
    byte-identical JSON. This is the tier-portability contract: the structured
    py path and the wire path a native gate speaks cannot disagree on the wire
    shape."""
    verdict = admit(source)
    assert not verdict.admitted, (
        f"{name}: the reference refuses this, so admit must too")
    assert verdict.kind() == "refused"
    assert verdict.message, f"{name}: a refusal carries a verbatim message"

    reread = Verdict.from_native(_rewire(verdict))
    assert reread.to_json() == verdict.to_json(), (
        f"{name}: structured refusal and wire re-read diverged on the wire\n"
        f"  structured: {verdict.to_json()}\n"
        f"  re-read:    {reread.to_json()}")
    # and the fixed shape holds: admitted is false, the message is verbatim.
    assert reread.message == verdict.message
    assert '"admitted":false' in verdict.to_json()


def test_from_native_admits_on_the_empty_wire_and_serialises_admitted():
    """The empty wire is the native no-refusal signal; on py it reads as an
    admission and serialises with `admitted` true (the py gate does admit,
    unlike the crate, which pins every arm false)."""
    v = Verdict.from_native("")
    assert v.admitted is True
    assert v.to_json() == (
        '{"verdict":"admitted","admitted":true,"code":null,"message":null}')


# --------------------------------------------------------------------------
# 4. Source-level drift guard against the crate's wire layout + escape set.
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _CRATE_LIB.is_file(),
                    reason="the revl-gate crate is not in this tree")
def test_crate_to_json_layout_has_not_drifted_from_py():
    """The crate's `Verdict::to_json` writes the fields in the order py mirrors.
    If either side reorders or renames a field, this reds and forces the two to
    be re-synced (the cheap always-on companion to the cargo-built differential
    in `test_gate_crate_admit.py`)."""
    src = _CRATE_LIB.read_text(encoding="utf-8")
    for fragment in (
        r'String::from("{\"verdict\":")',
        r'",\"admitted\":false,\"code\":"',
        r'",\"message\":"',
    ):
        assert fragment in src, (
            f"the crate's to_json layout changed ({fragment!r} missing); "
            f"re-sync revl.gate.Verdict.to_json with crates/revl-gate/src/lib.rs")


@pytest.mark.skipif(not _CRATE_LIB.is_file(),
                    reason="the revl-gate crate is not in this tree")
def test_crate_json_string_escape_set_matches_py():
    """The crate's `json_string` handles exactly the escapes py's `_json_string`
    does — the named `"`, `\\`, `\\n`, `\\r`, `\\t` plus the `\\u{:04x}` control
    fallback. A new escape on one side without the other would break byte
    parity on some message; this pins the set."""
    src = _CRATE_LIB.read_text(encoding="utf-8")
    arms = [
        "'\"' => out.push_str",   # the quote arm
        r"'\\' => out.push_str",  # the backslash arm
        r"'\n' => out.push_str",
        r"'\r' => out.push_str",
        r"'\t' => out.push_str",
        r'format!("\\u{:04x}"',   # the control fallback
    ]
    for arm in arms:
        assert arm in src, (
            f"the crate's json_string escape set changed ({arm!r} missing); "
            f"re-sync revl.gate._json_string with crates/revl-gate/src/lib.rs")
