import argparse
import json
import socket
import time
from dataclasses import dataclass

from atomberg_local_discovery import discover_ip_by_mac, normalize_mac


@dataclass(frozen=True)
class DecodedState:
    value: int
    power: bool
    led: bool
    sleep: bool
    speed: int
    timer_hours: int
    timer_elapsed_mins: int
    brightness: int | None
    color: str | None


def _parse_int_auto(s: str) -> int:
    s = s.strip()
    try:
        return int(s, 10)
    except ValueError:
        return int(s, 16)


def decode_state_value(value: int) -> DecodedState:
    # Logic copied from Atomberg docs (OpenAPI description):
    # power: (0x10) & value > 0
    # led: (0x20) & value > 0
    # sleep: (0x80) & value > 0
    # speed: (0x07) & value
    # fanTimer: ((0x0F0000 & value) / 65536).round()
    # fanTimerElapsedMins: ((0xFF000000 & value) * 4 / 16777216).round()
    power = (value & 0x10) > 0
    led = (value & 0x20) > 0
    sleep = (value & 0x80) > 0
    speed = value & 0x07
    timer_hours = (value & 0x0F0000) >> 16
    timer_elapsed_mins = ((value & 0xFF000000) >> 24) * 4

    # Aris Starlight specific (may be present on other models too, harmless if ignored)
    brightness = (value & 0x7F00) >> 8
    cool = (value & 0x08) > 0
    warm = (value & 0x8000) > 0
    color: str | None
    if cool and warm:
        color = "daylight"
    elif cool:
        color = "cool"
    elif warm:
        color = "warm"
    else:
        color = None

    return DecodedState(
        value=value,
        power=power,
        led=led,
        sleep=sleep,
        speed=speed,
        timer_hours=timer_hours,
        timer_elapsed_mins=timer_elapsed_mins,
        brightness=brightness,
        color=color,
    )


def try_decode_udp_payload(data: bytes) -> dict | None:
    """
    The fan broadcasts state updates on UDP 5625 as a HEX-encoded ASCII JSON string.
    Example HEX:
      7b226465766963655f6964223a...7d
    After hex->bytes->ascii:
      {"device_id":"...","message_id":"...","state_string":"20,1,B,...,END"}
    """
    # First attempt: treat payload as hex-encoded ASCII
    try:
        s = data.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        s = data.decode(errors="replace").strip()

    def looks_like_hex(x: str) -> bool:
        if len(x) < 2 or len(x) % 2 != 0:
            return False
        for ch in x:
            if ch not in "0123456789abcdefABCDEF":
                return False
        return True

    candidates: list[str] = [s]
    # Some stacks may include nulls/newlines
    candidates.append(s.replace("\x00", "").strip())

    for cand in candidates:
        # 1) hex encoded JSON
        if looks_like_hex(cand):
            try:
                decoded = bytes.fromhex(cand).decode("utf-8", errors="strict")
                return json.loads(decoded)
            except Exception:
                pass
        # 2) plain JSON (fallback)
        if cand.startswith("{") and cand.endswith("}"):
            try:
                return json.loads(cand)
            except Exception:
                pass
    return None


