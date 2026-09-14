"""
state.py

Game logic for Switch 'n Hack. Owns all game state and timers,
translates player commands into local state changes and/or NetBridge
calls, and turns incoming NetEvents into state changes and log lines.
ui.py should only need to read a Game instance's public attributes and
call handle_command() / tick().

=== Networking model ===
Each player's client is the sole authority for its OWN systems, since
only it holds the secrets (an active defend, an active honeypot) needed
to correctly resolve an incoming attack. So:
- "target" (attack) is a request: attacker -> defender. The DEFENDER
  computes success/failure using its own firewall/defend/honeypot
  state, applies the result locally, and reports the outcome back via
  attack_result. A honeypot hit is explicitly reported as a trap so the
  attacker can be locked out briefly, without revealing the real system's
  status.
- "inspect" is the same request/response shape: inspector -> target,
  and the target reports back regardless of its firewall state, with any
  honeypot-masked slot reported as a healthy decoy.
- "repair", "defend", and "honeypot" are 100% local. They only ever
  change your own systems, which you already authoritatively know, so
  nothing is sent for them. The opponent only ever learns your true
  status via a successful, unblocked inspect, a deliberate
  fog-of-war choice, not an oversight.
- "game_over" for a kernel compromise or a forfeit is always sent by
  the LOSING side, self-reporting its own loss ({"reason": "kernel failure"}
  or {"reason": "forfeit"}), so there's no relative "you"/"opponent"
  ambiguity to get backwards, whoever sends it just lost.
- The 10-minute timeout is different: neither side knows the winner
  unilaterally. At the deadline, each side reliably sends its own true
  compromised-count ({"reason": "timeout", "compromised_count": N})
  over the SAME guaranteed game_over channel, and both sides
  independently compute the same winner once they have both numbers.
  A tie is called a draw; the README doesn't specify a tiebreak beyond
  "fewest compromised", so this doesn't invent one.

=== Design decisions the README didn't specify (tunable) ===
The README describes these mechanics qualitatively but doesn't give
exact numbers. Everything below is a reasonable placeholder, not a
spec, adjust freely:
- Attack success chance is driven entirely by the DEFENDER's firewall
  status (compromised firewall = always vulnerable, per the README),
  plus a "significant" reduction while the target is actively
  defended. Exact percentages are guesses.
- Routing table's "attacks may be misdirected" is modeled as a chance
  (scaling with the ATTACKER's own routing_table degradation) that an
  outgoing attack silently retargets to a different system.
- ARP cache's "occasional real packet corruption" is wired directly
  into NetBridge.self_corruption_chance, an actual corrupted-frame
  chance on the wire, not just flavor text.
- Inspect bypasses firewalls and reports the requested status unless the
  request itself is invalid or the response is lost on the wire.
- A honeypot always presents as "operational" to the opponent, a
  healthy-looking decoy, since the README doesn't specify what a
  masked slot displays as.
- Honeypot setup is assumed to lock out other commands for its 10
  seconds, the same way repair explicitly does, since both are
  described as "spends 10 seconds" actions.
- Attack/inspect are one-way-lossy by design (per the README), so if
  no result/response arrives within 125 ms, the UI shows "failed" rather
  than waiting forever.
"""

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

try:  # Support both ``python -m game.main`` and direct module execution.
    from . import net_bridge
except ImportError:
    import net_bridge

# -------- tunables --------

ATTACK_COOLDOWN_S = 1.0
REPAIR_DURATION_S = 10.0
HONEYPOT_SETUP_DURATION_S = 10.0
HONEYPOT_COOLDOWN_S = 60.0
DEFEND_WINDOW_S = 5.0
TERMINAL_LOCKOUT_S = 30.0
MATCH_DURATION_S = 600.0  # 10 minutes
WARNING_BANNER_DURATION_S = 3.0
ATTACK_RESULT_WAIT_S = 0.125
INSPECT_RESULT_WAIT_S = 0.125
LOG_MAX_LINES = 200

# Attack success chance, keyed by the DEFENDER's own firewall status.
FIREWALL_BASE_SUCCESS = {
    "operational": 0.70,
    "degraded": 0.85,
    "compromised": 1.0,  # "vulnerable to all attacks"
}
DEFEND_SUCCESS_MULTIPLIER = 0.3  # "significantly lowered" while actively defended

# Attacker's own routing_table: chance an attack silently retargets.
MISDIRECT_CHANCE = {
    "operational": 0.0,
    "degraded": 0.20,
    "compromised": 0.50,
}

# Attacker's own arp_cache: chance an outgoing send is corrupted on the wire.
SELF_CORRUPTION_CHANCE = {
    "operational": 0.0,
    "degraded": 0.05,
    "compromised": 0.20,
}

