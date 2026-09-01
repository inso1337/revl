package wasm

// 手写 WASM 二进制编码器与测试 guest。
// 刻意不依赖 wabt/tinygo：guest 极小且全部静态，手写编码保证
// 测试字节码完全可控（包括构造畸形与 trap 模块）。

// 类型表（本 ABI 固定六个签名）：
//
//	0: (i32,i32,i32,i32) -> i32   provide / get
//	1: (i32,i32) -> i32           get_size
//	2: (i32,i32) -> ()            log / stc_free
//	3: () -> ()                   start / stop
//	4: (i32) -> i32               stc_alloc
//	5: (i32,i32) -> i64           invoke
//
// 导入函数索引：0=provide 1=get_size 2=get 3=log；
// 本地函数（start/stop/extra）从索引 4 起。
const (
	fnProvide = 0
	fnGetSize = 1
	fnGet     = 2
	fnLog     = 3
)

func uleb(v uint64) []byte {
	var out []byte
	for {
		b := byte(v & 0x7f)
		v >>= 7
		if v != 0 {
			out = append(out, b|0x80)
		} else {
			return append(out, b)
		}
	}
}

func sleb(v int64) []byte {
	var out []byte
	for {
		b := byte(v & 0x7f)
		v >>= 7
		done := (v == 0 && b&0x40 == 0) || (v == -1 && b&0x40 != 0)
		if !done {
			b |= 0x80
		}
		out = append(out, b)
		if done {
			return out
		}
	}
}

func i32c(v int) []byte   { return append([]byte{0x41}, sleb(int64(v))...) }
func i64c(v int64) []byte { return append([]byte{0x42}, sleb(v)...) }
func callf(i int) []byte  { return append([]byte{0x10}, uleb(uint64(i))...) }
func lget(i int) []byte   { return append([]byte{0x20}, uleb(uint64(i))...) }
func lset(i int) []byte   { return append([]byte{0x21}, uleb(uint64(i))...) }
func br(i int) []byte     { return append([]byte{0x0C}, uleb(uint64(i))...) }
func brIf(i int) []byte   { return append([]byte{0x0D}, uleb(uint64(i))...) }

var (
	opDrop     = []byte{0x1a}
	opUnreach  = []byte{0x00}
	opIf       = []byte{0x04, 0x40} // void block
	opBlock    = []byte{0x02, 0x40}
	opLoop     = []byte{0x03, 0x40}
	opEnd      = []byte{0x0b}
	opI32Ne    = []byte{0x47}
	opI32Add   = []byte{0x6a}
	opI32Load  = []byte{0x28, 0x02, 0x00} // align=4, offset=0
	opI32Store = []byte{0x36, 0x02, 0x00}
	opI64Shl   = []byte{0x86}
	opI64Or    = []byte{0x84}
	opI64Add   = []byte{0x7c}
	opI64ExtU  = []byte{0xad}                   // i64.extend_i32_u
	opMemCopy  = []byte{0xfc, 0x0a, 0x00, 0x00} // memory.copy mem0←mem0
)

func cat(bs ...[]byte) []byte {
	var out []byte
	for _, b := range bs {
		out = append(out, b...)
	}
	return out
}

func name(s string) []byte { return append(uleb(uint64(len(s))), s...) }

func section(id byte, payload []byte) []byte {
	return cat([]byte{id}, uleb(uint64(len(payload))), payload)
}

func vec(items ...[]byte) []byte {
	return cat(uleb(uint64(len(items))), cat(items...))
}

type guestData struct {
	off int
	s   string
}

// guestFunc 是 start/stop 之外的额外导出函数。
type guestFunc struct {
	name   string
	typ    byte   // 类型表索引
	instrs []byte // 指令序列（不含结尾 0x0b）
	locals int    // 额外 i32 局部变量数（参数之后编号）
}

type guestSpec struct {
	start  []byte // 指令序列（不含结尾 0x0b）；nil 表示不导出 start
	stop   []byte
	locals int // start 的 i32 局部变量数
	extra  []guestFunc
	data   []guestData
}

