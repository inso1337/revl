"""One-off generator for reserved_method_names.ir.json. NOT part of the build.

Issue #1512: a provided method named after a TypeScript keyword was renamed at
its definition (`delete` -> `delete_`) and then looked up under the renamed
spelling in the service it provides, so the ts emitter refused 25 of them. The
fix gives the method one spelling at every site, the contract name.

The fixture is every TypeScript keyword the revl frontend admits as an
identifier, plus four names on the append-`_` ladder, as the methods of ONE
service `S`. `P` provides each as `x + 1`; `C` requires `S` and provides one
`Ops.run<i>` per name that calls `s.<name>(x) * 10`, so every name is reached
at the definition, the interface, and a static call site through a required
service. `reserved_method_names.test.ts` calls each both ways, and
`scripts/typecheck-generated.mjs` typechecks the emitted module.

`KEYWORDS` is TypeScript 5.9.3's keyword table (`SyntaxKind.FirstKeyword` ..
`LastKeyword`), pinned here and checked against the installed compiler by the
vitest suite. Run once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_reserved_method_names.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

KEYWORDS = [
    "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "enum", "export", "extends", "false",
    "finally", "for", "function", "if", "import", "in", "instanceof", "new",
    "null", "return", "super", "switch", "this", "throw", "true", "try",
    "typeof", "var", "void", "while", "with", "implements", "interface", "let",
    "package", "private", "protected", "public", "static", "yield", "abstract",
    "accessor", "as", "asserts", "assert", "any", "async", "await", "boolean",
    "constructor", "declare", "get", "infer", "intrinsic", "is", "keyof",
    "module", "namespace", "never", "out", "readonly", "require", "number",
    "object", "satisfies", "set", "string", "symbol", "type", "undefined",
    "unique", "unknown", "using", "from", "global", "bigint", "override", "of",
    "defer",
]

#: TypeScript keywords that are revl keywords too: the frontend refuses them as
#: an identifier, so they never reach an emitter.
FRONTEND_KEYWORDS = [
    "break", "continue", "else", "false", "for", "if", "in", "null", "return",
    "true", "try", "var", "while", "with", "let", "as", "assert", "async",
    "await", "type", "of",
]

#: Names on the append-`_` ladder: `_mangle` shifted these too (`delete_` ->
#: `delete__`), so they were refused the same way.
LADDER = ["class_", "delete_", "let_", "yield__"]

ADMITTED = [name for name in KEYWORDS if name not in FRONTEND_KEYWORDS] + LADDER


def source(names: list) -> str:
    methods = "\n".join(f"  fn {name}(x: Int) -> Int" for name in names)
    provided = "\n".join(f"    fn {name}(x) {{ return x + 1 }}" for name in names)
    ops = "\n".join(f"  fn run{i}(x: Int) -> Int" for i in range(len(names)))
    calls = "\n".join(f"    fn run{i}(x) {{ return s.{name}(x) * 10 }}"
                      for i, name in enumerate(names))
    return (f"service S {{\n{methods}\n}}\n"
            f"component P provides s: S {{\n  provide s {{\n{provided}\n  }}\n}}\n"
            f"service Ops {{\n{ops}\n}}\n"
            f"component C requires s: S provides ops: Ops {{\n"
            f"  provide ops {{\n{calls}\n  }}\n}}\n")


def build() -> dict:
    return compile_source(source(ADMITTED), "reserved_method_names.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "reserved_method_names.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
