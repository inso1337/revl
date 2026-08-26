#!/usr/bin/env bash
# Regenerate the checked-in emitted Go from the source IR fixtures, then gofmt.
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

# --- ir_version 1/2 component scenarios (stc-go runtime) ------------------
python3 "$here/emit.py" "$root/examples/user_cache.ir.json" usercache \
  > "$here/scenarios/emitted/usercache/gen.go"
python3 "$here/emit.py" "$root/backends/typescript/tests/fixtures/tenants.ir.json" tenants \
  > "$here/scenarios/emitted/tenants/gen.go"

# host `Map.new()` iteration surface — keys()/size() (roadmap item 88). Compiled
# from scenarios/memkv.rvl by the frozen frontend (`revl compile`); the host
# `type Map struct` backs Size()/Keys(), and gen_exec_test.go proves both RUN on
# stc-go (keys canonically sorted, size counting).
python3 "$here/emit.py" "$here/scenarios/emitted/memkv/memkv.ir.json" memkv \
  > "$here/scenarios/emitted/memkv/gen.go"

# host `Map.new()` value type — the host Map is generic over V (item 113,
# FR-4). counter.rvl exercises `Map[Str, Int]` (Insert/Get of Int values) and
# tagger.rvl exercises `Map[Str, List[Str]]`; before item 113 the host Map held
# String values only, so both failed `go build` (`cannot use n (int) as string
# value`). Compiled from scenarios/{counter,tagger}.rvl by the frozen frontend;
# gen_exec_test.go proves the value round-trip RUNS on stc-go.
python3 "$here/emit.py" "$here/scenarios/emitted/counter/counter.ir.json" counter \
  > "$here/scenarios/emitted/counter/gen.go"
python3 "$here/emit.py" "$here/scenarios/emitted/tagger/tagger.ir.json" tagger \
  > "$here/scenarios/emitted/tagger/gen.go"

# --- instance-parametric spawn (docs/design-v2-instances.md, phase 1) ------
# spawn.ir.json is compiled from scenarios/spawn.rvl by the frozen frontend
# (`revl compile`); the emitter lowers its `spawn` acquisitions to child-fiber
# plugs on the real stc-go runtime (gen_exec_test.go proves the four DoD
# properties by RUNNING).
python3 "$here/emit.py" "$here/scenarios/emitted/spawn/spawn.ir.json" spawn \
  > "$here/scenarios/emitted/spawn/gen.go"

# --- instance accessor (docs/design-v2-instances.md, "Instance accessor") ---
# accessor.ir.json is compiled from scenarios/accessor.rvl by the frozen
# frontend (`revl compile`); the emitter lowers its `instance-get` reads to
# realm-scoped `stc.Service[..](handle.Ctx(), _key..)` resolutions on the real
# stc-go runtime (gen_exec_test.go proves the positive and negative by RUNNING).
python3 "$here/emit.py" "$here/scenarios/emitted/accessor/accessor.ir.json" accessor \
  > "$here/scenarios/emitted/accessor/gen.go"

# --- timers as revertible schedules (item 57, docs/time-coeffect.md) --------
# timer.ir.json is compiled from scenarios/emitted/timer/timer.rvl by the frozen
# frontend (`revl compile`); the emitter lowers its `every`/`after` timer steps
# to schedule/cancel effects on the clock coeffect (gen_exec_test.go proves
# deterministic firing under RevlClockAdvance and unload-cancels-no-residue by
# RUNNING on stc-go).
python3 "$here/emit.py" "$here/scenarios/emitted/timer/timer.ir.json" timer \
  > "$here/scenarios/emitted/timer/gen.go"

# --- `advance` lifecycle step drives the go Clock (item 102, go half) --------
# advance.ir.json is compiled from scenarios/advance.rvl by the frozen frontend
# (`revl compile`). A `lifecycle test` with `advance <n><unit>` lowers the step
# to RevlClockAdvance(N) (and RevlClockReset() at test start), so a timer's
# firing is an assertable step — before item 102 the go emitter refused
# `advance` outright. The emitted file IS the proof: it is a `*_test.go` whose
# `Test…` funcs RUN the every/after timelines on real stc-go under `go test`.
python3 "$here/emit.py" "$here/scenarios/emitted/advance/advance.ir.json" advance \
  > "$here/scenarios/emitted/advance/gen_advance_test.go"

# --- v3 records/lists/variants across a LIVE provide method (item 139) -------
# records.ir.json is compiled from scenarios/records.rvl by the frozen frontend
# (`revl compile`). A `provide` method that TAKES and RETURNS a record (and
# threads a List of them, and dispatches an ADT `match`) lowers in emit()'s
# live stc-go component world — before item 139 emit() carried no record/ADT
# lowering there ("record is not lowerable in the stc-go component world"),
# even though the same record lowers fine in a top-level fn and on the
# placement runner. The emitted file IS the proof: it is a `*_test.go` whose
# lifecycle `Test…` func RUNS the record round-trip on real stc-go.
python3 "$here/emit.py" "$here/scenarios/emitted/records/records.ir.json" records \
  > "$here/scenarios/emitted/records/gen_records_test.go"

