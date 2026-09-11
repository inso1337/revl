//! The declared-`Secret[T]` registry, the seam failure funnel and the console
//! funnel (item 421 F6, F5, F6(c)).
//!
//! This file is a fixed part of the runner crate, not generated code. The
//! emitted `components` module imports it (`use crate::confidential::*;`) when
//! the document declares a `Secret[T]`, so the plugin closures that register a
//! value and the runner's own printers that write one out read and write the
//! same process-global registry.
//!
//! Everything here is reachable from either half when secret mode is on and
//! from the runner alone when it is off, so a build that declares no
//! `Secret[T]` leaves some of it unused rather than warning about it.

#![allow(dead_code)]

// ---- declared Secret[T] confidentiality (item 421 F6) ----------------
//
// A declared marking registers its value here, at every end the IR carries it:
// revl_mark_secret at the head of the plugin closure for a config field declared
// Secret[T] (the value the operator supplied at load), at the head of a provide
// method that declares a Secret[T] parameter (the receiver), and
// revl_secret_result around an extern whose declared return was Secret[T] (the
// origin). Every free-form runtime sink this tier keeps reads through
// revl_redact_text: the host trace revl_stream_record gathers, the WAL
// descriptor arguments under --record, and -- item 421 F6(c) -- the channels the
// RUNNER writes itself, which pass through no emitted sink at all. The runner
// reaches those through this module, so the registry has to be reachable from
// it; it lives in this file (a fixed part of the runner crate) rather than being
// emitted, and the emitted half imports it. A unit that declares no Secret[T]
// still emits nothing extra and is byte-identical to before.

// Must equal confidential.REDACTED on the py tier: a polyglot composition
// redacts to the SAME marker whichever tier wrote the line.
pub const REVL_REDACTED_SECRET: &str = "<redacted:secret>";

// A remembered value has to be long enough that an exact match means something.
// Below this it is a coin flip against ordinary trace data ("", "1", "ok"), and
// blanket-erasing those would gut the trace for no confidentiality gain. Same
// bound as the py tier's _MIN_MARKABLE and the go tier's revlMinMarkable.
const REVL_MIN_MARKABLE: usize = 4;

// Process-wide, so a value registered by one plugin's load is scrubbed from a
// sink another component writes. Longest first, so a needle containing another
// leaves no tail behind (the ordering the go/java tiers keep too).
static REVL_SECRET_VALUES: std::sync::OnceLock<std::sync::Mutex<Vec<String>>> =
    std::sync::OnceLock::new();

fn revl_secret_values<R>(f: impl FnOnce(&mut Vec<String>) -> R) -> R {
    let cell = REVL_SECRET_VALUES.get_or_init(|| std::sync::Mutex::new(Vec::new()));
    let mut guard = cell.lock().unwrap_or_else(|e| e.into_inner());
    f(&mut guard)
}

// revl_renderings is every face one string value wears inside host text (item
// 421 F6(d)).
//
// The match in revl_redact_text is EXACT, and the text it runs over has already
// been RENDERED. A value that holds a quote, a backslash or a control character
// comes back ESCAPED from any encoder this tier renders it with: the seam wire
// writes it through serde_json, and a diagnostic a host builds with `{:?}`
// writes the Debug form, so the raw bytes match nothing there and a
// `Secret[Str]` holding such a value crossed verbatim while the identical value
// without the quote was scrubbed everywhere. Registering the encoders' bodies
// closes that.
//
// Derived from the encoders themselves rather than restating their escape
// tables, so a face cannot drift from the encoder that writes it. Both quote
// the result, hence the slices.
fn revl_renderings(text: &str) -> Vec<String> {
    let mut faces = vec![text.to_string()];
    if let Ok(quoted) = serde_json::to_string(text) {
        if quoted.len() >= 2 {
            let body = &quoted[1..quoted.len() - 1];
            if body != text {
                faces.push(body.to_string());
            }
        }
    }
    let debug = format!("{:?}", text);
    if debug.len() >= 2 {
        let body = &debug[1..debug.len() - 1];
        if body != text && !faces.iter().any(|face| face == body) {
            faces.push(body.to_string());
        }
    }
    faces
}

