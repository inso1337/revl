;; A stand-in agent artifact, for the SECOND enforcement layer only.
;;
;; This module is not compiled from any candidate in `../candidates/`. It exists
;; so the demo can show what item 289 calls the substrate half of the chain:
;; a compiled artifact declares its reach as WASM IMPORTS, and an import the
;; host's policy does not name is simply absent from the import object, so the
;; wasm ENGINE refuses to instantiate. Not this project's code, not the gate's:
;; the engine's.
;;
;; It reaches two host capabilities. A policy granting only `revl:host/log`
;; cannot instantiate it, and the failure is a `WebAssembly.LinkError` naming
;; the ungranted module. A policy granting both instantiates fine.
;;
;; It is committed as source and assembled to `reaching_tool.wasm` by
;; `wasm-tools parse`; `tests/test_gate_consumer_example_js.py` re-assembles it
;; and checks the committed bytes still match, so the binary in this directory
;; is never something a reader has to take on trust.
(module
  (import "revl:host/log" "write" (func $log (param i32)))
  (import "revl:host/db" "query" (func $query (param i32) (result i32)))

  (memory (export "memory") 1)

  ;; The reach the policy is asked about: read a row, then log that it happened.
  (func (export "run") (param $key i32) (result i32)
    (local $row i32)
    (local.set $row (call $query (local.get $key)))
    (call $log (local.get $row))
    (local.get $row)))
