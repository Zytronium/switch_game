"""Terminal UI and entry point for Switch 'n Hack."""

import argparse
import curses
from dataclasses import dataclass
import json
import os
import re
import sys
import threading
import time
import textwrap
from pathlib import Path
from typing import Optional, Sequence

try:  # Package execution: ``python -m game.main``.
    from .net_bridge import NetBridge, NetEvent
    from .state import Game, Status, get_cmd_action

except ImportError:  # Direct execution with ``game`` on PYTHONPATH.
    from net_bridge import NetBridge, NetEvent
    from state import Game, Status, get_cmd_action


FRAME_RATE = 60.0
FRAME_INTERVAL_S = 1.0 / FRAME_RATE
ESCAPE_SEQUENCE_TIMEOUT_S = 0.1
SYSTEM_LABELS = {
    "firewall": "FIREWALL",
    "antivirus": "ANTIVIRUS",
    "routing_table": "ROUTING TABLE",
    "arp_cache": "ARP CACHE",
    "terminal": "TERMINAL",
    "kernel": "KERNEL",
}
COMMAND_COMPLETIONS = (
    "atk", "attack", "def", "defend", "exit", "forfeit", "honeypot",
    "ins", "inspect", "pot", "quit", "rep", "repair", "tar", "target",
)
SYSTEM_COMPLETIONS = ("antivirus", "arp_cache", "firewall", "kernel", "routing_table", "terminal")

COLOR_PAIRS = {
    "green": 1,
    "yellow": 2,
    "red": 3,
    "blue": 4,
    "gray": 5,
    "cyan": 6,
}
STATUS_COLORS = {
    "operational": "green",
    "degraded": "yellow",
    "compromised": "red",
}
_STATUS_PATTERN = re.compile(r"\b(operational|degraded|compromised)\b", re.IGNORECASE)
_COLORS_READY = False
CONFIG_PATH = Path.home() / "switch_n_hack" / "config.json"
TUTORIAL_JSON_PATH = Path(__file__).parent / "tutorial.json"
_PRACTICE_PEER_MAC = "02:00:00:00:00:01"

@dataclass
class TutorialStep:
    title: str
    content: str
    tips: list[str]
    interactive: bool
    demo_command: Optional[str]
    next_button: str


def _load_tutorial_steps(path: Path = TUTORIAL_JSON_PATH) -> list[TutorialStep]:
    with path.open(encoding="utf-8") as tutorial_file:
        raw_steps = json.load(tutorial_file)

    if not isinstance(raw_steps, list):
        raise ValueError(f"{path} must contain a JSON array of tutorial steps.")

    steps = []
    for raw_step in raw_steps:
        steps.append(TutorialStep(
            title=raw_step.get("title", ""),
            content=raw_step.get("content", ""),
            tips=raw_step.get("tips", []),
            interactive=raw_step.get("interactive", False),
            demo_command=raw_step.get("demo_command", None),
            next_button=raw_step.get("next_button", "Press Enter")
        ))
    return steps


def _get_config_path() -> Path:
    """Return the per-user configuration path."""
    return Path(os.path.expanduser("~/switch_n_hack/config.json"))


def _load_config(config_path: Optional[Path] = None) -> dict:
    """Load configuration, falling back to an unseen tutorial."""
    path = config_path or _get_config_path()
    try:
        with path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"seen_tutorial": False}

    if not isinstance(config, dict):
        return {"seen_tutorial": False}
    if "seen_tutorial" not in config:
        config["seen_tutorial"] = False
    return config


def _save_config(config_dict: dict, config_path: Optional[Path] = None) -> None:
    """Persist configuration without preventing the game from starting."""
    path = config_path or _get_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as config_file:
            json.dump(config_dict, config_file, indent=2)
            config_file.write("\n")
    except (OSError, TypeError, ValueError) as exc:
        print(f"Warning: unable to save configuration: {exc}", file=sys.stderr)


def ensure_config(config_path: Path = CONFIG_PATH) -> None:
    """Ensure the tutorial marker exists without changing its current value."""
    _save_config(_load_config(config_path), config_path)


