//! Handwritten native reference for the cross-tier kernel harness (issue #2709).
//!
//! One implementation per kernel, written the way a competent Rust developer
//! writes that shape: byte/char iteration, Vec iteration, checked arithmetic
//! where revl's Int can trap. It computes the same checksum as
//! `bench/cross_tier_kernels/kernels.rvl` for the same `n`, and it is the floor
//! the emitted tiers are compared against.
//!
//! Usage: `cross_tier_kernels_native <kernel> <n>` prints one integer.

use std::env;

// Process CPU time without a crate dependency: the driver reports CPU, not
// wall time, so a machine running other work does not move the number.
#[repr(C)]
struct Timespec {
    tv_sec: i64,
    tv_nsec: i64,
}

extern "C" {
    fn clock_gettime(clk_id: i32, tp: *mut Timespec) -> i32;
}

#[cfg(target_os = "macos")]
const CLOCK_PROCESS_CPUTIME_ID: i32 = 12;
#[cfg(not(target_os = "macos"))]
const CLOCK_PROCESS_CPUTIME_ID: i32 = 2;

fn cpu_ns() -> i64 {
    let mut ts = Timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe {
        clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &mut ts);
    }
    ts.tv_sec * 1_000_000_000 + ts.tv_nsec
}

fn schema_validate(n: i64) -> i64 {
    const DOC: &str = "{\"name\":\"alice\",\"tags\":[\"alpha\",\"beta\",\"gamma\"],\"active\":true,\"score\":42}";
    let chars: Vec<char> = DOC.chars().collect();
    let mut hits = 0i64;
    for _ in 0..n {
        let mut depth = 0i64;
        let mut in_str = false;
        let mut esc = false;
        for &c in &chars {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = !in_str;
            } else if !in_str {
                match c {
                    '{' => depth += 1,
                    '}' => depth -= 1,
                    ':' => hits += depth,
                    ',' => hits += 1,
                    _ => {}
                }
            }
        }
    }
    hits
}

fn match_here(s: &[char], start: usize, len: usize) -> bool {
    if start + 3 > len {
        return false;
    }
    if s[start] != 'a' {
        return false;
    }
    let mut b: i64 = -1;
    for (i, &c) in s.iter().enumerate().skip(start + 1) {
        if c == 'b' {
            b = i as i64;
        }
    }
    if b < 0 {
        return false;
    }
    for &c in s.iter().skip(b as usize + 1) {
        if c == 'c' {
            return true;
        }
    }
    false
}

fn regex_scan(n: i64) -> i64 {
    const TEXT: &str = "abcXabcdXabbbcdXacXabcdXabdXabcd";
    let chars: Vec<char> = TEXT.chars().collect();
    let len = chars.len();
    let mut hits = 0i64;
    for _ in 0..n {
        let mut i = 0usize;
        while i < len {
            if match_here(&chars, i, len) {
                hits += 1;
                i += 4;
            } else {
                i += 1;
            }
        }
    }
    hits
}

fn vector_rerank(n: i64) -> i64 {
    let vectors: Vec<Vec<i64>> = vec![
        vec![3, 1, 4, 1, 5, 9, 2, 6],
        vec![2, 7, 1, 8, 2, 8, 1, 8],
        vec![5, 5, 5, 5, 5, 5, 5, 5],
        vec![1, 0, 0, 0, 0, 0, 0, 0],
        vec![9, 9, 9, 9, 9, 9, 9, 9],
        vec![2, 3, 5, 7, 11, 13, 17, 19],
        vec![1, 2, 4, 8, 16, 32, 64, 128],
        vec![7, 3, 7, 3, 7, 3, 7, 3],
    ];
    let query: Vec<i64> = vec![1, 2, 3, 4, 5, 6, 7, 8];
    let mut best = 0i64;
    let mut best_score = 0i64;
    for _ in 0..n {
        for (v, vec) in vectors.iter().enumerate() {
            let dot: i64 = vec.iter().zip(query.iter()).map(|(a, b)| a * b).sum();
            if dot > best_score {
                best_score = dot;
                best = v as i64;
            }
        }
    }
    best
}

fn policy_eval(n: i64) -> i64 {
    let actions: Vec<i64> = vec![1, 0, 2, 1, 0, 2, 2, 1, 0, 1, 2, 2, 0, 1];
    let thresholds: Vec<i64> = vec![10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140];
    let mut allowed = 0i64;
    for r in 0..n {
        let want = r % 3;
        for (i, &action) in actions.iter().enumerate() {
            if action == want && (r % 150) < thresholds[i] {
                allowed += 1;
            }
        }
    }
    allowed
}

fn hash_bytes(n: i64) -> i64 {
    const TEXT: &str = "the quick brown fox jumps over the lazy dog, then does it again";
    let chars: Vec<char> = TEXT.chars().collect();
    let mut h = 0i64;
    for _ in 0..n {
        h = 5381;
        for &c in &chars {
            h = h.wrapping_mul(33).wrapping_add(c as i64) % 4_294_967_296;
        }
    }
    h
}

fn simulate(n: i64) -> i64 {
    let mut pos = 1000i64;
    let mut vel = 3i64;
    let mut acc = 0i64;
    for _ in 0..n {
        for _ in 0..64 {
            vel -= pos / 100;
            pos += vel;
            if pos < 0 {
                pos = 0;
            }
            if pos > 100_000 {
                pos = 100_000;
            }
            acc = (acc + pos) % 1_000_003;
        }
    }
    acc
}

fn batch_transform(n: i64) -> i64 {
    let rows: Vec<i64> = vec![
        31, 7, 92, 14, 63, 8, 27, 55, 41, 2, 88, 19, 73, 6, 50, 99, 12, 44, 81, 23, 64, 5, 77, 38,
        90, 16, 58, 33, 71, 9, 47, 85,
    ];
    let mut total = 0i64;
    for r in 0..n {
        let mut acc = 0i64;
        let floor = 20 + (r % 9);
        let shift = r % 5;
        for &x in &rows {
            if x > floor && x < 90 {
                acc += x * 3 - 17 + shift;
            }
        }
        total = (total + acc) % 1_000_003;
    }
    total
}

fn loop_probe(n: i64) -> i64 {
    let pins = [1i64, 2, 3, 4, 5, 6, 7, 8];
    let mut acc = 0i64;
    let mut i = 0i64;
    while i < n {
        acc += pins[(i % 8) as usize];
        i += 1;
    }
    acc
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: {} <kernel> <n>", args[0]);
        std::process::exit(2);
    }
    let n: i64 = args[2].parse().expect("n is an integer");
    let kernel = args[1].as_str();
    let t0 = cpu_ns();
    let value = match kernel {
        "schema_validate" => schema_validate(n),
        "regex_scan" => regex_scan(n),
        "vector_rerank" => vector_rerank(n),
        "policy_eval" => policy_eval(n),
        "hash_bytes" => hash_bytes(n),
        "simulate" => simulate(n),
        "batch_transform" => batch_transform(n),
        "loop_probe" => loop_probe(n),
        other => {
            eprintln!("unknown kernel: {other}");
            std::process::exit(2);
        }
    };
    let cpu = cpu_ns() - t0;
    println!("{{\"value\":{value},\"cpu_ns\":{cpu}}}");
}
