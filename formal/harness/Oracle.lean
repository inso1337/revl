import RevL.Manifest

/-!
Formal oracle — the differential harness's Lean side (formal/STATUS.md,
"differential oracle"). Reads the corpus TSV the exporter wrote and emits
verdicts computed from the machine-checked models (`RevL.Manifest` for
G2/G3, the G4-family judgments over the extracted facts), coded
independently of the Python reference. A diff against the reference is
definitional drift between model and spec/extraction.

Fact rows in (tab-separated, one fact per line):
  M <file> <comp> <reqs-csv> <provs-csv>     component manifest
  R <file> <comp> <local> <svc>              require binding -> service
  B <file> <svc> <meth> <plain|any|scoped>   service-method emission bound
  Q <file> <svc> <meth> <entry>              a scoped bound's declared entry
  C <file> <comp> <key> <svc>                provide key -> service
  K <file> <comp> <local> <cap>              require-held capability
  A <file> <comp> <cap>                      activation emit-step surface
  F <file> <comp> <key> <svc> <meth> <cap>   provide-method emission reach
  S <file> <comp> <child>                    activation spawn edge
  H <file> <comp> <var> <child>              spawn handle var
  U <file> <comp> <ctx> <root> <svc> <meth>  call fact + marker context
  T <file> <comp> <kind>                     statement class (census)

Capabilities are canonical strings `tok` or `tok(n=v,..)`, where a value
is `"string"`, `/a/b` (a path), or an int (a ceiling). `covers` is the
cap_order fold: same token (unless `*` is top), per-param value <=
(path: component-prefix; int: <=; discrete: equality).

Verdict rows out:
  V <file> <disjoint=ok|fail> <closed=ok|fail>     G2/G3 manifest verdict
  G <file> <comp> <g4=ok|fail>                     marker rule (incl. handle)
  P <file> <comp> <key> <svc> <meth> <bound=ok|fail>
                                                   provide-method bound
  W <file> <comp> <child> <atten=ok|fail>          spawn attenuation
-/

namespace RevLOracle

open RevL.Manifest

def splitKeys (s : String) : List String :=
  if s == "" then [] else (s.splitOn ",").filter (fun k => k != "")

def union (x y : List String) : List String := (x ++ y).eraseDups

-- ---------------------------------------------------------------- caps

/-- Canonical capability value: a string, a path (component list), or an
integer (a ceiling). -/
inductive CapVal where
  | str : String → CapVal
  | path : List String → CapVal
  | num : Int → CapVal
  deriving BEq, Repr

def dropLast (s : String) : String := (s.take (s.length - 1)).toString

/-- Character index of `c` in `s`, or `s.length` when absent - a `Nat`-based
replacement for `String.find?` (whose result is a `String.Pos` here), so it
composes with the `Nat`-indexed `String.take`/`String.drop`. -/
def findCharIndex (c : Char) (s : String) : Nat :=
  let rec go (i : Nat) (rest : List Char) : Nat :=
    match rest with
    | [] => s.length
    | x :: xs => if x == c then i else go (i + 1) xs
  go 0 s.toList

def parseVal (raw : String) : CapVal :=
  if raw.startsWith "\"" then CapVal.str (dropLast ((raw.drop 1).toString))
  else if raw.startsWith "/" then
    CapVal.path ((raw.splitOn "/").filter (fun p => p != ""))
  else match raw.toInt? with
    | some n => CapVal.num n
    | none => CapVal.str raw

/-- Parse one `name=value` chunk of a canonical cap's parameter list. -/
def paramOf (chunk : String) : Option (String × CapVal) :=
  let i := findCharIndex '=' chunk
  if i < chunk.length then
    let name := ((chunk.take i).toString).trimAscii.toString
    let value := ((chunk.drop (i + 1)).toString).trimAscii.toString
    some (name, parseVal value)
  else none

def parseParams (inner : String) : List (String × CapVal) :=
  ((inner.splitOn ",").map (fun c => c.trimAscii.toString)).filterMap (fun c =>
    if c == "" then none else paramOf c)

/-- `(token, params)` of a canonical capability string. -/
def parseCap (s : String) : String × List (String × CapVal) :=
  let i := findCharIndex '(' s
  if i < s.length then
    let tok := (s.take i).toString
    let inner := dropLast ((s.drop (i + 1)).toString)
    (tok, parseParams inner)
  else (s, [])

def valLEQ (narrow wide : CapVal) : Bool :=
  match narrow, wide with
  | CapVal.str a, CapVal.str b => a == b
  | CapVal.path a, CapVal.path b => a.take b.length == b
  | CapVal.num a, CapVal.num b => a <= b
  | _, _ => false

