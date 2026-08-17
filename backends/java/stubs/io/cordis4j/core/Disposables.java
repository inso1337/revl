package io.cordis4j.core;

public final class Disposables {
    private Disposables() {}

    public static Disposable of(Runnable action) {
        return action::run;
    }

    /** Disposes in reverse registration order (LIFO), matching G7. */
    public static Disposable composite(Disposable... items) {
        return () -> {
            for (int i = items.length - 1; i >= 0; i--) {
                items[i].dispose();
            }
        };
    }

    public static Disposable none() {
        return () -> {};
    }
}
