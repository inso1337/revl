// The accepted-socket host engine behind `stdlib/socket.rvl` on the ts tier
// (issue #3357). The ts peer of `backends/python/revl_socket.py`: same knocks,
// same close codes, same limits, so a program reads the same `Knock`s on
// either tier.
//
// `stdlib/socket.rvl`'s externs reach this module through one `@ts ref` each
// (the `stdlib/fs.rvl` door, item 410: an install-origin ref, resolved against
// the install root at the extern's first call). node builtins are reached with
// `process.getBuiltinModule(...)`, never a bare `require` (the emitted-ts
// runtime contract). No package is added: the HTTP side is `node:http`, and the
// RFC 6455 handshake and text framing are written here, with no extensions and
// no subprotocols.
//
// One `Listener` owns a listening `http.Server` and the lines upgraded on it.
// The program's calls (`take`, `say`, `close`) are synchronous and never wait
// on the network: they read and change the listener's state, and node's event
// loop does the I/O between them.
//
// What `take` hands back is a list of plain objects, one per knock, that
// `stdlib/socket.rvl` maps onto `stdlib/wire.rvl`'s `Knock`. `seq` and `line`
// are `bigint`s, because revl's `Int` is a `bigint` on this tier:
//
//     {seq, kind: "opened" | "said" | "shut", why: <why_str> | "", line, who, text}
//
// Close codes this module sends on its own: 1009 for a message over `most`,
// 1003 for a binary frame, 1002 for a protocol violation (an unmasked client
// frame, a reserved bit or opcode, a bad fragment or control frame) and 1007
// for a text message or close reason that is not UTF-8. Each shuts the line
// `Refused`, apart from 1009, which is `TooLarge`. A peer that pings while more
// than `limit` bytes wait for it is shut `TooSlow` (1008). A close the program
// asks for carries the code `stdlib/socket.rvl`'s `close_code` chose. A line
// that ends without a close frame (EOF, reset, failed write) knocks
// `Shut(HostGone)`.
//
// Where this tier differs from the py engine, because node owns the accept
// loop and the bind:
//   * the bind completes on the event loop, after `listen` returns. A bind
//     failure cannot fail the acquire itself; it is recorded, and the next
//     `take` throws it, naming the address and the port.
//   * the request head is node's HTTP parser: a head over 16384 bytes is 431
//     and a head that does not arrive within `HEAD_MS` is dropped, both by
//     node. A connection over `MAX_LINES` is closed by node rather than left
//     waiting in the listen backlog.
//   * while `held` knocks wait, an upgrade is accepted but held unanswered (no
//     101, no knock) until `take` makes room, as the py engine leaves it
//     unaccepted; every open line stops being read, as on py. A held client
//     that hangs up leaves with no knock; one that sends over `MAX_HEAD` bytes
//     before its 101 is dropped.

import type { IncomingMessage, Server } from 'node:http'
import type { Socket } from 'node:net'

const http = process.getBuiltinModule('node:http')
const crypto = process.getBuiltinModule('node:crypto')

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
const MAX_HEAD = 16384        // bytes of request head before node answers 431
const DRAIN_MS = 2000         // how long a shutting line may take to finish
const HEAD_MS = 10000         // how long a connection may take to send its head
const MAX_LINES = 1024        // connections held at once

const CODE_CLOSED = 1000
const CODE_GONE = 1001
const CODE_PROTOCOL = 1002
const CODE_BINARY = 1003
const CODE_NOT_UTF8 = 1007
const CODE_TOO_SLOW = 1008
const CODE_TOO_LARGE = 1009

// close codes a peer may send (RFC 6455 section 7.4): 1004-1006 and 1015 are
// never on the wire, 1016-2999 are unassigned or reserved
const PEER_CODES = new Set([1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010,
  1011, 1012, 1013, 1014])

/** The door `stdlib/socket.rvl` builds from the provider's config. */
export interface Door {
  address: string
  port: bigint | number
  origins: string[]
  bare: boolean
  most: bigint | number
  held: bigint | number
  limit: bigint | number
}

/** One knock, as `stdlib/socket.rvl`'s `Raw` reads it. */
export interface Raw {
  seq: bigint
  kind: 'opened' | 'said' | 'shut'
  why: string
  line: bigint
  who: string
  text: string
}

function http_(status: string, extra = ''): Buffer {
  return Buffer.from(`HTTP/1.1 ${status}\r\nContent-Length: 0\r\nConnection: close\r\n`
    + `${extra}\r\n`, 'latin1')
}

