"""Driver sources the cross-tier kernel harness writes into its work dir.

Each driver takes `<kernel> <n>` and prints one JSON object with the kernel's
checksum and the PROCESS CPU time its call took. CPU time, not wall time, is
what the harness reports: the same reason `bench/extern_call_cost.py` uses
`time.process_time_ns`, a machine running other lanes moves wall time and not
this number.

They are kept as strings in one place so the harness, which builds them, and a
reader, who checks what was measured, look at the same text.
"""

PY_DRIVER = '''\
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("revl_cross_tier_kernels", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
kernel = getattr(module, sys.argv[2])
n = int(sys.argv[3])
t0 = time.process_time_ns()
value = kernel(n)
cpu_ns = time.process_time_ns() - t0
print(json.dumps({"value": value, "cpu_ns": cpu_ns}))
'''

TS_DRIVER = '''\
import * as kernels from './emitted/kernels.ts'

const [, , name, rawN] = process.argv
const n = BigInt(rawN)
const t0 = process.cpuUsage()
const value = kernels[name](n)
const used = process.cpuUsage(t0)
console.log(JSON.stringify({ value: value.toString(), cpu_ns: (used.user + used.system) * 1000 }))
'''

RUST_MAIN = '''\
mod emitted;

use std::env;

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

fn main() {
    let args: Vec<String> = env::args().collect();
    let n: i64 = args[2].parse().expect("n is an integer");
    let kernel = args[1].as_str();
    let t0 = cpu_ns();
    let value = match kernel {
        "schema_validate" => emitted::schema_validate(n),
        "regex_scan" => emitted::regex_scan(n),
        "vector_rerank" => emitted::vector_rerank(n),
        "policy_eval" => emitted::policy_eval(n),
        "hash_bytes" => emitted::hash_bytes(n),
        "simulate" => emitted::simulate(n),
        "batch_transform" => emitted::batch_transform(n),
        "loop_probe" => emitted::loop_probe(n),
        other => {
            eprintln!("unknown kernel: {other}");
            std::process::exit(2);
        }
    };
    let cpu = cpu_ns() - t0;
    println!("{{\\"value\\":{value},\\"cpu_ns\\":{cpu}}}");
}
'''
