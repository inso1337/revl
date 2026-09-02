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
for entry in $SEAMS; do
  [ -n "$entry" ] || continue
  label="${entry%%:*}"
  toml="${entry#*:}"
  echo "=== placement smoke: ${label} (${toml}) ==="
  log="$(mktemp)"
  "$PY" -m revl run "$APP" --placement "$toml" --once < /dev/null 2>&1 | tee "$log"
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
  rm -f "$log"
done

if [ "$fail" -ne 0 ]; then
  echo "placement seam smoke FAILED"
  exit 1
fi
echo "placement seam smoke OK - all seams crossed and tore down with no residue"
