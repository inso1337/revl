//! Hand-written comparator for programs/slice_scan.rvl.
//!
//! This is the item-277 residual, isolated. The emitted form calls
//! `revl_slice(i, i+1)`, which walks `i` codepoints from the front on every
//! iteration, so the scan is O(n^2).
//!
//! The comparator materializes the codepoints ONCE per call and indexes.
//! Item 277 recorded that doing this per FUNCTION regressed the self-host
//! lexer, because `source` is threaded as a parameter through a dozen
//! per-token helpers and the shadow became per CALL. That is a statement
//! about where the collect is placed, not about whether indexing beats a
//! front walk; this single-function benchmark measures the latter in
//! isolation so the two claims are not confused.
pub fn slice_scan(s: String) -> i64 {
    let cs: Vec<char> = s.chars().collect();
    let len = cs.len() as i64;
    let mut n = 0i64;
    let mut i = 0i64;
    while i < len {
        if cs[i as usize] == 'a' {
            n = n.checked_add(1).expect("revl: Int overflow");
        }
        i = i.checked_add(1).expect("revl: Int overflow");
    }
    n
}