def _auto_detect_interface(sysfs_root: Path = Path("/sys/class/net")) -> str:
    try:
        import switch_net
    except ModuleNotFoundError as exc:
        raise RuntimeError("unable to auto-detect an Ethernet interface: switch_net is unavailable") from exc

    for name, mac in switch_net.list_interfaces():
        interface_path = sysfs_root / name
        try:
            is_up = (interface_path / "operstate").read_text(encoding="ascii").strip() == "up"
        except OSError:
            continue
        if not mac or not is_up:
            continue
        # Physical interfaces have a device symlink; virtual interfaces such as
        # bridges, VLANs, and containers generally do not.
        if not (interface_path / "device").exists():
            continue
        if (interface_path / "wireless").exists():
            continue
        return name

    raise RuntimeError("no active wired Ethernet interface with a MAC address was found")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Play Switch 'n Hack over Ethernet.")
    parser.add_argument(
        "interface",
        nargs="?",
        help="Ethernet interface to use, for example eth0",
    )
    parser.add_argument(
        "--peer-mac",
        help="optional peer MAC address; otherwise discover a peer by broadcast",
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=30000,
        metavar="MILLISECONDS",
        help="how long to wait for the other player (default: 10000)",
    )
    parser.add_argument(
        "--tutorial",
        action="store_true",
        help="Play tutorial")
    return parser


def _run_tutorial(screen: "curses.window", steps: list[TutorialStep]) -> None:
    _init_colors()
    curses.curs_set(0)
    curses.noecho()
    curses.cbreak()
    screen.keypad(True)
    screen.nodelay(False)

    practice_game: Optional[Game] = None
    index = 0
    while index < len(steps):
        step = steps[index]

        _render_tutorial_card(screen, step, index, len(steps))
        key = screen.getch()
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            continue
        if key in (27, ord("q"), ord("Q"), 3):
            return
        if key not in (curses.KEY_ENTER, 10, 13):
            continue  # anything else: redraw this same slide, don't advance

        if not step.interactive:
            index += 1
            continue

        # interactive: hand off to the real dashboard temporarily
        if practice_game is None:
            practice_game = Game(_PracticeBridge())
            practice_game.phase = "playing"
            practice_game.start_time = time.monotonic()

        _prime_practice_state_for_step(practice_game, step)

        if not _run_interactive_step(screen, practice_game, step):
            return
        index += 1


class _PracticeBridge:
    def __init__(self) -> None:
        self._next_seq = 1
        self._pending: list[tuple[float, "NetEvent"]] = []
        self.self_corruption_chance = 0.0

    def _seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def _queue(self, event: "NetEvent", delay: float = 0.4) -> None:
        self._pending.append((time.monotonic() + delay, event))

    # outgoing sends called by Game

    def send_attack(self, system: str) -> int:
        seq = self._seq()
        self._queue(NetEvent(
            type="attack_result",
            seq=seq,
            data={"orig_seq": seq, "system": system, "success": True},
            src_mac=_PRACTICE_PEER_MAC
        ))

    def send_attack_result(self, system: str, success: bool, orig_seq: int, honeypot_hit: bool = False) -> None:
        pass  # No opponent listing for result

    def send_inspect_request(self, system: Optional[str]) -> int:
        seq = self._seq()
        targets = [system] if system else ["firewall", "antivirus", "routing_table", "arp_cache", "terminal", "kernel"]
        report = {sys: "operational" for sys in targets}
        self._queue(NetEvent(
            type="inspect_response",
            seq=seq,
            data={"orig_seq": seq, "systems": report, "blocked": False},
            src_mac=_PRACTICE_PEER_MAC
        ))
        return seq

    def send_inspect_response(self, report: dict, orig_seq: int, blocked: bool) -> None:
        pass

    def send_game_over(self, reason: str, damage_score: Optional[int] = None) -> None:
        pass

    def poll(self) -> list:
        now = time.monotonic()
        ready = [event for ready_time, event in self._pending if ready_time <= now]
        self._pending = [pair for pair in self._pending if pair[0] > now]
        return ready


def _safe_add(window: "curses.window", y: int, x: int, text: str, width: int,
              attr: int = 0) -> None:
    """Write a clipped line without letting a small terminal crash curses."""
    screen_width = window.getmaxyx()[1]
    if y < 0 or x >= screen_width:
        return
    try:
        window.addnstr(y, max(0, x), text, max(0, width), attr)
    except curses.error:
        pass


