# Switch 'n Hack
A switch game for networking switches, not gaming consoles.

Switch 'n Hack is a two-player game where both players connect each other's computers with a network switch and Ethernet
cables, then use custom CLI commands to try to hack the other player (only simulated hacks, actual device security will
not be compromised). Each player may run commands to play offensive or defensive moves or to repair sabotage done by the
other player. The first player to fully compromise all the other player's systems, rendering their interface fully
unusable, wins. If the game lasts more than 15 minutes, the player with the fewest compromised and degraded systems 
wins (compromised systems are worth 3 points while degraded systems are worth 1).

Since this game operates below the TCP layer, there is no guaranteed delivery or error correction. This is a feature,
not a design flaw. While acknowledgements are required to prevent desync between each client, nothing is re-transmitted
except the Game Over event and acknowledgements. All other events, such as offensive moves, are by design not guaranteed 
to be delivered intact or at all. If an event fails to deliver properly, the sender will be notified of the failure and
the player will see that in their interface.

Since this game also operates below the IP layer, this game will only work on network switches, not over WiFi routers.
This is by design to enforce the "Switch game" pun. However, if you don't have a network switch or a router with a
built-in switch, an ethernet cable between the two computers also technically works, though it takes out the fun of it
being a "switch" game. If one of the players' computers doesn't have an Ethernet port, there are USB to Ethernet 
adaptors you can purchase. 

## Game Mechanics

### Systems

Each player has the following systems that can be targeted by attacks, defended, or repaired:

1. **Firewall** - Helps filter out incoming attacks. When compromised, player is vulnerable to all 
   attacks.
2. **Antivirus** - Alerts player to incoming attacks. When compromised, player doesn't see attack
   warnings.
3. **Routing Table** - Manages network paths. When compromised, the player's attacks may be misdirected.
4. **ARP Cache** - Maps MAC addresses. When compromised, causes occasional real packet corruption.
5. **Terminal Interface** - The player's command interface. When all systems above are compromised, 
   this becomes a valid target. If compromised, the player is unable to do anything for 30 seconds and 
   the opponent gains access to the player's kernel.
6. **Kernel** - The operating system. When all systems above are compromised, this becomes the final
   target. If this is compromised, you lose.

Each system (except Kernel) has three states:

- **Operational** - Fully functional
- **Degraded** - Partially compromised, operates at reduced effectiveness
- **Compromised** - Completely non-functional, provides no protection/functionality

The Kernel has only two states: Operational or Compromised (game over).

### Moves

Each player can make the following moves at any time:

1. **Attack a system** - If successful, reduces the opponent's system's status by one level. Cooldown of 2 seconds.
2. **Defend a system** - If antivirus is not compromised and the opponent tries to attack the specified system in the
   next 5 seconds, the success rate is lowered significantly. Cannot defend more than one system at a time.
3. **Repair a system** - Spends 5 seconds to increase a system's status by one level. You cannot run any other commands
   during this time period.
4. **Inspect system(s)** - Displays the opponent's status of all systems, optionally specifying a single system to
   inspect instead. Inspections bypass firewalls. Does not reveal honeypots.
5. **Setup a honeypot system** - Spends 10 seconds creating a new honeypot system masking itself as one of the legitimate
   systems. When the opponent goes to attack the honeypot, the attack brings down the honeypot but doesn't touch the
   real system. 1 minute cooldown. Only 1 honeypot can exist at a time.
6. **Forfeit the game** - Admits loss, disconnecting from the game and allowing the other player to directly attack the Kernel
   and win.

### Commands

