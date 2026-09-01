"""Differential harness: the formal manifest model vs the extracted corpus.

Pipeline (formal/STATUS.md, "differential oracle"):

1. parse every .rvl in the corpus with revl's real parser (`revl.parser`)
   and export one TSV row per component: path, name, requires, provides;
2. compute the reference verdict per file in plain Python set logic — the
   G2/G3 spec the linker enforces (provision disjointness, requirement
   closure);
3. run the Lean oracle (`formal/harness/Oracle.lean`) over the same TSV —
   the same spec against the machine-checked model, coded independently;
4. diff. A mismatch is definitional drift between the model and the
   spec/extraction — the mechanism that keeps parallel edits to the formal
   model honest: drift surfaces here, not as a silent fork.

Verdict domain (v1): G2 provision disjointness + requirement closure per
file. Parse failures skip LOUDLY (counted, never silently dropped).
Statement-level verdicts (G4/G6-shaped) are TODO — they need statement
classification in the export, not just component headers.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FORMAL = Path(__file__).resolve().parents[1]
CORPUS_DIRS = ("examples", "tck", "tests")

sys.path.insert(0, str(REPO / "src"))

from revl.errors import RevlError
from revl.parser import Parser


def corpus_files() -> list[Path]:
    out: list[Path] = []
    for d in CORPUS_DIRS:
        root = REPO / d
        if root.is_dir():
            out.extend(sorted(root.rglob("*.rvl")))
    return out


def export() -> tuple[list[str], dict[str, tuple[bool, bool]], dict[str, int]]:
    """Parse the corpus; return (tsv rows, reference verdicts, census)."""
    tsv: list[str] = []
    reference: dict[str, tuple[bool, bool]] = {}
    files = comps = skipped = 0
    for path in corpus_files():
        files += 1
        try:
            prog = Parser(path.read_text(encoding="utf-8"), str(path)).parse()
        except RevlError:
            skipped += 1
            continue
        rel = str(path.relative_to(REPO))
        file_comps: list[tuple[str, list[str], list[str]]] = []
        for c in prog.components:
            requires = [local for local, _svc, _line in c.requires]
            provides = [key for key, _svc, _line in c.provides]
            file_comps.append((c.name, requires, provides))
            tsv.append("\t".join([rel, c.name, ",".join(requires), ",".join(provides)]))
        comps += len(file_comps)
        if not file_comps:
            continue  # services-only / extern-only file: no manifest to judge
        all_provides = [k for _n, _rs, ps in file_comps for k in ps]
        provided = set(all_provides)
        disjoint = len(all_provides) == len(provided)
        closed = all(k in provided for _n, rs, _ps in file_comps for k in rs)
        reference[rel] = (disjoint, closed)
    return tsv, reference, {
        "files": files, "components": comps, "parse_errors": skipped,
    }


def run_oracle(tsv_path: Path, out_path: Path) -> str | None:
    """Run the Lean oracle over the corpus TSV; None if lake is absent."""
    if shutil.which("lake") is None:
        print("SKIP (loud): lake not on PATH — formal verdicts NOT computed")
        return None
    proc = subprocess.run(
        ["lake", "env", "lean", "--run", str(FORMAL / "harness" / "Oracle.lean"),
         str(tsv_path), str(out_path)],
        cwd=FORMAL, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit("differential oracle: Lean oracle failed")
    return out_path.read_text(encoding="utf-8")


def parse_verdicts(text: str) -> dict[str, tuple[bool, bool]]:
    verdicts: dict[str, tuple[bool, bool]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] != "V":
            raise SystemExit(f"differential oracle: malformed verdict row {line!r}")
        verdicts[parts[1]] = (
            parts[2].split("=", 1)[1] == "ok",
            parts[3].split("=", 1)[1] == "ok",
        )
    return verdicts


def main() -> int:
    tsv, reference, census = export()
    print(
        f"corpus census: {census['files']} .rvl files, "
        f"{census['components']} components, "
        f"{census['parse_errors']} parse-error skip(s)"
    )
    if not tsv:
        print("differential oracle: no components extracted — nothing to diff")
        return 0

    out_dir = FORMAL / "harness" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = out_dir / "corpus.tsv"
    tsv_path.write_text("\n".join(tsv) + "\n", encoding="utf-8")

    formal_text = run_oracle(tsv_path, out_dir / "formal_verdicts.tsv")
    if formal_text is None:
        return 0
    formal = parse_verdicts(formal_text)

    missing = sorted(set(reference) - set(formal))
    extra = sorted(set(formal) - set(reference))
    mismatch = sorted(
        f for f in set(reference) & set(formal) if reference[f] != formal[f]
    )
    agree = len(set(reference) & set(formal)) - len(mismatch)
    print(
        f"differential oracle: {len(reference)} files compared — "
        f"{agree} agree, {len(mismatch)} mismatch(es), "
        f"{len(missing)} missing / {len(extra)} unexpected formal verdict(s)"
    )
    for f in mismatch:
        print(f"  MISMATCH {f}: reference={reference[f]} formal={formal[f]}")
    for f in missing:
        print(f"  MISSING formal verdict for {f}")
    for f in extra:
        print(f"  UNEXPECTED formal verdict for {f}")
    return 1 if (mismatch or missing or extra) else 0


if __name__ == "__main__":
    sys.exit(main())