/** One unmasked server frame, FIN set. */
function frame(opcode: number, payload: Buffer): Buffer {
  const n = payload.length
  let head: Buffer
  if (n < 126) {
    head = Buffer.from([0x80 | opcode, n])
  } else if (n < 65536) {
    head = Buffer.alloc(4)
    head[0] = 0x80 | opcode
    head[1] = 126
    head.writeUInt16BE(n, 2)
  } else {
    head = Buffer.alloc(10)
    head[0] = 0x80 | opcode
    head[1] = 127
    head.writeBigUInt64BE(BigInt(n), 2)
  }
  return Buffer.concat([head, payload])
}

function closePayload(code: number, text: string): Buffer {
  const reason = Buffer.from(text, 'utf8')
  let end = Math.min(reason.length, 123)
  // never cut a UTF-8 sequence in two: the peer would refuse the reason
  if (end < reason.length) {
    while (end > 0 && (reason[end] & 0xc0) === 0x80) end--
  }
  const out = Buffer.alloc(2 + end)
  out.writeUInt16BE(code, 0)
  reason.copy(out, 2, 0, end)
  return out
}

function unmask(mask: Buffer, data: Buffer): Buffer {
  const out = Buffer.alloc(data.length)
  for (let i = 0; i < data.length; i++) out[i] = data[i] ^ mask[i & 3]
  return out
}

/** Strict UTF-8, keeping a leading BOM as the py engine's `decode` does. */
function utf8(data: Buffer): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(data)
  } catch {
    return null
  }
}

function positive(door: Door, name: 'most' | 'held' | 'limit'): number {
  const value = Number(door[name])
  if (!Number.isFinite(value) || value < 1) {
    throw new Error(`socket door \`${name}\` must be at least 1, got ${door[name]}`)
  }
  return value
}

/** Every header of a request head, by lower-cased name, in order. */
function headersOf(req: IncomingMessage): Map<string, string[]> {
  const out = new Map<string, string[]>()
  const raw = req.rawHeaders
  for (let i = 0; i + 1 < raw.length; i += 2) {
    const name = raw[i].toLowerCase()
    const list = out.get(name)
    if (list === undefined) out.set(name, [raw[i + 1].trim()])
    else list.push(raw[i + 1].trim())
  }
  return out
}

function tokens(headers: Map<string, string[]>, name: string): Set<string> {
  const out = new Set<string>()
  for (const value of headers.get(name) ?? []) {
    for (const t of value.split(',')) out.add(t.trim().toLowerCase())
  }
  return out
}

type Phase = 'open' | 'shutting' | 'gone'

/** One upgraded connection, from its 101 to its shut. */
class Line {
  num = 0
  phase: Phase = 'open'
  inbuf: Buffer = Buffer.alloc(0)
  frag: Buffer[] = []
  fragLen = 0
  fragOp = 0           // 0: no fragmented message in progress
  eof = false
  paused = false       // frame parsing stopped on a full queue
  timer: ReturnType<typeof setTimeout> | null = null
  readonly sock: Socket
  constructor(sock: Socket) {
    this.sock = sock
  }
}

/** An upgrade that arrived while the queue was full, waiting to be answered. */
interface Pending {
  req: IncomingMessage
  sock: Socket
  head: Buffer
  // the listeners that watch it while it is held, removed when it is answered
  onData: (data: Buffer) => void
  onGone: () => void
}

/** A listening socket, its lines and the knocks waiting for `take`. */
export class Listener {
  readonly most: number
  readonly held: number
  readonly limit: number
  readonly origins: ReadonlySet<string>
  readonly bare: boolean
  /** Resolves with the bound port once the bind completes; rejects on a failed bind. */
  readonly ready: Promise<number>
  port = 0
  failure: Error | null = null
  private readonly server: Server
  private readonly knocks: Raw[] = []
  private seq = 0
  private next = 0
  private readonly lines = new Map<number, Line>()   // open lines, by number
  private readonly conns = new Set<Line>()           // every upgraded line
  private readonly pending: Pending[] = []
  private stopping = false

