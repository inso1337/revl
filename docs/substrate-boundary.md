# The substrate boundary

Classify each consequential action by the path that performs it. A system may
use more than one path, so describe the verified surface action by action.
To choose between paths 1 and 2, ask who performs the effect: revl component
code or the body of an extern whose declaration names the effect, doing the
work itself (path 1), or a separate host service that an extern body forwards
to (path 2).

## 1. The action goes through revl

The action is performed by a revl component, directly or through the body of
an extern the component calls whose declaration names the effect
(`emission[C]`, `witnessed[C]` or `acquire`), and that body does the work
itself. For example, a component sends mail through a declared
`emission[mail]` extern, or writes a file through the standard `witnessed[fs]`
`write` in `stdlib/fs.rvl` (py and ts tiers), whose witness records the path
and preimage ([witnessed filesystem operations](witnessed-fs.md)). At compile
time the checker holds the component to its declared capability
([capabilities](capabilities.md)) and to G4: the send must be an explicit
`emit`, approved where its capability requires approval, and any inverse or
`compensate` path must be present and well-shaped. On abort the runtime runs
the compensation, or the inverse with the stored witness.
You may say this action is inside revl's declaration, admission and teardown
guarantees. The extern body that performs the send or the write is trusted
host code, not verified, and an inverse is checked for presence, not for
correctness. That boundary remains trusted and enumerable under G8
([vision](vision.md)).

## 2. A host gateway performs it and revl records a supported effect

A gateway outside the revl program performs the action. It is on this path
only when revl reaches it through an effect path the tree already defines: a
declared `witnessed[C]` operation or `emission[C]` whose extern body hands the
request to the gateway and returns the gateway's report. For example, a
`witnessed[C]` extern whose body asks a host file service to do a
write and returns that service's report as the witness. The tree has no other
mechanism for this: path 2 uses the same declaration forms as path 1, and only
the performer, and so the claim, differs. You may say the record is declared
and its inverse, if one is declared, runs on abort; you may not say revl
performed or checked the action.

This is not a promise of a general signed-record format: the repository defines
no such envelope. The host gateway remains trusted to report its work honestly.
revl checks the declaration at compile time and, on abort, runs any declared
inverse with the stored witness; it does not check that the witness describes
what the host did.

## 3. The work is a proposal only

An agent may draft a command, tool call, or component for review without
performing it. For example, a proposed file deletion has no filesystem effect
until a later step executes it through path 1 or 2. You may say the action was
proposed or checked as text; you may not say it was executed or governed by
revl's effect guarantees.

## 4. The action is outside the verified surface

A host may call a browser, shell, or other tool directly, without passing the
action through revl and without producing a supported effect record. It may
also reach it through an extern whose declaration does not name the effect, or
through an `acquire` or `pure` extern that forwards it to a separate host
service (`acquire` is host-local by definition). That action is outside the
verified surface. Name the excluded action and its boundary; a
system with such a path must not claim that every consequential action passes
through revl's typed, capability-checked runtime.

When reporting a mixed system, state which actions use paths 1 and 2, which are
proposals under path 3, and which remain outside under path 4. With a
record, the verified claim is about the record, not the action; a record does
not turn an unmediated host action into a revl action ([the consumer-boundary
analysis](design/549-aios-consumer-boundary.md)).
