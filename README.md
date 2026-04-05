# Android Device Manager

A single Python script to manage Android devices via ADB:  
**USB ↔ TCP/IP switching**, charging control, and **scrcpy** mirroring.

---

## Requirements

| Tool | Install |
|------|---------|
| Python 3.9+ | built-in on most systems |
| `adb` | [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) |
| `scrcpy` | [github.com/Genymobile/scrcpy](https://github.com/Genymobile/scrcpy) |

### Quick install (macOS/Linux)

```bash
# macOS
brew install android-platform-tools scrcpy

# Ubuntu / Debian
sudo apt install adb scrcpy

# Windows — download Platform Tools zip + scrcpy zip, add both to PATH
```

---

## Recent fixes

- Added terminal color auto-detection so output stays readable on unsupported consoles.
- Improved Wi-Fi profile editing: existing profile values are now pre-filled when editing.
- Fixed open-network CLI handling for `wifi connect --security Open`.
- Saved Wi-Fi profile file permissions are now restricted only on POSIX platforms.

---

## Usage

### Interactive TUI (recommended)

```bash
python android_manager.py
```

You'll see a live device table and an action menu:

```
  Actions:
    [1] Select device → TCP/IP mode (disable charging)
    [2] Select device → Back to USB mode
    [3] Launch scrcpy on selected device
    [4] Launch scrcpy on ALL devices
    [5] Refresh device list
    [q] Quit
```

---

### Non-interactive CLI

```bash
# List all connected devices
python android_manager.py list

# Switch a specific USB device to Wi-Fi (TCP/IP)
python android_manager.py tcpip -s <USB_SERIAL>

# Switch back to USB mode
python android_manager.py usb -s <USB_SERIAL_OR_TCP_IP:PORT>

# Mirror all devices with scrcpy
python android_manager.py mirror

# Mirror one device
python android_manager.py mirror -s <SERIAL>

# Enable TCP/IP without opening scrcpy
python android_manager.py tcpip -s <SERIAL> --no-scrcpy

# Custom port (default 5555)
python android_manager.py tcpip -s <SERIAL> -p 5556
```

---

## How it works

### TCP/IP mode (Wi-Fi)

1. Runs `adb tcpip <port>` on the USB-connected device  
2. Detects the device's Wi-Fi IP via `ip addr` / `ip route` / `getprop`  
3. Runs `adb connect <ip>:<port>`  
4. Disables charging via sysfs (`/sys/class/power_supply/battery/charging_enabled`)  
   so the device **does not charge** even though the cable is physically plugged in  
5. Launches scrcpy over the TCP/IP connection (no USB required for mirroring)

### USB mode (restore)

1. Re-enables charging via sysfs  
2. Runs `adb disconnect <ip>:<port>`  
3. Runs `adb usb` on the device to restart ADB in USB mode  

State (USB serial → TCP serial mapping) is saved to `~/.android_manager_state.json`  
so you can switch back to USB mode even in a new terminal session.

---

## Charging disable — notes

The charging disable feature writes `0` to sysfs nodes:

```
/sys/class/power_supply/battery/charging_enabled
/sys/class/power_supply/battery/battery_charging_enabled
/sys/class/power_supply/usb/charge_enable
```

These paths exist on most stock Android devices (Samsung, Google Pixel, OnePlus, etc.)  
**without root**. If none are found, a `dumpsys battery` fallback is tried.  
If none work, a warning is shown — TCP/IP still works, only charging control is skipped.

---

## Device table columns

| Column   | Meaning |
|----------|---------|
| `#`      | Selection index |
| `Model`  | Device model name |
| `Serial` | ADB serial (USB) or `ip:port` (TCP/IP) |
| `IP`     | Current Wi-Fi IP |
| `Connect`| `USB` or `WIFI` |
| `Battery`| Current battery % |
| `Charging`| Charging source or `disabled` |

---

## Troubleshooting

**"No devices found"**  
→ Enable *USB Debugging* in Developer Options on the device  
→ Accept the ADB authorization dialog on the device  
→ Run `adb kill-server && adb start-server`

**"Could not determine IP"**  
→ Make sure the device is connected to Wi-Fi (not just mobile data)

**"Charging still on after disabling"**  
→ The device may need root for sysfs write access  
→ Some OEMs block this path; TCP/IP still works normally

**scrcpy shows "server not found"**  
→ Wait 1-2 seconds after TCP/IP connect, then retry  
→ Check firewall isn't blocking port 5555
