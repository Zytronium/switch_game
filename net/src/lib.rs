//! switch_net
//!
//! Raw, non-IP/TCP Ethernet frame transport for the "Switch Game".
//!
//! Frames are sent with a custom EtherType (0x88B5, one of the IEEE 802
//! "Local Experimental Ethertype" values) directly on top of Ethernet, with
//! no IP or TCP header at all. That means two machines can only talk to
//! each other if they sit on the same Layer 2 broadcast domain, i.e.
//! connected through a plain network switch (or a direct cable), not
//! through a router or the internet.
//!
//! This crate is exposed to Python via PyO3 as the `switch_net` module.

use std::str::FromStr;
use std::sync::Mutex;
use std::time::Duration;

use pnet::datalink::{self, Channel, Config, DataLinkReceiver, DataLinkSender, MacAddr, NetworkInterface};
use pnet::packet::Packet;
use pyo3::exceptions::{PyIOError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

// -------- constants --------

/// Custom EtherType used for all Switch Game frames.
/// 0x88B5 is reserved by IEEE 802 for local experimentation and is not
/// used by any standard protocol, so it will never collide with real
/// network traffic (ARP, IPv4, IPv6, etc).
const SWITCH_GAME_ETHERTYPE: u16 = 0x88B5;

/// Size in bytes of our sub-header inside the Ethernet payload:
/// msg_type (1) + sequence (4) + payload_len (2)
const HEADER_LEN: usize = 7;

/// Largest payload we'll accept/send in a single frame. Kept well under
/// the standard Ethernet MTU (1500) so frames never need fragmentation.
const MAX_PAYLOAD_LEN: usize = 1400;

// -------- helpers --------

fn parse_mac(s: &str) -> PyResult<MacAddr> {
    MacAddr::from_str(s).map_err(|_| {
        PyValueError::new_err(format!(
            "'{s}' is not a valid MAC address (expected format aa:bb:cc:dd:ee:ff)"
        ))
    })
}

fn find_interface(name: &str) -> PyResult<NetworkInterface> {
    datalink::interfaces()
        .into_iter()
        .find(|iface| iface.name == name)
        .ok_or_else(|| {
            PyValueError::new_err(format!(
                "no such network interface: '{name}' (see switch_net.list_interfaces())"
            ))
        })
}

// -------- public functions --------

/// List available network interfaces as (name, mac_address) tuples.
/// Interfaces with no MAC address (e.g. loopback on some platforms) are
/// still listed, with mac_address as an empty string.
#[pyfunction]
fn list_interfaces() -> Vec<(String, String)> {
    datalink::interfaces()
        .into_iter()
        .map(|iface| {
            let mac = iface
                .mac
                .map(|m| m.to_string())
                .unwrap_or_default();
            (iface.name, mac)
        })
        .collect()
}

// -------- SwitchSocket --------

/// A socket-like handle that sends and receives raw Switch Game frames
/// on a single network interface.
#[pyclass]
struct SwitchSocket {
    // -------- wire handles --------
    // Mutex-wrapped so the struct as a whole is Sync, which pyo3 requires
    // of pyclasses. The GIL already serializes calls from Python, so this
    // is just satisfying the type system, not guarding real contention.
    tx: Mutex<Box<dyn DataLinkSender>>,
    rx: Mutex<Box<dyn DataLinkReceiver>>,
    local_mac: MacAddr,
    peer_mac: MacAddr,
    interface_name: String,
    next_send_seq: u32,
}

#[pymethods]
impl SwitchSocket {
    /// Open a raw frame socket on `interface`.
    ///
    /// `peer_mac`: if given, only frames from this MAC address are
    /// returned by recv(); frames are also sent directly to this address.
    /// If omitted, frames are broadcast (ff:ff:ff:ff:ff:ff) and recv()
    /// accepts frames from any sender. Broadcast is the easy default for
    /// a two-player LAN game; a specific peer_mac is useful once you know
    /// who you're playing against.
    ///
    /// `read_timeout_ms`: how long recv() blocks waiting for a frame
    /// before returning None. Defaults to 5ms, short enough to poll from
    /// a 60Hz game loop without stalling a frame.
    ///
    /// Opening a raw socket normally requires root or CAP_NET_RAW.
    #[new]
    #[pyo3(signature = (interface, peer_mac=None, read_timeout_ms=5))]
    fn new(interface: &str, peer_mac: Option<&str>, read_timeout_ms: u64) -> PyResult<Self> {
        let iface = find_interface(interface)?;
        let local_mac = iface.mac.ok_or_else(|| {
            PyRuntimeError::new_err(format!("interface '{interface}' has no MAC address"))
        })?;

        let peer_mac = match peer_mac {
            Some(s) => parse_mac(s)?,
            None => MacAddr::broadcast(),
        };

        let config = Config {
            read_timeout: Some(Duration::from_millis(read_timeout_ms)),
            ..Config::default()
        };

        let channel = datalink::channel(&iface, config)
            .map_err(|e| PyIOError::new_err(format!("failed to open interface '{interface}': {e}")))?;

        let (tx, rx) = match channel {
            Channel::Ethernet(tx, rx) => (tx, rx),
            _ => {
                return Err(PyRuntimeError::new_err(
                    "unsupported channel type for this interface (expected Ethernet)",
                ))
            }
        };

        Ok(SwitchSocket {
            tx: Mutex::new(tx),
            rx: Mutex::new(rx),
            local_mac,
            peer_mac,
            interface_name: interface.to_string(),
            next_send_seq: 0,
        })
    }

    /// This machine's MAC address on the chosen interface, as a string.
    fn local_mac(&self) -> String {
        self.local_mac.to_string()
    }

    /// The peer MAC address frames are sent to (broadcast unless a
    /// specific peer_mac was given at construction).
    fn peer_mac(&self) -> String {
        self.peer_mac.to_string()
    }

    /// The interface name this socket is bound to.
    fn interface_name(&self) -> String {
        self.interface_name.clone()
    }

    /// Send one frame. `msg_type` is a small integer (0-255) the game
    /// defines the meaning of (e.g. attack, repair, heartbeat, sync).
    /// `seq` is the sequence number for this message; pass whatever
    /// scheme your game logic wants, or use next_seq() for a simple
    /// auto-incrementing counter.
    fn send(&mut self, msg_type: u8, seq: u32, payload: &[u8]) -> PyResult<()> {
        if payload.len() > MAX_PAYLOAD_LEN {
            return Err(PyValueError::new_err(format!(
                "payload too large: {} bytes (max {MAX_PAYLOAD_LEN})",
                payload.len()
            )));
        }

        let mut inner = Vec::with_capacity(HEADER_LEN + payload.len());
        inner.push(msg_type);
        inner.extend_from_slice(&seq.to_be_bytes());
        inner.extend_from_slice(&(payload.len() as u16).to_be_bytes());
        inner.extend_from_slice(payload);

        let eth_len = pnet::packet::ethernet::EthernetPacket::minimum_packet_size() + inner.len();
        let mut buf = vec![0u8; eth_len];

        let mut frame = pnet::packet::ethernet::MutableEthernetPacket::new(&mut buf)
            .ok_or_else(|| PyRuntimeError::new_err("failed to build ethernet frame buffer"))?;
        frame.set_destination(self.peer_mac);
        frame.set_source(self.local_mac);
        frame.set_ethertype(pnet::packet::ethernet::EtherType(SWITCH_GAME_ETHERTYPE));
        frame.set_payload(&inner);

        let mut tx = self.tx.lock().map_err(|_| PyRuntimeError::new_err("tx lock poisoned"))?;
        match tx.send_to(frame.packet(), None) {
            Some(Ok(())) => Ok(()),
            Some(Err(e)) => Err(PyIOError::new_err(format!("send failed: {e}"))),
            None => Err(PyIOError::new_err(
                "send failed: no packet was sent (interface may be down)",
            )),
        }
    }

    /// Convenience wrapper around send() that auto-increments the
    /// sequence number for you. Returns the sequence number used.
    fn send_auto(&mut self, msg_type: u8, payload: &[u8]) -> PyResult<u32> {
        let seq = self.next_send_seq;
        self.next_send_seq = self.next_send_seq.wrapping_add(1);
        self.send(msg_type, seq, payload)?;
        Ok(seq)
    }

    /// Wait up to `read_timeout_ms` (set at construction) for the next
    /// Switch Game frame addressed to us. Returns None on timeout, or on
    /// this platform's send-loopback of our own broadcast frame.
    ///
    /// On success returns (msg_type, seq, payload, src_mac).
    fn recv(&mut self, py: Python<'_>) -> PyResult<Option<(u8, u32, Py<PyBytes>, String)>> {
        let mut rx = self.rx.lock().map_err(|_| PyRuntimeError::new_err("rx lock poisoned"))?;
        loop {
            let packet_bytes = match rx.next() {
                Ok(bytes) => bytes,
                Err(e) => {
                    // A read timeout surfaces as a WouldBlock / TimedOut
                    // io::Error on every platform pnet supports; treat it
                    // as "nothing arrived yet" rather than a hard error.
                    return match e.kind() {
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => Ok(None),
                        _ => Err(PyIOError::new_err(format!("recv failed: {e}"))),
                    };
                }
            };

            let eth = match pnet::packet::ethernet::EthernetPacket::new(packet_bytes) {
                Some(p) => p,
                None => continue, // truncated/malformed, skip it
            };

            if eth.get_ethertype().0 != SWITCH_GAME_ETHERTYPE {
                continue; // not one of ours, ignore
            }

            let src = eth.get_source();
            if src == self.local_mac {
                continue; // our own broadcast frame looped back, ignore
            }
            if self.peer_mac != MacAddr::broadcast() && src != self.peer_mac {
                continue; // from someone other than our chosen peer, ignore
            }

            let inner = eth.payload();
            if inner.len() < HEADER_LEN {
                continue; // too short to contain our sub-header, skip it
            }

            let msg_type = inner[0];
            let seq = u32::from_be_bytes(inner[1..5].try_into().unwrap());
            let payload_len = u16::from_be_bytes(inner[5..7].try_into().unwrap()) as usize;

            if inner.len() < HEADER_LEN + payload_len {
                continue; // declared length doesn't fit what we received
            }
            let payload = &inner[HEADER_LEN..HEADER_LEN + payload_len];

            return Ok(Some((
                msg_type,
                seq,
                PyBytes::new(py, payload).into(),
                src.to_string(),
            )));
        }
    }

    /// Block until a frame arrives or `timeout_ms` total time has
    /// elapsed, polling recv() internally. Useful when you want a
    /// one-shot longer wait (e.g. "wait up to 3 seconds for the other
    /// player to connect") without changing the socket's normal short
    /// per-frame read_timeout_ms.
    fn recv_wait(
        &mut self,
        py: Python<'_>,
        timeout_ms: u64,
    ) -> PyResult<Option<(u8, u32, Py<PyBytes>, String)>> {
        let start = std::time::Instant::now();
        loop {
            if let Some(result) = self.recv(py)? {
                return Ok(Some(result));
            }
            if start.elapsed() >= Duration::from_millis(timeout_ms) {
                return Ok(None);
            }
        }
    }
}

// -------- module definition --------

#[pymodule]
fn switch_net(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<SwitchSocket>()?;
    m.add_function(wrap_pyfunction!(list_interfaces, m)?)?;
    m.add("ETHERTYPE", SWITCH_GAME_ETHERTYPE)?;
    m.add("MAX_PAYLOAD_LEN", MAX_PAYLOAD_LEN)?;
    Ok(())
}
