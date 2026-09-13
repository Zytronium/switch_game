# Switch 'n Hack
A switch game for networking switches, not gaming consoles.

Switch 'n Hack is a two-player game where both players connect each other's computers with a network switch and Ethernet
cables, then use custom CLI commands to try to hack the other player (only simulated hacks, actual device security will
not be compromised). Each player may run commands to play offensive or defensive moves or to repair sabotage done by the
other player. The first player to fully compromise all the other player's systems, rendering their interface fully
unusable, wins. If the game lasts more than 10 minutes, the player with the fewest compromised systems wins.

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

1. **Attack a system** - If successful, reduces the opponent's system's status by one level. Cooldown of 1 second.
2. **Defend a system** - If antivirus is not compromised and the opponent tries to attack the specified system in the
   next 5 seconds, the success rate is lowered significantly. Cannot defend more than one system at a time.
3. **Repair a system** - Spends 10 seconds to increase a system's status by one level. You cannot run any other commands
   during this time period.
4. **Inspect system(s)** - Displays the opponent's status of all systems, optionally specifying a single system to
   inspect instead. Not invulnerable to firewalls. Does not reveal honeypots.
5. **Setup a honeypot system** - Spends 10 seconds creating a new honeypot system masking itself as one of the legitimate
   systems. When the opponent goes to attack the honeypot, the attack brings down the honeypot but doesn't touch the
   real system. 1 minute cooldown. Only 1 honeypot can exist at a time.
6. **Forfeit the game** - Admits loss, disconnecting from the game and allowing the other player to directly attack the Kernel
   and win.

### Commands

| Commands                    | Parameters        | Description                                                                   |
|-----------------------------|-------------------|-------------------------------------------------------------------------------|
| `tar` or `target`           | system            | Targets an attack on a specific system                                        |
| `def` or `defend`           | system            | Focuses antivirus on a specific system for 5 seconds                          |
| `rep` or `repair`           | system            | Repairs a system by increasing its status by one level                        |
| `ins` or `inspect`          | system (optional) | Inspects the status of the opponent's system(s)                               |
| `pot` or `honeypot`         | system            | Sets up a honeypot system that masks itself as one of the legitimate systems. |
| `quit`, `exit` or `forfeit` |                   | Forfeits the game after confirming player is sure they want to forfeit.       |