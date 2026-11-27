"""Issue #3501: standard base64 doors in stdlib/base64.rvl, and the
sha256-to-base64url bridge in stdlib/crypto.rvl.

The stdlib had base64url only (stdlib/base64.rvl) and `sha256` spelled its
digest as hex only, so a PKCE S256 challenge or a JWT segment was hand-written
each time: a hex-to-bytes step, a base64url encoder over a list of byte
values, and a text-to-bytes step, re-derived per test file.

This suite pins, on the py tier (the other tiers inherit: every new fn is pure
revl over the base Str/Int surface, and crypto.rvl's only extern is the
existing `sha256`):

  * the STANDARD RFC 4648 section 4 variant in stdlib/base64.rvl:
    `base64_encode`/`base64_decode` (Str doors) and `base64_encode_bytes`/
    `base64_decode_bytes` (byte doors), with the `+`/`/` alphabet and `=`
    padding, pinned against RFC 4648 section 10's published vectors;
  * STRICT canonical standard decode: missing/wrong `=` padding, `=` outside
    the final group, the URL-safe `-`/`_` characters, whitespace, a length of
    1 mod 4 and non-zero tail bits are each refused with the named
    `Base64Error.code`;
  * the hex bridge in stdlib/crypto.rvl: `hex_to_bytes`, `hex_to_b64url`
    (bad hex is `Err` with code `invalid-hex`) and `sha256_b64url`, pinned
    against RFC 7636 appendix B's PKCE S256 vector and the NIST sha256("abc")
    digest encoded both ways;
  * crypto.rvl's `use "stdlib/base64.rvl"` adds NO extern of its own (the
    module's extern set is unchanged), and base64.rvl stays pure revl.
"""

import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.test import run_py  # noqa: E402
from _builtin_cases import serve_builtin_cases  # noqa: E402

BASE64 = ROOT / "stdlib" / "base64.rvl"
CRYPTO = ROOT / "stdlib" / "crypto.rvl"

