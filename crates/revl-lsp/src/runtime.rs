//! The private interpreter runtime — how ONE distributed `revl-lsp` reaches a
//! `revl`-capable Python with none installed on the machine (item 336, the
//! one-file bundling slice, issue #102).
//!
//! Slice 1 shipped the honest fallback design A2 records: the reference front
//! end runs as a child process against a `revl` the machine already has (see
//! `engine`). That is a binary PLUS a `revl` alongside it, not the "one
//! distributable file" the item's prose claims. The measured PyOxidizer spike
//! (issue #102, 2026-09-05) was a no-go — pinned to unsupported Python 3.10 and
//! an in-memory importer with no `__file__`, which Revl's filesystem-backed
//! runtime assets require. The path this module takes is the one that spike
//! left standing: a pinned `python-build-standalone` runtime, with the `revl`
//! wheel frozen into its OWN site, carried by the distributable and
//!
//!   * extracted ATOMICALLY into a VERSIONED private cache
//!     (`<cache>/revl-lsp/runtime/<pin>/`) on first use, and
//!   * run in ISOLATED MODE (`-I`), so the worker ignores the machine's
//!     `PYTHONPATH`, its per-user site, and any `python3` on `PATH`, and imports
//!     `revl` only from the private runtime's own site.
//!
//! A second launch reuses the populated cache; two concurrent first launches
//! race on a single atomic rename and the loser reuses the winner's tree, so a
//! half-written runtime is never observed under `<pin>`.
//!
//! This is the SUBPROCESS embodiment of the design's slice-0 embedding. Moving
//! the interpreter in-process (pyo3 linking `libpython`, `PyConfig.isolated`,
//! `Py_InitializeFromConfig`) is the next step and needs no change here: the
//! pin, the atomic versioned cache, and the isolated-init contract are what make
//! the artifact self-contained, and they live here regardless of whether the
//! interpreter is a child process or a linked library. Shipping the pinned
//! `python-build-standalone` archive itself is a distribution/build concern
//! (338), so this module RESOLVES a bundled runtime rather than embedding one:
//! it is a no-op (returns `None`, the current PATH behavior stands) until a
//! distributable actually carries the archive.

use std::path::{Path, PathBuf};
use std::process::Command;

/// A resolved private runtime: the interpreter to launch and the isolation the
/// launch must apply. `isolated` is always `true` for a private runtime — a
/// bundled interpreter must not read the machine's `PYTHONPATH` or user site —
/// and is carried as a field rather than assumed so the engine's launch code
/// reads one flag instead of re-deriving the rule.
pub struct Runtime {
    pub python: String,
    pub isolated: bool,
}

/// The runtime identity the cache is keyed by: bump it whenever the pinned
/// `python-build-standalone` build or the frozen `revl` wheel changes, so a new
/// pairing extracts to a NEW `<pin>` directory instead of colliding with a stale
/// one already on disk (design A3: a stale runtime is the skew hazard). A build
/// that bundles a runtime sets this to the archive's real pin; the default names
/// the unset state so a mis-set cache is legible.
const RUNTIME_PIN: &str = "unpinned-dev";

/// Point the resolver at an ALREADY-EXTRACTED runtime directory (one containing
/// `bin/python3`). This is what the distributable's install layout provides
/// once unpacked, and what a developer sets to test against a real
/// `python-build-standalone` tree without re-taring it.
const RUNTIME_DIR_ENV: &str = "REVL_LSP_RUNTIME";

/// Point the resolver at a pinned runtime ARCHIVE (a `tar` the system `tar` can
/// unpack). It is extracted atomically into the versioned cache on first use.
const RUNTIME_ARCHIVE_ENV: &str = "REVL_LSP_RUNTIME_ARCHIVE";

/// Override the cache root the versioned runtime is extracted under. Defaults to
/// the platform cache dir; set in tests and for a read-only `$HOME`.
const CACHE_ENV: &str = "REVL_LSP_CACHE";

/// Override the pin the cache is keyed by, so a test (or a side-by-side runtime)
/// does not collide with `RUNTIME_PIN`'s directory.
const PIN_ENV: &str = "REVL_LSP_RUNTIME_PIN";

