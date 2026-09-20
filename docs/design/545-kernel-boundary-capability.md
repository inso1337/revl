# 545: The admission kernel as a capability, not a sentence

Roadmap: item 544 (issue #1223), from the architecture review of the 512 to 541
wave. Slice 1 is LANDED with this note; the residuals are named in section 7
and are not written.

Reconciles with: item 520 and `docs/design/537-evolution-controller.md` (the
lifecycle, whose §11 says the kernel enumeration it built is a diff check and
that this item is the enforcement), item 519 and
`docs/design/539-model-in-attenuation.md` (the model role in the attenuation
product, whose `model_reach` record this consumes), item 66 and
`docs/capability-attenuation.md` (the product itself), item 329 and
`docs/design/329-untrusted-author-profile.md` (the profile that says what "a
generated component" is), item 472 and item 532 (the retention discipline and
the argument-blind refusal that showed how subtle its surface already is),
`docs/guarantees.md` (the code registry).

---

## 0. The decision in one paragraph

Item 520 carries the invariant the self-evolution programme rests on: *the
system may evolve its behaviour, but it may not unilaterally evolve the rules
that govern its authority.* Stated as a policy, that is a rule some later
generation can propose a change to, and the loop's proposal channel is exactly
the mechanism for proposing changes to rules. So it is not stated as a policy.
The admission kernel is enumerated once, in `src/revl/kernel_boundary.py`, as a
set of **capability tokens** with the tree paths each stands for and the
guarantee each defends, and the attenuation product refuses any component whose
effective ceiling is not provably disjoint from it. Retention is inside that
set, and an untrusted author may not declare a retention policy at all, because
a deadline is an authority and not a cache setting.

---

## 1. Why a diff check cannot be the answer

`tools/evolution_controller.py` (item 520) refuses a candidate whose **changed
files** reach the kernel. It is a necessary check and a cheap one, and its own
design note says what it cannot do:

> The kernel enumeration is a diff check, not a capability. It refuses a
> candidate whose *files* reach the kernel. It cannot refuse a candidate that
> changes kernel behaviour through a path that touches no enumerated file, and
> issue #1223 is right that only the attenuation product can.

The two questions are different in kind.

| | the question | the input | the evasion |
|---|---|---|---|
| diff check | did this candidate touch a kernel path | a changed-file set, after the fact | any route that reaches the same state without editing those files |
| capability | can this component hold the authority that reaches the kernel at all | the component's declared authority, at admission | none that does not first widen the declared set, which is itself refused |

The second is structural. A component does not have to *narrow* its way to the
kernel; it has to be unable to touch it. That is a property of the composition
the compiler already computes, not a property of a patch.

---

## 2. The enumeration, in one file

`src/revl/kernel_boundary.py` holds both halves of the list that issue #1223
asks for in one place.

`KERNEL_PATHS` is the tree side: the admit decider and the profile a candidate
is admitted under, the attestation chain, the taint lattice, the retention
discipline, this file itself, the gate crate, `formal/`, and the census tool
with its baseline (which carries the `NEVER_BASELINED` list). Every entry is
asserted to exist in the tree by `tests/test_kernel_boundary_544.py`. That
assertion is item 520's contribution and it is kept, for its reason: an
enumeration naming a file that is not there protects nothing.

What is deliberately **not** in it is the same set `tools/heldout_scoring.py`
leaves out of `HELD_OUT_FENCE`: `src/` at large, `selfhost/`, `backends/` and
the rest of `crates/` are the SUBJECT of the loop. Fencing them would forbid
the work the loop exists to produce. The fence is the judge, never the subject.

`KERNEL_CAPS` is the capability side: one `KernelCap` per member, carrying the
capability **token**, the **guarantee** a refusal cites, the `KERNEL_PATHS`
entries it stands for, and the one sentence a reader needs.

| token | guarantee | stands for |
|---|---|---|
| `kernel.admission` | G8 | `src/revl/admission.py`, `src/revl/admit_profile.py`, `src/revl/kernel_boundary.py` |
| `kernel.attest` | G8 | `src/revl/attest.py` |
| `kernel.taint` | G9 | `src/revl/taint.py` |
| `kernel.retention` | G-RETAIN | `src/revl/retention.py` |
| `kernel.gate` | G8 | `crates/revl-gate` |
| `kernel.census` | G8 | `tools/gate_reference_census.py` and its baseline |
| `kernel.formal` | G8 | `formal` |

Three tests keep the two halves one list: every member path is a
`KERNEL_PATHS` entry, every `KERNEL_PATHS` entry is stood for by some member,
and every member token is in the reserved namespace. Two lists that agree today
are still two lists.

**One enumeration, two consumers.** The diff-side consumer is
`tools/evolution_controller.py`, whose `KERNEL_PATHS` is the same tuple in the
same order. When both land it imports this one rather than keeping a copy, and
`test_the_controller_reads_this_enumeration_rather_than_copying_it` holds that
as soon as the file is on the tree (it skips, rather than pretending, while the
file is only on a branch). The capability-side consumer is
`lower._check_kernel_boundary`. The module lives under `src/revl/` and not
under `tools/` because the capability is enforced by the compiler and the
compiler cannot import from `tools/`.

---

## 3. The refusal, and what it extends

The rule is item 66's with the kernel on the left instead of a spawner:

```
held(kernel)  n  effective(C)  =  {}   ->  admit
held(kernel)  n  effective(C) !=  {}   ->  REFUSE, naming both sets
```

folded by `cap_order.disjoint` rather than `cap_order.covers`, because the
question is intersection and not coverage. There is no new algebra and no
second mechanism: `disjoint` is the same predicate `parallel.py` uses to prove
two emissions independent, and the kernel elements are ordinary `Cap`s under a
reserved token namespace.

`effective(C)` is what the component holds, folded with what any authority
surrogate it routes through can reach. **That second half is consumed from item
519 rather than re-derived.** `lower._check_model_attenuation` writes one
record per `route model` edge into `manifest["model_reach"]`, and this check
reads two fields of it and nothing else:

* `effective`, the per-edge ceiling as the product recorded it. Two
  derivations of one ceiling are two things that can disagree, so there is only
  one, and it is the product's.
* `reach_declared`, False when the role wrote no `reaches [...]` clause. An
  absent entry is not a proof of narrowness, so such a row contributes the
  unnameable `*` instead of the (empty, hence narrow-looking) reach the record
  renders.

The refusal names both sets, the tree the authority is authority over, and the
guarantee the member defends. It is not a second mechanism beside item 519's
refusal; it is the same fold with the kernel's held set on the left.

---

## 4. Which way it fails

Toward refusing, and every unknown is named rather than defaulted.

**The unnameable `*`**, a host emission or a first-class dispatch that no
`requires` key can name, is disjoint from nothing. That is `cap_order`'s own
rule, written there before this item and quoted here because it is exactly the
reading the kernel question needs: a boundary that may reach anything may reach
the kernel. An omitted `reaches [...]` clause lands on the same element, which
is item 519's own resolution of the same question and
`lower._spawn_emission_surface`'s precedent of mapping `None` to `*` rather
than to `set()`.

**A declared token in the `kernel` namespace that the enumeration has no member
for**, such as `kernel.something_new`, is refused under G8 rather than read as an
ordinary boundary. An enumeration that is behind is not a licence.

**The scope switch is a scope switch, not a default.** The `*` arm runs for a
candidate admitted under the untrusted-author profile (source whose author is
a model, which is what "a generated component" means in `src/revl/` today)
and not for the first-party tree, which is the subject of the loop rather than
a candidate passing through admission. A **declared** kernel token is refused
on both sides. The switch is `undeclared_reaches_kernel`, it is passed
explicitly at the one call site, and `kernel_boundary.offending` takes it as a
required keyword so no caller can forget which side it is asking about.

---

## 5. Retention is on the kernel side

This is the case the wave filed on the wrong side, and it is worth stating in
full because the mistake is easy to repeat.

A loop permitted to "update indexes and retention policies" as ordinary
behaviour tuning is a loop that can extend its own `Retained[T, P]` deadline.
`until` is the date past which a retained value may no longer reach a
persistence sink; `hold` is the legal-hold exception that overrides that date;
`deleters` is who may demand erasure. All three are authority. G-RETAIN is the
guarantee over them, and a policy tuned by the thing it bounds is that
guarantee relaxed by the party it constrains.

Item 532 measured how subtle the surface already is with no loop touching it:
the retention refusal was argument-blind across a service seam, so a value past
its deadline reached a provider's store with the compiler silent. A policy an
untrusted author wrote for itself is not a surface this gate should be
reasoning about at all.

So retention lands on the kernel side twice.

1. `kernel.retention` is a member of `KERNEL_CAPS`, so a component reaching a
   boundary declared under that token is refused by the fold in section 3,
   citing G-RETAIN.
2. `admit_profile.check_no_retention_policy` refuses a `retention <name> { … }`
   declaration written by an **untrusted author**, structurally and before
   lowering, exactly as `check_no_extern` and `check_no_asset` do. It rides the
   `no_extern` flag rather than growing a knob, for the reason `check_no_asset`
   gives: every field of this profile is a property of the author, and a door
   that picks a subset is a door with a hole in it.

Root-scoped, like its siblings. A `Retained[T, P]` qualifier naming a policy
the trusted composition declares is untouched; what an untrusted author may not
do is mint the policy that bounds its own data.

---

## 6. Non-vacuity, measured

This repository has eleven measured checks that ran on every PR and could not
fail. The evidence that this is not the twelfth is a differential, not an
argument.

`tests/test_kernel_boundary_544.py` compiles one corpus twice: once with
`lower._check_kernel_boundary` neutralised, which is the tree without this
change, and once with it live.

| | without the check | with it |
|---|---|---|
| 7 programs, one per enumerated kernel member | admitted | refused, each citing its member's guarantee |
| 1 program naming an unenumerated `kernel.*` token | admitted | refused under G8 |
| the control: a component declaring `kv.write` | admitted | **admitted** |

8 admitted before, 8 refused after, 1 control admitted on both sides. The
retention half adds a ninth refusal (an untrusted author declaring
`retention loop_cache`, refused under G-RETAIN naming the deadline it wrote)
with the trusted author's identical source not refused by that rule, which is
what makes it a property of the author.

The `*` arm is proved separately, at the seam it will arrive through:
`test_an_undeclared_surrogate_refuses_the_compile_end_to_end` injects a `*`
where item 519's product record supplies it and the compile is refused, and the
same injection against a first-party compile is inert.

---

## 7. What this slice does not do

Stated plainly, because a design that claims its own completeness is the thing
this repository keeps finding.

**The `key:` residual.** A service method that declares `emission` with no
capability list yields a `key:`-namespaced fold element: a declared *wiring*
with an undeclared *reach*. Treating it as provably disjoint from the kernel is
the weaker answer, and this slice takes it. The reason is a measurement, not a
preference: on this tree it is the ordinary spelling, so refusing it turns 14
tests across 7 files red and changes what `mcp.session.admit` accepts on every
turn whose granted services spell `emission` bare. Making those services
declare their tokens is a composition-side change on the operator's side of the
boundary, and it is its own item.
`test_the_key_namespaced_residual_is_still_admitted` pins the residual so it
stays visible.

**The `*` arm is dormant for a candidate's own reach on `main`.** Both routes
that would put a `*` into an untrusted-authored component's own reach are
already refused, earlier and by name: declaring a host extern
(`check_no_extern`) and reaching an imported one
(`check_no_host_extern_reach`). That is defence in depth, and
`test_the_host_extern_routes_to_star_are_closed_before_this_check` measures it
so the claim is a fact about the tree rather than an assumption. The arm is
live through item 519's product record, which is where an undeclared authority
surrogate will arrive from.

**Nothing here refuses a kernel reach made by host code.** G8's boundary is
"verbatim host code, unchecked inside" (item 24: the gate does not sandbox host
code). An `@py` body that imports `revl.admission` is not visible to a
capability fold, and the profile's answer to that is `no_extern` plus
`check_no_host_extern_reach`, not this check. What this check adds is that the
kernel is *nameable*, so a candidate that wants it has to say so, and saying so
is refusable.

**No service in the tree declares a `kernel.*` token.** That is the intended
state, not a gap: the namespace exists so that a reach into the kernel has to
be written in the one vocabulary the product already folds. The corpus in
section 6 is what proves the namespace bites.

**The self-host gate does not decide this refusal.** It is outside the layer
`selfhost/lower.rvl` covers, so it classifies out of slice and parks in
`no-objection-out-of-slice`, like every other refusal from a later frontend
phase. `tools/gate_reference_census.py --check` reports no change from the
baseline.
