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
# A label for the LOG ONLY, one per connection, counted upward without a limit.
#
# This used to be a fixed list of four names, indexed with a modulo, so the
# fifth player was handed the first name again and two connections claimed one
# identity. That was the 4-player cap.
#
# Nothing here reaches the wire. A player is identified by the name the CLIENT
# sent at login, held in client.realname and made unique by unique_name(). That
# is what the real server did: it never invented a name, it accepted yours and
# told you with C203_UserNameResult (1011) when it had to change it.
CONN_LABEL = "CONN{:d}"

TYPE_INIT = 501
TYPE_WHO = 502
TYPE_DELUSER = 503    # server -> client, remove one player from the user list
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
TYPE_LEAVE = 534         # client -> server, sent just before it hangs up
TYPE_HELLO = 566
TYPE_LOGIN = 1006
# C203_UserNameResult. MEASURED: the receive dispatcher runs a SECOND switch
# whose bias composes with the first, so case = type - 1008. Its handler copies
# the string at 0x1a into a global and raises the username-changed event, so the
# server uses this to tell a client which name it actually got. This is how a
# duplicate name was meant to be settled: rename, not refuse.
TYPE_USERNAMERESULT = 1011

# C203_GameListDone. The connect state machine waits on the byte at state+0xe6
# in its state 9, and this message is the only thing that sets it. Without it
# the client times out after 5000 ms and drops back to the lobby.
TYPE_GAMELISTDONE = 524

# THE MESSAGE OF THE DAY. Three messages open, fill and close one text block.
# Read from the DWANGO.DLL handlers, cases 9, 10 and 11 of the first switch:
#   FUN_100070f8  MSGSTART  no fields. It opens the buffer.
#   FUN_1000710c  LINE      ONE string at 0x1a. It appends the line.
#   FUN_10007124  MSGSTOP   no fields. It closes the buffer, then posts event
#                           0x10 to the lobby, which draws the block.
TYPE_MSGSTART = 510
TYPE_LINE = 511
TYPE_MSGSTOP = 512

# THE USER SEARCH, the lobby's "find" command.
#
# The client sends 1007 when the player confirms a name. MEASURED from a live
# press: 55 bytes, the name in Shift-JIS at 0x1a, the same character as UTF-16
# at 0x2c, and two guest RAM pointers we do not need. 1007 has NO case in
# either dispatcher switch, so it is a request only; the client never receives
# it. Before this handler existed the server treated it as race data and
# relayed it to the other players, so the search always came back empty.
#
# The answer is a list, in the same shape as every other list here.
# C203_SearchResult is handled by DWANGO FUN_100067d0, which copies four fields
# into a 0x44-byte record:
#     strcpy(rec+0x00, msg+0x1a)      20 bytes
#     memcpy(rec+0x14, msg+0x2f, 8)    8 bytes, binary, purpose not known
#     strcpy(rec+0x1c, msg+0x37)      20 bytes
#     strcpy(rec+0x30, msg+0x4c)      24 bytes
# INFERRED, and the one thing here that is not read from code: 0x37 is the room
# and 0x4c is the team. The offsets and sizes are MEASURED; only the meaning of
# the last two strings is a reading of what a "where is this player" answer
# needs. One run on the real client settles it.
TYPE_SEARCHREQ = 1007
TYPE_SEARCHSTART = 1008
TYPE_SEARCHSTOP = 1009
TYPE_SEARCHRESULT = 1010

# Shown to every player as they enter the lobby. Edit freely. Each entry is one
# line. Keep them short: the chat area is narrow.
MOTD = [
    "SR2:REDWANGO",
    "",
    "Sega Rally 2 online, restored.",
    "The first server since Dwango closed.",
    "",
    "Create a team, then race.",
    "Have fun.",
]

