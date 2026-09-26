"""The device profile of a model placement, and what `placement_digest` is
computed over (roadmap item 515).

`docs/design/539-model-portfolio.md` is the design. This module holds the two
halves that must not be confused with each other:

THE DEMAND, which revl checks
-----------------------------
`DeviceProfile` is what a `model role` DECLARES it needs:

    model role local_q4 on_device device gpu memory 6144 quant q4_k_m

a device class from a closed vocabulary, a resident-memory floor in MiB, and
an opaque quantisation tag. The compiler checks the shape of that declaration
and checks it against the arms that name the role. It does not, and cannot,
check it against a machine.

THE SUPPLY, which revl binds and never interprets
-------------------------------------------------
What the member was ACTUALLY loaded onto is a fact about a host. Item 538
records the decision that a device profile, a load cost and a quantisation
"are properties of a host and nothing under the compiler should learn what one
is", and that the provider publishes the profile rather than configuration
asserting it. Item 517's signed `ModelDecision` therefore carries a
`placement_digest`: a digest the PROVIDER computes over its own published
profile, which revl binds into the record's MAC and never reads a quantisation
out of.

Item 517's note ends with one open ask - "publish what `placement_digest` is
computed over" - and `PLACEMENT_DIGEST_FIELDS` plus `placement_preimage` below
are the answer. They are a WIRE FORMAT, not a rule: nothing in the compiler
calls `placement_digest`, no admission decision reads one, and a provider on
another tier can reimplement the encoding from the seven field names and the
four encoding rules without importing this module.

WHICH WAY THIS FAILS
--------------------
Closed, in both halves.

* An unknown device class is a refusal, not "any device". A memory floor of
  zero is a refusal, not "no requirement". Both are in `revl.model_route`,
  beside the rules they enforce.
* `placement_preimage` refuses a missing field and refuses a value containing
  the field separator. It does not skip a field it was not given, because a
  digest that silently covers six fields instead of seven is the same shape as
  a MAC over a subset of a record: two different placements collide and the
  record looks re-runnable.

WHAT IS NOT CHECKED, AND CANNOT BE
----------------------------------
That a declared profile matches the hardware. `device gpu memory 6144` is an
author's claim in exactly the sense `docs/design/411-sandbox-placement.md`
calls the `[sandbox.needs]` gate an author claim, and in the sense
`docs/design/531-model-placement.md` section 10 already records for
`on_device`. The comparison between the declared demand and the published
supply is a runtime check a provider performs with `declared_floor()` and the
five fields it owns; revl performs it nowhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# The device classes a placement may demand. CLOSED, for the reason every
# vocabulary in `revl.model_route` is closed: a typo must be a refusal rather
# than a placement that matches anything. `npu` is separate from `gpu` because
# a member built for one does not load on the other, which is the distinction
# the issue names ("a model that needs a GPU is not distinguishable from one
# that needs a CPU").
DEVICE_CLASSES = ("cpu", "gpu", "npu")

#: The hash `placement_digest` uses. Item 517's verifier accepts any 64-hex
#: value and reads nothing out of it; this is the algorithm that produces one.
HASH_ALG = "sha256"

#: The version line every preimage starts with. A second version of the
#: encoding gets a second tag rather than a reinterpretation of this one.
PLACEMENT_DIGEST_VERSION = "revl-placement-v1"

#: WHAT `placement_digest` IS COMPUTED OVER. Item 517 asked for this list and
#: deliberately did not write it.
#:
#: Seven fields, in this order. Two are declared in the revl program and are
#: the only two revl knows; the other five are the provider's published
#: profile and revl never learns their meaning.
#:
#:   role          - the `model role` name this placement is bound to (revl)
#:   residence     - `on_device` or `off_device` (revl)
#:   device        - the device class the member was loaded onto (provider)
#:   memory_mib    - the resident memory of that load, in MiB (provider)
#:   quantisation  - the provider's quantisation tag (provider)
#:   runtime_build - the inference runtime's build identifier (provider)
#:   weights_digest- a digest of the weights actually loaded (provider)
#:
#: Why each is in. `role` and `residence` bind the digest to the placement revl
#: admitted, so a record claiming role `local` whose digest was computed under
#: `cloud` is detectable by a verifier that holds the provider's profile table.
#: `device`, `memory_mib` and `quantisation` are the three the issue names as
#: the device profile, and they are the three that make one model at three
#: quantisation points three placements rather than three calls.
#: `runtime_build` is in because two runtime builds at the same quantisation
#: produce different distributions, which is the same argument that put
#: `placement_digest` in the record at all. `weights_digest` is in because
#: item 517's `model_digest` names the model and not the file, and a fine-tune
#: is a different placement.
PLACEMENT_DIGEST_FIELDS = (
    "role",
    "residence",
    "device",
    "memory_mib",
    "quantisation",
    "runtime_build",
    "weights_digest",
)

#: The four encoding rules, stated so another tier reimplements them rather
#: than importing this module:
#:
#: 1. The preimage is UTF-8 bytes.
#: 2. Line one is `PLACEMENT_DIGEST_VERSION`, then one line per field of
#:    `PLACEMENT_DIGEST_FIELDS`, in that order, none omitted.
#: 3. A field line is `name=value`. Every line, including the last, ends with
#:    a single LF. A reader splits a line on its FIRST `=`, so a value may
#:    contain `=` and a name may not.
#: 4. A value contains no LF and no CR. There is no escaping and no quoting:
#:    a value that would need one is refused rather than mangled.
_FIELD_SEPARATOR = "="
_LINE_SEPARATOR = "\n"


@dataclass(frozen=True)
class DeviceProfile:
    """The resource a `model role` declares it needs (the DEMAND).

    Validated by `revl.model_route.roles()`, which is where the refusals live.
    `quant` is carried and compared for equality and nothing else: no rule in
    the compiler knows what `q4_k_m` means, and item 538 is the reason.
    """
    device: str
    memory_mib: int
    quant: str
    line: int

    def describe(self) -> str:
        """The one-line spelling a diagnostic uses."""
        return (f"device {self.device} memory {self.memory_mib} "
                f"quant {self.quant}")


def declared_floor(profile: DeviceProfile | None) -> dict | None:
    """The declared demand as plain data, for a provider to compare against
    its own published profile.

    Returns `None` for a role with no `device` clause, which is a role that
    demands nothing and therefore rules nothing out. The comparison itself is
    the provider's: revl does not perform it and has no way to.
    """
    if profile is None:
        return None
    return {"device": profile.device,
            "memory_mib": profile.memory_mib,
            "quantisation": profile.quant}


def placement_preimage(fields: dict) -> bytes:
    """The exact bytes `placement_digest` hashes.

    `fields` must carry every name in `PLACEMENT_DIGEST_FIELDS` and no other.
    Values are rendered with `str()`, so `memory_mib` may be passed as an int.

    Refuses, rather than coercing:

    * a missing field, because a digest over six of seven fields collides two
      placements that differ in the seventh;
    * an unknown field, because a provider that adds one silently is computing
      a digest no other implementation can reproduce;
    * a value containing LF or CR, because there is no escaping in this format
      and a value carrying a line break could forge a field line.
    """
    missing = [name for name in PLACEMENT_DIGEST_FIELDS if name not in fields]
    if missing:
        raise ValueError(
            "placement_preimage is missing "
            + ", ".join(missing)
            + "; every field of PLACEMENT_DIGEST_FIELDS is required, because a "
              "digest over a subset collides two placements that differ only "
              "outside it")
    extra = sorted(set(fields) - set(PLACEMENT_DIGEST_FIELDS))
    if extra:
        raise ValueError(
            "placement_preimage was given field(s) it does not define: "
            + ", ".join(extra)
            + "; the field set is fixed by PLACEMENT_DIGEST_FIELDS so that two "
              "implementations agree byte for byte")
    lines = [PLACEMENT_DIGEST_VERSION]
    for name in PLACEMENT_DIGEST_FIELDS:
        value = str(fields[name])
        if "\n" in value or "\r" in value:
            raise ValueError(
                f"placement field `{name}` contains a line break; this format "
                f"has no escaping, so a value carrying one could forge a field "
                f"line and is refused instead")
        lines.append(name + _FIELD_SEPARATOR + value)
    return (_LINE_SEPARATOR.join(lines) + _LINE_SEPARATOR).encode("utf-8")


def placement_digest(fields: dict) -> str:
    """The provider's digest over its published profile: 64 lowercase hex.

    This is the value item 517's `ModelDecision` carries and binds into its
    MAC. Nothing in the compiler calls this function, and no admission
    decision reads its result; it is published here so that a provider and a
    verifier compute the same bytes, which is the whole of item 517's ask.
    """
    return hashlib.new(HASH_ALG, placement_preimage(fields)).hexdigest()
