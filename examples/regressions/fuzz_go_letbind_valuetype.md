# Go backend: a value-typed bracket acquisition compiles (roadmap item 320)

- Found by: hand, during Slice 2b go bring-up; recorded as roadmap item 320.
- Divergence kind: **build** (the go emitter produced Go that does not compile).
- Maps to roadmap item: **320**.

## Property under test

A bound `let x = effect <acquisition> undo ...` whose acquisition is a plain fn
or service-method call returning a VALUE type must declare `x` by that value
type. The go bracket codegen always declared a bound acquisition local (and its
provide-impl struct field) as `var x *T`, which only holds for a live pointer
resource — a host object or a spawn handle — and fails to compile for a value
result.

## Minimized program (`fuzz_go_letbind_valuetype.rvl`)

```revl
extern acquire fn openHandle() -> Int undo closeHandle(result)
  = @py { return 7 } = @go { return 7 }

component ValueBracket provides probe: Probe {
  let handle = effect openHandle() undo closeHandle(handle)
  provide probe { fn ping() = handle }
}
```

## Per-tier outcome

- `go`: before item 320, `go build` failed —
  `cannot use openHandle() (int64) as *int64 value in assignment`
  (the local and the provide-impl struct field were both `*int64`).
- `go`: after the fix, `handle` is declared `int64` (the acquisition's actual
  return type); the component loads, `probe.ping()` returns `7`, and teardown
  reverts cleanly (`no_residue`).

## Root cause

`backends/go/emit.py` decided the bound-acquisition declaration purely from the
host/spawn type table (`_host_of_bind`), which returns `any` for anything that
is neither a host object nor a spawn handle, and every caller prepended `*`. A
value-typed acquisition therefore emitted `var x *any` / an `x *any` struct
field.

## Fix

The pointer-vs-value decision now follows the acquisition kind: only `host` and
`spawn` acquisitions are pointer resources (`*T`, unchanged and byte-identical);
any other acquisition binds a VALUE, declared by the acquisition's resolved
return type (`_acquire_value_go_type`) with no `*`. Both the Apply-body local
and the provide-impl struct field consult the same `_bind_is_ptr` decision, so
they always agree.