func buildGuest(g guestSpec) []byte {
	types := vec(
		[]byte{0x60, 4, 0x7f, 0x7f, 0x7f, 0x7f, 1, 0x7f},
		[]byte{0x60, 2, 0x7f, 0x7f, 1, 0x7f},
		[]byte{0x60, 2, 0x7f, 0x7f, 0},
		[]byte{0x60, 0, 0},
		[]byte{0x60, 1, 0x7f, 1, 0x7f},
		[]byte{0x60, 2, 0x7f, 0x7f, 1, 0x7e},
	)
	imp := func(field string, typ byte) []byte {
		return cat(name("stc"), name(field), []byte{0x00, typ})
	}
	imports := vec(
		imp("provide", 0), imp("get_size", 1), imp("get", 0), imp("log", 2),
	)

	// 本地函数与导出。
	var funcs, codes, exports [][]byte
	next := 4
	export := func(n string, kind, idx byte) []byte {
		return cat(name(n), []byte{kind, idx})
	}
	exports = append(exports, export("memory", 0x02, 0))
	body := func(instrs []byte, locals int) []byte {
		var loc []byte
		if locals > 0 {
			loc = []byte{1, byte(locals), 0x7f}
		} else {
			loc = []byte{0}
		}
		code := cat(loc, instrs, opEnd)
		return cat(uleb(uint64(len(code))), code)
	}
	if g.start != nil {
		funcs = append(funcs, []byte{3})
		codes = append(codes, body(g.start, g.locals))
		exports = append(exports, export("start", 0x00, byte(next)))
		next++
	}
	if g.stop != nil {
		funcs = append(funcs, []byte{3})
		codes = append(codes, body(g.stop, 0))
		exports = append(exports, export("stop", 0x00, byte(next)))
		next++
	}
	for _, f := range g.extra {
		funcs = append(funcs, []byte{f.typ})
		codes = append(codes, body(f.instrs, f.locals))
		exports = append(exports, export(f.name, 0x00, byte(next)))
		next++
	}

	var datas [][]byte
	for _, d := range g.data {
		datas = append(datas, cat(
			[]byte{0x00}, i32c(d.off), opEnd, uleb(uint64(len(d.s))), []byte(d.s),
		))
	}

	return cat(
		[]byte{0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00},
		section(1, types),
		section(2, imports),
		section(3, vec(funcs...)),
		section(5, []byte{1, 0x00, 0x01}), // 1 页内存
		section(7, vec(exports...)),
		section(10, vec(codes...)),
		section(11, vec(datas...)),
	)
}

// helloGuest：start 提供 greeting=msg；stop 记录日志 "hello stopped"。
func helloGuest(msg string) []byte {
	const (
		keyOff = 16 // "greeting"
		valOff = 64
		logOff = 128
	)
	logMsg := "hello stopped"
	return buildGuest(guestSpec{
		start: cat(
			i32c(keyOff), i32c(8), i32c(valOff), i32c(len(msg)), callf(fnProvide), opDrop,
		),
		stop: cat(i32c(logOff), i32c(len(logMsg)), callf(fnLog)),
		data: []guestData{
			{keyOff, "greeting"}, {valOff, msg}, {logOff, logMsg},
		},
	})
}

// readerGuest：start 读取 greeting 并原样提供为 echo（依赖链跨边界）。
// greeting 不存在时什么都不提供（get_size 返回 -1）。
func readerGuest() []byte {
	const (
		greetOff = 16 // "greeting"
		echoOff  = 64 // "echo"
		buf      = 1024
	)
	return buildGuest(guestSpec{
		locals: 1,
		start: cat(
			// size = get_size("greeting")
			i32c(greetOff), i32c(8), callf(fnGetSize), lset(0),
			// if size != -1:
			lget(0), i32c(-1), opI32Ne, opIf,
			//   get("greeting", buf, 64)
			i32c(greetOff), i32c(8), i32c(buf), i32c(64), callf(fnGet), opDrop,
			//   provide("echo", buf, size)
			i32c(echoOff), i32c(4), i32c(buf), lget(0), callf(fnProvide), opDrop,
			opEnd,
		),
		data: []guestData{{greetOff, "greeting"}, {echoOff, "echo"}},
	})
}

