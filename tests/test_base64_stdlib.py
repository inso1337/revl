"""The stdlib BASE64 module (stdlib/base64.rvl): RFC 4648 base64url in pure revl.

base64url is how every web-text protocol carries BYTES through a text channel:
JWT segments (RFC 7515), OAuth state, reset tokens and KDF salts are all
base64url. It differs from standard base64 in two load-bearing ways, and this
suite pins both:

  * the alphabet is URL- and filename-safe -- values 62 and 63 are `-` and `_`,
    never `+` and `/` (RFC 4648 section 5);
  * the output is UNPADDED, so a token can be split on `.` without padding
    hanging off a segment.

The module is PURE revl (no `@py`, no `@<backend>` body, no extern at all),
built on the base Str/Int surface, so it lowers on every tier the day it lands.
The py tier is executed here.

Checked here:
  * the module imports through `use`, its public names reach the IR, it
    introduces NO externs, and the file is the documented public surface;
  * RFC 4648 section 10's vectors, through both the encode and the decode door;
  * the empty value, which must encode to "" and decode back to "" (it is a
    value, not a failure, so no sentinel is allowed);
  * the URL-safe alphabet at both special values, and that no output carries
    `=`, `+` or `/`;
  * byte-level encode/decode over List[Int], including every byte value 0..255
    and byte sequences that are not UTF-8;
  * Unicode round trips: 1-, 2-, 3- and 4-byte UTF-8 scalars;
  * STRICT CANONICAL DECODE: a non-zero padding bit, `=` padding, the standard
    base64 characters, whitespace, a length of 1 mod 4 and invalid UTF-8 output
    are each refused with the named error code;
  * `b64url_encode_bytes` refuses a non-byte element rather than masking it.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from _builtin_cases import serve_builtin_cases  # noqa: E402

MODULE = ROOT / "stdlib" / "base64.rvl"

#: a consumer that reaches every public name through one named `use`.
CONSUMER = """\
use "stdlib/base64.rvl" {
  Base64Error, b64url_encode, b64url_decode,
  b64url_encode_bytes, b64url_decode_bytes,
}

fn enc(text: Str) -> Str { return b64url_encode(text) }

fn enc_bytes(bs: List[Int]) -> Result[Str, Base64Error] { return b64url_encode_bytes(bs) }

fn dec(text: Str) -> Result[Str, Base64Error] { return b64url_decode(text) }

fn dec_bytes(text: Str) -> Result[List[Int], Base64Error] { return b64url_decode_bytes(text) }
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface).
    d = tmp_path_factory.mktemp("base64_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "base64.rvl").write_text(MODULE.read_text(encoding="utf-8"),
                                             encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_functions_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"enc", "enc_bytes", "dec", "dec_bytes"} <= names
    # PURE revl: no @py / @ts externs are introduced by the kit
    assert consumer_ir.get("externs", []) == []


def test_module_is_pure_revl_no_backend_body():
    text = MODULE.read_text(encoding="utf-8")
    for marker in ("= @py", "= @ts", "= @rs", "= @go", "= @java", "= @wasm",
                   "extern fn", "extern pure fn"):
        assert marker not in text, f"the module must stay pure revl, found {marker!r}"


def test_module_file_is_the_documented_surface():
    text = MODULE.read_text(encoding="utf-8")
    assert "pub type Base64Error = { code: Str, message: Str }" in text
    assert "pub fn b64url_encode(text: Str) -> Str" in text
    assert "pub fn b64url_encode_bytes(bytes: List[Int]) -> Result[Str, Base64Error]" in text
    assert "pub fn b64url_decode(text: Str) -> Result[Str, Base64Error]" in text
    assert "pub fn b64url_decode_bytes(text: Str) -> Result[List[Int], Base64Error]" in text


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_base64", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    # the emitted module imports the builtin sum cases from the shared runtime
    # instead of defining them itself, so the stub must serve the real Ok/Err
    # (a None placeholder breaks the lowered match).
    serve_builtin_cases(stub)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "base64.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


def _ok(result):
    """The payload of an Ok, asserted to be one (Ok/Err are __slots__ tagged)."""
    assert type(result).__name__ == "Ok", f"expected Ok, got {result!r}"
    return result.value


def _err(result):
    """The Base64Error of an Err, asserted to be one."""
    assert type(result).__name__ == "Err", f"expected Err, got {result!r}"
    return result.value


def _byte_list(result):
    return list(_ok(result))


# ---- RFC 4648 section 10: the published vectors ----------------------------

#: RFC 4648 section 10's test vectors, in the UNPADDED base64url spelling this
#: module emits. The RFC prints the padded standard-base64 form; dropping the
#: `=` and using the URL-safe alphabet is exactly what base64url means.
RFC4648_VECTORS = [
    ("", ""),
    ("f", "Zg"),
    ("fo", "Zm8"),
    ("foo", "Zm9v"),
    ("foob", "Zm9vYg"),
    ("fooba", "Zm9vYmE"),
    ("foobar", "Zm9vYmFy"),
]