  constructor(door: Door) {
    this.most = positive(door, 'most')
    this.held = positive(door, 'held')
    this.limit = positive(door, 'limit')
    this.origins = new Set(door.origins)
    this.bare = Boolean(door.bare)
    const address = door.address
    const port = Number(door.port)
    this.server = http.createServer({
      maxHeaderSize: MAX_HEAD,
      headersTimeout: HEAD_MS,
      requestTimeout: HEAD_MS,
      connectionsCheckingInterval: 500,
    })
    this.server.maxConnections = MAX_LINES
    // a request that is not an upgrade: serving files is beside this binding
    this.server.on('request', (_req, res) => {
      res.writeHead(404, { 'Content-Length': '0', Connection: 'close' })
      res.end()
    })
    this.server.on('upgrade', (req: IncomingMessage, sock: Socket, head: Buffer) => {
      if (this.stopping) {
        sock.destroy()
      } else if (this.full()) {
        this.hold(req, sock, head)
      } else {
        this.upgrade(req, sock, head)
      }
    })
    this.ready = new Promise<number>((resolve, reject) => {
      this.server.once('listening', () => {
        const bound = this.server.address()
        this.port = typeof bound === 'object' && bound !== null ? bound.port : port
        resolve(this.port)
      })
      // before the bind completes, an error is the bind failing; after it, a
      // failed accept (out of descriptors), which node retries on its own
      this.server.on('error', (e: Error) => {
        if (this.port !== 0 || this.failure !== null) return
        this.failure = new Error(
          `socket listener on ${address}:${port} failed: ${e.message}`)
        reject(this.failure)
      })
    })
    // a failed bind is read by the next `take`; an unobserved `ready` must not
    // become an unhandled rejection
    this.ready.catch(() => undefined)
    this.server.listen({ host: address, port, backlog: 128 })
  }

  /** How many upgrades are held unanswered while the queue is full. */
  get holding(): number {
    return this.pending.length
  }

  // ---------------------------------------------------------- the program's side

  take(most: number): Raw[] {
    if (this.failure !== null) throw this.failure
    const out = this.knocks.splice(0, Math.max(0, most))
    this.resume()
    return out
  }

  say(num: number, text: string): boolean {
    const line = this.lines.get(num)
    if (line === undefined || line.phase !== 'open'
      || line.sock.writableLength > this.limit) return false
    line.sock.write(frame(0x1, Buffer.from(text, 'utf8')))
    return true
  }

  close(num: number, code: number, why: string, text: string): boolean {
    const line = this.lines.get(num)
    if (line === undefined || line.phase !== 'open') return false
    this.shut(line, why, code, text)
    return true
  }

  /** The teardown: open lines hear 1001, the port is released, no more knocks. */
  stop(): void {
    this.stopping = true
    for (const p of this.pending) p.sock.destroy()
    this.pending.length = 0
    for (const line of [...this.conns]) {
      if (line.phase === 'open') {
        line.phase = 'gone'     // nobody takes a knock after the unload
        line.sock.end(frame(0x8, closePayload(CODE_GONE, '')))
        setTimeout(() => line.sock.destroy(), DRAIN_MS).unref()
      } else {
        this.drop(line)
      }
    }
    this.lines.clear()
    this.conns.clear()
    this.server.close()
    this.server.closeAllConnections()
  }

  // --------------------------------------------------------------- shared state

  private full(): boolean {
    return this.knocks.length >= this.held
  }

  private knock(kind: Raw['kind'], why: string, line: Line, who: string,
    text: string): void {
    if (this.stopping) return
    this.seq += 1
    this.knocks.push({ seq: BigInt(this.seq), kind, why, line: BigInt(line.num),
      who, text })
    if (this.full()) {
      // socket backpressure: stop reading every open line until `take`
      for (const each of this.lines.values()) each.sock.pause()
    }
  }

  /** Answer held upgrades and parse what a pause left buffered, once `take`
   * made room. */
  private resume(): void {
    while (this.pending.length > 0 && !this.full()) {
      const p = this.pending.shift() as Pending
      p.sock.removeListener('data', p.onData)
      p.sock.removeListener('end', p.onGone)
      p.sock.removeListener('close', p.onGone)
      if (p.sock.destroyed) continue
      this.upgrade(p.req, p.sock, p.head)
    }
    if (this.full()) return
    for (const line of [...this.lines.values()]) {
      if (line.paused) {
        line.paused = false
        this.parse(line)
      }
      if (line.phase === 'open' && !this.full()) line.sock.resume()
    }
  }

