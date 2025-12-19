import argparse
import json
import socket

from atomberg_local_discovery import discover_ip_by_mac, normalize_mac


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a local UDP JSON command to an Atomberg fan (port 5600).")
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--ip", help="Fan IP address (e.g. 192.168.0.13)")
    target_group.add_argument("--mac", help="Fan MAC address (12 hex chars, no separators)")
    parser.add_argument(
        "--discover-seconds",
        type=float,
        default=4.0,
        help="When using --mac, how long to listen for beacons to resolve IP (seconds).",
    )
    cmd_group = parser.add_mutually_exclusive_group(required=True)
    cmd_group.add_argument(
        "--json",
        dest="json_str",
        help='Command JSON, e.g. \'{"power":true}\' or \'{"speed":3}\'',
    )
    cmd_group.add_argument("--power", choices=["on", "off", "true", "false"], help="Turn fan power on/off.")
    cmd_group.add_argument("--speed", type=int, help="Set absolute speed (1..6).")
    cmd_group.add_argument("--speed-delta", type=int, help="Change speed relative (-5..5 excluding 0).")
    cmd_group.add_argument("--led", choices=["on", "off", "true", "false"], help="Turn fan light on/off.")
    cmd_group.add_argument("--timer", type=int, help="Set timer hours (0..4). 0 turns off timer.")
    parser.add_argument("--port", type=int, default=5600, help="Target UDP port (default: 5600)")
    parser.add_argument("--dry-run", action="store_true", help="Print parsed command but do not send")
    args = parser.parse_args()

    if args.mac is not None:
        mac = normalize_mac(args.mac)
        ip = discover_ip_by_mac(mac, seconds=args.discover_seconds)
        print(f"Discovered IP for {mac}: {ip}")
    else:
        ip = args.ip

    cmd: dict[str, object]
    if args.json_str is not None:
        try:
            cmd = json.loads(args.json_str)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Invalid JSON for --json: {e}") from e

        if not isinstance(cmd, dict):
            raise SystemExit("--json must decode to a JSON object (e.g. {\"power\":true})")
    elif args.power is not None:
        cmd = {"power": args.power in ("on", "true")}
    elif args.speed is not None:
        if not (1 <= args.speed <= 6):
            raise SystemExit("--speed must be between 1 and 6.")
        cmd = {"speed": args.speed}
    elif args.speed_delta is not None:
        if args.speed_delta == 0 or not (-5 <= args.speed_delta <= 5):
            raise SystemExit("--speed-delta must be between -5 and 5 and not 0.")
        cmd = {"speedDelta": args.speed_delta}
    elif args.led is not None:
        cmd = {"led": args.led in ("on", "true")}
    elif args.timer is not None:
        if not (0 <= args.timer <= 4):
            raise SystemExit("--timer must be between 0 and 4.")
        cmd = {"timer": args.timer}
    else:
        raise SystemExit("No command specified.")

    print(f"Target: {ip}:{args.port}")
    print(f"Command: {json.dumps(cmd, separators=(',', ':'))}")

    if args.dry_run:
        print("Dry run: not sending.")
        return 0

    msg = json.dumps(cmd).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(msg, (ip, args.port))
    sock.close()
    print("Sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


