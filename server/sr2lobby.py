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
"""A Dwango lobby server for Sega Rally 2 (Dreamcast, JP v1.04).

The game dials a modem and speaks the Dwango C203 protocol over the raw byte
stream. There is no PPP and no IP. This server answers that protocol, forms
teams, runs the launch handshake, and relays the peer-to-peer race traffic
between the players once the race module is running.

The lobby exchange, per client:

    guest -> 566    hello
      us  -> 501    C203_INIT
    guest -> 1006   login, the player name at offset 0x1a
      us  -> 514    C203_LOBBYINFO, the lobby name at 0x1a
      us  -> 521    C203_WhoStart      the user list follows
      us  -> 502    C203_WHO           one per player, name at 0x1a
      us  -> 522    C203_WhoStop       the list is complete

The user list goes to every client whenever the roster changes. C203_WHO looks
a name up before it adds it, so identical names collapse into one entry.

The launch handshake, after the leader presses the start button. The message
types are consecutive:

    guest -> 536    the leader asks to start
      us  -> 537    C203_TeamInfo      member count, node index, PAD mode
      us  -> 538    C203_NodeAddress   one per node, per console
    guest -> 573    every expected node has reported
      us  -> 539    C203_LaunchModule  the race module starts
"""
import os
import socket
import select
import time
import traceback

import c203

PORT = int(os.environ.get("SR2_PORT", "7654"))
LOBBY_NAME = "SEGA RALLY 2"
# The client keys its user list by name, so each connection gets a distinct one.
PLAYER_NAMES = ["RALLY2", "PLAYER2", "PLAYER3", "PLAYER4"]

TYPE_INIT = 501
TYPE_WHO = 502
TYPE_SAY = 504        # client -> server, the player typed a chat line
TYPE_MESSAGE = 506    # server -> client, display this line in the chat area
TYPE_LOBBYINFO = 514
TYPE_LISTSTART = 516
TYPE_LISTELEMENT = 517
TYPE_LOBBYLISTDONE = 518
TYPE_ENTERROOM = 519      # client -> server, move me to this chat room
TYPE_SYNC = 520
TYPE_WHOSTART = 521
TYPE_WHOSTOP = 522
TYPE_TEAMLISTREQ = 523    # client -> server, asks for the joinable team list
TYPE_CHANGECLIENTGAME = 526
TYPE_TEAMLISTDONE = 533
TYPE_JOINTEAM = 535       # client -> server, join this existing team
TYPE_TEAMINFO = 537
TYPE_NODEADDRESS = 538
TYPE_LAUNCHMODULE = 539
TYPE_TEAMINFO_ACK = 572   # client -> server, sent by the TeamInfo handler
TYPE_NODESREADY = 573     # client -> server, every expected node has reported
TYPE_HELLO = 566
TYPE_LOGIN = 1006

# C203_GameListDone. The connect state machine waits on the byte at state+0xe6
# in its state 9, and this message is the only thing that sets it. Without it
# the client times out after 5000 ms and drops back to the lobby.
TYPE_GAMELISTDONE = 524

# The rooms this server offers. The game calls a race room a "team".
#
# The order decides a room's index in the client's name table, and the
# C203_LOBBYINFO handler stores that index at state+0xe4. The start press reads
# it as the number of player records to walk, so a client sitting in the FIRST
# room read a count of 0 and the press did nothing. A filler room ahead of the
# real one moves the index off zero.
ROOMS = ["SEGA RALLY 2", "TEAM ALPHA", "TEAM BRAVO", "TEAM CHARLIE"]
TEAMS = ["TEAM A", "TEAM B"]

# How to wake the client.
#
# The connect state machine will not speak until its state 4 advances, and that
# happens when the AT response reader sees CONNECT. On real hardware the MODEM
# prints that text. Flycast's modem model is register level and emits no AT
# result codes, so nothing in the emulator ever says it. The server supplies it
# to stand in for the missing piece of modem emulation.
WAKE_TEXT = bytes([13, 10]) + b"CONNECT 33600" + bytes([13, 10])

