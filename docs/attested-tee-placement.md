# Attested-TEE placement: requiring an enclave in the placement file

Roadmap item 475 (issue #827). The placement file (`src/revl/placement.py`) could
already choose a **backend** per component, a **machine** per seam (item 56) and
a **host** by capability and realm (item 119). This item adds a fourth
dimension: a process may demand that whatever runs it can **prove** it is running
the approved bundle inside an approved enclave. The word for that demand is
`requires attested_tee`, and it is a word in the placement vocabulary.

The verifier is not here. `src/revl/tee_attestation.py` (roadmap item 474's
follow-on, design note `docs/design/475-attested-tee-placement.md`) already owns
the vocabulary of a remote-attestation proof and the decision it encodes, and
`src/revl/peer_offer.py` already owns the field it lands in. This surface is a
second way to **spell** the demand, so that a placement file can state it; there
is deliberately no second implementation of "attested", because two
implementations of one rule are two answers waiting to disagree.

## The shape (toml)

A process may carry an `[attest]` table. Every key but `requires`, `bundle`,
`measurements` and the region is optional:

```toml
[processes.worker]
components = ["Worker"]

[processes.worker.attest]
requires = "attested_tee"          # the only requirement word this surface knows
bundle = "abababababababababababababababababababababababababababababababab"
                                   # the approved bundle, by canonical content hash
measurements = ["cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"]
                                   # one hex digest per permitted enclave build
region = "eu-west"                 # the permitted region; `regions` for several
outbound_network = "forbidden"     # the only posture this item defines
max_age_s = 120                    # optional; the proof's freshness window
nonce = "…"                        # optional; the challenge the proof must answer
```

`bundle` is a 64-hex SHA-256, `measurements` is a non-empty list of hex digests,
and `outbound_network` accepts exactly one value, `forbidden`. `region` and
`regions` are the singular and plural of one fact: a table carrying both is
refused rather than resolved by a tie-break rule.

`nonce` is the challenge the attestation must answer. A placement may pin it; a
caller may supply one instead; failing both, the surface mints a fresh one. An
attestation that answers no challenge of the verifier's choosing is evidence of
nothing in particular, which is why the challenge is never left empty.

## What it wires to

Parsing the table produces the typed `TeeRequirement` that `tee_attestation.py`
already defines, which lands in the `attested_tee` field `PlacementSlot`
(`src/revl/peer_offer.py`) already carries. The verdict is
`peer_offer.offer_eligible`'s verdict, which routes the demand through
`tee_admits`:

- `placement.parse_tee_requirement(placement, process)` → `TeeRequirement`
- `placement.process_placement_slot(placement, process)` → `PlacementSlot`
- `placement.admit_peer_for_process(placement, process, offer, offer_key=…, root=…, tee_ledger=…, now=…)` → `(admitted, reason)`

`root` is the attestation root the evidence is verified against: a
`tee_quote.HardwareRoot` holding keys the operator pinned, which the peer does not
hold (`docs/tee-attestation-root.md`). `attester_key=` is the pre-root spelling and
selects the development symmetric-MAC verifier, which is not an attestation root
and labels every verdict it reaches. There is deliberately no placement-FILE key
for a root: a root is operator configuration, it is what makes the demand mean
anything, and a file a composition author edits is the wrong place to name it.

So a peer is admitted only on a proof that is authentic, about the approved
bundle, inside a permitted measurement and region, confined to a forbidden
outbound network, fresh, unanswered before, and vouched for by a key the peer
does not hold. An offer's asserted `trust = "attested"` facet is decoration: the
signature proves the peer wrote the word, not that it is so. The permitted
region is checked against the **attestation**, never copied into the slot's
asserted `regions`.

`admit_peer_for_process` never raises; it returns `(False, reason)` and the
reason is the verifier's own, so the placement spelling cannot refuse an offer
for a different cause than the demand would.

## What is refused, and why

Shape is judged at the placement surface, before anything spawns, in the
existing `abort(...)` style (a one-line diagnostic, a non-zero exit, nothing
spawned):

- **An unknown word.** `requires` accepts `"attested_tee"` and nothing else. An
  unrecognized word is refused rather than ignored.
- **A table that demands nothing.** A missing `requires`, an empty
  `measurements` or `regions` list, a posture other than `forbidden`, a missing
  `bundle`, a `region` that is not a name: each is refused by name, so a typo
  cannot silently drop a demand. A table with no `requires` word is not "a
  placement that requires nothing", it is a mistake, and it is refused as one.
- **An unknown key.** Every key the surface knows is listed in `ATTEST_KEYS`; a
  key outside it is refused by name, the same discipline `DEPLOY_KEYS` follows
  for the deploy map (item 118).
- **A demand this build cannot satisfy.** This is the plan-time verdict, and it
  is the fail-closed direction the item asks for. `run_placement` refuses a
  well-formed `requires = "attested_tee"` outright:

  ```
  error: process(es) 'worker' require an attested TEE (`requires =
         'attested_tee'`), but this build places a process on the planning host
         and has no peer transport and no attester root, so nothing can produce
         the enclave evidence the demand is checked against; an attested
         placement is refused rather than run unattested
  ```

  Running it anyway would be an unattested run of an attested placement, which
  is the one outcome the demand exists to forbid. The refusal names the processes
  that asked for it.

## Additivity

The dimension is entirely opt-in, and the opt-out is byte for byte the old
behaviour. A process with no `[attest]` table declares **no** requirement: it
parses to nothing, not to an empty demand, and it is checked against the empty
`PlacementSlot`, which demands nothing, consumes no challenge and pays one
`is None`. "Declares no requirement" therefore cannot be confused with "declares
a requirement that admits everything" — the second is refused at parse time.
Every existing placement file is unchanged.

## Tests

`tests/test_tee_hardware_root.py` drives this surface with a real TDX quote
chained to a pinned root, and pins that the file spelling's verdict is still
byte-identical to `offer_eligible`'s on the hardware path.

`tests/test_placement_attested_tee.py` pins the spelling: that it parses and
round-trips (`parse` → `render` → `parse` is stable, the minted challenge
included), that each way of spelling a demand that demands nothing is refused by
name, that a placement without the key behaves exactly as before, that a
placement with it refuses a non-attesting peer with `tee_admits`' own reason
(byte-identical to what `offer_eligible` says for the same offer and slot), and
that absent, unreadable, unattested-root, ledger-less, peer-held-key and replayed
evidence all refuse rather than admit. `tests/test_tee_attestation.py` carries
the same connection from the verifier's side. The full vocabulary and every
refusal reason are in `docs/design/475-attested-tee-placement.md`.
