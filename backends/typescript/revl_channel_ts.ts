// The same-origin channel host engine behind `stdlib/channel.rvl` on the ts
// tier (issue #3418, first slice). The page-to-page peer of
// `revl_socket_ts.ts`: the hosting page reads the same knocks a socket would
// give it, `{seq, kind, why, line, who, text}`, and says and shuts a line the
// same way, so a program written against `stdlib/wire.rvl` does not change.
//
// The carrier is the browser's `BroadcastChannel`, reached as a global: this
// module names no node builtin, so a page can load it as it is. A channel
// reaches only pages of one origin in one browser; that scope is the
// provider's whole answer to "who may connect", and the channel name is the
// room. Cross-origin and peer data channels are later slices.
//
// Two ends, each a plain object:
//   * `Hub`, the host. One per channel name. It answers a `join`, hands the
//     program a knock per event and carries what the program says. It is what
//     the extern entry points `listen`, `take`, `say`, `close`, `shut` drive.
//   * `Peer`, the joining page. `join(...)` returns it; the page calls
//     `peer.take(most)` for what happened (`opened`, `said`, `shut`),
//     `peer.say(text)` and `peer.leave()`. It is the end a page writes against
//     where a socket client would use `WebSocket`.
//
// Rules a channel does not give for free, each declared in the door and none a
// constant here:
//   most      a text message over `most` UTF-8 bytes shuts the line
//             `too_large`; none of it is delivered. The joining end refuses to
//             send one.
//   held      a channel cannot be paused, so a line that makes `held` knocks
//             wait for `take` is shut `too_fast`, and a join while `held`
//             knocks wait is shut `refused` and makes no knock. Nothing
//             already queued is dropped. A label and a `bye` text longer than
//             `most` characters are cut to it.
//   lines     a join past `lines` open lines is shut `refused` and makes no
//             knock.
//   beat      the host broadcasts a beat every `beat` ms and a joined end
//             answers at that rate and sends at most `most` bytes, both read
//             from the host's `open` reply.
//   silence   a line heard from in no way for `silence` ms is shut `silent`
//             on the host and `host_gone` on the joined end. A page that is
//             only slow can say anything to the host; it is told its line is
//             `expired` and joins again.
// A page that closes can say `bye` and the host reads it as `closed`; one that
// does not get to is found by `silence`.
//
// What a channel does not do: there is no order or size limit to declare (the
// browser queues, delivers in order and completely, and does not size-limit a
// message), and a slow reader is not visible, so there is no `limit`.
//
// Who joins is not asked. Any page of the origin that knows the name may
// join, and the `label` it offers is its own claim: it is the knock's `who` on
// `Opened`, and a program must not take it as an identity. A channel keeps no
// secret: every page of the origin hears every message, so a line number, the
// ids a line and the host go by, and the host's beat are all readable and
// forgeable by any page of the origin, which can speak for a line, shut a
// joined end or stop a host. Between pages that trust each other that is the
// scope; a join that must be authenticated is a later slice.
//
// Timers: a page in a background tab has its timers slowed by the browser (about
// once a second, and once a minute after five minutes hidden). A host or a
// joined end in such a tab can pass a `silence` of a few seconds; choose
// `silence` for the tab that hosts.
//
// One host per channel name. A host that finds another with a better claim
// (earlier start, then smaller id) stops, and its next `take` throws.
//
// `seq` and `line` are `bigint`s on `take`: revl's `Int` is a `bigint` on this
// tier.

const V = 1

export interface Door {
  channel: string
  most: bigint | number
  held: bigint | number
  lines: bigint | number
  beat: bigint | number
  silence: bigint | number
}

/** One knock, as `stdlib/channel.rvl`'s `Raw` reads it. */
export interface Raw {
  seq: bigint
  kind: 'opened' | 'said' | 'shut'
  why: string
  line: bigint
  who: string
  text: string
}

/** What the joining end reports: a knock with no line number. */
export interface Event {
  kind: 'opened' | 'said' | 'shut'
  why: string
  text: string
}

