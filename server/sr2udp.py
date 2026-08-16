#!/usr/bin/env python3
#
# Copyright (C) 2026 awarmplace
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""sr2udp.py - UDP transport gateway, for Broadband Adapter clients.

Sega Rally 2 dials a modem and speaks the Dwango C203 protocol over a raw byte stream, and
sr2lobby.py answers that stream over TCP. A Dreamcast running the BROADBAND ADAPTER PATCH has
no modem: it is an ordinary IP host on the player's network and reaches the server over UDP.

This gateway terminates that UDP session and relays the same byte stream to sr2lobby.py over
an ordinary TCP connection, so the lobby is unmodified and cannot tell the two kinds of
client apart.

The patch itself: https://github.com/awarmplace/sr2-bba

    BBA console   --UDP-->  sr2udp.py  --TCP-->  sr2lobby.py
    modem client  ---------------TCP--------->   sr2lobby.py

One TCP connection per session. Run one instance per lobby:

    python sr2udp.py
    python sr2udp.py --port 7665 --lobby-port 7655

RATE LIMIT. --rate defaults to 3360 bytes/sec, which is 33600 baud 8N1 (ten bits to the
byte). Do not raise it: the game's read loop stops permanently if bytes arrive faster than a
33.6k modem would have delivered them.

WIRE FORMAT. 14 byte header, big-endian:

     0  2  magic 'S2'      2  1  version 1     3  1  flags
     4  4  session         8  2  seq          10  2  ack        12  2  len

Flags: SYN 0x01, FIN 0x02, RST 0x04, ACK 0x08. Datagrams are numbered, not bytes;
acknowledgement is cumulative; loss is handled Go-Back-N.
"""
import argparse
import errno
import selectors
import socket
import struct
import sys
import time

MAGIC = 0x5332
VERSION = 1
HDR = struct.Struct(">HBBIHHH")
assert HDR.size == 14

F_SYN, F_FIN, F_RST, F_ACK = 0x01, 0x02, 0x04, 0x08

SEG_MAX = 512          # must not exceed the console's SEG_MAX
WINDOW = 8             # ours may differ from the console's; deliberately does
RTO = 0.5
KEEPALIVE = 0.5

# 33600 baud, 8N1: ten bits to the byte, so baud / 10. Not 33600 / 8 - the framing bits
# are not free.
DEFAULT_RATE = 3360.0

# A client silent for this long is gone. The client uses the same value.
SESSION_IDLE = 30.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def seq_lt(a, b):
    """Signed 16-bit comparison: sequence numbers wrap."""
    return ((a - b) & 0xFFFF) > 0x7FFF


class Session:
    """One console. Owns its ARQ state and its TCP connection to the lobby."""

    def __init__(self, sid, addr, lobby_addr, rate):
        self.sid = sid
        self.addr = addr
        self.rate = rate

        self.rcv_next = 0          # the datagram index we want from the console
        self.snd_una = 0           # oldest unacknowledged datagram of ours
        self.snd_next = 0
        self.win = {}              # seq -> [payload, sent_at]

        self.out = bytearray()     # lobby -> console, not yet segmented
        self.credit = 0.0
        self.last_fill = time.monotonic()
        self.last_rx = time.monotonic()
        self.last_tx = 0.0

        self.bytes_up = 0
        self.bytes_down = 0
        self.retx = 0
        self.dead = False

        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.connect(lobby_addr)
        self.tcp.setblocking(False)
        self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log(f"session {sid:#010x} from {addr[0]}:{addr[1]} -> lobby fd {self.tcp.fileno()}")

    # ---- outbound ------------------------------------------------------------------

    def emit(self, udp, flags=0, seq=0, payload=b""):
        pkt = HDR.pack(MAGIC, VERSION, flags | F_ACK, self.sid,
                       seq, self.rcv_next, len(payload)) + payload
        try:
            udp.sendto(pkt, self.addr)
        except OSError as e:
            # ICMP unreachable from a previous datagram surfaces on the next send. It does
            # not mean the session has ended.
            if e.errno not in (errno.ECONNRESET, errno.ECONNREFUSED, 10054, 10061):
                raise
        self.last_tx = time.monotonic()

    def fill(self, udp):
        """Segment the lobby's bytes into the window, subject to the rate limit."""
        now = time.monotonic()
        elapsed = now - self.last_fill
        self.last_fill = now
        self.credit = min(self.credit + elapsed * self.rate, self.rate)

        while ((self.snd_next - self.snd_una) & 0xFFFF) < WINDOW and self.out:
            n = min(len(self.out), SEG_MAX, int(self.credit))
            if n <= 0:
                break
            self.credit -= n
            chunk = bytes(self.out[:n])
            del self.out[:n]
            self.win[self.snd_next] = [chunk, now]
            self.emit(udp, seq=self.snd_next, payload=chunk)
            self.bytes_down += n
            self.snd_next = (self.snd_next + 1) & 0xFFFF

    def poll(self, udp):
        now = time.monotonic()

        if now - self.last_rx > SESSION_IDLE:
            log(f"session {self.sid:#010x}: {SESSION_IDLE:.0f}s of silence, closing")
            self.dead = True
            return

        self.fill(udp)

        if self.snd_una != self.snd_next:
            oldest = self.win.get(self.snd_una)
            if oldest and now - oldest[1] >= RTO:
                # Go-Back-N: resend everything still in flight.
                s = self.snd_una
                while seq_lt(s, self.snd_next):
                    slot = self.win.get(s)
                    if slot:
                        slot[1] = now
                        self.emit(udp, seq=s, payload=slot[0])
                        self.retx += 1
                    s = (s + 1) & 0xFFFF
        elif now - self.last_tx >= KEEPALIVE:
            # Keeps the client's NAT mapping alive and carries the cumulative ack.
            self.emit(udp)

    # ---- inbound -------------------------------------------------------------------

    def input(self, udp, flags, seq, ack, payload):
        self.last_rx = time.monotonic()

        if flags & F_RST:
            self.dead = True
            return

        if flags & F_ACK:
            if seq_lt(self.snd_una, ack) and not seq_lt(self.snd_next, ack):
                s = self.snd_una
                while seq_lt(s, ack):
                    self.win.pop(s, None)
                    s = (s + 1) & 0xFFFF
                self.snd_una = ack

        if payload:
            if seq == self.rcv_next:
                try:
                    self.tcp.sendall(payload)
                except BlockingIOError:
                    # The lobby is not reading. Do not advance and do not acknowledge; the
                    # client resends. Backpressure rather than silent loss.
                    self.emit(udp)
                    return
                except OSError as e:
                    log(f"session {self.sid:#010x}: lobby write failed, {e}")
                    self.dead = True
                    return
                self.rcv_next = (self.rcv_next + 1) & 0xFFFF
                self.bytes_up += len(payload)
            self.emit(udp)

    def close(self, udp):
        try:
            self.emit(udp, flags=F_RST)
        except OSError:
            pass
        try:
            self.tcp.close()
        except OSError:
            pass
        log(f"session {self.sid:#010x} closed: {self.bytes_up} up, "
            f"{self.bytes_down} down, {self.retx} retransmits")


