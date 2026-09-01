import RevL.Manifest

/-!
Formal oracle — the differential harness's Lean side (formal/STATUS.md,
"differential oracle"). Reads the corpus TSV the exporter wrote and emits
verdicts computed from the machine-checked models (`RevL.Manifest` for
G2/G3, the G4-shaped no-raw judgment over statement facts), coded
independently of the Python reference. A diff against the reference is
definitional drift between model and spec/extraction.

Rows in (tab-separated, one fact per line):
  M <file> <comp> <requires-csv> <provides-csv>   component manifest
  E <file> <service> <method>                     declared emission method
  T <file> <comp> <kind>                          statement class:
                                                  effect | emit | pure
  U <file> <comp> <ctx> <local> <service> <method> call fact: a call whose
                                                  receiver is a declared
                                                  require binding, with its
                                                  marker context
                                                  (plain | emit); only
                                                  `emit` marks a crossing

Rows out:
  V <file> <disjoint=ok|fail> <closed=ok|fail>    G2/G3 manifest verdict
  G <file> <comp> <g4=ok|fail>                    G4-shaped body verdict:
                                                  no unmarked call to a
                                                  declared emission method
-/

def splitTab (s : String) : List String := s.splitOn "\t"

def splitKeys (s : String) : List String :=
  if s == "" then [] else (s.splitOn ",").filter (fun k => k != "")

structure MRow where
  path : String
  name : String
  requires : List String
  provides : List String

structure URow where
  path : String
  comp : String
  ctx : String
  bind : String
  svc : String
  meth : String

def parseM (f : List String) : Option MRow :=
  match f with
  | ["M", path, name, reqs, provs] => some ⟨path, name, splitKeys reqs, splitKeys provs⟩
  | _ => none

def parseU (f : List String) : Option URow :=
  match f with
  | ["U", path, comp, ctx, bind, svc, meth] => some ⟨path, comp, ctx, bind, svc, meth⟩
  | _ => none

/-- G2 (Def. 43): the provision surface has no duplicate key. -/
def disjointOK (providesLists : List (List String)) : Bool :=
  decide (List.Nodup providesLists.flatten)

/-- Requirement closure: every requirement is provided in the file. -/
def closedOK (mrows : List MRow) : Bool :=
  mrows.all fun r =>
    r.requires.all fun k =>
      mrows.any fun q => q.provides.contains k

/-- G4-shaped body verdict: marker presence must equal the interface's
declaration — every call that reaches a declared emission method must be
marked (`ctx == "emit"`), and an `"emit"`-marked call to a method that is
not a declared emission is itself refused. `(service, method)` membership
is the interface's mutation classification; `effect`-form and `emit`-form
statements are plain/emit call contexts carried in the U row. -/
def g4OK (ems : List (String × String)) (calls : List URow) : Bool :=
  !calls.any fun u =>
    let em := ems.any fun e => e.1 == u.svc && e.2 == u.meth
    (u.ctx == "emit") != em

def main (args : List String) : IO UInt32 := do
  match args with
  | [inPath, outPath] =>
    let text ← IO.FS.readFile inPath
    let fields := (text.splitOn "\n").filter (fun l => l != "")
      |>.map (·.splitOn "\t")
    let mrows := fields.filterMap parseM
    let urows := fields.filterMap parseU
    let erows := fields.filterMap (fun f =>
      match f with
      | ["E", file, svc, meth] => some (file, svc, meth)
      | _ => none)
    let paths := (mrows.map (·.path)).eraseDups
    let mut out := ""
    for p in paths do
      let fm := mrows.filter (fun r => r.path == p)
      let ems := (erows.filter (fun e => e.1 == p)).map (fun e => (e.2.1, e.2.2))
      let dv := if disjointOK (fm.map (·.provides)) then "ok" else "fail"
      let cv := if closedOK fm then "ok" else "fail"
      out := out ++ s!"V\t{p}\tdisjoint={dv}\tclosed={cv}\n"
      let calls := urows.filter (fun r => r.path == p)
      for cn in fm.map (·.name) do
        let gv := if g4OK ems (calls.filter (fun r => r.comp == cn)) then "ok" else "fail"
        out := out ++ s!"G\t{p}\t{cn}\tg4={gv}\n"
    IO.FS.writeFile outPath out
    return 0
  | _ =>
    IO.eprintln "usage: Oracle.lean <corpus.tsv> <verdicts.tsv>"
    return 1
