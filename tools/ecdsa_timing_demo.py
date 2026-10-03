"""A DEMONSTRATION, not a proof: signing time against the nonce's bit pattern
(issue #1460).

`revl.tee_quote.ecdsa_sign_pure` multiplies by the RFC 6979 nonce `k` with
double-and-add, one point addition per set bit. So its running time should
track the Hamming weight of `k`. The `cryptography` backend should not.

Both backends are deterministic and byte-compatible, so the nonce of every
signature is known in advance: this script derives `k` for a few thousand
messages under one key, keeps the messages whose `k` has the fewest and the most
set bits, and times BOTH backends signing exactly those messages, interleaved so
machine drift lands on both groups alike.

What it shows: a median gap between the two groups on the pure path, and none
worth the name on the backend. What it does not show: that an attacker can
recover a key (that is the published lattice literature, not this file), that
the backend is constant time in any formal sense, or anything at all on a loaded
machine, where the noise can swamp the gap. It is deliberately NOT a test and CI
does not run it: a timing assertion on a shared runner is a flaky test.

    python tools/ecdsa_timing_demo.py            # about 20 s
    python tools/ecdsa_timing_demo.py --quick    # about 5 s, noisier
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from revl import _ecdsa_backend
from revl.tee_quote import (
    CURVE_P256,
    _rfc6979_k,
    ecdsa_sign_pure,
    private_key_from_seed,
)


def _median_seconds(sign, key: int, message: bytes, reps: int) -> float:
    samples = []
    for _ in range(reps):
        start = time.perf_counter()
        sign(CURVE_P256, key, message)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _groups(key: int, candidates: int, size: int):
    scored = []
    for index in range(candidates):
        message = b"revl timing demo %d" % index
        nonce = _rfc6979_k(CURVE_P256, key, CURVE_P256.hash(message))
        scored.append((bin(nonce).count("1"), message))
    scored.sort()
    return scored[:size], scored[-size:]


def _time_groups(sign, key: int, low, high, reps: int):
    low_times, high_times = [], []
    for (_, low_msg), (_, high_msg) in zip(low, high):
        low_times.append(_median_seconds(sign, key, low_msg, reps))
        high_times.append(_median_seconds(sign, key, high_msg, reps))
    return statistics.median(low_times), statistics.median(high_times)


def _report(name: str, low_s: float, high_s: float) -> None:
    gap = (high_s - low_s) / low_s * 100
    print(f"  {name:<13} low-weight k {low_s * 1e6:9.1f} us   "
          f"high-weight k {high_s * 1e6:9.1f} us   gap {gap:+6.1f}%")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true",
                        help="fewer messages and repetitions; noisier")
    args = parser.parse_args(argv)
    candidates, size, pure_reps, backend_reps = (
        (1000, 12, 3, 50) if args.quick else (4000, 30, 5, 200))

    key = private_key_from_seed(CURVE_P256, b"revl-1460-timing-demo")
    low, high = _groups(key, candidates, size)
    print("DEMONSTRATION ONLY: timings on this machine, not a security proof.")
    print(f"P-256, one key, {size} messages per group chosen from {candidates} "
          f"by the Hamming weight of their RFC 6979 nonce:")
    print(f"  low-weight k: {low[0][0]}..{low[-1][0]} set bits; "
          f"high-weight k: {high[0][0]}..{high[-1][0]} set bits")

    low_s, high_s = _time_groups(ecdsa_sign_pure, key, low, high, pure_reps)
    _report("pure", low_s, high_s)

    found = _ecdsa_backend.probe()
    if not found.available:
        print(f"  cryptography  not measured: {found.reason}")
        return 0
    for _, message in low + high:
        assert _ecdsa_backend.sign(CURVE_P256, key, message) == \
            ecdsa_sign_pure(CURVE_P256, key, message), "backends disagree"
    low_s, high_s = _time_groups(_ecdsa_backend.sign, key, low, high,
                                 backend_reps)
    _report("cryptography", low_s, high_s)
    print("  (the two backends produced identical bytes for every message "
          "above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