// trapGuest：实例化合法，但 start 立即 trap（post-实例化失败路径）。
func trapGuest() []byte {
	return buildGuest(guestSpec{start: opUnreach})
}

// badGuest：版本号非法，编译期即失败。
func badGuest() []byte {
	return []byte{0x00, 0x61, 0x73, 0x6d, 0x0d, 0x00, 0x00, 0x00}
}

// withModuleName 给模块追加 name section 的模块名子节，
// 模拟工具链产物（TinyGo 的模块名恒为 "main"）。
// 回归场景：同名模块 Update 不得因实例名冲突失败。
func withModuleName(src []byte, modName string) []byte {
	sub := cat([]byte{0x00}, uleb(uint64(len(modName)+1)), name(modName))
	return cat(src, section(0, cat(name("name"), sub)))
}

// callGuest：可调用 guest。stc_alloc 是 bump 分配器（堆指针存 addr 0，
// 初值 4096）；invoke 用 memory.copy 把 prefix 与入参拼进 addr 8192 的
// 暂存区后返回——证明入参真实跨边界传入、结果真实传回；stc_free 记
// "freed" 日志（测试观察入参与结果两块缓冲都被释放）。
func callGuest(prefix string) []byte {
	const (
		bumpOff   = 0
		prefixOff = 16
		freedOff  = 64
		scratch   = 8192
	)
	return buildGuest(guestSpec{
		extra: []guestFunc{
			{
				name: "stc_alloc", typ: 4, locals: 1,
				instrs: cat(
					i32c(bumpOff), opI32Load, lset(1), // r = mem[0]
					i32c(bumpOff), lget(1), lget(0), opI32Add, opI32Store, // mem[0] = r+n
					lget(1),
				),
			},
			{
				name: "stc_free", typ: 2,
				instrs: cat(i32c(freedOff), i32c(5), callf(fnLog)),
			},
			{
				name: "invoke", typ: 5,
				instrs: cat(
					i32c(scratch), i32c(prefixOff), i32c(len(prefix)), opMemCopy,
					i32c(scratch+len(prefix)), lget(0), lget(1), opMemCopy,
					i64c(scratch), i64c(32), opI64Shl,
					i64c(int64(len(prefix))), lget(1), opI64ExtU, opI64Add,
					opI64Or,
				),
			},
		},
		data: []guestData{
			{bumpOff, "\x00\x10\x00\x00"}, // bump = 4096（小端）
			{prefixOff, prefix},
			{freedOff, "freed"},
		},
	})
}

// spinGuest：invoke 先记 "spinning" 日志，然后空转自旋直到宿主提供
// 字符串服务 "release"，最后返回静态结果。不导出 stc_alloc（空调用
// 无需分配）。用于验证 Call 与 Update 互斥。
func spinGuest(result string) []byte {
	const (
		keyOff = 16 // "release"
		resOff = 64
		logOff = 96
	)
	packed := int64(resOff)<<32 | int64(len(result))
	return buildGuest(guestSpec{
		extra: []guestFunc{
			{
				name: "invoke", typ: 5,
				instrs: cat(
					i32c(logOff), i32c(8), callf(fnLog),
					opBlock,
					opLoop,
					i32c(keyOff), i32c(7), callf(fnGetSize),
					i32c(-1), opI32Ne,
					brIf(1), // n != -1：退出自旋
					br(0),
					opEnd,
					opEnd,
					i64c(packed),
				),
			},
		},
		data: []guestData{
			{keyOff, "release"}, {resOff, result}, {logOff, "spinning"},
		},
	})
}