/// Resolve the private runtime this distributable should analyse with, or
/// `None` when there is none to resolve (no runtime env set and no runtime
/// bundled beside the executable) — in which case the caller keeps the current
/// PATH behavior. A runtime that IS configured but cannot be prepared is
/// `Some(Err(_))`: a configured-but-broken runtime fails closed (the engine
/// renders the error as a visible diagnostic) rather than silently degrading to
/// a `python3` on PATH that may be a different — or no — `revl`.
pub fn locate() -> Option<Result<Runtime, String>> {
    if let Some(dir) = env_path(RUNTIME_DIR_ENV) {
        return Some(from_extracted_dir(&dir));
    }
    if let Some(archive) = env_path(RUNTIME_ARCHIVE_ENV) {
        return Some(ensure_extracted(&archive));
    }
    // The real single-file install layout: a runtime unpacked, or an archive,
    // sitting beside the executable. Absent in a bare `cargo` checkout, present
    // in a packaged distributable.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let bundled_dir = dir.join("runtime").join(pin());
            if interpreter_in(&bundled_dir).is_some() {
                return Some(from_extracted_dir(&bundled_dir));
            }
            let bundled_archive = dir.join("runtime.tar");
            if bundled_archive.is_file() {
                return Some(ensure_extracted(&bundled_archive));
            }
        }
    }
    None
}

fn from_extracted_dir(dir: &Path) -> Result<Runtime, String> {
    match interpreter_in(dir) {
        Some(python) => Ok(Runtime {
            python: python.to_string_lossy().into_owned(),
            isolated: true,
        }),
        None => Err(format!(
            "the runtime at {} has no bin/python3 (set {RUNTIME_DIR_ENV} to a \
             python-build-standalone tree, or unset it to use a python on PATH)",
            dir.display()
        )),
    }
}

/// Extract `archive` into `<cache>/revl-lsp/runtime/<pin>/` if it is not already
/// there, atomically, and return the private interpreter inside it. The extract
/// lands in a sibling temp directory and is `rename`d into place in one step, so
/// no reader ever sees `<pin>/` half-populated, and two racing extractors settle
/// on whichever `rename` won.
fn ensure_extracted(archive: &Path) -> Result<Runtime, String> {
    let dest = runtime_dir();
    if let Some(python) = interpreter_in(&dest) {
        // already populated by an earlier launch (or a peer that just won)
        return Ok(Runtime {
            python: python.to_string_lossy().into_owned(),
            isolated: true,
        });
    }
    if !archive.is_file() {
        return Err(format!(
            "the runtime archive {} does not exist",
            archive.display()
        ));
    }
    let parent = dest
        .parent()
        .ok_or_else(|| "the runtime cache has no parent directory".to_string())?;
    std::fs::create_dir_all(parent)
        .map_err(|err| format!("cannot create the runtime cache {}: {err}", parent.display()))?;

    let staging = parent.join(format!(
        ".tmp-extract-{}-{}",
        std::process::id(),
        nonce()
    ));
    // a leftover from a crashed extraction under the same pid+nonce is never
    // reused: clear it first so `tar` writes into a clean tree
    let _ = std::fs::remove_dir_all(&staging);
    std::fs::create_dir_all(&staging)
        .map_err(|err| format!("cannot create the staging dir {}: {err}", staging.display()))?;

    let extracted = extract_into(archive, &staging).and_then(|()| runtime_root(&staging));
    let root = match extracted {
        Ok(root) => root,
        Err(err) => {
            let _ = std::fs::remove_dir_all(&staging);
            return Err(err);
        }
    };

    match std::fs::rename(&root, &dest) {
        Ok(()) => {}
        Err(err) => {
            // A peer extractor won the race: `<pin>/` now exists, populated by
            // it, so drop our staging and use theirs. Any other rename failure
            // is real and surfaced.
            let _ = std::fs::remove_dir_all(&staging);
            if interpreter_in(&dest).is_none() {
                return Err(format!(
                    "cannot move the extracted runtime into {}: {err}",
                    dest.display()
                ));
            }
        }
    }
    let _ = std::fs::remove_dir_all(&staging);

    match interpreter_in(&dest) {
        Some(python) => Ok(Runtime {
            python: python.to_string_lossy().into_owned(),
            isolated: true,
        }),
        None => Err(format!(
            "the runtime archive {} unpacked without a bin/python3",
            archive.display()
        )),
    }
}