| Commands                           | Parameters        | Description                                                                   |
|------------------------------------|-------------------|-------------------------------------------------------------------------------|
| `atk`, `attack`, `tar` or `target` | system            | Targets an attack on a specific system                                        |
| `def` or `defend`                  | system            | Focuses antivirus on a specific system for 5 seconds                          |
| `rep` or `repair`                  | system            | Repairs a system by increasing its status by one level                        |
| `ins` or `inspect`                 | system (optional) | Inspects the status of the opponent's system(s)                               |
| `pot` or `honeypot`                | system            | Sets up a honeypot system that masks itself as one of the legitimate systems. |
| `pot` or `honeypot`                | system            | Sets up a honeypot system that masks itself as one of the legitimate systems. |
| `?`, or `help`                     |                   | Displays a list of commands and what they do.                                 |

## Download and run a release

Releases are Linux executables built for one CPU architecture and glibc baseline. The release script builds in a Debian
12 container by default, giving the artifact a glibc 2.36 baseline that is compatible with distributions using glibc
2.36 or newer. Download the artifact whose name matches your machine, make it executable, and run it with the name of
a physical Ethernet interface:

```bash
chmod +x switch-n-hack-linux-x86_64-glibc-2.36
sudo ./switch-n-hack-linux-x86_64-glibc-2.36 eth0
```

The executable bundles Python, the Rust `switch_net` extension, and the tutorial. Players do not need Python, Rust,
Maturin, or any Python package installed. It is not a universal Linux binary: an artifact built on `x86_64` cannot run
on ARM, and an artifact built against a newer glibc may not run on an older distribution. Build a separate release on
each supported target.

### Network and privilege requirements

Both players must use computers connected to the same Layer-2 Ethernet switch (or directly by Ethernet cable). Wi-Fi,
routers, and internet connections are not supported. Find interface names with:

```bash
ip -br link
```

Choose the connected wired interface, such as `enp3s0` or `eth0`; do not choose `lo` or a Wi-Fi interface.

The game sends raw Ethernet frames, so it requires root or `CAP_NET_RAW`. The simplest option is `sudo` as shown above.
To run as your normal user instead, apply the deliberately scoped capability to the downloaded executable:

```bash
sudo setcap cap_net_raw+eip ./switch-n-hack-linux-x86_64-glibc-2.44
./switch-n-hack-linux-x86_64-glibc-2.44 enp3s0
```

Only grant this capability to a trusted executable and remove it when finished with `sudo setcap -r ./switch-n-hack-*`.
Using `sudo` can cause the first-run configuration marker to be written under the elevated user's home directory; the
capability approach preserves the normal user's `~/switch_n_hack/config.json`.

The first run shows the tutorial. It can also be selected explicitly with `--tutorial`; its completion marker is stored
in `~/switch_n_hack/config.json`. The peer can be selected explicitly when broadcast discovery is unsuitable:

```bash
./switch-n-hack-linux-x86_64-glibc-2.44 --peer-mac 02:00:00:00:00:02 --connect-timeout 15000 enp3s0
```

### Building a release

The portable build uses Docker or Podman and network access for the container and Python build tools on the first run:

```bash
./build-release.sh
```

The script creates a disposable Debian 12 build environment, compiles the Rust extension in release mode, freezes the
game with PyInstaller, and writes an architecture/ABI-labeled executable and `.sha256` checksum under `release/`.
Re-running it removes only its own temporary build directory, ensuring an old native extension cannot be selected. If
Docker and Podman are unavailable, the script warns and falls back to the current host automatically; that artifact is
only suitable for systems with a compatible glibc. To select this fallback explicitly, use
`LOCAL_BUILD=1 ./build-release.sh`.

### Troubleshooting

- `Permission denied`, raw-socket errors, or a datalink permission message: rerun with `sudo` or use the scoped
  `setcap` command above.
- An interface-not-found or invalid-interface error: rerun `ip -br link` and pass the connected Ethernet interface,
  not its description or MAC address.
- `! unable to connect to another player`: confirm both processes use the same switch, wired interfaces, and compatible
  release builds; wait for the peer or increase `--connect-timeout`.
- The terminal is too small or the UI is hard to read: enlarge the terminal before starting the game.
- The tutorial appears again: check that the process can write `~/switch_n_hack/config.json`; this is commonly caused by
  running once with `sudo` and once as the normal user.