# UTF-8, not the platform default. Team and player names arrive in Shift-JIS,
# and a decoded name raised UnicodeEncodeError inside the log call on a cp1252
# console, which killed the server the moment a player created a team.
LOG = open("sr2lobby.log", "w", encoding="utf-8")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # The console keeps the platform encoding even when the file does not.
        print(line.encode("ascii", "backslashreplace").decode(), flush=True)
    LOG.write(line + "\n")
    LOG.flush()


def make(mtype, fields=None, size=0xB0):
    """A lobby message with strings at the offsets the handlers read.

    0xB0 covers the largest layout in use: C203_WHO reads as far as 0xa4.
    """
    m = bytearray(size)
    m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = mtype.to_bytes(2, "little")
    for off, text in (fields or {}).items():
        raw = text.encode("latin1")[:19]
        m[off:off + len(raw)] = raw          # zero fill terminates the string
    return c203.sign(bytes(m))


class Client:
    def __init__(self, conn, addr, name):
        self.conn = conn
        self.addr = addr
        self.deframer = c203.Deframer()
        self.name = name
        self.in_lobby = False
        self.last_wake = 0.0
        self.wakes = 0
        self.room = ROOMS[0]       # every client starts in the first room
        self.team = b""            # the team name, as the client spelled it
        self.realname = b""        # the name the client gave itself at login
        self.leader = False        # created the team, so leads it
        self.ready = False         # every node it expects has reported
        self.relayed = 0           # untyped frames relayed to peers
        self.hubbed = 0            # typed frames forwarded to peers

    def send(self, msg, label):
        try:
            self.conn.sendall(c203.frame(msg))
            log(f"  -> {self.name}: {label}")
        except OSError as e:
            log(f"  -> {self.name}: send failed, {e}")

    def pump_wakeup(self):
        """Send the modem CONNECT text until the client answers."""
        if self.in_lobby:
            return
        now = time.time()
        if now - self.last_wake >= 3.0:
            self.last_wake = now
            self.wakes += 1
            try:
                self.conn.sendall(WAKE_TEXT)
            except OSError:
                pass
            if self.wakes <= 2 or self.wakes % 5 == 0:
                log(f"  -> {self.name}: modem CONNECT #{self.wakes}")