/-- `held` covers `reach` iff same token (unless `*` is top) and reach
narrows every parameter held binds — cap_order.covers on the canonical
string form. -/
def capCovers (held reach : String) : Bool :=
  let (th, ph) := parseCap held
  let (tr, pr) := parseCap reach
  if th == "*" then tr == "*"
  else if tr == "*" then false
  else if th != tr then false
  else ph.all fun (k, av) =>
    match pr.find? (fun (k2, _) => k2 == k) with
    | none => false
    | some (_, bv) => valLEQ bv av

def capToken (s : String) : String :=
  let i := findCharIndex '(' s
  if i < s.length then (s.take i).toString else s

-- ---------------------------------------------------------------- rows

structure MRow where
  path : String
  name : String
  requires : List String
  provides : List String

structure URow where
  path : String
  comp : String
  ctx : String
  root : String
  svc : String
  meth : String

structure BRow where
  path : String
  svc : String
  meth : String
  mode : String

structure QRow where
  path : String
  svc : String
  meth : String
  entry : String

structure ARow where
  path : String
  comp : String
  cap : String

structure FRow where
  path : String
  comp : String
  key : String
  svc : String
  meth : String
  cap : String

structure KRow where
  path : String
  comp : String
  bindn : String
  cap : String

structure SRow where
  path : String
  parent : String
  child : String

def parseM (f : List String) : Option MRow :=
  match f with
  | ["M", path, name, reqs, provs] => some ⟨path, name, splitKeys reqs, splitKeys provs⟩
  | _ => none

def parseU (f : List String) : Option URow :=
  match f with
  | ["U", path, comp, ctx, root, svc, meth] => some ⟨path, comp, ctx, root, svc, meth⟩
  | _ => none

def parseB (f : List String) : Option BRow :=
  match f with
  | ["B", path, svc, meth, mode] => some ⟨path, svc, meth, mode⟩
  | _ => none

def parseQ (f : List String) : Option QRow :=
  match f with
  | ["Q", path, svc, meth, entry] => some ⟨path, svc, meth, entry⟩
  | _ => none

def parseA (f : List String) : Option ARow :=
  match f with
  | ["A", path, comp, cap] => some ⟨path, comp, cap⟩
  | _ => none

def parseF (f : List String) : Option FRow :=
  match f with
  | ["F", path, comp, key, svc, meth, cap] => some ⟨path, comp, key, svc, meth, cap⟩
  | _ => none

def parseK (f : List String) : Option KRow :=
  match f with
  | ["K", path, comp, bind, cap] => some ⟨path, comp, bind, cap⟩
  | _ => none

def parseS (f : List String) : Option SRow :=
  match f with
  | ["S", path, parent, child] => some ⟨path, parent, child⟩
  | _ => none

-- ------------------------------------------------------------ verdicts

def disjointOK (providesLists : List (List String)) : Bool :=
  decide (List.Nodup providesLists.flatten)

def closedOK (mrows : List MRow) : Bool :=
  mrows.all fun r =>
    r.requires.all fun k =>
      mrows.any fun q => q.provides.contains k

/-- Marker rule (G4-shaped): marker presence must equal the interface's
declaration — every call to a declared emission method must be `emit`
-marked, and an `emit`-marked call to a non-emission method is refused.
Receivers include spawn handles (the exporter resolves them). -/
def g4OK (ems : List (String × String)) (calls : List URow) : Bool :=
  !calls.any fun u =>
    let em := ems.any fun e => e.1 == u.svc && e.2 == u.meth
    (u.ctx == "emit") != em

/-- Component -> capability set, as an association list. -/
abbrev CapMap := List (String × List String)

def lookupCaps (m : CapMap) (k : String) : List String :=
  match m.find? (·.1 == k) with
  | none => []
  | some (_, cs) => cs

/-- Insert-or-union. An ABSENT key is ADDED, not dropped: a component with
no reach of its own still holds whatever its `requires` bindings grant it,
and a spawner with no emissions of its own is still a closure node. This is
the reference's `setdefault(k, set()).update(caps)`; losing the absent case
is what made a reach-less spawner look like it held nothing at all. -/
def upsertCaps (m : CapMap) (k : String) (caps : List String) : CapMap :=
  if m.any (·.1 == k) then
    m.map (fun (n, cs) => if n == k then (n, union cs caps) else (n, cs))
  else m ++ [(k, caps.eraseDups)]

/-- Group `(comp, cap)` pairs by component. -/
def groupCaps (pairs : List (String × String)) : CapMap :=
  pairs.foldl (fun acc (k, c) => upsertCaps acc k [c]) []

/-- One closure step over the spawn edges: every parent absorbs its
children's caps. Monotone and edge-order independent at the fixed point, so
`edges.length + 1` passes reach what the reference's `while changed` loop
reaches. -/
def oneStep (edges : List (String × String)) (closed : CapMap) : CapMap :=
  edges.foldl (fun acc (p, c) => upsertCaps acc p (lookupCaps acc c)) closed

def closeN (n : Nat) (edges : List (String × String)) (closed : CapMap) : CapMap :=
  match n with
  | 0 => closed
  | n + 1 => closeN n edges (oneStep edges closed)

