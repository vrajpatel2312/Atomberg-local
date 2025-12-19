import argparse
import socket
import time


def listen_for_beacons(target_mac: str | None, seconds: float) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Allow rebinding quickly
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 5625))
    sock.settimeout(0.5)

    deadline = time.time() + seconds
    seen: dict[str, tuple[str, float]] = {}

    print("Listening for Atomberg UDP beacons on 0.0.0.0:5625 ...")
    if target_mac:
        print(f"Filtering for MAC: {target_mac}")

    while time.time() < deadline:
        try:
            data, (ip, port) = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            continue

        content = data.decode(errors="replace").strip()
        if len(content) < 12:
            continue

        mac = content[:12].upper()
        now = time.time()
        seen[mac] = (ip, now)

        if (target_mac is None) or (mac == target_mac):
            series = content[12:].strip() or "(unknown series)"
            print(f"Beacon: mac={mac} ip={ip} src_port={port} series={series}")

    sock.close()

    if not seen:
        print("\nNo beacons received. If you're on the same Wi‑Fi, check firewall/UDP/broadcast.")
        return 2

    print("\nSummary (last seen):")
    for mac, (ip, ts) in sorted(seen.items()):
        age = time.time() - ts
        print(f"- {mac} -> {ip} (last seen {age:.1f}s ago)")

    if target_mac and target_mac not in seen:
        print(f"\nDid not see target MAC {target_mac}.")
        return 3

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Listen for Atomberg fan UDP beacons (port 5625).")
    parser.add_argument("--mac", help="MAC address (12 hex chars, no separators) to filter for.", default=None)
    parser.add_argument("--seconds", type=float, default=6.0, help="How long to listen for (seconds).")
    args = parser.parse_args()

    target_mac = args.mac.strip().upper() if args.mac else None
    if target_mac is not None:
        target_mac = target_mac.replace(":", "").replace("-", "").upper()
        if len(target_mac) != 12:
            raise SystemExit("MAC must be 12 hex chars (e.g. 10B41D181E58).")

    return listen_for_beacons(target_mac=target_mac, seconds=args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())


