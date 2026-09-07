//! Build script: optionally bake a pinned runtime archive INTO the binary, so
//! ONE distributed file carries its own interpreter (item 336, issue #102, the
//! one-file bundling slice).
//!
//! The runtime-management contract in `src/runtime.rs` already resolves a
//! private `python-build-standalone` runtime from a file BESIDE the executable
//! (`REVL_LSP_RUNTIME_ARCHIVE`, a `runtime.tar`, or an extracted `runtime/<pin>/`
//! tree) and extracts it atomically into a versioned private cache. That is a
//! self-contained runtime, but a self-contained *pair* of files: the binary plus
//! the archive. The genuinely single distributed FILE the item's prose reaches
//! for wants the archive INSIDE the executable, which is what this script wires:
//! a distribution/build step (338) that sets `REVL_LSP_EMBED_RUNTIME` to a pinned
//! runtime archive bakes its bytes into the binary via `include_bytes!`, and
//! `runtime::locate` extracts them through the very same atomic, versioned,
//! isolated cache path — no interpreter change, the child process just reads its
//! bytes from the binary instead of a sibling file.
//!
//! When `REVL_LSP_EMBED_RUNTIME` is unset (a bare `cargo build`, and every CI
//! job today) the generated constant is `None` and NOTHING changes: no archive
//! is embedded, `locate` falls through to the existing beside-the-executable and
//! `PATH` behavior. The archive bytes themselves are supplied by the build, not
//! committed to the crate, exactly as `runtime.rs` "resolves a bundled runtime
//! rather than embedding one" — this is a no-op until a distributable build
//! actually points at an archive.

use std::path::Path;

fn main() {
    // A change to either knob, or to the archive the first names, must re-bake.
    println!("cargo:rerun-if-env-changed=REVL_LSP_EMBED_RUNTIME");
    println!("cargo:rerun-if-env-changed=REVL_LSP_EMBED_RUNTIME_PIN");

    let out_dir = std::env::var("OUT_DIR").expect("cargo sets OUT_DIR for a build script");
    let generated = Path::new(&out_dir).join("embedded_runtime.rs");

    let body = match std::env::var("REVL_LSP_EMBED_RUNTIME") {
        Ok(path) if !path.is_empty() => {
            let archive = Path::new(&path);
            assert!(
                archive.is_file(),
                "REVL_LSP_EMBED_RUNTIME points at {path}, which is not a file"
            );
            // `include_bytes!` resolves relative to the generated file in OUT_DIR,
            // so bake an ABSOLUTE path; re-run if the archive's bytes change.
            let absolute = archive
                .canonicalize()
                .unwrap_or_else(|err| panic!("cannot resolve {path}: {err}"));
            println!("cargo:rerun-if-changed={}", absolute.display());

            let pin_literal = match std::env::var("REVL_LSP_EMBED_RUNTIME_PIN") {
                Ok(pin) if !pin.is_empty() => format!("Some({pin:?})"),
                _ => "None".to_string(),
            };

            format!(
                "/// The pinned runtime archive baked into this binary, if the build \
                 supplied one.\n\
                 pub const EMBEDDED_RUNTIME: Option<&[u8]> = Some(include_bytes!({absolute:?}));\n\
                 /// The pin the baked runtime's cache is keyed by, if the build named one.\n\
                 pub const EMBEDDED_RUNTIME_PIN: Option<&str> = {pin_literal};\n",
            )
        }
        _ => "/// No runtime archive was baked into this binary.\n\
              pub const EMBEDDED_RUNTIME: Option<&[u8]> = None;\n\
              /// No baked-in runtime, so no baked-in pin.\n\
              pub const EMBEDDED_RUNTIME_PIN: Option<&str> = None;\n"
            .to_string(),
    };

    std::fs::write(&generated, body)
        .unwrap_or_else(|err| panic!("cannot write {}: {err}", generated.display()));
}
