//! Hand-written comparator for programs/list_append.rvl.
//!
//! Same semantics, including revl's trapping `Int` arithmetic.
pub fn list_append(n: i64) -> i64 {
    let mut out: Vec<i64> = Vec::new();
    let mut i = 0i64;
    while i < n {
        out.push(i.checked_mul(3).expect("revl: Int overflow"));
        i = i.checked_add(1).expect("revl: Int overflow");
    }
    out.len() as i64
}
