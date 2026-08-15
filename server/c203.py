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
"""The Sega Rally 2 lobby protocol: framing, checksum and messages.

Every fact here is read out of the game's own code, not guessed. The sources:

  DWANGO.DLL FUN_10008e80   the receiver's deframer. It defines what the game
                            accepts, which is what matters when we send to it.
  DWANGO.DLL FUN_10009114   the sender's framer. Symmetric with the above.
  DWANGO.DLL FUN_10007228   the checksum check. It rejects with "Invalid CRC".
  DWANGO.DLL FUN_100090d4   the message-type extractor.
  DWANGO.DLL FUN_10007280   the message dispatcher, with the C203 names.
  DWANGO.DLL FUN_10005ffc   the C203_INIT handler.

THE FRAME

  70 60 | payload, with every 0x70 byte sent twice | 70 61

The receiver holds one escape flag. On 0x70 it sets the flag. With the flag
set: 0x60 empties the buffer (a frame starts), 0x61 delivers the buffer (a
frame ends), and any other byte is appended as itself. So 0x70 0x70 delivers
one 0x70, and no other value needs an escape.

THE MESSAGE

  offset 0x14  uint16  checksum, little-endian
  offset 0x16  uint16  zero
  offset 0x18  uint16  message type, little-endian
  minimum length 26 bytes (0x1a)

The checksum is CRC-16/CCITT: polynomial 0x1021, most significant bit first,
initial value 0. It covers the WHOLE message, with bytes 0x14 to 0x17 set to
zero first. The game's table was compared against a reference implementation
and all 256 entries match, so this is the stock algorithm.

THE TYPES

The dispatcher adds a fixed bias to the type and switches on the result. The
bias is -501, read from the instruction that loads it, so C203_INIT is type
501. The names below are the game's own, read from the strings the dispatcher
passes to its logger.
"""

# Frame marks. FUN_10008e80.
ESC = 0x70
FRAME_START = 0x60
FRAME_END = 0x61
# The race layer's record terminators. Same 0x70 escape, no start marker.
# 0x44 carries the DirectPlay requests, 0x45 carries the enumeration replies.
#
# ANY escaped byte that is not lobby framing (0x60 start, 0x61 end, 0x70
# literal) terminates a record of that type. The server is a relay: it routes a
# record by its terminator without needing to know what the record means.
RACE_END = 0x44

# Message layout. FUN_10007228 and FUN_100090d4.
OFF_CRC = 0x14
OFF_TYPE = 0x18
MIN_LEN = 0x1a

# Type numbering. The dispatcher adds a constant to the type and switches on
# the result: case = type + bias. The load is `mov.w 0x100073a0,r5` at
# 0x100072b0, a SIXTEEN bit load, and the halfword there is 0xfe0b = -501.
# So case N is type 501 + N, and C203_INIT is type 501.
#
# This was wrong once. The send path compares the type against 500, which
# looked like the base, and a first live run sent type 500. The game ignored
# it: 500 is case -1, which is not in the switch. Read the instruction, not
# the neighbouring constant.
TYPE_BASE = 501

NAMES = {
    0x00: "C203_INIT",            0x01: "C203_WHO",
    0x02: "C203_DELUSER",         0x03: "C203_SAY",
    0x04: "C203_TELL",            0x05: "C203_MESSAGE",
    0x09: "C203_MSGSTART",        0x0a: "C203_LINE",
    0x0b: "C203_MSGSTOP",         0x0d: "C203_LOBBYINFO",
    0x0f: "C203_ListStart",       0x10: "C203_ListElement",
    0x11: "C203_LobbyListDone",   0x13: "C203_Sync",
    0x14: "C203_WhoStart",        0x15: "C203_WhoStop",
    0x17: "C203_GameListDone",    0x19: "C203_ChangeClientGame",
    0x1c: "C203_CreateTeamDialog", 0x20: "C203_TeamListDone",
    0x24: "C203_TeamInfo",        0x25: "C203_NodeAddress",
    0x26: "C203_LaunchModule",    0x28: "C203_ReInit",
    0x29: "C203_ReConnect",       0x2b: "C203_NUStageRequest",
    0x2e: "C203_NUResult",        0x34: "C203_InfoRequest",
    0x36: "C203_VerifyWADRequest", 0x37: "C203_HexenClassRequest",
    0x3b: "C203_EmailRequest",    0x44: "C203_SubscribeMsgStop",
}

# The one the game sends and repeats every 5 seconds while it waits. The send
# path holds it in DAT_1000923c, one of four types that skip the checksum and
# the sequence number, which is why the captured frames carry a zero header.
# The other three are 500, 540 and DAT_10009240 = 542.
TYPE_CLIENT_HELLO = 566
TYPE_SPECIAL = (500, 540, 542, 566)

# The reply that sets the game's "link ready" flag. FUN_10005ffc copies 0x8a
# bytes out of this message, so it must be at least that long.
TYPE_INIT = TYPE_BASE + 0x00
INIT_MIN_LEN = 0x8a


