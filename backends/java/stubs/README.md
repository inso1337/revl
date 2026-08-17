# cordis4j API stubs (compile gate)

This is the **assumed** `io.cordis4j.core` surface the emitter targets —
exactly the types and signatures the generated code uses, with minimal
working bodies. The javac gate in `test_emit_java.py` compiles emitted
sources against these stubs, which proves the emitted Java is valid and
internally consistent with this declared surface.

What it does **not** prove: that this surface matches the real
[cordis4j](https://github.com/1na-ko/cordis4j) API, or that the runtime
semantics (LIFO disposal of `EffectScope`/`composite`, divert-during-await)
hold. Validating against the real jar — and porting the A1/G7 runtime
scenarios the python/ts/wasm backends already pass — is tracked in
docs/v2.0-roadmap.md. If the real API differs, fix the emitter *and* these
stubs together.

The stub bodies are functional enough (a real `dispose()` cascade, LIFO
`composite`) that a future runtime-behavior test can reuse them as a
reference harness in the meantime.
