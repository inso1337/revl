"""The composition authority panel — roadmap item 426, slice S5 (§8).

The panel is the approval surface an operator reads before applying a layer to
a composition. Both source designs made an authority diff the headline; both
then found a CRITICAL in it, and neither design's fix closed the other's
(§8.1). This module builds the merged panel that closes both:

  - CRITICAL A (the panel is computed from the attacker's own DECLARATIONS):
    every non-first-party row admits under the untrusted-author profile (S4),
    so there is no unchecked host body for a declaration to lie about. The one
    escape, `--trust-host-code`, changes the panel's SHAPE (§8.8) and forfeits
    the word `clean`.
  - CRITICAL B (the panel is blind to CONFIG): a sixth crossing kind,
    `config:<row-label>:<field>:<digest8>` (§8.4), is emitted for a config
    field whose value is authority-bearing, so a `configure`-only layer that
    steers a host body it does not touch is no longer invisible.

Two structural rules come before any content (§8.2): the panel opens with a
TRUST BASIS line, and every token is keyed by ROW LABEL, not component name, so
a component rename reads as a non-event and not a full authority turnover.

The headline is FAIL-CLOSED (§8.5): the panel never prints `clean` unless every
conjunct holds, and conjunct 5 — an unclassifiable config field is treated as
authority-bearing — states the direction: incompleteness costs noise, never
silence. The BLIND SPOTS block (§8.7) is printed ALWAYS, because a screen that
looks complete while being structurally blind to the likeliest attack is worse
than no screen.

This module reads a resolved `RowTable` (base) and the folded candidate table.
Config classification is header-only and syntactic, which is the fail-closed
direction: a string-valued field that is not pinned by a `reach` bound is
UNCLASSIFIABLE and therefore authority-bearing; a numeric/bool field that feeds
only pure computation gets no token. `reach`-bound violations never reach the
panel — they are refused at resolution (`composition._check_reach_bounds`).
"""

from __future__ import annotations

import hashlib
import json


