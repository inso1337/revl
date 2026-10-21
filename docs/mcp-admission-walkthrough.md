# A worked model-authored admission

This example follows one candidate from a G1 refusal to admission. The running
composition already provides the `store` service; the host supplies its
compiled IR as `runningManifest` to `revl_admit`.

The running composition is:

```revl
service Store { fn get(key: Str) -> Int }
component StoreImpl provides store: Store {
  provide store { fn get(key) = 1 }
}
```

The first candidate calls `store.get`, but does not declare that dependency:

```revl reject G1
service Store { fn get(key: Str) -> Int }
service Worker { fn run() -> Int }
component WorkerImpl provides worker: Worker {
  provide worker { fn run() = store.get("boot") }
}
```

Calling `revl_check({ source: candidate })` refuses it. The test
`tests/test_mcp_admission_walkthrough_2748.py` obtains these fields from the
live MCP handler and checks this block, so changes to the diagnostic fail the
test instead of leaving stale refusal text here:

```json
{
  "code": "G1",
  "message": "`store` is not a declared requirement of WorkerImpl",
  "hint": "component WorkerImpl requires <nothing> — add `requires store: <Service>`?",
  "guarantee": "declared access: a component reads only what it requires",
  "fix": "add the key to the component's `requires` clause, or drop the access"
}
```

The rewrite declares the service key that the method uses:

```revl
service Store { fn get(key: Str) -> Int }
service Worker { fn run() -> Int }
component WorkerImpl requires store: Store provides worker: Worker {
  provide worker { fn run() = store.get("boot") }
}
```

`revl_check({ source: correctedCandidate })` now returns `ok: true` and
`selfCheck.admissible: true`. The host then passes the same source and its
compiled running manifest to `revl_admit`:

```text
revl_admit({ source: correctedCandidate, manifest: runningManifest })
→ { "ok": true, "admitted": true, ... }
```

`revl_check` shows that the candidate passes the checker on its own. It does not
resolve `requires` against a provider. `revl_admit` links it against the current
composition, where `StoreImpl` provides the required `store` key. The
manifest input is required by the
[`revl_admit` contract](mcp-reference.md#revl_admit); this example makes no
claim about MCP transport latency.
