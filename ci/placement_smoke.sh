#!/usr/bin/env bash
# Placement seam smoke for CI (roadmap item 25 - "Placement in CI").
#
# `revl run --placement <manifest> --once` boots examples/user_cache.rvl split
# across two processes and tears it down. UserCache (the consumer) runs on a
# NON-py tier and proxies the `db` seam to PgDatabase (the py provider) over a
# Unix socket, so cache.put's `emit db.execute(...)` crosses the process
# boundary. That py<->other-tier bridge is what roadmap item 23's `revl swap`
# live-migrates over; before this job it was exercised only where a dev
# happened to have every toolchain installed, and docs/guide-humans.md flags
# the running composition as "demonstrated, not gated". This job gates it.
#
# Why grep the trace as well as check the exit code: `--once`'s own exit code
# fails ONLY when a process does not come UP. A probe that errors crossing the
# seam (`probe ... ERROR ...`) or a teardown that leaves state behind
# (`residue RESIDUE LEFT ...`) is printed but does NOT set the exit code
# (src/revl/placement.py, src/revl/_process_runner.py). So this script checks
# the exit code, scans the captured trace for those failure markers, AND
# requires the provider's positive `residue no residue` proof to appear -
# a silent teardown that never printed a proof is treated as a failure.
#
# Seams covered here (all placement runtimes - py/node/rust/java/go - are
# installed in the conformance job that calls this script):
#   py<->py     cordis-py    both ends. Not a tier crossing, so it looks
#                            redundant next to the four below - it is not. Every
#                            other entry places py as the PROVIDER, and item 337
#                            Seam 2 (the consumer re-admitting its provider
#                            before it wires the proxy) runs only in a PY
#                            CONSUMER. Until this entry existed no job executed
#                            that seam at all, and a py consumer that refused
#                            every legitimate provider shipped green: the proxy
#                            went unwired, both probes failed with "'cache' has
#                            no method ...", and `--once` still exited 0. This
#                            is the py-consumer coverage; the probe-ERROR grep
#                            below is what makes it bite.
#   py<->node   cordis-ts    reactive
#   py<->rust   cordis-rs    reactive
#   py<->go     cordis-go    reactive
#   py<->java   cordis4j     reactive when REVL_CORDIS4J_CLASSES points at a
#                            compiled cordis4j-core + a JDK 21 is found; else
#                            the in-repo stub runtime, which still CROSSES the
#                            socket bridge (it just does not withdraw). Either
#                            way the seam this smoke proves - the crossing and a
#                            no-residue teardown - is exercised.
#
# Plus one entry that is not a new SEAM but a new PAYLOAD (issue #295):
#   ts-host-body  py<->node, running examples/ts_host_body.rvl instead of the
#                 default app, so that a verbatim `@ts` extern body executes on
#                 the node process. The five entries above share
#                 examples/user_cache.rvl, which has no `@ts` in it at all, so
#                 no hand-written TypeScript had ever run on a real node
#                 process in CI. See the block above that entry, below.
#
# Not covered: there is no py<->wasm seam - wasm is not a placement backend
#   (KNOWN_BACKENDS in src/revl/placement.py is py/node/rust/java/go), so it has
#   no placement runner to boot.
#
# Override REVL_SMOKE_SEAMS (whitespace-separated `label:manifest` entries) to
# scope the set - e.g. on a machine missing a JRE - and REVL_PY to pick the
# interpreter that carries the cordis-py runtime.
set -u

PY="${REVL_PY:-backends/python/.venv/bin/python}"
APP="${REVL_SMOKE_APP:-examples/user_cache.rvl}"

DEFAULT_SEAMS="
py-py:examples/placement/user_cache.toml
py-node:examples/placement/user_cache_pynode.toml
py-rust:examples/placement/user_cache_pyrust.toml
py-go:examples/placement/user_cache_pygo.toml
py-java:examples/placement/user_cache_pyjava.toml
"
SEAMS="${REVL_SMOKE_SEAMS:-$DEFAULT_SEAMS}"

