//! Hand-written comparator for programs/str_eq_literal.rvl.
//!
//! `String` implements `PartialEq<&str>`, so comparing against a literal is
//! a length check plus a memcmp with no allocation at all.
pub fn str_eq_literal(xs: Vec<String>) -> i64 {
    let mut n = 0i64;
    for x in xs {
        if x == "alpha" {
            n = n.checked_add(1).expect("revl: Int overflow");
        }
        if x == "beta" {
            n = n.checked_add(2).expect("revl: Int overflow");
        }
        if x != "gamma" {
            n = n.checked_add(3).expect("revl: Int overflow");
        }
    }
    n
}