def _init_colors() -> None:
    """Set up the small, semantic palette used by the dashboard."""
    global _COLORS_READY
    if _COLORS_READY or not curses.has_colors():
        return
    try:
        curses.start_color()
        if hasattr(curses, "use_default_colors"):
            curses.use_default_colors()
        curses.init_pair(COLOR_PAIRS["green"], curses.COLOR_GREEN, -1)
        curses.init_pair(COLOR_PAIRS["yellow"], curses.COLOR_YELLOW, -1)
        curses.init_pair(COLOR_PAIRS["red"], curses.COLOR_RED, -1)
        curses.init_pair(COLOR_PAIRS["blue"], curses.COLOR_BLUE, -1)
        curses.init_pair(COLOR_PAIRS["gray"], curses.COLOR_WHITE, -1)
        curses.init_pair(COLOR_PAIRS["cyan"], curses.COLOR_CYAN, -1)
        _COLORS_READY = True
    except curses.error:
        # Monochrome terminals still get the complete, usable UI.
        _COLORS_READY = False


_CONNECTING_SPINNER = (
    ("╭─   ", "│    ", "     "),
    ("╭──  ", "     ", "     "),
    (" ─── ", "     ", "     "),
    ("  ──╮", "     ", "     "),
    ("   ─╮", "    │", "     "),
    ("    ╮", "    │", "    ╯"),
    ("     ", "    │", "   ─╯"),
    ("     ", "     ", "  ──╯"),
    ("     ", "     ", " ─── "),
    ("     ", "     ", "╰──  "),
    ("     ", "│    ", "╰─   "),
    ("╭    ", "│    ", "╰    "),
)