export interface Join {
  channel: string
  label?: string
  most?: number
  beat?: number
  silence?: number
}

interface Msg {
  v: number
  t: 'join' | 'open' | 'say' | 'shut' | 'bye' | 'beat' | 'host'
  from?: string
  to?: string
  host?: string
  since?: number
  label?: string
  why?: string
  text?: string
  beat?: number
  most?: number
}

function id(): string {
  return globalThis.crypto.randomUUID()
}

function bytes(text: string): number {
  return new TextEncoder().encode(text).length
}

function positive(door: Door, name: 'most' | 'held' | 'lines' | 'beat' | 'silence'): number {
  const n = Number(door[name])
  if (!Number.isInteger(n) || n < 1) {
    throw new Error(`channel door \`${name}\` must be a positive integer, got ${String(door[name])}`)
  }
  return n
}

function channelOf(name: string): BroadcastChannel {
  if (typeof BroadcastChannel !== 'function') {
    throw new Error('this page has no BroadcastChannel, so a channel cannot be opened')
  }
  if (typeof name !== 'string' || name === '') {
    throw new Error('a channel needs a name')
  }
  return new BroadcastChannel(name)
}

function ticker(fn: () => void, ms: number): ReturnType<typeof setInterval> {
  const t = setInterval(fn, ms)
  ;(t as unknown as { unref?: () => void }).unref?.()
  return t
}

function mine(m: unknown): m is Msg {
  return typeof m === 'object' && m !== null && (m as Msg).v === V
}

interface Line {
  num: number
  peer: string
  seen: number
}

export class Hub {
  readonly channel: string
  readonly most: number
  readonly held: number
  readonly lines: number
  readonly beat: number
  readonly silence: number
  failure: Error | null = null
  private readonly me = id()
  private readonly since = Date.now()
  private readonly ch: BroadcastChannel
  private readonly timer: ReturnType<typeof setInterval>
  private readonly knocks: Raw[] = []
  private readonly open = new Map<number, Line>()
  private readonly byPeer = new Map<string, Line>()
  private seq = 0
  private next = 0
  private stopped = false

  constructor(door: Door) {
    this.channel = door.channel
    this.most = positive(door, 'most')
    this.held = positive(door, 'held')
    this.lines = positive(door, 'lines')
    this.beat = positive(door, 'beat')
    this.silence = positive(door, 'silence')
    if (this.silence <= this.beat) {
      throw new Error(`channel door \`silence\` (${this.silence}) must be longer than \`beat\` (${this.beat})`)
    }
    this.ch = channelOf(door.channel)
    this.ch.onmessage = (ev: MessageEvent) => this.hear(ev.data)
    this.timer = ticker(() => this.tick(), this.beat)
    this.tell({ t: 'host', host: this.me, since: this.since })
  }

  private tell(m: Omit<Msg, 'v'>): void {
    if (!this.stopped) this.ch.postMessage({ v: V, ...m })
  }

  private knock(kind: Raw['kind'], why: string, line: number, who: string, text: string): void {
    this.knocks.push({ seq: BigInt(++this.seq), kind, why, line: BigInt(line), who, text })
  }

  private hear(m: unknown): void {
    if (this.stopped || !mine(m)) return
    if (m.t === 'host') {
      this.contest(m)
    } else if (m.t === 'join' && typeof m.from === 'string') {
      this.admit(m.from, typeof m.label === 'string' ? m.label : '')
    } else if (m.host === this.me && typeof m.from === 'string') {
      this.fromPeer(m)
    }
  }

  /** Another host on this channel: the earlier start keeps it, ties by id. */
  private contest(m: Msg): void {
    if (m.host === undefined || m.host === this.me) return
    const theirs: [number, string] = [Number(m.since), m.host]
    const ours: [number, string] = [this.since, this.me]
    if (theirs[0] < ours[0] || (theirs[0] === ours[0] && theirs[1] < ours[1])) {
      this.failure = new Error(
        `channel "${this.channel}" already has a host; one host per channel name`)
      this.stop()
    } else {                      // the other end is the later one: tell it now
      this.tell({ t: 'host', host: this.me, since: this.since })
    }
  }