fail=0

# run_seam <label> <manifest> <app> [required-trace-pattern]
#
# The four checks below are the script header's argument, in code: `--once`'s
# exit code fails ONLY when a process does not come up, so a probe that errored
# and a teardown that left state behind are both green by exit code alone.
# A fifth, optional check requires a positive marker in the trace - used by the
# ts-host-body case, where "the process came up" is precisely the answer that
# would reproduce the defect being closed.
run_seam() {
  label="$1"; toml="$2"; app="$3"; required="${4:-}"
  echo "=== placement smoke: ${label} (${app} + ${toml}) ==="
  log="$(mktemp)"
  "$PY" -m revl run "$app" --placement "$toml" --once < /dev/null 2>&1 | tee "$log"
  rc="${PIPESTATUS[0]}"

  if [ "$rc" -ne 0 ]; then
    echo "::error::placement smoke ${label}: revl run --placement --once exited ${rc} (a process did not come up across the seam)"
    fail=1
  fi
  if grep -q "RESIDUE LEFT" "$log"; then
    echo "::error::placement smoke ${label}: teardown left residue across the seam"
    fail=1
  fi
  if grep -Eq "probe .*ERROR" "$log"; then
    echo "::error::placement smoke ${label}: a probe errored crossing the seam"
    fail=1
  fi
  if ! grep -q "residue no residue" "$log"; then
    echo "::error::placement smoke ${label}: no 'residue no residue' proof in the trace - the provider teardown did not complete"
    fail=1
  fi
  if [ -n "$required" ] && ! grep -q "$required" "$log"; then
    echo "::error::placement smoke ${label}: the trace does not contain '${required}' - the probe did not report the answer this case exists to check"
    fail=1
  fi
  rm -f "$log"
}

for entry in $SEAMS; do
  [ -n "$entry" ] || continue
  run_seam "${entry%%:*}" "${entry#*:}" "$APP"
done

# --- the ts host body, on the shipping runtime (issue #295) ------------------
#
# Everything above runs examples/user_cache.rvl, which contains no `@ts` at all
# (`grep -c "@ts" examples/user_cache.rvl` is 0). So until this entry, NO
# hand-written TypeScript had ever executed on a real `node` process in CI, and
# the ts tier's only executor was vitest - which supplies a CommonJS module
# scope (`require`, `module`, `exports`, `__dirname`, `__filename`), vite's
# module resolution and full TypeScript, none of which the emitted module gets
# from node. A green `--backend ts` suite therefore meant "this runs under
# vitest", and nothing could report the difference (docs/ts-runtime-contract.md).
#
# examples/ts_host_body.rvl is user_cache.rvl plus one verbatim `@ts` extern
# body and one method that calls it, on the NODE-placed component. The probe
# `cache.module_scope()` executes that body on the node process and prints what
# module scope it was evaluated in; the marker below requires the answer to be
# ESM. Asserting the ANSWER and not the exit code is deliberate: `--once`
# exits 0 even when every probe errors, and a case that only proved the process
# came up would reproduce this issue's own failure in a new place.
#
# Set REVL_SMOKE_TS_BODY empty to scope it out on a machine with no node.
TS_BODY="${REVL_SMOKE_TS_BODY-ts-host-body:examples/placement/ts_host_body_pynode.toml:examples/ts_host_body.rvl}"
if [ -n "$TS_BODY" ]; then
  ts_label="${TS_BODY%%:*}"; ts_rest="${TS_BODY#*:}"
  run_seam "$ts_label" "${ts_rest%%:*}" "${ts_rest#*:}" "TS-MODULE-SCOPE ESM"
fi

if [ "$fail" -ne 0 ]; then
  echo "placement seam smoke FAILED"
  exit 1
fi
echo "placement seam smoke OK - all seams crossed and tore down with no residue, and the ts host body ran in a plain-node ESM scope"
