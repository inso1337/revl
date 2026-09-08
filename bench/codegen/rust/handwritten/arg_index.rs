//! Hand-written comparator for programs/arg_index.rvl.
//!
//! The needle `needles[i]` lands in a `&str` argument slot (`str::starts_with`
//! takes a pattern, and `&needles[i]` — a `&String` — coerces to `&str`), so a
//! competent rust developer borrows the element in place. Cloning the whole
//! element out of the vector on every read, purely so the `&` has something to
//! point at, is a heap allocation per read. The needles are built once and read
//! in a hot loop, so that per-read clone is what this measures.
pub fn arg_index(needles: Vec<String>) -> i64 {
    let hay = String::from("prefix-match-target-suffix");
    let mut hits = 0i64;
    let mut pass = 0i64;
    let n = needles.len() as i64;
    while pass < 20 {
        let mut i = 0i64;
        while i < n {
            if hay.starts_with(&needles[i as usize]) {
                hits = hits.checked_add(1).expect("revl: Int overflow");
            }
            i = i.checked_add(1).expect("revl: Int overflow");
        }
        pass = pass.checked_add(1).expect("revl: Int overflow");
    }
    hits
}
