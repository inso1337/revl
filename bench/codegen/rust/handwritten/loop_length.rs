//! Hand-written comparator for programs/loop_length.rvl.
//!
//! `s` is never rebound inside the loop, so its codepoint length is a loop
//! invariant. A competent rust developer computes `chars().count()` once
//! rather than once per iteration.
pub fn loop_length(s: String) -> i64 {
    let len = s.chars().count() as i64;
    let mut n = 0i64;
    let mut i = 0i64;
    while i < len {
        n = n.checked_add(i).expect("revl: Int overflow");
        i = i.checked_add(1).expect("revl: Int overflow");
    }
    n
}
