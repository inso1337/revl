//! Hand-written comparator for programs/list_index.rvl.
//!
//! Both index reads land in READ-ONLY positions: one side of an equality and
//! the receiver of a prefix probe. Neither needs an owned value, so a
//! competent rust developer indexes and borrows. Cloning the `String` out of
//! the vector to compare it and throw it away is a heap allocation and a
//! memcpy per read.
pub fn list_index(xs: Vec<String>, probe: String) -> i64 {
    let mut hits = 0i64;
    let mut i = 0i64;
    let n = xs.len() as i64;
    while i < n {
        if xs[i as usize] == probe {
            hits = hits.checked_add(1).expect("revl: Int overflow");
        }
        if xs[i as usize].starts_with("k") {
            hits = hits.checked_add(1).expect("revl: Int overflow");
        }
        i = i.checked_add(1).expect("revl: Int overflow");
    }
    hits
}
