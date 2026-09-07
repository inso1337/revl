//! Hand-written comparator for programs/field_index.rvl.
//!
//! The field read `recs[i].tag` lands in a READ-ONLY position (one side of an
//! equality), so a competent rust developer indexes the vector, borrows the
//! element, and reads the field in place. Cloning the whole element out of the
//! vector on every read — just to move one field out and compare it — is a heap
//! allocation and a memcpy per read. The records are built once and read in a
//! hot loop, so that per-read clone is what this measures.
struct Rec {
    tag: String,
}

pub fn field_index(raw: Vec<String>) -> i64 {
    // The record literal owns its field, so `s` is cloned into it exactly as
    // the emitter renders `{ tag: s }` — the build cost is identical on both
    // sides, so the only difference this bench reports is the per-read element
    // clone, which is the shape under test.
    let mut recs: Vec<Rec> = Vec::new();
    for s in raw {
        recs.push(Rec { tag: s.clone() });
    }
    let mut hits = 0i64;
    let mut pass = 0i64;
    let n = recs.len() as i64;
    while pass < 20 {
        let mut i = 0i64;
        while i < n {
            if recs[i as usize].tag == "key" {
                hits = hits.checked_add(1).expect("revl: Int overflow");
            }
            i = i.checked_add(1).expect("revl: Int overflow");
        }
        pass = pass.checked_add(1).expect("revl: Int overflow");
    }
    hits
}
