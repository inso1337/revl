"""Constrained decoding through the provider adapters (issue #1462).

`revl.decode_grammar` derives two artifacts from a `validated` crossing's
return type: a GBNF grammar and a JSON Schema (the "wire schema"), registered
per crossing in the py runtime (`register_grammars`). This module is how each
adapter hands one of them to its provider, and says, per provider, which revl
types the provider can represent exactly and which it only approximates.

TAKING VERSUS READING
---------------------
The runtime seam has two calls (`docs/design/542-grammar-constrained-decoding.md`
section 9):

* `revl_constrain(key, dialects)` TAKES the artifact and CLAIMS the decode was
  constrained by it. `validate_response` then holds the completion to it, and a
  false claim is a named `GrammarNotHonouredError`.
* `revl_decode_grammar(key)` READS it and claims nothing.

An adapter takes the artifact only when its provider enforces exactly that
artifact during decoding. That is the OpenAI-compatible adapter in
`json-schema` mode (`response_format`) and in `gbnf` mode (llama.cpp's
`grammar` field). The Anthropic and Gemini adapters READ the wire schema and
send it (a forced tool's `input_schema`; `responseSchema`), but claim nothing:
tool input is schema-guided rather than grammar-constrained, and
`responseSchema` is a translation into a smaller vocabulary. Their output is
still validated against the revl type on return, by item 257's seam, like
every `validated` crossing.

WHAT EACH ADAPTER CAN REPRESENT
-------------------------------
`representation(provider, mode, wire_schema)` returns the gaps for one type,
and `docs/model-providers.md` tabulates them. In short:

* **openai-compatible, json-schema**: the wire schema is sent unchanged. Exact
  on a server whose JSON Schema engine supports every keyword it uses (`type`,
  `properties`, `required`, `additionalProperties`, `items`, `anyOf`, `oneOf`,
  `const`); llama.cpp, Ollama, vLLM and SGLang do. Hosted OpenAI's strict mode
  refuses a non-object root and `oneOf` with an HTTP 400, which is loud.
  `Bytes` is a plain string in this dialect (base64 is not constrained), the
  one difference `decode_grammar` already names.
* **openai-compatible, gbnf**: the GBNF text is sent as `grammar`. Exact on
  llama.cpp's server. A server that ignores the field (Ollama does, silently:
  design note section 11.1) is caught: the claim is checked on return, and with
  llguidance installed the raw completion is checked byte for byte.
* **anthropic, tool**: every type approximated (not decoder-enforced). A root
  that is not an object is wrapped as `{"value": ...}` and unwrapped on return.
* **gemini, response-schema**: `Opt` becomes `nullable`, a string `const`
  becomes a one-value `enum`, a tagged variant's `oneOf` becomes `anyOf` (exact:
  the arms are disjoint by tag), and member order is pinned with
  `propertyOrdering`. A closed object loses `additionalProperties: false`
  (approximated), and a `Map[Str, V]` has no `responseSchema` form at all, so a
  type containing one is sent as `responseMimeType: application/json` only.

THE LOCAL ENGINE (optional)
---------------------------
With `llguidance` installed (`pip install revl[constrain]`), two things are
checked that are otherwise not:

* a derived GBNF grammar is compiled by a real grammar engine before an
  adapter sends it, and an adapter will not claim a grammar that engine
  refuses;
* a completion made under a `gbnf` claim is matched BYTE FOR BYTE against the
  grammar. The runtime's own check sees only the decoded value (member order);
  whitespace, number spelling and text around the JSON are gone by then. A
  completion the engine refuses raises `GrammarNotHonouredError` from the
  crossing, which the retry loop treats like any other response fault.

Without it, both checks are skipped and everything else is unchanged.
llguidance is never a required dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

#: `Binding.structured_output` values per provider; the first is the default.
MODES = {
    "openai-compatible": ("json-schema", "gbnf", "none"),
    "anthropic": ("tool", "none"),
    "gemini": ("response-schema", "none"),
}

#: The modes that claim (take the artifact), and the dialect each takes.
CLAIMING = {"json-schema": "json-schema", "gbnf": "gbnf"}

#: The tool name the Anthropic adapter forces.
TOOL_NAME = "respond"

APPROXIMATE_TOOL = ("Anthropic tool input is schema-guided, not "
                    "grammar-constrained, so no decode is claimed; the value "
                    "is validated on return")


@dataclass(frozen=True)
class Structured:
    """The artifact attached to one request.

    `claimed` is True when the adapter took it through `revl_constrain`, so
    the runtime will hold the completion to it."""

    mode: str
    dialect: str
    artifact: object
    digest: str | None = None
    claimed: bool = False
    gaps: tuple = ()


# --------------------------------------------------------------------------
# Anthropic: a forced tool
# --------------------------------------------------------------------------

def tool_input_schema(wire: dict) -> tuple:
    """`(input_schema, wrapped)`. A tool's input must be an object, so any
    other root is wrapped as `{"value": <root>}`."""
    if isinstance(wire, dict) and wire.get("type") == "object" \
            and "properties" in wire:
        return wire, False
    return ({"type": "object", "properties": {"value": wire},
             "required": ["value"], "additionalProperties": False}, True)


# --------------------------------------------------------------------------
# Gemini: responseSchema, the OpenAPI subset
# --------------------------------------------------------------------------

_GEMINI_TYPES = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
                 "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT"}


class _NoForm(Exception):
    """A node `responseSchema` cannot express at all."""


def _gemini_node(node, gaps: list):
    if not isinstance(node, dict):
        raise _NoForm("a schema node that is not an object")
    arms = node.get("anyOf")
    if isinstance(arms, list) and len(arms) == 2 \
            and {"type": "null"} in arms:
        inner = next(a for a in arms if a != {"type": "null"})
        out = dict(_gemini_node(inner, gaps))
        out["nullable"] = True
        return out
    for key in ("oneOf", "anyOf"):
        if isinstance(node.get(key), list):
            tags = [((arm.get("properties") or {}).get("tag") or {})
                    .get("const") for arm in node[key]]
            if key == "oneOf" and (None in tags or len(set(tags)) != len(tags)):
                gaps.append("a `oneOf` whose arms are not disjoint by tag "
                            "becomes `anyOf`, which admits a value matching "
                            "two arms")
            return {"anyOf": [_gemini_node(arm, gaps) for arm in node[key]]}
    if "const" in node:
        value = node["const"]
        if not isinstance(value, str):
            raise _NoForm("a non-string `const`")
        return {"type": "STRING", "enum": [value]}
    kind = node.get("type")
    if kind not in _GEMINI_TYPES:
        raise _NoForm(f"type {kind!r}")
    out: dict = {"type": _GEMINI_TYPES[kind]}
    if kind == "array":
        out["items"] = _gemini_node(node.get("items") or {}, gaps)
    if kind == "object":
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            raise _NoForm("`Map[Str, V]` (an object keyed by any string)")
        props = node.get("properties") or {}
        if not props:
            raise _NoForm("an object with no declared members")
        out["properties"] = {name: _gemini_node(sub, gaps)
                             for name, sub in props.items()}
        out["required"] = list(node.get("required") or props)
        out["propertyOrdering"] = list(props)
        if extra is False:
            gaps.append("a closed object: `responseSchema` has no "
                        "`additionalProperties: false`, so extra members are "
                        "not excluded by the decoder")
    return out


def gemini_response_schema(wire: dict) -> tuple:
    """`(schema or None, gaps)`. None means the type has no `responseSchema`
    form and only `responseMimeType: application/json` is sent."""
    gaps: list = []
    try:
        schema = _gemini_node(wire, gaps)
    except _NoForm as exc:
        return None, (f"{exc} has no `responseSchema` form, so only "
                      f"`responseMimeType: application/json` is sent",)
    return schema, tuple(dict.fromkeys(gaps))


# --------------------------------------------------------------------------
# per adapter, per type: exact or approximated
# --------------------------------------------------------------------------

def _uses(node, keyword) -> bool:
    if isinstance(node, dict):
        if keyword in node:
            return True
        return any(_uses(v, keyword) for v in node.values())
    if isinstance(node, list):
        return any(_uses(v, keyword) for v in node)
    return False


def representation(provider: str, mode: str, wire: dict) -> tuple:
    """The gaps between the revl type (as its wire schema) and what this
    adapter sends. An empty tuple means exact."""
    if mode == "none":
        return ("no structured output is requested",)
    if provider == "openai-compatible":
        if mode == "gbnf":
            return ()
        gaps = []
        if not (isinstance(wire, dict) and wire.get("type") == "object") \
                or _uses(wire, "oneOf"):
            gaps.append("hosted OpenAI strict mode refuses a non-object root "
                        "and `oneOf` (HTTP 400); local servers accept both")
        return tuple(gaps)
    if provider == "anthropic":
        return (APPROXIMATE_TOOL,)
    if provider == "gemini":
        return gemini_response_schema(wire)[1]
    raise ValueError(f"unknown provider {provider}")


# --------------------------------------------------------------------------
# the optional local engine
# --------------------------------------------------------------------------

class GrammarEngineError(ValueError):
    """The local grammar engine refused a derived grammar."""


def engine_available() -> bool:
    try:
        import llguidance  # noqa: F401, PLC0415 - optional
    except ImportError:
        return False
    return True


class _ByteTokenizer:
    """A 256-byte vocabulary, so the engine matches text byte by byte and no
    model tokenizer is needed."""

    eos_token_id = 256
    bos_token_id = None
    tokens = [bytes([i]) for i in range(256)] + [b"<eos>"]
    special_token_ids = [256]

    def __call__(self, text):
        return list(text if isinstance(text, bytes) else text.encode("utf-8"))


_TOKENIZER = None


class Recogniser:
    """A compiled GBNF grammar that answers "is this text in the language"."""

    def __init__(self, gbnf: str) -> None:
        global _TOKENIZER
        import llguidance as llg  # noqa: PLC0415 - optional
        from llguidance.gbnf_to_lark import gbnf_to_lark  # noqa: PLC0415
        if _TOKENIZER is None:
            _TOKENIZER = llg.LLTokenizer(llg.TokenizerWrapper(_ByteTokenizer()),
                                         slices=[])
        try:
            lark = gbnf_to_lark(gbnf)
        except Exception as exc:  # noqa: BLE001 - the converter's own errors
            raise GrammarEngineError(f"llguidance cannot read the GBNF: "
                                     f"{exc}") from None
        self._grammar = llg.LLMatcher.grammar_from_lark(lark)
        error = llg.LLMatcher.validate_grammar(self._grammar, _TOKENIZER)
        if error:
            raise GrammarEngineError(f"llguidance refuses the grammar: "
                                     f"{error.splitlines()[0]}")
        self._llg = llg

    def error(self, text: str) -> str | None:
        """None when `text` is a complete sentence of the grammar, else the
        first line of the engine's reason."""
        matcher = self._llg.LLMatcher(_TOKENIZER, self._grammar, log_level=0)
        ok = matcher.consume_tokens(list(text.encode("utf-8")))
        if ok and not matcher.is_error() and matcher.is_accepting():
            return None
        if not matcher.is_error():
            return "the completion ends before the grammar's language does"
        return matcher.get_error().splitlines()[0]


_RECOGNISERS: dict = {}


def recogniser_for(gbnf: str, digest: str | None):
    """The compiled recogniser for a grammar, cached by digest, or None when
    llguidance is not installed. Raises `GrammarEngineError` when it is
    installed and refuses the grammar."""
    if not engine_available():
        return None
    key = digest or gbnf
    if key not in _RECOGNISERS:
        _RECOGNISERS[key] = Recogniser(gbnf)
    return _RECOGNISERS[key]
