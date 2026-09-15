import time
from dataclasses import dataclass
from enum import Enum
from random import Random
from typing import Optional

# Support both python -m game.main and direct module execution.
try:
    from . import net_bridge
except ImportError:
    import net_bridge

# -------- Constants --------
# Durations (seconds)
ATTACK_COOLDOWN = 2.0
REPAIR_DURATION = 5.0
HONEYPOT_SETUP_DURATION = 10.0
HONEYPOT_COOLDOWN = 60
DEFEND_WINDOW = 5.0
HONEYPOT_LOCKOUT = 5.0
TERMINAL_LOCKOUT = 30.0
MATCH_DURATION = 900.0 # 15 Min
WARNING_BANNER_DURATION = 3.0
ATTACK_RESULT_WAIT = 0.1
INSPECT_RESULT_WAIT = 0.1

# Logging
LOG_MAX_LINES = 255

# System status modifiers
FIREWALL_BASE_SUCCESS = { # Firewall
    "operational": 0.75,
    "degraded": 0.9,
    "compromised": 1.0
}
DEFEND_SUCCESS_MULTIPLIER = { # Antivirus
    "operational": 0.25,
    "degraded": 0.15,
    "compromised": 0.0
}
MISDIRECT_CHANCE = { # Routing table
    "operational": 0.0,
    "degraded": 0.25,
    "compromised": 0.6667
}
SELF_CORRUPTION_CHANCE = { # ARP cache
    "operational": 0.0, # plus chances of extremely rare but legitimate natural corruption
    "degraded": 0.1,
    "compromised": 0.3333
}
TYPE_SUCCESS_CHANCE = { # Terminal
    "operational": 1.0,
    "degraded": 0.6667,
    "compromised": 0.0
}

# Systems
BASE_SYSTEMS = ["firewall", "antivirus", "routing_table", "arp_cache"]
ALL_SYSTEMS = BASE_SYSTEMS + ["terminal", "kernel"]

# Commands and aliases
CMD_ALIASES = {
    "attack": ["atk", "attack", "tar", "target"], # attacks a system
    "defend": ["def", "defend"], # focuses antivirus on a system
    "repair": ["rep", "repair"], # repairs a system
    "inspect": ["ins", "inspect"], # inspects opponent's systems
    "honeypot": ["pot", "honeypot"], # masks a system with a honeypot
    "help": ["?", "help"], # lists commands and what they do
    "forfeit": ["exit", "quit", "forfeit", "upupdowndownleftrightleftrightabstart", "up up down down left right left right a b start"], # forfeit and quit game
}


def get_cmd_action(cmd: str) -> Optional[str]:
    for action, aliases in CMD_ALIASES.items():
        if cmd in aliases:
            return action
    return None


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

    def calculate_damage_score(self) -> int:
        score = 0

        for status in (self.firewall, self.antivirus, self.routing_table, self.arp_cache, self.terminal):
            if status == Status.COMPROMISED:
                score += 3
            elif status == Status.DEGRADED:
                score += 1

        return score