# --- stdlib JSON wire protocol crosses to go (item 140) ---------------------
# jsonwire.ir.json is compiled from scenarios/emitted/jsonwire/jsonwire.rvl by
# the frozen frontend (`revl compile`). The @go bodies of stdlib/json.rvl reach
# `encoding/json` through the `//revl:import encoding/json` hoist directive, and
# revl `Any` erases to Go `any` — before item 140 the go emitter refused the
# module ("no @go body"). The emitted file IS the proof: a `*_test.go` whose
# `Test…` func RUNS json_stringify∘json_parse on a structured document.
python3 "$here/emit.py" "$here/scenarios/emitted/jsonwire/jsonwire.ir.json" jsonwire \
  > "$here/scenarios/emitted/jsonwire/gen_jsonwire_test.go"

# --- witnessed-effects three-entry-kind teardown loop (items 243/247 Slice 2b,
# docs/design/teardown-contract.md) ------------------------------------------
# witnessed_teardown.ir.json is compiled from scenarios/witnessed_teardown.rvl
# by the frozen frontend (`revl compile`). A component carrying a bracket, a
# `witnessed` effect and an `emit ... compensate ...` in the SAME activation
# proves mixed-entry LIFO and the two-phase abort against the real stc-go
# runtime; the file is a `*_test.go` (the document carries a `lifecycle
# test`) whose generated Test RUNS the commit path. exec_test.go (hand-written,
# not regenerated) drives LoadCAbort directly for the abort path, and
# panic_guard_test.go exercises the RevlFrame teardown accumulator directly
# (the goroutine-abandon / panic-guard / concurrency rules).
python3 "$here/emit.py" "$here/scenarios/emitted/witnessed_teardown/witnessed_teardown.ir.json" witnessedteardown \
  > "$here/scenarios/emitted/witnessed_teardown/gen_witnessed_teardown_test.go"

# --- per-tool-call witnessed effect in a provide METHOD (item 318, the H1 gate,
# docs/design/243-witnessed-externs.md) --------------------------------------
# provide_method_witnessed.ir.json is compiled from
# scenarios/provide_method_witnessed.rvl by the frozen frontend (`revl
# compile`). A component whose provide-METHOD does a witnessed fs mutation PER
# TOOL CALL registers each per-call inverse into the component's activation
# frame (`RevlFrame.registerMethodWitnessed`, parked for `commit()` to dispose
# once the commit-vs-abort bit is settled) — the go mirror of the py reference
# tier's `Frame.transactional_method`/`_deferred_transactional`. The emitted
# file is a `*_test.go` (the document carries a `lifecycle test` smoke path);
# exec_test.go (hand-written, not regenerated) drives the real per-tool-call H1
# proof against files on disk — persist on clean unload, revert on abort — the
# go mirror of tests/test_provide_method_witnessed.py.
python3 "$here/emit.py" "$here/scenarios/emitted/provide_method_witnessed/provide_method_witnessed.ir.json" providemethodwitnessed \
  > "$here/scenarios/emitted/provide_method_witnessed/gen_provide_method_witnessed_test.go"

# --- ir_version 3 pure/typed-core fixtures (ordinary Go, no stc runtime) ---
# The v3_tests fixture carries `test` blocks that become real Go tests, so it
# is emitted straight into a *_test.go file. The other two are libraries the
# hand-written *_exec_test.go harnesses call with computed assertions.
fx="$root/backends/typescript/tests/fixtures"
python3 "$here/emit.py" "$fx/v3_tests.ir.json"            tests \
  > "$here/v3/tests/gen_test.go"
python3 "$here/emit.py" "$fx/v3_types_functions.ir.json"  types_functions \
  > "$here/v3/types_functions/gen.go"
python3 "$here/emit.py" "$fx/v3_stdlib.ir.json"           stdlib \
  > "$here/v3/stdlib/gen.go"

if command -v gofmt >/dev/null 2>&1; then
  gofmt -w "$here/scenarios/emitted/usercache/gen.go" \
           "$here/scenarios/emitted/tenants/gen.go" \
           "$here/scenarios/emitted/memkv/gen.go" \
           "$here/scenarios/emitted/counter/gen.go" \
           "$here/scenarios/emitted/tagger/gen.go" \
           "$here/scenarios/emitted/spawn/gen.go" \
           "$here/scenarios/emitted/accessor/gen.go" \
           "$here/scenarios/emitted/timer/gen.go" \
           "$here/scenarios/emitted/advance/gen_advance_test.go" \
           "$here/scenarios/emitted/records/gen_records_test.go" \
           "$here/scenarios/emitted/jsonwire/gen_jsonwire_test.go" \
           "$here/scenarios/emitted/witnessed_teardown/gen_witnessed_teardown_test.go" \
           "$here/scenarios/emitted/provide_method_witnessed/gen_provide_method_witnessed_test.go" \
           "$here/v3/tests/gen_test.go" \
           "$here/v3/types_functions/gen.go" \
           "$here/v3/stdlib/gen.go"
fi
echo "regenerated emitted Go under $here/scenarios/emitted/ and $here/v3/"
