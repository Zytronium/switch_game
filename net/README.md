# switch_net

Raw, non-IP/TCP Ethernet frame transport for the Switch Game, written in
Rust and exposed to Python as a native extension module via PyO3.

## Why raw frames

`switch_net` sends frames with a custom EtherType (`0x88B5`, one of the
IEEE 802 "Local Experimental Ethertype" values) directly on top of
Ethernet. There is no IP header and no TCP/UDP header at all, just an
Ethernet header followed by the game's own tiny sub-header and payload.

Because there's no IP header, there is nothing for a router to route on.
Frames only travel within a single Layer 2 broadcast domain, meaning two
machines can talk to each other only if they're on the same physical or
virtual network switch (or a direct cable). Put them on opposite sides of
a router, or try it over the internet, and it simply doesn't go anywhere.
That's the whole joke: the game needs an actual network switch in the
middle, not "any network."

## How it's built

- **Rust** (`switch_net` crate): owns the raw socket, frame construction,
  and frame parsing. Uses [`pnet`](https://docs.rs/pnet) for the
  datalink channel and Ethernet packet types, and
  [PyO3](https://pyo3.rs) to expose a Python module.
- **Python**: everything else (game state, timers, UI, input handling)
  calls into this module for anything that touches the wire.

## Requirements

- Rust 1.75+ and Cargo
- Python 3.14+
- [`maturin`](https://www.maturin.rs/) to build the extension:
  `pip install maturin`
- Linux. Raw `AF_PACKET` sockets are a Linux concept; `pnet`'s datalink
  layer also supports BSD/macOS via BPF, but this crate has only been
  built and tested on Linux.
- Root, or the `CAP_NET_RAW` capability, to open a raw socket. Either run
  your game with `sudo`, or grant the capability once to your Python
  interpreter (or a venv's interpreter) with:
  ```
  sudo setcap cap_net_raw+eip $(readlink -f $(which python3))
  ```
  Note that granting the capability to a system-wide `python3` gives
  *any* script run with it raw-socket access, so prefer doing this to a
  venv interpreter you control.

## Building

```
cd switch-net
maturin build --release
pip install target/wheels/switch_net-*.whl
```

or, for local development where you want the module usable in your
current environment without building a wheel each time:

```
maturin develop --release
```

## Frame format

```
Ethernet header (14 bytes, handled by pnet)
├─ destination MAC (6 bytes)
├─ source MAC      (6 bytes)
└─ ethertype       (2 bytes)   -> always 0x88B5 for switch_net frames

Switch Game sub-header + payload (this crate's format)
├─ msg_type      (1 byte)      -> game-defined meaning (e.g. attack, repair, ping)
├─ seq           (4 bytes, big-endian)  -> sequence number, caller-assigned
├─ payload_len   (2 bytes, big-endian)  -> length of payload that follows
└─ payload       (0-1400 bytes)
```

`msg_type` and `seq` are both left for the game layer to define. Nothing
in this crate interprets `msg_type`; it's just an integer 0-255 handed
back to you on `recv()`.

## API reference

### Module-level

- `switch_net.list_interfaces() -> list[tuple[str, str]]`
  Returns `(interface_name, mac_address)` for every network interface on
  the machine. `mac_address` is `""` for interfaces with no MAC (e.g.
  loopback). Use this to figure out what to pass as `interface` below.

- `switch_net.ETHERTYPE` (`int`)
  The EtherType this library sends and filters on (`0x88B5`).

- `switch_net.MAX_PAYLOAD_LEN` (`int`)
  Largest payload accepted by `send()` (1400 bytes, comfortably under
  the standard 1500-byte Ethernet MTU once the sub-header is added).

### `switch_net.SwitchSocket`

```python
sock = switch_net.SwitchSocket(interface, peer_mac=None, read_timeout_ms=5)
```

- `interface` (`str`): interface name, e.g. `"eth0"`.
- `peer_mac` (`str | None`): if given, frames are sent directly to this
  MAC address and `recv()` only accepts frames from it. If omitted
  (the default), frames are broadcast and `recv()` accepts frames from
  any sender. Broadcast is the simplest way to get two machines talking
  without knowing MAC addresses ahead of time; switch to a specific
  `peer_mac` once you've learned it (e.g. from the first frame you
  receive) if you want to ignore any other chatter on the switch.
- `read_timeout_ms` (`int`, default `5`): how long `recv()` blocks
  before returning `None` when nothing has arrived. The default is
  tuned for polling from a 60Hz game loop (a 16.6ms frame budget)
  without stalling it.

Opening a `SwitchSocket` raises `PermissionError`-style `OSError` (via
`IOError`/`OSError` from Python's side) if the process lacks permission
to open a raw socket, and `ValueError` if the interface name doesn't
exist or has no MAC address.

**Methods**

- `.send(msg_type: int, seq: int, payload: bytes) -> None`
  Send one frame with an explicit sequence number. Raises `ValueError`
  if `payload` exceeds `MAX_PAYLOAD_LEN`, or `OSError` if the send
  itself fails at the OS level.

- `.send_auto(msg_type: int, payload: bytes) -> int`
  Same as `send()`, but assigns and returns an auto-incrementing
  sequence number for you, starting at 0 for each `SwitchSocket`.

- `.recv() -> tuple[int, int, bytes, str] | None`
  Waits up to `read_timeout_ms` for the next frame addressed to us
  (matching EtherType, not our own MAC, and matching `peer_mac` if one
  was set). Returns `None` on timeout, otherwise
  `(msg_type, seq, payload, src_mac)`.

- `.recv_wait(timeout_ms: int) -> tuple[int, int, bytes, str] | None`
  Like `.recv()`, but keeps polling internally until either a frame
  arrives or `timeout_ms` total has elapsed, regardless of the socket's
  normal `read_timeout_ms`. Handy for a one-off longer wait, e.g.
  "wait up to 3 seconds for the other player's hello frame", without
  changing the per-frame timeout used everywhere else.

- `.local_mac() -> str`, `.peer_mac() -> str`, `.interface_name() -> str`
  Simple accessors for the socket's configuration.

## Example

```python
import switch_net

sock = switch_net.SwitchSocket("eth0")  # broadcast mode
print("my MAC is", sock.local_mac())

sock.send_auto(1, b"hello")

while True:
    result = sock.recv()
    if result is not None:
        msg_type, seq, payload, src_mac = result
        print(f"from {src_mac}: type={msg_type} seq={seq} payload={payload!r}")
        break
```

## Limitations / things the game layer needs to handle itself

- **No reliability guarantees.** Raw L2 has no acknowledgment, ordering,
  or retransmission. `seq` is provided so we can build whatever
  ordering/dedup/retry scheme fits the game (e.g. treat state-sync
  frames as "latest wins" and ignore anything with an older `seq`).
- **No fragmentation.** Payloads over `MAX_PAYLOAD_LEN` are rejected
  outright rather than split across frames.
- **No encryption or authentication.** Anything on the same switch can
  see or forge these frames. Fine for a local two-player party game;
  not something to rely on beyond that.
- **Single interface, single peer per socket.** Open one `SwitchSocket`
  per interface/peer combination you need.
