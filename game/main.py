"""Terminal UI and entry point for Switch 'n Hack."""

import argparse
import curses
import sys
import time
import textwrap
from typing import Optional, Sequence

try:  # Package execution: ``python -m game.main``.
    from .net_bridge import NetBridge
    from .state import Game
except ImportError:  # Direct execution with ``game`` on PYTHONPATH.
    from net_bridge import NetBridge
    from state import Game


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Play Switch 'n Hack over Ethernet.")
    parser.add_argument(
        "interface",
        help="Ethernet interface to use, for example eth0",
    )
    parser.add_argument(
        "--peer-mac",
        help="optional peer MAC address; otherwise discover a peer by broadcast",
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=10000,
        metavar="MILLISECONDS",
        help="how long to wait for the other player (default: 10000)",
    )
    return parser


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
            cursor: int, command_history: Sequence[str], now: float) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    if height < 18 or width < 72:
        _safe_add(screen, 0, 0, "Terminal too small; resize to at least 72x18.", width,
                  curses.A_BOLD)
        screen.refresh()
        return

    warning = "[WARNING] INCOMING ATTACK DETECTED!" if game.warning_until > now else ""
    title = "SWITCH 'n HACK v1.0"
    _safe_add(screen, 0, 0, "╔" + "═" * (width - 2) + "╗", width)
    _safe_add(screen, 1, 0, "║", width)
    _safe_add(screen, 1, 2, warning, width - 2, curses.A_BOLD | curses.A_BLINK)
    _safe_add(screen, 1, max(2, width - len(title) - 3), title, width - 1, curses.A_BOLD)
    _safe_add(screen, 1, width - 1, "║", width)
    _safe_add(screen, 2, 0, "╠" + "═" * (width - 2) + "╣", width)

    left_width = max(30, width // 2)
    divider = min(width - 2, left_width)
    for row in range(3, height - 2):
        _safe_add(screen, row, 0, "║", width)
        _safe_add(screen, row, divider, "║", width)
        _safe_add(screen, row, width - 1, "║", width)

    _safe_add(screen, 4, divider + 2, "SYSTEM STATUS", width - divider - 2, curses.A_BOLD)
    systems = ("firewall", "antivirus", "routing_table", "arp_cache", "terminal", "kernel")
    for index, system in enumerate(systems):
        row = 6 + index * 2
        status = ("compromised" if game.my.kernel_compromised else "operational") \
            if system == "kernel" else game.my.get(system)
        label = SYSTEM_LABELS[system]
        _safe_add(screen, row, divider + 2, f"{label:<14} {_status_bar(status)}",
                  width - divider - 3)

    hp_row = 19
    if hp_row < height - 3:
        _safe_add(screen, hp_row, divider + 2, "HONEYPOT STATUS", width - divider - 2, curses.A_BOLD)
        if game.busy_action == "honeypot":
            hp_text = f"[SETTING UP] {game.busy_target or ''} ({_remaining(game.busy_until, now)})"
        elif game.honeypot_system:
            hp_text = f"[ACTIVE] Masking: {SYSTEM_LABELS.get(game.honeypot_system, game.honeypot_system)}"
        else:
            hp_text = "[INACTIVE]"
        _safe_add(screen, hp_row + 1, divider + 2, hp_text, width - divider - 3)

    cooldown_row = min(height - 5, hp_row + 4)
    _safe_add(screen, cooldown_row, divider + 2, "COOLDOWNS", width - divider - 2, curses.A_BOLD)
    repair = _remaining(game.busy_until, now) if game.busy_action == "repair" else "READY"
    honeypot = _remaining(game.honeypot_cooldown_until, now)
    _safe_add(screen, cooldown_row + 1, divider + 2,
              f"Attack: {_remaining(game.attack_cooldown_until, now):<7} Repair: {repair:<7}",
              width - divider - 3)
    _safe_add(screen, cooldown_row + 2, divider + 2, f"Honeypot: {honeypot}", width - divider - 3)

    log_top = 4
    log_bottom = height - 4
    visible = _wrap_log(game.log, divider - 3, log_bottom - log_top)
    for row, line in enumerate(visible, log_top):
        _safe_add(screen, row, 2, line, divider - 3)

    _safe_add(screen, height - 3, 0, "╠" + "═" * (width - 2) + "╣", width)
    prompt = "> " + command
    _safe_add(screen, height - 2, 0, "║", width)
    _safe_add(screen, height - 2, 2, prompt, width - 3)
    _safe_add(screen, height - 2, width - 1, "║", width)
    _safe_add(screen, height - 1, 0, "╚" + "═" * (width - 2) + "╝", width)

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


def _run_ui(screen: "curses.window", game: Game) -> int:
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
) -> int:
    """Connect and run the 60 FPS terminal dashboard."""
    bridge = NetBridge(interface=interface, peer_mac=peer_mac)
    game = Game(bridge)

    print(f"Connecting on {interface}...", flush=True)
    if not game.connect(timeout_ms=connect_timeout_ms):
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
        return run(
            interface=args.interface,
            peer_mac=args.peer_mac,
            connect_timeout_ms=args.connect_timeout,
        )
    except KeyboardInterrupt:
        print("\nExiting.", flush=True)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
