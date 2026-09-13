#!/usr/bin/env python3
"""
test_switch_net.py

A tiny interactive CLI for exercising the switch_net Rust extension by
hand. Run this on two machines connected through the same network
switch (or a direct Ethernet cable) and type messages back and forth.

Usage:
    sudo python3 test_switch_net.py --list
    sudo python3 test_switch_net.py --iface eth0
    sudo python3 test_switch_net.py --iface eth0 --peer aa:bb:cc:dd:ee:ff

Needs root (or CAP_NET_RAW) to open the raw socket. See README.md for
the setcap alternative to running as root.

Commands once running:
    just type a line + Enter  -> sent as a CHAT frame
    /ping                     -> sends a PING frame
    /quit                     -> exit
"""

import argparse
import sys
import threading
import time

import switch_net

# -------- msg types --------

MSG_CHAT = 1
MSG_PING = 2
MSG_PONG = 3

MSG_NAMES = {
    MSG_CHAT: "CHAT",
    MSG_PING: "PING",
    MSG_PONG: "PONG",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Interactive CLI tester for switch_net.")
    parser.add_argument("--list", action="store_true", help="list network interfaces and exit")
    parser.add_argument("--iface", help="interface to use, e.g. eth0")
    parser.add_argument("--peer", default=None, help="peer MAC address (default: broadcast)")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=100,
        help="recv() poll timeout in ms for the background listener thread (default: 100)",
    )
    return parser.parse_args()


def list_interfaces():
    print("Available interfaces:")
    for name, mac in switch_net.list_interfaces():
        mac_display = mac if mac else "(no MAC)"
        print(f"  {name:<12} {mac_display}")


def listen_loop(sock, stop_event):
    """
    Background thread: continuously poll for incoming frames and print
    them. Runs until stop_event is set.
    """
    while not stop_event.is_set():
        try:
            result = sock.recv()
        except OSError as e:
            print(f"\n[recv error: {e}]")
            break

        if result is None:
            continue

        msg_type, seq, payload, src_mac = result
        label = MSG_NAMES.get(msg_type, f"type={msg_type}")

        if msg_type == MSG_CHAT:
            text = payload.decode("utf-8", errors="replace")
            print(f"\n[{src_mac}] {text}\n> ", end="", flush=True)
        elif msg_type == MSG_PING:
            print(f"\n[{src_mac}] PING (seq {seq}), replying PONG\n> ", end="", flush=True)
            try:
                sock.send_auto(MSG_PONG, b"")
            except OSError as e:
                print(f"\n[send error replying to PING: {e}]")
        elif msg_type == MSG_PONG:
            print(f"\n[{src_mac}] PONG (seq {seq})\n> ", end="", flush=True)
        else:
            print(f"\n[{src_mac}] {label} seq={seq} payload={payload!r}\n> ", end="", flush=True)


def main():
    args = parse_args()

    if args.list or not args.iface:
        list_interfaces()
        if not args.iface:
            print("\nPass --iface <name> to start the tester.")
        return

    try:
        sock = switch_net.SwitchSocket(args.iface, peer_mac=args.peer, read_timeout_ms=args.timeout_ms)
    except (OSError, ValueError) as e:
        print(f"Failed to open interface '{args.iface}': {e}")
        print("Hint: raw sockets usually need root. Try running with sudo.")
        sys.exit(1)

    print(f"Listening on {sock.interface_name()} as {sock.local_mac()}")
    print(f"Sending to peer: {sock.peer_mac()}")
    print("Type a message and press Enter to send it. /ping to ping, /quit to exit.\n")

    stop_event = threading.Event()
    listener = threading.Thread(target=listen_loop, args=(sock, stop_event), daemon=True)
    listener.start()

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break

            line = line.strip()
            if not line:
                continue
            if line == "/quit":
                break
            elif line == "/ping":
                seq = sock.send_auto(MSG_PING, b"")
                print(f"sent PING (seq {seq})")
            else:
                seq = sock.send_auto(MSG_CHAT, line.encode("utf-8"))
                print(f"sent CHAT (seq {seq}): {line}")
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        print("\nExiting.")
        time.sleep(0.05)  # give the listener thread a moment to notice stop_event


if __name__ == "__main__":
    main()
