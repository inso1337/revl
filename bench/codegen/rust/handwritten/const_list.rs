//! Hand-written comparator for programs/const_list.rvl.
//!
//! The keyword table is a compile-time constant. A competent rust developer
//! writes it once as a `static` and lends it out; nobody rebuilds 35 heap
//! `String`s on every identifier token. revl `List` is PERSISTENT (no
//! in-place mutation exists at the revl level), so a shared immutable table
//! is observationally identical to a fresh one per call.
static KEYWORDS: &[&str] = &[
    "service", "component", "requires", "provides", "config", "let", "effect",
    "undo", "emit", "emission", "provide", "fn", "return", "true", "false",
    "null", "isolate", "intercept", "realm", "in", "with", "handoff", "spawn",
    "every", "after", "subscribe", "type", "use", "pub", "var", "while", "for",
    "of", "if", "else",
];

fn keywords() -> &'static [&'static str] {
    KEYWORDS
}

fn index_of(table: &[&str], needle: &str) -> i64 {
    match table.iter().position(|x| *x == needle) {
        Some(i) => i as i64,
        None => -1,
    }
}

pub fn const_list(words: Vec<String>) -> i64 {
    let mut n = 0i64;
    for w in words {
        if index_of(keywords(), &w) >= 0 {
            n = n.checked_add(1).expect("revl: Int overflow");
        }
    }
    n
}