def crc16(data):
    """CRC-16/CCITT, polynomial 0x1021, most significant bit first, initial 0.
    The game uses a 256-entry table; this computes the same values."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    return crc


def message_type(msg):
    """The type field, or None when the message is too short to have one."""
    if len(msg) < MIN_LEN:
        return None
    return int.from_bytes(msg[OFF_TYPE:OFF_TYPE + 2], "little")


def name_of(msgtype):
    return NAMES.get(msgtype - TYPE_BASE, f"type {msgtype}")


def sign(msg):
    """Return the message with a correct checksum at offset 0x14.

    The check zeroes bytes 0x14 to 0x17 before computing, so this does the
    same, then writes the result into the low half of that field."""
    m = bytearray(msg)
    if len(m) < OFF_CRC + 4:
        raise ValueError("a message must be at least 24 bytes to hold a checksum")
    m[OFF_CRC:OFF_CRC + 4] = b"\0\0\0\0"
    crc = crc16(bytes(m))
    m[OFF_CRC:OFF_CRC + 2] = crc.to_bytes(2, "little")
    return bytes(m)


def check(msg):
    """True when the checksum in the message is the one the game would compute."""
    if len(msg) < OFF_CRC + 4:
        return False
    stored = int.from_bytes(msg[OFF_CRC:OFF_CRC + 2], "little")
    m = bytearray(msg)
    m[OFF_CRC:OFF_CRC + 4] = b"\0\0\0\0"
    return stored == crc16(bytes(m))


def frame(payload):
    """Wrap a message in the link framing, doubling every 0x70."""
    out = bytearray([ESC, FRAME_START])
    for b in payload:
        if b == ESC:
            out.append(ESC)
        out.append(b)
    out += bytes([ESC, FRAME_END])
    return bytes(out)


def frame_race(payload, end=RACE_END):
    """Wrap a race-layer record: 70 70 doubling, a 70 4x terminator carrying
    the record type, NO 70 60 start. MEASURED from the raw socket log: race
    records chain directly after the previous terminator."""
    out = bytearray()
    for b in payload:
        out.append(b)
        if b == ESC:
            out.append(ESC)
    out += bytes([ESC, end])
    return bytes(out)


# The largest thing a peer can legitimately send between two frame markers.
# Real messages are 176 bytes and race records are smaller. This cap only
# exists to stop a peer that never sends a terminator from growing the buffer
# without limit. On a public server that stream is a trivial memory-exhaustion
# attack; on any server it is a bug. 64 KiB is far above any real frame.
MAX_FRAME = 0x10000


class DeframeError(Exception):
    """A peer sent more than MAX_FRAME bytes with no terminator. The owner
    should drop the connection; the byte stream is not the game's."""


class Deframer:
    """The receiving half, written to mirror FUN_10008e80 exactly.

    Feed it bytes; it yields complete messages. It is a class because the
    escape flag and the buffer have to survive between reads from the socket.
    """

    def __init__(self):
        self.buf = bytearray()
        # Race records (70 44 terminated) land here instead of the return
        # value, so every existing caller of feed() keeps its behaviour. The
        # owner drains this list after each feed.
        self.race = []
        self.escaped = False

    def feed(self, data):
        out = []
        for b in data:
            if not self.escaped:
                if b == ESC:
                    self.escaped = True
                else:
                    self.buf.append(b)
                    if len(self.buf) > MAX_FRAME:
                        raise DeframeError()
                continue
            self.escaped = False
            if b == FRAME_START:
                self.buf.clear()
            elif b == FRAME_END:
                out.append(bytes(self.buf))
                self.buf.clear()
            elif b != ESC:
                # A race-layer record. It ends with 70 4x instead of 70 61,
                # keeps the same 70 70 doubling, and has NO 70 60 start marker,
                # so records chain straight after the previous terminator.
                #
                # Before this branch existed the terminator fell through to the
                # append below, so a race record CONTAMINATED the buffer and
                # rode into the front of the next lobby frame.
                self.race.append((b, bytes(self.buf)))
                self.buf.clear()
            else:
                self.buf.append(b)
                if len(self.buf) > MAX_FRAME:
                    raise DeframeError()
        return out


def make_init(params=None):
    """Build a C203_INIT. This is the reply that sets the game's ready flag.

    The handler copies 0x8a bytes, so the message is that long. The parameter
    fields are left zero until the live test shows which ones the game acts on.
    """
    msg = bytearray(INIT_MIN_LEN)
    msg[OFF_TYPE:OFF_TYPE + 2] = TYPE_INIT.to_bytes(2, "little")
    if params:
        for off, val in params.items():
            msg[off:off + 4] = int(val).to_bytes(4, "little")
    return sign(bytes(msg))


if __name__ == "__main__":
    # Self-check against the captured traffic and against the game's rules.
    init = make_init()
    assert check(init), "our own checksum must verify"
    assert message_type(init) == TYPE_INIT == 501
    print(f"C203_INIT: {len(init)} bytes, type {message_type(init)}, "
          f"crc {int.from_bytes(init[OFF_CRC:OFF_CRC+2], 'little'):#06x}, check ok")

    # The framing must survive a round trip, including a payload 0x70.
    probe = bytes([0x70, 0x00, 0x70, 0x70, 0x61, 0x60])
    d = Deframer()
    got = d.feed(frame(probe))
    assert got == [probe], f"round trip failed: {got}"
    print("framing round trip with embedded 0x70: ok")

    # And it must deframe a real captured frame from the game.
    captured = bytes.fromhex(
        "7060" + "00" * 24 + "3602c600000004000000000100" + "00" + "7061")
    msgs = Deframer().feed(captured)
    print(f"captured frame -> {len(msgs[0])} byte message, "
          f"type {message_type(msgs[0])} = {name_of(message_type(msgs[0]))}")
