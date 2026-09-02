#!/usr/bin/env python3
"""The L0/L1/L2 import-layering gate (roadmap item 418, step 8).

formal/STATUS.md opens with "Rules of the layering (enforced by imports,
not by hope)". Item 418's MEDIUM findings recorded that nothing actually
enforced them. This does.

The rules, as STATUS.md states them:

* **L0** (`RevL.Syntax`, `RevL.Typing`, `RevL.Semantics`, `RevL.Manifest`,
  `RevL.Boundary`) is architect-owned. An L0 file imports L0 only.
* **L1** (`RevL.Lemmas.*`) is the lemma farm. A farm file imports L0 only
  and never another farm file.
* **L2** (`RevL.Theorems.*`) is one file per guarantee. A theorem file
  imports L0 and L1 and never another theorem file.

Two further checks, because a file outside the build is a file outside the
gate:

* every L1 and L2 module is imported by the root `RevL.lean`, so it is
  built and its theorems reach `CheckAxioms.lean`;
* every module the root imports exists.

Exit 0 when the tree obeys the layering, 1 otherwise, with every violation
named.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORMAL = Path(__file__).resolve().parent.parent

L0 = {
    "RevL.Syntax",
    "RevL.Typing",
    "RevL.Semantics",
    "RevL.Manifest",
    "RevL.Boundary",
}

IMPORT = re.compile(r"^import\s+([A-Za-z0-9_.]+)\s*$")


def module_of(path: Path) -> str:
    return ".".join(path.relative_to(FORMAL).with_suffix("").parts)


def imports_of(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        m = IMPORT.match(line.strip())
        if m:
            out.append(m.group(1))
    return out


def layer_of(module: str) -> str | None:
    if module in L0:
        return "L0"
    if module.startswith("RevL.Lemmas."):
        return "L1"
    if module.startswith("RevL.Theorems."):
        return "L2"
    return None


def main() -> int:
    errors: list[str] = []

    modules = sorted(module_of(p) for p in (FORMAL / "RevL").rglob("*.lean"))

    for module in modules:
        layer = layer_of(module)
        path = FORMAL / (module.replace(".", "/") + ".lean")
        if layer is None:
            errors.append(
                f"{module}: not in any layer. Put an L0 module in "
                f"layering_gate.py's L0 set, or move the file under "
                f"RevL/Lemmas/ (L1) or RevL/Theorems/ (L2)."
            )
            continue
        for imported in imports_of(path):
            ilayer = layer_of(imported)
            if ilayer is None:
                errors.append(f"{module} imports {imported}, which is in no layer")
                continue
            if layer == "L0" and ilayer != "L0":
                errors.append(
                    f"L0 {module} imports {ilayer} {imported}: "
                    f"L0 is architect-owned and imports L0 only"
                )
            elif layer == "L1" and ilayer != "L0":
                errors.append(
                    f"L1 {module} imports {ilayer} {imported}: "
                    f"a lemma farm file imports L0 only, never another farm file"
                )
            elif layer == "L2" and ilayer == "L2":
                errors.append(
                    f"L2 {module} imports L2 {imported}: "
                    f"one guarantee per file, and they never import each other"
                )

    root = FORMAL / "RevL.lean"
    rooted = set(imports_of(root))
    for module in modules:
        if layer_of(module) in ("L1", "L2") and module not in rooted:
            errors.append(
                f"{module} is not imported by RevL.lean, so it is never built "
                f"and its theorems never reach CheckAxioms.lean"
            )
    for imported in sorted(rooted):
        if not (FORMAL / (imported.replace(".", "/") + ".lean")).exists():
            errors.append(f"RevL.lean imports {imported}, which does not exist")

    if errors:
        print("import layering: FAILED")
        for e in errors:
            print(f"  {e}")
        return 1

    counts = {"L0": 0, "L1": 0, "L2": 0}
    for module in modules:
        counts[layer_of(module)] += 1
    print(
        f"import layering: clean, {counts['L0']} L0, {counts['L1']} L1, "
        f"{counts['L2']} L2 modules, no upward or sideways import"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