fn extract_into(archive: &Path, into: &Path) -> Result<(), String> {
    let status = Command::new("tar")
        .arg("-xf")
        .arg(archive)
        .arg("-C")
        .arg(into)
        .status()
        .map_err(|err| format!("cannot run `tar` to unpack {}: {err}", archive.display()))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!(
            "`tar` failed to unpack {} ({status})",
            archive.display()
        ))
    }
}

/// The runtime root inside a freshly extracted staging tree. An archive may
/// unpack its `bin/`, `lib/` at the top level, or nest them under a single
/// container directory (the shape `python-build-standalone` tarballs use); both
/// are accepted, anything else is reported.
fn runtime_root(staging: &Path) -> Result<PathBuf, String> {
    if interpreter_in(staging).is_some() {
        return Ok(staging.to_path_buf());
    }
    let mut entries: Vec<PathBuf> = std::fs::read_dir(staging)
        .map_err(|err| format!("cannot read the extracted tree {}: {err}", staging.display()))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .collect();
    entries.retain(|path| path.is_dir());
    if let [only] = entries.as_slice() {
        if interpreter_in(only).is_some() {
            return Ok(only.clone());
        }
    }
    Err(format!(
        "the extracted runtime under {} has no bin/python3 at its root",
        staging.display()
    ))
}

/// The interpreter inside a runtime directory: `bin/python3`, or `bin/python`
/// if that is the only launcher present.
fn interpreter_in(dir: &Path) -> Option<PathBuf> {
    for name in ["python3", "python"] {
        let candidate = dir.join("bin").join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

fn runtime_dir() -> PathBuf {
    cache_root()
        .join("revl-lsp")
        .join("runtime")
        .join(pin())
}

fn cache_root() -> PathBuf {
    if let Some(explicit) = env_path(CACHE_ENV) {
        return explicit;
    }
    if let Some(xdg) = env_path("XDG_CACHE_HOME") {
        return xdg;
    }
    if let Some(home) = env_path("HOME") {
        return home.join(".cache");
    }
    std::env::temp_dir()
}

fn pin() -> String {
    std::env::var(PIN_ENV)
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| RUNTIME_PIN.to_string())
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

/// A short, per-call disambiguator for the staging directory name. The full
/// atomicity guarantee is the `rename`, not this; the nonce only keeps two
/// extractions in the same process from sharing a staging path.
fn nonce() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_runtime_configured_resolves_to_nothing() {
        // With no runtime env set, `locate` may only answer from a runtime
        // bundled beside the test binary, which a `cargo` build never has — so
        // the PATH fallback stands and slice-1 behavior is unchanged.
        for var in [RUNTIME_DIR_ENV, RUNTIME_ARCHIVE_ENV] {
            assert!(
                std::env::var_os(var).is_none(),
                "{var} leaked into the unit-test environment"
            );
        }
        assert!(locate().is_none());
    }

    #[test]
    fn a_missing_runtime_dir_fails_closed_rather_than_resolving() {
        let answer = from_extracted_dir(Path::new("/no/such/runtime"));
        assert!(answer.is_err(), "a broken runtime must not resolve silently");
    }

    #[test]
    fn the_pin_can_be_overridden_for_a_side_by_side_cache() {
        // guard the default without perturbing the process env for other tests
        assert_eq!(RUNTIME_PIN, "unpinned-dev");
        assert!(runtime_dir().ends_with(pin()));
    }

    #[test]
    fn the_runtime_dir_is_under_the_cache_root_and_keyed_by_pin() {
        let dir = runtime_dir();
        assert!(dir.starts_with(cache_root()));
        assert!(dir.ends_with(pin()));
        assert!(dir.to_string_lossy().contains("revl-lsp"));
    }
}
