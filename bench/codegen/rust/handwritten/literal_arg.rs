//! Hand-written comparator for programs/literal_arg.rvl.
//!
//! Every one of these builtins takes a string SLICE. A literal argument is
//! already a `&'static str`; promoting it to an owned `String` first is a
//! heap allocation and a memcpy per call site per iteration, for nothing.
fn index_of(hay: &str, needle: &str) -> i64 {
    match hay.find(needle) {
        Some(byte) => hay[..byte].chars().count() as i64,
        None => -1,
    }
}

pub fn literal_arg(xs: Vec<String>) -> i64 {
    let mut n = 0i64;
    for x in xs {
        if x.starts_with("pre") {
            n = n.checked_add(1).expect("revl: Int overflow");
        }
        if x.ends_with("fix") {
            n = n.checked_add(1).expect("revl: Int overflow");
        }
        if index_of(&x, "mid") >= 0 {
            n = n.checked_add(1).expect("revl: Int overflow");
        }
        let joined = {
            let mut s = String::with_capacity(x.len() + 1);
            s.push_str(&x);
            s.push_str("!");
            s
        };
        n = n
            .checked_add(joined.chars().count() as i64)
            .expect("revl: Int overflow");
    }
    n
}