/-- The spawn-attenuation verdict per edge: the child's transitively closed
reach must be covered by the spawner's held capabilities. -/
def attenOK (closed held : CapMap) (parent child : String) : Bool :=
  let childCaps := lookupCaps closed child
  let heldCaps := lookupCaps held parent
  childCaps.all fun c => heldCaps.any fun h => capCovers h c

/-- Provide-method bound: the reached emission tokens must be within the
declared bound (plain => none; any => free; scoped => the declared
entries). -/
def methodBoundOK (bounds : List (String × String × String × List String))
                   (svc meth : String) (caps : List String) : Bool :=
  let b := bounds.find? (fun x => x.1 == svc && x.2.1 == meth)
  let mode := match b with | some x => x.2.2.1 | none => "plain"
  let entries := match b with | some x => x.2.2.2 | none => []
  if mode == "any" then true
  else if mode == "scoped" then
    (caps.map capToken).all fun t => entries.contains t
  else caps.isEmpty

-- ---------------------------------------------------------------- main

def main (args : List String) : IO UInt32 := do
  match args with
  | [inPath, outPath] =>
    let text ← IO.FS.readFile inPath
    let fields := (text.splitOn "\n").filter (fun l => l != "")
      |>.map (·.splitOn "\t")
    let mrows := fields.filterMap parseM
    let urows := fields.filterMap parseU
    let brows := fields.filterMap parseB
    let qrows := fields.filterMap parseQ
    let arows := fields.filterMap parseA
    let frows := fields.filterMap parseF
    let krows := fields.filterMap parseK
    let srows := fields.filterMap parseS
    let paths := (mrows.map (·.path)).eraseDups
    let mut out := ""
    for p in paths do
      let fm := mrows.filter (fun r => r.path == p)
      let ub := brows.filter (fun r => r.path == p)
      let uq := qrows.filter (fun r => r.path == p)
      let ua := arows.filter (fun r => r.path == p)
      let uf := frows.filter (fun r => r.path == p)
      let uk := krows.filter (fun r => r.path == p)
      let us := srows.filter (fun r => r.path == p)
      let uu := urows.filter (fun r => r.path == p)
      let ems : List (String × String) :=
        (ub.filter (fun b => b.mode != "plain")).map (fun b => (b.svc, b.meth))
      let bounds : List (String × String × String × List String) :=
        ub.map fun b =>
          (b.svc, b.meth, b.mode,
           (uq.filter (fun q => q.svc == b.svc && q.meth == b.meth)).map (·.entry))
      let dv := if disjointOK (fm.map (·.provides)) then "ok" else "fail"
      let cv := if closedOK fm then "ok" else "fail"
      out := out ++ s!"V\t{p}\tdisjoint={dv}\tclosed={cv}\n"
      let aPairs := ua.map (fun r => (r.comp, r.cap))
      let fPairs := uf.map (fun r => (r.comp, r.cap))
      let owns := groupCaps (aPairs ++ fPairs)
      -- Held = own reach + every capability the `requires` bindings grant.
      let held := uk.foldl (fun acc r => upsertCaps acc r.comp [r.cap]) owns
      let edges := (us.map (fun r => (r.parent, r.child))).eraseDups
      let closed := closeN (edges.length + 1) edges owns
      -- G verdicts (marker rule) per component
      for cn in fm.map (·.name) do
        let gv := if g4OK ems (uu.filter (fun r => r.comp == cn)) then "ok" else "fail"
        out := out ++ s!"G\t{p}\t{cn}\tg4={gv}\n"
      -- P verdicts (provide-method bound) per method reach group
      let fkeys := (uf.map (fun r => (r.comp, r.key, r.svc, r.meth))).eraseDups
      for k in fkeys do
        let caps := (uf.filter (fun r => r.comp == k.1 && r.key == k.2.1
                                && r.svc == k.2.2.1 && r.meth == k.2.2.2))
                    |>.map (·.cap) |>.eraseDups
        let ok := methodBoundOK bounds k.2.2.1 k.2.2.2 caps
        let pv := if ok then "ok" else "fail"
        out := out ++ s!"P\t{p}\t{k.1}\t{k.2.1}\t{k.2.2.1}\t{k.2.2.2}\tbound={pv}\n"
      -- W verdicts (spawn attenuation) per edge
      for e in edges do
        let av := if attenOK closed held e.1 e.2 then "ok" else "fail"
        out := out ++ s!"W\t{p}\t{e.1}\t{e.2}\tatten={av}\n"
    IO.FS.writeFile outPath out
    return 0
  | _ =>
    IO.eprintln "usage: Oracle.lean <corpus.tsv> <verdicts.tsv>"
    return 1

end RevLOracle

-- `lean --run` needs a root-level `main`.
def main (args : List String) : IO UInt32 :=
  RevLOracle.main args
