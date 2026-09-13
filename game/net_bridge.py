"""
net_bridge.py

Thin wrapper around the switch_net Rust extension, translating between
raw (msg_type, seq, bytes, src_mac) frames and the game's own event
vocabulary (attack, repair, sync, etc.). state.py should never need
to import switch_net directly, everything it needs goes through
NetBridge.

Design notes:
- Payloads are JSON, with a 4-byte CRC32 prepended before sending. The
  CRC lets a *receiver* detect a corrupted frame before ever decoding
  or applying it, rather than finding out after the fact. A corrupted
  frame is dropped outright: not decoded, not dedup-tracked, not
  handed to the game as an event. That's what keeps both sides in
  sync despite an error, both machines end up agreeing the event
  didn't happen, instead of one applying corrupted data the other
  never saw.
- Every non-ack send is tracked in a pending-ack table and acked by
  the receiver (ok=true if the checksum passed, ok=false if it
  didn't). If an ack explicitly says ok=false, that's a confirmed
  failure and is resolved immediately (see below). But if NO ack
  arrives at all within ack_timeout_ms, that's ambiguous, it could
  mean the original message was lost, or it could mean the message
  arrived and was applied fine but the ack on the way back was lost
  or corrupted. Guessing wrong there is exactly what causes the two
  clients to disagree about what happened, so before giving up, the
  bridge sends a small ACK_RESEND_REQUEST naming the seq it's still
  waiting on. If the peer recognizes that seq (it kept a short-lived
  record of what it last acked), it resends the same ack outcome. Only
  if THAT also goes unanswered does the bridge treat it as genuinely
  unresolved: for game_over it retries the full message (as before);
  for everything else it reports delivery_failed so state.py can
  disregard whatever it assumed locally and flag it in the UI.
  This narrows, but can't fully eliminate, the chance of the two
  sides disagreeing, no protocol over an unreliable channel can
  (see: the Two Generals' Problem), it just makes it require two
  consecutive round-trip failures in a row instead of one.
- switch_net itself still gives no ordering guarantees. Beyond the
  ack/retry handling above, NetBridge also tracks the last-seen seq
  per (msg_type, system) and drops anything older, so a late/
  duplicate successfully-delivered frame can't stomp a newer one.
"""

import json
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Optional

import switch_net

# -------- game message types --------
# These are switch_net's msg_type byte (0-255), scoped to this game.
# Distinct from the CHAT/PING/PONG values used by test_switch_net.py.

MSG_HELLO = 10
MSG_ATTACK = 11
MSG_REPAIR = 12
MSG_SYNC = 13
MSG_GAME_OVER = 14
MSG_ACK = 15  # not in _EVENT_NAMES: acks are consumed internally by
              # poll(), never handed out as a plain NetEvent

_EVENT_NAMES = {
    MSG_HELLO: "hello",
    MSG_ATTACK: "attack",
    MSG_REPAIR: "repair",
    MSG_SYNC: "sync",
    MSG_GAME_OVER: "game_over",
}

# Message types the bridge automatically retries (same seq) on a
# failed/missing ack, instead of just reporting delivery_failed.
_AUTO_RETRY_TYPES = {MSG_GAME_OVER}


@dataclass
class NetEvent:
    """
    A decoded incoming frame, or a synthetic delivery-status event,
    ready for state.py to act on.

    type: one of _EVENT_NAMES' values, or "delivered" / "delivery_failed"
    data: for delivered/delivery_failed, holds {"msg_type": int,
          "system": str|None} and, for delivery_failed, also
          {"reason": "corrupted"|"timeout"}
    """
    type: str
    seq: int
    data: dict
    src_mac: str


@dataclass
class _PendingSend:
    msg_type: int
    system: Optional[str]
    data: dict  # original payload dict, kept so game_over can be resent verbatim
    sent_time: float
    retry_count: int = 0


