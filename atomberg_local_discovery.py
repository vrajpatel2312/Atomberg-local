import socket
import time


def normalize_mac(mac: str) -> str:
    mac = mac.strip().upper().replace(":", "").replace("-", "")
    if len(mac) != 12:
        raise ValueError("MAC must be 12 hex chars (e.g. 10B41D181E58).")
    return mac


def discover_ip_by_mac(mac: str, *, seconds: float = 4.0, port: int = 5625) -> str:
    """
    Listen for Atomberg UDP beacons on port 5625 and return the sender IP for the given MAC.
    Beacon format: first 12 chars are MAC (no separators), followed by series.
    """
    target_mac = normalize_mac(mac)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)

    deadline = time.time() + seconds
    last_ip: str | None = None

    while time.time() < deadline:
        try:
            data, (ip, _src_port) = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            continue

        content = data.decode(errors="replace").strip()
        if len(content) < 12:
            continue
        seen_mac = content[:12].upper()
        if seen_mac == target_mac:
            last_ip = ip
            break

    sock.close()

    if not last_ip:
        raise RuntimeError(
            f"Did not receive a beacon for MAC {target_mac} on UDP {port} within {seconds}s. "
            "Check same LAN/Wi‑Fi and firewall allowing UDP 5625."
        )

    return last_ip