class Lobby:
    def __init__(self):
        self.clients = {}
        self.next_name = 0
        self.last_rearm = 0.0
        # Teams created by players: (id, name as the client sent it).
        self.teams = []
        # Teams whose leader has sent type 536, the start request. Only these
        # get the launch handshake and the LaunchModule.
        self.launching = set()

    def roster(self):
        return [c for c in self.clients.values() if c.in_lobby]

    def team_members(self, raw):
        return [c for c in self.roster() if c.team == raw]

    def broadcast_userlist(self):
        """Send the user list to every client, on every roster change.

        The list is per room. A player in TEAM ALPHA must not appear in the
        occupancy of SEGA RALLY 2, or the room list counts lie.
        """
        for c in self.roster():
            here = [o for o in self.roster() if o.room == c.room]
            c.send(make(TYPE_WHOSTART), "C203_WhoStart")
            for other in here:
                c.send(self.who_record(other), f"C203_WHO {other.name}"
                       + (f" team={other.team!r}" if other.team else ""))
            c.send(make(TYPE_WHOSTOP), "C203_WhoStop")
        rooms = {}
        for c in self.roster():
            rooms.setdefault(c.room, []).append(c.name)
        log(f"rooms are now {rooms}")

    def send_room_lists(self, client):
        """Push the room list. The element carries its name at offset 0x1a.

        C203_LobbyListDone terminates a ROOM list; C203_TeamListDone terminates
        a TEAM list. The terminator is what decides where the elements land.
        """
        client.send(make(TYPE_LISTSTART), "C203_ListStart")
        for room in ROOMS:
            client.send(make(TYPE_LISTELEMENT, {0x1a: room}),
                        f"C203_ListElement {room}")
        client.send(make(TYPE_LOBBYLISTDONE), "C203_LobbyListDone")

    def send_team_list(self, client):
        """Push every known team, so a second player can see and join one.

        The client never asks for this list. It displays what the server has
        already pushed, so the server must push on every change or the join
        menu stays empty and reads "there are no teams you can join".
        """
        client.send(make(TYPE_LISTSTART), "C203_ListStart")
        for team_id, raw_name in self.teams:
            try:
                shown = raw_name.decode("shift_jis")
            except UnicodeDecodeError:
                shown = raw_name.decode("latin1", "replace")
            m = bytearray(0xB0)
            m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_LISTELEMENT.to_bytes(2, "little")
            m[0x1a:0x1a + len(raw_name)] = raw_name    # send the bytes it gave us
            client.send(c203.sign(bytes(m)), f"C203_ListElement team {shown!r}")
        client.send(make(TYPE_TEAMLISTDONE), "C203_TeamListDone")

    def who_record(self, member):
        """One user-list entry, carrying the player's ROOM and TEAM.

        The C203_WHO handler reads:
            0x1a  the player name
            0x2e  the room name, resolved to a room id
            0x56  the team name, resolved within that room id
            0x88  a leading '*' marks this player as the team LEADER
            0xa4  non-zero sets three per-player flags

        The client treats its create-team request as finished only when it sees
        ITS OWN entry carrying a team id that differs from the one it held. So
        the entry must use the name the client gave itself, and the room name
        must be right, because the team lookup is scoped by the room id.

        The leader mark at 0x88 chooses the menu: the handler stores
        (msg[0x88] == '*') in the player record, and the menu builder reads it
        back. Non-zero picks the menu that carries the start command. The same
        flag decides who HOSTS the DirectPlay session.

        The dword at 0xa4 stays zero. Setting it marks a player as already
        busy, and a team whose members are busy is not offered to a joiner.
        """
        m = bytearray(0xB0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_WHO.to_bytes(2, "little")
        # The player's OWN name, exactly as it registered it. Never our label.
        name = (member.realname or member.name.encode("latin1"))[:0x13]
        m[0x1a:0x1a + len(name)] = name
        room = member.room.encode("latin1")[:0x13]
        m[0x2e:0x2e + len(room)] = room
        if member.team:
            team = member.team[:0x13]          # the client's own bytes, Shift-JIS
            m[0x56:0x56 + len(team)] = team
            if member.leader:
                m[0x88] = ord("*")
        return c203.sign(bytes(m))

    def send_team_info(self, raw):
        """The launch handshake: C203_TeamInfo, then the node table.

        C203_TeamInfo, type 537. The handler stores:
            0x1a  team id
            0x1c  member count, which becomes the node count at state+0xe0
            0x1e  the RECIPIENT'S OWN node index
            0x20  four 16-bit settings
            0x28  a flags word
            0x2c  the team name

        The field at 0x1e is not a capacity. The service provider's device open
        requires 0 <= index < member count <= 5, or it fails with NOTLOBBIED.
        Every console is LOCALLY node 0: the provider fills only node-table row
        0 with the self PAD, but every send path reads the source PAD from
        row[index]. A joiner told index 1 read a row that is never filled, so it
        advertised PAD 0xff and the host could not place it. Send index 0 to
        every member.

        The flags word at 0x28 carries bit 0x80000, which the provider tests to
        enable PAD MODE. Without it remote node PADs stay 0xff, the host's
        reliable unicast is stamped for the broadcast node, the class byte
        disagrees, and the message is dropped unacknowledged.

        C203_NodeAddress, type 538, then names each node:
            0x1a  the node index, relative to the RECIPIENT
            0x1c  the PAD, which is GLOBALLY unique
            0x1e  a 15-byte name

        DAPI uses local numbering. Each console is node 0 in its own view, and
        consoles are told apart by the PAD. So each target gets itself as node
        0, carrying its own PAD, then the peers as nodes 1..N-1.
        """
        members = self.team_members(raw)
        if not members:
            return
        team_id = next((t[0] for t in self.teams if t[1] == raw), 1)
        try:
            shown = raw.decode("shift_jis")
        except UnicodeDecodeError:
            shown = raw.decode("latin1", "replace")

        m = bytearray(0xB0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_TEAMINFO.to_bytes(2, "little")
        m[0x1a:0x1c] = team_id.to_bytes(2, "little")
        m[0x1c:0x1e] = len(members).to_bytes(2, "little")   # the real count
        m[0x28:0x2c] = (0x00080000).to_bytes(4, "little")   # PAD mode bit
        m[0x2c:0x2c + len(raw[:0x13])] = raw[:0x13]
        for c in members:
            m[0x1e:0x20] = (0).to_bytes(2, "little")        # every console is node 0
            c.send(c203.sign(bytes(m)),
                   f"C203_TeamInfo {shown!r} members={len(members)} node=0")

        # Arm the launch. Nothing clears state+0xe6, and the TeamInfo handler
        # leaves it alone, so setting it here holds.
        for c in members:
            c.send(make(TYPE_GAMELISTDONE), "C203_GameListDone")

        # Name every node, once per console. Leader takes PAD 1, then 2, 3.
        ranked = sorted(members, key=lambda c: not c.leader)  # leader first
        pad_of = {c: i + 1 for i, c in enumerate(ranked)}
        for target in members:
            ordered = [target] + [c for c in ranked if c is not target]
            for node_idx, c in enumerate(ordered):
                n = bytearray(0xE0)
                n[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_NODEADDRESS.to_bytes(2, "little")
                n[0x1a:0x1c] = node_idx.to_bytes(2, "little")     # index, target=0
                n[0x1c:0x1e] = pad_of[c].to_bytes(2, "little")    # GLOBAL PAD
                who = (c.realname or c.name.encode("latin1"))[:0xF]
                n[0x1e:0x1e + len(who)] = who
                target.send(c203.sign(bytes(n)),
                            f"C203_NodeAddress[{target.name}] node {node_idx} "
                            f"= {c.name} pad {pad_of[c]}")

    def launch_after_team(self, client):
        """Start the race module, once every node has reported.

        C203_LaunchModule, type 539, carries five 20-byte strings from offsets
        0x1a, 0x2e, 0x42, 0x56 and 0x6a. Its handler runs only when the node
        table is complete. The strings feed the DirectPlay session setup.
        """
        # A node 0 registration precedes the launch. This repeats what
        # send_team_info already sent, and it was present in the run that first
        # raced. Its effect has not been established on its own.
        m = bytearray(0xE0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_NODEADDRESS.to_bytes(2, "little")
        m[0x1a:0x1c] = (0).to_bytes(2, "little")
        m[0x1c:0x1e] = (1).to_bytes(2, "little")
        addr = b"NODE0"
        m[0x1e:0x1e + len(addr)] = addr
        client.send(c203.sign(bytes(m)), "C203_NodeAddress node 0")

        m = bytearray(0xE0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_LAUNCHMODULE.to_bytes(2, "little")
        for off, text in ((0x1a, "SEGA RALLY 2"), (0x2e, "MGNETWK"),
                          (0x42, LOBBY_NAME), (0x56, client.name),
                          (0x6a, "TEAM A")):
            raw = text.encode("latin1")[:0x13]
            m[off:off + len(raw)] = raw
        client.send(c203.sign(bytes(m)), "C203_LaunchModule")

    def handle(self, client, msg):
        # THE DAPI RELAY. After LaunchModule the race layer streams frames with
        # the same 70 60 / 70 61 framing but no C203 header. They are peer
        # traffic: relay them, do not dispatch them.
        #
        # The discriminator cannot be the checksum, because the client's
        # dial-time type 566 messages carry CRC zero. Use the type range: every
        # lobby message carries a plausible type at 0x18, and the race frames
        # have entropy there.
        t_raw = int.from_bytes(msg[0x18:0x1a], "little") if len(msg) >= 0x1a else -1
        if len(msg) < 26 or not (500 <= t_raw < 1100):
            # Relay ONLY inside a team whose launch has fired. Relaying
            # room-wide forwarded dial-time handshake fragments into the other
            # client's modem stream and broke dialling.
            if not (client.team and client.team in self.launching):
                return
            peers = [c for c in self.team_members(client.team)
                     if c is not client]
            if not peers:
                return
            client.relayed += 1
            if client.relayed <= 5 or client.relayed % 50 == 0:
                log(f"  DAPI relay: {client.name} frame {client.relayed}, "
                    f"{len(msg)} bytes -> {[c.name for c in peers]}")
            for c in peers:
                try:
                    c.conn.sendall(c203.frame(msg))
                except OSError:
                    pass
            return

        t = c203.message_type(msg)

        # After LaunchModule both clients keep sending TYPED messages at
        # session-creation time. Forward those to the other members as well as
        # handling them, so the session announcement reaches the peer. 1015 is
        # the periodic status the client sends the SERVER, so it is excluded.
        if client.team and client.team in self.launching and t != 1015:
            for c in self.team_members(client.team):
                if c is not client:
                    try:
                        c.conn.sendall(c203.frame(msg))
                    except OSError:
                        pass
            client.hubbed += 1
            if client.hubbed <= 8 or client.hubbed % 25 == 0:
                log(f"  HUB: forwarded type {t} from {client.name} to team")

        if t == TYPE_HELLO:
            client.send(c203.make_init(), "C203_INIT")

        elif t == TYPE_LOGIN:
            # Keep the name the client gave itself, as raw bytes. The C203_WHO
            # handler guards everything it does for the local player behind
            # strcmp(name_in_the_entry, my_own_name), so a name we invented
            # means the client never recognises its own entry. The name is
            # Shift-JIS and must go back on the wire byte for byte.
            client.realname = msg[0x1a:0x2e].split(b"\0")[0]
            reported = client.realname.decode("latin1", "replace")
            log(f"{client.name} logged in (the client calls itself {reported!r})")
            client.send(make(TYPE_LOBBYINFO, {0x1a: LOBBY_NAME}), "C203_LOBBYINFO")
            client.in_lobby = True
            self.broadcast_userlist()
            self.send_room_lists(client)

        elif t == 530:
            # CREATE TEAM. 82 bytes: the team name at 0x1a in Shift-JIS, a
            # second string at 0x2e, four 16-bit settings at 0x42, and a flags
            # word at 0x4a.
            raw = msg[0x1a:0x2e].split(b"\0")[0]
            try:
                shown = raw.decode("shift_jis")
            except UnicodeDecodeError:
                shown = raw.decode("latin1", "replace")
            # Reuse a team of the same name. The client repeats its request
            # while it waits, and a fresh id per repeat would change its team
            # id underneath it.
            existing = [t for t in self.teams if t[1] == raw]
            if existing:
                team_id = existing[0][0]
            else:
                team_id = len(self.teams) + 1
                self.teams.append((team_id, raw))
            client.team = raw
            client.leader = True       # the creator leads the team
            log(f"  <- {client.name}: CREATE TEAM {shown!r}, id {team_id}")

            # NO C203_TeamInfo here. Its handler sets the gate byte at
            # state+0xe8 to 1, node completion sets state+0xe9, and the start
            # button requires BOTH to be zero. Only C203_LaunchModule reopens
            # that gate, so a TeamInfo at team formation disables the start
            # button permanently. TeamInfo is the server's REPLY to the start
            # request, type 536. Team id, team size and the member buckets all
            # come from the WHO records instead.
            ack = bytearray(0xB0)
            ack[c203.OFF_TYPE:c203.OFF_TYPE + 2] = (529).to_bytes(2, "little")
            ack[0x1a:0x1c] = team_id.to_bytes(2, "little")
            client.send(c203.sign(bytes(ack)),
                        f"C203_CreateTeamDialog id={team_id}")

            # Move the creator into the team screen. That screen's menu carries
            # the start commands; the main lobby menu does not.
            chg = bytearray(0xB0)
            chg[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_CHANGECLIENTGAME.to_bytes(2, "little")
            chg[0x1a:0x1c] = team_id.to_bytes(2, "little")
            chg[0x2c:0x2c + len(raw)] = raw
            client.send(c203.sign(bytes(chg)),
                        f"C203_ChangeClientGame -> team {team_id}")

            for other in self.roster():
                self.send_team_list(other)

            # The user list goes LAST. The C203_WHO handler resolves the team
            # NAME at 0x56 to a team id, and that lookup only succeeds once the
            # team is known to the client.
            self.broadcast_userlist()

        elif t == 515:
            # The LOBBY list request. Its sender clears the list-mode flag, so
            # elements sent in reply become rooms, not teams.
            log(f"  <- {client.name}: 515, asking for the lobby list")
            client.send(make(TYPE_LISTSTART), "C203_ListStart")
            for room in ROOMS:
                client.send(make(TYPE_LISTELEMENT, {0x1a: room}),
                            f"C203_ListElement {room}")
            client.send(make(TYPE_LOBBYLISTDONE), "C203_LobbyListDone")

        elif t == TYPE_SAY:
            # CHAT. The player typed a line and sent it. MEASURED: a 146-byte
            # message with the text at offset 0x2e, SINGLE BYTE, not UTF-16, so
            # Shift-JIS from the software keyboard. Typing AAAA put 41 41 41 41
            # at 0x2e.
            #
            # The text is relayed as raw bytes. Decoding it and re-encoding it
            # would corrupt every Japanese character, the same trap that broke
            # create-team.
            #
            # The sender is not named in the message, because the server knows
            # who sent it. So the server fills the speaker's name in the copy it
            # sends out.
            said = msg[0x2e:0x6e].split(b"\0")[0]
            try:
                shown = said.decode("shift_jis")
            except UnicodeDecodeError:
                shown = said.decode("latin1", "replace")
            log(f"  <- {client.name} says {shown!r} ({len(said)} bytes)")

            # Rebroadcast to everyone in the room, the speaker INCLUDED. The
            # client shows nothing of its own until the server sends it back,
            # so leaving the sender out makes chat look broken to the person
            # typing.
            #
            # C203_MESSAGE carries ONE display string, and it starts at 0x1e.
            # Offsets 0x1a to 0x1d are a separate 4-byte field.
            #
            # MEASURED, from two messages sent with the line written at 0x1a:
            #   sent 'りりりり: KKKK'  ->  drew 'りり: KKKK'
            #   sent 'を: QQQ'        ->  drew 'QQQ'
            # Each lost exactly four bytes from the front, and 'を: ' is four
            # bytes, which is why that one lost its name completely.
            who = (client.realname or client.name.encode("latin1"))[:0x13]
            line = who + b": " + said
            out = bytearray(0xB0)
            out[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_MESSAGE.to_bytes(2, "little")
            out[0x1e:0x1e + len(line[:0x80])] = line[:0x80]
            body = c203.sign(bytes(out))
            for c in [o for o in self.roster() if o.room == client.room]:
                c.send(body, f"C203_MESSAGE from {client.name}")

        elif t == 525:
            # The client asking to JOIN a lobby, with the name at 0x1a. It
            # repeats this while its chat line reads "no response from the
            # server". C203_LOBBYINFO does not accept the join: its handler
            # sets state+0xe4 while the state machine tests state+0xe5.
            # C203_Sync is the acceptance, and the client then asks who is
            # present.
            want = msg[0x1a:0x40].split(b"\0")[0].decode("latin1", "replace")
            log(f"  <- {client.name}: 525, join {want!r}, accepting with C203_Sync")
            client.send(make(TYPE_SYNC, {0x1a: want or LOBBY_NAME}),
                        "C203_Sync (join accepted)")

        elif t >= 1000 and t != 1015:
            # THE GAME-DATA CHANNEL. The race traffic rides in these
            # high-numbered messages, and the real server relayed them between
            # the players. Without the relay one client's ConnectByLobby
            # returns DP_OK and the other times out with 0x887700dc.
            peers = []
            if client.team:
                peers = [c for c in self.team_members(client.team) if c is not client]
            if not peers:
                peers = [c for c in self.roster() if c is not client]
            log(f"  <- {client.name}: game channel type {t}, {len(msg)} bytes, "
                f"relaying to {[c.name for c in peers]}")
            for c in peers:
                c.send(bytes(msg), f"relay type {t} from {client.name}")

        elif t == 540:
            # The module handoff announcement, sent once after LaunchModule.
            # The reply cannot be an echo, because the receive case for 540 is
            # in the ignore group. 541 is C203_ReInit, whose handler copies a
            # config block out of the message and reinitialises the transport.
            log(f"  <- {client.name}: 540 module notification, answering with 541")
            client.send(make(541), "C203_ReInit (empty)")

        elif t == 542:
            # C203_ReConnect. When no answer arrives the client TEARS THE
            # TRANSPORT DOWN and redials, five times, then reports no response
            # from the server. That teardown killed DirectPlay before
            # ConnectByLobby could run. The receive handler clears the pending
            # flag, the retry counter and the timestamp, so the ack is an echo.
            log(f"  <- {client.name}: 542 ReConnect, echoing")
            client.send(make(542), "C203_ReConnect echo")

        elif t == 536:
            # THE START REQUEST. The start button, with the gate open, sends
            # this bare 26-byte message. The server answers with the launch
            # handshake: TeamInfo with the real member count, then the node
            # table. The clients register the nodes, report 573, and the
            # all-ready path sends LaunchModule.
            log(f"  <- {client.name}: 536 GAME START REQUEST team={client.team!r}")
            if client.team:
                self.launching.add(client.team)
                self.send_team_info(client.team)

        elif t == TYPE_JOINTEAM:
            # Join an existing team, 48 bytes, the team name at 0x1a. The
            # answer is the same shape as create-team without the creation:
            # record the membership, then send the user list. The client's own
            # C203_WHO entry carrying the team completes the join. The joiner
            # is NOT the leader.
            raw = msg[0x1a:0x2e].split(b"\0")[0]
            try:
                shown = raw.decode("shift_jis")
            except UnicodeDecodeError:
                shown = raw.decode("latin1", "replace")
            known = [t for t in self.teams if t[1] == raw]
            log(f"  <- {client.name}: 535, join team {shown!r}"
                f"{'' if known else ' (unknown team)'}")
            if not known:
                team_id = len(self.teams) + 1
                self.teams.append((team_id, raw))
            client.team = raw
            client.leader = False
            for other in self.roster():
                self.send_team_list(other)
            self.broadcast_userlist()
            # No TeamInfo here either. Membership travels in the WHO records.

        elif t == TYPE_ENTERROOM:
            # Move to another chat room, 76 bytes with the room name at 0x1a.
            # Answer it the way the lobby join is answered: name the room,
            # accept with C203_Sync, then resend the user list, because the
            # occupancy counts come from it.
            want = msg[0x1a:0x40].split(b"\0")[0].decode("latin1", "replace")
            room = want or client.room
            log(f"  <- {client.name}: 519, move to room {room!r}")
            client.room = room
            client.send(make(TYPE_LOBBYINFO, {0x1a: room}), f"C203_LOBBYINFO {room}")
            client.send(make(TYPE_SYNC, {0x1a: room}), "C203_Sync (room entered)")
            self.broadcast_userlist()

        elif t == TYPE_TEAMLISTREQ:
            log(f"  <- {client.name}: 523, asking for the team list "
                f"({len(self.teams)} team(s) known)")
            self.send_team_list(client)

        elif t == TYPE_NODESREADY:
            # Every node the client was told to expect has reported. The
            # C203_NodeAddress handler sends this by itself once the node table
            # is full. It is the only readiness signal the client ever emits.
            client.ready = True
            log(f"  <- {client.name}: ready to race")
            if client.team:
                members = self.team_members(client.team)
                if members and all(c.ready for c in members):
                    if client.team in self.launching:
                        log(f"  every member of {client.team!r} is ready. Launching.")
                        for c in members:
                            self.launch_after_team(c)
                    else:
                        # A team that is ready but has not asked to start is
                        # waiting for its leader. The server must not start a
                        # race nobody asked for.
                        log(f"  every member of {client.team!r} is ready. "
                            f"Waiting for the leader to start.")

        elif t == TYPE_TEAMINFO_ACK:
            # The TeamInfo handler sends this itself, three lines after it sets
            # the gate at state+0xe8 and the node count at state+0xe0. So this
            # message on the wire proves the gate is armed.
            log(f"  <- {client.name}: team accepted, the launch gate is armed")

        elif t == TYPE_WHO:
            log(f"  <- {client.name}: C203_WHO, answering with the user list")
            self.broadcast_userlist()

        else:
            # Log the payload, not just the type. The client's requests carry
            # names and ids that say what they ask for.
            body = msg[0x1a:0x40]
            text = body.split(b"\0")[0].decode("latin1", "replace")
            log(f"  <- {client.name}: type {t} ({c203.name_of(t)}), "
                f"{len(msg)} bytes, name@0x1a={text!r}")
            log(f"       {msg[0x18:0x40].hex(' ')}")

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", PORT))
        srv.listen(4)
        log(f"lobby listening on port {PORT}. Start each emulator with "
            f"MODEMBRIDGE=127.0.0.1:{PORT}")

        while True:
            socks = [srv] + [c.conn for c in self.clients.values()]
            readable, _, _ = select.select(socks, [], [], 0.1)

            now = time.time()
            # Re-arm any team member that has not reported ready. A client that
            # has been sitting in a team for some minutes ignores the TeamInfo
            # we resend when someone joins, so its node table never fills.
            #
            # Only teams whose leader has pressed start are re-armed. Re-arming
            # at rest keeps state+0xe8 and 0xe9 set and holds the launch gate
            # shut.
            if now - self.last_rearm >= 4.0:
                self.last_rearm = now
                teams = {c.team for c in self.roster() if c.team}
                for raw in teams:
                    if raw not in self.launching:
                        continue
                    members = self.team_members(raw)
                    if len(members) >= 2 and not all(c.ready for c in members):
                        stale = [c.name for c in members if not c.ready]
                        log(f"  re-arming {raw!r}, still waiting on {stale}")
                        self.send_team_info(raw)

            for c in list(self.clients.values()):
                c.pump_wakeup()

            for s in readable:
                if s is srv:
                    conn, addr = srv.accept()
                    name = PLAYER_NAMES[self.next_name % len(PLAYER_NAMES)]
                    self.next_name += 1
                    self.clients[conn] = Client(conn, addr, name)
                    log(f"emulator connected from {addr[0]}:{addr[1]}, "
                        f"seated as {name}")
                    continue

                client = self.clients.get(s)
                if client is None:
                    continue
                try:
                    data = s.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    log(f"{client.name} disconnected")
                    del self.clients[s]
                    s.close()
                    self.broadcast_userlist()
                    continue

                for msg in client.deframer.feed(data):
                    # One malformed message must not take the server down. A
                    # crash here closes the socket, and the client then waits
                    # for a reply that can never arrive.
                    try:
                        self.handle(client, msg)
                    except Exception:
                        log(f"HANDLER CRASHED on a {len(msg)}-byte message "
                            f"from {client.name}")
                        log(f"  raw: {msg[:0x60].hex(' ')}")
                        for line in traceback.format_exc().splitlines():
                            log(f"  {line}")

                # THE RACE RECORD RELAY. After launch the client sends records
                # terminated 70 44 rather than 70 61. They are peer traffic:
                # relay each to the other members of the sender's team, framed
                # the way the sender framed it - 0x70 doubling, the same end
                # marker, no 70 60 start.
                if client.deframer.race:
                    records, client.deframer.race = client.deframer.race, []
                    peers = []
                    if client.team:
                        peers = [c for c in self.team_members(client.team)
                                 if c is not client]
                    for end, rec in records:
                        log(f"  RACE RECORD 70 {end:02x} from {client.name}, "
                            f"{len(rec)} bytes -> {[c.name for c in peers]}")
                        for c in peers:
                            try:
                                c.conn.sendall(c203.frame_race(rec, end))
                            except OSError:
                                pass


if __name__ == "__main__":
    try:
        Lobby().run()
    except KeyboardInterrupt:
        log("stopped")
