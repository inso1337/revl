"""Per-file compiled units kept on disk between compiles (issue #3429).

Opt-in only: `compile_files(..., cache_dir=...)` reaches this module; with no
cache directory nothing here runs and no file is written.

The first slice keeps ONE unit per file: the parse of that file, the
`Program` that `parse_text(text, path)` returns. That result is a function of
the file's text, the path it is parsed as (provenance is the path relative to
the invocation directory), and the compiler that parsed it; the key carries
all three, so an entry can only be found by a compile that would have built
the same result. Everything after the parse (body-file, ref and asset
resolution, the merge, checking, lowering, every whole-program analysis, the
link) runs on the decoded program exactly as it runs on a fresh parse.
docs/design/3429-compiled-units.md holds the design and the soundness
argument.

The format is data only: a JSON document whose nodes name a parser class from
a fixed table (`_NODE_CLASSES`). Loading never imports, evaluates or unpickles
anything, and any entry that does not decode exactly (unreadable, truncated,
another format, another key, an unknown class) is rebuilt from the text.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import sys
import tempfile

from . import parser as _parser
from ._paths import relpath_or_abs

#: Bumped whenever the entry layout below changes.
FORMAT = 1

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


class _Undecodable(Exception):
    """An entry that is not exactly what this compiler would have written."""


def _node_classes() -> dict[str, type]:
    """Every dataclass the parser module defines, by name: the only classes
    an entry may name. A name outside this table makes the entry undecodable."""
    table = {}
    for name, value in vars(_parser).items():
        if (isinstance(value, type) and dataclasses.is_dataclass(value)
                and value.__module__ == _parser.__name__):
            table[name] = value
    return table


_NODE_CLASSES = _node_classes()


@functools.lru_cache(maxsize=1)
def compiler_digest() -> str:
    """sha256 over the source of every module in the revl package, plus the
    Python version and the entry format. Wider than the parser alone on
    purpose: any change to the compiler's code invalidates every entry."""
    digest = hashlib.sha256()
    digest.update(f"format={FORMAT}\0python={sys.version}\0".encode())
    for path in _package_sources():
        digest.update(os.path.relpath(path, _PACKAGE_DIR).encode() + b"\0")
        with open(path, "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest()


def _package_sources() -> list[str]:
    found = []
    for root, dirs, files in os.walk(_PACKAGE_DIR):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        found.extend(os.path.join(root, f) for f in sorted(files)
                     if f.endswith(".py"))
    return found


def unit_key(text: str, abs_path: str) -> dict:
    """Everything the parse of `text` at `abs_path` depends on."""
    return {
        "format": FORMAT,
        "compiler": compiler_digest(),
        "path": abs_path,
        "source": relpath_or_abs(abs_path),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _entry_name(key: dict) -> str:
    blob = json.dumps(key, sort_keys=True).encode("utf-8")
    return "parse-" + hashlib.sha256(blob).hexdigest() + ".json"


class UnitCache:
    """One compile's view of a cache directory. `built` and `reused` list the
    absolute paths whose parse unit this compile rebuilt or read back."""

    def __init__(self, directory: str) -> None:
        self.directory = os.path.abspath(directory)
        self.built: list[str] = []
        self.reused: list[str] = []

    def parse(self, text: str, abs_path: str) -> _parser.Program:
        """`parse_text(text, abs_path)`, read back from the directory when an
        entry with this exact key decodes, built and written otherwise. A
        parse that refuses raises as `parse_text` does and writes nothing."""
        key = unit_key(text, abs_path)
        entry_path = os.path.join(self.directory, _entry_name(key))
        program = _read_entry(entry_path, key)
        if program is not None:
            self.reused.append(abs_path)
            return program
        program = _parser.Parser(text, abs_path).parse()
        self.built.append(abs_path)
        _write_entry(entry_path, key, program)
        _parser.record_provenance(program, abs_path)
        return program

    def summary(self) -> dict:
        return {"directory": self.directory, "built": list(self.built),
                "reused": list(self.reused)}


def _read_entry(entry_path: str, key: dict) -> _parser.Program | None:
    try:
        with open(entry_path, encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    try:
        if not isinstance(entry, dict) or entry.get("key") != key:
            return None
        return _decode_program(entry.get("program"), key)
    except (_Undecodable, RecursionError, TypeError, ValueError,
            AttributeError, KeyError, IndexError):
        return None


def _write_entry(entry_path: str, key: dict, program: _parser.Program) -> None:
    """Write atomically (temp file, then rename), so a reader sees a whole
    entry or none. A failure to write leaves the compile's result unchanged."""
    if program.decl_files:
        # identity-keyed, so it cannot be carried to another process; the
        # parser leaves it empty and `record_provenance` fills it
        return
    try:
        encoded = _Encoder().value(program)
        blob = json.dumps({"key": key, "program": encoded},
                          separators=(",", ":"))
        os.makedirs(os.path.dirname(entry_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(entry_path),
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
        os.replace(tmp, entry_path)
    except (OSError, RecursionError, TypeError, ValueError):
        return


# The encoding. A list is a JSON array; str, int, float, bool and None are
# themselves. Everything else is a one-key object whose key is its tag:
#   {"n": [class, {attr: value}]}  a parser node, every attribute it holds
#   {"t": [...]} tuple   {"f": [...]} frozenset   {"s": [...]} set
#   {"d": [[k, v], ...]} dict, keys encoded like values
#   {"r": i}  the i-th node, list, dict or set already met (shared objects
#             stay shared, so identity-keyed tables and the parser's shared
#             `assets` registry keep pointing at the nodes in the tree)
# Encoder and decoder number mutable objects in the same depth-first order.

# Exact types only: a subclass (an IntEnum, a NamedTuple) is not one of these
# and is refused by the encoder, so nothing is written for that program.
_SCALARS = frozenset({str, int, float, bool, type(None)})

_REQUIRED = {name: frozenset(f.name for f in dataclasses.fields(cls))
             for name, cls in _NODE_CLASSES.items()}


class _Encoder:
    def __init__(self) -> None:
        self._seen: dict[int, int] = {}
        self._keep: list = []

    def _register(self, obj) -> dict | None:
        index = self._seen.get(id(obj))
        if index is not None:
            return {"r": index}
        self._seen[id(obj)] = len(self._seen)
        self._keep.append(obj)
        return None

    def value(self, obj):
        kind = type(obj)
        if kind in _SCALARS:
            return obj
        if kind is tuple:
            return {"t": [self.value(x) for x in obj]}
        if kind is frozenset:
            return {"f": [self.value(x) for x in obj]}
        ref = self._register(obj)
        if ref is not None:
            return ref
        if kind is list:
            return [self.value(x) for x in obj]
        if kind is set:
            return {"s": [self.value(x) for x in obj]}
        if kind is dict:
            return {"d": [[self.value(k), self.value(v)]
                          for k, v in obj.items()]}
        return self._node(obj)

    def _node(self, obj):
        cls = type(obj)
        if _NODE_CLASSES.get(cls.__name__) is not cls:
            raise TypeError(f"not a parser node: {cls.__module__}.{cls.__name__}")
        return {"n": [cls.__name__,
                      {name: self.value(v) for name, v in vars(obj).items()}]}


class _Decoder:
    """The inverse of `_Encoder` over what `json.load` returns (dict, list
    and scalars only), refusing anything `_Encoder` would not have written."""

    def __init__(self) -> None:
        self._objects: list = []

    def value(self, data):
        kind = type(data)
        if kind is list:
            out: list = []
            self._objects.append(out)
            value = self.value
            out.extend([value(x) for x in data])
            return out
        if kind is not dict:
            return data
        if len(data) != 1:
            raise _Undecodable("bad value")
        (tag, body), = data.items()
        if tag == "n":
            return self._node(body)
        return self._tagged(tag, body)

    def _tagged(self, tag: str, body):
        if tag == "r":
            if type(body) is not int or not 0 <= body < len(self._objects):
                raise _Undecodable("bad reference")
            return self._objects[body]
        if type(body) is not list:
            raise _Undecodable("bad container")
        if tag == "t":
            return tuple([self.value(x) for x in body])
        if tag == "f":
            return frozenset([self.value(x) for x in body])
        if tag == "s":
            out: set = set()
            self._objects.append(out)
            out.update([self.value(x) for x in body])
            return out
        if tag == "d":
            return self._dict(body)
        raise _Undecodable(f"unknown tag {tag!r}")

    def _dict(self, body: list) -> dict:
        table: dict = {}
        self._objects.append(table)
        for pair in body:
            if type(pair) is not list or len(pair) != 2:
                raise _Undecodable("bad pair")
            table[self.value(pair[0])] = self.value(pair[1])
        return table

    def _node(self, body):
        if type(body) is not list or len(body) != 2:
            raise _Undecodable("bad node")
        name, attrs = body
        cls = _NODE_CLASSES.get(name) if type(name) is str else None
        if cls is None or type(attrs) is not dict:
            raise _Undecodable(f"unknown node {name!r}")
        obj = cls.__new__(cls)
        self._objects.append(obj)
        value = self.value
        fields = {attr: (v if type(v) in _SCALARS else value(v))
                  for attr, v in attrs.items()}
        if not _REQUIRED[name] <= fields.keys():
            raise _Undecodable(f"{name} lacks a field")
        obj.__dict__.update(fields)
        return obj


def _decode_program(data, key: dict) -> _parser.Program:
    with _parser.recursion_headroom():
        program = _Decoder().value(data)
    if type(program) is not _parser.Program or program.decl_files:
        raise _Undecodable("not a freshly parsed program")
    _parser.record_provenance(program, key["path"])
    return program