#: a consumer that reaches every new public name through named `use`s, with
#: `pub`-free wrappers for the py tier and `test` blocks for the RUNNERS path.
CONSUMER = """\
use "stdlib/base64.rvl" {
  Base64Error,
  b64url_encode, b64url_decode, b64url_decode_bytes,
  base64_encode, base64_decode, base64_encode_bytes, base64_decode_bytes,
}
use "stdlib/crypto.rvl" { sha256, hex_to_bytes, hex_to_b64url, sha256_b64url }

fn b64e(s: Str) -> Str { return base64_encode(s) }
fn b64d(s: Str) -> Result[Str, Base64Error] { return base64_decode(s) }
fn b64eb(bs: List[Int]) -> Result[Str, Base64Error] { return base64_encode_bytes(bs) }
fn b64db(s: Str) -> Result[List[Int], Base64Error] { return base64_decode_bytes(s) }
fn ue(s: Str) -> Str { return b64url_encode(s) }
fn ud(s: Str) -> Result[Str, Base64Error] { return b64url_decode(s) }
fn udb(s: Str) -> Result[List[Int], Base64Error] { return b64url_decode_bytes(s) }
fn hx(s: Str) -> Result[List[Int], Str] { return hex_to_bytes(s) }
fn hb(s: Str) -> Result[Str, Base64Error] { return hex_to_b64url(s) }
fn s256(s: Str) -> Str { return sha256(s) }
fn s256b64(s: Str) -> Str { return sha256_b64url(s) }

test "RFC 4648 vectors in both spellings" {
  assert base64_encode("foobar") == "Zm9vYmFy"
  assert b64url_encode("foobar") == "Zm9vYmFy"
  assert base64_encode("f") == "Zg=="
  assert b64url_encode("f") == "Zg"
}

test "PKCE S256 (RFC 7636 appendix B)" {
  assert sha256_b64url("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
}
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    d = tmp_path_factory.mktemp("b64std_consumer")
    (d / "stdlib").mkdir()
    shutil.copy(BASE64, d / "stdlib" / "base64.rvl")
    shutil.copy(CRYPTO, d / "stdlib" / "crypto.rvl")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_base64_module_pins_the_standard_doors():
    text = BASE64.read_text(encoding="utf-8")
    assert "pub fn base64_encode(text: Str) -> Str" in text
    assert "pub fn base64_encode_bytes(bytes: List[Int]) -> Result[Str, Base64Error]" in text
    assert "pub fn base64_decode(text: Str) -> Result[Str, Base64Error]" in text
    assert "pub fn base64_decode_bytes(text: Str) -> Result[List[Int], Base64Error]" in text


def test_crypto_module_pins_the_bridge():
    text = CRYPTO.read_text(encoding="utf-8")
    assert 'use "stdlib/base64.rvl" { Base64Error, b64url_encode_bytes }' in text
    assert "pub fn hex_to_bytes(hex: Str) -> Result[List[Int], Str]" in text
    assert "pub fn hex_to_b64url(hex: Str) -> Result[Str, Base64Error]" in text
    assert "pub fn sha256_b64url(data: Str) -> Str" in text


def test_base64_module_stays_pure_revl():
    text = BASE64.read_text(encoding="utf-8")
    for marker in ("= @py", "= @ts", "= @rs", "= @go", "= @java", "= @wasm",
                   "extern fn", "extern pure fn"):
        assert marker not in text, f"base64.rvl must stay pure revl, found {marker!r}"


def test_crypto_import_of_base64_adds_no_extern(consumer_ir):
    # crypto.rvl's own externs are unchanged; stdlib/base64.rvl contributes none
    externs = {e["name"] for e in consumer_ir["externs"]}
    assert externs == {"sha256", "hmac_sha256", "ct_equal", "random_token"}


def test_new_functions_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"base64_encode", "base64_decode", "base64_encode_bytes",
            "base64_decode_bytes", "hex_to_bytes", "hex_to_b64url",
            "sha256_b64url"} <= names


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_3501", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    serve_builtin_cases(stub)
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "b64std.py", "exec"), namespace)
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
    assert type(result).__name__ == "Ok", f"expected Ok, got {result!r}"
    return result.value


def _err(result):
    assert type(result).__name__ == "Err", f"expected Err, got {result!r}"
    return result.value


def _byte_list(result):
    return list(_ok(result))


def test_run_py_test_blocks_pass(consumer_ir):
    verdict, detail = run_py(consumer_ir)
    assert verdict == "pass", detail


# ---- RFC 4648 section 10 vectors, standard PADDED spelling ----------------

RFC4648_STD = [
    ("", ""),
    ("f", "Zg=="),
    ("fo", "Zm8="),
    ("foo", "Zm9v"),
    ("foob", "Zm9vYg=="),
    ("fooba", "Zm9vYmE="),
    ("foobar", "Zm9vYmFy"),
]


@pytest.mark.parametrize("raw,encoded", RFC4648_STD)
def test_rfc4648_standard_encode(ns, raw, encoded):
    assert ns["b64e"](raw) == encoded


@pytest.mark.parametrize("raw,encoded", RFC4648_STD)
def test_rfc4648_standard_decode(ns, raw, encoded):
    assert _ok(ns["b64d"](encoded)) == raw


def test_standard_output_carries_padding_and_the_plus_slash_alphabet(ns):
    out = ns["b64e"]("foob")
    assert out == "Zm9vYg=="
    assert "=" in out
    assert ns["b64eb"]([255, 255, 255]) is not None
    assert _ok(ns["b64eb"]([255, 255, 255])) == "////"
    assert _ok(ns["b64eb"]([251, 255])) == "+/8="


# ---- byte-level standard vectors -------------------------------------------

STD_BYTE_VECTORS = [
    ([0, 0, 0], "AAAA"),
    ([255, 255, 255], "////"),
    ([251, 255], "+/8="),
    ([251, 239, 190], "++++"),
    ([0, 1, 255], "AAH/"),
    ([255], "/w=="),
    ([0], "AA=="),
    ([255, 0], "/wA="),
    ([222, 173, 190, 239, 0, 66, 127], "3q2+7wBCfw=="),
    ([0, 1, 2, 3, 4, 250, 251, 252, 253, 254, 255], "AAECAwT6+/z9/v8="),
]


@pytest.mark.parametrize("raw,encoded", STD_BYTE_VECTORS)
def test_std_byte_vector_encodes(ns, raw, encoded):
    assert _ok(ns["b64eb"](raw)) == encoded


@pytest.mark.parametrize("raw,encoded", STD_BYTE_VECTORS)
def test_std_byte_vector_decodes(ns, raw, encoded):
    assert _byte_list(ns["b64db"](encoded)) == raw


#: standard base64 of every byte value 0..255 in order.
STD_ALL_256 = (
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4v"
    "MDEyMzQ1Njc4OTo7PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5f"
    "YGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn+AgYKDhIWGh4iJiouMjY6P"
    "kJGSk5SVlpeYmZqbnJ2en6ChoqOkpaanqKmqq6ytrq+wsbKztLW2t7i5uru8vb6/"
    "wMHCw8TFxsfIycrLzM3Oz9DR0tPU1dbX2Nna29zd3t/g4eLj5OXm5+jp6uvs7e7v"
    "8PHy8/T19vf4+fr7/P3+/w=="
)


def test_std_every_byte_value_round_trips(ns):
    raw = list(range(256))
    assert _ok(ns["b64eb"](raw)) == STD_ALL_256
    assert _byte_list(ns["b64db"](STD_ALL_256)) == raw


def test_std_encode_bytes_refuses_a_non_byte(ns):
    err = _err(ns["b64eb"]([0, 300]))
    assert err["code"] == "not-a-byte"
    assert "offset 1" in err["message"]


# ---- Unicode through the standard doors -------------------------------------

STD_UNICODE = [
    ("hello", "aGVsbG8="),
    ("h\u00e9llo", "aMOpbGxv"),
    ("w\u00f6rld \u2713", "d8O2cmxkIOKckw=="),
    ("\u65e5\u672c\u8a9e", "5pel5pys6Kqe"),
    ("\U0001f600", "8J+YgA=="),
    ("e\u0301", "ZcyB"),
    ("\U0010ffff", "9I+/vw=="),
    ("\x00a\nb", "AGEKYg=="),
]


@pytest.mark.parametrize("raw,encoded", STD_UNICODE)
def test_std_unicode_encodes_utf8_bytes(ns, raw, encoded):
    assert ns["b64e"](raw) == encoded


@pytest.mark.parametrize("raw,encoded", STD_UNICODE)
def test_std_unicode_round_trips(ns, raw, encoded):
    assert _ok(ns["b64d"](encoded)) == raw


def test_empty_value_round_trips_on_all_four_doors(ns):
    assert ns["b64e"]("") == ""
    assert _ok(ns["b64d"]("")) == ""
    assert _ok(ns["b64eb"]([])) == ""
    assert _byte_list(ns["b64db"]("")) == []


# ---- strict canonical STANDARD decode: the refusals -------------------------

STD_REFUSALS = [
    ("A", "invalid-length"),          # a group cannot end after one char
    ("Zm9vY", "invalid-length"),
    ("Zg", "invalid-padding"),        # missing `==`
    ("Zg=", "invalid-padding"),       # needs two `=`
    ("Zg===", "invalid-padding"),     # too many `=`
    ("Zm9vYg=", "invalid-padding"),   # complete group + stray pad
    ("Zm9v====", "invalid-padding"),
    ("=Zg=", "invalid-padding"),      # `=` outside the final group
    ("Zh==", "non-canonical-tail"),   # low 4 bits of the 2-char tail are 1
    ("Zm9=", "non-canonical-tail"),   # low 2 bits of the 3-char tail are 1
    ("-_8=", "invalid-character"),    # the URL-safe alphabet is refused here
    ("Zg!", "invalid-character"),
    ("Zg =", "invalid-character"),
    ("Zg\n==", "invalid-character"),
]


@pytest.mark.parametrize("text,code", STD_REFUSALS)
def test_std_non_canonical_text_is_refused(ns, text, code):
    assert _err(ns["b64d"](text))["code"] == code


@pytest.mark.parametrize("text,code", STD_REFUSALS)
def test_std_refusal_at_the_byte_door_too(ns, text, code):
    assert _err(ns["b64db"](text))["code"] == code


def test_std_invalid_character_names_the_offset(ns):
    err = _err(ns["b64d"]("Zg!"))
    assert err["code"] == "invalid-character"
    assert "offset 2" in err["message"]
    assert "`!`" in err["message"]


def test_std_padding_refusal_names_the_counts(ns):
    err = _err(ns["b64d"]("Zg="))
    assert err["code"] == "invalid-padding"
    assert "needs 2" in err["message"]
    assert "got 1" in err["message"]


def test_std_door_accepts_its_canonical_tails(ns):
    assert _ok(ns["b64d"]("Zg==")) == "f"
    assert _ok(ns["b64d"]("Zm8=")) == "fo"
    assert _ok(ns["b64d"]("Zm9v")) == "foo"


def test_the_two_variants_refuse_each_others_spelling(ns):
    # `+/8=` is the standard spelling of [251, 255]; the URL-safe door refuses
    # it, the standard door accepts it, and vice versa for `Zg==`.
    assert _ok(ns["b64db"]("+/8=")) == [251, 255]
    assert _err(ns["b64db"]("-_8="))["code"] == "invalid-character"
    assert _err(ns["ud"]("Zg=="))["code"] == "unexpected-padding"
    assert _ok(ns["b64db"]("Zg==")) == [102]


def test_std_str_door_refuses_non_utf8_output(ns):
    # "/w==" decodes to byte 255, which is not UTF-8: fine at the byte door,
    # refused by the Str door.
    assert _byte_list(ns["b64db"]("/w==")) == [255]
    err = _err(ns["b64d"]("/w=="))
    assert err["code"] == "invalid-utf8"


# ---- the hex / sha256 bridge in crypto.rvl ----------------------------------

def test_hex_to_bytes_vectors(ns):
    assert _byte_list(ns["hx"]("")) == []
    assert _byte_list(ns["hx"]("00ff")) == [0, 255]
    assert _byte_list(ns["hx"]("FF")) == [255]          # upper case accepted
    assert _byte_list(ns["hx"]("Ba78")) == [186, 120]


@pytest.mark.parametrize("bad", ["abc", "zz", "0g", "ba7816bf!"])
def test_hex_to_bytes_refuses_bad_hex(ns, bad):
    assert type(ns["hx"](bad)).__name__ == "Err"


def test_hex_to_bytes_refusal_names_the_pair(ns):
    err = ns["hx"]("0g")
    assert type(err).__name__ == "Err"
    assert "not a hex digit" in err.value


def test_hex_to_b64url_of_the_sha256_abc_digest(ns):
    # sha256("abc") = ba7816bf...; its RAW digest in unpadded base64url:
    hex_abc = ns["s256"]("abc")
    assert hex_abc == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert _ok(ns["hb"](hex_abc)) == "ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0"


def test_hex_to_b64url_refuses_bad_hex_with_invalid_hex_code(ns):
    err = _err(ns["hb"]("zz"))
    assert err["code"] == "invalid-hex"
    assert "not a hex digit" in err["message"]


def test_sha256_b64url_pkce_rfc7636_vector(ns):
    # RFC 7636 appendix B: verifier -> S256 challenge.
    assert ns["s256b64"]("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") \
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_sha256_b64url_matches_python_for_nist_vectors(ns):
    assert ns["s256b64"]("abc") == "ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0"
    assert ns["s256b64"]("") == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"


def test_sha256_b64url_is_unpadded_url_safe_text(ns):
    out = ns["s256b64"]("hello")
    assert len(out) == 43              # 32 bytes -> ceil(32*8/6) = 43 chars
    assert "=" not in out
    assert "+" not in out and "/" not in out
    # and it round-trips through the base64url byte door to the raw digest,
    # whose hex spelling is sha256 itself
    digest = _byte_list(ns["udb"](out))
    assert len(digest) == 32
    var_hex = "".join("{:02x}".format(b) for b in digest)
    assert var_hex == ns["s256"]("hello")
