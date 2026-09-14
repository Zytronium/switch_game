"""Basic command-line entry point for Switch 'n Hack.

This is intentionally a small line-oriented interface.  The richer terminal
UI described in the README can be built on top of :class:`state.Game` later.
"""

import argparse
import select
import sys
from typing import Optional, Sequence

try:  # Package execution: ``python -m game.main``.
    from .net_bridge import NetBridge
    from .state import Game
except ImportError:  # Direct execution with ``game`` on PYTHONPATH.
    from net_bridge import NetBridge
    from state import Game


DEFAULT_TICK_INTERVAL_S = 0.01


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


def _print_new_logs(game: Game, printed_count: int) -> int:
    for line in game.log[printed_count:]:
        print(line, flush=True)
    return len(game.log)


def run(
    interface: str,
    peer_mac: Optional[str] = None,
    connect_timeout_ms: int = 10000,
    tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
) -> int:
    """Connect and run the basic command loop.

    Returns a process-style status code.  ``Game`` remains responsible for
    all command validation, timers, networking events, and game rules.
    """
    bridge = NetBridge(interface=interface, peer_mac=peer_mac)
    game = Game(bridge)

    print(f"Connecting on {interface}...", flush=True)
    if not game.connect(timeout_ms=connect_timeout_ms):
        print("! unable to connect to another player", flush=True)
        return 1

    print("Connected. Enter a command, or 'quit' to forfeit.", flush=True)
    printed_count = 0
    while game.phase != "game_over":
        game.tick()
        printed_count = _print_new_logs(game, printed_count)

        ready, _, _ = select.select([sys.stdin], [], [], tick_interval_s)
        if not ready:
            continue
        line = sys.stdin.readline()
        if line == "":  # End-of-file is not an implicit forfeit.
            print("Input closed; exiting.", flush=True)
            return 0
        game.handle_command(line)

    game.tick()
    _print_new_logs(game, printed_count)
    return 0


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