def listen_state(seconds: float, device_id_filter: str | None) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 5625))
    sock.settimeout(0.5)

    deadline = time.time() + seconds
    latest: tuple[dict, DecodedState] | None = None

    print("Listening for Atomberg UDP state updates on 0.0.0.0:5625 ...")
    if device_id_filter:
        print(f"Filtering for device_id: {device_id_filter}")
    print("Tip: state updates are broadcast after the fan receives a command and changes state.")

    while time.time() < deadline:
        try:
            data, (ip, port) = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            continue

        obj = try_decode_udp_payload(data)
        if not isinstance(obj, dict):
            continue

        dev_id = str(obj.get("device_id", ""))
        if device_id_filter and dev_id.lower() != device_id_filter.lower():
            continue

        state_string = obj.get("state_string")
        if not isinstance(state_string, str) or not state_string:
            continue

        parts = [p.strip() for p in state_string.split(",")]
        if not parts:
            continue

        try:
            value = _parse_int_auto(parts[0])
        except Exception:
            continue

        decoded = decode_state_value(value)
        latest = (obj, decoded)

        print(f"\nFrom {ip}:{port} device_id={dev_id}")
        print(f"state_value={decoded.value} power={decoded.power} speed={decoded.speed} led={decoded.led} sleep={decoded.sleep}")
        print(f"timer_hours={decoded.timer_hours} timer_elapsed_mins={decoded.timer_elapsed_mins}")
        if decoded.color is not None:
            print(f"color={decoded.color}")
        if decoded.brightness is not None:
            print(f"brightness={decoded.brightness}")
        # show raw state_string briefly for debugging
        print(f"raw_state_string={state_string}")

    sock.close()

    if latest is None:
        print("\nNo state updates received.")
        return 2

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Listen for Atomberg fan state updates over UDP (port 5625).")
    parser.add_argument("--seconds", type=float, default=6.0, help="How long to listen for (seconds).")
    parser.add_argument("--device-id", default=None, help="Optional device_id to filter for.")
    parser.add_argument("--mac", default=None, help="Optional MAC (12 hex chars) to filter for (matches device_id).")
    parser.add_argument(
        "--poke-ip",
        default=None,
        help="Optional: send a command to this fan IP while listening (helps capture state).",
    )
    parser.add_argument("--poke-mac", default=None, help="Optional: poke by MAC (auto-discovers IP via beacons).")
    parser.add_argument("--poke-port", type=int, default=5600, help="UDP port for poke command (default: 5600)")
    parser.add_argument(
        "--discover-seconds",
        type=float,
        default=4.0,
        help="When using --poke-mac, how long to listen for beacons to resolve IP (seconds).",
    )
    poke_group = parser.add_mutually_exclusive_group()
    poke_group.add_argument("--poke-speed", type=int, help="Poke by (re)setting absolute speed (1..6).")
    poke_group.add_argument("--poke-power", choices=["on", "off", "true", "false"], help="Poke by setting power.")
    poke_group.add_argument("--poke-led", choices=["on", "off", "true", "false"], help="Poke by setting LED.")
    parser.add_argument("--poke-after", type=float, default=1.0, help="Seconds after start to send the poke command.")
    args = parser.parse_args()

    # If MAC is provided, treat it as device_id filter because local state payload uses device_id=mac (lowercase).
    device_id_filter = args.device_id
    if args.mac:
        device_id_filter = normalize_mac(args.mac).lower()

    # If requested, run the listener inline and send a poke command while listening.
    if not args.poke_ip and not args.poke_mac:
        return listen_state(seconds=args.seconds, device_id_filter=device_id_filter)

    if args.poke_speed is None and args.poke_power is None and args.poke_led is None:
        raise SystemExit("When using --poke-ip/--poke-mac, you must specify one of --poke-speed/--poke-power/--poke-led.")

    poke_ip = args.poke_ip
    if poke_ip is None and args.poke_mac is not None:
        poke_mac = normalize_mac(args.poke_mac)
        poke_ip = discover_ip_by_mac(poke_mac, seconds=args.discover_seconds)
        print(f"Discovered poke IP for {poke_mac}: {poke_ip}")

    # Start listening socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 5625))
    sock.settimeout(0.5)

    deadline = time.time() + args.seconds
    poke_at = time.time() + args.poke_after
    poked = False

    print("Listening for Atomberg UDP state updates on 0.0.0.0:5625 ...")
    if device_id_filter:
        print(f"Filtering for device_id: {device_id_filter}")
    print(f"Poke enabled: will send command to {poke_ip}:{args.poke_port} after {args.poke_after}s")

    while time.time() < deadline:
        now = time.time()
        if (not poked) and now >= poke_at:
            if args.poke_speed is not None:
                if not (1 <= args.poke_speed <= 6):
                    raise SystemExit("--poke-speed must be between 1 and 6.")
                cmd = {"speed": args.poke_speed}
            elif args.poke_power is not None:
                cmd = {"power": args.poke_power in ("on", "true")}
            else:
                cmd = {"led": args.poke_led in ("on", "true")}

            msg = json.dumps(cmd).encode("utf-8")
            poke_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            poke_sock.sendto(msg, (poke_ip, args.poke_port))
            poke_sock.close()
            print(f"Sent poke command: {cmd}")
            poked = True

        try:
            data, (ip, port) = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            continue

        obj = try_decode_udp_payload(data)
        if not isinstance(obj, dict):
            continue

        dev_id = str(obj.get("device_id", ""))
        if device_id_filter and dev_id.lower() != str(device_id_filter).lower():
            continue

        state_string = obj.get("state_string")
        if not isinstance(state_string, str) or not state_string:
            continue

        parts = [p.strip() for p in state_string.split(",")]
        if not parts:
            continue

        try:
            value = _parse_int_auto(parts[0])
        except Exception:
            continue

        decoded = decode_state_value(value)
        print(f"\nFrom {ip}:{port} device_id={dev_id}")
        print(f"state_value={decoded.value} power={decoded.power} speed={decoded.speed} led={decoded.led} sleep={decoded.sleep}")
        print(f"timer_hours={decoded.timer_hours} timer_elapsed_mins={decoded.timer_elapsed_mins}")
        if decoded.color is not None:
            print(f"color={decoded.color}")
        if decoded.brightness is not None:
            print(f"brightness={decoded.brightness}")
        print(f"raw_state_string={state_string}")
        sock.close()
        return 0

    sock.close()
    print("\nNo state updates received.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