  /** Hold an upgrade unanswered until `take` makes room. It is still read, so
   * that a client that hangs up (or resets) while held is noticed: it leaves the
   * queue with no knock, as the py engine drops a head-phase connection at EOF.
   * What it sends while held is kept for its line, up to `MAX_HEAD` bytes; a
   * client that sends more before its 101 is dropped. */
  private hold(req: IncomingMessage, sock: Socket, head: Buffer): void {
    const p: Pending = {
      req, sock, head,
      onData: (data: Buffer) => {
        p.head = p.head.length === 0 ? data : Buffer.concat([p.head, data])
        if (p.head.length > MAX_HEAD) p.onGone()
      },
      onGone: () => {
        const at = this.pending.indexOf(p)
        if (at >= 0) this.pending.splice(at, 1)
        sock.destroy()
      },
    }
    sock.on('error', () => undefined)   // a reset while held is not a fault
    sock.on('data', p.onData)
    sock.on('end', p.onGone)
    sock.on('close', p.onGone)
    this.pending.push(p)
  }

  /** Queue the last bytes of a line and give it `DRAIN_MS` to finish, written
   * or not. */
  private shutting(line: Line, last: Buffer): void {
    line.phase = 'shutting'
    line.inbuf = Buffer.alloc(0)
    line.sock.resume()               // read its EOF even while the queue is full
    line.sock.end(last)
    line.timer = setTimeout(() => this.drop(line), DRAIN_MS)
    line.timer.unref()
  }

  /** End an open line from this side: close frame, then a `Shut` knock. */
  private shut(line: Line, why: string, code: number, text: string): void {
    this.shutting(line, frame(0x8, closePayload(code, text)))
    this.lines.delete(line.num)
    this.knock('shut', why, line, '', text)
  }

  /** Close a connection. An open line that ends here (the peer went away, or a
   * write to it failed) knocks `Shut(HostGone)`. */
  private drop(line: Line): void {
    if (line.phase === 'gone') return
    if (line.phase === 'open') {
      this.lines.delete(line.num)
      this.knock('shut', 'host_gone', line, '', '')
    }
    line.phase = 'gone'
    if (line.timer !== null) clearTimeout(line.timer)
    this.conns.delete(line)
    line.sock.destroy()
  }

  private refuse(sock: Socket, status: string, extra = ''): void {
    sock.end(http_(status, extra))
    setTimeout(() => sock.destroy(), DRAIN_MS).unref()
  }

  // ------------------------------------------------------------- the upgrade

