//! Hand-written comparator for programs/for_over_local.rvl.
//!
//! `rows` is dead after the loop, so a competent rust developer iterates it
//! by reference (or moves it); cloning the whole vector, and with it every
//! `String` field, buys nothing.
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub id: i64,
    pub tag: String,
}

pub fn for_over_local(n: i64) -> i64 {
    let mut rows: Vec<Row> = Vec::new();
    let mut i = 0i64;
    while i < n {
        rows.push(Row { id: i, tag: String::from("row") });
        i = i.checked_add(1).expect("revl: Int overflow");
    }
    let mut s = 0i64;
    for r in &rows {
        s = s.checked_add(r.id).expect("revl: Int overflow");
    }
    s
}