@pytest.mark.parametrize("raw,encoded", RFC4648_VECTORS)
def test_rfc4648_vector_encodes(ns, raw, encoded):
    assert ns["enc"](raw) == encoded


@pytest.mark.parametrize("raw,encoded", RFC4648_VECTORS)
def test_rfc4648_vector_decodes(ns, raw, encoded):
    assert _ok(ns["dec"](encoded)) == raw


def test_rfc4648_vectors_are_the_unpadded_url_safe_spelling(ns):
    for _, encoded in RFC4648_VECTORS:
        assert "=" not in encoded
        assert "+" not in encoded and "/" not in encoded


# ---- the empty value -------------------------------------------------------

def test_empty_input_encodes_to_empty_text(ns):
    assert ns["enc"]("") == ""
    assert _ok(ns["enc_bytes"]([])) == ""


def test_empty_text_decodes_to_empty_value_not_a_sentinel(ns):
    # "" is a legitimate value, so the empty result must be an Ok of "", not an
    # Err that a caller could read as failure.
    assert _ok(ns["dec"]("")) == ""
    assert _byte_list(ns["dec_bytes"]("")) == []


# ---- the URL-safe alphabet at 62 and 63 ------------------------------------

#: byte sequences that exercise the two values where base64url differs from
#: standard base64, plus every remainder length of a 3-byte group.
BYTE_VECTORS = [
    ([0, 0, 0], "AAAA"),
    ([255, 255, 255], "____"),
    ([251, 255], "-_8"),              # 62 then 63 in the last group
    ([251, 239, 190], "----"),        # 62 four times
    ([0, 1, 255], "AAH_"),            # 63 in the last, padded group
    ([255], "_w"),                    # one byte, value 63 then 0
    ([0], "AA"),
    ([255, 0], "_wA"),
    ([222, 173, 190, 239, 0, 66, 127], "3q2-7wBCfw"),
    ([0, 1, 2, 3, 4, 250, 251, 252, 253, 254, 255], "AAECAwT6-_z9_v8"),
]


@pytest.mark.parametrize("raw,encoded", BYTE_VECTORS)
def test_byte_vector_encodes(ns, raw, encoded):
    assert _ok(ns["enc_bytes"](raw)) == encoded


@pytest.mark.parametrize("raw,encoded", BYTE_VECTORS)
def test_byte_vector_decodes(ns, raw, encoded):
    assert _byte_list(ns["dec_bytes"](encoded)) == raw


def test_standard_base64_alphabet_is_refused(ns):
    # 0xFB 0xFF is `-_8` here; standard base64 would spell it `+/8`. The
    # standard characters are NOT accepted, because a value that reaches this
    # module came from a URL-safe channel.
    assert _err(ns["dec"]("+/8"))["code"] == "invalid-character"
    assert _err(ns["dec"]("-_8"))["code"] != "invalid-character"


#: every byte value, in order (0..255), in the contract's own spelling.
ALL_256 = (
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
    "MDEyMzQ1Njc4OTo7PD0-P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5f"
    "YGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn-AgYKDhIWGh4iJiouMjY6P"
    "kJGSk5SVlpeYmZqbnJ2en6ChoqOkpaanqKmqq6ytrq-wsbKztLW2t7i5uru8vb6_"
    "wMHCw8TFxsfIycrLzM3Oz9DR0tPU1dbX2Nna29zd3t_g4eLj5OXm5-jp6uvs7e7v"
    "8PHy8_T19vf4-fr7_P3-_w"
)


def test_every_byte_value_round_trips(ns):
    raw = list(range(256))
    assert _ok(ns["enc_bytes"](raw)) == ALL_256
    assert _byte_list(ns["dec_bytes"](ALL_256)) == raw


# ---- Unicode: the UTF-8 byte contract --------------------------------------

#: (Str, the base64url of its UTF-8 bytes). The last four cover the 2-, 3- and
#: 4-byte UTF-8 lengths and a combining sequence, so a decoder that ever went
#: through UTF-16 units or a replacement-character path would disagree.
UNICODE_VECTORS = [
    ("hello", "aGVsbG8"),
    ("héllo", "aMOpbGxv"),
    ("wörld ✓", "d8O2cmxkIOKckw"),
    ("日本語", "5pel5pys6Kqe"),
    ("😀", "8J-YgA"),
    ("a😀b", "YfCfmIBi"),
    ("e\u0301", "ZcyB"),
    ("\U0010ffff", "9I-_vw"),          # the top Unicode scalar, a 4-byte form
]


@pytest.mark.parametrize("raw,encoded", UNICODE_VECTORS)
def test_unicode_encodes_utf8_bytes(ns, raw, encoded):
    assert ns["enc"](raw) == encoded


@pytest.mark.parametrize("raw,encoded", UNICODE_VECTORS)
def test_unicode_round_trips(ns, raw, encoded):
    assert _ok(ns["dec"](encoded)) == raw


