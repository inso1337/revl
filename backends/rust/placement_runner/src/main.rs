//! cordis-rs placement runner: one process of a `revl run --placement`
//! composition placed on the Rust (cordis-rs) backend. Reads a spec JSON
//! (argv[1]), consumes cross-process keys through bridge proxies, loads its
//! own cordis-rs components, runs probes, and (unless "once") holds until its
//! stdin closes, then tears down consumers-first.
//!
//! FIRST CUT scope: the `Database` proxy and the component name -> plugin
//! mapping are hand-written for the user_cache composition. Generalizing is
//! emitter work: `backends/rust/emit.py` would generate, per service, a proxy
//! `impl <Svc>` (and a stub dispatcher) and a name->plugin table, since
//! cordis-rs services are static traits (a runtime-generic proxy is impossible
//! in Rust). The transport, spec contract, and lifecycle here are already
//! general; only the typed proxy/stub and the plugin table are per-composition.

use serde_json::{json, Value as J};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;

mod components;
use cordis::Value;
use components::{pg_database, user_cache, Cache, Database};

/// One blocking request/response against a bridge stub over the Unix socket.
fn rpc(socket: &str, key: &str, method: &str, args: Vec<J>) -> J {
    // Retry the connect: under placement the provider and consumer start
    // concurrently, so the provider's socket may not exist yet (matches the
    // py/node connect-retry).
    let mut stream = None;
    for _ in 0..200 {
        match UnixStream::connect(socket) {
            Ok(s) => {
                stream = Some(s);
                break;
            }
            Err(_) => std::thread::sleep(std::time::Duration::from_millis(50)),
        }
    }
    let stream = stream.expect("bridge connect (provider never came up)");
    let mut writer = stream.try_clone().expect("clone stream");
    let mut line = serde_json::to_string(&json!({ "key": key, "method": method, "args": args })).unwrap();
    line.push('\n');
    writer.write_all(line.as_bytes()).expect("write request");
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader.read_line(&mut response).expect("read reply");
    let reply: J = serde_json::from_str(&response).expect("parse reply");
    if !reply["ok"].as_bool().unwrap_or(false) {
        panic!("remote error: {}", reply["error"]);
    }
    reply["value"].clone()
}

/// Hand-written proxy for the `Database` service. One `impl <Svc>` per service
/// is what the emitter would generate; the bodies are mechanical.
struct DatabaseProxy {
    socket: String,
    key: String,
}
impl Database for DatabaseProxy {
    fn query(&self, sql: String) -> Vec<Value> {
        let value = rpc(&self.socket, &self.key, "query", vec![json!(sql)]);
        value
            .as_array()
            .map(|rows| rows.iter().map(|row| Value::new(row.to_string())).collect())
            .unwrap_or_default()
    }
    fn execute(&self, sql: String) -> i64 {
        rpc(&self.socket, &self.key, "execute", vec![json!(sql)])
            .as_i64()
            .unwrap_or(0)
    }
}

fn db_proxy_plugin(key: String, socket: String) -> cordis::PluginHandle {
    cordis::plugin_sync::<(), _>("DatabaseProxy", cordis::Inject::none(), move |ctx, _config| {
        let proxy: Box<dyn Database> = Box::new(DatabaseProxy {
            socket: socket.clone(),
            key: key.clone(),
        });
        ctx.provide(key.as_str(), proxy)?;
        Ok(cordis::PluginOutput::none())
    })
}

/// A driver plugin that requires `cache` and runs the spec's probes in its
/// activation (the runtime has no root-level `require`, so probing rides a
/// plugin). Hand-written for the `Cache` service.
fn probe_plugin(name: String, probes: Vec<J>) -> cordis::PluginHandle {
    cordis::plugin_sync::<(), _>("Probe", cordis::Inject::new(["cache"]), move |ctx, _config| {
        let cache = ctx.require::<Box<dyn Cache>>("cache")?;
        for probe in probes.clone() {
            let key = probe["key"].as_str().unwrap_or("");
            let method = probe["method"].as_str().unwrap_or("");
            let empty = vec![];
            let args: Vec<String> = probe["args"]
                .as_array()
                .unwrap_or(&empty)
                .iter()
                .map(|a| a.as_str().unwrap_or("").to_string())
                .collect();
            match (key, method) {
                ("cache", "put") => {
                    cache.put(args[0].clone(), args[1].clone());
                    println!("[{}] probe | cache.put({:?}, {:?}) -> ()", name, args[0], args[1]);
                }
                ("cache", "get") => {
                    let got = cache.get(args[0].clone());
                    println!("[{}] probe | cache.get({:?}) -> {:?}", name, args[0], got);
                }
                _ => println!("[{}] probe | unsupported {}.{}", name, key, method),
            }
        }
        Ok(cordis::PluginOutput::none())
    })
}

fn main() {
    let spec_path = std::env::args().nth(1).expect("usage: revl_placement_runner <spec.json>");
    let spec: J = serde_json::from_str(&std::fs::read_to_string(&spec_path).expect("read spec"))
        .expect("parse spec");
    let name = spec["name"].as_str().unwrap_or("proc").to_string();
    let log = |channel: &str, subject: &str, detail: &str| {
        println!("[{}] {:<6}| {:<16}| {}", name, channel, subject, detail);
    };

    let root = cordis::Context::new();
    let mut fibers = Vec::new();

    // 1. proxies for keys provided by other processes (Database only, first cut)
    if let Some(proxies) = spec["proxies"].as_object() {
        for (key, info) in proxies {
            let socket = info["socket"].as_str().expect("proxy socket").to_string();
            let service = info["service"].as_str().unwrap_or("Database");
            if service != "Database" {
                log("proxy", key, &format!("UNSUPPORTED service {service} (first cut: Database)"));
                continue;
            }
            let fiber = root.plugin(db_proxy_plugin(key.clone(), socket.clone()), ());
            fiber.wait().unwrap();
            fibers.push((format!("{key}-proxy"), fiber));
            log("proxy", key, &format!("-> {socket} [{service}]"));
        }
    }

    // 2. this process's own components, in the order given
    if let Some(list) = spec["components"].as_array() {
        for entry in list {
            let cname = entry.as_str().unwrap_or("");
            let handle = match cname {
                "user_cache" => user_cache(),
                "pg_database" => pg_database(),
                other => {
                    log("load", other, "UNKNOWN component (first cut: user_cache/pg_database)");
                    continue;
                }
            };
            let fiber = root.plugin(handle, ());
            fiber.wait().unwrap();
            let state = fiber.state();
            fibers.push((cname.to_string(), fiber));
            log("load", cname, &format!("state={state:?}"));
        }
    }

    // 3. probes (ride a driver plugin that requires the probed service)
    let probes: Vec<J> = spec["probe"].as_array().cloned().unwrap_or_default();
    if !probes.is_empty() {
        let fiber = root.plugin(probe_plugin(name.clone(), probes), ());
        fiber.wait().unwrap();
        fibers.push(("probe".to_string(), fiber));
    }

    println!("[{name}] UP");

    // 4. hold until stdin closes (the conductor closes it to stop us), unless
    //    "once" (used by the standalone verification).
    if !spec["once"].as_bool().unwrap_or(false) {
        let mut sink = String::new();
        let _ = std::io::stdin().read_to_string(&mut sink);
    }

    // 5. teardown, consumers first (reverse load order)
    for (label, fiber) in fibers.iter().rev() {
        let _ = fiber.dispose();
        log("swap", label, "dispose");
    }
    println!("[{name}] DOWN");
}
