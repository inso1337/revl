"""Issue #2648: text-mode `open()` and `Path.open()` must name an encoding.

A text-mode `open(path)` with no encoding uses the platform default: UTF-8 on
macOS and Linux, the ANSI code page (cp1252) on Windows. A write of a character
outside cp1252 raises `UnicodeEncodeError`; a read of a UTF-8 file containing
non-ASCII decodes to different characters on Windows, so a compare, parse or
re-emit that depends on the characters is wrong. #2639 is the same defect on
`Path.read_text()`; this guard covers the builtin and `Path.open()`.

THE RULE. Two calls are in scope:

* a call to the builtin `open` in text mode with no encoding;
* a call to `.open` whose receiver resolves to `pathlib.Path` from evidence in
  the same file, in text mode with no encoding.

Text mode means the mode argument is absent or a string literal that does not
contain `b`. An encoding is named when the call has an `encoding` keyword, or
when it passes the encoding positionally: the fourth positional argument of the
builtin `open(file, mode, buffering, encoding)`, the third of
`Path.open(mode, buffering, encoding)`. Binary modes are counted separately and
are not violations; a binary stream must keep returning bytes.

AMBIGUOUS CALLS, NAMED RATHER THAN GUESSED.

* A builtin `open` whose mode is not a string literal is treated as text, the
  conservative reading, and is reported.
* A call that passes `**kwargs` cannot be shown to name an encoding, so it is
  reported until the encoding is written out.
* An attribute `.open` call is in scope only when the receiver is PROVEN to be
  a `pathlib.Path`, never by a file:line allowlist. Evidence, all from the same
  file: a parameter or return annotation naming `Path` or `pathlib.Path`,
  `Path(...)`, `tmp_path / ...` (the pytest fixture), a same-file helper whose
  annotation or return value is a Path, a path-typed local used as a call
  argument, and tuple unpacking whose elements are path expressions. The 148
  attribute `.open` calls in this tree whose receiver is not proven a Path are
  different APIs: os.open 31, tarfile.open 6, replay.WriteAheadLog(...).open 74,
  Pool.open 16, provisions.open 15, and wal, self, self._revl_provisions, log and
  opener 6. None of them takes the `encoding` argument the way text-mode
  `open` does; os.open takes flags and tarfile.open passes an encoding through
  to TarFile.

The inventory line the tree test asserts is a floor on the calls examined and an
exact zero on the violations, so a receiver filter that silently drops calls
cannot turn the test green. The seven text-mode `Path.open` sites in the agreed
scope and the twelve builtin text-mode `open` sites that main added after the
sweep are pinned at zero unpinned; the three binary `Path.open` calls are
counted as binary and stay outside the scope.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {".git", ".claude", "node_modules", ".venv", "venv", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", "dist",
             "build"}

REASON = (
    "text-mode open() without an encoding uses the platform default, cp1252 on "
    "Windows, so a UTF-8 read decodes differently there and a write of a "
    'character outside cp1252 raises UnicodeEncodeError. Name the encoding, '
    'e.g. open(path, encoding="utf-8").')

# Floors, not exact counts: they exist so a broken walk or a receiver filter
# that drops calls is visible. The two zeros are exact. The text-with-encoding
# floor is the sweep's own result, so converting a pinned text call to binary
# or deleting it is a deliberate inventory change, not a silent one.
MIN_FILES = 1566
MIN_BUILTIN_OPEN = 232
MIN_BUILTIN_TEXT_ENC = 200
MIN_BUILTIN_BINARY = 32
MIN_ATTR_OPEN = 159
MIN_PATHLIB = 11
MIN_PATH_TEXT_ENC = 8
MIN_PATH_BINARY = 3


def source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def _is_path_annotation(node) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "Path"
    if isinstance(node, ast.Attribute):
        return node.attr == "Path"
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.strip("'\"") in (
            "Path", "pathlib.Path")
    if isinstance(node, ast.Subscript):
        return _is_path_annotation(node.slice)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_path_annotation(node.left) or _is_path_annotation(node.right)
    return False


def _func_name(node: ast.Call):
    return node.func.id if isinstance(node.func, ast.Name) else None


def _mode(call: ast.Call, index: int):
    """(mode, is_literal). No mode argument is the literal default read."""
    if len(call.args) > index:
        arg = call.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value, True
        return None, False
    return "r", True


def _encoding_state(call: ast.Call, positional_index: int):
    """True: named. False: absent. None: hidden behind **kwargs."""
    for keyword in call.keywords:
        if keyword.arg == "encoding":
            return True
        if keyword.arg is None:
            return None
    if len(call.args) > positional_index:
        return True
    return False


class FileResolution:
    """Path-typed names and same-file helpers, for one parsed module.

    The analysis is a small fixed point over assignments, annotations, call
    arguments and return values. It is deliberately same-file: a receiver is in
    scope only when this file proves it, so a custom `x.open()` is never
    guessed into the scope.
    """

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.funcs = {}
        self.returns = {}
        self.return_positions = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name] = node
        self.path_names = {"tmp_path"}
        self.path_funcs = set()
        for name, function in self.funcs.items():
            for argument in (list(function.args.posonlyargs)
                             + list(function.args.args)
                             + list(function.args.kwonlyargs)):
                if _is_path_annotation(argument.annotation):
                    self.path_names.add(argument.arg)
            if _is_path_annotation(function.returns):
                self.path_funcs.add(name)
            self.return_positions[name] = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Return):
                    self.returns.setdefault(name, []).append(node)
        self.calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self._fixpoint()

    def _is_path(self, node) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in self.path_names
        if isinstance(node, ast.Attribute):
            return node.attr == "parent" and self._is_path(node.value)
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                return function.id == "Path" or function.id in self.path_funcs
            if isinstance(function, ast.Attribute):
                if function.attr == "Path":
                    return True
                if function.attr in {"resolve", "absolute", "expanduser",
                                     "joinpath", "with_name", "with_suffix"}:
                    return self._is_path(function.value)
            return False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_path(node.left) or self._is_path(node.right)
        if isinstance(node, ast.IfExp):
            return self._is_path(node.body) or self._is_path(node.orelse)
        return False

    def _positions(self, expression) -> set:
        if isinstance(expression, ast.Tuple):
            return {i for i, element in enumerate(expression.elts)
                    if self._is_path(element)}
        return {None} if self._is_path(expression) else set()

    @staticmethod
    def _state(path_names, path_funcs, return_positions):
        return (frozenset(path_names), frozenset(path_funcs),
                tuple(sorted((name, tuple(sorted(
                    (-1 if i is None else i) for i in positions)))
                    for name, positions in return_positions.items())))

    def _fixpoint(self):
        assignments = [n for n in ast.walk(self.tree)
                       if isinstance(n, (ast.Assign, ast.AnnAssign,
                                         ast.NamedExpr))]
        for _ in range(50):
            before = self._state(self.path_names, self.path_funcs,
                                 self.return_positions)
            for name, returns in self.returns.items():
                for node in returns:
                    if node.value is not None:
                        self.return_positions[name] |= self._positions(node.value)
            for name, positions in self.return_positions.items():
                if positions:
                    self.path_funcs.add(name)
            for node in assignments:
                self._assign(node)
            for node in self.calls:
                self._call_flow(node)
            if before == self._state(self.path_names, self.path_funcs,
                                     self.return_positions):
                return

    def _assign(self, node):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if isinstance(node, ast.AnnAssign) and _is_path_annotation(node.annotation):
            for target in targets:
                if isinstance(target, ast.Name):
                    self.path_names.add(target.id)
        if value is None:
            return
        if (isinstance(value, ast.Tuple) and len(targets) == 1
                and isinstance(targets[0], ast.Tuple)):
            for target, element in zip(targets[0].elts, value.elts):
                if isinstance(target, ast.Name) and self._is_path(element):
                    self.path_names.add(target.id)
            return
        if targets and isinstance(targets[0], ast.Tuple):
            if isinstance(value, ast.Call):
                name = _func_name(value)
                positions = self.return_positions.get(name, set()) if name else set()
                for index in positions:
                    if index is None or index >= len(targets[0].elts):
                        continue
                    target = targets[0].elts[index]
                    if isinstance(target, ast.Name):
                        self.path_names.add(target.id)
            return
        if (all(isinstance(target, ast.Name) for target in targets)
                and self._is_path(value)):
            for target in targets:
                self.path_names.add(target.id)

    def _call_flow(self, call: ast.Call):
        name = _func_name(call)
        if not name or name not in self.funcs:
            return
        function = self.funcs[name]
        parameters = list(function.args.posonlyargs) + list(function.args.args)
        for argument, parameter in zip(call.args, parameters):
            if self._is_path(argument):
                self.path_names.add(parameter.arg)


def scan(root: Path):
    """(findings as `file:line: reason`, inventory dict, attribute calls seen)."""
    findings: list[str] = []
    inventory = {
        "files": 0,
        "builtin_open": 0,
        "builtin_text": 0,
        "builtin_text_enc": 0,
        "builtin_binary": 0,
        "attr_open": 0,
        "pathlib": 0,
        "path_text": 0,
        "path_text_enc": 0,
        "path_binary": 0,
        "violations": 0,
    }
    for path in source_files(root):
        data = path.read_bytes()
        inventory["files"] += 1
        if b"open(" not in data and b".open" not in data:
            continue
        tree = ast.parse(data, str(path))
        relative = path.relative_to(root)
        resolution = FileResolution(tree) if b".open" in data else None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == "open":
                inventory["builtin_open"] += 1
                mode, is_literal = _mode(node, 1)
                encoding = _encoding_state(node, 3)
                if encoding is True:
                    inventory["builtin_text_enc"] += 1
                    continue
                if is_literal and "b" in mode:
                    inventory["builtin_binary"] += 1
                    continue
                inventory["builtin_text"] += 1
                inventory["violations"] += 1
                if encoding is None:
                    detail = "passes **kwargs, so the encoding is unproven"
                elif not is_literal:
                    detail = "mode is not a literal, treated as text"
                else:
                    detail = "no encoding in text mode"
                findings.append(f"{relative}:{node.lineno}: builtin open(): {detail}. "
                                f"{REASON}")
            elif isinstance(function, ast.Attribute) and function.attr == "open":
                inventory["attr_open"] += 1
                if resolution is None or not resolution._is_path(function.value):
                    continue
                inventory["pathlib"] += 1
                mode, is_literal = _mode(node, 0)
                encoding = _encoding_state(node, 2)
                if encoding is True:
                    inventory["path_text_enc"] += 1
                    continue
                if is_literal and "b" in mode:
                    inventory["path_binary"] += 1
                    continue
                inventory["path_text"] += 1
                inventory["violations"] += 1
                findings.append(f"{relative}:{node.lineno}: Path.open(): "
                                f"no encoding in text mode. {REASON}")
    return findings, inventory, inventory["attr_open"]


def test_the_tree_has_no_text_mode_open_without_an_encoding():
    findings, inventory, attr_calls = scan(ROOT)
    assert findings == [], "\n".join(findings)
    assert inventory["violations"] == 0
    assert inventory["builtin_text"] == 0
    assert inventory["path_text"] == 0
    # Vacuity: a green verdict over a walk that read nothing means nothing.
    assert inventory["files"] >= MIN_FILES, (
        f"only {inventory['files']} .py files scanned; the walk is broken")
    assert inventory["builtin_open"] >= MIN_BUILTIN_OPEN, (
        f"only {inventory['builtin_open']} builtin open() calls seen")
    assert inventory["builtin_text_enc"] >= MIN_BUILTIN_TEXT_ENC, (
        f"only {inventory['builtin_text_enc']} pinned text-mode builtin open() "
        "calls seen")
    assert inventory["builtin_binary"] >= MIN_BUILTIN_BINARY, (
        f"only {inventory['builtin_binary']} binary builtin open() calls seen")
    assert inventory["attr_open"] >= MIN_ATTR_OPEN, (
        f"only {inventory['attr_open']} attribute .open() calls seen")
    assert attr_calls == inventory["attr_open"]
    assert inventory["pathlib"] >= MIN_PATHLIB, (
        f"only {inventory['pathlib']} pathlib .open() calls seen")
    assert inventory["path_text_enc"] >= MIN_PATH_TEXT_ENC, (
        f"only {inventory['path_text_enc']} pinned text-mode Path.open() calls seen")
    assert inventory["path_binary"] >= MIN_PATH_BINARY, (
        f"only {inventory['path_binary']} binary Path.open() calls seen")


def _write(root: Path, name: str, source: str) -> None:
    (root / name).write_text(source, encoding="utf-8")


def test_a_new_builtin_text_open_without_an_encoding_fails(tmp_path):
    _write(tmp_path, "a.py",
           "def load(path):\n"
           "    with open(path) as handle:\n"
           "        return handle.read()\n")
    findings, inventory, _ = scan(tmp_path)
    assert len(findings) == 1, findings
    assert findings[0].startswith("a.py:2: "), findings[0]
    assert "cp1252" in findings[0]
    assert inventory["builtin_text"] == 1 and inventory["violations"] == 1


def test_a_new_path_open_without_an_encoding_fails(tmp_path):
    _write(tmp_path, "a.py",
           "from pathlib import Path\n"
           "\n"
           "\n"
           "def load(path: Path) -> str:\n"
           "    with path.open() as handle:\n"
           "        return handle.read()\n")
    findings, inventory, _ = scan(tmp_path)
    assert len(findings) == 1, findings
    assert findings[0].startswith("a.py:5: "), findings[0]
    assert "Path.open()" in findings[0] and "cp1252" in findings[0]
    assert inventory["path_text"] == 1 and inventory["violations"] == 1


def test_a_path_receiver_through_a_same_file_helper_is_in_scope(tmp_path):
    _write(tmp_path, "a.py",
           "from pathlib import Path\n"
           "\n"
           "\n"
           "def project(tmp_path: Path) -> Path:\n"
           "    return tmp_path / 'app'\n"
           "\n"
           "\n"
           "def load(tmp_path: Path) -> None:\n"
           "    proj = project(tmp_path)\n"
           "    with (proj / 'truc.toml').open('a') as handle:\n"
           "        handle.write('x')\n")
    findings, inventory, _ = scan(tmp_path)
    assert len(findings) == 1, findings
    assert findings[0].startswith("a.py:10: "), findings[0]
    assert inventory["path_text"] == 1


def test_binary_modes_and_explicit_encodings_are_accepted(tmp_path):
    _write(tmp_path, "a.py",
           "from pathlib import Path\n"
           "\n"
           "\n"
           "def load(path: Path):\n"
           "    a = open(path, 'rb')\n"
           "    b = open(path, 'w', encoding='utf-8')\n"
           "    c = open(path, 'w', 1, 'utf-8')\n"
           "    d = path.open('rb')\n"
           "    e = path.open('w', encoding='utf-8')\n"
           "    f = path.open('w', -1, 'utf-8')\n"
           "    return a, b, c, d, e, f\n")
    findings, inventory, _ = scan(tmp_path)
    assert findings == [], findings
    assert inventory["builtin_text"] == 0 and inventory["path_text"] == 0
    assert inventory["builtin_binary"] == 1 and inventory["path_binary"] == 1
    assert inventory["builtin_text_enc"] == 2 and inventory["path_text_enc"] == 2


def test_other_attribute_open_apis_are_not_flagged(tmp_path):
    _write(tmp_path, "a.py",
           "import os\n"
           "import tarfile\n"
           "\n"
           "\n"
           "def load(path, wal, opener, WriteAheadLog, Pool, provisions):\n"
           "    os.open(path, os.O_RDONLY)\n"
           "    tarfile.open(path)\n"
           "    WriteAheadLog(path).open()\n"
           "    Pool.open(path)\n"
           "    provisions.open()\n"
           "    wal.open()\n"
           "    opener.open(path)\n")
    findings, inventory, attr_calls = scan(tmp_path)
    assert findings == [], findings
    assert attr_calls == 7
    assert inventory["pathlib"] == 0


def test_a_dynamic_mode_or_hidden_encoding_is_reported(tmp_path):
    _write(tmp_path, "a.py",
           "def load(path, mode, options):\n"
           "    a = open(path, mode)\n"
           "    b = open(path, **options)\n"
           "    return a, b\n")
    findings, inventory, _ = scan(tmp_path)
    assert len(findings) == 2, findings
    assert findings[0].startswith("a.py:2: "), findings[0]
    assert "mode is not a literal" in findings[0]
    assert findings[1].startswith("a.py:3: "), findings[1]
    assert "**kwargs" in findings[1]
    assert inventory["builtin_text"] == 2
