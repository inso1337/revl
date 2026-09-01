# Plugin HTTP server example

A small HTTP server where every feature is a plugin (fiber) on an stc-go
context. It demonstrates the whole stack end to end:

- **Routes as revertible effects** — registering a route returns an inverse;
  unloading a fiber removes its routes exactly (temporal composability).
- **Cascading reloads** — re-providing the `banner` service makes the `hello`
  plugin (which injects it) unload and reload automatically
  (spatial composability).
- **Cross-boundary dependency chains** — a TinyGo-compiled WASM guest
  provides a string service; a Go bridge fiber injects it and serves it as
  `/wasm`.
- **Hot reload** — `hmr.Watch` watches `guest.wasm`; rebuilding the guest
  atomically swaps it, and the bridge fiber reloads with the new value.
  A broken build keeps the old version serving.

## Run

```sh
go run .          # guest.wasm is committed; no toolchain needed
```

Then, in another terminal:

```console
$ curl localhost:8080/hello
hello from banner v1
$ curl localhost:8080/wasm
hello from TinyGo guest v1
$ curl 'localhost:8080/admin/banner?text=v2'
banner="v2"; /hello will serve it after reload
$ curl localhost:8080/hello
hello from banner v2
```

Watch the server logs: each reload prints which fiber was re-applied and
with what values.

## Hot-reload the WASM guest

Requires [TinyGo](https://tinygo.org/) (and `wasm-opt` from binaryen):

```sh
$EDITOR guest/main.go   # change the provided string
make wasm               # rebuilds guest.wasm
curl localhost:8080/wasm # serves the new string — old version swapped out
```

The rebuild triggers `hmr.Watch` → `Handle.Update`: probe, dispose old,
load new. If the new guest fails to build or traps in `start`, the update
fails and the old guest keeps serving (see the server log).

Ctrl-C shuts down: `root.Close()` rewinds every effect — routes removed,
guest `stop` called, server gracefully shut down.

## 中文

插件式 HTTP 服务器示例：路由是可逆效应（卸载精确摘除）；重提供 banner
会让注入它的 hello 插件级联重载；TinyGo 编译的 WASM guest 提供字符串
服务，由 Go 桥插件暴露为 /wasm；`make wasm` 重编译后 hmr 自动原子换血，
构建失败则旧版本继续服役。运行 `go run .` 后按上文 curl 观察，
服务器日志会打印每个 fiber 的重载过程。Ctrl-C 触发 root.Close()，
全部效应精确回卷。
