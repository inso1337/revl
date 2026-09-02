//! Hand-written comparator for programs/int_arith.rvl.
//!
//! revl `Int` traps on overflow, so `checked_*().expect(..)` IS the
//! semantics, not emitter waste. This comparator is deliberately identical
//! to the emitted shape: the benchmark exists to confirm the emitter pays
//! nothing EXTRA on pure integer code, i.e. to produce a negative result.
pub fn int_arith(n: i64) -> i64 {
    let mut acc = 0i64;
    let mut i = 1i64;
    while i < n {
        acc = acc
            .checked_add(i.checked_mul(2).expect("revl: Int overflow"))
            .expect("revl: Int overflow")
            .checked_sub(i / 3)
            .expect("revl: Int overflow");
        i = i.checked_add(1).expect("revl: Int overflow");
    }
    acc
}