# The rooms this server offers. The game calls a race room a "team".
#
# The order decides a room's index in the client's name table, and the
# C203_LOBBYINFO handler stores that index at state+0xe4. The start press reads
# it as the number of player records to walk, so a client sitting in the FIRST
# room read a count of 0 and the press did nothing. A filler room ahead of the
# real one moves the index off zero.
ROOMS = ["SEGA RALLY 2", "GAMEOVERYEAH", "CRAIGSTADLER", "KATANA"]
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


def sjis_trim(raw, limit):
    """Cut a Shift-JIS byte string to at most `limit` bytes, on a CHARACTER
    boundary.

    Shift-JIS is multi-byte: a lead byte in 0x81-0x9f or 0xe0-0xfc is followed
    by a trailing byte. Slicing at an arbitrary offset can leave a dangling lead
    byte, which draws as a broken glyph or swallows the byte after it. The user
    list, the chat line and the rename all cut names, so they all need this.
    """
    out = 0
    while out < len(raw):
        lead = raw[out]
        width = 2 if (0x81 <= lead <= 0x9F or 0xE0 <= lead <= 0xFC) else 1
        if out + width > limit:
            break
        out += width
    return raw[:out]


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
        self.racing = False        # the race module has this player
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
        # Teams whose launch has ALREADY fired. Separate from `launching`,
        # which must stay populated: the race relay at handle() forwards peer
        # frames only for a team that is in `launching`, so removing a team
        # there to stop a second launch would silence the race itself.
        self.launched = set()

    def roster(self):
        return [c for c in self.clients.values() if c.in_lobby]

    def team_members(self, raw):
        return [c for c in self.roster() if c.team == raw]

    def unique_name(self, wanted, me):
        """Return a name nobody else in the lobby is using.

        MEASURED 2026-08-14: two players who logged in with the same name broke
        the lobby for BOTH. C203_WHO looks a name up before it adds it, so the
        second player folded into the first entry. One client showed a room
        count of 1 and one user; the other showed 0 and an EMPTY user list, and
        could not see itself.

        The original service never met this case: DWANGO was a subscription
        service with accounts, so names were unique by registration. That is
        why DWLOBBY.DLL holds no "name taken" text - the lobby never had to
        report it. The server has to guarantee what the account system used to.
        """
        taken = {c.realname for c in self.roster() if c is not me}
        if wanted and wanted not in taken:
            return wanted
        # A client that sent no name at all still needs one, or every nameless
        # player collides with every other.
        stem = wanted or b"PLAYER"
        # Try a numeric suffix. The stem is cut on a CHARACTER boundary, so a
        # Shift-JIS name is never sliced through the middle of a character.
        for n in range(2, 1000):
            suffix = str(n).encode("ascii")
            cand = sjis_trim(stem, 0x13 - len(suffix)) + suffix
            if cand not in taken:
                return cand
        # 998 players share one name. Not reachable in practice, but returning
        # the duplicate would put us back to the bug this exists to prevent, so
        # fall back to something that cannot collide.
        return sjis_trim(stem, 0x13 - 8) + os.urandom(4).hex().encode("ascii")

    def announce(self, room, text):
        """Put a line in the chat area of everyone in a room.

        Uses the same message the players' own chat uses. The display string
        starts at 0x1e, not at 0x1a.
        """
        out = bytearray(0xB0)
        out[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_MESSAGE.to_bytes(2, "little")
        raw = text if isinstance(text, bytes) else text.encode("latin1", "replace")
        out[0x1e:0x1e + len(raw[:0x80])] = raw[:0x80]
        body = c203.sign(bytes(out))
        for c in [o for o in self.roster() if o.room == room]:
            c.send(body, "C203_MESSAGE (system)")

    def send_motd(self, client):
        """Put the welcome lines in this player's chat area.

        WHY NOT MSGSTART / LINE / MSGSTOP, which is the mechanism built for
        exactly this. It was tried first and the client drew nothing, although
        all nine messages left the server without error.

        The reason is in the game, not in us. MEASURED: the MSGSTOP handler
        (DWANGO FUN_10007124) finishes the block and posts event 0x10 to the
        lobby. DWLOBBY dispatches lobby events through a jump table at
        0x10002480, indexed by event minus 0x0c, so event 0x10 is entry 4. That
        entry is 0x100026b2, which is `bra 0x10002702; nop` - a jump to the
        function epilogue. The lobby IGNORES the event.

        DWANGO does store the lines, but nothing can read them back: the whole
        client API is DWL_GetGameInfo, DWL_GetLobbyInfo, DWL_GetSearchUserResult,
        DWL_GetServerInfo, DWL_GetTeamInfo and DWL_GetUserInfo. There is no
        message getter. So the message of the day is a Dwango feature that Sega
        Rally 2 never connected to its interface.

        C203_MESSAGE draws. The chat and the join announcements already use it.
        """
        for line in MOTD:
            out = bytearray(0xB0)
            out[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_MESSAGE.to_bytes(2, "little")
            raw = line.encode("latin1", "replace")[:0x80]
            out[0x1e:0x1e + len(raw)] = raw
            client.send(c203.sign(bytes(out)), f"C203_MESSAGE motd {line!r}")

    def send_search_results(self, client, wanted):
        """Answer a find request: start, one result per match, stop.

        The block is sent even when nothing matches. The client draws its own
        "user not found" line when the list closes empty, so an empty answer is
        a real answer and silence is not.
        """
        client.send(make(TYPE_SEARCHSTART), "C203_SearchResultStart")
        hits = [c for c in self.roster() if c.realname == wanted]
        for other in hits:
            m = bytearray(0xB0)
            m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_SEARCHRESULT.to_bytes(2, "little")
            for off, raw, limit in ((0x1a, other.realname, 0x13),
                                    (0x37, other.room.encode("latin1"), 0x13),
                                    (0x4c, other.team, 0x17)):
                cut = sjis_trim(raw, limit)
                m[off:off + len(cut)] = cut
            client.send(c203.sign(bytes(m)),
                        f"C203_SearchResult {other.realname!r} in {other.room!r}")
        log(f"  search for {wanted!r}: {len(hits)} match(es)")
        client.send(make(TYPE_SEARCHSTOP), "C203_SearchResultStop")

    def leave_race(self, client):
        """The player came back from the race. Put them back in the lobby clean.

        MEASURED 2026-08-14, three clients that finished a race and exited:
        each one sent 542 ReConnect, then 540, and then re-sent 535 to join the
        team it was ALREADY in. The server still held the old membership, the
        old leader flag and the old ready flag, so the re-join stacked on top of
        stale state and the team ended up with no leader at all.

        So a return from the race resets the player completely. They keep their
        name, their room and their place in the lobby; they lose the team, the
        leader mark, the ready flag and the busy flag. A fresh 535 then behaves
        exactly like a first join, which is what the client expects.

        This is NOT depart(). The player stays in the lobby and stays in the
        user list.
        """
        if not (client.racing or client.team or client.leader or client.ready):
            return                      # nothing to undo; 542 also arrives idle

        team = client.team
        log(f"  {client.name} returned from the race, clearing team state")

        client.racing = False
        client.team = b""
        client.leader = False
        client.ready = False

        if team:
            members = self.team_members(team)
            if not members:
                self.teams = [t for t in self.teams if t[1] != team]
                self.launching.discard(team)
                self.launched.discard(team)
                log(f"  team {team!r} is empty now, forgetting it")
            elif not any(m.leader for m in members):
                # Same rule as depart(): a team with no leader cannot start a
                # race, because only the leader is shown the start command.
                members[0].leader = True
                log(f"  {members[0].name} is the new leader of {team!r}")

        # Tell everyone. The team list changed, and this player's own record
        # changed twice over: no team, and no longer busy.
        for other in self.roster():
            self.send_team_list(other)
        self.push_user(client)

    def depart(self, client, why):
        """Take a client out of the shared state, cleanly.

        MEASURED 2026-08-14: when a player closed the window, the user list
        updated but the player stayed in their team. If that player was the
        LEADER, the team was left with no leader, so it could never start a
        race and the remaining member had no way out except reconnecting. The
        team also stayed in `self.teams` and in `self.launching` for ever.

        Called from two places, because a client can leave two ways: it sends
        type 534 and then drops the line, or the socket simply dies. The guard
        stops the work happening twice.
        """
        if getattr(client, "departed", False):
            return
        client.departed = True

        room = client.room
        who = (client.realname or client.name.encode("latin1"))
        team = client.team

        client.in_lobby = False        # drop out of the roster and the user list
        client.team = b""
        client.leader = False
        client.ready = False

        if team:
            members = self.team_members(team)
            if not members:
                # nobody is left in it, so the team stops existing
                self.teams = [t for t in self.teams if t[1] != team]
                self.launching.discard(team)
                self.launched.discard(team)
                log(f"  team {team!r} is empty now, forgetting it")
            elif not any(m.leader for m in members):
                # the leader left. Promote someone, or the team is stuck: the
                # start command is shown to the leader only.
                members[0].leader = True
                log(f"  {members[0].name} is the new leader of {team!r}")
                self.announce(room, (members[0].realname or
                                     members[0].name.encode("latin1")) + b" now leads the team")

        self.announce(room, who + b" has left")
        self.drop_user(client, room)

    def send_full_list(self, client):
        """Send the whole user list to ONE client.

        Only the players in this client's room. The client does not filter by
        the room field itself: it lists and counts everything it is sent.
        """
        here = [o for o in self.roster() if o.room == client.room]
        client.send(make(TYPE_WHOSTART), "C203_WhoStart")
        for other in here:
            client.send(self.who_record(other), f"C203_WHO {other.name}")
        client.send(make(TYPE_WHOSTOP), "C203_WhoStop")

    def push_user(self, member, room=None):
        """Tell everyone in a room about ONE player, added or changed.

        The C203_WHO handler looks a name up before it adds it, so sending a
        record for a player who is already listed UPDATES that entry. The same
        message therefore serves both "someone arrived" and "someone's team
        changed".
        """
        rec = self.who_record(member)
        for c in [o for o in self.roster() if o.room == (room or member.room)]:
            c.send(rec, f"C203_WHO {member.name}"
                   + (f" team={member.team!r}" if member.team else ""))

    def drop_user(self, member, room):
        """Tell everyone in a room to remove ONE player from their list.

        C203_DELUSER deletes a row from the displayed user list. It does not
        delete anything else; the name is the key, at 0x1a as in C203_WHO.

        This is why the message exists. Resending the whole list on every change
        costs (players x players) messages, and on the 33.6k link the game
        expects that is about a second of dead air per player at twenty players.
        """
        m = bytearray(0xB0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_DELUSER.to_bytes(2, "little")
        name = sjis_trim(member.realname or member.name.encode("latin1"), 0x13)
        m[0x1a:0x1a + len(name)] = name
        rec = c203.sign(bytes(m))
        for c in [o for o in self.roster() if o.room == room and o is not member]:
            c.send(rec, f"C203_DELUSER {member.name}")

    def broadcast_userlist(self):
        """Resend the whole list to everyone. Kept for the cases that still
        need it, and as the fallback if an incremental update is ever wrong."""
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

    # EXPERIMENT. The room-select dialog shows an occupancy number beside each
    # room. MEASURED: a client shows its OWN room's count correctly (it counts
    # its user list) but every other room reads 0, which is what we leave in the
    # C203_ListElement message. So the count for a room you are NOT in comes
    # from that message, in a field we have never filled.
    #
    # To find the field without guessing: write a DIFFERENT recognisable number
    # at each candidate offset. Whichever number appears on screen names the
    # offset. Set SR2_PROBE=1 to turn this on.
    def room_element(self, room, count):
        """One C203_ListElement: the room name at 0x1a, its occupancy at 0x48.

        The occupancy number is a 32-bit little-endian integer at offset 0x48.
        This was read out of DWANGO.DLL, not guessed. Six guesses failed first
        because they searched the wrong record.

        The handler is FUN_100066e8 in DWANGO.DLL. In lobby-list mode it builds
        a 28-byte record: the Shift-JIS name at +0, and a 32-bit count at +0x18
        assembled byte by byte from message offsets 0x48 to 0x4b, least
        significant byte first:

            r4 = msg[0x4b]<<24 | msg[0x4a]<<16 | msg[0x49]<<8 | msg[0x48]
            mov.l r4,@(40,r15)          ; sp+40 == buf+0x18

        The dialog then prints that record with the format string at
        0x1001c6b4, "%s / %d人", referenced once from 0x10009a8a:

            mov.l @(24,r2),r2           ; +0x18, the number on screen

        WHY THE EARLIER SEARCH FAILED. The hunt was aimed at a 0x38-byte record
        whose renderer sums fields at +0x2c and +0x30. That is the USER record,
        not the room record. Its format string at 0x1001c558 is "%4d戦 %4d勝",
        so the sum is a player's battle count. Two different tables, two
        different renderers.

        TWO LIMITS, both read from add_lobby_record at 0x10003c94:
        - The table holds 32 rooms. Entry 32 and beyond is refused.
        - Insert matches on the NAME first. A second element with the same name
          OVERWRITES the record. So never send a room twice in one list with a
          stale count; the last one wins.
        """
        m = bytearray(0xB0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_LISTELEMENT.to_bytes(2, "little")
        raw = room.encode("latin1")[:0x13]
        m[0x1a:0x1a + len(raw)] = raw
        m[0x48:0x4c] = int(count).to_bytes(4, "little")
        return c203.sign(bytes(m))

    def room_counts(self):
        n = {r: 0 for r in ROOMS}
        for c in self.roster():
            n[c.room] = n.get(c.room, 0) + 1
        return n

    def send_room_lists(self, client):
        """Push the room list. The element carries its name at offset 0x1a.

        C203_LobbyListDone terminates a ROOM list; C203_TeamListDone terminates
        a TEAM list. The terminator is what decides where the elements land.
        """
        counts = self.room_counts()
        client.send(make(TYPE_LISTSTART), "C203_ListStart")
        for room in ROOMS:
            n = counts.get(room, 0)
            client.send(self.room_element(room, n),
                        f"C203_ListElement {room} count={n}")
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

        The dword at 0xa4 marks a player as BUSY. MEASURED from the handler:
        three separate flag bytes are each set to `1 - (msg[0xa4] == 0)`, so one
        non-zero value turns all three on together. The lobby draws a busy
        player differently, and a team whose members are busy is not offered to
        a joiner. We set it while the race module has the player.
        """
        m = bytearray(0xB0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_WHO.to_bytes(2, "little")
        # The player's OWN name, exactly as it registered it. Never our label.
        name = sjis_trim(member.realname or member.name.encode("latin1"), 0x13)
        m[0x1a:0x1a + len(name)] = name
        room = member.room.encode("latin1")[:0x13]
        m[0x2e:0x2e + len(room)] = room
        if member.team:
            team = member.team[:0x13]          # the client's own bytes, Shift-JIS
            m[0x56:0x56 + len(team)] = team
            if member.leader:
                m[0x88] = ord("*")
        if member.racing:
            m[0xa4:0xa8] = (1).to_bytes(4, "little")
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
        # REMOVED 2026-08-14, and this is the ONE variable in this run.
        #
        # A second C203_NodeAddress used to go out here, just before the launch:
        # node index 0, pad 1, address "NODE0". The old comment admitted it
        # repeated what send_team_info had already sent and that its effect had
        # never been established. It was copied from the first two-player race.
        #
        # It is wrong now. send_team_info builds a PER-CONSOLE table in which
        # node 0 is that console ITSELF, with that console's own pad. This
        # message overwrote node 0 on every client with a fake name and pad 1.
        # With two players that was survivable. With four, three of the four
        # consoles got a wrong entry for themselves, and one client failed to
        # reach the race while the other three raced.
        #
        # To put it back, restore the six lines that built a 0xE0 message with
        # TYPE_NODEADDRESS, 0 at 0x1a, 1 at 0x1c and b"NODE0" at 0x1e.

        # 0x56 carries the PLAYER name. It used to send client.name, which was
        # our own log label, so the launch announced a player called RALLY2 who
        # did not exist. Send the name the client gave itself, and trim it on a
        # character boundary because it is Shift-JIS.
        m = bytearray(0xE0)
        m[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_LAUNCHMODULE.to_bytes(2, "little")
        for off, text in ((0x1a, b"SEGA RALLY 2"), (0x2e, b"MGNETWK"),
                          (0x42, LOBBY_NAME.encode("latin1")),
                          (0x56, client.realname or client.name.encode("latin1")),
                          (0x6a, b"TEAM A")):
            raw = sjis_trim(text, 0x13)
            m[off:off + len(raw)] = raw
        client.send(c203.sign(bytes(m)), "C203_LaunchModule")
        client.racing = True        # the race module owns this player now

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
            wanted = msg[0x1a:0x2e].split(b"\0")[0]
            client.realname = self.unique_name(wanted, client)
            reported = client.realname.decode("latin1", "replace")
            log(f"{client.name} logged in (the client calls itself {reported!r})")
            if client.realname != wanted:
                # Tell the client which name it actually got, with
                # C203_UserNameResult. Its handler copies the string at 0x1a
                # into a global and raises DWLEVENT_CONNECTION_USERNAMECHANGED,
                # so the client adopts the new name from then on.
                #
                # This MUST go out before the user list. The C203_WHO handler
                # only recognises a player's OWN entry by comparing the name in
                # it against that global, so the client has to know its new name
                # before the list arrives.
                r = bytearray(0xB0)
                r[c203.OFF_TYPE:c203.OFF_TYPE + 2] = TYPE_USERNAMERESULT.to_bytes(2, "little")
                r[0x1a:0x1a + len(client.realname[:0x13])] = client.realname[:0x13]
                client.send(c203.sign(bytes(r)), f"C203_UserNameResult {reported!r}")
                log(f"  {wanted!r} was taken, renamed to {client.realname!r}")
            client.send(make(TYPE_LOBBYINFO, {0x1a: LOBBY_NAME}), "C203_LOBBYINFO")
            client.in_lobby = True
            self.send_full_list(client)     # the newcomer has no list yet
            self.push_user(client)          # everyone else gains one row
            self.announce(client.room, client.realname + b" has joined")
            self.send_room_lists(client)
            self.send_motd(client)          # the welcome block, this player only

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

            # The user record goes LAST. The C203_WHO handler resolves the team
            # NAME at 0x56 to a team id, and that lookup only succeeds once the
            # team is known to the client. Only this player's record changed.
            self.push_user(client)

        elif t == 515:
            # The LOBBY list request. Its sender clears the list-mode flag, so
            # elements sent in reply become rooms, not teams.
            log(f"  <- {client.name}: 515, asking for the lobby list")
            # Use the ONE builder. This handler used to build its own elements
            # with make(TYPE_LISTELEMENT, {0x1a: room}), which carried the name
            # and nothing else. The chat-room dialog sends 515 every time it
            # opens, so that copy was the ONLY list the player ever saw, and the
            # occupancy count in the other builder never reached the wire at
            # all. Two builders for one message is how that hid.
            self.send_room_lists(client)

        elif t == TYPE_LEAVE:
            # The client says goodbye before it drops the line. MEASURED
            # 2026-08-14: a player who closed the window sent this 26-byte
            # message with no payload, and the socket closed a moment later.
            # Handling it here means the lobby reacts at once instead of
            # waiting for the socket to die.
            log(f"  <- {client.name}: 534, leaving")
            self.depart(client, "left")

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

        elif t == TYPE_SEARCHREQ:
            # FIND A USER. This case MUST come before the game-data branch
            # below, which swallows everything above 1000 and relays it to the
            # other players. That relay is why find did nothing for so long.
            wanted = msg[0x1a:0x2c].split(b"\0")[0]
            log(f"  <- {client.name}: 1007, find {wanted!r}")
            self.send_search_results(client, wanted)

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
            # Dump the body of anything that arrives OUTSIDE a race. A type
            # 1007 arrived six seconds after the client opened the find dialog,
            # and this channel relays it to the other players instead of
            # answering it. Print the bytes so the search request can be read.
            if not client.team:
                log(f"       {bytes(msg[0x18:]).hex(' ')}")
                text = bytes(msg[0x1a:]).split(b"\0")[0]
                log(f"       0x1a as text: {text!r}")
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
            # 542 is also the first thing a client sends when it drops out of
            # the race module and comes back to the lobby. Reset it there.
            self.leave_race(client)

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
            self.push_user(client)
            # No TeamInfo here either. Membership travels in the WHO records.

        elif t == TYPE_ENTERROOM:
            # Move to another chat room, 76 bytes with the room name at 0x1a.
            # Answer it the way the lobby join is answered: name the room,
            # accept with C203_Sync, then resend the user list, because the
            # occupancy counts come from it.
            want = msg[0x1a:0x40].split(b"\0")[0].decode("latin1", "replace")
            room = want or client.room
            log(f"  <- {client.name}: 519, move to room {room!r}")
            was = client.room
            client.room = room
            client.send(make(TYPE_LOBBYINFO, {0x1a: room}), f"C203_LOBBYINFO {room}")
            client.send(make(TYPE_SYNC, {0x1a: room}), "C203_Sync (room entered)")
            # MEASURED 2026-08-14: the client does NOT filter its user list by
            # the room field at 0x2e. Two players in different rooms were each
            # sent both records, and BOTH clients listed both players and
            # counted 2 in their own room. So a client must only ever be told
            # about players in ITS room, or its own occupancy count is wrong.
            #
            # A room change is therefore a delete from the old room and an add
            # to the new one, not one updated record.
            self.drop_user(client, was)
            self.send_full_list(client)
            self.push_user(client)

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
                    if client.team in self.launching and client.team not in self.launched:
                        log(f"  every member of {client.team!r} is ready. Launching.")
                        # Take the team OUT of the launching set FIRST, and
                        # clear every ready flag, so this runs exactly once.
                        #
                        # MEASURED 2026-08-14 with four players: without this,
                        # the launch block went out FIVE times. client.ready was
                        # never reset after a race, so the "all ready" test
                        # passed on the very first 573 of the next race and
                        # passed again on each one after it. Every repeat sent
                        # another C203_LaunchModule to all four clients, which
                        # restarts the race module under them. One emulator was
                        # thrown back to NAME ENTRY, one sat at PLEASE WAIT and
                        # one reached SELECT COURSE. It looked like a timing or
                        # threading fault. It was neither: all five rounds
                        # happened inside one second, in one select loop.
                        self.launched.add(client.team)
                        for c in members:
                            c.ready = False
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
            self.send_full_list(client)

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
                    self.next_name += 1
                    name = CONN_LABEL.format(self.next_name)
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
                    # depart() already told the room, with one C203_DELUSER
                    self.depart(client, "disconnected")
                    del self.clients[s]
                    s.close()
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
                        # Byte 0 is the SENDER's node id, the same value we
                        # handed out as its pad in C203_NodeAddress. MEASURED
                        # 2026-08-14 across a four-player attempt: the ids seen
                        # were 1, 2, 3 and 4 and nothing else.
                        #
                        # It is NOT the documented DPDW30DC node byte
                        # 0xc0 | dst<<3 | src. That form does not appear in this
                        # traffic at all, so a record does not say who it is
                        # FOR. This relay therefore broadcasts, and the receiver
                        # decides what to keep.
                        node = rec[0] if rec else 0
                        log(f"  RACE RECORD 70 {end:02x} from {client.name} "
                            f"node {node}, {len(rec)} bytes "
                            f"-> {[c.name for c in peers]}")
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
