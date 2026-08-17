package io.cordis4j.core;

public final class Disposables {
    private Disposables() {}

    public static Disposable of(Runnable action) {
        return action::run;
    }

    /** Disposes in the GIVEN order — matching the real cordis4j (verified
     * against its source); the emitter lists inverses in reverse
     * acquisition order to get LIFO teardown. */
    public static Disposable composite(Disposable... items) {
        return () -> {
            for (Disposable item : items) {
                item.dispose();
            }
        };
    }

    public static Disposable none() {
        return () -> {};
    }
}
