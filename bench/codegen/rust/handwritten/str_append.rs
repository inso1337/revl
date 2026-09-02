//! Hand-written comparator for programs/str_append.rvl.
//!
//! Same semantics: concatenate every part in order, return the CODEPOINT
//! length of the result. A competent rust developer appends into the
//! accumulator in place; there is no reason to build a fresh String per part.
pub fn str_append(parts: Vec<String>) -> i64 {
    let mut s = String::new();
    for p in parts {
        s.push_str(&p);
    }
    s.chars().count() as i64
}
