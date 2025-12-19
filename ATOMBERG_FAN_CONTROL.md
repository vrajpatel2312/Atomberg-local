# Atomberg Fan (Local LAN + Cloud) Control Guide

This repo includes small scripts to:
- **Identify** Atomberg fans on your network (MAC → IP)
- **Read current state** (local UDP broadcast and/or cloud API)
- **Control speed/power/light** (local UDP and/or cloud API)

This guide is based on Atomberg’s official developer OpenAPI spec (`https://developer.atomberg-iot.com/openapi.yaml`) and overview docs (`https://developer.atomberg-iot.com/#overview`).

---

## Requirements

### Network requirements (local LAN mode)
- Your PC and the fan must be on the **same LAN/Wi‑Fi** (same subnet is strongly recommended).
- **UDP** must be allowed on your network and host firewall.
- The fan uses these ports:
  - **UDP 5625**: fan broadcasts **beacons** (every ~1s) and **state updates**
  - **UDP 5600**: fan receives **control commands** (JSON payloads)

### Machine requirements
- **Python 3** installed and runnable via `python`.
- Windows firewall must allow inbound UDP on **5625** (for discovery/state receive).

---

## Identify a fan on the network (MAC → IP)

Atomberg fans broadcast a **beacon packet** on **UDP port 5625** every ~1 second.  
The beacon includes the **MAC address** (first 12 characters, no separators), followed by the series.

### Step 1 — Listen for beacons

From the repo root:

```powershell
python .\atomberg_listen_beacons.py --seconds 6
```

To filter for one device MAC:

```powershell
python .\atomberg_listen_beacons.py --mac 10B41D181E58 --seconds 6
```

### Output you care about
- The script prints a line like:
  - `Beacon: mac=10B41D181E58 ip=192.168.0.13 ...`
- That gives you the **current IP** to use for local commands.

---

## Push commands locally (set speed / power / LED)

Local control is via **UDP JSON** to the fan’s **IP on port 5600**.

### Multi-fan tip: always target by MAC (recommended)
If you have multiple fans, **MAC is the stable identifier** and IP can change (DHCP).

The scripts in this repo support:
- Resolving **MAC → IP** by listening to the fan’s UDP beacons on **5625**
- Then sending commands to the resolved IP automatically

### Supported speed range (important)
Atomberg’s documented “Speed Absolute” accepted values are **1..6** (not 0).  
If you need “speed 0”, treat it as **power off** (`{"power": false}`).

### Step 2 — Send speed (1..6)

```powershell
python .\atomberg_send_command.py --ip 192.168.0.13 --speed 3
```

Same command, but target by MAC (recommended for multiple fans):

```powershell
python .\atomberg_send_command.py --mac 10B41D181E58 --speed 3
```

### Other common local commands

```powershell
# Power ON
python .\atomberg_send_command.py --ip 192.168.0.13 --power on

# Power OFF (equivalent to "speed 0")
python .\atomberg_send_command.py --ip 192.168.0.13 --power off

# LED (light) OFF
python .\atomberg_send_command.py --ip 192.168.0.13 --led off

# Speed delta (+1/-1/etc)
python .\atomberg_send_command.py --ip 192.168.0.13 --speed-delta -1
```

Targeting by MAC equivalents:

```powershell
python .\atomberg_send_command.py --mac 10B41D181E58 --power on
python .\atomberg_send_command.py --mac 10B41D181E58 --power off
python .\atomberg_send_command.py --mac 10B41D181E58 --led off
python .\atomberg_send_command.py --mac 10B41D181E58 --speed-delta -1
```

---

## Read current state locally (UDP broadcast)

### How local state works
Per Atomberg docs: **after the fan receives a command and takes action**, it broadcasts a **state update** on UDP **5625**.

The state payload is typically a **HEX-encoded JSON string**, which includes `state_string`.  
The first field of `state_string` (an integer) encodes key bits like **power**, **speed**, **LED**, **sleep**, **timer**.

### Step 3 — Listen for state updates

```powershell
python .\atomberg_listen_state.py --seconds 8
```

If you don’t receive updates, it often means **no recent state change happened** during the listen window.

### Reliable way: listen + “poke” (re-send a command while listening)

This re-sends speed 3 while listening, which triggers the fan to broadcast its state:

```powershell
python .\atomberg_listen_state.py --seconds 8 --poke-ip 192.168.0.13 --poke-speed 3 --poke-after 1
```

Same, but poke by MAC (recommended for multiple fans):

```powershell
python .\atomberg_listen_state.py --seconds 8 --poke-mac 10B41D181E58 --poke-speed 3 --poke-after 1
```

You can also filter state output by MAC/device_id:

```powershell
python .\atomberg_listen_state.py --seconds 8 --mac 10B41D181E58
```

---

## Cloud API option (read state + send speed)

If you prefer cloud APIs, Atomberg provides:
- Base server: `https://api.developer.atomberg-iot.com`
- Token endpoint: `/v1/get_access_token`
- Device list: `/v1/get_list_of_devices` (to get `device_id`)
- Read state: `/v1/get_device_state`
- Send command: `/v1/send_command`

You need:
- **API Key**
- **Refresh token**
from Atomberg Home app developer options.

### Cloud flow (high level)
1. Call **Get Access Token** using your API key + refresh token (token valid ~24h).
2. Call **Get List of Devices** to get the `device_id` for your fan.
3. Read state via **Get Device State** (by `device_id`).
4. Set speed via **Send Command** using command `{"speed": <1..6>}`.

### Notes
- Cloud APIs can return common statuses: **200, 401, 403, 404, 429** (see overview docs).
- If you poll state too frequently you may hit rate limits; local UDP state listening avoids that.

---

## Troubleshooting

- **No beacons on 5625**:
  - Confirm PC and fan are on the same Wi‑Fi/LAN
  - Allow inbound UDP 5625 in Windows Firewall
  - Some routers block broadcast across SSIDs/VLANs (guest network isolation)

- **Commands send but nothing happens**:
  - Re-check fan IP (DHCP changes); rediscover via beacons
  - Ensure you’re sending to **UDP port 5600**

- **No state updates**:
  - State updates are typically sent **after a command/state change**
  - Use `--poke-ip ...` to force a state broadcast during the listening window


