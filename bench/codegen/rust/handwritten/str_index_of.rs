//! Hand-written comparator for programs/str_index_of.rvl.
//!
//! revl's `Str.indexOf` returns a CODEPOINT index, or -1. That does not
//! require materializing the haystack as chars: find the BYTE offset with
//! the standard two-way searcher, then convert that prefix to a codepoint
//! count. Zero allocations, and the search itself is not naive O(n*m).
fn index_of(hay: &str, needle: &str) -> i64 {
    match hay.find(needle) {
        Some(byte) => hay[..byte].chars().count() as i64,
        None => -1,
    }
}

pub fn str_index_of(hay: String, needles: Vec<String>) -> i64 {
    let mut total = 0i64;
    for nd in needles {
        total = total
            .checked_add(index_of(&hay, &nd))
            .expect("revl: Int overflow");
    }
    total
}