  private admit(peer: string, label: string): void {
    const known = this.byPeer.get(peer)
    if (known !== undefined) {              // a join said again: answer again
      known.seen = Date.now()
      this.opened(peer)
      return
    }
    if (this.open.size >= this.lines) {
      this.tell({ t: 'shut', to: peer, host: this.me, why: 'refused', text: 'channel full' })
      return
    }
    if (this.knocks.length >= this.held) {
      this.tell({ t: 'shut', to: peer, host: this.me, why: 'refused', text: 'host is not reading' })
      return
    }
    const line: Line = { num: ++this.next, peer, seen: Date.now() }
    this.open.set(line.num, line)
    this.byPeer.set(peer, line)
    this.knock('opened', '', line.num, label.slice(0, this.most), '')
    this.opened(peer)
  }

  /** The `open` reply: the rules a joined end keeps, from the door. */
  private opened(peer: string): void {
    this.tell({ t: 'open', to: peer, host: this.me, beat: this.beat, most: this.most })
  }

  private fromPeer(m: Msg): void {
    const peer = m.from as string
    const line = this.byPeer.get(peer)
    if (line === undefined) {   // dropped while only slow: the way back in is a new join
      if (m.t !== 'bye') {
        this.tell({ t: 'shut', to: peer, host: this.me, why: 'expired', text: 'join again' })
      }
      return
    }
    line.seen = Date.now()
    if (m.t === 'bye') {
      this.end(line, 'closed', typeof m.text === 'string' ? m.text.slice(0, this.most) : '')
    } else if (m.t === 'say' && typeof m.text === 'string') {
      if (bytes(m.text) > this.most) {
        this.shut(line, 'too_large', 'message over the limit')
      } else if (this.knocks.length >= this.held) {
        this.shut(line, 'too_fast', 'host is not reading')
      } else {
        this.knock('said', '', line.num, '', m.text)
      }
    }
  }

  private tick(): void {
    this.tell({ t: 'host', host: this.me, since: this.since })
    const now = Date.now()
    for (const line of [...this.open.values()]) {
      if (now - line.seen > this.silence) this.shut(line, 'silent', 'no beat')
    }
  }

  /** Drop the line and knock `Shut(why)`; no word to the peer. */
  private end(line: Line, why: string, text: string): void {
    this.open.delete(line.num)
    this.byPeer.delete(line.peer)
    this.knock('shut', why, line.num, '', text)
  }

  /** Tell the peer, then drop the line. */
  private shut(line: Line, why: string, text: string): void {
    this.tell({ t: 'shut', to: line.peer, host: this.me, why, text })
    this.end(line, why, text)
  }

  take(most: number): Raw[] {
    if (this.failure !== null) throw this.failure
    return this.knocks.splice(0, Math.max(0, most))
  }

  say(line: number, text: string): boolean {
    const l = this.open.get(line)
    if (l === undefined || this.stopped) return false
    this.tell({ t: 'say', to: l.peer, host: this.me, text })
    return true
  }

  close(line: number, why: string, text: string): boolean {
    const l = this.open.get(line)
    if (l === undefined || this.stopped) return false
    this.shut(l, why, text)
    return true
  }

  /** Unload: every open line is told the host is gone, then the channel closes. */
  stop(): void {
    if (this.stopped) return
    for (const line of [...this.open.values()]) {
      this.tell({ t: 'shut', to: line.peer, host: this.me, why: 'host_gone', text: 'host closed' })
    }
    this.stopped = true
    clearInterval(this.timer)
    this.ch.onmessage = null
    this.ch.close()
  }
}

// ------------------------------------------------------ the extern entry points

export function listen(door: Door): Hub {
  return new Hub(door)
}

export function shut(h: Hub): void {
  h.stop()
}

export function settled(): void {
  return undefined
}

export function take(h: Hub, most: bigint | number): Raw[] {
  return h.take(Number(most))
}

