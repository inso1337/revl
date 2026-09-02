//! Hand-written comparator for programs/concat_append.rvl.
//!
//! Both accumulators are rebound over their own value, so the pre-image is
//! unreachable and there is nothing to preserve: a competent rust developer
//! extends the vector and pushes onto the string in place. The `lines` clone
//! for the second loop IS genuine here, because `lines.len()` is read after
//! it, so the comparator keeps it rather than quietly winning on a
//! difference that is not the emitter's fault.
pub fn concat_append(chunks: Vec<Vec<String>>) -> i64 {
    let mut lines: Vec<String> = Vec::new();
    for c in chunks {
        lines.extend(c);
    }
    let mut text = String::new();
    for l in lines.clone() {
        text.push_str(&l);
    }
    (text.chars().count() as i64)
        .checked_add(lines.len() as i64)
        .expect("revl: Int overflow")
}
