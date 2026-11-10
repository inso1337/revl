"""One connection that speaks nothing must not hold the pool peer, and a peer
that drips an answer must not hold the operator (issue #3153).

Both ends of the pool channel read a frame with ``_read_frame``, and both
applied their timeout to each ``recv`` rather than to the whole frame. A far
side that sends one byte before every expiry therefore resets the timer
forever: on the peer, one unauthenticated socket that dripped a byte at a time
held the only worker, so a probe that had already been sent went unanswered
(the issue's part B); on the operator, a peer that dripped its answer held
``send_task`` (and therefore ``revl pool probe``) for several times the
timeout it was given.

These are the regressions:

1. A holder is mid-frame when an honest probe arrives; the probe is answered
   while the holder is still connected (the read phase is off the worker).
2. A holder that drips one byte at a time, or says nothing at all, is dropped
   at a whole-frame deadline that is shorter than the task timeout, instead of
   renewing it on every byte.
3. ``send_task`` and ``pool_health._exchange`` return at their timeout, not at
   the number of bytes times the per-``recv`` timeout.
4. A permit taken for a held frame is given back when that frame's deadline
   fires, so a burst of held connections cannot leave the cap full.

Every test here fails on the tree before the fix: the probe gets no answer
inside its window, the holder outlives the deadline window, and the dripping
answer takes several times the timeout it was given.
"""

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_pool as pp  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl import pool_health as ph  # noqa: E402

#: Deterministic fixtures only. A seed committed to a repository is a published
#: private key, and these keys never leave the test.
OPERATOR = pi.identity_from_seed("operator", b"seed/operator/3153")
PEER = pi.identity_from_seed("peer", b"seed/peer/3153")
CHARTER = {"pool_id": "pool-3153", "tier": pp.ENTRY_TIER}


def peer_runner(tmp_path, *, timeout=4.0):
    return pd.PeerRunner(charter_record=CHARTER, identity=PEER,
                         operator_public=OPERATOR.public(),
                         workspace=tmp_path / "peer-work", timeout=timeout)


def a_signed_probe():
    body = ph.build_probe(pool_id=CHARTER["pool_id"],
                          charter_digest=pp.canonical_digest(CHARTER),
                          peer_id=PEER.peer_id)
    return ph.sign_probe(body, OPERATOR)


def start_peer(tmp_path, monkeypatch, *, frame_deadline):
    """Serve continuously on a free loopback port until the process exits."""
    # `raising=False`: on the tree before the fix there is no such constant,
    # and the test must fail on the channel's behaviour, not on an AttributeError.
    monkeypatch.setattr(pd, "FRAME_READ_DEADLINE", frame_deadline,
                        raising=False)
    bound, ready = {}, threading.Event()

    def announce(_host, port):
        bound["port"] = port
        ready.set()

    thread = threading.Thread(
        target=lambda: pd.serve(peer_runner(tmp_path), host="127.0.0.1",
                                port=0, on_listen=announce),
        daemon=True)
    thread.start()
    assert ready.wait(30), "the peer never bound a port"
    return bound["port"]


