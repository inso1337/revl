package io.cordis4j.core;

public interface AsyncPlugin {
    Disposable apply(Context ctx) throws Exception;
}