def _valid_target(systems: PlayerSystems, system: str) -> bool:
    if system in BASE_SYSTEMS:
        return True
    if system == "terminal":
        return all(systems.get(sys) == Status.COMPROMISED for sys in BASE_SYSTEMS)
    if system == "kernel":
        return all(systems.get(sys) == Status.COMPROMISED for sys in BASE_SYSTEMS) and systems.terminal == Status.COMPROMISED
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
    def __init__(self, bridge: "net_bridge.NetBridge", rng: Optional[Random] = None):
        self.bridge = bridge
        self._rng = rng or Random()

        self.my = PlayerSystems()
        self.known_opponent: dict = {} # from last inspect, may be out of date

        self.phase = "connecting" # connecting | playing | game_over
        self.winner: Optional[str] = None # you | opponent | draw | None (if not over yet)
        self.game_over_reason: Optional[str] = None # none if not over yet
        self.start_time: Optional[float] = None # none if not started yet

        self.log: list = []

        self.defended_system: Optional[str] = None # System antivirus is focusing on for better defense
        self.honeypot_system: Optional[str] = None # System masked by honeypot, if active

        self.busy_action: Optional[str] = None  # repair | honeypot
        self.busy_target: Optional[str] = None

        self.defended_until: float = 0.0
        self.honeypot_cooldown_until: float = 0.0
        self.busy_until: float = 0.0  # repair or honeypot setup in progress
        self.attack_cooldown_until: float = 0.0
        self.locked_until: float = 0.0 # terminal compromised and locked out temporarily
        self.warning_until: float = 0.0 # attack warning banner shown

        self.pending_attacks: dict = {} # seq: PendingAttack
        self.pending_inspects: dict = {} # seq: PendingInspect

        self.awaiting_forfeit_confirm = False
        self._final_tally_sent = False
        self._my_final_tally: Optional[int] = None
        self._opponent_final_tally: Optional[int] = None
        self.opponent_forfeited = False

    # -------- Connection --------

    def connect(self, timeout_ms: int = 10_000) -> bool:
        ok = self.bridge.connect(timeout_ms)
        if ok:
            self.phase = "playing"
            self.start_time = time.monotonic()
        return ok

    def time_remaining(self) -> float:
        if self.start_time is None:
            return MATCH_DURATION
        return max(0.0, MATCH_DURATION - (time.monotonic() - self.start_time))

    def can_type_character(self, character: str) -> bool:
        """Return whether a letter keystroke is successfully entered."""
        if not character.isalpha():
            return True
        chance = TYPE_SUCCESS_CHANCE[self.my.terminal.value]
        return self._rng.random() < chance

    # -------- Main loop --------

    def tick(self) -> None:
        now = time.monotonic()
        events = self.bridge.poll()

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
            self._log(f"repair complete: {self.busy_target}: {new_status.value}")
            self._log(f"repair complete: {self.busy_target}: {new_status.value}")
        elif self.busy_action == "honeypot":
            self.honeypot_system = self.busy_target
            self.honeypot_cooldown_until = now + HONEYPOT_COOLDOWN
            self._log(f"honeypot ready and masking {self.busy_target}")
        self.busy_until = 0.0
        self.busy_action = None
        self.busy_target = None

    def _check_pending_timeouts(self, now: float) -> None:
        for seq, pa in list(self.pending_attacks.items()):
            if now - pa.sent_time > ATTACK_RESULT_WAIT:
                self._log("[!] failed! (attack frame lost in transit)")
                del self.pending_attacks[seq]
        for seq, pi in list(self.pending_inspects.items()):
            if now - pi.sent_time > INSPECT_RESULT_WAIT:
                self._log("[!] failed! (inspect frame lost in transit)")

    def _check_match_timer(self, now: float) -> None:
        if self._final_tally_sent or self.start_time is None:
            return
        if now - self.start_time < MATCH_DURATION:
            return
        self._my_final_tally = self.my.calculate_damage_score()
        self.bridge.send_game_over("timeout", damage_score=self._my_final_tally)
        self._final_tally_sent = True
        self._log("time's up, exchanging damage scores...")
        self._maybe_conclude_timeout()

    def _maybe_conclude_timeout(self) -> None:
        if self.phase == "game_over":
            return
        if self._my_final_tally is None or self._opponent_final_tally is None:
            return

        self.phase = "game_over"
        self.game_over_reason = "timeout"

        my_score, opp_score  = self._opponent_final_tally, self._my_final_tally
        if my_score > opp_score :
            self.winner = "you"
        elif my_score < opp_score:
            self.winner = "opponent"
        else:
            self.winner = "draw"

        self._log(f"GAME OVER (timer expired) - {self._win_line()} You scored {my_score} pts vs {opp_score} pts")

    def _win_line(self) -> str:
        if self.winner == "you":
            return "You win!"
        if self.winner == "opponent":
            return "You lose..."
        return "It's a draw."

    # -------- Commands --------

    def handle_command(self, raw_line: str) -> None:
        if self.phase != "playing":
            return
        now = time.monotonic()

        if self.locked_until and now < self.locked_until:
            self._log("[!] terminal locked, can't act")
            return

        parts = raw_line.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        action = get_cmd_action(cmd)
        if action is None:
            self._log(f"unknown command: '{raw_line}'")
            return
        
        if action != "forfeit" and self.awaiting_forfeit_confirm:
            self.awaiting_forfeit_confirm = False
            
        if action != "forfeit" and self.busy_until and now < self.busy_until:
            self._log("[!] busy with another task")
            return
        
        if action == "attack":
            if not args:
                self._log(f"[!] usage: {cmd} <system>")
                return
            self._cmd_attack(args[0].lower(), now)
        elif action == "defend":
            if not args:
                self._log(f"[!] usage: {cmd} <system>")
                return
            self._cmd_defend(args[0].lower(), now)
        elif action == "repair":
            if not args:
                self._log(f"[!] usage: {cmd} <system>")
                return
            self._cmd_repair(args[0].lower(), now)
        elif action == "inspect":
            self._cmd_inspect(args[0].lower() if args else None, now)
        elif action == "honeypot":
            if not args:
                self._log(f"[!] usage: {cmd} <system>")
                return
            self._cmd_honeypot(args[0].lower(), now)
        elif action == "help":
            self._cmd_help()
        elif action == "forfeit":
            self._cmd_forfeit()
            
    def _opponent_target_valid(self, system: str) -> bool:
        if system in BASE_SYSTEMS:
            return True
        if self.opponent_forfeited:
            return system == "kernel"
        if system == "terminal":
            return all(self.known_opponent.get(sys) == Status.COMPROMISED.value for sys in BASE_SYSTEMS)
        if system == "kernel":
            return all(self.known_opponent.get(sys) == Status.COMPROMISED.value for sys in BASE_SYSTEMS) and self.known_opponent.get("terminal") == Status.COMPROMISED.value
        return False

    def _cmd_attack(self, system: str, now: float) -> None:
        if system not in ALL_SYSTEMS:
            self._log(f"[!] unknown system '{system}'")
            return
        if now < self.attack_cooldown_until:
            self._log("[!] attack on cooldown")
            return
        if system == "kernel" and self.opponent_forfeited:
            self.bridge.send_attack(system)
            self.phase = "game_over"
            self.winner = "you"
            self.game_over_reason = "forfeit"
            self._log("GAME OVER - You win! (final blow after opponent forfeited)")
            return

        actual_system = system
        mis_chance = MISDIRECT_CHANCE[self.my.routing_table.value]
        if mis_chance and self._rng.random() < mis_chance:
            others = [
                s for s in ALL_SYSTEMS
                if s != system and self._opponent_target_valid(s)
            ]
            if not others:
                others = [system]
            actual_system = self._rng.choice(others)
            if actual_system != system:
                self._log(f"[!] routing table misdirected attack: {system} to {actual_system}")

        self.bridge.self_corruption_chance = SELF_CORRUPTION_CHANCE[self.my.arp_cache.value]
        seq = self.bridge.send_attack(actual_system)
        self.pending_attacks[seq] = PendingAttack(system=actual_system, sent_time=now)
        self.attack_cooldown_until = now + ATTACK_COOLDOWN

    def _cmd_defend(self, system: str, now: float) -> None:
        if system not in ALL_SYSTEMS:
            self._log(f"[!] unknown system '{system}'")
            return
        if self.my.antivirus == Status.COMPROMISED:
            self._log("[!] can't defend, antivirus is compromised")
            return
        self.defended_system = system
        self.defended_until = now + DEFEND_WINDOW
        self._log(f"focusing antivirus on {system} for {DEFEND_WINDOW:.1f} seconds...")

    def _cmd_repair(self, system: str, now: float) -> None:
        if system not in ALL_SYSTEMS:
            self._log(f"[!] unknown system '{system}'")
            return
        if system == "kernel":
            self._log("[!] can't repair kernel if you've already lost, how did you even trigger this message?")
            return
        if self.my.get(system) == Status.OPERATIONAL:
            self._log(f"[!] {system} is already fully operational")
            return
        self.busy_action = "repair"
        self.busy_target = system
        self.busy_until = now + REPAIR_DURATION
        self._log(f"repairing {system}...")

    def _cmd_inspect(self, system: Optional[str], now: float) -> None:
        if system is not None and system not in ALL_SYSTEMS:
            self._log(f"[!] unknown system '{system}'")
            return
        seq = self.bridge.send_inspect_request(system)
        self.pending_inspects[seq] = PendingInspect(system=system, sent_time=now)

    def _cmd_honeypot(self, system: str, now: float) -> None:
        if system not in BASE_SYSTEMS:
            self._log(f"[!] unknown system '{system}'")
            return
        self.busy_action = "honeypot"
        self.busy_target = system
        self.busy_until = now + HONEYPOT_SETUP_DURATION
        self._log(f"setting up honeypot to mask {system}...")

    def _cmd_help(self) -> None:
        self._log("Available commands:")
        for cmd, aliases in CMD_ALIASES.items():
            aliases = [a for a in aliases if a != cmd and a not in ["upupdowndownleftrightleftrightabstart", "up up down down left right left right a b start"]]
            self._log(f"  {cmd} (aliases: {', '.join(aliases)})")
        return

    # -------- Incoming events --------

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

    def _handle_incoming_attack(self, event: "net_bridge.NetEvent", now: float) -> None:
        system = event.data.get("system")
        orig_seq = event.seq
        if system not in ALL_SYSTEMS:
            self.bridge.send_attack_result(system or "", False, orig_seq)
            return

        if self.my.antivirus != Status.COMPROMISED:
            self.warning_until = now + WARNING_BANNER_DURATION

        if self.honeypot_system == system:
            self.honeypot_system = None
            self._log(f"[honeypot] opponent's attack on {system} hit the honeypot, masking is now disabled and opponent is temporarily disabled")
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
                    self.locked_until = now + TERMINAL_LOCKOUT
                    self._log("[!] TERMINAL COMPROMISED! Locked out for 30 seconds")

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
            base *= DEFEND_SUCCESS_MULTIPLIER[self.my.antivirus.value]
        return self._rng.random() < base

    def _handle_attack_result(self, event: "net_bridge.NetEvent") -> None:
        orign_seq = event.data.get("orig_seq")
        pa = self.pending_attacks.pop(orign_seq, None)
        if pa is None:
            return
        if event.data.get("honeypot_hit"):
            self.locked_until = time.monotonic() + HONEYPOT_LOCKOUT
            self._log(f"[!] failed! You fell into a honeypot trap (locked out for {HONEYPOT_LOCKOUT:.1f} seconds)")
        elif event.data.get("success"):
            self._log(f"attack on {pa.system} succeeded!")
        else:
            self._log("[!] failed!")

    def _handle_inspect_request(self, event: "net_bridge.NetEvent") -> None:
        requested_system = event.data.get("system")
        orig_seq = event.seq

        if requested_system is not None and requested_system not in ALL_SYSTEMS:
            self.bridge.send_inspect_response({}, orig_seq, True)
            return

        targets = [requested_system] if requested_system else ALL_SYSTEMS
        report = {}
        for sys in targets:
            if sys == "kernel":
                report[sys] = "compromised" if self.my.kernel_compromised else "operational"
            elif self.honeypot_system == sys:
                report[sys] = "operational"
            else:
                report[sys] = self.my.get(sys).value

        self.bridge.send_inspect_response(report, orig_seq, False)

    def _handle_inspect_response(self, event: "net_bridge.NetEvent") -> None:
        orig_seq = event.data.get("orig_seq")
        pi = self.pending_inspects.pop(orig_seq, None)
        if pi is None:
            return
        if event.data.get("blocked"):
            self._log("[!] failed! Blocked by firewall")
            return
        systems = event.data.get("systems", {})
        self.known_opponent.update(systems)
        if pi.system:
            self._log(f"{pi.system}: {systems.get(pi.system, '?')}")
        else:
            self._log("inspect: " + ", ".join(f"{sys}={status}" for sys, status in systems.items()))

    def _handle_incoming_game_over(self, event: "net_bridge.NetEvent") -> None:
        reason = event.data.get("reason")
        if reason in ["kernel failure", "forfeit"]:
            if reason == "forfeit":
                self.opponent_forfeited = True
                self.known_opponent.update({s: Status.COMPROMISED.value for s in BASE_SYSTEMS})
                self.known_opponent["terminal"] = Status.COMPROMISED.value
                self._log("Opponent forfeited. Attack the kernel for the final blow!")
            else:
                self.phase = "game_over"
                self.winner = "you"
                self.game_over_reason = "kernel failure"
                self._log("GAME OVER - You win! (opponent kernel compromised)")
        elif reason == "timeout":
            if not self._final_tally_sent:
                self._my_final_tally = self.my.calculate_damage_score()
                self.bridge.send_game_over("timeout", self._my_final_tally)
                self._final_tally_sent = True
            self._opponent_final_tally = event.data.get("damage_score")
            self._maybe_conclude_timeout()

    def _handle_delivery_failed(self, event: "net_bridge.NetEvent") -> None:
        msg_type = event.data.get("msg_type")
        reason = event.data.get("reason")
        if msg_type == net_bridge.MSG_ATTACK:
            pa = self.pending_attacks.pop(event.seq, None)
            system = pa.system if pa else "?"
            self._log(f"[!] failed! Attack on {system} never reached opponent ({reason})")
        elif msg_type == net_bridge.MSG_INSPECT_REQUEST:
            self.pending_inspects.pop(event.seq, None)
            self._log(f"[!] failed! Inspect never reached opponent ({reason})")

    def _declare_loss(self, reason: str) -> None:
        self.bridge.send_game_over(reason)
        self.phase = "game_over"
        self.winner = "opponent"
        self.game_over_reason = reason
        self._log(f"GAME OVER - You lose... ({reason})")

    def _log(self, msg: str) -> None:
        self.log.append(msg)
        if len(self.log) > LOG_MAX_LINES:
            self.log = self.log[-LOG_MAX_LINES:]