BASE_SYSTEMS = ["firewall", "antivirus", "routing_table", "arp_cache"]
ALL_ATTACKABLE = BASE_SYSTEMS + ["terminal", "kernel"]

COMMAND_ALIASES = {
    "tar": "target", "target": "target", "atk": "target", "attack": "target",
    "def": "defend", "defend": "defend",
    "rep": "repair", "repair": "repair",
    "ins": "inspect", "inspect": "inspect",
    "pot": "honeypot", "honeypot": "honeypot",
    "quit": "forfeit", "exit": "forfeit", "forfeit": "forfeit",
}


class Status(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    COMPROMISED = "compromised"

    def worse(self) -> "Status":
        if self == Status.OPERATIONAL:
            return Status.DEGRADED
        return Status.COMPROMISED

    def better(self) -> "Status":
        if self == Status.COMPROMISED:
            return Status.DEGRADED
        return Status.OPERATIONAL


@dataclass
class PlayerSystems:
    """
    One player's own systems. Each player's client is the sole
    authority for its own PlayerSystems, only it has the secrets
    (defend/honeypot) needed to correctly resolve incoming attacks.
    """
    firewall: Status = Status.OPERATIONAL
    antivirus: Status = Status.OPERATIONAL
    routing_table: Status = Status.OPERATIONAL
    arp_cache: Status = Status.OPERATIONAL
    terminal: Status = Status.OPERATIONAL
    kernel_compromised: bool = False

    def get(self, system: str) -> Status:
        return getattr(self, system)

    def set(self, system: str, value: Status) -> None:
        setattr(self, system, value)

    def compromised_count(self) -> int:
        return sum(
            1 for s in (self.firewall, self.antivirus, self.routing_table, self.arp_cache, self.terminal)
            if s == Status.COMPROMISED
        )


def _valid_target(systems: PlayerSystems, system: str) -> bool:
    if system in BASE_SYSTEMS:
        return True
    if system == "terminal":
        return all(systems.get(s) == Status.COMPROMISED for s in BASE_SYSTEMS)
    if system == "kernel":
        return systems.terminal == Status.COMPROMISED
    return False


@dataclass
class PendingAttack:
    system: str
    sent_time: float


@dataclass
class PendingInspect:
    system: Optional[str]
    sent_time: float


class Game:
    def __init__(self, bridge: "net_bridge.NetBridge", rng: Optional[random.Random] = None):
        self.bridge = bridge
        self._rng = rng or random.Random()

        self.my = PlayerSystems()
        self.known_opponent: dict = {}  # system -> status string, from last successful inspect (may be stale)

        self.phase = "connecting"  # connecting -> playing -> game_over
        self.winner: Optional[str] = None  # "you" / "opponent" / "draw"
        self.game_over_reason: Optional[str] = None
        self.start_time: Optional[float] = None

        self.log: list = []

        self.honeypot_system: Optional[str] = None
        self.defended_system: Optional[str] = None
        self.defended_until: float = 0.0
        self.attack_cooldown_until: float = 0.0
        self.busy_until: float = 0.0        # repair or honeypot setup in progress
        self.busy_action: Optional[str] = None  # "repair" | "honeypot"
        self.busy_target: Optional[str] = None
        self.honeypot_cooldown_until: float = 0.0
        self.locked_until: float = 0.0      # terminal-compromise lockout
        self.warning_until: float = 0.0
        self.awaiting_forfeit_confirm = False

        self.pending_attacks: dict = {}   # seq -> PendingAttack
        self.pending_inspects: dict = {}  # seq -> PendingInspect

        self._final_tally_sent = False
        self._my_final_tally: Optional[int] = None
        self._opponent_final_tally: Optional[int] = None
        self.opponent_forfeited = False

    # -------- connection --------

    def connect(self, timeout_ms: int = 10000) -> bool:
        ok = self.bridge.connect(timeout_ms)
        if ok:
            self.phase = "playing"
            self.start_time = time.monotonic()
        return ok

    def time_remaining(self) -> float:
        if self.start_time is None:
            return MATCH_DURATION_S
        return max(0.0, MATCH_DURATION_S - (time.monotonic() - self.start_time))

    # -------- main loop --------

    def tick(self) -> None:
        now = time.monotonic()
        events = self.bridge.poll()

        # Network events are authoritative for the opponent's state.  Drain
        # them before completing local timed actions so a kernel attack and a
        # repair becoming due in the same tick have one deterministic order:
        # the attack is resolved against the state that existed when this
        # batch was received.  In particular, never finish a repair after a
        # kernel attack has already ended the match.
        if self.phase != "game_over":
            for event in events:
                self._handle_event(event, now)
                if self.phase == "game_over":
                    break

            if self.phase == "game_over":
                return

            self._check_pending_timeouts(now)
            self._resolve_busy_action(now)
            self._check_match_timer(now)

    def _resolve_busy_action(self, now: float) -> None:
        if not self.busy_until or now < self.busy_until:
            return
        if self.busy_action == "repair":
            new_status = self.my.get(self.busy_target).better()
            self.my.set(self.busy_target, new_status)
            self._log(f"repair complete: {self.busy_target} -> {new_status.value}")
        elif self.busy_action == "honeypot":
            self.honeypot_system = self.busy_target
            self.honeypot_cooldown_until = now + HONEYPOT_COOLDOWN_S
            self._log(f"honeypot ready, masking {self.busy_target}")
        self.busy_until = 0.0
        self.busy_action = None
        self.busy_target = None

    def _check_pending_timeouts(self, now: float) -> None:
        for seq, pa in list(self.pending_attacks.items()):
            if now - pa.sent_time > ATTACK_RESULT_WAIT_S:
                self._log("! failed")
                del self.pending_attacks[seq]
        for seq, pi in list(self.pending_inspects.items()):
            if now - pi.sent_time > INSPECT_RESULT_WAIT_S:
                self._log("! failed")
                del self.pending_inspects[seq]

    def _check_match_timer(self, now: float) -> None:
        if self._final_tally_sent or self.start_time is None:
            return
        if now - self.start_time < MATCH_DURATION_S:
            return
        self._my_final_tally = self.my.compromised_count()
        self.bridge.send_game_over(reason="timeout", compromised_count=self._my_final_tally)
        self._final_tally_sent = True
        self._log("time's up, exchanging final tallies...")
        self._maybe_conclude_timeout()

    def _maybe_conclude_timeout(self) -> None:
        if self.phase == "game_over":
            return
        if self._my_final_tally is None or self._opponent_final_tally is None:
            return
        my_n, opp_n = self._my_final_tally, self._opponent_final_tally
        if my_n < opp_n:
            self.winner = "you"
        elif opp_n < my_n:
            self.winner = "opponent"
        else:
            self.winner = "draw"
        self.phase = "game_over"
        self.game_over_reason = "timeout"
        self._log(f"GAME OVER - {self._win_line()} (timeout: {my_n} vs {opp_n} compromised)")

    def _win_line(self) -> str:
        if self.winner == "you":
            return "you win!"
        if self.winner == "opponent":
            return "opponent wins"
        return "draw"

    # -------- commands --------

    def handle_command(self, raw_line: str) -> None:
        if self.phase != "playing":
            return
        now = time.monotonic()

        if self.locked_until and now < self.locked_until:
            self._log("! terminal locked, can't act")
            return

        parts = raw_line.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        action = COMMAND_ALIASES.get(cmd)
        if action is None:
            self._log(f"! unknown command '{cmd}'")
            return

        if action != "forfeit" and self.awaiting_forfeit_confirm:
            self.awaiting_forfeit_confirm = False

        if action != "forfeit" and self.busy_until and now < self.busy_until:
            self._log("! busy")
            return

        if action == "target":
            if not args:
                self._log("! usage: tar <system>")
                return
            self._cmd_target(args[0].lower(), now)
        elif action == "defend":
            if not args:
                self._log("! usage: def <system>")
                return
            self._cmd_defend(args[0].lower(), now)
        elif action == "repair":
            if not args:
                self._log("! usage: rep <system>")
                return
            self._cmd_repair(args[0].lower(), now)
        elif action == "inspect":
            self._cmd_inspect(args[0].lower() if args else None, now)
        elif action == "honeypot":
            if not args:
                self._log("! usage: pot <system>")
                return
            self._cmd_honeypot(args[0].lower(), now)
        elif action == "forfeit":
            self._cmd_forfeit()

    def _cmd_target(self, system: str, now: float) -> None:
        if system not in ALL_ATTACKABLE:
            self._log(f"! unknown system '{system}'")
            return
        if now < self.attack_cooldown_until:
            self._log("! attack on cooldown")
            return
        if system == "kernel" and self.opponent_forfeited:
            self.bridge.send_attack(system)
            self.phase = "game_over"
            self.winner = "you"
            self.game_over_reason = "forfeit"
            self._log("GAME OVER - you win! (final blow after opponent forfeited)")
            return

        actual_system = system
        mis_chance = MISDIRECT_CHANCE[self.my.routing_table.value]
        if mis_chance and self._rng.random() < mis_chance:
            others = [
                s for s in ALL_ATTACKABLE
                if s != system and self._opponent_target_valid(s)
            ]
            if not others:
                others = [system]
            actual_system = self._rng.choice(others)
            self._log(f"! routing table misdirected attack: {system} -> {actual_system}")

        self.bridge.self_corruption_chance = SELF_CORRUPTION_CHANCE[self.my.arp_cache.value]
        seq = self.bridge.send_attack(actual_system)
        self.pending_attacks[seq] = PendingAttack(system=actual_system, sent_time=now)
        self.attack_cooldown_until = now + ATTACK_COOLDOWN_S

    def _opponent_target_valid(self, system: str) -> bool:
        """Return whether a target is currently known to be unlocked."""
        if system in BASE_SYSTEMS:
            return True
        if self.opponent_forfeited:
            return system == "kernel"
        if system == "terminal":
            return all(
                self.known_opponent.get(s) == Status.COMPROMISED.value
                for s in BASE_SYSTEMS
            )
        if system == "kernel":
            return self.known_opponent.get("terminal") == Status.COMPROMISED.value
        return False

    def _cmd_defend(self, system: str, now: float) -> None:
        if system not in ALL_ATTACKABLE:
            self._log(f"! unknown system '{system}'")
            return
        if self.my.antivirus == Status.COMPROMISED:
            self._log("! antivirus is compromised, can't defend")
            return
        self.defended_system = system
        self.defended_until = now + DEFEND_WINDOW_S

    def _cmd_repair(self, system: str, now: float) -> None:
        if system not in ALL_ATTACKABLE or system == "kernel":
            self._log(f"! can't repair '{system}'")
            return
        if self.my.get(system) == Status.OPERATIONAL:
            self._log(f"! {system} is already fully operational")
            return
        self.busy_until = now + REPAIR_DURATION_S
        self.busy_action = "repair"
        self.busy_target = system

    def _cmd_inspect(self, system: Optional[str], now: float) -> None:
        if system is not None and system not in ALL_ATTACKABLE:
            self._log(f"! unknown system '{system}'")
            return
        seq = self.bridge.send_inspect_request(system)
        self.pending_inspects[seq] = PendingInspect(system=system, sent_time=now)

    def _cmd_honeypot(self, system: str, now: float) -> None:
        if system not in BASE_SYSTEMS:
            self._log(f"! unknown system '{system}'")
            return
        if self.honeypot_system is not None:
            self._log("! a honeypot is already active")
            return
        if now < self.honeypot_cooldown_until:
            self._log("! honeypot on cooldown")
            return
        self.busy_until = now + HONEYPOT_SETUP_DURATION_S
        self.busy_action = "honeypot"
        self.busy_target = system

    def _cmd_forfeit(self) -> None:
        if not self.awaiting_forfeit_confirm:
            self.awaiting_forfeit_confirm = True
            self._log("Are you sure you want to forfeit? Type 'quit' again to confirm.")
            return
        self._declare_loss("forfeit")

    # -------- incoming events --------

    def _handle_event(self, event: "net_bridge.NetEvent", now: float) -> None:
        if event.type == "attack":
            self._handle_incoming_attack(event, now)
        elif event.type == "attack_result":
            self._handle_attack_result(event)
        elif event.type == "inspect_request":
            self._handle_inspect_request(event)
        elif event.type == "inspect_response":
            self._handle_inspect_response(event)
        elif event.type == "game_over":
            self._handle_incoming_game_over(event)
        elif event.type == "delivery_failed":
            self._handle_delivery_failed(event)
        # "delivered" and "hello": nothing state.py needs to act on

    def _handle_incoming_attack(self, event: "net_bridge.NetEvent", now: float) -> None:
        system = event.data.get("system")
        orig_seq = event.seq
        if system not in ALL_ATTACKABLE:
            self.bridge.send_attack_result(system or "", False, orig_seq)
            return

        if self.my.antivirus != Status.COMPROMISED:
            self.warning_until = now + WARNING_BANNER_DURATION_S

        if self.honeypot_system == system:
            self.honeypot_system = None
            self._log(f"[honeypot] opponent's attack on {system} hit the honeypot")
            self.bridge.send_attack_result(system, True, orig_seq, honeypot_hit=True)
            return

        if not _valid_target(self.my, system):
            self.bridge.send_attack_result(system, False, orig_seq)
            return

        success = self._roll_attack_success(system, now)
        if success:
            if system == "kernel":
                self.my.kernel_compromised = True
            else:
                new_status = self.my.get(system).worse()
                self.my.set(system, new_status)
                if system == "terminal" and new_status == Status.COMPROMISED:
                    self.locked_until = now + TERMINAL_LOCKOUT_S
                    self._log("! TERMINAL COMPROMISED - locked out for 30 seconds")

        self.bridge.send_attack_result(system, success, orig_seq)

        if success and system == "kernel":
            self._declare_loss("kernel failure")

    def _roll_attack_success(self, system: str, now: float) -> bool:
        base = FIREWALL_BASE_SUCCESS[self.my.firewall.value]
        if (
            self.defended_system == system
            and now < self.defended_until
            and self.my.antivirus != Status.COMPROMISED
        ):
            base *= DEFEND_SUCCESS_MULTIPLIER
        return self._rng.random() < base

    def _handle_attack_result(self, event: "net_bridge.NetEvent") -> None:
        orig_seq = event.data.get("orig_seq")
        pa = self.pending_attacks.pop(orig_seq, None)
        if pa is None:
            return
        if event.data.get("honeypot_hit"):
            self.locked_until = time.monotonic() + 5.0
            self._log("! failed - you fell into a honeypot trap (locked for 5 seconds)")
        elif event.data.get("success"):
            self._log(f"attack on {pa.system} succeeded")
        else:
            self._log("! failed")

    def _handle_inspect_request(self, event: "net_bridge.NetEvent") -> None:
        requested_system = event.data.get("system")
        orig_seq = event.seq

        if requested_system is not None and requested_system not in ALL_ATTACKABLE:
            self.bridge.send_inspect_response({}, orig_seq, blocked=True)
            return

        targets = [requested_system] if requested_system else ALL_ATTACKABLE
        report = {}
        for s in targets:
            if s == "kernel":
                report[s] = "compromised" if self.my.kernel_compromised else "operational"
            elif self.honeypot_system == s:
                report[s] = "operational"  # honeypot presents as a healthy decoy
            else:
                report[s] = self.my.get(s).value

        self.bridge.send_inspect_response(report, orig_seq, blocked=False)

    def _handle_inspect_response(self, event: "net_bridge.NetEvent") -> None:
        orig_seq = event.data.get("orig_seq")
        pi = self.pending_inspects.pop(orig_seq, None)
        if pi is None:
            return
        if event.data.get("blocked"):
            self._log("! failed")
            return
        systems = event.data.get("systems", {})
        self.known_opponent.update(systems)
        if pi.system:
            self._log(f"{pi.system}: {systems.get(pi.system, '?')}")
        else:
            self._log("inspect: " + ", ".join(f"{k}={v}" for k, v in systems.items()))

    def _handle_incoming_game_over(self, event: "net_bridge.NetEvent") -> None:
        reason = event.data.get("reason")
        if reason in ("kernel failure", "forfeit"):
            if reason == "forfeit":
                self.opponent_forfeited = True
                self.known_opponent.update({s: Status.COMPROMISED.value for s in BASE_SYSTEMS})
                self.known_opponent["terminal"] = Status.COMPROMISED.value
                self._log("opponent forfeited; attack the kernel for the final blow")
            else:
                self.phase = "game_over"
                self.winner = "you"
                self.game_over_reason = "kernel failure"
                self._log("GAME OVER - you win! (opponent: kernel failure)")
        elif reason == "timeout":
            if not self._final_tally_sent:
                self._my_final_tally = self.my.compromised_count()
                self.bridge.send_game_over(
                    reason="timeout", compromised_count=self._my_final_tally
                )
                self._final_tally_sent = True
            self._opponent_final_tally = event.data.get("compromised_count")
            self._maybe_conclude_timeout()

    def _handle_delivery_failed(self, event: "net_bridge.NetEvent") -> None:
        msg_type = event.data.get("msg_type")
        reason = event.data.get("reason")
        if msg_type == net_bridge.MSG_ATTACK:
            pa = self.pending_attacks.pop(event.seq, None)
            system = pa.system if pa else "?"
            self._log(f"! attack on {system} never reached them ({reason})")
        elif msg_type == net_bridge.MSG_INSPECT_REQUEST:
            self.pending_inspects.pop(event.seq, None)
            self._log(f"! inspect never reached them ({reason})")
        # attack_result / inspect_response failing just means the OTHER
        # side never finds out; nothing for us to clean up locally.

    def _declare_loss(self, reason: str) -> None:
        self.bridge.send_game_over(reason=reason)
        self.phase = "game_over"
        self.winner = "opponent"
        self.game_over_reason = reason
        self._log(f"GAME OVER - you lost ({reason})")

    # -------- logging --------

    def _log(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > LOG_MAX_LINES:
            self.log = self.log[-LOG_MAX_LINES:]
