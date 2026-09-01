import RevL.Manifest

/-!
Formal oracle — the differential harness's Lean side (formal/STATUS.md,
"differential oracle"). Reads the corpus TSV the exporter wrote (one row
per component: path, name, requires, provides) and emits one verdict row
per file: the G2/G3 model's decision, computed here independently of the
Python reference. A diff against the reference is definitional drift
between the machine-checked model and the spec/extraction.

TSV in:  `<path>\t<name>\t<requires-csv>\t<provides-csv>` per component
TSV out: `V\t<path>\tdisjoint=ok|fail\tclosed=ok|fail` per file
-/

def splitTab (s : String) : List String := s.splitOn "\t"

def splitKeys (s : String) : List String :=
  if s == "" then [] else (s.splitOn ",").filter (fun k => k != "")

structure Row where
  path : String
  name : String
  requires : List String
  provides : List String

def parseRow (line : String) : Option Row :=
  match splitTab line with
  | [path, name, reqs, provs] => some ⟨path, name, splitKeys reqs, splitKeys provs⟩
  | _ => none

/-- G2 (Def. 43): the provision surface has no duplicate key. -/
def disjointOK (providesLists : List (List String)) : Bool :=
  decide (List.Nodup providesLists.flatten)

/-- Requirement closure: every requirement is provided in the file. -/
def closedOK (rows : List Row) : Bool :=
  rows.all fun r =>
    r.requires.all fun k =>
      rows.any fun p => p.provides.contains k

def main (args : List String) : IO UInt32 := do
  match args with
  | [inPath, outPath] =>
    let text ← IO.FS.readFile inPath
    let rows := (text.splitOn "\n").filterMap parseRow
    let paths := (rows.map (·.path)).eraseDups
    let mut out := ""
    for p in paths do
      let fileRows := rows.filter (fun r => r.path == p)
      let dv := if disjointOK (fileRows.map (·.provides)) then "ok" else "fail"
      let cv := if closedOK fileRows then "ok" else "fail"
      out := out ++ s!"V\t{p}\tdisjoint={dv}\tclosed={cv}\n"
    IO.FS.writeFile outPath out
    return 0
  | _ =>
    IO.eprintln "usage: Oracle.lean <corpus.tsv> <verdicts.tsv>"
    return 1