export function say(h: Hub, line: bigint | number, text: string): boolean {
  return h.say(Number(line), text)
}

export function close(h: Hub, line: bigint | number, why: string, text: string): boolean {
  return h.close(Number(line), why, text)
}

// ------------------------------------------------------ the joining end

export class Peer {
  readonly me = id()
  private host: string | null = null
  private readonly ch: BroadcastChannel
  private timer: ReturnType<typeof setInterval>
  private readonly events: Event[] = []
  private most: number
  private readonly silence: number
  private readonly label: string
  private heard = Date.now()
  private done = false

  constructor(opts: Join) {
    this.most = opts.most ?? 65536
    this.silence = opts.silence ?? 8000
    this.label = opts.label ?? ''
    const beat = opts.beat ?? 2000
    for (const [name, n] of [['most', this.most], ['silence', this.silence], ['beat', beat]] as const) {
      if (!Number.isInteger(n) || n < 1) {
        throw new Error(`join option \`${name}\` must be a positive integer, got ${String(n)}`)
      }
    }
    this.ch = channelOf(opts.channel)
    this.ch.onmessage = (ev: MessageEvent) => this.hear(ev.data)
    this.timer = ticker(() => this.tick(), beat)
    this.hello()
  }

  get isOpen(): boolean {
    return this.host !== null && !this.done
  }

  private hello(): void {
    this.ch.postMessage({ v: V, t: 'join', from: this.me, label: this.label })
  }

  private hear(m: unknown): void {
    if (this.done || !mine(m)) return
    if (m.t === 'host') {                  // the host's own beat, for everyone
      if (m.host !== undefined && m.host === this.host) this.heard = Date.now()
      return
    }
    if (m.to !== this.me) return
    if (this.host !== null && m.host !== this.host) return
    this.heard = Date.now()
    if (m.t === 'open' && this.host === null && typeof m.host === 'string') {
      this.host = m.host
      // the host's rules win: its message limit, and its beat as our own
      if (Number.isInteger(m.most) && (m.most as number) > 0) this.most = m.most as number
      if (Number.isInteger(m.beat) && (m.beat as number) > 0) {
        clearInterval(this.timer)
        this.timer = ticker(() => this.tick(), m.beat as number)
      }
      this.events.push({ kind: 'opened', why: '', text: '' })
    } else if (m.t === 'say' && this.host !== null && typeof m.text === 'string') {
      this.events.push({ kind: 'said', why: '', text: m.text })
    } else if (m.t === 'shut') {
      this.finish(typeof m.why === 'string' ? m.why : 'refused', typeof m.text === 'string' ? m.text : '')
    }
  }

  private tick(): void {
    if (this.done) return
    if (Date.now() - this.heard > this.silence) {
      this.finish('host_gone', this.host === null ? 'no host answered' : 'no beat from the host')
    } else if (this.host === null) {
      this.hello()
    } else {
      this.ch.postMessage({ v: V, t: 'beat', from: this.me, host: this.host })
    }
  }

  private finish(why: string, text: string): void {
    if (this.done) return
    this.done = true
    clearInterval(this.timer)
    this.ch.onmessage = null
    this.ch.close()
    this.events.push({ kind: 'shut', why, text })
  }

  /** What happened since the last call, oldest first, at most `most`. */
  take(most: number): Event[] {
    return this.events.splice(0, Math.max(0, most))
  }

  /** Say `text` to the host. False: not open, or over the message limit. */
  say(text: string): boolean {
    if (!this.isOpen || bytes(text) > this.most) return false
    this.ch.postMessage({ v: V, t: 'say', from: this.me, host: this.host, text })
    return true
  }

  /** Leave: the host reads it as `closed`. */
  leave(text = ''): void {
    if (this.done) return
    if (this.host !== null) {
      this.ch.postMessage({ v: V, t: 'bye', from: this.me, host: this.host, text })
    }
    this.finish('closed', text)
  }
}

export function join(opts: Join): Peer {
  return new Peer(opts)
}