// revl_remember_secret is the raw registration primitive. It is public because
// the emitted half reaches the registry through a glob import, and the origin
// door's leaf walk registers a container's own face with it directly.
pub fn revl_remember_secret(text: String) {
    if text.len() < REVL_MIN_MARKABLE {
        return;
    }
    // The bound gates the RAW value only: an escape can only ever expand, so a
    // value that cleared it clears it in every escaped face too.
    revl_secret_values(|values| {
        for face in revl_renderings(&text) {
            if !values.iter().any(|known| *known == face) {
                values.push(face);
            }
        }
        values.sort_by(|a, b| b.len().cmp(&a.len()));
    });
}

// revl_mark_secret registers a declared-Secret value (the config and receiver ends).
pub fn revl_mark_secret<T: std::fmt::Display>(value: &T) {
    revl_remember_secret(value.to_string());
}

// revl_secret_result registers a declared-Secret return and hands it back
// unchanged, so a call site wraps with no change in meaning (the origin end).
pub fn revl_secret_result<T: std::fmt::Display>(value: T) -> T {
    revl_remember_secret(value.to_string());
    value
}

// A declared-Secret value that is NOT a scalar has no `Display` on this tier:
// `Bytes` lowers to `Vec<u8>` and `List[Str]` to `Vec<String>`, and rust
// implements `Display` for neither, so the two doors above cannot take them at
// all (item 421 F6(f)). `Debug` is the one formatter every type a `Secret[T]`
// lowers to implements -- the std containers, the emitted records and variants,
// and cordis `Value` -- so the container's Debug form is registered here,
// through the same `revl_renderings` the scalar doors use.
//
// That form is the CONTAINER's face, and `revl_redact_text` matches an exact
// needle, so it does not cover a sink that writes one element on its own
// (`stream.emit(leaves[0])`), which is a form an emitted sink can and does
// write (item 421 F6(i), the same gap the go tier closed by walking the value
// with `revlRegisterValue`). Rust has no reflection, so the leaves are
// registered by the door instead: `_secret_face_lines` in the emitter resolves
// the declared surface type at emit time and emits one registration per
// reachable leaf, each through the door that leaf's own type takes.
pub fn revl_mark_secret_encoded<T: std::fmt::Debug + ?Sized>(value: &T) {
    revl_remember_secret(format!("{:?}", value));
}

pub fn revl_secret_result_encoded<T: std::fmt::Debug>(value: T) -> T {
    revl_remember_secret(format!("{:?}", value));
    value
}

// `Bytes` carries a second face the Debug form does not contain: a sink that
// prints the payload writes the decoded text, and the wire carries the json
// array. Both are registered, the same pair the go tier registers for a byte
// slice (item 421 F6(e)). Concrete in `&[u8]`, so the json face comes from the
// encoder itself rather than from a bound `Value` does not satisfy.
pub fn revl_mark_secret_bytes(value: &[u8]) {
    revl_remember_secret(String::from_utf8_lossy(value).into_owned());
    if let Ok(json) = serde_json::to_string(value) {
        revl_remember_secret(json);
    }
    revl_remember_secret(format!("{:?}", value));
}

pub fn revl_secret_result_bytes(value: Vec<u8>) -> Vec<u8> {
    revl_mark_secret_bytes(&value);
    value
}

// revl_redact_text replaces every registered secret in free-form host text.
pub fn revl_redact_text(text: String) -> String {
    revl_secret_values(|values| {
        let mut text = text;
        for needle in values.iter() {
            if !needle.is_empty() && text.contains(needle.as_str()) {
                text = text.replace(needle.as_str(), REVL_REDACTED_SECRET);
            }
        }
        text
    })
}

// revl_forget_secrets drops every remembered value (test isolation).
pub fn revl_forget_secrets() {
    revl_secret_values(|values| values.clear());
}

