package io.cordis4j.core;

public interface Plugin {
    Disposable apply(Context ctx);
}