def _render_connecting(screen: "curses.window", interface: str, now: float,
                       timeout_ms: int = 0, started_at: float = 0.0) -> None:
    """Render the animated screen shown while discovering the opponent."""
    height, width = screen.getmaxyx()
    screen.erase()
    if height < 12 or width < 48:
        _safe_add(screen, 0, 0, "Terminal too small; resize to at least 48x12.", width,
                  curses.A_BOLD)
        screen.refresh()
        return

    horizontal = "═" * (width - 2)
    _safe_add(screen, 0, 0, "╔" + horizontal + "╗", width)
    for row in range(1, height - 1):
        _safe_add(screen, row, 0, "║", width)
        _safe_add(screen, row, width - 1, "║", width)
    _safe_add(screen, height - 1, 0, "╚" + horizontal + "╝", width)

    title = "Switch 'n Hack"
    spinner = _CONNECTING_SPINNER[int(now * 12) % len(_CONNECTING_SPINNER)]
    _safe_add(screen, 2, max(2, (width - len(title)) // 2), title, width - 1,
              curses.A_BOLD | _color_attr("cyan"))
    spinner_top = height // 2 - 2
    for offset, line in enumerate(spinner):
        _safe_add(screen, spinner_top + offset, max(2, (width - len(line)) // 2), line,
                  width - 1, curses.A_BOLD | _color_attr("yellow"))
    message = "Waiting for opponent"
    _safe_add(screen, spinner_top + len(spinner) + 1, max(2, (width - len(message)) // 2),
              message, width - 1, curses.A_BOLD | _color_attr("yellow"))
    if timeout_ms > 0:
        remaining = max(0, int((timeout_ms / 1000) - (now - started_at) + 0.999))
        timer = f"Timeout in {remaining}s"
        _safe_add(screen, spinner_top + len(spinner) + 2, max(2, (width - len(timer)) // 2),
                  timer, width - 1, _color_attr("gray"))
        cancel_msg = "Press Ctrl+C to cancel"
        _safe_add(screen, spinner_top + len(spinner) + 3, max(2, (width - len(cancel_msg)) // 2),
                  cancel_msg, width - 1, _color_attr("gray"))
    connection = f"Listening on {interface}"
    _safe_add(screen, height - 2, max(2, (width - len(connection)) // 2), connection, width - 1,
              _color_attr("gray"))
    screen.refresh()


def _run_connection_screen(screen: "curses.window", game: Game, interface: str,
                           timeout_ms: int) -> bool:
    """Animate the connection screen while the blocking handshake runs."""
    _init_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    result: dict[str, object] = {}
    started_at = time.monotonic()

    def connect() -> None:
        try:
            result["connected"] = game.connect(timeout_ms=timeout_ms)
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    while worker.is_alive():
        _render_connecting(screen, interface, time.monotonic(), timeout_ms, started_at)
        time.sleep(0.1)
    worker.join()

    if "error" in result:
        raise result["error"]
    return bool(result.get("connected", False))


def _color_attr(color: str, extra: int = 0) -> int:
    if not _COLORS_READY:
        return extra
    return curses.color_pair(COLOR_PAIRS[color]) | extra


def _default_attr(game: Game) -> int:
    """Use gray for otherwise-uncolored UI when the local terminal is compromised."""
    if game.my.terminal.value == "compromised":
        return _color_attr("gray", curses.A_DIM)
    return 0


def _line_attr(line: str, game: Game) -> int:
    if line.startswith("GAME OVER"):
        if game.winner == "you":
            return _color_attr("green", curses.A_BOLD)
        if game.winner == "opponent":
            return _color_attr("red", curses.A_BOLD)
    if line.startswith("[!]") or "failed" in line.lower():
        return _color_attr("red")
    if "succeeded" in line.lower() or "repaired" in line.lower():
        return _color_attr("green")
    return _default_attr(game)


def _add_log_line(window: "curses.window", y: int, x: int, line: str,
                  width: int, game: Game) -> None:
    """Render a log line, coloring inspection status values individually."""
    line_attr = _line_attr(line, game)
    position = 0
    remaining = width
    for match in _STATUS_PATTERN.finditer(line):
        if remaining <= 0:
            break
        prefix = line[position:match.start()]
        if prefix:
            _safe_add(window, y, x, prefix, min(len(prefix), remaining), line_attr)
            x += len(prefix)
            remaining -= len(prefix)
        if remaining <= 0:
            break
        value = match.group(1)
        attr = _color_attr(STATUS_COLORS[value.lower()])
        _safe_add(window, y, x, value, min(len(value), remaining), attr)
        x += len(value)
        remaining -= len(value)
        position = match.end()
    if remaining > 0 and position < len(line):
        _safe_add(window, y, x, line[position:], remaining, line_attr)


def _status_bar(status: "object") -> str:
    value = getattr(status, "value", str(status))
    if value == "operational":
        return "[████] Operational"
    if value == "degraded":
        return "[██░░] Degraded"
    return "[░░░░] Compromised"


def _remaining(until: float, now: float) -> str:
    if until <= now:
        return "READY"
    seconds = max(0, int(until - now + 0.999))
    return f"00:{seconds:02d}"


def _wrap_log(lines: Sequence[str], width: int, max_lines: int) -> list[str]:
    """Wrap log entries to fit the left pane and return the visible tail."""
    if width <= 0 or max_lines <= 0:
        return []

    wrapped: list[str] = []
    for line in lines:
        chunks = textwrap.wrap(
            str(line),
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
        )
        wrapped.extend(chunks or [""])
    return wrapped[-max_lines:]


def _render(screen: "curses.window", game: Game, command: str,
            cursor: int, command_history: Sequence[str], now: float,
            tutorial_hint: Optional[str] = None) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    if height < 20 or width < 72:
        _safe_add(screen, 0, 0, "Terminal too small; resize to at least 72x20.", width,
                  curses.A_BOLD)
        screen.refresh()
        return

    warning = "[WARNING] INCOMING ATTACK DETECTED!" if game.warning_until > now else ""
    title = "Switch 'n Hack"
    inner = width - 2
    half_left = inner // 2
    half_right = inner - half_left
    default_attr = _default_attr(game)

    # top banner: tutorial hint takes priority over the real warning
    if tutorial_hint:
        banner_text = tutorial_hint
        banner_attr = _color_attr("cyan", curses.A_BOLD)
    elif game.warning_until > now:
        banner_text = "[WARNING] INCOMING ATTACK DETECTED!"
        banner_attr = _color_attr("yellow", curses.A_BOLD | curses.A_BLINK)
    else:
        banner_text = ""
        banner_attr = default_attr

    _safe_add(screen, 0, 0, "╔" + "═" * (width - 2) + "╗", width, default_attr)
    _safe_add(screen, 1, 0, "║", width, default_attr)
    _safe_add(screen, 1, 2, banner_text, width - 2, banner_attr)
    _safe_add(screen, 1, max(2, width - len(title) - 3), title, width - 1,
              default_attr | curses.A_BOLD)
    _safe_add(screen, 1, width - 1, "║", width, default_attr)
    _safe_add(screen, 2, 0, "╠" + "═" * half_left + "╦" + "═" * (half_right - 1) + "╣", width,
              default_attr)

    left_width = max(30, width // 2)
    divider = min(width - 2, left_width)
    for row in range(3, height - 2):
        _safe_add(screen, row, 0, "║", width, default_attr)
        _safe_add(screen, row, divider, "║", width, default_attr)
        _safe_add(screen, row, width - 1, "║", width, default_attr)

    _safe_add(screen, 4, divider + 2, "SYSTEM STATUS", width - divider - 2,
              default_attr | curses.A_BOLD)
    systems = ("firewall", "antivirus", "routing_table", "arp_cache", "terminal", "kernel")
    for index, system in enumerate(systems):
        row = 6 + index * 2
        status = ("compromised" if game.my.kernel_compromised else "operational") \
            if system == "kernel" else game.my.get(system)
        label = SYSTEM_LABELS[system]
        status_value = getattr(status, "value", str(status))
        _safe_add(screen, row, divider + 2, f"{label:<14} ", width - divider - 3, default_attr)
        _safe_add(screen, row, divider + 17, _status_bar(status), width - divider - 18,
                  _color_attr(STATUS_COLORS[status_value]))

    hp_row = 19
    if hp_row < height - 3:
        _safe_add(screen, hp_row, divider + 2, "HONEYPOT STATUS", width - divider - 2,
                  default_attr | curses.A_BOLD)
        if game.busy_action == "honeypot":
            hp_text = f"[SETTING UP] {game.busy_target or ''} ({_remaining(game.busy_until, now)})"
        elif game.honeypot_system:
            hp_text = f"[ACTIVE] Masking: {SYSTEM_LABELS.get(game.honeypot_system, game.honeypot_system)}"
        else:
            hp_text = "[INACTIVE]"
        hp_color = "green" if game.honeypot_system else "cyan"
        if game.busy_action == "honeypot":
            hp_color = "yellow"
        _safe_add(screen, hp_row + 1, divider + 2, hp_text, width - divider - 3,
                  _color_attr(hp_color))

    cooldown_row = min(height - 5, hp_row + 4)
    _safe_add(screen, cooldown_row, divider + 2, "COOLDOWNS", width - divider - 2,
              default_attr | curses.A_BOLD)
    repair = _remaining(game.busy_until, now) if game.busy_action == "repair" else "READY"
    honeypot = _remaining(game.honeypot_cooldown_until, now)
    attack_text = _remaining(game.attack_cooldown_until, now)
    repair_text = repair
    _safe_add(screen, cooldown_row + 1, divider + 2, "Attack: ", width - divider - 3, default_attr)
    _safe_add(screen, cooldown_row + 1, divider + 10, f"{attack_text:<7}", width - divider - 10,
              _color_attr("green" if attack_text == "READY" else "yellow"))
    _safe_add(screen, cooldown_row + 1, divider + 18, "Repair: ", width - divider - 18, default_attr)
    _safe_add(screen, cooldown_row + 1, divider + 26, f"{repair_text:<7}", width - divider - 26,
              _color_attr("green" if repair_text == "READY" else "yellow"))
    _safe_add(screen, cooldown_row + 2, divider + 2, "Honeypot: ", width - divider - 3, default_attr)
    _safe_add(screen, cooldown_row + 2, divider + 12, honeypot, width - divider - 12,
              _color_attr("green" if honeypot == "READY" else "yellow"))

    log_top = 4
    log_bottom = height - 4
    visible = _wrap_log(game.log, divider - 3, log_bottom - log_top)
    for row, line in enumerate(visible, log_top):
        _add_log_line(screen, row, 2, line, divider - 3, game)

    _safe_add(screen, height - 3, 0, "║" + " " * half_left + "╚" + "═" * (half_right - 1) + "╣", width, default_attr)
    prompt = "> " + command
    _safe_add(screen, height - 2, 0, "║", width, default_attr)
    _safe_add(screen, height - 2, 2, prompt, width - 3, default_attr)
    _safe_add(screen, height - 2, width - 1, "║", width, default_attr)
    _safe_add(screen, height - 1, 0, "╚" + "═" * (width - 2) + "╝", width, default_attr)

    # Keep the cursor in the input box, even when the command is wider than it.
    cursor_x = min(width - 2, 4 + cursor)
    try:
        screen.move(height - 2, cursor_x)
    except curses.error:
        pass
    screen.refresh()


def _read_key(screen: "curses.window") -> Optional[int]:
    try:
        key = screen.get_wch()
    except curses.error:
        return None
    if isinstance(key, str):
        return ord(key)
    return key


def _autocomplete(command: str, cursor: int) -> tuple[str, int]:
    """Complete the word at the cursor, returning the new input and cursor."""
    cursor = max(0, min(cursor, len(command)))
    start = cursor
    while start > 0 and not command[start - 1].isspace():
        start -= 1
    end = cursor
    while end < len(command) and not command[end].isspace():
        end += 1

    prefix = command[start:cursor].lower()
    before = command[:start]
    candidates = COMMAND_COMPLETIONS if not before.strip() else SYSTEM_COMPLETIONS
    matches = [candidate for candidate in candidates if candidate.startswith(prefix)]
    if not matches:
        return command, cursor

    # Complete the shared prefix when several choices remain; a unique command
    # also gets a separating space so a second Tab can complete its argument.
    completion = matches[0]
    if len(matches) > 1:
        common = matches[0]
        for match in matches[1:]:
            length = 0
            while length < min(len(common), len(match)) and common[length] == match[length]:
                length += 1
            common = common[:length]
        completion = common
    completed = before + completion + command[end:]
    new_cursor = start + len(completion)
    if len(matches) == 1 and not before.strip() and end == len(command):
        completed += " "
        new_cursor += 1
    return completed, new_cursor


def _delete_previous_word(command: str, cursor: int) -> tuple[str, int]:
    """Delete the word immediately before the cursor, like Alt+Backspace."""
    end = cursor
    while cursor > 0 and command[cursor - 1].isspace():
        cursor -= 1
    while cursor > 0 and not command[cursor - 1].isspace():
        cursor -= 1
    return command[:cursor] + command[end:], cursor


def _render_tutorial_card(screen: "curses.window", step: TutorialStep, index: int, total: int) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    if height < 14 or width < 60:
        _safe_add(screen, 0, 0, "Terminal too small; resize to at least 60x14.", width, curses.A_BOLD)
        screen.refresh()
        return

    header = "Switch 'n Hack - Tutorial"
    progress = f"[{index + 1}/{total}]"

    # frame
    _safe_add(screen, 0, 0, "╔" + "═" * (width - 2) + "╗", width)
    _safe_add(screen, 1, 0, "║", width)
    _safe_add(screen, 1, 2, header, width - 4, curses.A_BOLD | _color_attr("cyan"))
    _safe_add(screen, 1, max(2, width - len(progress) - 3), progress, width - 1, _color_attr("gray"))
    _safe_add(screen, 1, width - 1, "║", width)
    _safe_add(screen, 2, 0, "╠" + "═" * (width - 2) + "╣", width)
    for row in range(3, height - 3):
        _safe_add(screen, row, 0, "║", width)
        _safe_add(screen, row, width - 1, "║", width)
    _safe_add(screen, height - 3, 0, "╠" + "═" * (width - 2) + "╣", width)
    _safe_add(screen, height - 1, 0, "╚" + "═" * (width - 2) + "╝", width)

    body_width = width - 4
    row = 4

    # title
    _safe_add(screen, row, 2, step.title, body_width, curses.A_BOLD | _color_attr("yellow"))
    row += 2

    # body text
    for line in textwrap.wrap(step.content, width=body_width):
        if row >= height - 4:
            break
        _safe_add(screen, row, 2, line, body_width, 0)
        row += 1

    # tips
    if step.tips and row < height - 4:
        row += 1
        row += 1
        for tip in step.tips:
            if row >= height - 4:
                break
            for line in textwrap.wrap(f"‣ {tip}", width=body_width):
                if row >= height - 4:
                    break
                _safe_add(screen, row, 2, line, body_width, _color_attr("gray"))
                row += 1

    # footer prompt
    _safe_add(screen, height - 2, 0, "║", width)
    _safe_add(screen, height - 2, 2, step.next_button, body_width, curses.A_BLINK | curses.A_REVERSE)
    _safe_add(screen, height - 2, width - 1, "║", width)

    screen.refresh()

def _run_interactive_step(screen: "curses.window", game: Game, step: TutorialStep) -> bool:
    curses.curs_set(1)
    command = ""
    cursor = 0
    history: list = []
    history_index: Optional[int] = None
    escape_pending = False
    escape_deadline = 0.0
    next_frame = time.monotonic()

    demo_action = None
    demo_system = None
    if step.demo_command:
        demo_parts = step.demo_command.split()
        demo_action = get_cmd_action(demo_parts[0].lower())
        demo_system = demo_parts[1].lower() if len(demo_parts) > 1 else None
    hint = f"Try it: {step.demo_command}" if step.demo_command else step.next_button

    while True:
        now = time.monotonic()
        if escape_pending and now >= escape_deadline:
            return False
        if now >= next_frame:
            game.tick()
            _render(screen, game, command, cursor, history, now, tutorial_hint=hint)
            next_frame = now + FRAME_INTERVAL_S

        key = _read_key(screen)
        if key is not None:
            if key == 3:  # Ctrl-C
                return False
            if escape_pending:
                if key in (curses.KEY_BACKSPACE, 8, 127):
                    command, cursor = _delete_previous_word(command, cursor)
                    escape_pending = False
                    continue
                return False
            if key == 27:
                escape_pending = True
                escape_deadline = time.monotonic() + ESCAPE_SEQUENCE_TIMEOUT_S
                continue
            if key in (getattr(curses, "KEY_TAB", 9), 9):
                command, cursor = _autocomplete(command, cursor)
            if key in (curses.KEY_ENTER, 10, 13):
                submitted = command
                command = ""
                cursor = 0
                if submitted.strip():
                    history.append(submitted)
                    game.log.append("> " + submitted)
                    game.handle_command(submitted)
                    history_index = None

                    # did this satisfy the demo?
                    typed_parts = submitted.strip().split()
                    typed_action = get_cmd_action(typed_parts[0].lower())
                    typed_system = typed_parts[1].lower() if len(typed_parts) > 1 else None
                    matched = (
                            demo_action is None
                            or (typed_action == demo_action and typed_system == demo_system)
                    )
                    if matched:
                        _render(screen, game, command, cursor, history,
                                time.monotonic(), tutorial_hint=hint)
                        time.sleep(0.6)  # let the player see the result before advancing
                        return True
            elif key in (curses.KEY_BACKSPACE, 8, 127):
                if cursor:
                    command = command[:cursor - 1] + command[cursor:]
                    cursor -= 1
            elif key == curses.KEY_DC:
                command = command[:cursor] + command[cursor + 1:]
            elif key in (curses.KEY_LEFT,):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_RIGHT,):
                cursor = min(len(command), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(command)
            elif key == curses.KEY_UP:
                if history:
                    history_index = len(history) - 1 if history_index is None else max(0, history_index - 1)
                    command = history[history_index]
                    cursor = len(command)
            elif key == curses.KEY_DOWN:
                if history_index is not None:
                    history_index += 1
                    if history_index >= len(history):
                        history_index = None
                        command = ""
                    else:
                        command = history[history_index]
                    cursor = len(command)
            elif 32 <= key <= 126:
                character = chr(key)
                if character.isalpha() and not game.can_type_character(character):
                    continue
                command = command[:cursor] + character + command[cursor:]
                cursor += 1

        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 0.002))


def _prime_practice_state_for_step(game: Game, step: TutorialStep) -> None:
    if not step.demo_command:
        return
    parts = step.demo_command.split()
    action = get_cmd_action(parts[0].lower())
    system = parts[1].lower() if len(parts) > 1 else None
    if action == "repair" and system and system != "kernel":
        if game.my.get(system) == Status.OPERATIONAL:
            game.my.set(system, Status.DEGRADED)


def _run_ui(screen: "curses.window", game: Game) -> int:
    _init_colors()
    curses.curs_set(1)
    curses.noecho()
    curses.cbreak()
    screen.keypad(True)
    screen.nodelay(True)

    command = ""
    cursor = 0
    history: list = []
    history_index: Optional[int] = None
    escape_pending = False
    escape_deadline = 0.0
    next_frame = time.monotonic()
    while True:
        now = time.monotonic()
        if escape_pending and now >= escape_deadline:
            return 130
        if now >= next_frame:
            game.tick()
            _render(screen, game, command, cursor, history, now)
            next_frame = now + FRAME_INTERVAL_S

        key = _read_key(screen)
        if key is not None:
            if key == 3:  # Ctrl-C
                return 130
            if escape_pending:
                if key in (curses.KEY_BACKSPACE, 8, 127):
                    command, cursor = _delete_previous_word(command, cursor)
                    escape_pending = False
                    continue
                # A standalone Escape exits; do not accidentally process the
                # key that followed it as part of the command line.
                return 130
            if key == 27:
                # Alt+Backspace arrives as the two-byte escape sequence
                # ESC DEL. Wait briefly before treating ESC as quit.
                escape_pending = True
                escape_deadline = time.monotonic() + ESCAPE_SEQUENCE_TIMEOUT_S
                continue
            if key in (getattr(curses, "KEY_TAB", 9), 9):
                command, cursor = _autocomplete(command, cursor)
            if key in (curses.KEY_ENTER, 10, 13):
                submitted = command
                if submitted.strip():
                    history.append(submitted)
                    # Record every submitted command before handling it.  Some
                    # commands are rejected without any state-side command
                    # log (for example while the terminal is locked), but
                    # they still belong in the visible command history.
                    game.log.append("> " + submitted)
                    game.handle_command(submitted)
                    history_index = None
                command = ""
                cursor = 0
            elif key in (curses.KEY_BACKSPACE, 8, 127):
                if cursor:
                    command = command[:cursor - 1] + command[cursor:]
                    cursor -= 1
            elif key == curses.KEY_DC:
                command = command[:cursor] + command[cursor + 1:]
            elif key in (curses.KEY_LEFT,):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_RIGHT,):
                cursor = min(len(command), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(command)
            elif key == curses.KEY_UP:
                if history:
                    history_index = len(history) - 1 if history_index is None else max(0, history_index - 1)
                    command = history[history_index]
                    cursor = len(command)
            elif key == curses.KEY_DOWN:
                if history_index is not None:
                    history_index += 1
                    if history_index >= len(history):
                        history_index = None
                        command = ""
                    else:
                        command = history[history_index]
                    cursor = len(command)
            elif 32 <= key <= 126:
                character = chr(key)
                if character.isalpha() and not game.can_type_character(character):
                    continue
                command = command[:cursor] + character + command[cursor:]
                cursor += 1

        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 0.002))
        if game.phase == "game_over":
            # Leave the final frame visible until the player acknowledges it.
            _render(screen, game, command, cursor, history, time.monotonic())
            while True:
                key = _read_key(screen)
                if key in (3, 10, 13, 27, ord("q"), ord("Q")):
                    return 0
                time.sleep(0.02)


def run(
    interface: str,
    peer_mac: Optional[str] = None,
    connect_timeout_ms: int = 10000,
    tick_interval_s: float = FRAME_INTERVAL_S,
    tutorial: bool = False,
) -> int:
    """Connect and run the 60 FPS terminal dashboard."""
    config = _load_config()
    if tutorial or not config.get("seen_tutorial", False):
        steps = _load_tutorial_steps()
        curses.wrapper(_run_tutorial, steps)
        config["seen_tutorial"] = True
        _save_config(config)

    bridge = NetBridge(interface=interface, peer_mac=peer_mac)
    game = Game(bridge)

    connected = curses.wrapper(_run_connection_screen, game, interface, connect_timeout_ms)
    if not connected:
        print("! unable to connect to another player", flush=True)
        return 1

    print("Connected. Enter a command, or 'quit' to forfeit.", flush=True)
    return curses.wrapper(_run_ui, game)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.connect_timeout <= 0:
        print("! connect timeout must be positive", file=sys.stderr)
        return 2
    try:
        interface = args.interface or _auto_detect_interface()
        return run(
            interface=interface,
            peer_mac=args.peer_mac,
            connect_timeout_ms=args.connect_timeout,
            tutorial=args.tutorial,
        )
    except KeyboardInterrupt:
        print("\nExiting.", flush=True)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