// ---- the seam failure funnel (item 421 F5) ---------------------------
//
// The cross-process error channel. A served method returning Result<T, E> whose
// E quotes the call's own arguments or a held credential is serialized into the
// reply's value channel by revl_seam_failure before it leaves this process: the
// same two-stage contract the py, ts, go and java tiers implement, in the same
// order. Stage 1 replaces the call's OWN argument values -- the caller's bytes
// crossing back -- and stage 2 the values a declared Secret[T] registered. A
// failure that quotes a registered credential the call was NOT made with is
// stage 2's case and stage 1 cannot see it, which is why both stages run.
//
// The registry is in this file rather than emitted because the runner needs it
// too: rust (native) has no reflection, so the process-global registry is
// populated by the emitted code itself, and the runner writes console channels
// (load, serve, probe, the boot failure) that no emitted sink sees. Emitting the
// registry into the generated module would leave the runner's own printers with
// nothing to consult -- which is exactly the hole item 421 F6(c) closed. Both
// halves therefore share this one registry, and the generated half reaches it
// with a glob import.
//
// Rust's error channel is the value channel: `handle_conn` always replies
// {"ok": true, "value": ...}, and a Result's Err is encoded as the canonical
// {"$kind": "Err", "$value": ...}. So the funnel runs over the DECODED error
// value's string leaves and the reply is rendered afterwards. Scrubbing the
// rendered JSON instead would match nothing for a value holding a quote or a
// backslash -- the escape-before-redact hole of F6(d) -- so the order here is
// deliberate.

// Must equal confidential.REDACTED_ARG on the py tier and bridge.RedactedArg on
// the go tier: a polyglot seam produces the SAME marker whichever tier answered.
pub const REVL_REDACTED_ARG: &str = "<redacted:arg>";

// The length below which an argument is left alone: a shorter substring match is
// a coin flip against ordinary English and replacing it would shred the
// diagnostic for no confidentiality gain. Same bound as the go tier's
// minMatchableArg.
const REVL_MIN_MATCHABLE_ARG: usize = 3;

// revl_arg_needles collects the string forms an argument can take inside error
// text. Booleans and null are skipped (their renderings are ordinary words); an
// object's KEYS are skipped too, because they are field names the author wrote
// rather than the caller's data.
fn revl_arg_needles(value: &serde_json::Value, into: &mut Vec<String>) {
    match value {
        serde_json::Value::String(s) => {
            if s.chars().count() >= REVL_MIN_MATCHABLE_ARG {
                into.push(s.clone());
            }
        }
        serde_json::Value::Number(n) => {
            let spelled = n.to_string();
            if spelled.chars().count() >= REVL_MIN_MATCHABLE_ARG {
                into.push(spelled);
            }
        }
        serde_json::Value::Array(items) => {
            for item in items.iter() {
                revl_arg_needles(item, into);
            }
        }
        serde_json::Value::Object(map) => {
            for (_key, item) in map.iter() {
                revl_arg_needles(item, into);
            }
        }
        _ => {}
    }
}

// revl_seam_failure is the error text a provider-side failure is allowed to send
// back, with this call's own argument values replaced by REVL_REDACTED_ARG and
// every registered secret value replaced by REVL_REDACTED_SECRET. Longest needle
// first, so one that contains another leaves no tail behind.
pub fn revl_seam_failure(text: String, args: &[serde_json::Value]) -> String {
    let mut needles: Vec<String> = Vec::new();
    for arg in args.iter() {
        revl_arg_needles(arg, &mut needles);
    }
    // Longest first, and equal-length needles ordered by value so a duplicate is
    // adjacent and `dedup` can drop it -- the set the go tier collects.
    needles.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
    needles.dedup();
    let mut text = text;
    for needle in needles.iter() {
        text = text.replace(needle.as_str(), REVL_REDACTED_ARG);
    }
    revl_redact_text(text)
}

// revl_funnel_err_value runs the funnel over every string leaf of a decoded
// error value, leaving the shape alone: an error type that is a plain String
// (the common `Result<T, Str>`) is one leaf, and a structured one has each field
// it quotes scrubbed. A value that is not a string carries no caller bytes.
pub fn revl_funnel_err_value(value: &mut serde_json::Value, args: &[serde_json::Value]) {
    match value {
        serde_json::Value::String(s) => {
            let text = std::mem::take(s);
            *s = revl_seam_failure(text, args);
        }
        serde_json::Value::Array(items) => {
            for item in items.iter_mut() {
                revl_funnel_err_value(item, args);
            }
        }
        serde_json::Value::Object(map) => {
            for (_key, item) in map.iter_mut() {
                revl_funnel_err_value(item, args);
            }
        }
        _ => {}
    }
}