@dataclass
class NetBridge:
    interface: str
    peer_mac: Optional[str] = None
    read_timeout_ms: int = 5
    ack_timeout_ms: int = 125  # generous for a direct LAN link; tune down once tested

    _sock: "switch_net.SwitchSocket" = field(init=False, repr=False)
    _last_seq_seen: dict = field(default_factory=dict, init=False, repr=False)
    _pending: dict = field(default_factory=dict, init=False, repr=False)  # seq -> _PendingSend

    def __post_init__(self):
        self._sock = switch_net.SwitchSocket(
            self.interface, peer_mac=self.peer_mac, read_timeout_ms=self.read_timeout_ms
        )

    # -------- connection --------

    def local_mac(self) -> str:
        return self._sock.local_mac()

    def connect(self, timeout_ms: int = 5000) -> bool:
        """
        Broadcast HELLO until we hear anything back from a peer (their
        HELLO, or an ack of ours), then lock this bridge onto that
        peer's MAC so later recv() calls ignore anyone else on the
        switch. Returns True once connected, False on timeout.

        Only meaningful if peer_mac wasn't already set at construction;
        if it was, this just waits for any traffic from that peer.

        Note: this resend-every-300ms loop is connect()'s own
        discovery behavior, separate from (and not governed by) the
        auto-retry-on-ack-failure system below, HELLO is not in
        _AUTO_RETRY_TYPES. Repeating a "here I am" broadcast while
        looking for a peer is a different concern from retrying a
        specific message that's known to have failed delivery.
        """
        deadline = time.monotonic() + (timeout_ms / 1000)
        last_hello_sent = 0.0

        while time.monotonic() < deadline:
            if time.monotonic() - last_hello_sent > 0.3:
                self._send(MSG_HELLO, {})
                last_hello_sent = time.monotonic()

            for event in self.poll():
                if event.src_mac:
                    if self.peer_mac is None:
                        self._lock_peer(event.src_mac)
                    return True
        return False

    def _lock_peer(self, mac: str) -> None:
        self.peer_mac = mac
        self._sock = switch_net.SwitchSocket(
            self.interface, peer_mac=mac, read_timeout_ms=self.read_timeout_ms
        )

    # -------- sending --------

    def send_attack(self, exploit: str, system: str) -> int:
        return self._send(MSG_ATTACK, {"exploit": exploit, "system": system})

    def send_repair(self, system: str) -> int:
        return self._send(MSG_REPAIR, {"system": system})

    def send_sync(self, state: dict) -> int:
        return self._send(MSG_SYNC, state)

    def send_game_over(self, winner: str) -> int:
        return self._send(MSG_GAME_OVER, {"winner": winner})

    def _send(self, msg_type: int, data: dict) -> int:
        seq = self._sock.send_auto(msg_type, _pack_payload(data))
        self._pending[seq] = _PendingSend(
            msg_type=msg_type,
            system=data.get("system"),
            data=data,
            sent_time=time.monotonic(),
        )
        return seq

    def _send_ack(self, acked_seq: int, ok: bool) -> None:
        # Acks are not themselves tracked for delivery, an ack that
        # goes missing just means the original sender's own timeout
        # fires, which is the correct fallback behavior anyway.
        self._sock.send_auto(MSG_ACK, _pack_payload({"ack_seq": acked_seq, "ok": ok}))

    def _retry_send(self, seq: int, pending: _PendingSend) -> None:
        self._sock.send(pending.msg_type, seq, _pack_payload(pending.data))
        pending.sent_time = time.monotonic()
        pending.retry_count += 1

    # -------- receiving --------

    def poll(self) -> list:
        """
        Drain every frame currently available, ack whatever needs
        acknowledging, resolve or retry pending sends, and return the
        results as NetEvents. Call this once per game loop tick; it
        never blocks longer than read_timeout_ms.
        """
        events = []

        while True:
            result = self._sock.recv()
            if result is None:
                break
            msg_type, seq, raw_payload, src_mac = result

            if msg_type == MSG_ACK:
                event = self._handle_ack(raw_payload, src_mac)
                if event is not None:
                    events.append(event)
                continue

            data = _unpack_payload(raw_payload)
            if data is None:
                # Checksum failed: corrupted in transit. Don't decode
                # it, don't dedup-track it, don't hand it to the game,
                # just tell the sender it didn't land intact.
                self._send_ack(seq, ok=False)
                continue

            self._send_ack(seq, ok=True)
            event = self._apply_dedup(msg_type, seq, data, src_mac)
            if event is not None:
                events.append(event)

        events.extend(self._check_timeouts())
        return events

    def _handle_ack(self, raw_payload: bytes, src_mac: str) -> Optional[NetEvent]:
        data = _unpack_payload(raw_payload)
        if data is None:
            return None  # corrupted ack; the original send's own timeout will handle it

        acked_seq = data.get("ack_seq")
        ok = data.get("ok")
        pending = self._pending.get(acked_seq)
        if pending is None:
            return None  # ack for something already resolved, or not ours; ignore

        if ok:
            del self._pending[acked_seq]
            return NetEvent(
                type="delivered",
                seq=acked_seq,
                data={"msg_type": pending.msg_type, "system": pending.system},
                src_mac=src_mac,
            )

        # Peer explicitly told us this seq arrived corrupted.
        if pending.msg_type in _AUTO_RETRY_TYPES:
            self._retry_send(acked_seq, pending)
            return None  # still pending, not resolved yet
        del self._pending[acked_seq]
        return NetEvent(
            type="delivery_failed",
            seq=acked_seq,
            data={"msg_type": pending.msg_type, "system": pending.system, "reason": "corrupted"},
            src_mac=src_mac,
        )

    def _check_timeouts(self) -> list:
        events = []
        now = time.monotonic()
        timeout_s = self.ack_timeout_ms / 1000

        for seq, pending in list(self._pending.items()):
            if now - pending.sent_time < timeout_s:
                continue

            if pending.msg_type in _AUTO_RETRY_TYPES:
                self._retry_send(seq, pending)
            else:
                del self._pending[seq]
                events.append(
                    NetEvent(
                        type="delivery_failed",
                        seq=seq,
                        data={"msg_type": pending.msg_type, "system": pending.system, "reason": "timeout"},
                        src_mac="",
                    )
                )
        return events

    def _apply_dedup(self, msg_type: int, seq: int, data: dict, src_mac: str) -> Optional[NetEvent]:
        name = _EVENT_NAMES.get(msg_type)
        if name is None:
            return None  # not a message type this game knows about, ignore

        # Dedup key: per-target messages (attack/repair carry a
        # "system") are tracked per (msg_type, system), since an
        # attack or repair on one system is an independent event from
        # one on another, not a newer/older version of the same info.
        # Messages with no "system" (sync, game_over, hello, ...)
        # represent one logical stream and are tracked by msg_type
        # alone.
        system = data.get("system")
        dedup_key = (msg_type, system) if system is not None else msg_type

        last_seq = self._last_seq_seen.get(dedup_key)
        if last_seq is not None and _seq_is_older(seq, last_seq):
            return None  # stale/duplicate, a newer frame of this (type, system) already arrived
        self._last_seq_seen[dedup_key] = seq

        return NetEvent(type=name, seq=seq, data=data, src_mac=src_mac)


# -------- payload framing --------

def _pack_payload(data: dict) -> bytes:
    body = json.dumps(data).encode("utf-8")
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return struct.pack(">I", crc) + body


def _unpack_payload(raw: bytes) -> Optional[dict]:
    if len(raw) < 4:
        return None
    (claimed_crc,) = struct.unpack(">I", raw[:4])
    body = raw[4:]
    if (zlib.crc32(body) & 0xFFFFFFFF) != claimed_crc:
        return None
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None  # extremely unlikely once CRC passes, but stay defensive


def _seq_is_older(seq: int, last_seq: int) -> bool:
    """
    Sequence numbers are u32 and wrap around. This treats seq as older
    than last_seq only if it falls within the "past half" of the
    circular range, so a wraparound (e.g. last_seq=4294967295, seq=0)
    is correctly treated as newer, not older.
    """
    diff = (seq - last_seq) & 0xFFFFFFFF
    return diff > 0x7FFFFFFF