def run(bind_host, bind_port, lobby_host, lobby_port, rate):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind((bind_host, bind_port))
    udp.setblocking(False)
    log(f"listening on udp {bind_host}:{bind_port}, relaying to "
        f"{lobby_host}:{lobby_port} at {rate:.0f} bytes/sec")

    sessions = {}          # sid -> Session
    sel = selectors.DefaultSelector()
    sel.register(udp, selectors.EVENT_READ, "udp")

    while True:
        for key, _ in sel.select(timeout=0.02):
            if key.data == "udp":
                while True:
                    try:
                        data, addr = udp.recvfrom(2048)
                    except BlockingIOError:
                        break
                    except OSError:
                        # Windows raises WSAECONNRESET on recvfrom after an ICMP
                        # port-unreachable for an earlier datagram. Not fatal.
                        break
                    handle_datagram(udp, sessions, sel, data, addr,
                                    (lobby_host, lobby_port), rate)
            else:
                sess = key.data
                try:
                    buf = sess.tcp.recv(65536)
                except BlockingIOError:
                    continue
                except OSError as e:
                    log(f"session {sess.sid:#010x}: lobby read failed, {e}")
                    sess.dead = True
                    continue
                if not buf:
                    log(f"session {sess.sid:#010x}: the lobby closed the connection")
                    sess.dead = True
                    continue
                sess.out += buf

        for sess in list(sessions.values()):
            if not sess.dead:
                sess.poll(udp)
            if sess.dead:
                try:
                    sel.unregister(sess.tcp)
                except (KeyError, ValueError):
                    pass
                sess.close(udp)
                del sessions[sess.sid]


def handle_datagram(udp, sessions, sel, data, addr, lobby_addr, rate):
    if len(data) < HDR.size:
        return
    magic, ver, flags, sid, seq, ack, plen = HDR.unpack_from(data, 0)
    if magic != MAGIC or ver != VERSION:
        return
    if HDR.size + plen > len(data):
        return
    payload = data[HDR.size:HDR.size + plen]

    sess = sessions.get(sid)

    if flags & F_SYN:
        if sess is None:
            try:
                sess = Session(sid, addr, lobby_addr, rate)
            except OSError as e:
                log(f"cannot reach the lobby for session {sid:#010x}: {e}")
                return
            sessions[sid] = sess
            sel.register(sess.tcp, selectors.EVENT_READ, sess)
        # Idempotent: a repeated SYN means our SYN|ACK was lost. A genuinely new
        # connection arrives under a new session id.
        sess.addr = addr
        sess.last_rx = time.monotonic()
        sess.emit(udp, flags=F_SYN)
        return

    if sess is None:
        # Data for an unknown session: reset it rather than let it retransmit into silence.
        try:
            udp.sendto(HDR.pack(MAGIC, VERSION, F_RST, sid, 0, 0, 0), addr)
        except OSError:
            pass
        return

    # Follow the client if its NAT mapping moves: the session id identifies it, not the
    # address it arrived from.
    if sess.addr != addr:
        log(f"session {sid:#010x} moved {sess.addr} -> {addr}")
        sess.addr = addr

    sess.input(udp, flags, seq, ack, payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bind", default="0.0.0.0")
    # The UDP port is the lobby's TCP port + 10, so one convention covers any number of
    # lobbies on a host. Defaults are the standard pair; pass both for anything else.
    ap.add_argument("--port", type=int, default=7664,
                    help="UDP port clients reach this gateway on")
    ap.add_argument("--lobby-host", default="127.0.0.1")
    ap.add_argument("--lobby-port", type=int, default=7654,
                    help="the sr2lobby.py this gateway feeds")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE,
                    help="bytes/sec to the client. Do not raise: the game's read loop stops "
                         "permanently above modem speed")
    a = ap.parse_args()

    try:
        run(a.bind, a.port, a.lobby_host, a.lobby_port, a.rate)
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
