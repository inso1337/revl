package io.cordis4j.core;

/**
 * Root-context factory — the real cordis4j entry point.
 *
 * In the real runtime `Context` is an interface and a root is minted with
 * `Contexts.create()`; the stub `Context` is a class, so this is a thin
 * `new`. Emitted code that needs a root of its own (the lifecycle-test
 * driver, docs/syntax-2.0.md §7.1) goes through this factory rather than
 * `new Context()`, so the SAME emitted source compiles and runs against the
 * stubs here and against a compiled cordis4j-core
 * (`REVL_CORDIS4J_CLASSES`).
 */
public final class Contexts {
    private Contexts() {}

    public static Context create() {
        return new Context();
    }
}
