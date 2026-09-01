# stc-go：时空可组合性的 Go 实现——从一篇 PL 论文到一个定理验证的库

*2026-08-17 · v0.2.0 · [github.com/0xdenny218/stc-go](https://github.com/0xdenny218/stc-go)*

今年早些时候，DeepSeek 开源了 [DeepSeek Harness（dsh）](https://github.com/deepseek-ai/deepseek-harness)，
一个建立在「**一切皆插件**」之上的 agent harness：工具、记忆、模型客户端、
UI——每个部件都是可以在运行时装载、卸载、热替换的组件，且系统不丢一致性。

dsh 的底座是 [Cordis](https://github.com/cordiverse/cordis)——驱动 Koishi
插件生态的 TypeScript 框架。而 Cordis 本身是一篇论文的实现：
[*A Programming Paradigm for Spatiotemporal Composability*](https://github.com/cordiverse/paper)。

**stc-go 是同一范式的 Go 实现**——以论文为规格（不是 Cordis 的移植），
验收标准是论文自己的五条元理论定理，逐条落成 property-based 测试。

## 范式一段话

插件系统传统上在两个方向上出问题。**时间**：卸载插件时它的副作用——
订阅、定时器、注册项——会泄漏，因为没有任何机制记录"怎么撤销"。
**空间**：插件的依赖消失又出现（配置变更、热替换）时，没有可靠机制通知
依赖方重载；就算有，重载的时机和顺序也是错的。

论文的答案是单一 **context** 类型：既是服务容器，也是*副作用累加器*。
每个效应注册时携带逆操作，卸载按 LIFO 逆序精确回卷（**可逆效应**——
时间可组合性）；组件声明依赖（inject），运行时跟踪依赖的满足与失效，
让每个组件实例（**fiber**）在 `Pending → Loading → Active → Unloading`
之间受控转移（**响应式协效应**——空间可组合性）。

## 为什么 Go 是另一回事

Cordis 大量借力 JavaScript：`Proxy` 做魔法般的服务访问、declaration
merging 做类型化协效应、单线程事件循环让整类竞态根本无法表达。
Go 一样都没有：

- **没有 `Proxy`** → 服务访问走显式泛型访问器
  `stc.Service[T](ctx, key)`——论文 §6.4 明确认可的编译期路线；
- **plugin 包无法卸载** → 组件一律静态注册；运行时装载的代码走
  **WASM**（经 [wazero](https://github.com/tetratelabs/wazero)），
  *实例化即引入、关闭即撤销*；
- **真并发** → fiber 状态转移由中心 orchestrator goroutine 串行化，
  `Apply` 与逆操作在锁外的独立 goroutine 执行，完成结果作为命令回流。
  论文不规定并发模型；这套模型让它的汇合定理在 `-race` 下可测。

## 验收 = 定理，不是感觉

大多数"框架重写"跑通示例就宣布胜利。stc-go 的验收是论文 §4.4 的五条
元理论定理，每条落成一个 property 测试（`property_test.go`），
在随机生成的操作序列上、全程 `-race` 运行：

| 定理 | 检验的性质 |
|---|---|
| T59 Preservation | 任意操作后注册表良构不变量保持 |
| T61 Recovery exactness | 撤销 fiber 后的状态 ≡ 它从未装载的状态 |
| T63 Ordering | fiber 仅在依赖就绪后进入 Loading |
| T66 Progress | 有界步数内达到静默 |
| T73 Confluence | 静默终态与调度顺序无关 |

此外还有 Go 原生 fuzzing 跑随机交错（`go test -fuzz FuzzInterleaving`），
以及一场 **84 个 agent 的对抗性评审**：实证复现了 33 项发现，其中 5 个是
示例驱动开发永远不会暴露的真实严重缺陷——先订阅后检查窗口里的丢失唤醒、
fiber context 数据竞争、关停期的孤儿 fiber、全局 Close 的误用脚枪、
重复提供的良构性漏检。全部修复并各配回归测试，沉淀出的生命周期契约
写在 README 里。

## 眼见为实：插件式 HTTP 服务器 + WASM 热重载

[`examples/plugin-http`](https://github.com/0xdenny218/stc-go/tree/main/examples/plugin-http)
是一台每个功能都是 fiber 的小服务器：

- 路由注册是可逆效应——fiber 卸载时路由被*精确*摘除；
- 管理端点重提供 `banner` 服务，注入它的 hello 插件自动级联重载；
- 一个 **TinyGo 编译的 WASM guest**（基于新的
  [`guest` SDK](https://github.com/0xdenny218/stc-go/tree/main/guest)）
  提供字符串服务，Go 桥插件注入后暴露为 `/wasm`；
- 新的 [`hmr` 包](https://github.com/0xdenny218/stc-go/tree/main/hmr)
  监听 `guest.wasm`，每次重编译原子换血正在运行的 guest。

一次真实会话：

```console
$ curl localhost:8080/wasm
hello from TinyGo guest v2
$ $EDITOR guest/main.go && make wasm      # 改字符串，重编译
$ curl localhost:8080/wasm
hello from TinyGo guest v3                # 已换血；桥插件级联重载
$ printf 'corrupted!' > guest.wasm        # 模拟一次坏构建
$ curl localhost:8080/wasm
hello from TinyGo guest v3                # 旧版本继续服役
```

服务器日志把整个过程讲给你听：

```
[wasm-bridge] loaded, message="hello from TinyGo guest v3"
[hmr] guest reloaded
[hmr] reload failed, old version kept: wasm: update probe failed: invalid magic number
```

而这个 demo 也立刻证明了自己的价值：TinyGo 产物的第一次热替换就失败于
`module[main] has already been instantiated`——工具链二进制携带固定模块名，
与现役旧实例在原子换血的探针阶段撞名。已修复（唯一实例名）并配回归测试。
此前的 property 测试都没抓到它，因为测试 guest 是手写编码、不带名段。
**定理验证语义，但只有端到端 demo 验证世界。**

## 现状与下一步

v0.2.0 已发布：范式核心（context、fiber、效应、隔离）、五定理测试、
带原子回滚的 WASM 组件、TinyGo guest SDK、`hmr` 监听包、示例应用。
API 处于 v0.x——未冻结。

记录在案的下一步：Cordis 式事件派发模式
（`emit/parallel/serial/bail/waterfall`，卫星包）、插件配置 schema，
以及两处论文层面的放宽（嵌套 fiber 级联卸载、迭代器式持续 yield）
——目前在案为收窄实现。

如果你在 Go 里构建插件系统、agent harness 或热重载宿主——Cordis 给
TypeScript 世界的东西，现在有了 Go 版本：给定理，不给承诺。

- 仓库：[github.com/0xdenny218/stc-go](https://github.com/0xdenny218/stc-go)
- 文档：[pkg.go.dev/github.com/0xdenny218/stc-go](https://pkg.go.dev/github.com/0xdenny218/stc-go)
- 论文：[cordiverse/paper](https://github.com/cordiverse/paper)
- 姊妹实现：[Cordis](https://github.com/cordiverse/cordis)（TypeScript）、
  [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（agent harness）