def _digest8(value: object) -> str:
    """An 8-hex digest of a config value's canonical form (§8.4).

    The digest, not the value, goes in the token, so a config value that is
    itself sensitive is never carried into an ack file, a CI log or a commit
    message — the panel prints the values, which is where a human reads them.
    A later change to the same field produces a DIFFERENT token and re-prompts,
    which is what stops `--accept config:@db:url` from accepting every future
    value of the field forever (the second-order form of the same critical).
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _authority_bearing(field: str, value: object,
                       reach: dict | None) -> tuple[bool, bool]:
    """`(bearing, unclassifiable)` for one config field's value.

    A numeric or boolean value that feeds pure computation is not
    authority-bearing (`pool: 8`, §8.7 "pure, no token"). A value with a
    declared `reach` bound is authority-bearing and CLASSIFIED (its host was
    checked at resolution). Anything else the classifier cannot decide — a
    bare string, a structured value — is UNCLASSIFIABLE and therefore treated
    as authority-bearing (§8.5 conjunct 5, the load-bearing one).
    """
    if reach and field in reach:
        return True, False
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return False, False
    # A string (or anything else) with no declared bound: the dataflow to a
    # host argument cannot be decided header-only, so fail closed.
    return True, True


def _config_of(table) -> dict[str, dict]:
    """label -> config dict, keyed by BARE label (§8.2 re-keying)."""
    return {row.label: dict(row.config or {}) for row in table.rows}


def _reach_of(table) -> dict[str, dict]:
    return {row.label: dict(row.reach or {}) for row in table.rows}


def _trust_of(table) -> dict[str, str]:
    from .composition import row_trust  # noqa: PLC0415 — avoids import cycle
    return {row.label: row_trust(row) for row in table.rows}


def panel(base, candidate, *, trust_host_code=False) -> dict:
    """Compute the authority panel comparing `base` to the folded `candidate`.

    Both are resolved `composition.RowTable`s. `trust_host_code` is the §8.8
    escape hatch: `True` trusts every non-first-party row, a set of qualified
    labels trusts the named ones. Returns a dict; `render` turns it into text.
    """
    from .composition import _trusts  # noqa: PLC0415

    base_cfg = _config_of(base)
    cand_cfg = _config_of(candidate)
    cand_reach = _reach_of(candidate)
    cand_trust = _trust_of(candidate)
    qual_of = {row.label: row.qualified for row in candidate.rows}

    tokens: list[dict] = []
    unclassifiable: list[str] = []
    for label, cfg in cand_cfg.items():
        prior = base_cfg.get(label, {})
        reach = cand_reach.get(label) or None
        for field, value in cfg.items():
            if field in prior and prior[field] == value:
                continue                              # unchanged
            bearing, unclass = _authority_bearing(field, value, reach)
            if not bearing:
                continue                              # pure, no token (§8.7)
            token = f"config:@{label}:{field}:{_digest8(value)}"
            tokens.append({
                "token": token,
                "label": label,
                "field": field,
                "value": value,
                "was": prior.get(field),
                "bounded": bool(reach and field in reach),
                "unclassifiable": unclass,
            })
            if unclass:
                unclassifiable.append(f"@{label}.{field}")

    # §8.8: the rows admitted under `--trust-host-code`. Only a non-first-party
    # row can be trusted this way; trusting a first-party row is a no-op.
    claimed: list[str] = [
        qual_of[label] for label, cls in cand_trust.items()
        if cls == "non-first-party" and _trusts(trust_host_code, qual_of[label])]

    # §8.2 TRUST BASIS. CLAIMED the moment any row is admitted with
    # --trust-host-code; otherwise the ordinary measured state.
    if claimed:
        trust_basis = "CLAIMED"
    else:
        trust_basis = "MEASURED, first-party bodies trusted by premise"

    # §8.5 the fail-closed headline. `clean` requires: no config token changed,
    # no unclassifiable field, and no --trust-host-code row (conjunct 3).
    clean = (not tokens) and (not unclassifiable) and (not claimed)

    return {
        "composition": candidate.name,
        "trust_basis": trust_basis,
        "tokens": tokens,
        "unclassifiable": unclassifiable,
        "claimed": claimed,
        "clean": clean,
    }


# The blind spots the panel does NOT measure (§8.7). Printed ALWAYS, including
# on a clean verdict — a structurally blind screen that looks complete is worse
# than no screen.
_BLIND_SPOTS = (
    "Token granularity. A crossing that widens which PATHS an already-held "
    "capability reaches produces no new token (parameterized spellings are "
    "item 294).",
    "First-party host bodies. Externs in the project's own sources are read by "
    "DECLARATION, not by inspection; a config change can steer them (see the "
    "config tokens above).",
    "Unbounded externs. A host extern with no declared `reach` cannot have a "
    "config value checked against a bound — declare one in the composition to "
    "close it.",
)


def render(result: dict) -> str:
    """The operator-facing panel text (§8.7 / §8.8)."""
    lines: list[str] = []
    lines.append(f"TRUST BASIS  {result['trust_basis']}")

    if result["claimed"]:
        # §8.8: --trust-host-code changes the SHAPE, not just the content.
        lines.append(f"             {len(result['claimed'])} row(s) admitted "
                     "with --trust-host-code. This panel cannot say what they do.")
        lines.append("")
        lines.append("UNCHECKED HOST CODE")
        for qual in result["claimed"]:
            lines.append(f"  {qual}  admitted as reviewed first-party code")
        lines.append("  Nothing verifies these bodies against their "
                     "declarations. Read them.")

    lines.append("")
    lines.append(f"AUTHORITY    applying to {result['composition']}")
    if result["tokens"]:
        for tok in result["tokens"]:
            bound = "bounded, in bound" if tok["bounded"] else \
                ("unclassifiable — authority-bearing" if tok["unclassifiable"]
                 else "config")
            lines.append(f"  ~ {tok['token']}")
            lines.append(f"      @{tok['label']}.{tok['field']}: "
                         f"{tok['was']!r} -> {tok['value']!r}   ({bound})")
    else:
        lines.append("  = no config or crossing tokens changed")

    lines.append("")
    lines.append("BLIND SPOTS  what this panel does NOT measure")
    for spot in _BLIND_SPOTS:
        lines.append(f"  * {spot}")
    lines.append(f"  * {len(result['unclassifiable'])} unclassifiable config "
                 "field(s) this run" + (
                     ": " + ", ".join(result["unclassifiable"])
                     if result["unclassifiable"] else
                     " — any would count as authority-bearing"))

    lines.append("")
    if result["clean"]:
        lines.append("VERDICT      authority-drift: clean")
    else:
        why = []
        if result["tokens"]:
            why.append(f"{len(result['tokens'])} config token(s)")
        if result["claimed"]:
            why.append(f"{len(result['claimed'])} --trust-host-code row(s)")
        if result["unclassifiable"]:
            why.append(f"{len(result['unclassifiable'])} unclassifiable field(s)")
        lines.append("VERDICT      NOT clean — " + ", ".join(why))
        lines.append("             Acknowledge with --accept <token> or "
                     "--accept-all.")
    return "\n".join(lines)