def test_control_characters_round_trip(ns):
    # a NUL and a newline are ordinary Unicode scalars; a `Str` door that used a
    # sentinel would not survive either.
    raw = "\x00a\nb"
    encoded = ns["enc"](raw)
    assert "=" not in encoded
    assert _ok(ns["dec"](encoded)) == raw


# ---- strict canonical decode: the refusals --------------------------------

#: (text, error code) for every refusal that is NOT about UTF-8 output.
CANONICAL_REFUSALS = [
    # a group cannot end after one character
    ("A", "invalid-length"),
    ("Zm9vY", "invalid-length"),
    # unpadded only: a padded input is refused, never silently stripped
    ("Zg=", "unexpected-padding"),
    ("Zg==", "unexpected-padding"),
    ("Zm9vYg==", "unexpected-padding"),
    # non-canonical trailing bits: the same bytes have another spelling
    ("Zh", "non-canonical-tail"),      # 2-char tail, low 4 bits are 1
    ("Zm9", "non-canonical-tail"),     # 3-char tail, low 2 bits are 1
    # characters outside the base64url alphabet
    ("+w", "invalid-character"),       # standard base64 62
    ("/w", "invalid-character"),       # standard base64 63
    ("Zg!", "invalid-character"),
    ("Zg ", "invalid-character"),
    ("Zg\n", "invalid-character"),
    ("w6ké", "invalid-character"),     # a non-ASCII code point
]


@pytest.mark.parametrize("text,code", CANONICAL_REFUSALS)
def test_non_canonical_text_is_refused(ns, text, code):
    assert _err(ns["dec"](text))["code"] == code


@pytest.mark.parametrize("text,code", CANONICAL_REFUSALS)
def test_non_canonical_text_is_refused_at_the_byte_door_too(ns, text, code):
    # the byte door must not be a lenient second decoder: the same text is
    # refused by both doors, before UTF-8 is ever considered.
    assert _err(ns["dec_bytes"](text))["code"] == code


def test_refusal_names_the_offset(ns):
    err = _err(ns["dec"]("Zg!"))
    assert err["code"] == "invalid-character"
    assert "offset 2" in err["message"]
    assert "`!`" in err["message"]


def test_padding_refusal_names_the_padding(ns):
    err = _err(ns["dec"]("Zg=="))
    assert err["code"] == "unexpected-padding"
    assert "offset 2" in err["message"]
    assert "UNPADDED" in err["message"]


def test_canonical_spellings_are_accepted(ns):
    # the short tails this module DOES accept: 2 chars for one byte, 3 for two.
    assert _ok(ns["dec"]("Zg")) == "f"
    assert _ok(ns["dec"]("Zm8")) == "fo"
    assert _ok(ns["dec"]("Zm9v")) == "foo"


# ---- invalid UTF-8 output is refused by the Str door only ------------------

#: (base64url text, decoded bytes, a phrase from the UTF-8 refusal) pairs. Every
#: shape a lenient decoder would paper over with a replacement character.
INVALID_UTF8 = [
    ("_w", [255], "cannot start a UTF-8 sequence"),
    ("wK8", [192, 175], "cannot start a UTF-8 sequence"),
    ("4ICA", [224, 128, 128], "an overlong form"),
    ("8ICAgA", [240, 128, 128, 128], "an overlong form"),
    ("7aCA", [237, 160, 128], "surrogate"),
    ("4oI", [226, 130], "truncated"),
    ("wg", [194], "truncated"),
    ("9YCAgA", [245, 128, 128, 128], "cannot start a UTF-8 sequence"),
    ("9JCAgA", [244, 144, 128, 128], "above U+10FFFF"),
]


@pytest.mark.parametrize("text,raw,why", INVALID_UTF8)
def test_byte_door_returns_non_utf8_bytes(ns, text, raw, why):
    # the byte door's contract is bytes, so these decode fine there ...
    assert _byte_list(ns["dec_bytes"](text)) == raw


@pytest.mark.parametrize("text,raw,why", INVALID_UTF8)
def test_str_door_refuses_non_utf8_output(ns, text, raw, why):
    # ... and only the Str door, whose declared result is a Str, refuses.
    err = _err(ns["dec"](text))
    assert err["code"] == "invalid-utf8"
    assert why in err["message"]


# ---- encode_bytes: a byte is 0..255, never a masked Int --------------------

@pytest.mark.parametrize("bad", [[256], [-1], [0, 300], [1, 2, -7]])
def test_encode_bytes_refuses_a_non_byte(ns, bad):
    err = _err(ns["enc_bytes"](bad))
    assert err["code"] == "not-a-byte"
    assert "0..255" in err["message"]


def test_encode_bytes_refusal_names_the_offending_element(ns):
    err = _err(ns["enc_bytes"]([0, 300]))
    assert "offset 1" in err["message"]
    assert "300" in err["message"]