  private upgrade(req: IncomingMessage, sock: Socket, head: Buffer): void {
    sock.on('error', () => undefined)   // a refused socket's reset is not a fault
    const headers = headersOf(req)
    const isUpgrade = req.method === 'GET'
      && tokens(headers, 'upgrade').has('websocket')
      && tokens(headers, 'connection').has('upgrade')
    if (!isUpgrade) {
      this.refuse(sock, '404 Not Found')
      return
    }
    const version = headers.get('sec-websocket-version') ?? []
    if (version.length !== 1 || version[0] !== '13') {
      this.refuse(sock, '426 Upgrade Required', 'Sec-WebSocket-Version: 13\r\n')
      return
    }
    const keys = headers.get('sec-websocket-key') ?? []
    if (keys.length !== 1 || !/^[A-Za-z0-9+/]{22}==$/.test(keys[0])) {
      this.refuse(sock, '400 Bad Request')
      return
    }
    const origins = headers.get('origin') ?? []
    if (origins.length > 1 || (origins.length === 0 && !this.bare)
      || (origins.length === 1 && !this.origins.has(origins[0]))) {
      this.refuse(sock, '403 Forbidden')
      return
    }
    const accept = crypto.createHash('sha1').update(keys[0] + GUID).digest('base64')
    sock.setTimeout(0)
    sock.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
      + `Connection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`, 'latin1')
    const line = new Line(sock)
    this.next += 1
    line.num = this.next
    this.lines.set(line.num, line)
    this.conns.add(line)
    this.knock('opened', '', line, origins.length === 1 ? origins[0] : '', '')
    sock.on('data', (data: Buffer) => this.read(line, data))
    sock.on('end', () => this.ended(line))
    sock.on('close', () => this.drop(line))
    if (head.length > 0) this.read(line, head)
    if (this.full()) sock.pause()
  }

  private read(line: Line, data: Buffer): void {
    if (line.phase !== 'open') return
    line.inbuf = line.inbuf.length === 0 ? data : Buffer.concat([line.inbuf, data])
    this.parse(line)
  }

  private ended(line: Line): void {
    if (line.phase === 'shutting') {
      this.drop(line)
      return
    }
    line.eof = true
    this.parse(line)
  }

  private parse(line: Line): void {
    if (line.phase === 'open') this.frames(line)
    if (line.phase === 'open' && line.eof && !line.paused) {
      // the peer went away without a close frame; whatever is left in `inbuf`
      // is a frame it never finished
      this.drop(line)
    }
  }

  // ------------------------------------------------------------------ frames

  private frames(line: Line): void {
    while (line.phase === 'open' && line.inbuf.length >= 2) {
      if (this.full()) {
        line.paused = true
        line.sock.pause()
        return
      }
      const buf = line.inbuf
      const b0 = buf[0]
      const b1 = buf[1]
      const fin = b0 & 0x80
      const rsv = b0 & 0x70
      const op = b0 & 0x0f
      let n = b1 & 0x7f
      let off = 2
      if (n === 126) {
        if (buf.length < 4) return
        n = buf.readUInt16BE(2)
        off = 4
      } else if (n === 127) {
        if (buf.length < 10) return
        const big = buf.readBigUInt64BE(2)
        n = big > BigInt(Number.MAX_SAFE_INTEGER) ? Number.MAX_SAFE_INTEGER : Number(big)
        off = 10
      }
      if (rsv || !(b1 & 0x80)) {
        this.shut(line, 'refused', CODE_PROTOCOL, 'protocol error')
        return
      }
      if (op >= 0x8) {
        if (!fin || n > 125 || op > 0xa) {
          this.shut(line, 'refused', CODE_PROTOCOL, 'protocol error')
          return
        }
      } else if (op === 0x2 || (op === 0x0 && line.fragOp === 0x2)) {
        this.shut(line, 'refused', CODE_BINARY, 'binary frames are refused')
        return
      } else if ((op !== 0x0 && op !== 0x1) || (op === 0x0) !== (line.fragOp !== 0)) {
        this.shut(line, 'refused', CODE_PROTOCOL, 'protocol error')
        return
      } else if (line.fragLen + n > this.most) {
        this.shut(line, 'too_large', CODE_TOO_LARGE, `message over ${this.most} bytes`)
        return
      }
      if (buf.length < off + 4 + n) return
      const payload = unmask(buf.subarray(off, off + 4), buf.subarray(off + 4, off + 4 + n))
      line.inbuf = buf.subarray(off + 4 + n)
      if (op === 0x8) {
        this.peerClose(line, payload)
      } else if (op === 0x9) {
        if (line.sock.writableLength > this.limit) {
          // a peer that pings and never reads the pongs: `limit` bounds what
          // waits for it, control replies included
          this.shut(line, 'too_slow', CODE_TOO_SLOW, `over ${this.limit} unwritten bytes`)
          return
        }
        line.sock.write(frame(0xa, payload))
      } else if (op === 0xa) {
        // an unsolicited pong is ignored
      } else {
        line.frag.push(payload)
        line.fragLen += payload.length
        line.fragOp = 0x1
        if (fin) this.said(line)
      }
    }
  }

  private said(line: Line): void {
    const data = Buffer.concat(line.frag)
    line.frag = []
    line.fragLen = 0
    line.fragOp = 0
    const text = utf8(data)
    if (text === null) {
      this.shut(line, 'refused', CODE_NOT_UTF8, 'text is not UTF-8')
      return
    }
    this.knock('said', '', line, '', text)
  }

  private peerClose(line: Line, payload: Buffer): void {
    let code = CODE_CLOSED
    let reason = ''
    if (payload.length > 0) {
      code = payload.length >= 2 ? payload.readUInt16BE(0) : 0
      if (!PEER_CODES.has(code) && !(code >= 3000 && code <= 4999)) {
        this.shut(line, 'refused', CODE_PROTOCOL, 'bad close frame')
        return
      }
      const text = utf8(payload.subarray(2))
      if (text === null) {
        this.shut(line, 'refused', CODE_NOT_UTF8, 'close reason is not UTF-8')
        return
      }
      reason = text
    }
    this.shutting(line, frame(0x8, payload.subarray(0, 2)))
    this.lines.delete(line.num)
    this.knock('shut', code === CODE_GONE ? 'host_gone' : 'closed', line, '', reason)
  }
}

// ------------------------------------------------------ the extern entry points

export function listen(door: Door): Listener {
  return new Listener(door)
}

export function shut(l: Listener): void {
  l.stop()
}

export function settled(): void {
  return undefined
}

export function take(l: Listener, most: bigint | number): Raw[] {
  return l.take(Number(most))
}

export function say(l: Listener, line: bigint | number, text: string): boolean {
  return l.say(Number(line), text)
}

export function close(l: Listener, line: bigint | number, code: bigint | number,
  why: string, text: string): boolean {
  return l.close(Number(line), Number(code), why, text)
}
