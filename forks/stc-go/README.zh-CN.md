# stc-go

[English](README.md) | **简体中文**

[![CI](https://github.com/0xdenny218/stc-go/actions/workflows/ci.yml/badge.svg)](https://github.com/0xdenny218/stc-go/actions/workflows/ci.yml)
[![Go Reference](https://pkg.go.dev/badge/github.com/0xdenny218/stc-go.svg)](https://pkg.go.dev/github.com/0xdenny218/stc-go)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**时空可组合性范式（spatiotemporal composability）的 Go 实现** —— 即
[Cordis](https://github.com/cordiverse/cordis)（TypeScript）与
[DeepSeek Harness（dsh）](https://github.com/deepseek-ai/deepseek-harness)
（DeepSeek「一切皆插件」的 agent harness）背后的编程模型。

stc-go 以论文
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)
（钉定 `948a07b`，2026-08-14 草稿）为唯一规格——**不是** Cordis 的移植。
Cordis（钉定 `8cc9e33`）仅作语义参考与测试场景语料库。验收标准是论文
§4.4 的五条元理论定理，逐条落成 property-based 测试。

## 范式一瞥

- **时间可组合性**：组件装载时注册的每个副作用都携带逆操作，卸载时按
  LIFO 逆序精确回卷（revertible effects）。
- **空间可组合性**：组件声明依赖（inject），运行时响应式地管理依赖的
  满足与失效，fiber 据此在 Pending/Loading/Active/Unloading 之间转移
  （reactive coeffects）。
- 两者统一在单一 **context** 类型上：context 既是服务容器，也是副作用
  累加器。

这让插件宿主能够热重载组件（Go 或 WASM）并获得可证明的清理保证：
无泄漏订阅、无残留服务、无悬挂状态，且依赖方组件自动级联重载。

## 安装

```sh
go get github.com/0xdenny218/stc-go
```

WASM 组件装载（可选）在子包中：

```go
import "github.com/0xdenny218/stc-go/wasm"
```

## 快速上手

```go
package main

import (
	"context"
	"fmt"

	stc "github.com/0xdenny218/stc-go"
)

var greeting = stc.NewKey[string]("greeting")

func main() {
	root := stc.New()
	defer root.Close()

	// 先装载的消费者停在 Pending：它的依赖尚未满足。
	consumer := root.Load(stc.Component{
		Name:   "consumer",
		Inject: []stc.Key{greeting},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			msg, err := stc.Service[string](c, greeting)
			if err != nil {
				return nil, err
			}
			fmt.Println("consumer saw:", msg)
			return nil, nil
		},
	})

	// Provide 自动注册撤销效应，卸载时自动回卷。
	root.Load(stc.Component{
		Name:    "provider",
		Provide: []stc.Key{greeting},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			_, err := c.Provide(greeting, "hello, spatiotemporal world")
			return nil, err
		},
	})

	// 仅在 greeting 被提供后转为 Active（定理：Ordering）。
	if err := consumer.Ready(context.Background()); err != nil {
		panic(err)
	}
}
```

## 理论对应表（论文 §5.1 Table 2）

| 论文构造 | 符号 | stc-go |
|---|---|---|
| context（第一类上下文） | Γ∞ | `Context`（树状作用域，`New`/`Child`） |
| 可逆效应 | e ∈ 𝔈Γ | `Context.Effect(install)` |
| 上下文读写 | get(k) / set(k,v) | `Context.Get` / `Context.Set` |
| 服务提供 | provide | `Context.Provide`（自动注册撤销效应） |
| 协效应读取 | d（inject） | `Component.Inject` + `stc.Service[T]` |
| 隔离 | isolate(k, r) | `Context.Isolate(key, realm)` |
| 拦截 | intercept(k, ν) | `Context.Intercept(key, meta)` |
| 组件实例 | ⟨d,p,e,π,σ,τ,θ⟩ | `Fiber`（`Load` 创建，`Dispose` 撤退） |
| 注册表 | dom(Fγ) | 每棵树一份 fiber 注册表，`Context.Fibers()` 快照枚举 |
| fiber 状态 | τ | `Pending → Loading → Active → Unloading → (Pending \| Failed)`；显式 `Dispose` → `Gone` |

## 生命周期契约要点

- **`Close` 仅限根 context**：关停 orchestrator 并回卷整棵树；非根作用域的
  子树清理用 `Release`（不动系统）。
- **同键服务换血必须等旧提供者完全撤退**（`Dispose` 后 `Gone` 返回）再装载
  新提供者；重叠窗口内的重复提供被 `ErrDuplicateProvide` 拒绝
  （论文 Def.58 良构性的 fail-fast 强制）。
- **`Fiber.Context()` 返回当前装载周期的 context**；惯性重载会更换它，
  读到上一周期（已回卷）的 context 是合法的竞态结果。
- **`Gone()` 在 fiber 出册（Gone 或 Failed）时返回**；**`Ready()`** 在
  Active / Failed / Gone 时返回（分别对应 nil / 装载错误 / `ErrDisposed`）。
- **`Context.Fibers()` 枚举本树注册表**：在册未撤退 fiber 的只读快照，
  按 ID 升序（每个 `New()` 一份注册表；已 Dispose 与装载失败的 fiber
  均已出册，只会在之后的快照中消失——消费方无需再自建登记面）。
- **`Load` 是异步的**：立即返回句柄，`Apply` 随后由 orchestrator 执行。
  若某 fiber 的 `Apply` 立即消费兄弟 fiber 提供的能力（例如启动服务
  循环），先等提供者的 `Ready` 再装载它——否则它的首批动作可能看到
  一个尚未注册完成的世界（启动编排顺序）。

## 验收 = 五条元理论定理（论文 §4.4）

`property_test.go` 逐条落实为 property-based 测试：

| 定理 | 性质 |
|---|---|
| T59 Preservation | 任意操作后注册表良构不变量保持 |
| T61 Recovery exactness | 撤销 fiber 后的状态 ≡ 其从未装载的状态 |
| T63 Ordering | fiber 仅在依赖就绪后进入 Loading |
| T66 Progress | 有界 orchestrator 步数内达到静默 |
| T73 Confluence | 静默终态与调度顺序无关（`-race` + 随机调度） |

```sh
go test -race ./...
go test -run Property -fuzz FuzzInterleaving -fuzztime 10s ./...
```

## WASM 组件装载（`stc-go/wasm`）

模块实例化 = 引入，模块关闭 = 撤销（论文 §6.4 的运行时代码路线）：
fiber 的依赖门控、惯性锁、精确回卷对 WASM 组件与 Go 组件一视同仁。

- `wasm.Runtime` 封装 [wazero](https://github.com/tetratelabs/wazero)
  （解释器配置，平台无关）与 `stc` 宿主模块；`wasi_snapshot_preview1`
  始终实例化，工具链产物开箱即用。guest 经导出函数 `start()/stop()`
  参与生命周期（reactor 模式的 `_initialize()` 会在 start 前调用）；
  宿主函数 `provide/get/get_size/log` 在 fiber 自己的 context 上登记
  服务——卸载回卷由核心机制保证，guest 无需自登记清理。
- `wasm.Load` 先探针（编译+试实例化）再装载；`Handle.Update` 实现
  原子换血：探针失败旧版本原样保留，start trap 自动用旧字节回滚。
- `Handle.Call(ctx, name, arg)` 以全字符串 ABI 调用 guest 导出函数
  （guest 导出 `stc_alloc`；被调函数收 `(ptr, len)`、返回打包为
  `(ptr<<32)|len` 的结果；可选 `stc_free` 释放入参与结果两块缓冲）。
  Call 与 Update 持同一把锁：进行中的调用完整跑完、Update 等待，
  Update 落定后的调用走新版本。
- 验收（`wasm/wasm_test.go`、`wasm/call_test.go`）：HMR 三契约
  （重载、跨边界依赖链、失败回滚）+ 规格 Test/WasmRollback +
  T61 跨边界卸载精确性 + Call 契约（往返、缓冲释放、坏构建旧版本
  服役、-race 下 Update–Call 互斥）。
- 测试 guest 为手写 WASM 二进制（`guest_test.go` 的微型编码器），
  零工具链依赖。

### 用 Go 写 guest（`stc-go/guest`）

guest 就是普通的 Go，用 [TinyGo](https://tinygo.org/) 对着 guest 侧
SDK 编译：

```go
//go:build wasm

package main

import "github.com/0xdenny218/stc-go/guest"

//export start
func start() {
	_ = guest.Provide("wasm-message", "hello from a guest")
}

//export stop
func stop() { guest.Log("bye") }

func main() {} // reactor 模式：入口是 start/stop
```

```sh
tinygo build -target wasip1 -buildmode=c-shared -o guest.wasm .
```

host→guest 调用的 `stc_alloc`/`stc_free` 与 `invoke` 入口由 SDK 自己
导出——guest 只需注册处理器，无需额外 `//export` 样板：

```go
func init() {
	guest.OnInvoke(func(args string) string { return `{"echo":` + args + `}` })
}
```

宿主侧经 `handle.Call(ctx, "invoke", args)` 触发调用。

### 热重载（`stc-go/hmr`）

`hmr.Watch(ctx, handle, "guest.wasm")` 监听文件（目录级，兼容原子保存
的 rename 形态），防抖后对每次变化执行 `Handle.Update`。更新失败时
旧版本继续服役；结果经 `OnReload` 回调上报。

### 监听原语（`stc-go/watch`）

`watch.Watch(ctx, path, opts)` 是 hmr 背后的极简防抖监听原语：文件或
目录上的事件停歇一个防抖窗口（默认 200ms）后，`OnFire` 以窗口内最后
事件的路径与类别（create/write/remove/rename）回调一次。刻意不做
diff、不带领域语义——fire 意味着什么由消费方决定（stat 定
reload/gone，或全量扫目录做装/卸差分）。文件形态监听所在目录（原子
保存 rename 安全）；目录形态监听目录自身。经回流评审从 stc-agent
skills 包的两套手写 fsnotify 循环提取（
[#6](https://github.com/0xdenny218/stc-go/issues/6)）。

## 稳定注册表（`stc-go/registry`）

**稳定注册表**模式：一个 fiber 提供键身份永不变的注册表（Inject 为
空 → 挺过一切级联重载），成员 fiber 以可逆效应登记自己（逆 = 注销）。
消费方按用读取当前视图，成员增删从不引发消费方重载——
[stc-agent](https://github.com/0xdenny218/stc-agent) 对话中途热替换
工具而 agent 循环不感知，靠的就是它。

```go
var KeyTools = stc.NewKey[*registry.Registry[Tool]]("tools")

// 一个稳定的提供者 fiber
root.Load(registry.Component[Tool]("toolset", KeyTools))

// 每个成员 fiber：登记 = 可逆效应
stc.Component{
	Name:   "tool:" + t.Name,
	Inject: []stc.Key{KeyTools},
	Apply: func(c *stc.Context) (stc.Inverse, error) {
		ts, err := stc.Service[*registry.Registry[Tool]](c, KeyTools)
		if err != nil {
			return nil, err
		}
		return ts.Register(t.Name, t), nil
	},
}
```

`Register` 返回注销逆（幂等）；同名再登记覆盖旧值，被覆盖的旧逆
不会误删新值。`Lookup` / `List` / `Names` 读当前视图（`List` 按名
排序）。该包经回流评审从 stc-agent 的两处独立使用（工具注册表与
斜杠命令注册表）提取（
[#2](https://github.com/0xdenny218/stc-go/issues/2)）。

## 示例应用

[`examples/plugin-http`](examples/plugin-http) 是一个插件式 HTTP
服务器：路由是可逆效应（卸载精确摘除）、服务重提供触发级联重载、
TinyGo WASM guest 经 Go 桥插件对外提供字符串、重编译即热重载。
开箱可跑（目录内 `go run .`，已提交预构建 guest）；导览见该目录
README。

## 与 Cordis / DeepSeek Harness 的关系

时空可组合性范式只有一份规格（论文），有多个实现：

| 项目 | 语言 | 角色 |
|---|---|---|
| [cordiverse/paper](https://github.com/cordiverse/paper) | — | 规格（preprint，活跃修订中） |
| [cordiverse/cordis](https://github.com/cordiverse/cordis) | TypeScript | 参考实现；驱动 [Koishi](https://koishi.chat) 插件生态 |
| [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | TypeScript | DeepSeek 的 agent harness（dsh），「一切皆插件」，由 Cordis 驱动 |
| **stc-go**（本仓库） | Go | 独立实现；论文为规格、五定理为验收 |

如果你在 Go 里构建插件系统、agent harness 或热重载宿主，想要 Cordis
在 TypeScript 世界提供的同等组合保证（依赖门控装载、精确效应回卷、
依赖方响应式重载），stc-go 就是这个库。

刻意不从 Cordis 移植的内容：四种事件派发模式
（`emit/parallel/serial/bail/waterfall`）、带配置 schema 的
`ctx.plugin()`、`hmr`/`loader` 卫星包——它们是 Cordis 生态关切，
不是范式核心。Go 侧的替代均按惯用法设计：显式类型化访问器替代
`Proxy` + declaration merging；静态组件注册替代 Go plugin 包
（无法卸载）；运行时装载的代码走 WASM。

## 相邻项目

stc-go 所处的层——**运行期依赖响应与可证明的卸载语义**——目前在 Go 生态
是空位。相邻项目解决的是相邻的层，与 stc-go 互补而非竞争：

| 层 | 项目 | 解决什么 | 不解决什么 |
|---|---|---|---|
| 启动期依赖注入 | [uber/fx](https://github.com/uber-go/fx)、[google/wire](https://github.com/google/wire)、[sarulabs/di](https://github.com/sarulabs/di) | 静态依赖图、应用级生命周期钩子 | 运行期提供/撤销、依赖方级联重载 |
| WASM 插件装载 | [Extism](https://github.com/extism/extism)、[knqyf263/go-plugin](https://github.com/knqyf263/go-plugin) | 沙箱化装载与调用 WASM 插件、代码生成、OCI 分发 | 插件间依赖、精确卸载语义 |
| 进程插件 | [hashicorp/go-plugin](https://github.com/hashicorp/go-plugin) | 子进程 + gRPC 的崩溃隔离 | 原地热替换、依赖图 |
| 开发期热重载 | [air](https://github.com/air-verse/air)、[edwingeng/hotswap](https://github.com/edwingeng/hotswap) | 开发时重启/替换 Go 代码 | 状态保持、失败回滚、依赖追踪 |

stc-go 的 WASM 层构建在 [wazero](https://github.com/tetratelabs/wazero)
之上——与 Extism、go-plugin 同一运行时。层与层互补：带类型的
host↔guest 调用、沙箱加固、OCI 产物分发等机制是自然的后续集成方向，
由真实消费者的需求牵引落地。

## 与论文/Cordis 的已记录偏差

- 无 `Proxy`：协效应访问走显式泛型 `stc.Service[T]`（论文 §6.4 认可的
  编译期路线）。
- 并发模型：单一 RWMutex + 中心 orchestrator goroutine 串行化 fiber
  转移；`Apply`/逆操作在锁外 goroutine 中运行（论文不规定并发模型）。
- 嵌套子 fiber 不随父 fiber 级联卸载（收窄，见项目规格）。
- 同 key 重复 provide 排除在汇合保证之外（对应定理的条件式表述）。
- 效应累加发生在注册点（`Effect`/`Apply` 返回值），未实现论文迭代器式
  的持续 yield——验收场景未依赖，列为后续扩展。

## License

[MIT](LICENSE)