def drip_server(*, gap=0.4, count=8):
    """A peer that writes one byte per `gap` and only then a newline."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    finished = threading.Event()

    def drip():
        try:
            conn, _peer = listener.accept()
            with conn:
                for _ in range(count):
                    time.sleep(gap)
                    conn.sendall(b" ")
                conn.sendall(b"\n")
        except OSError:
            # The operator gave up at its deadline and closed: that is the
            # behaviour under test, not a failure of the fake peer.
            pass
        finally:
            listener.close()
            finished.set()

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    return port, thread, finished


def read_answer(conn):
    """One newline-terminated answer, however the far side segments it."""
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def test_a_holder_mid_frame_does_not_stop_the_peer_answering_a_probe(
        tmp_path, monkeypatch):
    """The issue's part B. The holder is still connected, mid-frame, when a
    signed probe already in flight is answered. On the old tree this waited
    until the holder closed."""
    # Longer than the test: the holder must still be mid-frame when the probe
    # is answered. The per-recv timeout it used to rely on was 4 s.
    port = start_peer(tmp_path, monkeypatch, frame_deadline=5.0)
    holder = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        holder.sendall(b"{")    # one byte: a frame that is not a frame
        time.sleep(0.1)         # let the peer accept it and start its read
        probe = a_signed_probe()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as waiter:
            waiter.sendall(json.dumps(probe, sort_keys=True).encode() + b"\n")
            waiter.settimeout(2.0)
            answer = json.loads(read_answer(waiter))
        # The holder is still mid-frame: the answer did not wait for it.
        holder.settimeout(0.2)
        with pytest.raises(socket.timeout):
            holder.recv(1)
    finally:
        holder.close()

    assert answer["ok"], answer
    assert answer["heartbeat"]["nonce"] == probe["nonce"], answer
    assert answer["heartbeat"]["peer_id"] == PEER.peer_id, answer


@pytest.mark.parametrize("drips", [False, True], ids=["silent", "dripping"])
def test_a_holder_is_dropped_at_the_frame_deadline(tmp_path, monkeypatch,
                                                   drips):
    """The connection is bounded by the whole frame, not by each byte. With a
    0.5 s deadline a holder is gone in under 2 s; on the old tree, a silent
    one held the worker for the 4 s task timeout and a dripping one renewed
    it on every byte."""
    port = start_peer(tmp_path, monkeypatch, frame_deadline=0.5)
    started = time.monotonic()
    dropped = False
    with socket.create_connection(("127.0.0.1", port), timeout=5) as holder:
        if drips:
            holder.sendall(b"{")
        while time.monotonic() - started < 2.0:
            holder.settimeout(0.2)
            try:
                data = holder.recv(65536)
            except socket.timeout:
                if drips:
                    holder.sendall(b"x")    # keep resetting the old timer
                continue
            # Either EOF or the peer's refusal means the deadline fired.
            dropped = True
            assert data == b"" or data.startswith(b"{"), data
            break
    assert dropped, ("the holder outlived the frame deadline: "
                     f"{time.monotonic() - started:.2f}s")


def test_a_full_cap_is_released_at_the_frame_deadline(tmp_path, monkeypatch):
    """A permit taken for a held frame is given back when that frame's
    deadline fires. With the cap set low, fill it, let every held frame
    expire, then prove a signed probe is still answered. A leaked permit
    would make the probe the over-cap connection, closed unanswered."""
    monkeypatch.setattr(pd, "MAX_PENDING_FRAMES", 4, raising=False)
    port = start_peer(tmp_path, monkeypatch, frame_deadline=0.5)
    held = []
    try:
        for _ in range(4):
            held.append(socket.create_connection(("127.0.0.1", port),
                                                 timeout=5))
        time.sleep(2.0)             # four times the 0.5 s frame deadline
        probe = a_signed_probe()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as waiter:
            waiter.sendall(json.dumps(probe, sort_keys=True).encode() + b"\n")
            waiter.settimeout(2.0)
            answer = json.loads(read_answer(waiter))
    finally:
        for conn in held:
            conn.close()

    assert answer["ok"], answer
    assert answer["heartbeat"]["nonce"] == probe["nonce"], answer


@pytest.mark.parametrize(
    "call",
    [lambda host, port: pd.send_task({"task_id": "t-3153"}, host=host,
                                     port=port, timeout=1.0),
     lambda host, port: ph._exchange({"kind": "probe"}, host=host, port=port,
                                     timeout=1.0)],
    ids=["send-task", "probe-exchange"])
def test_a_dripping_answer_returns_at_the_operators_timeout(call):
    """The operator's mirror. Eight bytes at 0.4 s each took about 3.2 s with
    `timeout=1.0`; the whole frame is now bounded by that timeout."""
    port, thread, finished = drip_server()
    try:
        started = time.monotonic()
        answer = call("127.0.0.1", port)
        elapsed = time.monotonic() - started
    finally:
        thread.join(10)
    assert finished.is_set()
    assert answer["link"] == pd.LINK_PEER_UNREACHABLE, answer
    assert elapsed < 2.5, f"the answer held the operator for {elapsed:.2f}s"
